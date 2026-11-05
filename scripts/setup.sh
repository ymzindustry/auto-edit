#!/usr/bin/env bash
# setup.sh — (Re)create the three Python venvs inside the profile, and install deps.
# Run once from the profile root:  bash scripts/setup.sh
#
# Everything lands INSIDE the profile: no root, no system packages, no ~/.u2net.
# Uses the bundled uv + CPython 3.11 when present (the versions below do not build
# on a newer system python), and falls back to `python3 -m venv` otherwise.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="$(dirname "$HERE")"          # /home/ymz/.hermes/profiles/videoeditor
VENV_AUDIO="$PROFILE/venv_audio"
VENV_VIDEO="$PROFILE/venv_video"
VENV_STT="$PROFILE/venv_stt"
UV="$PROFILE/toolchain/bin/uv"
PY311="$PROFILE/toolchain/python/cpython-3.11.17-linux-x86_64-gnu/bin/python3"

make_venv() {                         # make_venv <dir>
    if [ -x "$UV" ] && [ -x "$PY311" ]; then
        UV_LINK_MODE=copy "$UV" venv "$1" --python "$PY311"
    else
        python3 -m venv "$1"
    fi
}

install() {                           # install <venv> <pip args...>
    local venv="$1"; shift
    if [ -x "$UV" ] && [ -x "$PY311" ]; then
        UV_LINK_MODE=copy "$UV" pip install --python "$venv/bin/python" "$@"
    else
        "$venv/bin/python" -m pip install --upgrade pip
        "$venv/bin/python" -m pip install "$@"
    fi
}

# ---- audio venv (torch / deepfilternet / scipy) : stages 1-2 ----------------
if [ ! -x "$VENV_AUDIO/bin/python" ]; then
    make_venv "$VENV_AUDIO"
    # torch/torchaudio MUST be pinned to 2.0.1 / 2.0.2 for deepfilternet 0.5.6
    install "$VENV_AUDIO" torch==2.0.1 torchaudio==2.0.2 \
        --index-url https://download.pytorch.org/whl/cpu
    install "$VENV_AUDIO" deepfilternet==0.5.6 numpy==1.26.4 scipy imageio-ffmpeg
else
    echo "audio venv already present."
fi

# ---- video venv (rembg / opencv) : stages 2-4 ------------------------------
if [ ! -x "$VENV_VIDEO/bin/python" ]; then
    make_venv "$VENV_VIDEO"
    install "$VENV_VIDEO" "rembg[cpu]" opencv-python-headless numpy pillow imageio-ffmpeg
else
    echo "video venv already present."
fi

# ---- speech-to-text venv (faster-whisper) : transcription -------------------
# huggingface_hub must stay < 1 (hub 2.x dropped `requests`, which faster-whisper
# 1.0.3 imports), otherwise `import faster_whisper` fails at startup.
if [ ! -x "$VENV_STT/bin/python" ]; then
    make_venv "$VENV_STT"
    install "$VENV_STT" "faster-whisper==1.0.3" "huggingface_hub<1" requests \
        "numpy<2" imageio-ffmpeg
else
    echo "stt venv already present."
fi

echo
echo "Done. Everything is in $PROFILE"
echo "  full test chain : python3 scripts/test_workflow.py"
echo "  one stage       : python3 scripts/workflows/fragmentation_workflow.py <folder>"
