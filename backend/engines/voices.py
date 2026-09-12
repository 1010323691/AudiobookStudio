"""Character voice-prep engine (port of source ``app/generate_personas.py``).

Splits voice preparation into two independent phases so the LLM and the TTS engine
never share the GPU at once (each can then run at its own max concurrency):

**Phase 1 — ``prepare_foundations`` (LLM only, no TTS).** Detects every speaker in
the parsed script (in order of first appearance), folds obvious aliases into an
existing character, then — in parallel, bounded by ``generation.max_concurrency`` —
asks the LLM for each character's voice *foundation*: a ``description`` + a
multi-sentence ``ref_text`` seed, reasoned from the character's own lines sampled
across the book (front / middle / back), each carrying its ±window local context
(surrounding narration and other characters). The foundation is persisted to
``voice_config.json`` as ``type: "foundation"``. No TTS runs in this phase.

**Phase 2 — ``make_clones`` (TTS only, no LLM).** Reads back the persisted
foundations and — in parallel, up to the caller's ``concurrency`` — renders each
character's clone seed WAV via the worker's ``design`` mode, storing it as a **clone**
reference (``type: clone, ref_audio, ref_text``). On a render failure it falls back to
a **design** voice (``type: design``) so the character still gets a working voice.
On cancel every in-flight TTS child process is killed so GPU memory is freed at once.

The book's per-line synthesis remains the separate 音频合成 (``tts_batch``) stage, which
consumes the finished ``voice_config.json``. A single character's foundation / clone can
be (re)generated in isolation via ``speakers`` (+ an optional ``description`` override).

Both workers are Task workers (first arg is a :class:`TaskHandle`); each streams rich
progress/logs over SSE and honours cooperative cancel between characters. A per-character
failure is recorded (and, in phase 2, that character falls back to ``design``) — it never
aborts the whole run; only a *fatal* error (no script, no engine) raises.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core.config import get_config
from ..core.paths import ALL_PARSED_JSON, PROJECT_ROOT, get_layout, resolve_parsed_json, resolve_parsed_json_all
from .persona_prompts import PERSONA_SYSTEM_PROMPT, PERSONA_USER_PROMPT
from .tts import _child_env, resolve_engine

IMPLEMENTED = True

# How much local context (entries, on each side) is attached to each sampled target
# line, and how many target lines are drawn from each of the front / middle / back bands.
CONTEXT_WINDOW = 4
SAMPLES_PER_BAND = 8


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


def _select_target_bands(pairs, per=SAMPLES_PER_BAND):
    """Split a character's own lines into (front, middle, back) bands of script indices.

    ``pairs`` is a list of ``(script_index, text)`` for the character's lines, in script
    order. ``front`` = the first ``per``; ``back`` = the last ``per``; ``middle`` =
    ``per`` lines spread evenly through the middle region. When the character has too few
    lines to fill three bands, all of them are returned as ``front`` (none dropped), so a
    small cast still gets every line with its context. Returns three lists of indices.
    """
    n = len(pairs)
    if n == 0:
        return [], [], []
    idxs = [i for i, _t in pairs]
    if n <= 3 * per:
        return idxs, [], []
    front = idxs[:per]
    back = idxs[-per:]
    region = idxs[per:n - per]
    if len(region) <= per:
        middle = list(region)
    else:
        step = (len(region) - 1) / (per - 1)
        middle = [region[round(k * step)] for k in range(per)]
    return front, middle, back


def _window_block(script, idx, window=CONTEXT_WINDOW):
    """One target line with its ±``window`` surrounding entries (any speaker) as a block.

    The surrounding entries keep script order; the target line is marked ``★`` and the
    rest are indented. Entries with empty text are dropped, and the window clamps at the
    start / end of the script (fewer than ``window`` neighbours are taken when present).
    """
    total = len(script)
    lo = max(0, idx - window)
    hi = min(total, idx + window + 1)
    lines = []
    for j in range(lo, hi):
        txt = _entry_text(script[j])
        if not txt:
            continue
        spk = _entry_speaker(script[j]) or "(?)"
        marker = "★ " if j == idx else "   "
        lines.append(f"{marker}{spk}: {txt}")
    return "\n".join(lines)


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

def _llm_persona(handle, llm, system, user_template, speaker, script, bands):
    """Ask the LLM for a character's voice ``description`` + a ``ref_text`` seed.

    Reuses the stdlib-urllib LLM channel from ``engines/script.py``. The prompt feeds the
    character's own lines sampled across the book (front / middle / back) with each line's
    ±window local context (see :func:`_select_target_bands` / :func:`_window_block`), so
    the model judges the voice from the character's full range of delivery, not just the
    intro. One retry, then the caller falls back to :func:`_fallback_persona`. Returns
    ``(description, ref_text)``.
    """
    from .script import _llm_chat_completion

    if not (llm.model_name or "").strip():
        raise RuntimeError("未配置 LLM 模型名称（在「文本解析」页填写模型）。")

    front, middle, back = bands

    def band_lines(indices, title):
        if not indices:
            return []
        out = [f"【{title}】"]
        for k, idx in enumerate(indices, 1):
            out.append(f"── 台词 {k} ──")
            out.append(_window_block(script, idx, CONTEXT_WINDOW))
        out.append("")
        return out

    parts = []
    parts += band_lines(front, f"开场 · {speaker} 的前 {len(front)} 句台词")
    parts += band_lines(middle, f"中段 · {speaker} 的中 {len(middle)} 句台词")
    parts += band_lines(back, f"结尾 · {speaker} 的后 {len(back)} 句台词")
    line_windows = "\n".join(parts).strip() or "（该角色没有可用的台词样本。）"

    # str.replace (not str.format) so a user-edited template holding other braces can't
    # raise; {line_windows} is filled before {speaker} (the windows already carry the name).
    user_prompt = (user_template
                   .replace("{line_windows}", line_windows)
                   .replace("{speaker}", speaker))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    gen = get_config().generation
    for attempt in range(2):
        try:
            # 1024 (not 400): the answer now carries a multi-sentence ref_text, and a
            # thinking model spends part of the budget on reasoning before the JSON.
            text, _finish, _usage = _llm_chat_completion(
                llm.base_url, llm.api_key, llm.model_name, messages,
                temperature=0.3, top_p=gen.top_p, presence_penalty=gen.presence_penalty,
                max_tokens=1024, top_k=gen.top_k, min_p=gen.min_p,
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


def _design_preview(handle, description: str, ref_text: str, out_wav, reg=None) -> str:
    """Render a per-character clone-seed WAV via the worker's ``design`` mode.

    Spawns the isolated TTS env (``resolve_engine`` raises a clear error if it is not
    installed) and pumps its stdout into the task. ``reg`` (an optional :class:`_ChildReg`)
    registers the live child process so a cancel can kill it; when omitted the child runs
    to completion (the original, non-parallel behaviour). Returns the produced file path;
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

    if reg is not None:
        reg.add(proc)  # register the live child so a cancel can kill it (frees GPU memory)

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
        if reg is not None:
            reg.remove(proc)
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
# Shared preparation helpers (used by both phase workers)
# ---------------------------------------------------------------------------

def _load_script(handle, script_name):
    """Load the parsed script: the chosen file, the most recent one, or ALL of them."""
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
    return script


def _collect_samples(script):
    """Each character's OWN lines as (script_index, text), in order of first appearance.

    The index lets the persona prompt attach each sampled line's ±window local context
    (surrounding narration and other characters). Returns ``(samples, order)``.
    """
    samples: dict = {}
    order: list = []
    for i, entry in enumerate(script):
        sp = _entry_speaker(entry)
        if not sp:
            continue
        if sp not in samples:
            samples[sp] = []
            order.append(sp)
        samples[sp].append((i, _entry_text(entry)))
    return samples, order


def _load_voice_config(handle):
    """Load the existing voice_config.json (preserving any hand-edited entries)."""
    vc_path = get_layout().voice_profiles / "voice_config.json"
    voice_config = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception as e:  # noqa: BLE001
            handle.log(f"现有 voice_config.json 无法解析（{e}），将重建。", "WARNING")
    return vc_path, voice_config


def _fold_aliases(handle, selected, voice_config):
    """Fold a label that clearly matches an existing character into an ``alias_of``
    pointer instead of giving it a new voice (heuristic; runs before the LLM / TTS).

    Returns ``(unique_speakers, resolved_aliases)``.
    """
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
    return unique_speakers, resolved_aliases


def _has_foundation(entry) -> bool:
    """Whether a stored entry already carries a voice foundation (a non-empty description)."""
    return bool(entry) and bool((entry.get("description") or "").strip())


def _clone_done(entry) -> bool:
    """Whether a stored entry already holds a usable clone (``type: clone`` + ``ref_audio``)."""
    return bool(entry) and entry.get("type") == "clone" and bool(entry.get("ref_audio"))


class _ChildReg:
    """Thread-safe registry of live TTS child processes, so a cancel can kill them all.

    Phase 2 runs N design-renders in parallel (each its own model-loading subprocess);
    on cancel the coordinator calls :meth:`kill_all` so in-flight children (each holding a
    loaded model in GPU memory) are torn down immediately instead of running to completion.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set = set()

    def add(self, proc) -> None:
        with self._lock:
            self._procs.add(proc)

    def remove(self, proc) -> None:
        with self._lock:
            self._procs.discard(proc)

    def kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# The two-phase Task workers
# ---------------------------------------------------------------------------

def prepare_foundations(handle, speakers=None, new_only=False, overrides=None, script_name=None) -> dict:
    """Phase 1 (LLM only): generate each character's voice *foundation* and persist it.

    First arg is the :class:`TaskHandle`. ``speakers`` is an optional allowlist (single-
    character regeneration); ``new_only`` regenerates only characters without a foundation
    yet; ``overrides`` maps a speaker → a user description (skips the LLM for that
    character); ``script_name`` selects which parsed JSON to read (None → most recent).

    The persona LLM calls run in parallel (bounded by ``generation.max_concurrency``) so
    the LLM runs at full concurrency on its own. **No TTS is started** — the VoiceDesign
    seed render is Phase 2 (``make_clones``). A per-character failure is recorded; only a
    *fatal* error (no script) raises.
    """
    overrides = overrides or {}

    script = _load_script(handle, script_name)
    samples, order = _collect_samples(script)
    handle.log(f"检测到 {len(order)} 个角色：{'、'.join(order)}")

    cfg = get_config()
    llm = cfg.llm
    pp = cfg.persona_prompts
    persona_system = pp.system_prompt or PERSONA_SYSTEM_PROMPT
    persona_user = pp.user_prompt or PERSONA_USER_PROMPT
    if not (llm.model_name or "").strip() and not overrides:
        handle.log("警告：未配置 LLM 模型——未提供提示词的角色将使用兜底描述。", "WARNING")

    vc_path, voice_config = _load_voice_config(handle)

    # Characters to (re)generate a foundation for: everyone, or (new_only) only those
    # without a foundation yet; an explicit ``speakers`` allowlist narrows it further.
    selected = [s for s in order if not (new_only and _has_foundation(voice_config.get(s)))]
    if speakers:
        allow = {s for s in speakers if s}
        selected = [s for s in selected if s in allow]
    if not selected:
        handle.log("没有需要生成基础的角色。")
        return {"count": 0, "aliases": 0, "speakers": order,
                "voice_config_path": str(vc_path), "results": []}

    handle.log(f"本次为 {len(selected)} 个角色生成语音推理基础（仅 LLM，不启动 TTS）。")

    unique_speakers, resolved_aliases = _fold_aliases(handle, selected, voice_config)

    n = len(unique_speakers)
    max_workers = max(1, int(cfg.generation.max_concurrency or 1))
    handle.log(f"LLM 并发 {max_workers}。")
    results = []
    done = 0

    def gen_one(sp):
        # Pure: compute the foundation and return it. The single-threaded coordinator below
        # applies it to the shared ``voice_config`` and persists, so workers never mutate it
        # (no lock needed, and no "dict changed size" hazard with the list-polling reader).
        handle.log(f"[{sp}] 开始生成语音推理基础（LLM 推理）…")
        pairs = samples.get(sp, [])
        lines = [t for _i, t in pairs]  # texts only, for ref-text selection / fallback
        bands = _select_target_bands(pairs)
        # Description + ref text: an override wins, else the LLM, else a fallback.
        description = (overrides.get(sp) or "").strip()
        ref_text = ""
        if description:
            handle.log(f"  [{sp}] 使用自定义提示词：{description[:60]}")
            ref_text = pick_ref_text(lines)
        else:
            try:
                description, ref_text = _llm_persona(
                    handle, llm, persona_system, persona_user, sp, script, bands,
                )
            except Exception as e:  # noqa: BLE001
                handle.log(f"  [{sp}] LLM 生成描述失败：{e}（改用兜底）", "WARNING")
                description, ref_text = "", ""
        if not description:
            description, ref_text = _fallback_persona(sp, lines)
            handle.log(f"  [{sp}] 使用兜底描述。", "WARNING")
        if not ref_text:
            ref_text = pick_ref_text(lines) or f"{sp} speaks in a clear, natural voice."
        return {
            "speaker": sp,
            "ok": bool(description),
            "type": "foundation",
            "description": description,
            "ref_text": ref_text,
            "foundation_status": "done" if description else "failed",
        }

    def persist():
        # Single-threaded incremental persist: every finished character is written back the
        # moment it completes, so the 角色配音 list (status / preview) refreshes in real time.
        vc_path.parent.mkdir(parents=True, exist_ok=True)
        vc_path.write_bytes(json.dumps(voice_config, indent=2, ensure_ascii=False).encode("utf-8"))

    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {ex.submit(gen_one, sp): sp for sp in unique_speakers}
    cancelled = False
    try:
        handle.progress(0.02, "启动 LLM（并行）")
        for fut in as_completed(futs):
            sp = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001 — a per-char error never aborts the run
                handle.log(f"  {sp} 生成基础失败：{e}", "ERROR")
                r = {"speaker": sp, "ok": False, "type": "foundation", "description": "",
                     "ref_text": "", "foundation_status": "failed"}
            # Apply the pure worker's result on the single coordinator thread, then persist.
            entry = voice_config.get(sp, {})
            entry.update({
                "type": r["type"],
                "description": r["description"],
                "ref_text": r.get("ref_text", ""),
                "foundation_status": r.get("foundation_status") or ("failed" if not r["ok"] else "done"),
                "seed": entry.get("seed", -1),
            })
            voice_config[sp] = entry
            persist()
            results.append({"speaker": r["speaker"], "ok": r["ok"], "type": r["type"],
                            "description": r["description"]})
            done += 1
            if r["ok"]:
                handle.log(f"  ✓ [{done}/{n}] {sp} 语音推理基础完成。")
            else:
                handle.log(f"  ✗ [{done}/{n}] {sp} 语音推理基础失败（无可用描述）。", "ERROR")
            handle.progress(0.02 + 0.98 * (done / (n or 1)), f"[{done}/{n}] 语音推理基础：{sp}")
            handle.check()  # cooperative cancel between completions
    except BaseException:
        cancelled = True
        raise
    finally:
        # On cancel, don't wait for in-flight (uninterruptible) LLM calls — drop them.
        if cancelled:
            ex.shutdown(wait=False, cancel_futures=True)
        else:
            ex.shutdown(wait=True)

    # Final persist also covers the early-cancel / no-completion case.
    persist()
    handle.log(f"voice_config 已保存：{vc_path}")

    handle.progress(1.0, "完成")
    handle.log(f"语音推理基础生成完成：{len(unique_speakers)} 个角色 + {len(resolved_aliases)} 个别名。")
    return {
        "count": len(unique_speakers),
        "aliases": len(resolved_aliases),
        "speakers": order,
        "voice_config_path": str(vc_path),
        "results": results,
    }


def make_clones(handle, speakers=None, new_only=False, concurrency=None, script_name=None) -> dict:
    """Phase 2 (TTS only): render each foundation-bearing character's clone seed WAV.

    First arg is the :class:`TaskHandle`. Reads back the foundations persisted by Phase 1
    and, for every in-scope non-alias character that has a foundation, renders its clone
    seed WAV via the worker's ``design`` mode and stores it as the clone reference
    (``type: clone, ref_audio``). On a render failure it falls back to a ``design`` voice.

    ``concurrency`` is the number of TTS subprocesses to run in parallel (each loads the
    model once — GPU memory scales with it); ``new_only`` limits the run to characters
    not yet holding a usable clone; ``speakers`` restricts to an allowlist (single-character
    remake). On cancel every in-flight TTS child is killed so GPU memory frees at once.
    **No LLM is used.**
    """
    script = _load_script(handle, script_name)
    samples, order = _collect_samples(script)
    handle.log(f"检测到 {len(order)} 个角色：{'、'.join(order)}")

    vc_path, voice_config = _load_voice_config(handle)

    # Characters eligible for a clone: in-scope, non-alias, already carrying a foundation.
    def _is_alias(sp):
        return bool((voice_config.get(sp) or {}).get("alias_of"))

    selected = [s for s in order if _has_foundation(voice_config.get(s)) and not _is_alias(s)]
    no_foundation = [s for s in order if not _has_foundation(voice_config.get(s)) and not _is_alias(s)]
    if no_foundation:
        handle.log(f"提示：{len(no_foundation)} 个角色尚无语音推理基础，本次跳过——请先运行阶段 1。", "WARNING")
    if new_only:
        selected = [s for s in selected if not _clone_done(voice_config.get(s))]
    if speakers:
        allow = {s for s in speakers if s}
        selected = [s for s in selected if s in allow]
    if not selected:
        handle.log("没有可制作克隆音频的角色（请先运行阶段 1 生成语音推理基础）。", "WARNING")
        return {"count": 0, "ok": 0, "failed": 0, "speakers": order,
                "voice_config_path": str(vc_path),
                "output_dir": str(get_layout().voice_profiles / "designed_voices"),
                "results": []}

    n = len(selected)
    workers = max(1, int(concurrency or 1))
    handle.log(f"本次为 {n} 个角色制作克隆音频（TTS 并发 {workers}，请确保已关闭 LLM 以释放显存）。")

    results = []
    ok = failed = done = 0
    reg = _ChildReg()

    def render_one(sp):
        # Pure: render the clone seed and return the result. The coordinator applies it to
        # the shared ``voice_config`` and persists; workers only *read* it (description/ref_text).
        handle.log(f"[{sp}] 开始制作克隆音频（VoiceDesign / TTS 渲染）…")
        entry = voice_config.get(sp, {})
        description = (entry.get("description") or "").strip()
        ref_text = (entry.get("ref_text") or "").strip()
        if not ref_text:
            ref_text = pick_ref_text([t for _i, t in samples.get(sp, [])]) \
                or f"{sp} speaks in a clear, natural voice."
        out_wav = str(get_layout().voice_profiles / "designed_voices" /
                      f"{_sanitize(sp)}_{time.time_ns()}.wav")
        try:
            produced = _design_preview(handle, description, ref_text, out_wav, reg=reg)
            return {"speaker": sp, "ok": True, "type": "clone", "preview": produced,
                    "description": description, "ref_text": ref_text, "clone_status": "done"}
        except Exception as e:  # noqa: BLE001 — this character falls back; the run continues
            return {"speaker": sp, "ok": False, "type": "design", "preview": "", "reason": str(e),
                    "description": description, "ref_text": ref_text, "clone_status": "failed"}

    def persist():
        # Single-threaded incremental persist: each finished character is written back the
        # moment it completes, so the 角色配音 list (status / preview) refreshes in real time.
        vc_path.parent.mkdir(parents=True, exist_ok=True)
        vc_path.write_bytes(json.dumps(voice_config, indent=2, ensure_ascii=False).encode("utf-8"))

    ex = ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(render_one, sp): sp for sp in selected}
    cancelled = False
    try:
        handle.progress(0.02, "启动 TTS（并行）")
        for fut in as_completed(futs):
            sp = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001
                handle.log(f"  {sp} 制作克隆失败：{e}", "ERROR")
                r = {"speaker": sp, "ok": False, "type": "design", "preview": "", "reason": str(e),
                     "description": "", "ref_text": "", "clone_status": "failed"}
            # Apply the pure worker's result on the single coordinator thread, then persist.
            entry = voice_config.get(sp, {})
            if r["ok"]:
                entry.update({
                    "type": "clone",
                    "ref_audio": r["preview"],  # absolute path (the worker reads it directly)
                    "ref_text": r["ref_text"],
                    "description": r["description"],
                    "character_style": r["description"],
                    "clone_status": "done",
                    "seed": entry.get("seed", -1),
                })
            else:
                entry.update({"type": "design", "description": r["description"],
                              "ref_text": r["ref_text"], "clone_status": "failed"})
            voice_config[sp] = entry
            persist()
            item = {"speaker": r["speaker"], "ok": r["ok"], "type": r["type"], "preview": r["preview"]}
            if r.get("reason"):
                item["reason"] = r["reason"]
            results.append(item)
            done += 1
            if r["ok"]:
                ok += 1
                handle.log(f"  ✓ [{done}/{n}] {sp} 克隆音频就绪。")
            else:
                failed += 1
                handle.log(f"  ✗ [{done}/{n}] {sp} 克隆生成失败（{r.get('reason', '')}），改用 design 兜底。", "ERROR")
            handle.progress(0.02 + 0.98 * (done / (n or 1)), f"[{done}/{n}] 克隆音频：{sp}")
            handle.check()  # cooperative cancel between completions -> kill_all in finally
    except BaseException:
        cancelled = True
        raise
    finally:
        if cancelled:
            reg.kill_all()  # tear down in-flight TTS children (frees GPU memory at once)
            ex.shutdown(wait=False, cancel_futures=True)
        else:
            ex.shutdown(wait=True)

    # Final persist also covers the early-cancel / no-completion case.
    persist()
    handle.log(f"voice_config 已保存：{vc_path}")

    handle.progress(1.0, "完成")
    handle.log(f"克隆音频制作完成：成功 {ok} / 失败 {failed} / 共 {n} 个角色。")
    return {
        "count": n,
        "ok": ok,
        "failed": failed,
        "speakers": order,
        "voice_config_path": str(vc_path),
        "output_dir": str(get_layout().voice_profiles / "designed_voices"),
        "results": results,
    }
