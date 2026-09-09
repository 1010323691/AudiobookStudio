"""Script-generation engine — LLM → JSON (port of source ``app/generate_script.py``).

Migrates the source project's "call LLM → build prompt → generate JSON → parse/repair
JSON" pipeline into the app, so the "文本解析" page can turn novel text into the
``{speaker, text, instruct}`` entries that the (already-working) local TTS engine
consumes directly. The JSON clean / repair / salvage / chunk-splitting logic is a
faithful 1:1 port (sliced byte-for-byte from the source); the only adaptation is the
transport: the source called an OpenAI-compatible endpoint via the ``openai`` SDK,
which the dependency-lean 3.14 backend does not install, so the request is issued with
stdlib ``urllib`` — a byte-identical body and headers (see ``_llm_chat_completion``).

``generate`` is a Task worker (first arg is a :class:`TaskHandle`); it streams
per-chunk progress and logs over SSE and honours cooperative cancel between chunks.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from ..core.config import GenerationConfig, LLMConfig, PromptsConfig
from ..core.paths import get_layout
from .script_prompts import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT

IMPLEMENTED = True

HTTP_TIMEOUT = 300  # seconds — LLM calls can be slow on large chunks / CPU models


# ---------------------------------------------------------------------------
# Pure JSON / text helpers (1:1 ports of the source; spliced in by the build step so
# their regex backslashes are never re-typed).
# ---------------------------------------------------------------------------
def clean_json_string(text):
    """Clean and extract valid JSON array from LLM response."""
    # Remove thinking tags (various formats used by different models)
    # GLM, DeepSeek, Qwen, etc. use different thinking tag formats
    text = re.sub(r'<think>[\s\S]*?</think>', '', text)
    text = re.sub(r'<thinking>[\s\S]*?</thinking>', '', text)
    text = re.sub(r'<reflection>[\s\S]*?</reflection>', '', text)
    text = re.sub(r'<reasoning>[\s\S]*?</reasoning>', '', text)
    # Handle unclosed thinking tags (model started thinking but didn't close)
    text = re.sub(r'<think>[\s\S]*$', '', text)
    text = re.sub(r'<thinking>[\s\S]*$', '', text)

    # Remove markdown code blocks
    if "```" in text:
        # Find content between ```json and ``` or just ``` and ```
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            text = match.group(1).strip()

    # Find the JSON array - match from first [ to its closing ]
    # Use a bracket counter to find the correct closing bracket
    start = text.find('[')
    if start == -1:
        return None

    bracket_count = 0
    end = -1
    in_string = False
    escape_next = False

    for i, char in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if char == '\\':
            escape_next = True
            continue
        if char == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == '[':
            bracket_count += 1
        elif char == ']':
            bracket_count -= 1
            if bracket_count == 0:
                end = i + 1
                break

    if end == -1:
        # No closing bracket found, try to salvage
        last_complete = text.rfind('},')
        if last_complete > start:
            return text[start:last_complete+1] + ']'
        return None

    json_text = text[start:end]

    # Clean control characters inside strings (common LLM issue)
    # Replace literal newlines/tabs inside JSON strings with escaped versions
    def fix_control_chars(match):
        s = match.group(0)
        # Replace unescaped control characters
        s = s.replace('\n', '\\n')
        s = s.replace('\r', '\\r')
        s = s.replace('\t', '\\t')
        return s

    # Fix control characters inside string values
    json_text = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', fix_control_chars, json_text)

    return json_text


def repair_json_array(json_text, log=None):
    """Attempt to repair common JSON array issues from LLM output."""
    if not json_text:
        return None

    def _filter_entries(lst):
        """Keep only dict entries; LLMs sometimes emit bare strings in the array."""
        filtered = [e for e in lst if isinstance(e, dict)]
        if len(filtered) < len(lst) and log:
            log(f"Dropped {len(lst) - len(filtered)} non-object entries from LLM JSON array")
        return filtered if filtered else None

    # Try parsing as-is first
    try:
        result = json.loads(json_text)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 1: Add missing commas between objects (}\s*{" -> },\n{")
    fixed = re.sub(r'\}\s*\{', '},\n{', json_text)
    try:
        result = json.loads(fixed)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 2: Remove trailing commas before ]
    fixed = re.sub(r',\s*\]', ']', fixed)
    try:
        result = json.loads(fixed)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 3: Try to extract individual entries and rebuild
    entries = []
    # Match individual JSON objects
    pattern = r'\{\s*"speaker"\s*:\s*"[^"]*"\s*,\s*"text"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*"instruct"\s*:\s*"(?:[^"\\]|\\.)*"\s*\}'
    matches = re.findall(pattern, json_text, re.DOTALL)

    for match in matches:
        try:
            entry = json.loads(match)
            entries.append(entry)
        except json.JSONDecodeError:
            continue

    if entries:
        return entries

    # Fix 4: Last resort - find last complete entry and truncate
    last_complete = json_text.rfind('},')
    if last_complete > 0:
        try:
            truncated = json_text[:last_complete+1] + ']'
            # Ensure it starts with [
            if not truncated.strip().startswith('['):
                truncated = '[' + truncated
            result = json.loads(truncated)
            if isinstance(result, list):
                return _filter_entries(result)
        except json.JSONDecodeError:
            pass

    return None

def salvage_json_entries(json_text):
    """Last resort: extract individual valid entries with regex."""
    entries = []
    # Match individual JSON objects with speaker, text, instruct fields
    pattern = r'\{\s*"speaker"\s*:\s*"([^"]*)"\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"instruct"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
    matches = re.finditer(pattern, json_text, re.DOTALL)

    for match in matches:
        try:
            entry = {
                "speaker": match.group(1),
                "text": match.group(2).replace('\\"', '"').replace('\\n', '\n'),
                "instruct": match.group(3).replace('\\"', '"').replace('\\n', '\n')
            }
            entries.append(entry)
        except Exception:
            continue

    return entries if entries else None


def fix_mojibake(text):
    """Fix common mojibake characters resulting from CP1252-as-UTF8."""
    replacements = {
        'â€™': ''',  # Right single quote
        'â€˜': ''',  # Left single quote
        'â€œ': '"',  # Left double quote
        'â€\x9d': '"', # Right double quote
        'â€?': '"', # Sometimes ? if undefined
        'â€"': '—',  # Em dash
        'â€"': '–',  # En dash
        'â€¦': '…',  # Ellipsis
    }

    for bad, good in replacements.items():
        text = text.replace(bad, good)

    return text

def split_into_chunks(text, max_size=3000):
    """Split text into chunks at paragraph/sentence boundaries."""
    paragraphs = re.split(r'\n\s*\n', text)

    chunks = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current_chunk) + len(para) + 2 > max_size:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""

            if len(para) > max_size:
                sentences = re.split(r'(?<=[.!?])\s+', para)
                for sentence in sentences:
                    if len(current_chunk) + len(sentence) + 1 > max_size:
                        if current_chunk:
                            chunks.append(current_chunk.strip())
                        current_chunk = sentence
                    else:
                        current_chunk += " " + sentence if current_chunk else sentence
            else:
                current_chunk = para
        else:
            current_chunk += "\n\n" + para if current_chunk else para

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks


# ---------------------------------------------------------------------------
# LLM transport (stdlib urllib — no third-party HTTP client in the 3.14 backend).
# ---------------------------------------------------------------------------

def _llm_chat_completion(base_url, api_key, model, messages,
                         temperature, top_p, presence_penalty, max_tokens,
                         top_k=0, min_p=0, banned_tokens=None):
    """Issue an OpenAI-compatible ``chat/completions`` POST with stdlib ``urllib``.

    Sends the same body/headers the ``openai`` SDK would for
    ``client.chat.completions.create(...)``: ``Authorization: Bearer <api_key>``, a JSON
    body of ``model`` / ``messages`` / sampling params, plus any non-zero ``extra_body``
    keys (``top_k`` / ``min_p`` / ``banned_tokens``) merged at the top level. Returns
    ``(content, finish_reason, usage)`` where ``usage`` is a dict
    ``{"prompt_tokens", "completion_tokens"}`` (or ``None``). Raises on HTTP / network /
    parse failure — the caller's retry loop handles it.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "presence_penalty": presence_penalty,
        "max_tokens": max_tokens,
    }
    # openai ``extra_body`` keys go at the top level, only when set (non-zero).
    if top_k:
        body["top_k"] = top_k
    if min_p:
        body["min_p"] = min_p
    if banned_tokens:
        body["banned_tokens"] = banned_tokens

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # Surface the server's message (rate limit, auth, model-not-found, ...) in the log.
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"LLM HTTP {e.code}: {detail}") from e

    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("LLM 响应缺少 choices。")
    first = choices[0]
    message = first.get("message") or {}
    content = (message.get("content") or "").strip()
    finish_reason = first.get("finish_reason")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = None
    return content, finish_reason, usage


# ---------------------------------------------------------------------------
# Per-chunk orchestration + the Task worker.
# ---------------------------------------------------------------------------

def process_chunk(handle, llm, model_name, chunk, chunk_num, total_chunks,
                  previous_entries=None, max_retries=2,
                  system_prompt=None, user_prompt_template=None,
                  max_tokens=4096, temperature=0.6, top_p=0.8, top_k=0, min_p=0,
                  presence_penalty=0.0, banned_tokens=None):
    """Process one text chunk via the LLM and return its JSON script entries.

    Faithful port of the source ``process_chunk``: build the cross-chunk context
    (Part N/M header + character roster + last-3 entries), fill the *user* template
    via ``.format(context=..., chunk=...)`` (the system prompt is used verbatim),
    then call the LLM up to ``max_retries + 1`` times, cleaning / repairing / salvaging
    the JSON each attempt. Progress is logged through the Task ``handle``.
    """
    sys_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    usr_template = user_prompt_template or DEFAULT_USER_PROMPT

    context_parts = []
    if chunk_num == 1:
        context_parts.append("(Beginning of text)")
    elif chunk_num == total_chunks:
        context_parts.append("(End of text)")
    else:
        context_parts.append(f"(Part {chunk_num} of {total_chunks})")

    if previous_entries and len(previous_entries) > 0:
        # Build a character roster for name consistency across chunks.
        characters_seen = sorted(set(
            entry.get("speaker", "") for entry in previous_entries
            if entry.get("speaker", "") and entry.get("speaker", "") != "NARRATOR"
        ))
        if characters_seen:
            context_parts.append(f"Characters in this book: {', '.join(characters_seen)}")
        # Include the last few entries so the model keeps style / tone continuity.
        tail = previous_entries[-3:]
        context_parts.append("\nPrevious section ended with:")
        for entry in tail:
            context_parts.append(json.dumps(entry, ensure_ascii=False))

    context = "\n".join(context_parts)
    user_prompt = usr_template.format(context=context, chunk=chunk)

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    for attempt in range(max_retries + 1):
        try:
            text, finish_reason, usage = _llm_chat_completion(
                llm.base_url, llm.api_key, model_name, messages,
                temperature=temperature, top_p=top_p,
                presence_penalty=presence_penalty, max_tokens=max_tokens,
                top_k=top_k, min_p=min_p, banned_tokens=banned_tokens,
            )
            pt = usage.get("prompt_tokens", "?") if usage else "?"
            ct = usage.get("completion_tokens", "?") if usage else "?"
            handle.log(f"chunk {chunk_num}/{total_chunks} attempt {attempt + 1}: "
                       f"finish_reason={finish_reason} | tokens prompt={pt} completion={ct}")
            if finish_reason == "length":
                handle.log(f"WARNING: 响应被截断（达到 max_tokens={max_tokens}），可增大 max_tokens。", "WARNING")
        except Exception as e:  # noqa: BLE001 — a failed call retries, then gives up
            handle.log(f"调用 LLM API 出错（attempt {attempt + 1}）：{e}", "ERROR")
            if attempt < max_retries:
                continue
            return []

        # Clean and extract JSON from the response.
        json_text = clean_json_string(text)

        if not json_text:
            handle.log(f"chunk {chunk_num} 响应中未找到 JSON 数组（attempt {attempt + 1}）", "WARNING")
            if attempt < max_retries:
                handle.log("Retrying...")
                continue
            handle.log(f"Response preview: {text[:300]}...", "WARNING")
            return []

        # Try to parse, with repair attempts.
        entries = repair_json_array(json_text, log=lambda m: handle.log(m, "WARNING"))

        if entries and len(entries) > 0:
            if attempt > 0:
                handle.log(f"  Succeeded on retry {attempt + 1}")
            return entries

        handle.log(f"chunk {chunk_num} 响应无法解析为 JSON（attempt {attempt + 1}）", "WARNING")
        handle.log(f"JSON preview: {json_text[:300]}...", "WARNING")
        if attempt < max_retries:
            handle.log("Retrying with lower temperature...")

        # Last resort: extract individual valid entries with regex.
        salvaged_entries = salvage_json_entries(json_text)
        if salvaged_entries:
            handle.log(f"正则抢救出 {len(salvaged_entries)} 条 entries（来自畸形响应）")
            return salvaged_entries

    return []


def generate(handle, text, llm: LLMConfig, prompts: PromptsConfig, generation: GenerationConfig) -> dict:
    """Task worker: turn novel ``text`` into ``{speaker, text, instruct}`` entries.

    Contract: first arg is the :class:`TaskHandle``. Streams per-chunk progress and
    logs over SSE, honours cooperative cancel between chunks, and writes the result to
    ``03_parsed_json/annotated_script.json`` (served by the shared
    ``GET /api/files/download/03_parsed_json/{name}`` route). ``llm`` / ``prompts`` / ``generation``
    are the config section objects; empty ``prompts`` fall back to the bundled defaults.
    """
    body = (text or "").strip()
    if not body:
        raise RuntimeError("输入文本为空。")
    if not (llm.model_name or "").strip():
        raise RuntimeError("请先在「文本解析」页配置 LLM 模型名称（模型不能为空）。")

    body = fix_mojibake(body)
    handle.log(f"读入 {len(body)} 字")

    chunks = split_into_chunks(body, max_size=generation.chunk_size)
    total = len(chunks)
    if total == 0:
        raise RuntimeError("未从输入文本切分出任何片段。")
    handle.log(f"按段落/句子边界切分为 {total} 段（每段约 {generation.chunk_size} 字）")
    handle.log(f"模型：{llm.model_name} · 端点：{llm.base_url}")

    sys_prompt = prompts.system_prompt or DEFAULT_SYSTEM_PROMPT
    usr_template = prompts.user_prompt or DEFAULT_USER_PROMPT

    all_entries = []
    for i, chunk in enumerate(chunks, 1):
        handle.check()  # cooperative cancel / pause between chunks
        handle.log(f"处理第 {i}/{total} 段（{len(chunk)} 字）…")
        handle.progress((i - 1) / total, f"处理第 {i}/{total} 段")
        previous = all_entries if all_entries else None
        entries = process_chunk(
            handle, llm, llm.model_name, chunk, i, total,
            previous_entries=previous,
            system_prompt=sys_prompt,
            user_prompt_template=usr_template,
            max_tokens=generation.max_tokens,
            temperature=generation.temperature,
            top_p=generation.top_p,
            top_k=generation.top_k,
            min_p=generation.min_p,
            presence_penalty=generation.presence_penalty,
            banned_tokens=generation.banned_tokens,
        )
        all_entries.extend(entries)
        handle.log(f"  得到 {len(entries)} 条")

    if not all_entries:
        raise RuntimeError("未生成任何脚本条目。")

    out_path = get_layout().parsed_json / "annotated_script.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_entries, indent=2, ensure_ascii=False), encoding="utf-8")

    speakers = sorted({(e.get("speaker") or "UNKNOWN") for e in all_entries})
    handle.progress(1.0, "完成")
    handle.log(f"共生成 {len(all_entries)} 条；讲者：{', '.join(speakers)}")
    return {
        "entries": all_entries,
        "output_path": str(out_path),
        "count": len(all_entries),
        "speakers": speakers,
    }
