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
  batch              all segments in a file, one subprocess, needed models loaded once;
                     segments are padded into native tensor batches; --concurrency is only the
                     per-batch CEILING — the real size follows the segment-length bands and a
                     measured VRAM governor that shrinks / grows it as the run goes
  merge              combine per-segment files (in order) into the final audiobook

Contract with the backend (all on STDOUT unless noted)
------------------------------------------------------
  - ``[progress] <0.0-1.0> <label>`` -> backend calls handle.progress(frac, label)
  - ``[result]   <absolute path>``    -> the primary file produced
  - ``[segment]  <index> ok <path>``  -> one batch segment succeeded
  - ``[segment]  <index> error <reason>`` -> one batch segment failed (batch continues)
  - ``[watchdog] timeout batch=<n> indices=[...] elapsed=<s>`` -> a sub-batch produced no
    output within its budget; the process then exits 124 (the backend shrinks the batch and
    restarts a fresh subprocess)
  - any other line                    -> forwarded as a log entry
  - exit 0 on success; non-zero on failure, error on STDERR.

Batch mode returns 0 even if individual segments failed (they are reported via the
``[segment]`` lines); it returns non-zero only on a *fatal* setup error (missing /
unreadable voice config, empty segment list, model load failure, ...).
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import os
import sys
import threading
import time

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

# Batch-mode guards (see ``plan_sub_batches`` / ``estimate_batch_vram`` below):
MAX_NEW_TOKENS = 2048  # per-row generation cap (the decode length the VRAM budget plans for)
MAX_SEQ_CHARS = 2500   # a row longer than this never shares a batch with shorter rows

# Fixed input tokens a row carries BEYOND its target text (role marker + codec prefill + tts
# structural tokens). The O(L^2) attention term is sized by the row's FULL input length, so the
# VRAM budget must count these or every sub-batch is under-sized (see ``plan_row_tokens``).
ROW_STRUCTURAL_OVERHEAD = 16
# A clone (ICL) row also carries the reference: one frame per ref_code row on the talker's main
# stream plus the ref_text tokens. That is measured per speaker from the built prompt (no GPU
# forward); this conservative total stands in when the prompt is missing or the measurement faults.
CLONE_FALLBACK_OVERHEAD = 140

# Char counts are the SOLE length metric in the planning path (no tokenizer: pricing every
# segment's tokens up-front is slow, and chars x 1.2 is a conservative over-estimate for
# Chinese — it keeps the VRAM budget honest without a tokenizer round-trip).
CHAR_TOKENS_PER_CHAR = 1.2

# Length-class concurrency bands: (class ceiling in chars, fraction of the manual cap).
# A batch is left-padded to its longest row and the O(L^2) attention term is quadratic in that
# length, so long rows must run in smaller batches than short ones; beyond the last band a row
# runs solo. The manual --concurrency value is only a CEILING — the real per-batch size is
# min(length band, the measured VramGovernor cap, the VRAM estimate, the char caps). The
# fractions are deliberately conservative (code predictors + the WDDM driver reservation sit on
# top of the modelled terms); the VramGovernor below corrects the static guess with per-batch
# measurements, so a wrong fraction self-corrects within a few batches of the run.
LENGTH_BANDS = (
    (64, 1.0),    # very short -> full manual cap
    (256, 0.75),  # short
    (512, 0.5),   # medium
    (1024, 0.4),  # long
    (2048, 0.2),  # very long
)  # > 2048 chars -> solo (1)

# VramGovernor reaction thresholds — all against MEASURED numbers (see VramGovernor):
PEAK_PRESSURE_FRAC = 0.9   # a batch consumed >= 90% of the available pool -> shrink
PEAK_GROW_FRAC = 0.55      # consumed < 55% and healthy -> may grow
FREE_FLOOR_GB = 1.0        # free VRAM under this (and under 20% of the total) -> shrink
VRAM_SCALE_MIN, VRAM_SCALE_MAX = 0.5, 4.0  # trust range for the static L^2 estimate

# Live "still generating" heartbeat (see run_with_watchdog): the first line ~FIRST seconds in,
# then one every INTERVAL seconds while a sub-batch decodes. It reports *measured* elapsed time
# against the watchdog budget — never a fabricated percentage (true fractional progress is unknown
# mid-batch) — so a long sub-batch doesn't go silent in the log and the timeout budget stays legible.
HEARTBEAT_FIRST = 10.0     # seconds before the first heartbeat (shorter batches finish before it)
HEARTBEAT_INTERVAL = 20.0  # seconds between subsequent heartbeats


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


# ---------------------------------------------------------------------------
# Batch: native tensor-batch planning + the per-sub-batch watchdog
# ---------------------------------------------------------------------------

def plan_sub_batches(char_lens, *, max_batch, max_batch_chars, max_seq_chars=0,
                     length_ratio=5, min_ratio_size=2, vram_ok=None, tokens=None,
                     band_cap=None):
    """Split rows (sorted by length ASCENDING) into tensor sub-batches, greedily (pure).

    Returns a list of sub-batches; each is a list of positions into ``char_lens``. Every row
    lands in exactly one batch and the smallest batch is always 1 — no row is ever skipped.
    A new batch opens when adding the next row would break any constraint:

      * at most ``max_batch`` rows (the caller's row cap — the governor's current adaptive cap);
      * ``band_cap`` (optional callable chars -> rows): the length-class cap (see
        ``band_cap_for_chars``) — the batch may never exceed the band of its LONGEST row (rows
        are ascending, so the row being added is the longest), which keeps short rows out of
        long-row batches even beyond the ratio rule;
      * batch total chars ``<= max_batch_chars`` (guards an oversized prefill / a TDR hang);
      * a row over ``max_seq_chars`` (if > 0) never mixes with the shorter rows already in the
        open batch (it opens its own batch; its size is then governed by ``vram_ok``);
      * length ratio: with ``>= min_ratio_size`` rows (default 2 — even a two-row batch must
        stay within the ratio), longest/shortest ``> length_ratio`` splits (rows are
        left-padded to the longest, so a wide spread wastes compute);
      * ``vram_ok(tokens)`` (optional callable) returns False for the candidate batch.

    ``tokens`` is the per-row *input* token count (aligned with ``char_lens``); it is what
    ``vram_ok`` is fed (the VRAM budget is token-based, the char caps a coarser backstop).
    """
    char_lens = [max(0, int(c)) for c in char_lens]
    n = len(char_lens)
    if n == 0:
        return []
    if tokens is None:
        tokens = [0] * n
    max_batch = max(1, int(max_batch))

    def _ratio_ok(start, j):
        # Adding position j to the (ascending) batch [start, j): is the spread within ratio?
        if j - start + 1 < min_ratio_size:
            return True
        if start >= j:  # first row of the batch
            return True
        shortest = char_lens[start]  # ascending -> the batch's first row is the shortest
        if shortest <= 0:
            return True
        return char_lens[j] / shortest <= length_ratio

    batches = []
    start = 0
    while start < n:
        j = start
        batch_chars = 0
        batch_tokens = []
        while j < n:
            overlong = max_seq_chars > 0 and char_lens[j] > max_seq_chars
            if overlong and j > start:
                break  # never mix an overlong row with the shorter rows already batched
            if (j - start) >= max_batch:
                break
            if band_cap is not None and (j - start) >= band_cap(char_lens[j]):
                break
            if batch_chars + char_lens[j] > max_batch_chars:
                break
            if not _ratio_ok(start, j):
                break
            if vram_ok is not None and not vram_ok(batch_tokens + [tokens[j]]):
                break
            batch_chars += char_lens[j]
            batch_tokens.append(tokens[j])
            j += 1
        if j == start:
            j = start + 1  # a row that fits no batch still gets a solo batch (min size 1)
        batches.append(list(range(start, j)))
        start = j
    return batches


def sub_batch_timeout_seconds(device, total_chars, vtype="custom"):
    """A sub-batch's watchdog budget: a floor, scaled by the batch's text, then capped (pure).

    GPU decode is fast, so a tight budget (a hung batch is caught in minutes); CPU decode is
    far slower, so a much looser budget (a slow-but-healthy CPU batch must not be killed).

    Clone gets a roomier per-char rate than custom/design (measured on this machine: 16 long
    clone rows = 0.29 s/char on an idle GPU vs ~0.2 for custom, and Windows WDDM time-slices the
    GPU with every other process — browser, compositor — so a budget calibrated on an idle GPU
    false-kills a healthy-but-slow batch under contention; each false kill loses the batch's
    work and burns a restart attempt). A genuinely hung batch is still caught — it just takes
    the (larger) budget to elapse before the process is sacrificed.
    """
    total_chars = max(0, int(total_chars))
    if device == "cpu":
        return max(600, min(10800, int(600 + 4 * total_chars)))
    if vtype == "clone":
        return max(300, min(3600, int(120 + 0.7 * total_chars)))
    return max(180, min(1500, int(60 + 0.4 * total_chars)))


def estimate_batch_vram(num_rows, heads, kv_per_token, seq_tokens, max_new):
    """Estimated peak VRAM (bytes) a tensor batch of ``num_rows`` rows needs (pure).

    Two terms (the reference project counted only the second — exactly why its auto-batches
    OOM'd):
      1. the hand-written O(L^2) attention peak: one fp32 matrix per head, sized by the
         LONGEST row in the batch (rows are left-padded to it):
         ``num_rows * heads * (max(seq_tokens) + max_new)^2 * 4``;
      2. the KV cache (+ ~1.5x headroom for activations), summed per row:
         ``sum(seq_tokens[i] + max_new) * kv_per_token * 1.5``.

    When ``heads`` is unknown (``<= 0``) only the (still real) KV term is usable.
    """
    kv = sum(t + max_new for t in seq_tokens) * kv_per_token * 1.5
    if not heads or heads <= 0:
        return kv
    l_max = max(seq_tokens) if seq_tokens else 0
    attn = num_rows * heads * (l_max + max_new) ** 2 * 4
    return attn + kv


def band_cap_for_chars(n_chars, manual_cap):
    """The effective per-batch row cap for a row of ``n_chars`` chars (pure, char-based).

    Short text pays little padding and a small L^2 term, so it may run at the full manual cap;
    long text inflates every row's padded length (the O(L^2) term is quadratic in it), so its
    cap steps down class by class; beyond the last band a row runs solo. ``manual_cap`` is a
    ceiling the result can never exceed — never a fixed size.
    """
    cap = max(1, int(manual_cap))
    for limit, frac in LENGTH_BANDS:
        if n_chars <= limit:
            return max(1, int(cap * frac))
    return 1


class VramGovernor:
    """Runtime concurrency governor: adapts the per-batch row cap to MEASURED VRAM behaviour.

    The static plan (length bands + the L^2 VRAM estimate) is a guess made before any batch
    runs; the governor is the feedback loop that corrects it as the run goes. After every
    sub-batch the caller reports what the GPU actually did — the pool's free VRAM before and
    after (the difference is the batch's true working set; Windows WDDM starts paging that
    working set to system memory exactly when it eats the pool), the measured throughput, and
    the static estimate the planner used. The governor then:

      * shrinks the cap (halving, floor 1) when the batch consumed most of the available pool,
        left the pool nearly empty, or throughput collapsed while VRAM ran hot;
      * grows the cap (toward the manual ceiling) when the batch stayed well under the pool and
        throughput held;
      * re-calibrates its trust in the static L^2 estimate (``vram_scale``, bounded to
        ``[VRAM_SCALE_MIN, VRAM_SCALE_MAX]``) so the planner's VRAM check follows measured
        reality instead of a fixed guess.

    The manual cap is a ceiling the governor can never cross, and the length bands always apply
    on top (``row_cap_for``) — so the configured 32/64 can only ever be a maximum, never a
    fixed size. Pure arithmetic (no torch): the caller measures, the governor decides — which
    keeps the rules unit-testable in the lean backend venv.
    """

    def __init__(self, manual_cap, *, device="cuda", total_vram=0.0):
        self.manual_cap = max(1, int(manual_cap))
        self.device = device
        self.total_vram = max(0.0, float(total_vram))
        self.cap = self.manual_cap  # the adaptive part (starts at the ceiling, stays within it)
        self.vram_scale = 1.0       # trust factor applied to the static L^2 estimate
        self.rate_ema = None        # measured chars/sec (exponential moving average)
        self.events = []            # (action, detail) — most recent last (fed to the run log)

    @property
    def cuda(self) -> bool:
        return "cuda" in str(self.device)

    def row_cap_for(self, n_chars: int) -> int:
        """The row cap a new sub-batch of this length class may use (adaptive cap ∩ band)."""
        return min(self.cap, band_cap_for_chars(n_chars, self.manual_cap))

    def observe_success(self, *, free_before, free_after, rows, chars, elapsed,
                        static_est=0):
        """Feed one completed sub-batch's measured numbers; returns the action taken (or None).

        ``free_before`` / ``free_after`` are the pool's free VRAM in bytes before the batch and
        after its post-batch cache clear; ``static_est`` is the planner's L^2 estimate for the
        same batch (0 = the VRAM term was inactive, so calibration is skipped).
        """
        if not self.cuda or free_before is None or free_after is None:
            return None
        free_before, free_after = float(free_before), float(free_after)
        if free_before <= 0:
            return None  # a degenerate reading: don't act on it
        rows = max(1, int(rows))
        consumed = max(0.0, free_before - free_after)  # the batch's true (physical) working set
        frac = consumed / free_before

        # Throughput (chars/sec) against its moving average — the "throughput collapsing"
        # signal (a paging / contended GPU shows it here long before an outright fault).
        rate = max(0.0, float(chars)) / max(1e-6, float(elapsed))
        prev_ema = self.rate_ema
        self.rate_ema = rate if self.rate_ema is None else 0.5 * self.rate_ema + 0.5 * rate

        # Re-calibrate trust in the static estimate against the measured working set.
        static_beaten = False
        if static_est and static_est > 0:
            r = consumed / float(static_est)
            if r < 0.6:
                self.vram_scale = min(VRAM_SCALE_MAX, self.vram_scale * 1.5)
            elif r > 1.0:
                self.vram_scale = max(VRAM_SCALE_MIN, self.vram_scale / 1.5)
                static_beaten = True  # the guess was beaten: treat as pressure too

        floor = (min(FREE_FLOOR_GB * (2 ** 30), 0.2 * self.total_vram)
                 if self.total_vram > 0 else FREE_FLOOR_GB * (2 ** 30))
        pressured = (
            frac >= PEAK_PRESSURE_FRAC
            or free_after < floor
            or (frac >= 0.7 and prev_ema is not None and rate < 0.5 * prev_ema)
            or static_beaten
        )
        if pressured:
            new = max(1, self.cap // 2)
            if new < self.cap:
                self.cap = new
                self.events.append(
                    ("shrink", f"峰值占用 {frac:.0%}、剩余 {free_after / 2 ** 30:.1f}GB"))
                return "shrink"
            return None
        if (frac < PEAK_GROW_FRAC and self.cap < self.manual_cap
                and self.rate_ema is not None and rate >= 0.8 * self.rate_ema):
            new = min(self.manual_cap, self.cap + max(1, self.cap // 4))
            if new > self.cap:
                self.cap = new
                self.events.append(("grow", f"峰值占用 {frac:.0%}、余量充足"))
                return "grow"
        return None

    def observe_fault(self, rows) -> str | None:
        """A sub-batch of ``rows`` rows faulted (e.g. OOM): the cap can never propose that size
        again (it drops to the half the in-process retry will run)."""
        new = max(1, min(self.cap, max(1, int(rows) // 2)))
        if new < self.cap:
            self.cap = new
            self.events.append(("fault", f"{rows} 段子批失败"))
            return "fault"
        return None


def run_with_watchdog(fn, timeout_s, batch_label, indices, *, n_rows=None):
    """Run ``fn()`` in a daemon thread; the main thread polls, beats, and kills on timeout.

    ``fn`` runs out-of-line so a hung GPU kernel can't block this poll loop. While it runs, the
    (live) main thread emits a throttled *liveness heartbeat* — a measured "已用时 Xs / 预算 Ys"
    line, never a fabricated percentage (the true fractional progress is unknown mid-batch) — so a
    long sub-batch doesn't go silent in the log and the watchdog budget stays legible. On timeout
    we emit a ``[watchdog]`` line (flushed first, so the backend always sees it) and ``os._exit(124)``
    — the process (and its CUDA context) dies, so the fault can't poison a later run; the backend
    sees exit 124 and shrinks the batch / restarts a fresh subprocess. A non-timeout fault (e.g.
    OOM) is re-raised so the caller can do its one in-process halving retry. Returns ``fn``'s result
    on success. ``n_rows`` (when given) is the segment count shown in each heartbeat.
    """
    box = {}
    start = time.monotonic()

    def _target():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 — surface any fault to the poller
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    budget = max(1.0, float(timeout_s))
    deadline = start + budget
    last_beat = -1.0  # sentinel: the first heartbeat is due at HEARTBEAT_FIRST, not last_beat+interval
    while t.is_alive():
        now = time.monotonic()
        if now > deadline:
            print(f"[watchdog] timeout batch={batch_label} indices={list(indices)} "
                  f"elapsed={now - start:.0f}s", flush=True)
            os._exit(124)  # process + CUDA context die; the backend shrinks the batch, restarts
        # Real-time liveness heartbeat (measured elapsed, NOT a fake percentage).
        if n_rows:
            elapsed = now - start
            due = HEARTBEAT_FIRST if last_beat < 0 else last_beat + HEARTBEAT_INTERVAL
            if elapsed >= due:
                last_beat = elapsed
                log(f"子批 {batch_label}（{n_rows} 段）生成中… 已用时 {elapsed:.0f}s / 预算 {budget:.0f}s")
        time.sleep(0.5)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _clear_gpu_cache(device) -> None:
    """gc + ``torch.cuda.empty_cache()`` between sub-batches (CUDA only); a no-op off CUDA.

    Frees fragmented / cached memory so a later, larger batch can still allocate.
    """
    gc.collect()
    if "cuda" in device:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def plan_row_tokens(texts, instructs, overhead):
    """Per-row input token counts for the VRAM budget, from char counts alone (no tokenizer).

    ``chars x CHAR_TOKENS_PER_CHAR`` (a conservative over-estimate for Chinese) over the row's
    target text + instruct, plus the fixed ``overhead`` a row carries BEYOND its target text
    (structural markers, and for clone the reference frames + ref_text — see
    ``_clone_input_overhead``). The O(L^2) attention term is sized by the longest row's full
    input length, so dropping the overhead systematically under-sizes the budget and admits
    oversized batches.
    """
    if instructs is None:
        instructs = [""] * len(texts)
    return [max(1, int((len(t) + len(i)) * CHAR_TOKENS_PER_CHAR)) + max(0, int(overhead))
            for t, i in zip(texts, instructs)]


def _clone_input_overhead(prompt, voice_data):
    """Fixed per-row input tokens a clone row adds beyond its target text (measured, no GPU
    forward, no tokenizer).

    A clone (ICL) row's talker input is: target text + the reference frames (one per ``ref_code``
    row, on the talker's main stream) + the ref_text + structural markers (see
    ``modeling_qwen3_tts.generate``, ICL branch). The frame count is read from the already-built
    prompt's ``ref_code`` shape; the ref_text is priced in chars (``CHAR_TOKENS_PER_CHAR``).
    Falls back to a conservative ``CLONE_FALLBACK_OVERHEAD`` when the prompt is missing /
    unreadable.
    """
    if prompt is None:
        return CLONE_FALLBACK_OVERHEAD
    try:
        item = prompt[0] if isinstance(prompt, (list, tuple)) else prompt
        rc = getattr(item, "ref_code", None)
        shape = getattr(rc, "shape", None)
        ref_frames = int(shape[0]) if (rc is not None and shape) else 0
        ref_text = getattr(item, "ref_text", None) or (voice_data or {}).get("ref_text") or ""
        ref_text = str(ref_text).strip()
        return (ROW_STRUCTURAL_OVERHEAD + ref_frames
                + int(len(ref_text) * CHAR_TOKENS_PER_CHAR))
    except Exception:  # noqa: BLE001 — a measurement hiccup -> the conservative constant
        return CLONE_FALLBACK_OVERHEAD


def _talker_vram_params(model):
    """The attention/KV params for the L^2 VRAM budget, read from ``model.model.talker.config``.

    Returns ``{"heads": int, "kv_per_token": int}`` or ``None`` (unreadable -> the VRAM term is
    skipped and the char caps govern). ``kv_per_token`` = K+V, bf16 (2B), all layers:
    ``2 * num_key_value_heads * head_dim * 2 * num_hidden_layers``.
    """
    try:
        cfg = model.model.talker.config
        hidden = int(getattr(cfg, "hidden_size", 0) or 0)
        layers = int(getattr(cfg, "num_hidden_layers", 0) or 0)
        heads = int(getattr(cfg, "num_attention_heads", 0) or 0)
        kv_heads = int(getattr(cfg, "num_key_value_heads", 0) or 0) or heads
        if not (hidden and layers and heads):
            return None
        head_dim = hidden // heads
        return {"heads": heads, "kv_per_token": 2 * kv_heads * head_dim * 2 * layers}
    except Exception:  # noqa: BLE001
        return None


def _free_vram_budget(device):
    """The VRAM a new batch may use: 80% of the currently-free GPU memory, or ``None`` off CUDA.

    ``None`` means the VRAM term doesn't participate (the char + manual caps still bound the
    batch). ``mem_get_info`` also reports memory this process already holds, so the budget
    naturally shrinks as the model + KV cache accumulate.
    """
    if "cuda" not in device:
        return None
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        free, _total = torch.cuda.mem_get_info()
        return int(free * 0.8)
    except Exception:  # noqa: BLE001
        return None


def _free_vram(device):
    """The pool's currently-free VRAM in bytes, or ``None`` off CUDA / on any torch fault.

    (Unlike ``_free_vram_budget`` this is the RAW free figure — the number the VramGovernor
    measures a batch's working set against.)
    """
    if "cuda" not in device:
        return None
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return int(torch.cuda.mem_get_info()[0])
    except Exception:  # noqa: BLE001
        return None


def _total_vram(device):
    """The GPU's total VRAM in bytes (0 off CUDA / on fault) — sizes the governor's free floor."""
    if "cuda" not in device:
        return 0
    try:
        import torch
        if not torch.cuda.is_available():
            return 0
        return int(torch.cuda.mem_get_info()[1])
    except Exception:  # noqa: BLE001
        return 0


def _warmup(model, vtype, language, device) -> None:
    """One tiny generation before the first real batch (CUDA only) to absorb first-call
    overhead (kernels / allocator warm-up) so the first measured batch is fair. Clone is
    skipped (it needs a prompt); a warm on any loaded model warms the shared GPU kernels."""
    if "cuda" not in device or vtype not in ("custom", "design"):
        return
    try:
        import torch
        if not torch.cuda.is_available():
            return
        with torch.no_grad():
            if vtype == "design":
                model.generate_voice_design(
                    text="你好", instruct="a clear, natural speaking voice", language=language,
                    non_streaming_mode=True, max_new_tokens=32)
            else:
                model.generate_custom_voice(
                    text="你好", language=language, speaker=DEFAULT_SPEAKER,
                    instruct="neutral", non_streaming_mode=True, max_new_tokens=32)
        log("warmup 完成（已吸收首次调用开销）")
    except Exception as e:  # noqa: BLE001 — warmup is best-effort, never fatal
        log(f"warmup 跳过（{e}）")


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


def _effective_instruct(r, vtype):
    """The instruct/description actually sent for a row (token counting must match generation)."""
    if vtype == "custom":
        return (r["instruct"] or (r["vd"].get("default_style") or "").strip() or "neutral")
    if vtype == "design":
        base = (r["vd"].get("description") or "").strip()
        if base and r["instruct"]:
            return f"{base}, {r['instruct']}"
        return base or r["instruct"] or "A clear, natural speaking voice"
    return ""  # clone: no instruct


def _generate_rows(model, vtype, rows, args, clone_prompts, batch_seed):
    """The GPU generate for a sub-batch (the model's list API). Returns ``[(ok, payload), ...]``
    in row order. Runs in the watchdog's daemon thread; it never saves files or emits protocol
    lines (that happens on the main thread after the watchdog returns, so the ``[segment]`` /
    ``[progress]`` stream stays single-threaded and monotonic)."""
    if batch_seed is not None:
        import torch

        torch.manual_seed(batch_seed)
    texts = [r["text"] for r in rows]
    if vtype == "custom":
        speakers = [(r["vd"].get("voice") or args.speaker or DEFAULT_SPEAKER) for r in rows]
        instructs = [_effective_instruct(r, "custom") for r in rows]
        wavs, _sr = model.generate_custom_voice(
            text=texts, language=args.language, speaker=speakers, instruct=instructs,
            non_streaming_mode=True, max_new_tokens=MAX_NEW_TOKENS)
    elif vtype == "clone":
        # a clone sub-batch is single-speaker; the per-speaker length-1 prompt broadcasts to N
        prompt = clone_prompts[rows[0]["speaker"]]
        wavs, _sr = model.generate_voice_clone(
            text=texts, voice_clone_prompt=prompt,
            non_streaming_mode=True, max_new_tokens=MAX_NEW_TOKENS)
    else:  # design (a design sub-batch is always size 1, each with its own description)
        description = _effective_instruct(rows[0], "design")
        wavs, _sr = model.generate_voice_design(
            text=texts, instruct=description, language=args.language,
            non_streaming_mode=True, max_new_tokens=MAX_NEW_TOKENS)
    if not wavs:
        return [(False, "模型未返回音频")] * len(rows)
    return [(True, (w, _sr)) for w in wavs]


def _save_and_report(rows, results, out_dir, width, report) -> None:
    """Save each generated row (wav -> mp3) and emit its ``[segment]`` line + progress.

    Runs on the main (coordinator) thread after the watchdog returns. A per-row save / encode
    fault is a recorded error, never a run abort (matching the old per-segment tolerance).
    """
    import numpy as np

    for i, r in enumerate(rows):
        index = r["index"]
        ok, payload = results[i]
        if not ok:
            report(index, False, payload if isinstance(payload, str) else str(payload))
            continue
        wav, sr = payload
        if not isinstance(wav, np.ndarray):
            wav = np.array(wav)
        if wav.size == 0:
            report(index, False, "模型返回空音频")
            continue
        fname = str(index + 1).zfill(width)
        out_mp3 = os.path.join(out_dir, fname + ".mp3")
        wav_tmp = os.path.join(out_dir, fname + ".wav")
        try:
            _save_wav(wav, sr, wav_tmp)
            if _wav_to_mp3(wav_tmp, out_mp3):
                produced = out_mp3
                try:
                    if os.path.exists(wav_tmp):
                        os.remove(wav_tmp)
                except OSError:
                    pass
            else:
                produced = wav_tmp  # MP3 unavailable -> keep the WAV
            report(index, True, produced)
        except Exception as e:  # noqa: BLE001 — one bad row must not kill the batch
            for p in (wav_tmp, out_mp3):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            report(index, False, str(e))


def _synth_sub_batch(model, vtype, rows, *, args, clone_prompts, device, seed, sub_counter,
                     out_dir, width, report, gov=None):
    """Generate + save + report a sub-batch, under its watchdog, with in-process halving retry.

    ``sub_counter`` is a mutable ``[int]`` (a global sequence shared across every sub-batch, so a
    fixed seed + fixed layout reproduces). On a non-timeout fault (e.g. OOM) the GPU cache is
    cleared, the governor is told (so no later batch re-proposes a failing size), the batch split
    in half, and each half retried recursively (each under its own watchdog) down to size 1. A
    size-1 fault hands off to the backend (``os._exit(124)``) so the specific segment can be
    struck / isolated. Timeouts never retry in-process (a hang means the GPU context is suspect)
    — the watchdog already ``os._exit``'d.
    """
    if model is None:
        for r in rows:
            report(r["index"], False, "所需模型未加载")
        return

    sub_counter[0] += 1
    label = f"{vtype}#{sub_counter[0]}"
    indices = [r["index"] for r in rows]
    batch_seed = (seed + sub_counter[0]) if seed >= 0 else None
    total_chars = sum(r["chars"] for r in rows)

    def _gen():
        return _generate_rows(model, vtype, rows, args, clone_prompts, batch_seed)

    try:
        results = run_with_watchdog(
            _gen, sub_batch_timeout_seconds(device, total_chars, vtype), label, indices,
            n_rows=len(rows))
    except Exception as e:  # noqa: BLE001 — a fault (e.g. OOM), not a timeout
        if len(rows) == 1:
            # even a lone row failed -> hand off to the backend to strike / isolate it
            log(f"段 {indices[0] + 1} 生成失败（{e}）——交由后端隔离")
            _clear_gpu_cache(device)
            os._exit(124)
        if gov is not None:
            gov.observe_fault(len(rows))  # no later batch may re-propose a failing size
        log(f"子批 {label}（{len(rows)} 段）生成失败（{e}）——清空显存缓存并对半拆分重试")
        _clear_gpu_cache(device)
        mid = len(rows) // 2
        _synth_sub_batch(model, vtype, rows[:mid], args=args, clone_prompts=clone_prompts,
                         device=device, seed=seed, sub_counter=sub_counter,
                         out_dir=out_dir, width=width, report=report, gov=gov)
        _synth_sub_batch(model, vtype, rows[mid:], args=args, clone_prompts=clone_prompts,
                         device=device, seed=seed, sub_counter=sub_counter,
                         out_dir=out_dir, width=width, report=report, gov=gov)
        return

    _save_and_report(rows, results, out_dir, width, report)


def plan_next_sub_batch(remaining, *, vtype, overhead, params, budget, gov,
                        max_batch, max_batch_chars):
    """One lazy planning round (pure): the next sub-batch + the rows left after it.

    The round's row cap is the governor's current adaptive cap (1 for design rows — each
    carries its own voice description); the length bands apply on top (a batch may never
    exceed the band of its longest row), as do the char caps and the (trust-scaled) VRAM
    estimate. The planner's first batch is always a prefix of the (ascending) rows, so the
    remainder is well defined. Returns ``(batch_rows, remaining_rows)``.
    """
    tokens = plan_row_tokens([r["text"] for r in remaining],
                             [_effective_instruct(r, vtype) for r in remaining], overhead)
    vram_ok = None
    if params is not None and budget is not None:
        heads, kvt = params["heads"], params["kv_per_token"]

        def vram_ok(toks, _h=heads, _k=kvt, _b=budget):
            # the static L^2 estimate, scaled by the governor's measured trust
            return (estimate_batch_vram(len(toks), _h, _k, toks, MAX_NEW_TOKENS)
                    <= _b * gov.vram_scale)
    rows_cap = 1 if vtype == "design" else gov.cap
    batches = plan_sub_batches(
        [r["chars"] for r in remaining],
        max_batch=rows_cap,
        band_cap=lambda c: band_cap_for_chars(c, max_batch),
        max_batch_chars=max_batch_chars, max_seq_chars=MAX_SEQ_CHARS,
        vram_ok=vram_ok, tokens=tokens)
    pos = batches[0]  # always a prefix of the ascending rows
    pos_set = set(pos)
    return [remaining[i] for i in pos], [r for i, r in enumerate(remaining) if i not in pos_set]


def _run_batch(args) -> int:
    """Synthesize every segment in ``--segments-file``, one subprocess.

    Loads only the models the batch needs, once; then runs the segments as **native tensor
    batches** (the model's list API pads a group of rows into one forward, so the GPU truly
    processes several segments at once) instead of the old one-segment-per-thread pool.
    ``--concurrency`` is only a ceiling: the real per-batch size follows the length bands
    (short rows run at the full cap, long rows step down, extreme rows run solo) and a measured
    VramGovernor that shrinks / grows it batch by batch from the GPU's actual VRAM consumption
    and throughput (never chasing 100% VRAM). Rows run ascending by length (short first: early
    progress + crash resilience); files are still named by segment index, so the generation
    order never affects the assembled audiobook. Each sub-batch runs under a watchdog that kills
    the process on a hang (exit 124) so the backend can shrink and restart. A per-segment fault
    (missing config, model error) is a recorded ``[segment]`` line, never a run abort.
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

    # -- batch parameters ---------------------------------------------------------
    # ``--concurrency`` is only the MANUAL CEILING (the most rows that may EVER share one tensor
    # batch); the real per-batch size is set at runtime by min(length band, the measured
    # VramGovernor cap, the VRAM estimate, the char caps). Clamped to [1, 64] (the backend
    # already clamps; defence in depth).
    max_batch = max(1, min(64, int(args.concurrency)))
    seed = int(args.seed)
    max_batch_chars = max(1000, int(args.max_batch_chars))
    log(f"批内段数上限 {max_batch}（仅上限；实际每批条数 = min(段长分档, 动态调节, 显存估算, 字符上限)）· "
        f"单批 ≤{max_batch_chars} 字 · 单行 ≤{MAX_SEQ_CHARS} 字")
    log("段长分档：" + " · ".join(
        f"≤{limit}字→{band_cap_for_chars(limit, max_batch)}段" for limit, _f in LENGTH_BANDS)
        + f" · >{LENGTH_BANDS[-1][0]}字→单独")
    # An honest note about the attention backend (no flash-attn / SDPA path in this qwen_tts).
    log("注意力：手写 O(L²)（本环境无 flash-attn / SDPA；峰值已计入显存预算与字符上限）")
    if seed >= 0:
        log(f"seed = {seed}（可复现：同输入 + 同 seed + 同批布局 → 相同结果）")

    # -- classify every segment: immediate errors, then the three generation groups --
    total = len(segments)
    width = max(4, len(str(total)))
    counts = {"completed": 0, "failed": 0}
    clone_prompts: dict = {}

    custom_rows = []
    clone_rows_by_speaker: dict = {}
    design_rows = []

    def report_result(index, ok, detail):
        """Emit the ``[segment]`` line + progress (single-threaded, monotonic)."""
        if ok:
            _segment_ok(index, detail)
            counts["completed"] += 1
        else:
            _segment_error(index, detail)
            counts["failed"] += 1
        done = counts["completed"] + counts["failed"]
        progress(0.05 + 0.95 * (done / total),
                 f"完成 {done}/{total} 段（成功 {counts['completed']} / 失败 {counts['failed']}）")

    for seg in segments:
        index = int(seg.get("index", 0))
        speaker = (seg.get("speaker") or "").strip()
        text = (seg.get("text") or "").strip()
        instruct = (seg.get("instruct") or "").strip()

        if not text:
            report_result(index, False, "空文本")
            continue

        canonical = _resolve_alias(speaker, voice_config)
        vd = voice_config.get(canonical) or {}
        if not vd:
            report_result(index, False,
                          f"缺少角色声音配置（{speaker or canonical}）——请先在「角色声音」页生成")
            continue
        vtype = vd.get("type", "custom")
        if vtype not in SUPPORTED_TYPES:
            report_result(index, False, f"不支持的声音类型：{vtype}")
            continue

        row = {"seg": seg, "index": index, "speaker": canonical, "vd": vd,
               "text": text, "instruct": instruct, "chars": len(text)}
        if vtype == "custom":
            custom_rows.append(row)
        elif vtype == "clone":
            clone_rows_by_speaker.setdefault(canonical, []).append(row)
        else:  # design
            design_rows.append(row)

    # -- build the execution groups: one entry per (model, vtype, rows) — planned lazily --
    # Rows are sorted ascending by length within a group (short first: early progress + crash
    # resilience), and each group's rows are planned into sub-batches LAZILY at run time (one
    # sub-batch per planning round, the remainder re-planned after each round) so the governor's
    # measured adjustments reshape every batch that follows.
    groups = []  # each: {"model", "vtype", "rows" (ascending by length), "overhead"}

    if custom_rows:
        groups.append({"model": models.get("custom"), "vtype": "custom",
                       "rows": sorted(custom_rows, key=lambda r: r["chars"]),
                       "overhead": ROW_STRUCTURAL_OVERHEAD})
    for canonical, rows in clone_rows_by_speaker.items():
        # each speaker's clone prompt is built once and reused across that speaker's sub-batches
        model = models.get("clone")
        if model is None:
            for r in rows:
                report_result(r["index"], False, "Base 模型未加载")
            continue
        try:
            clone_prompts[canonical] = _build_clone_prompt(
                model, voice_config[canonical], os.getcwd(), canonical)
        except Exception as e:  # noqa: BLE001 — a bad reference poisons only this speaker's rows
            for r in rows:
                report_result(r["index"], False, f"克隆提示构建失败：{e}")
            continue
        # Measure this speaker's fixed per-row input overhead (reference frames + ref_text +
        # structural) from the prompt just built, so the VRAM budget sizes this group's
        # sub-batches against the rows' TRUE sequence length (not just the target text).
        groups.append({"model": model, "vtype": "clone",
                       "rows": sorted(rows, key=lambda r: r["chars"]),
                       "overhead": _clone_input_overhead(clone_prompts[canonical],
                                                         voice_config[canonical])})
    if design_rows:
        # each design row is unique (its own description) -> one row per sub-batch
        groups.append({"model": models.get("design"), "vtype": "design",
                       "rows": sorted(design_rows, key=lambda r: r["chars"]),
                       "overhead": ROW_STRUCTURAL_OVERHEAD})

    if not groups:
        # nothing to generate (every segment was an immediate error) — report and finish
        progress(1.0, f"完成（成功 {counts['completed']} / 失败 {counts['failed']} / 共 {total}）")
        log(f"批量合成结束：成功 {counts['completed']}，失败 {counts['failed']}，共 {total} 段。"
            f"输出目录：{out_dir}")
        return 0

    gov = VramGovernor(max_batch, device=device, total_vram=_total_vram(device))

    # warm up the GPU once, on the model the first sub-batch will use (CUDA only)
    first_model = groups[0]["model"]
    if first_model is not None:
        _warmup(first_model, groups[0]["vtype"], args.language, device)

    # -- run: plan each group's remaining rows one sub-batch at a time ----------------
    # Every planning round sees the caps the governor's measurements justify, so the run adapts
    # to the GPU as it goes (the static estimate is only the first guess; the measured loop is
    # the authority).
    sub_counter = [0]  # global sub-batch sequence (a reproducible per-sub-batch seed offset)
    for g in groups:
        vtype = g["vtype"]
        params = _talker_vram_params(g["model"]) if g["model"] is not None else None
        remaining = g["rows"]
        while remaining:
            budget = _free_vram_budget(device) if params is not None else None  # fresh per round
            rows_b, remaining = plan_next_sub_batch(
                remaining, vtype=vtype, overhead=g["overhead"], params=params, budget=budget,
                gov=gov, max_batch=max_batch, max_batch_chars=max_batch_chars)

            rows_cap = 1 if vtype == "design" else gov.cap
            log(f"子批（{vtype}）：{len(rows_b)} 段（批内上限 {rows_cap}）")
            free_before = _free_vram(device)
            t0 = time.monotonic()
            _synth_sub_batch(
                g["model"], vtype, rows_b,
                args=args, clone_prompts=clone_prompts, device=device, seed=seed,
                sub_counter=sub_counter, out_dir=out_dir, width=width, report=report_result,
                gov=gov)
            _clear_gpu_cache(device)
            elapsed = time.monotonic() - t0
            free_after = _free_vram(device)
            static_est = 0
            if params is not None:
                static_est = estimate_batch_vram(
                    len(rows_b), params["heads"], params["kv_per_token"],
                    plan_row_tokens([r["text"] for r in rows_b],
                                    [_effective_instruct(r, vtype) for r in rows_b],
                                    g["overhead"]),
                    MAX_NEW_TOKENS)
            action = gov.observe_success(
                free_before=free_before, free_after=free_after, rows=len(rows_b),
                chars=sum(r["chars"] for r in rows_b), elapsed=elapsed, static_est=static_est)
            if action:
                _what, _detail = gov.events[-1]
                log(f"动态并发{action}（{_detail}）→ 批内上限 {gov.cap}")

    progress(1.0, f"完成（成功 {counts['completed']} / 失败 {counts['failed']} / 共 {total}）")
    log(f"批量合成结束：成功 {counts['completed']}，失败 {counts['failed']}，共 {total} 段。"
        f"输出目录：{out_dir}")
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
                    help="per-batch CEILING: the max rows in one tensor batch (batch mode); the "
                         "length bands + measured VRAM governor set the actual size (1 = sequential)")
    ap.add_argument("--max-batch-chars", type=int, default=12000,
                    help="max total chars in one sub-batch (batch; guards an oversized prefill)")
    ap.add_argument("--seed", type=int, default=-1,
                    help="reproducible seed offset per sub-batch (batch; -1 = random)")
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
