"""Qwen3-TTS synthesis worker.

Runs in the ISOLATED TTS virtualenv (``.venv-tts``, Python 3.10) as a one-shot
subprocess, spawned by the app backend (3.14) — see ``backend/engines/tts.py`` and
the sibling ``backend/engines/tts_batch.py`` / ``merge.py``. Keeping the heavy ML
stack out of the app's 3.14 environment is deliberate: ``qwen-tts`` is only
officially supported through Python 3.13 and pulls a large dependency tree, and the
app's venv must stay lean and stable.

Ported (minimal core, not the whole app) from ``alexandria-audiobook``:
  - model resolve / load      (tts.py  _resolve_local_model_path / _init_local_*)
  - custom inference          (tts.py  _local_generate_custom)
  - clone inference           (tts.py  _get_clone_prompt / _local_generate_clone)
  - design inference          (tts.py  generate_voice_design / generate_design_voice)
  - WAV save                  (tts.py  _save_wav)  + WAV->MP3 (project.py, via pydub)
  - merge timeline + combine  (tts.py  compute_timeline / combine_audio_with_pauses)

Modes (``--mode``)
------------------
  custom   (default)  one segment, CustomVoice model — the original one-shot behaviour
  design             one VoiceDesign preview wav (used to seed a character voice)
  clone              one segment via a cloned (Base + reference) voice
  batch              all segments in a file, one subprocess, needed models loaded once,
                     synthesized by a thread pool of --concurrency (default 4; 1 = sequential)
  merge              combine per-segment files (in order) into the final audiobook

Contract with the backend (all on STDOUT unless noted)
------------------------------------------------------
  - ``[progress] <0.0-1.0> <label>`` -> backend calls handle.progress(frac, label)
  - ``[result]   <absolute path>``    -> the primary file produced
  - ``[segment]  <index> ok <path>``  -> one batch segment succeeded
  - ``[segment]  <index> error <reason>`` -> one batch segment failed (batch continues)
  - any other line                    -> forwarded as a log entry
  - exit 0 on success; non-zero on failure, error on STDERR.

Batch mode returns 0 even if individual segments failed (they are reported via the
``[segment]`` lines); it returns non-zero only on a *fatal* setup error (missing /
unreadable voice config, empty segment list, model load failure, ...).
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# transformers reconfigures its root logger on first import: it resets the level to
# WARNING, attaches its own stderr handler and disables propagation — which stomps any
# ``logging.getLogger("transformers").setLevel(...)`` set earlier in this process (the
# ML stack is imported lazily, always after ``main()``). The TRANSFORMERS_VERBOSITY env
# var is read at exactly that reconfiguration moment, so it is the reliable way to keep
# the benign "Setting `pad_token_id` to `eos_token_id` ... for open-end generation"
# WARNING (emitted on every ``generate()``) out of the live log. ``setdefault`` lets an
# explicit value (e.g. "info" while debugging) still win.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

# Model ids. The three Qwen3-TTS 1.7B variants share the same loader; only the
# suffix (CustomVoice / Base / VoiceDesign) selects the behaviour.
DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_BASE_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
DEFAULT_DESIGN_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
DEFAULT_SPEAKER = "serena"
DEFAULT_LANGUAGE = "chinese"

SUPPORTED_TYPES = ("custom", "clone", "design")


def log(msg: str) -> None:
    """A plain status line -> backend forwards it as a log entry."""
    print(msg, flush=True)


def progress(frac: float, label: str) -> None:
    """A tagged progress line -> backend maps it to handle.progress()."""
    print(f"[progress] {max(0.0, min(1.0, frac)):.2f} {label}", flush=True)


def _segment_ok(index: int, path: str) -> None:
    print(f"[segment] {index} ok {path}", flush=True)


def _segment_error(index: int, reason: str) -> None:
    # Collapse whitespace so a traceback-ish reason stays on one log line.
    reason = " ".join(str(reason).split())
    print(f"[segment] {index} error {reason}", flush=True)


@contextlib.contextmanager
def _silence_streams():
    """Temporarily redirect the process stdout/stderr (fd 1 & 2) to devnull.

    Importing the ML stack and calling ``from_pretrained`` spam benign,
    non-actionable notices that would otherwise clutter the live log: torchaudio's
    "SoX could not be found!" (→ stderr; SoX is never used — audio I/O goes through
    soundfile/pydub/ffmpeg) and qwen-tts' "flash-attn is not installed" (→ stdout;
    we simply take the slower manual-attention path). None of these indicate a fault,
    so they are dropped.

    Redirecting the *raw file descriptors* (not just ``sys.stdout``/``sys.stderr``)
    also swallows C-level writes (which is why the SoX notice, in GBK on zh-CN
    Windows, would otherwise reach the parent as mojibake). The descriptors are
    restored before any ``[progress]``/``[result]`` line is emitted, so real output —
    and real errors, which surface as Python exceptions — are never lost.
    """
    saved_out, saved_err = os.dup(1), os.dup(2)
    try:
        with open(os.devnull, "wb") as devnull:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            yield
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)


def resolve_device(pref: str) -> str:
    """Resolve an 'auto' (or explicit) device preference to a concrete device."""
    if pref and pref != "auto":
        return pref
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _resolve_local_model_path(model_id: str):
    """If the model snapshot is already in the HF cache, return its dir; else None."""
    from huggingface_hub import try_to_load_from_cache

    result = try_to_load_from_cache(model_id, "config.json")
    if isinstance(result, str):
        return os.path.dirname(result)
    return None


def load_model(model_id: str, device: str):
    """Load ``Qwen3TTSModel``, preferring a local cache hit; downloads on first run.

    The ``qwen_tts`` import and the ``from_pretrained`` call are the source of the
    benign library notices (SoX-missing, flash-attn-missing), so they run inside
    :func:`_silence_streams`. The human-readable status lines are emitted *before*
    the silenced block so they still reach the log.
    """
    import torch

    dtype = torch.bfloat16 if "cuda" in device else torch.float32
    load_kwargs = {"dtype": dtype}
    if device != "cpu":
        load_kwargs["device_map"] = device

    local_path = _resolve_local_model_path(model_id)
    if local_path:
        log(f"Loading model from local cache: {local_path}")
    else:
        log(f"Model not cached locally; downloading {model_id} (first run, a few GB)...")

    with _silence_streams():
        from qwen_tts import Qwen3TTSModel  # import-time notices (SoX) fire silenced
        if local_path:
            try:
                model = Qwen3TTSModel.from_pretrained(local_path, **load_kwargs)
            except Exception as e:  # incomplete snapshot -> fall back to a hub download
                log(f"Local cache load failed ({e}); retrying via model id (may download).")
                model = Qwen3TTSModel.from_pretrained(model_id, **load_kwargs)
        else:
            model = Qwen3TTSModel.from_pretrained(model_id, **load_kwargs)
    return model


def _save_wav(audio_array, sample_rate: int, output_path: str) -> None:
    import numpy as np
    import soundfile as sf

    if not isinstance(audio_array, np.ndarray):
        audio_array = np.array(audio_array)
    if audio_array.ndim > 1:
        audio_array = audio_array.flatten()
    sf.write(output_path, audio_array, sample_rate)


def _wav_to_mp3(wav_path: str, mp3_path: str) -> bool:
    """Convert WAV -> MP3 via pydub (needs ffmpeg on PATH). False if it can't."""
    from pydub import AudioSegment

    segment = AudioSegment.from_wav(wav_path)
    if len(segment) == 0:
        return False
    segment.export(mp3_path, format="mp3")
    # A broken ffmpeg (no libmp3lame) yields a tiny header-only file without raising.
    size = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
    if size < 1024:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
        return False
    return True


def _concat(wavs):
    """``generate_*`` returns a list of numpy arrays; join into one."""
    import numpy as np

    return np.concatenate(wavs) if len(wavs) > 1 else wavs[0]


def _add_ffmpeg_to_path(ffmpeg: str) -> None:
    """Let pydub find a user-provided ffmpeg (its directory must be on PATH)."""
    if ffmpeg:
        fdir = os.path.dirname(os.path.abspath(ffmpeg))
        if fdir:
            os.environ["PATH"] = fdir + os.pathsep + os.environ.get("PATH", "")


def _resolve_alias(speaker: str, voice_config: dict) -> str:
    """Follow the ``alias_of`` chain to the canonical speaker name (port of
    ``ProjectManager._resolve_alias``). Cycle-guarded, capped at 8 hops."""
    if not speaker:
        return speaker
    name = speaker
    seen = set()
    for _ in range(8):
        if name in seen:
            break
        seen.add(name)
        entry = voice_config.get(name, {}) or {}
        alias = entry.get("alias_of") or entry.get("alias")
        if not alias:
            break
        if not isinstance(alias, str) or alias.strip() == "" or alias == name:
            break
        name = alias
    return name


def _build_clone_prompt(model, voice_data: dict, root: str, speaker: str):
    """Create (and the caller caches) a Base-model voice-clone prompt (port of
    ``TTSEngine._get_clone_prompt``). Raises if the reference is missing/invalid."""
    import soundfile as sf

    ref_audio_path = voice_data.get("ref_audio")
    ref_text = voice_data.get("ref_text")
    if not ref_audio_path or not ref_text:
        raise ValueError(f"Clone voice for '{speaker}' missing ref_audio or ref_text")
    if not os.path.isabs(ref_audio_path):
        ref_audio_path = os.path.join(root, ref_audio_path)
    if not os.path.exists(ref_audio_path):
        raise FileNotFoundError(f"Reference audio not found for '{speaker}': {ref_audio_path}")

    audio_array, sample_rate = sf.read(ref_audio_path)
    if audio_array.ndim > 1:  # ensure mono
        audio_array = audio_array.mean(axis=1)
    return model.create_voice_clone_prompt(
        ref_audio=(audio_array, sample_rate),
        ref_text=ref_text,
    )


def _needed_types(segments, voice_config: dict) -> set:
    """Which of the three models a batch actually requires (so we load only those)."""
    types = set()
    for seg in segments:
        canonical = _resolve_alias((seg.get("speaker") or "").strip(), voice_config)
        vd = voice_config.get(canonical) or {}
        if not vd:
            continue
        vt = vd.get("type", "custom")
        if vt in SUPPORTED_TYPES:
            types.add(vt)
    return types


def _load_models_for(needed: set, device: str, args):
    """Load exactly the models ``needed`` holds, each once, for the whole batch."""
    models = {}
    if "custom" in needed:
        log("Loading CustomVoice model…")
        models["custom"] = load_model(args.model, device)
    if "clone" in needed:
        log("Loading Base model (voice cloning)…")
        models["clone"] = load_model(args.base_model, device)
    if "design" in needed:
        log("Loading VoiceDesign model…")
        models["design"] = load_model(args.design_model, device)
    return models


def run_bounded(items, concurrency, fn, on_result=None):
    """Run ``fn(item)`` for every ``item``, with at most ``concurrency`` in flight.

    ``concurrency <= 1`` (or a single item) runs sequentially in the calling thread —
    exactly the original single-threaded behaviour (no pool, no threads). Otherwise a
    ``ThreadPoolExecutor`` of ``concurrency`` workers overlaps the (model) work, hard-
    capped at ``concurrency``: the pool can never run more than ``concurrency`` ``fn``
    calls at once, so the limit is enforced by construction (verified in
    ``backend/tests/test_tts_worker.py``).

    Results are returned in input order. ``on_result(item, result)`` is invoked from the
    *calling* thread (never the workers) as each completes, so the progress / log lines it
    emits are single-threaded and the progress fractions stay monotonic. A per-item
    exception raised by ``fn`` propagates out of ``fut.result()``; callers that want
    per-item tolerance (the batch) make ``fn`` never raise and instead return an error.
    """
    try:
        limit = int(concurrency)
    except (TypeError, ValueError):
        limit = 1
    limit = max(1, limit)

    if limit <= 1 or len(items) <= 1:
        out = []
        for item in items:
            r = fn(item)
            out.append(r)
            if on_result is not None:
                on_result(item, r)
        return out

    out = [None] * len(items)
    with ThreadPoolExecutor(max_workers=limit) as ex:
        futs = {ex.submit(fn, item): i for i, item in enumerate(items)}
        for fut in as_completed(futs):
            i = futs[fut]
            out[i] = fut.result()  # re-raises a per-item exception, if any
            if on_result is not None:
                on_result(items[i], out[i])
    return out


# ---------------------------------------------------------------------------
# Merge: timeline + pause-aware combine (1:1 ports of tts.py)
# ---------------------------------------------------------------------------

def combine_audio_with_pauses(audio_segments, speakers, pause_ms=500,
                              same_speaker_pause_ms=250, pause_overrides=None):
    """Combine audio segments with pauses between them (port of ``tts.py``).

    ``pause_overrides[i]`` (ms, or None) is the pause inserted *after* segment i;
    the last entry is ignored. None falls back to the speaker-change default.
    """
    from pydub import AudioSegment

    if not audio_segments:
        return None

    combined = audio_segments[0]
    prev_speaker = speakers[0]

    for i, (segment, speaker) in enumerate(zip(audio_segments[1:], speakers[1:])):
        override = pause_overrides[i] if pause_overrides else None
        if override is not None:
            gap = AudioSegment.silent(duration=override)
        elif speaker == prev_speaker:
            gap = AudioSegment.silent(duration=same_speaker_pause_ms)
        else:
            gap = AudioSegment.silent(duration=pause_ms)
        combined += gap + segment
        prev_speaker = speaker

    return combined


def compute_timeline(chunks_with_audio, pause_ms=500, same_speaker_pause_ms=250):
    """Compute ``(chunk, segment, abs_start_ms)`` tuples (port of ``tts.py``)."""
    timeline = []
    cursor_ms = 0
    prev_speaker = None
    prev_chunk = None

    for chunk, segment in chunks_with_audio:
        if prev_speaker is not None:
            override = prev_chunk.get("pause_after")
            if override is not None:
                gap = int(override)
            elif chunk["speaker"] == prev_speaker:
                gap = same_speaker_pause_ms
            else:
                gap = pause_ms
            cursor_ms += gap

        timeline.append((chunk, segment, cursor_ms))
        cursor_ms += len(segment)
        prev_speaker = chunk["speaker"]
        prev_chunk = chunk

    return timeline


# ---------------------------------------------------------------------------
# Mode implementations
# ---------------------------------------------------------------------------

def _run_custom(args) -> int:
    """The original one-shot CustomVoice synthesis (unchanged behaviour)."""
    if not args.out:
        print("TTS_WORKER_ERROR: --out is required for custom mode", file=sys.stderr, flush=True)
        return 2

    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            text = f.read().strip()
    else:
        text = (args.text or "").strip()
    if not text:
        print("TTS_WORKER_ERROR: empty input text (use --text or --text-file)",
              file=sys.stderr, flush=True)
        return 2

    _add_ffmpeg_to_path(args.ffmpeg)

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    base, _ext = os.path.splitext(out_path)
    wav_tmp = base + ".wav"  # temp WAV; removed on a successful MP3 encode

    try:
        progress(0.05, "Preparing")
        device = resolve_device(args.device)
        log(f"device = {device}")

        progress(0.10, "Loading model")
        model = load_model(args.model, device)
        log("Model ready.")

        progress(0.40, "Synthesizing speech")
        instruct = args.instruct or "neutral"
        wavs, sr = model.generate_custom_voice(
            text=text,
            language=args.language,
            speaker=args.speaker,
            instruct=instruct,
            non_streaming_mode=True,
            max_new_tokens=2048,
        )
        if not wavs:
            raise RuntimeError("Model returned no audio.")

        audio = _concat(wavs)
        _save_wav(audio, sr, wav_tmp)
        log(f"Synthesized {len(audio) / sr:.1f}s audio @ {sr} Hz.")

        progress(0.80, "Encoding MP3")
        if _wav_to_mp3(wav_tmp, out_path):
            produced = out_path
            if os.path.exists(wav_tmp):
                os.remove(wav_tmp)
        else:
            log("MP3 encoding unavailable (ffmpeg missing?); keeping WAV instead.")
            produced = wav_tmp
        log(f"Wrote {produced}")
        print(f"[result] {produced}", flush=True)
        progress(1.0, "Done")
        return 0
    except Exception as e:  # noqa: BLE001 — surface any failure to the backend
        import traceback

        traceback.print_exc()
        print(f"TTS_WORKER_ERROR: {e}", file=sys.stderr, flush=True)
        return 1


def _run_design(args) -> int:
    """Render a VoiceDesign preview WAV from a text description (seeds a voice)."""
    if not args.out:
        print("TTS_WORKER_ERROR: --out is required for design mode", file=sys.stderr, flush=True)
        return 2

    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            sample_text = f.read().strip()
    else:
        sample_text = (args.sample_text or args.text or "").strip()
    if not sample_text:
        print("TTS_WORKER_ERROR: empty sample text for design mode", file=sys.stderr, flush=True)
        return 2
    if args.description_file:
        with open(args.description_file, "r", encoding="utf-8") as f:
            description = f.read().strip()
    else:
        description = (args.description or "").strip()
    if not description:
        description = "A clear, natural speaking voice"

    _add_ffmpeg_to_path(args.ffmpeg)

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    try:
        progress(0.10, "Loading VoiceDesign model")
        device = resolve_device(args.device)
        log(f"device = {device}")
        model = load_model(args.design_model, device)
        log("VoiceDesign model ready.")

        progress(0.40, "Generating voice from description")
        wavs, sr = model.generate_voice_design(
            text=sample_text,
            instruct=description,
            language=args.language,
            non_streaming_mode=True,
            max_new_tokens=2048,
        )
        if not wavs:
            raise RuntimeError("VoiceDesign model returned no audio.")

        audio = _concat(wavs)
        _save_wav(audio, sr, out_path)
        log(f"Designed voice: {len(audio) / sr:.1f}s audio @ {sr} Hz")
        print(f"[result] {out_path}", flush=True)
        progress(1.0, "完成")
        return 0
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"TTS_WORKER_ERROR: {e}", file=sys.stderr, flush=True)
        return 1


def _run_clone(args) -> int:
    """Synthesize one segment with a cloned (Base + reference) voice."""
    if not args.out:
        print("TTS_WORKER_ERROR: --out is required for clone mode", file=sys.stderr, flush=True)
        return 2

    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            text = f.read().strip()
    else:
        text = (args.text or "").strip()
    if not text:
        print("TTS_WORKER_ERROR: empty input text for clone mode", file=sys.stderr, flush=True)
        return 2

    _add_ffmpeg_to_path(args.ffmpeg)

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    base, _ext = os.path.splitext(out_path)
    wav_tmp = base + ".wav"

    try:
        progress(0.05, "Preparing")
        device = resolve_device(args.device)
        log(f"device = {device}")

        progress(0.10, "Loading Base model (voice cloning)")
        model = load_model(args.base_model, device)
        log("Base model ready.")

        progress(0.30, "Building voice clone prompt")
        voice_data = {"ref_audio": args.ref_audio, "ref_text": args.ref_text}
        prompt = _build_clone_prompt(model, voice_data, os.getcwd(), args.speaker or "clone")

        progress(0.50, "Synthesizing cloned speech")
        wavs, sr = model.generate_voice_clone(
            text=text,
            voice_clone_prompt=prompt,
            non_streaming_mode=True,
            max_new_tokens=2048,
        )
        if not wavs:
            raise RuntimeError("Model returned no audio.")

        audio = _concat(wavs)
        _save_wav(audio, sr, wav_tmp)
        log(f"Synthesized {len(audio) / sr:.1f}s audio @ {sr} Hz.")

        progress(0.80, "Encoding MP3")
        if _wav_to_mp3(wav_tmp, out_path):
            produced = out_path
            if os.path.exists(wav_tmp):
                os.remove(wav_tmp)
        else:
            log("MP3 encoding unavailable (ffmpeg missing?); keeping WAV instead.")
            produced = wav_tmp
        log(f"Wrote {produced}")
        print(f"[result] {produced}", flush=True)
        progress(1.0, "完成")
        return 0
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"TTS_WORKER_ERROR: {e}", file=sys.stderr, flush=True)
        return 1


def _run_batch(args) -> int:
    """Synthesize every segment in ``--segments-file``, one subprocess.

    Loads only the models the batch needs, once; loops segments in file order;
    routes each by its voice type; saves ``NNNN.<ext>`` per segment; reports
    ``[segment]`` lines so a single failure never aborts the run.
    """
    import json as _json

    if not args.segments_file or not args.voice_config or not args.out_dir:
        print("TTS_WORKER_ERROR: batch mode needs --segments-file, --voice-config and --out-dir",
              file=sys.stderr, flush=True)
        return 2

    _add_ffmpeg_to_path(args.ffmpeg)

    try:
        with open(args.segments_file, "r", encoding="utf-8") as f:
            segments = _json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"TTS_WORKER_ERROR: cannot read segments file: {e}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(segments, list) or not segments:
        print("TTS_WORKER_ERROR: segments file is empty or not a list", file=sys.stderr, flush=True)
        return 2

    try:
        with open(args.voice_config, "r", encoding="utf-8") as f:
            voice_config = _json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"TTS_WORKER_ERROR: cannot read voice config: {e}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(voice_config, dict):
        print("TTS_WORKER_ERROR: voice config is not a JSON object", file=sys.stderr, flush=True)
        return 2

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    device = resolve_device(args.device)
    log(f"device = {device} · {len(segments)} 段待合成")

    needed = _needed_types(segments, voice_config)

    progress(0.02, "解析输入")
    if needed:
        log("需加载模型：" + "、".join(t for t in SUPPORTED_TYPES if t in needed))
    else:
        log("警告：没有任何角色匹配到声音配置——所有段都会失败。请先在「角色声音」页生成声音。")

    progress(0.04, "加载模型")
    models = _load_models_for(needed, device, args)
    progress(0.05, "模型就绪")
    log("模型就绪。")

    # Concurrency: how many segments synthesize at once (a thread pool inside this one
    # subprocess, so the model loads only once). Clamped to [1, 32] — the backend already
    # clamps, this is defence in depth against a hand-edited / stale value.
    concurrency = max(1, min(32, int(args.concurrency)))
    if concurrency > 1:
        log(f"并发 {concurrency} 段（模型只加载一次，线程池并行）")
    else:
        log("逐段串行（并发 1，模型只加载一次）")

    clone_prompts: dict = {}
    clone_lock = threading.Lock()
    total = len(segments)
    width = max(4, len(str(total)))
    counts = {"completed": 0, "failed": 0}

    def synth(seg):
        """Synthesize one segment on a pool worker thread; return ``(ok, detail)``.

        Never raises: any fault (missing config, unsupported type, model error) folds into
        an ``ok=False`` result so a single bad segment can't abort the run. The coordinator
        (``report``) is the only place that emits ``[segment]`` / ``[progress]`` lines, so
        those stay ordered on the live log; this thread only logs the in-flight "正在生成".
        """
        index = int(seg.get("index", 0))
        speaker = (seg.get("speaker") or "").strip()
        text = (seg.get("text") or "").strip()
        instruct = (seg.get("instruct") or "").strip()
        preview = text if len(text) <= 60 else text[:60] + "…"
        log(f"  正在生成（角色 {speaker or '(未知)'}）：{preview}")

        if not text:
            return False, "空文本"

        canonical = _resolve_alias(speaker, voice_config)
        vd = voice_config.get(canonical) or {}
        if not vd:
            return False, f"缺少角色声音配置（{speaker or canonical}）——请先在「角色声音」页生成"

        vtype = vd.get("type", "custom")
        if vtype not in SUPPORTED_TYPES:
            return False, f"不支持的声音类型：{vtype}"

        fname = str(index + 1).zfill(width)
        out_mp3 = os.path.join(out_dir, fname + ".mp3")
        wav_tmp = os.path.join(out_dir, fname + ".wav")

        try:
            if vtype == "clone":
                model = models.get("clone")
                if model is None:
                    raise RuntimeError("Base 模型未加载")
                # The clone prompt is shared per character — build it once, under a lock, so
                # two threads synthesizing the same character don't both call create_voice_clone_prompt.
                with clone_lock:
                    if canonical not in clone_prompts:
                        log(f"  构建 {canonical} 的克隆提示…")
                        clone_prompts[canonical] = _build_clone_prompt(model, vd, os.getcwd(), canonical)
                    prompt = clone_prompts[canonical]
                wavs, sr = model.generate_voice_clone(
                    text=text,
                    voice_clone_prompt=prompt,
                    non_streaming_mode=True,
                    max_new_tokens=2048,
                )
            elif vtype == "design":
                model = models.get("design")
                if model is None:
                    raise RuntimeError("VoiceDesign 模型未加载")
                base_desc = (vd.get("description") or "").strip()
                if base_desc and instruct:
                    description = f"{base_desc}, {instruct}"
                elif base_desc:
                    description = base_desc
                elif instruct:
                    description = instruct
                else:
                    description = "A clear, natural speaking voice"
                wavs, sr = model.generate_voice_design(
                    text=text,
                    instruct=description,
                    language=args.language,
                    non_streaming_mode=True,
                    max_new_tokens=2048,
                )
            else:  # custom
                model = models.get("custom")
                if model is None:
                    raise RuntimeError("CustomVoice 模型未加载")
                voice = vd.get("voice") or args.speaker or DEFAULT_SPEAKER
                default_style = (vd.get("default_style") or "").strip()
                i_instruct = instruct or (default_style if default_style else "neutral")
                wavs, sr = model.generate_custom_voice(
                    text=text,
                    language=args.language,
                    speaker=voice,
                    instruct=i_instruct,
                    non_streaming_mode=True,
                    max_new_tokens=2048,
                )

            if not wavs:
                raise RuntimeError("模型未返回音频")
            audio = _concat(wavs)
            _save_wav(audio, sr, wav_tmp)
            if _wav_to_mp3(wav_tmp, out_mp3):
                produced = out_mp3
                if os.path.exists(wav_tmp):
                    os.remove(wav_tmp)
            else:
                produced = wav_tmp  # MP3 unavailable -> keep the WAV
            return True, produced
        except Exception as e:  # noqa: BLE001 — one bad segment must not kill the batch
            for p in (wav_tmp, out_mp3):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            return False, str(e)

    def report(seg, result):
        """Coordinator-thread completion handler: emit the ``[segment]`` line + progress.

        Runs single-threaded in completion order, so the ``[progress]`` fractions it emits
        are monotonic and the ``[segment]`` log lines stay well-ordered on the live log.
        """
        ok, detail = result
        index = int(seg.get("index", 0))
        if ok:
            _segment_ok(index, detail)
            counts["completed"] += 1
        else:
            _segment_error(index, detail)
            counts["failed"] += 1
        done = counts["completed"] + counts["failed"]
        progress(
            0.05 + 0.95 * (done / total),
            f"完成 {done}/{total} 段（成功 {counts['completed']} / 失败 {counts['failed']}）",
        )

    run_bounded(segments, concurrency, synth, on_result=report)

    completed, failed = counts["completed"], counts["failed"]

    progress(1.0, f"完成（成功 {completed} / 失败 {failed} / 共 {total}）")
    log(f"批量合成结束：成功 {completed}，失败 {failed}，共 {total} 段。输出目录：{out_dir}")
    return 0


def _run_merge(args) -> int:
    """Combine per-segment files (in order) into the final audiobook mp3."""
    import json as _json
    from pydub import AudioSegment

    if not args.segments_file or not args.out:
        print("TTS_WORKER_ERROR: merge mode needs --segments-file and --out", file=sys.stderr, flush=True)
        return 2

    _add_ffmpeg_to_path(args.ffmpeg)

    try:
        with open(args.segments_file, "r", encoding="utf-8") as f:
            segs = _json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"TTS_WORKER_ERROR: cannot read segments file: {e}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(segs, list) or not segs:
        print("TTS_WORKER_ERROR: no segments to merge", file=sys.stderr, flush=True)
        return 2

    pause_ms = int(args.pause_ms)
    same_ms = int(args.same_same_ms)
    total = len(segs)

    chunks_with_audio = []
    skipped = 0
    for i, s in enumerate(segs):
        p = s.get("path")
        if not p:
            skipped += 1
            continue
        full = p if os.path.isabs(p) else os.path.join(os.getcwd(), p)
        if not os.path.exists(full):
            skipped += 1
            continue
        try:
            seg = AudioSegment.from_file(full)
        except Exception as e:  # noqa: BLE001
            log(f"跳过无法读取的段 {s.get('index')}（{os.path.basename(full)}）：{e}")
            skipped += 1
            continue
        chunk = {"speaker": s.get("speaker", ""), "pause_after": s.get("pause_after"),
                 "text": s.get("text", "")}
        chunks_with_audio.append((chunk, seg))
        if (i + 1) % 50 == 0 or i == total - 1:
            progress(0.10 + 0.70 * (i + 1) / total, f"读取音频 {i + 1}/{total}")

    if not chunks_with_audio:
        print("TTS_WORKER_ERROR: 没有可合并的音频段", file=sys.stderr, flush=True)
        return 2
    if skipped:
        log(f"警告：{skipped} 段被跳过（文件缺失或无法读取）")

    progress(0.85, "计算时间轴并合并")
    timeline = compute_timeline(chunks_with_audio, pause_ms, same_ms)
    audio_segments = [seg for _, seg, _ in timeline]
    speakers = [c["speaker"] for c, _, _ in timeline]
    pause_overrides = [c.get("pause_after") for c, _, _ in timeline]
    final = combine_audio_with_pauses(audio_segments, speakers, pause_ms, same_ms, pause_overrides)
    if final is None:
        print("TTS_WORKER_ERROR: 合并结果为空", file=sys.stderr, flush=True)
        return 1

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    base, _ = os.path.splitext(out_path)
    wav_tmp = base + ".wav"

    final.export(wav_tmp, format="wav")
    duration_s = len(final) / 1000.0
    progress(0.95, "编码 MP3")
    if _wav_to_mp3(wav_tmp, out_path):
        produced = out_path
        if os.path.exists(wav_tmp):
            os.remove(wav_tmp)
    else:
        log("MP3 编码不可用（缺少 ffmpeg？）；保留 WAV。")
        produced = wav_tmp
    log(f"合并完成：{duration_s / 60:.1f} 分钟，{len(audio_segments)} 段 → {produced}")
    print(f"[result] {produced}", flush=True)
    progress(1.0, "完成")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen3-TTS synthesis worker")
    ap.add_argument("--mode", default="custom",
                    choices=["custom", "design", "clone", "batch", "merge"])
    # shared
    ap.add_argument("--text", default="", help="text to synthesize (or use --text-file)")
    ap.add_argument("--text-file", default="",
                    help="path to a UTF-8 file holding the text (robust for long / non-ASCII text)")
    ap.add_argument("--out", default="", help="absolute path of the intended output file")
    ap.add_argument("--speaker", default=DEFAULT_SPEAKER)
    ap.add_argument("--language", default=DEFAULT_LANGUAGE)
    ap.add_argument("--instruct", default="", help="style/delivery instruction")
    ap.add_argument("--device", default="auto", help="auto|cuda|cpu|mps")
    ap.add_argument("--ffmpeg", default="", help="path to ffmpeg (its dir is added to PATH)")
    # model ids
    ap.add_argument("--model", default=DEFAULT_MODEL, help="CustomVoice model id")
    ap.add_argument("--base-model", default=DEFAULT_BASE_MODEL, help="Base (clone) model id")
    ap.add_argument("--design-model", default=DEFAULT_DESIGN_MODEL, help="VoiceDesign model id")
    # design / clone
    ap.add_argument("--description", default="", help="voice description (design mode)")
    ap.add_argument("--description-file", default="",
                    help="path to a UTF-8 file holding the description (robust for long / non-ASCII)")
    ap.add_argument("--sample-text", default="", help="sample text to design a voice from")
    ap.add_argument("--ref-audio", default="", help="clone reference audio path")
    ap.add_argument("--ref-text", default="", help="clone reference transcript")
    # batch
    ap.add_argument("--segments-file", default="", help="JSON list of segments (batch/merge)")
    ap.add_argument("--voice-config", default="", help="voice_config.json path (batch)")
    ap.add_argument("--out-dir", default="", help="per-segment output dir (batch)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="max segments synthesized in parallel (batch; 1 = sequential)")
    # merge
    ap.add_argument("--pause-ms", type=int, default=500, help="pause between different speakers")
    ap.add_argument("--same-same-ms", type=int, default=250, help="pause for same speaker")
    args = ap.parse_args()

    if args.mode == "custom":
        return _run_custom(args)
    if args.mode == "design":
        return _run_design(args)
    if args.mode == "clone":
        return _run_clone(args)
    if args.mode == "batch":
        return _run_batch(args)
    if args.mode == "merge":
        return _run_merge(args)

    print(f"TTS_WORKER_ERROR: unknown mode {args.mode}", file=sys.stderr, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
