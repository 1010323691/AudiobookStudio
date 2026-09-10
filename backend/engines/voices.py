"""Character voice-prep engine (port of source ``app/generate_personas.py``).

Turns the parsed script (``03_parsed_json/<name>.json``) into a
``voice_config.json`` that the batch TTS stage consumes:

  1. detect every speaker in the script (in order of first appearance);
  2. (heuristic) fold obvious aliases of an already-configured character into it;
  3. for each remaining character, ask the LLM for a short voice ``description``
     + a ``ref_text`` sample (port of the persona prompts);
  4. render a per-character preview WAV via the worker's ``design`` mode and store it
     as a **clone** reference (``type: clone, ref_audio, ref_text``);
  5. on a preview failure, fall back to a **design** voice (``type: design``) so the
     character still gets a distinct, working voice.

The result: one click → ``JSON → 所有角色声音准备完成``. A single character's voice can
be regenerated in isolation via ``speakers`` (+ an optional ``description`` override)
without re-running the rest — requirement #2.

``prepare`` is a Task worker (first arg is a :class:`TaskHandle`); it streams rich,
meaningful progress/logs over SSE and honours cooperative cancel between characters.
A per-character failure is recorded (and that character falls back to ``design``) —
it never aborts the whole run; only a *fatal* error (no script, no engine) raises.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid

from ..core.config import get_config
from ..core.paths import ALL_PARSED_JSON, PROJECT_ROOT, get_layout, resolve_parsed_json, resolve_parsed_json_all
from .persona_prompts import PERSONA_SYSTEM_PROMPT, PERSONA_USER_PROMPT
from .tts import _child_env, resolve_engine

IMPLEMENTED = True

_NARRATOR_LABELS = frozenset({"NARRATOR", "NARRATION", "NARRATIVE"})


# ---------------------------------------------------------------------------
# Pure helpers (1:1 ports of generate_personas.py / project.py)
# ---------------------------------------------------------------------------

def extract_json_object(text):
    """Find and parse the first JSON object in ``text`` (port of the source)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    end = None
    for i, ch in enumerate(text[start:], start):
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
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(text[start:end])
    except Exception:  # noqa: BLE001
        return None


def normalize_speaker_name(name):
    if not isinstance(name, str):
        return ""
    s = name.strip().lower()
    s = re.sub(r"^(mr|mrs|ms|miss|dr|prof|sir|lady|lord)\.?\s+", "", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity on normalized name tokens."""
    norm_a, norm_b = normalize_speaker_name(a), normalize_speaker_name(b)
    if not norm_a or not norm_b:
        return 0.0
    tokens_a, tokens_b = set(norm_a.split()), set(norm_b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a.intersection(tokens_b)) / len(tokens_a.union(tokens_b))


def _resolve_to_canonical(raw_name: str, allowed, threshold=0.4):
    """Map a raw name to the closest canonical label, or None (exact → substring → Jaccard)."""
    if not raw_name:
        return None
    norm_raw = normalize_speaker_name(raw_name)
    if not norm_raw:
        return None
    for name in allowed:
        if normalize_speaker_name(name) == norm_raw:
            return name
    for name in allowed:
        norm_name = normalize_speaker_name(name)
        if norm_name and (norm_name in norm_raw or norm_raw in norm_name):
            return name
    best_name, best_score = None, 0.0
    for name in allowed:
        score = _token_jaccard(raw_name, name)
        if score > best_score:
            best_score, best_name = score, name
    return best_name if best_score >= threshold else None


def _entry_speaker(entry):
    return (entry.get("speaker") or entry.get("type") or "").strip()


def _entry_text(entry):
    return (entry.get("text") or "").strip()


def _collect_narrator_context(script, speaker, window=4):
    """Gather unique narrator lines near any appearance of ``speaker`` (port)."""
    context_lines, seen_lines = [], set()
    window = max(1, int(window or 4))
    speaker_indices = [i for i, e in enumerate(script) if _entry_speaker(e) == speaker]
    for idx in speaker_indices:
        for j in range(max(0, idx - window), min(len(script), idx + window + 1)):
            if j == idx:
                continue
            entry = script[j]
            if _entry_speaker(entry).upper() in _NARRATOR_LABELS:
                line = _entry_text(entry)
                if line and line not in seen_lines:
                    seen_lines.add(line)
                    context_lines.append(line)
                    if len(context_lines) >= window:
                        return context_lines
    return context_lines


def pick_ref_text(lines):
    """First line long enough to be a good clone reference; else the first non-empty line."""
    for ln in lines:
        if ln and len(ln.strip()) >= 12:
            return ln.strip()
    return next((ln.strip() for ln in lines if ln and ln.strip()), "")


def _fallback_persona(speaker, lines):
    """A minimal, always-valid persona if the LLM is unavailable or unparseable."""
    description = f"{speaker} has a clear, natural audiobook voice."
    return description, pick_ref_text(lines)


def _sanitize(name):
    return re.sub(r"[^\w\-]", "_", name or "unknown").lower()


# ---------------------------------------------------------------------------
# LLM + worker bridges
# ---------------------------------------------------------------------------

def _llm_persona(handle, llm, system, user_template, speaker, lines, narrator_ctx):
    """Ask the LLM for a character's voice ``description`` + ``ref_text``.

    Reuses the stdlib-urllib LLM channel from ``engines/script.py``. One retry, then
    the caller falls back to :func:`_fallback_persona`. Returns ``(description, ref_text)``.
    """
    from .script import _llm_chat_completion

    if not (llm.model_name or "").strip():
        raise RuntimeError("未配置 LLM 模型名称（在「文本解析」页填写模型）。")
    sample_text = "\n".join(lines[:8])
    intro = "\n".join(narrator_ctx) if narrator_ctx else "(No nearby narrator intro lines found.)"
    user_prompt = user_template.format(speaker=speaker, narrator_context=intro, sample_lines=sample_text)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    gen = get_config().generation
    for attempt in range(2):
        try:
            text, _finish, _usage = _llm_chat_completion(
                llm.base_url, llm.api_key, llm.model_name, messages,
                temperature=0.3, top_p=gen.top_p, presence_penalty=gen.presence_penalty,
                max_tokens=400, top_k=gen.top_k, min_p=gen.min_p,
                banned_tokens=gen.banned_tokens,
            )
        except Exception as e:  # noqa: BLE001 — a failed call retries, then falls back
            handle.log(f"  调用 LLM 出错（{speaker}, 第 {attempt + 1} 次）：{e}", "ERROR")
            continue
        parsed = extract_json_object(text)
        if parsed:
            desc = str(parsed.get("description", "") or "").strip()
            ref = str(parsed.get("ref_text", "") or "").strip()
            if desc:
                return desc, ref
        handle.log(f"  LLM 响应无法解析为 persona（第 {attempt + 1} 次）", "WARNING")
    return "", ""


def _design_preview(handle, description: str, ref_text: str, out_wav) -> str:
    """Render a per-character preview WAV via the worker's ``design`` mode.

    Spawns the isolated TTS env (``resolve_engine`` raises a clear error if it is not
    installed) and pumps its stdout into the task. Returns the produced file path;
    raises on failure so the caller can fall back to a ``design`` voice.
    """
    import subprocess

    python, worker = resolve_engine()
    cfg = get_config()
    t = cfg.tts
    out_wav = out_wav if isinstance(out_wav, str) else str(out_wav)
    layout = get_layout()
    layout.voice_profiles.mkdir(parents=True, exist_ok=True)

    # Long / non-ASCII description + sample go through UTF-8 files (robust on Windows).
    tmp = layout.temp
    desc_file = tmp / f"persona_{uuid.uuid4().hex[:10]}.desc"
    text_file = tmp / f"persona_{uuid.uuid4().hex[:10]}.txt"
    desc_file.write_text(description, encoding="utf-8")
    text_file.write_text(ref_text, encoding="utf-8")

    cmd = [
        str(python), str(worker),
        "--mode", "design",
        "--description-file", str(desc_file),
        "--text-file", str(text_file),
        "--out", out_wav,
        "--design-model", t.design_model or "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "--language", t.language or "chinese",
        "--device", t.device or "auto",
    ]
    if cfg.ffmpeg.ffmpeg_path:
        cmd += ["--ffmpeg", cfg.ffmpeg.ffmpeg_path]

    handle.log("  正在用 VoiceDesign 模型渲染预览…")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(PROJECT_ROOT), env=_child_env())
    except FileNotFoundError:
        # Popen failed — the child never ran, so clean up the input files, then re-raise.
        for p in (desc_file, text_file):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError(f"无法启动 TTS 引擎：{python}")

    # Read the child to completion, forwarding its lines as logs. The input files must
    # outlive the child (it reads them only after a slow torch import), so they are
    # removed *after* communicate() returns — never right after Popen (that raced ahead
    # of the child and caused a FileNotFoundError in _run_design).
    produced = ""
    try:
        out, err = proc.communicate(timeout=None)
    except Exception as e:  # noqa: BLE001
        proc.kill()
        raise RuntimeError(f"TTS 引擎异常：{e}")
    finally:
        for p in (desc_file, text_file):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    for line in out.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[result]"):
            produced = line[len("[result]"):].strip()
        elif line.startswith("[progress]"):
            parts = line.split(None, 2)
            try:
                frac = float(parts[1])
            except (ValueError, IndexError):
                frac = 0.0
            handle.progress(min(frac, 1.0), parts[2] if len(parts) > 2 else "渲染预览")
        else:
            handle.log(line)
    for line in err.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            handle.log(line, "WARNING")

    if proc.returncode != 0:
        tail = " | ".join(err.decode("utf-8", "replace").splitlines())[-300:]
        raise RuntimeError(f"预览生成失败（退出码 {proc.returncode}）" + (f"：{tail}" if tail else ""))
    if not produced or not os.path.exists(produced):
        raise RuntimeError("引擎报告成功，但未找到预览文件。")
    return produced


# ---------------------------------------------------------------------------
# The Task worker
# ---------------------------------------------------------------------------

def prepare(handle, speakers=None, new_only=False, overrides=None, script_name=None) -> dict:
    """Task worker: prepare voices for every (selected) character in the script.

    Contract: first arg is the :class:`TaskHandle``. ``speakers`` is an optional
    allowlist (for regenerating a subset); ``new_only`` skips characters already in
    ``voice_config.json``; ``overrides`` maps a speaker → a user-supplied description
    (skips the LLM for that character); ``script_name`` selects which parsed JSON in
    ``03_parsed_json/`` to read (None → the most recent one). Returns a summary for the UI.
    """
    overrides = overrides or {}

    # 1. Load the parsed script(s): the chosen file, the most recent one, or ALL of them.
    if script_name == ALL_PARSED_JSON:
        # Whole-book aggregate: concatenate every 分册 (each upgraded to its _checked
        # copy) in reading order. An unreadable/empty file is skipped — the run continues.
        script_paths = resolve_parsed_json_all()
        if not script_paths:
            raise RuntimeError("未找到脚本 JSON（03_parsed_json/）——请先在「文本解析」生成脚本。")
        script = []
        for sp in script_paths:
            if not sp.exists():
                continue
            try:
                data = json.loads(sp.read_text("utf-8"))
            except Exception as e:  # noqa: BLE001 — skip an unreadable file, keep going
                handle.log(f"{sp.name} 无法解析（{e}），已跳过。", "WARNING")
                continue
            if isinstance(data, list):
                script.extend(data)
                handle.log(f"读入 {sp.name}：{len(data)} 条")
        if not script:
            raise RuntimeError("所有脚本 JSON 均为空——请先生成脚本。")
        handle.log(f"读入全部 {len(script_paths)} 个脚本：共 {len(script)} 条")
    else:
        script_path = resolve_parsed_json(script_name)
        if not script_path.exists():
            raise RuntimeError("未找到脚本 JSON（03_parsed_json/）——请先在「文本解析」生成脚本。")
        try:
            script = json.loads(script_path.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{script_path.name} 无法解析：{e}")
        if not isinstance(script, list) or not script:
            raise RuntimeError(f"{script_path.name} 为空——请先生成脚本。")
        handle.log(f"读入脚本 {script_path.name}：{len(script)} 条")

    # 2. Collect sample lines per speaker, in order of first appearance.
    samples: dict = {}
    order: list = []
    for entry in script:
        sp = _entry_speaker(entry)
        if not sp:
            continue
        if sp not in samples:
            samples[sp] = []
            order.append(sp)
        samples[sp].append(_entry_text(entry))
    handle.log(f"检测到 {len(order)} 个角色：{'、'.join(order)}")

    # 3. Narrator-intro context per character (feeds the persona prompt).
    narrator_ctx = {sp: _collect_narrator_context(script, sp, window=4) for sp in order}

    # 4/5. LLM + persona prompts.
    cfg = get_config()
    llm = cfg.llm
    pp = cfg.persona_prompts
    persona_system = pp.system_prompt or PERSONA_SYSTEM_PROMPT
    persona_user = pp.user_prompt or PERSONA_USER_PROMPT
    if not (llm.model_name or "").strip() and not overrides:
        handle.log("警告：未配置 LLM 模型——未提供提示词的角色将使用兜底描述。", "WARNING")

    # 6. Load the existing voice config (preserve any hand-edited entries).
    vc_path = get_layout().voice_profiles / "voice_config.json"
    voice_config = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception as e:  # noqa: BLE001
            handle.log(f"现有 voice_config.json 无法解析（{e}），将重建。", "WARNING")

    # 7. Select which characters to process this run.
    selected = list(order)
    if new_only:
        selected = [s for s in selected if s not in voice_config]
    if speakers:
        allow = {s for s in speakers if s}
        selected = [s for s in selected if s in allow]
    if not selected:
        handle.log("没有需要处理的角色。")
        return {"count": 0, "aliases": 0, "speakers": order,
                "voice_config_path": str(vc_path), "results": []}

    handle.log(f"本次处理 {len(selected)} 个角色。")

    # 8. Heuristic alias fold: a label that clearly matches an existing character
    #    becomes a pointer (``alias_of``) to it instead of getting a new voice.
    resolved_aliases: dict = {}
    unique_speakers: list = []
    for sp in selected:
        existing = [n for n in voice_config.keys() if n != sp]
        alias = ""
        norm_self = normalize_speaker_name(sp)
        for cand in existing:
            if norm_self and normalize_speaker_name(cand) == norm_self:
                alias = cand
                break
        if not alias and existing:
            alias = _resolve_to_canonical(sp, existing, threshold=0.8)
        if alias:
            handle.log(f"识别到别名：{sp} → {alias}（复用其声音）")
            entry = voice_config.get(sp, {})
            entry.update({"alias_of": alias, "seed": entry.get("seed", -1)})
            voice_config[sp] = entry
            resolved_aliases[sp] = alias
        else:
            unique_speakers.append(sp)

    # 9. Generate a persona + design preview for each unique character.
    n = len(unique_speakers)
    results = []
    for i, sp in enumerate(unique_speakers, 1):
        handle.check()  # cooperative cancel / pause between characters
        lines = samples.get(sp, [])
        handle.progress(i / (n or 1), f"[{i}/{n}] 正在生成角色：{sp}")
        handle.log(f"[{i}/{n}] 角色 {sp}（{len(lines)} 句样本）")

        # Description + ref text: an override wins, else the LLM, else a fallback.
        description = (overrides.get(sp) or "").strip()
        ref_text = ""
        if description:
            handle.log(f"  使用自定义提示词：{description[:60]}")
            ref_text = pick_ref_text(lines)
        else:
            try:
                description, ref_text = _llm_persona(
                    handle, llm, persona_system, persona_user, sp, lines, narrator_ctx.get(sp, []),
                )
            except Exception as e:  # noqa: BLE001
                handle.log(f"  LLM 生成描述失败：{e}（改用兜底）", "WARNING")
                description, ref_text = "", ""
        if not description:
            description, ref_text = _fallback_persona(sp, lines)
            handle.log("  使用兜底描述。", "WARNING")
        if not ref_text:
            ref_text = pick_ref_text(lines) or f"{sp} speaks in a clear, natural voice."

        # Render the preview WAV; on success store a clone reference, else a design voice.
        out_wav = str(get_layout().voice_profiles / "designed_voices" /
                      f"{_sanitize(sp)}_{time.time_ns()}.wav")
        try:
            produced = _design_preview(handle, description, ref_text, out_wav)
            entry = voice_config.get(sp, {})
            entry.update({
                "type": "clone",
                "ref_audio": produced,  # absolute path (the worker reads it directly)
                "ref_text": ref_text,
                "description": description,
                "character_style": description,
                "seed": entry.get("seed", -1),
            })
            voice_config[sp] = entry
            handle.log(f"  [{i}/{n}] {sp} 声音就绪（克隆）。")
            results.append({"speaker": sp, "ok": True, "type": "clone",
                            "preview": produced, "description": description})
        except Exception as e:  # noqa: BLE001 — this character falls back; the run continues
            handle.log(f"  [{i}/{n}] {sp} 预览生成失败：{e}（改用 design 兜底）", "ERROR")
            entry = voice_config.get(sp, {})
            entry.update({"type": "design", "description": description, "ref_text": ref_text})
            voice_config[sp] = entry
            results.append({"speaker": sp, "ok": False, "type": "design",
                            "preview": "", "description": description})

    # 10. Persist the voice config.
    vc_path.parent.mkdir(parents=True, exist_ok=True)
    vc_path.write_text(json.dumps(voice_config, indent=2, ensure_ascii=False), encoding="utf-8")
    handle.log(f"voice_config 已保存：{vc_path}")

    handle.progress(1.0, "完成")
    handle.log(f"所有角色声音准备完成：{len(unique_speakers)} 个新角色 + {len(resolved_aliases)} 个别名。")
    return {
        "count": len(unique_speakers),
        "aliases": len(resolved_aliases),
        "speakers": order,
        "voice_config_path": str(vc_path),
        "output_dir": str(get_layout().voice_profiles / "designed_voices"),
        "results": results,
    }
