"""Speaker check engine — re-judge each batch of entries' speakers with a context window.

Reads the ORIGINAL parsed JSON (``<stem>.json`` in ``03_parsed_json/``) and processes it
in batches of ``BATCH_SIZE`` entries. Each batch is ONE LLM call: the window holds the
batch's target entries (each flagged ``"target": true``) flanked by ``±N`` context
entries (``N = speaker_check.context_window``), and the prompt states both the target
count and the context scope so the model knows what it may reason from. The LLM re-judges
EVERY target entry's ``speaker`` in that single call.

Per batch:
- **no disagreement** (the re-judged batch matches the originals) → accept and move on;
- **disagreement** → re-sample the SAME batch segment 3 times (never the wider context),
  then vote per discrepant entry: a speaker holding ≥2 of the 3 votes wins; an entry whose
  3 samples show no majority gets a 4th sample and is then resolved from all 4 (strict
  majority, else its original speaker is kept).

Only a target entry's ``speaker`` may change — ``text``/``instruct`` and every context
neighbour are untouched, and every window is built from the ORIGINAL speakers (a batch is
never judged against a partially-updated one). A NEW file ``<stem>_checked.json`` is
written once at the end; the original is never modified, and downstream stages read the
checked copy preferentially (``resolve_parsed_json``).

``check_file`` is a Task worker (first arg is the :class:`TaskHandle`), mirroring
``script.generate_file``'s concurrency: it holds ONE shared-gate slot for the whole file
(serial LLM calls within, parallel across files) and uses per-item failure isolation
(like ``voices.prepare``) — an entry whose calls can't be resolved keeps its original
speaker, so a single bad response can never abort (or corrupt) the whole file.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..core.config import GenerationConfig, LLMConfig, SpeakerCheckConfig
from ..core.concurrency import gate
from ..core.tasks import TaskCancelled
from . import check_prompts
from .script import _llm_chat_completion, _llm_chat_completion_stream


# How many entries are re-judged per LLM call. A batch whose re-judged speakers disagree
# with the originals is re-sampled (3×, then a 4× tie-break) — see ``check_file``.
BATCH_SIZE = 20


def build_batch_window(entries: list, start: int, size: int, n: int) -> list[dict]:
    """The context window for a batch of ``size`` target entries starting at ``start``.

    The target range ``entries[start .. start+size)`` (clamped to the file) is flagged
    ``"target": true``; ``n`` entries before ``start`` and ``n`` after the block (clamped)
    are context only (no target flag). Each item carries its absolute ``index``, the
    ORIGINAL ``speaker``, and the ``text`` (``instruct`` is dropped to keep the window
    compact — speaker judgment does not need the voice direction). Yields the union, in
    index order, so the LLM sees the full scope of what it may reason from.
    """
    total = len(entries)
    t_lo = max(0, start)
    t_hi = min(total, start + size)
    lo = max(0, start - n)
    hi = min(total, start + size + n)
    out = []
    for j in range(lo, hi):
        item = {
            "index": j,
            "speaker": entries[j].get("speaker", ""),
            "text": entries[j].get("text", ""),
        }
        if t_lo <= j < t_hi:
            item["target"] = True
        out.append(item)
    return out


def target_indices(start: int, size: int, total: int) -> list[int]:
    """The absolute indices of a batch's target entries, clamped to the file length."""
    return list(range(max(0, start), min(total, start + size)))


def _strip_thinking(text: str) -> str:
    """Drop thinking/reasoning tags (a Qwen3 thinking model may leak them into content)."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"<think>[\s\S]*$", "", text)
    for tag in ("thinking", "reflection", "reasoning"):
        text = re.sub(rf"<{tag}>[\s\S]*?</{tag}>", "", text)
        text = re.sub(rf"<{tag}>[\s\S]*$", "", text)
    return text


def _extract_balanced(text: str, start: int, opener: str, closer: str):
    """Parse the JSON value that begins at ``text[start]`` (an ``opener``), string/escape
    aware, counting only ``opener``/``closer`` depth. Returns the parsed value or ``None``.
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:  # noqa: BLE001
                    return None
    return None


def _extract_json(text: str):
    """The first top-level JSON value (object or array) in ``text``, else ``None``.

    Whichever bracket opens first wins, so a bare ``[...]`` reply is not mistaken for the
    ``{...}`` object nested inside it.
    """
    o = text.find("{")
    a = text.find("[")
    if o == -1 and a == -1:
        return None
    if a != -1 and (o == -1 or a < o):
        return _extract_balanced(text, a, "[", "]")
    return _extract_balanced(text, o, "{", "}")


def _clean_reply(text: str) -> str:
    """Strip markdown code fences and thinking/reasoning tags from an LLM reply."""
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    return _strip_thinking(text).strip()


def parse_speaker(text: str | None) -> str | None:
    """Extract the re-judged speaker from a single-entry LLM response; ``None`` if unreadable.

    A ``None`` result means "keep the entry's original speaker" (the caller's safe
    default). Order: strip thinking tags / code fences → read a JSON object's ``speaker``
    (or a one-element string array); if no object, accept a single short bare token (a
    lone word with no spaces, not shaped like JSON) so a model that replies ``ELENA``
    instead of ``{"speaker": "ELENA"}`` still lands correctly. Anything else → ``None``.
    """
    if not text:
        return None
    text = _clean_reply(text.strip())

    value = _extract_json(text)
    if isinstance(value, dict):
        sp = value.get("speaker")
        if isinstance(sp, str) and sp.strip():
            return sp.strip()
        return None  # an object was present but had no usable speaker → don't guess
    if isinstance(value, list) and len(value) == 1:
        if isinstance(value[0], str) and value[0].strip():
            return value[0].strip()

    candidate = text.strip()
    if (
        candidate
        and " " not in candidate
        and len(candidate) <= 32
        and not candidate.startswith(("[", "{"))
    ):
        return candidate
    return None


def _as_int(v):
    """``int(v)`` when ``v`` is an int or an int-like string, else ``None``."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_speaker_map(text: str | None, target_indices: list) -> dict:
    """Parse a batch LLM reply into ``{absolute_index: speaker}`` for the target entries.

    Robust to thinking tags / code fences and to the reply being a wrapped
    ``{"results": [...]}`` object, a bare ``[...]`` array, or an index-keyed object.
    Returns ``{}`` for anything unreadable — the caller then keeps the original speaker
    for those entries (per-item isolation). A single-target batch falls back to
    :func:`parse_speaker`.
    """
    targets = list(target_indices)
    if not targets or not text:
        return {}
    text = _clean_reply(text.strip())
    tset = set(targets)
    # Primary: the batch mapping ({"results": [...]}, a bare array, or an index-keyed object).
    value = _extract_json(text)
    if value is not None:
        m = _map_from_value(value, tset, targets)
        if m:
            return m
    # Single-target fallback: the scalar parser, so a reply shaped for one entry
    # ({"speaker": ...}, a bare token, or a one-element string array) still lands.
    if len(targets) == 1:
        sp = parse_speaker(text)
        if sp:
            return {targets[0]: sp}
    return {}


def _map_from_value(v, tset: set, targets: list) -> dict:
    """Extract ``{index: speaker}`` from a parsed JSON value (object or array)."""
    if isinstance(v, dict):
        # {"results" / "speakers" / ... : [ ... ]}
        for key in ("results", "speakers", "entries", "items", "list"):
            inner = v.get(key)
            if isinstance(inner, list):
                m = _map_from_list(inner, tset, targets)
                if m:
                    return m
        # index-keyed object: {"0": "NARRATOR", "3": "BOB"}
        m = {}
        for k, sp in v.items():
            idx = _as_int(k)
            if isinstance(sp, str) and sp.strip() and idx is not None and idx in tset:
                m[idx] = sp.strip()
        return m
    if isinstance(v, list):
        return _map_from_list(v, tset, targets)
    return {}


def _map_from_list(lst: list, tset: set, targets: list) -> dict:
    if not lst:
        return {}
    # form A: [{"index": .., "speaker": ..}, ...]
    if all(isinstance(x, dict) for x in lst):
        m = {}
        for x in lst:
            idx = _as_int(x.get("index"))
            sp = x.get("speaker")
            if idx is not None and isinstance(sp, str) and sp.strip() and idx in tset:
                m[idx] = sp.strip()
        if m:
            return m
    # form B: ["SPEAKER", ...] in target order
    if all(isinstance(x, str) for x in lst):
        m = {}
        for i, sp in enumerate(lst):
            if i < len(targets) and sp.strip():
                m[targets[i]] = sp.strip()
        if m:
            return m
    return {}


def _pick_majority(candidates):
    """The unique speaker holding a strict majority (≥2 votes) of ``candidates``, else ``None``.

    ``None`` entries (a re-run that failed to return a speaker for an entry) are ignored;
    a tie at the top — or fewer than 2 votes for the leader — yields ``None`` so the caller
    keeps the original speaker rather than guessing.
    """
    counts: dict = {}
    for c in candidates:
        if c:
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    if top < 2:
        return None
    leaders = [sp for sp, c in counts.items() if c == top]
    return leaders[0] if len(leaders) == 1 else None


def _llm_call(llm: LLMConfig, generation: GenerationConfig, messages, handle=None) -> str:
    """One chat-completion against the configured LLM (streaming or not); returns the text.

    Shares the parse pipeline's transport (which already handles a Qwen3 thinking model's
    ``reasoning_content``) and its sampling params, so the check makes the exact same kind
    of call the parse does — only the prompt differs.
    """
    if llm.stream:
        content, _fr, _usage = _llm_chat_completion_stream(
            llm.base_url, llm.api_key, llm.model_name, messages,
            temperature=generation.temperature, top_p=generation.top_p,
            presence_penalty=generation.presence_penalty,
            max_tokens=generation.max_tokens, top_k=generation.top_k,
            min_p=generation.min_p, banned_tokens=generation.banned_tokens,
            handle=handle,
        )
    else:
        content, _fr, _usage = _llm_chat_completion(
            llm.base_url, llm.api_key, llm.model_name, messages,
            temperature=generation.temperature, top_p=generation.top_p,
            presence_penalty=generation.presence_penalty,
            max_tokens=generation.max_tokens, top_k=generation.top_k,
            min_p=generation.min_p, banned_tokens=generation.banned_tokens,
        )
    return content


def _batch_user_prompt(template: str, context: str, size: int, n: int) -> str:
    """Fill the user template's ``{context}`` and append the context-scope note.

    ``replace`` (not ``format``) keeps a user-edited template containing other braces from
    raising; the appended note states the target count and the ``±n`` context scope so the
    model knows exactly what it may reason from (requirement: attach the context-window
    entry count).
    """
    base = template.replace("{context}", context)
    note = (
        f"\n\n【上下文范围】上方窗口含 {size} 个 target=true 的目标条目（请逐一给出 speaker），"
        f"目标块前后各有 {n} 条未标记 target 的上下文条目，仅供理解、不得改动。"
    )
    return base + note


def check_file(handle, path, llm: LLMConfig, check: SpeakerCheckConfig, generation: GenerationConfig) -> dict:
    """Task worker: re-judge every entry's speaker in batches → ``<stem>_checked.json``.

    Contract: first arg is the :class:`TaskHandle`; the second is the absolute path of the
    ORIGINAL ``<stem>.json`` in ``03_parsed_json/``. Entries are re-judged ``BATCH_SIZE``
    at a time (one LLM call each); a batch whose re-judged speakers disagree with the
    originals is re-sampled 3× (then a 4× tie-break) and resolved by majority. Concurrency
    is bounded by the shared gate (``generation.max_concurrency``) like parsing — one slot
    held for the whole file. Only a target entry's ``speaker`` may change; the original
    file is never modified (a new ``<stem>_checked.json`` is written once at the end).
    """
    # Fail fast on a misconfigured model *before* taking a concurrency slot.
    if not (llm.model_name or "").strip():
        raise RuntimeError("请先在「文本解析」页配置 LLM 模型名称（模型不能为空）。")

    src = Path(path)
    handle.progress(0.0, "排队中（等待并发槽位）")
    gate().acquire()
    try:
        if not src.is_file():
            raise RuntimeError(f"文件不存在：{src.name}")
        try:
            original = json.loads(src.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{src.name} 无法解析：{e}")
        if not isinstance(original, list) or not original:
            raise RuntimeError(f"{src.name} 为空——请先生成脚本。")
        if not all(isinstance(e, dict) for e in original):
            raise RuntimeError(f"{src.name} 含非对象条目，无法检查。")

        n = max(0, int(check.context_window or 0))
        total = len(original)
        # Shallow copies: only `speaker` may change, so `original` stays pristine and every
        # window (first pass + all re-runs) is built from the ORIGINAL speakers.
        result = [dict(e) for e in original]

        sys_prompt = check.system_prompt or check_prompts.DEFAULT_CHECK_SYSTEM_PROMPT
        usr_template = check.user_prompt or check_prompts.DEFAULT_CHECK_USER_PROMPT

        handle.log(f"读入 {src.name}（{total} 条）· 每批 {BATCH_SIZE} 条 · 上下文窗口 ±{n}")
        handle.log(f"模型：{llm.model_name} · 端点：{llm.base_url}")

        changed = 0
        rechecked = 0  # entries that went through re-sampling (disagreement resolution)
        proc_start = time.monotonic()
        window_chars = 0

        for start in range(0, total, BATCH_SIZE):
            size = min(BATCH_SIZE, total - start)
            targets = target_indices(start, size, total)
            # The window is built from the ORIGINAL entries and reused for every call in
            # this batch (first pass + re-runs) so each sample sees identical context.
            context = json.dumps(build_batch_window(original, start, size, n),
                                 ensure_ascii=False, indent=2)
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": _batch_user_prompt(usr_template, context, size, n)},
            ]

            handle.check()  # cooperative cancel / pause before the batch
            handle.progress(start / total, f"检查第 {start + 1}-{start + size} 条")
            handle.llm_rate(0.0)  # reset the 吞吐 gauge for this call
            handle.log(f"检查第 {start + 1}-{start + size} 条（{size} 条/批）…")

            # -- First pass: re-judge the whole batch in one call -------------------
            try:
                first_map = parse_speaker_map(_llm_call(llm, generation, messages, handle), targets)
            except TaskCancelled:
                raise  # a cancel raised mid-stream must propagate, not be swallowed
            except Exception as e:  # noqa: BLE001 — unreadable first pass → keep originals
                handle.log(f"  首批解析失败，本批保留原 speaker：{e}", "WARNING")
                first_map = {}

            # Discrepant targets: a valid re-judged speaker that differs from the original.
            discrepant = [
                t for t in targets
                if (sp := first_map.get(t)) is not None and sp != original[t].get("speaker")
            ]

            if discrepant:
                # -- Branch 2: disagreement → re-sample the SAME batch 3× (then 4×) --
                rechecked += len(discrepant)
                handle.log(f"  检测到 {len(discrepant)} 条分歧，重新解析本片段（3 次投票）…")
                samples = []  # one {index: speaker} map per re-run
                for run in range(1, 4):  # 共 3 次解析
                    handle.check()
                    handle.llm_rate(0.0)
                    handle.log(f"  重新解析第 {run}/3 次…")
                    try:
                        samples.append(parse_speaker_map(_llm_call(llm, generation, messages, handle), targets))
                    except TaskCancelled:
                        raise
                    except Exception as e:  # noqa: BLE001 — a failed re-run just adds no votes
                        handle.log(f"  重新解析第 {run}/3 次失败：{e}", "WARNING")
                        samples.append({})

                # Resolve each entry from its 3 samples; remember which ones stay in a tie.
                resolved: dict = {}
                need_fourth = []
                for t in discrepant:
                    w = _pick_majority([s.get(t) for s in samples])
                    resolved[t] = w
                    if w is None:
                        need_fourth.append(t)

                # A 4th sample, run once, for the entries still in a tie after 3.
                if need_fourth:
                    handle.check()
                    handle.llm_rate(0.0)
                    handle.log(f"  {len(need_fourth)} 条三次无共识，进行第 4 次解析…")
                    try:
                        samples.append(parse_speaker_map(_llm_call(llm, generation, messages, handle), targets))
                    except TaskCancelled:
                        raise
                    except Exception as e:  # noqa: BLE001
                        handle.log(f"  第 4 次解析失败：{e}", "WARNING")
                        samples.append({})
                    for t in need_fourth:
                        resolved[t] = _pick_majority([s.get(t) for s in samples])

                # Apply: adopt the majority only if it beats the original; else keep original.
                for t in discrepant:
                    winner = resolved[t]
                    orig_sp = original[t].get("speaker")
                    if winner is not None and winner != orig_sp:
                        result[t]["speaker"] = winner  # only `speaker` changes
                        changed += 1
                        handle.log(f"  {t + 1}: {orig_sp} → {winner}")
                    # winner None (no consensus) or == original → the original is kept.
            # (no disagreement → the batch matches the originals; nothing to change)

            # Advance the cumulative metrics + progress for both branches.
            window_chars += len(context)
            handle.llm_chars(window_chars, time.monotonic() - proc_start)
            handle.progress((start + size) / total, f"已检查 {start + size}/{total} 条")

        # Write the NEW file — the original ``<stem>.json`` is left byte-for-byte intact.
        out_path = src.with_name(src.stem + "_checked.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        speakers = sorted({(e.get("speaker") or "UNKNOWN") for e in result})
        handle.progress(1.0, "完成")
        handle.log(f"检查 {total} 条（{rechecked} 条经重判），{changed} 条 speaker 已更正 → {out_path.name}")
        return {
            "checked": total,
            "rechecked": rechecked,
            "changed": changed,
            "output_name": out_path.name,
            "output_path": str(out_path),
            "speakers": speakers,
            "input_name": src.name,
        }
    finally:
        gate().release()
