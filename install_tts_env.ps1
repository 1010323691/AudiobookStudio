# install_tts_env.ps1
# Build the isolated TTS engine environment (kept separate from the 3.14 app backend).
#   .venv-tts  (Python 3.10)
#     - torch / torchaudio  2.11.0, from the PyTorch cu128 index (= CUDA build,
#       supports RTX 5090 / sm_120) - installed LAST and force-reinstalled
#     - qwen-tts 0.1.1 + transformers 4.57.3 + accelerate 1.12.0
#     - soundfile / pydub / numpy / huggingface_hub
#
# INSTALL ORDER MATTERS (pattern ported from Alexandria's install.js + torch.js):
#   [3a] the rest of the TTS stack from PyPI FIRST. On Windows this pulls a
#        CPU-only torch in transitively (PyPI has no CUDA wheel) - expected.
#   [3b] the CUDA torch trio LAST, force-reinstalled from the PyTorch cu128
#        index with --no-deps, so nothing runs afterwards that could replace it.
#        The original script had this backwards (torch first, PyPI deps after)
#        and the PyPI step silently swapped the CUDA build for the CPU wheel
#        (cuda_available ended up False). --no-deps is safe on Windows: the cu128
#        wheel bundles the CUDA runtime DLLs, so no nvidia-* packages are needed.
#
# Usage (from the project root, the directory containing this file):
#   powershell -ExecutionPolicy Bypass -File .\install_tts_env.ps1
#
# Notes:
#   - ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 with the system ANSI
#     codepage (e.g. GBK), so any non-ASCII text would be mangled and break parsing.
#   - $ErrorActionPreference is "Continue", not "Stop": in PS 5.1 a native command
#     (py/pip/uv/python) writing to stderr is wrapped in an ErrorRecord and would
#     abort a "Stop" script even on success. We detect real failures via $LASTEXITCODE.
#   - Step 4 ASSERTS cuda_available=True and throws otherwise: a python one-liner
#     exits 0 even when it prints False, so the old exit-code-only check let a
#     CPU-only environment pass silently.
#
# Idempotent: removes and rebuilds .venv-tts if it already exists.

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot

# uv is invoked as `py -m uv` (it was pip-installed into the user site, not on PATH),
# which is robust to PATH and exe-location issues.
function Test-Uv { py -m uv --version; return ($LASTEXITCODE -eq 0) }

Write-Output ""
Write-Output "===== [1/4] ensure uv (invoked via `py -m uv`) ====="
if (-not (Test-Uv)) {
    py -m pip install --quiet --upgrade uv
    if ($LASTEXITCODE -ne 0) { throw "pip install uv failed (exit $LASTEXITCODE)" }
}
Write-Output "uv: " + (py -m uv --version)

Write-Output ""
Write-Output "===== [2/4] fetch Python 3.10 and create .venv-tts ====="
$venv = Join-Path $root ".venv-tts"
if (Test-Path $venv) {
    Write-Output ".venv-tts exists; removing and rebuilding..."
    Remove-Item -Recurse -Force $venv
}
& py -m uv python install 3.10
if ($LASTEXITCODE -ne 0) { throw "uv python install 3.10 failed (exit $LASTEXITCODE)" }
& py -m uv venv $venv --python 3.10
if ($LASTEXITCODE -ne 0) { throw "uv venv failed (exit $LASTEXITCODE)" }
$py = Join-Path $venv "Scripts\python.exe"
Write-Output "venv python: $py"

Write-Output ""
Write-Output "===== [3/4] install TTS dependencies ====="
# [3a] TTS deps from PyPI. This brings in a CPU-only torch transitively on Windows
# - expected and harmless, [3b] force-replaces it with the CUDA build.
Write-Output "[3a] TTS deps from PyPI (brings in a CPU torch; [3b] replaces it)"
& py -m uv pip install -p $py "qwen-tts==0.1.1" "transformers==4.57.3" "accelerate==1.12.0" soundfile pydub numpy huggingface_hub
if ($LASTEXITCODE -ne 0) { throw "uv pip install TTS deps failed (exit $LASTEXITCODE)" }
# [3b] CUDA torch trio, installed LAST. --force-reinstall overwrites the CPU wheel
# from [3a]; --no-deps drops only these wheels in, never re-resolving the rest of
# the environment. Pinned to 2.11.0 = the build verified working on the RTX 5090
# (Alexandria pins 2.7.0, which does NOT support sm_120 - do not copy that pin).
Write-Output "[3b] FORCE CUDA torch/torchaudio 2.11.0 from PyTorch cu128 index (runs last)"
& py -m uv pip install -p $py "torch==2.11.0" "torchaudio==2.11.0" --index-url "https://download.pytorch.org/whl/cu128" --force-reinstall --no-deps
if ($LASTEXITCODE -ne 0) { throw "uv pip install torch (cu128) failed (exit $LASTEXITCODE)" }

Write-Output ""
Write-Output "===== [4/4] verify GPU (expect: cuda_available=True, NVIDIA GeForce RTX 5090) ====="
$gpu = @(& $py -c "import torch;print('cuda_available='+str(torch.cuda.is_available()));print('device='+(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE'));print('torch='+torch.__version__)")
$gpu | ForEach-Object { Write-Output $_ }
if (($gpu -join ' ') -notmatch 'cuda_available=True') {
    throw "GPU check FAILED: .venv-tts has a CPU-only torch build. Do not use it for GPU synthesis - fix step [3b] first."
}
& $py -c "import qwen_tts;from qwen_tts import Qwen3TTSModel;print('qwen_tts import OK')"
if ($LASTEXITCODE -ne 0) { throw "qwen_tts import check failed (exit $LASTEXITCODE)" }

Write-Output ""
Write-Output "===== done ====="
Write-Output "Next: run tts-engine\tts_worker.py with .venv-tts to confirm it produces an mp3."
