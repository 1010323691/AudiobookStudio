"""Qwen3-TTS synthesis worker.

Runs in the ISOLATED TTS virtualenv (``.venv-tts``, Python 3.10) as a one-shot
subprocess, spawned by the app backend (3.14) — see ``backend/engines/tts.py``.
Keeping the heavy ML stack out of the app's 3.14 environment is deliberate:
``qwen-tts`` is only officially supported through Python 3.13 and pulls a large
dependency tree, and the app's venv must stay lean and stable.

Ported (minimal core, not the whole app) from ``alexandria-audiobook/app/tts.py``:
  - model resolve / download  (_resolve_local_model_path / _load_model)
  - model load                (_init_local_custom)
  - inference                 (generate_custom_voice)
  - WAV save                  (_save_wav)  + WAV->MP3 (app/project.py, via pydub)

CLI
---
    python tts_worker.py --text "..." --out <abs mp3 path> \
        [--speaker NAME] [--language chinese] [--instruct "..."] \
        [--model MODEL_ID] [--device auto|cuda|cpu|mps] [--ffmpeg /path/to/ffmpeg]

Contract with the backend (all on STDOUT unless noted)
------------------------------------------------------
  - ``[progress] <0.0-1.0> <label>``  -> backend calls handle.progress(frac, label)
  - ``[result]   <absolute path>``    -> the audio file actually produced (mp3, or
                                         wav if MP3 encoding was unavailable)
  - any other line                    -> forwarded as a log entry
  - exit 0 on success; non-zero on failure, error on STDERR.
"""
from __future__ import annotations

import argparse
import os
import sys

# Default model / voice. ``serena`` is a valid CustomVoice speaker (used by the
# reference project's warmup); it is multilingual and handles Chinese. Override
# with --speaker / config.tts.speaker.
DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_SPEAKER = "serena"
DEFAULT_LANGUAGE = "chinese"


def log(msg: str) -> None:
    """A plain status line -> backend forwards it as a log entry."""
    print(msg, flush=True)


def progress(frac: float, label: str) -> None:
    """A tagged progress line -> backend maps it to handle.progress()."""
    print(f"[progress] {max(0.0, min(1.0, frac)):.2f} {label}", flush=True)


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
    """Load ``Qwen3TTSModel``, preferring a local cache hit; downloads on first run."""
    import torch
    from qwen_tts import Qwen3TTSModel

    dtype = torch.bfloat16 if "cuda" in device else torch.float32
    load_kwargs = {"dtype": dtype}
    if device != "cpu":
        load_kwargs["device_map"] = device

    local_path = _resolve_local_model_path(model_id)
    if local_path:
        log(f"Loading model from local cache: {local_path}")
        try:
            return Qwen3TTSModel.from_pretrained(local_path, **load_kwargs)
        except Exception as e:  # incomplete snapshot -> fall back to a hub download
            log(f"Local cache load failed ({e}); retrying via model id (may download).")
            return Qwen3TTSModel.from_pretrained(model_id, **load_kwargs)
    log(f"Model not cached locally; downloading {model_id} (first run, a few GB)...")
    return Qwen3TTSModel.from_pretrained(model_id, **load_kwargs)


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


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen3-TTS one-shot synthesis worker")
    ap.add_argument("--text", default="", help="text to synthesize (or use --text-file)")
    ap.add_argument("--text-file", default="",
                    help="path to a UTF-8 file holding the text (robust for long / non-ASCII text)")
    ap.add_argument("--out", required=True, help="absolute path of the intended mp3")
    ap.add_argument("--speaker", default=DEFAULT_SPEAKER)
    ap.add_argument("--language", default=DEFAULT_LANGUAGE)
    ap.add_argument("--instruct", default="", help="style/delivery instruction")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="auto", help="auto|cuda|cpu|mps")
    ap.add_argument("--ffmpeg", default="", help="path to ffmpeg (its dir is added to PATH)")
    args = ap.parse_args()

    # Text arrives inline (--text) or, more robustly for long / non-ASCII text, via a
    # UTF-8 file (--text-file). A file sidesteps Windows command-line encoding and the
    # ~32KB command-line length limit entirely.
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            text = f.read().strip()
    else:
        text = (args.text or "").strip()
    if not text:
        print("TTS_WORKER_ERROR: empty input text (use --text or --text-file)",
              file=sys.stderr, flush=True)
        return 2

    # Let pydub find a user-provided ffmpeg (its directory must be on PATH).
    if args.ffmpeg:
        fdir = os.path.dirname(os.path.abspath(args.ffmpeg))
        if fdir:
            os.environ["PATH"] = fdir + os.pathsep + os.environ.get("PATH", "")

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
        import numpy as np

        audio = np.concatenate(wavs) if len(wavs) > 1 else wavs[0]
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


if __name__ == "__main__":
    sys.exit(main())
