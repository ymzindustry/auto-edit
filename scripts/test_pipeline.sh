#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  test_pipeline.sh — the whole test, one command per stage, runnable by hand.
#
#  The 5 stages live in scripts/workflows/. This script only calls them in order:
#  you can copy any single line into your shell to replay just that stage.
#
#      ./scripts/test_pipeline.sh            # everything (10 s slice)
#      ./scripts/test_pipeline.sh 3          # start at stage 3 (carbg)
#
#  Stages:
#    0  cut        main.MP4 + screen.mp4 -> first 10 s
#    1  fragment   silence cut + voice enhancement (DeepFilterNet) + transcription
#    2  green      u2net background removal, HAND ONLY
#    3  carbg      green replaced by carbg.png, HAND ONLY
#    4  screen     screen on the LEFT of the hand (plan: scripts/test_effects.py)
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/.."                       # profile root

SRC="raw_data/video"                          # main.MP4 + screen.mp4 + carbg.png
PROJ="raw_data/video/test10s"                 # working project (fragmentation/ .. effects/ are created here)
SECONDS_CUT=10                                # length of the test slice
BG="raw_data/video/carbg.png"                 # image put behind the hand
PLAN="scripts/test_effects.py"                # effect parameters (stage 4)
MODEL="u2net"                                 # segmentation model (hand)
START="${1:-0}"

# ── 0. CUT — only the first 10 s, otherwise far too long to process ──────────
if [ "$START" -le 0 ]; then
  echo "── 0. cut ${SECONDS_CUT}s ───────────────────────────────────────────────"
  mkdir -p "$PROJ"
  for cam in main screen; do
    src=$(ls "$SRC/$cam".* 2>/dev/null | head -1)
    ffmpeg -y -v warning -i "$src" -t "$SECONDS_CUT" -map 0:v:0 -map 0:a:0? \
      -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
      -c:a aac -b:a 192k -movflags +faststart "$PROJ/$cam.mp4"
  done
fi

# ── 1. FRAGMENT + ENHANCE — cut the silences, clean the voice, transcribe ────
#    also writes $PROJ/fragmentation/preview_720p.mp4 : every segment glued back,
#    to judge the cut (--no-preview to skip, --preview-only to rebuild it)
if [ "$START" -le 1 ]; then
  echo "── 1. fragment + enhance ────────────────────────────────────────────────"
  python3 scripts/workflows/fragmentation_workflow.py "$PROJ"
fi

# ── 2. GREEN — a solid green background behind the hand (u2net) ──────────────
if [ "$START" -le 2 ]; then
  echo "── 2. green screen, hand only ───────────────────────────────────────────"
  python3 scripts/workflows/greenscreen_workflow.py "$PROJ" \
    --seg-model "$MODEL" --only main
fi

# ── 3. CARBG — that green is replaced by the carbg image (hand only) ─────────
if [ "$START" -le 3 ]; then
  echo "── 3. carbg background, hand only ───────────────────────────────────────"
  python3 scripts/workflows/background_workflow.py "$PROJ" \
    --background "$BG" --only main --outline ""
fi

# ── 4. SCREEN LEFT — screen on the left of the hand ──────────────────────────
echo "── 4. screen on the left of the hand ──────────────────────────────────────"
python3 scripts/workflows/effects_workflow.py --plan "$PLAN" --input "$PROJ"

echo
echo "done -> $PROJ/effects/   (one picture per segment: screen | hand)"
echo "preview  : python3 scripts/preview.py $PROJ/fragmentation"
