#!/home/ymz/.hermes/profiles/videoeditor/venv_video/bin/python
"""
background_workflow.py — greenscreen/ -> background/ (the green is replaced)

    python3 scripts/workflows/background_workflow.py <folder> --background <image|video> [options]

WHAT IT DOES
  Keys out the green with a fast chroma key (no AI: the segmentation already happened in
  greenscreen/) and composites every segment over the background. A video background is
  advanced by the sum of the segment durations, so it never jumps at the joins.

OPTIONS
  --background FILE     the image or video to put behind the subject (required)
  --out DIR             output folder (default: <project>/background)
  --key HEX             colour to remove (00FF00)
  --only PREFIX         only the camera files whose name starts with this prefix
  --max-size WxH        working resolution ('none' = as it is)
  --tolerance N         how close a pixel must be to the key to be removed
  --bg-fit MODE         cover / contain / stretch
  --spill N             green halo removal on the edges (0.6)
  --outline COLOR       ring around the subject; pass "" for no ring
  --outline-width N     ring thickness
  --crf N               H.264 quality (18)

EXAMPLES
    python3 scripts/workflows/background_workflow.py shoulder --background ../carbg.png --only main --outline ""
    python3 scripts/workflows/background_workflow.py shoulder --background ../salon.mp4
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
PROFILE = os.path.dirname(SCRIPTS)
_VENV_DIR = os.path.join(PROFILE, "venv_video")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (os.environ.get("_WK_REEXEC") != "1" and os.path.exists(_VENV_PY)
        and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR)):
    os.environ["_WK_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import glob
import re
import shutil
import subprocess

VENV_VIDEO = os.environ.get("VENV_VIDEO", os.path.join(PROFILE, "venv_video", "bin", "python"))

sys.path.insert(0, SCRIPTS)
import segments as seglib


def wanted(name: str, prefixes) -> bool:
    """True when this file must be treated (no prefix given = treat everything)."""
    return (not prefixes) or any(name.startswith(p) for p in prefixes)


def run(cmd: list) -> None:
    print("  $ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def probe_duration(path: str) -> float:
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    out = subprocess.run([ff, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def require_wk(folder: str, name: str) -> str:
    path = os.path.join(folder, name)
    if not os.path.isdir(path):
        raise SystemExit(f"Missing '{name}' folder in {folder} — run the previous workflow first.")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Replace the green of <folder>/greenscreen with an image/video -> <folder>/background.")
    ap.add_argument("input", metavar="FOLDER",
                    help="Project folder (holding greenscreen/), the src_dir folder itself, or ANY "
                         "folder that directly holds segment_i/*.mp4 + *.txt")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="Output folder for the composited segments (default: background/ next "
                         "to the input's project folder)")
    ap.add_argument("--background", required=True,
                    help="Background image or video (a video is looped and advanced so it "
                         "never jumps between segments)")
    ap.add_argument("--key", default="00FF00",
                    help="Colour keyed out of src_dir, as hex (default 00FF00 = the green "
                         "greenscreen_workflow painted)")
    ap.add_argument("--only", action="append", default=None, metavar="PREFIX",
                    help="Only treat the video files whose name STARTS WITH this prefix "
                         "(e.g. --only main). Repeat it for several prefixes. The others "
                         "are copied unchanged; without it, every video is treated.")
    ap.add_argument("--max-size", default="none", metavar="WxH",
                    help="Working resolution (default 'none' = the clip as it is)")
    ap.add_argument("--tolerance", type=float, default=60.0,
                    help="How far from that colour still gets keyed (default 60). Do NOT "
                         "raise it to fix a green edge — use --spill, raising it erodes "
                         "the subject")
    ap.add_argument("--bg-fit", default="cover", choices=["cover", "contain", "stretch"],
                    help="How the background fills the frame: cover = fill+crop, no "
                         "distortion (default); contain = letterbox; stretch = distort")
    ap.add_argument("--spill", type=float, default=0.6,
                    help="Green-halo removal on the subject's edges: 0.6 default, 0 = off")
    ap.add_argument("--outline", default="#FFFFFF",
                    help="Coloured ring around the subject, e.g. '#FFFFFF' (default: none)")
    ap.add_argument("--outline-width", type=float, default=4,
                    help="Thickness of that ring in pixels (default 4)")
    ap.add_argument("--crf", type=int, default=18,
                    help="H.264 quality: 18 visually lossless (default), 23 smaller")
    args = ap.parse_args()

    if not os.path.isfile(args.background):
        raise SystemExit(f"Background not found: {args.background}")
    if args.background.strip().lower() == "green":
        raise SystemExit("background_workflow replaces the green — give it an image or a video, not 'green'.")

    src_dir, project, out_dir = seglib.resolve_input(args.input, "greenscreen", "background", args.out)
    os.makedirs(out_dir, exist_ok=True)

    seg_dirs = seglib.segment_dirs(src_dir)
    if not seg_dirs:
        raise SystemExit(f"No segment folder found in {src_dir} — a segment folder holds "
                         f"<name>/<name>.mp4 (run greenscreen_workflow.py first)")

    print(f"project   : {project}")
    print(f"input     : {src_dir}")
    print(f"output    : {out_dir}")
    print(f"segments  : {len(seg_dirs)}")
    print(f"background: {args.background}   (key #{args.key}, tolerance {args.tolerance})")

    cum = 0.0
    for i, sd in enumerate(seg_dirs, 1):
        stem = os.path.basename(sd)
        media = seglib.media_files(sd)
        if not media:
            print(f"  ! {stem}: no video, skipped")
            continue
        dur = probe_duration(os.path.join(sd, media[0]))
        dest = os.path.join(out_dir, stem)
        os.makedirs(dest, exist_ok=True)
        print(f"[{i}/{len(seg_dirs)}] {stem}  ({len(media)} camera(s), bg offset "
              f"{cum:.2f}s, dur {dur:.2f}s)")
        outline_args = (["--outline", args.outline,
                         "--outline-width", str(args.outline_width)]
                        if args.outline else [])
        for j, mp4 in enumerate(media, 1):
            if not wanted(mp4, args.only):
                print(f"    [{j}/{len(media)}] {mp4}  -> copied unchanged (--only)")
                shutil.copy2(os.path.join(sd, mp4), os.path.join(dest, mp4))
                continue
            print(f"    [{j}/{len(media)}] {mp4}  -> keyed + new background")
            run([VENV_VIDEO, os.path.join(SCRIPTS, "apply_background.py"),
                 os.path.join(sd, mp4), "-o", os.path.join(dest, mp4),
                 "--background", args.background, "--model", "without",
                 "--key", args.key, "--tolerance", str(args.tolerance),
                 "--bg-fit", args.bg_fit, "--spill", str(args.spill)]
                + outline_args +
                ["--offset", f"{cum:.4f}", "--crf", str(args.crf),
                 "--max-size", str(args.max_size)])
        src_txt = seglib.sidecar_txt(sd)
        if src_txt:
            shutil.copy2(src_txt, os.path.join(dest, os.path.basename(src_txt)))
        cum += dur

    seglib.copy_order(src_dir, out_dir)
    print(f"\nDone -> {out_dir}")
    print(f"  {len(seg_dirs)} segment(s), {cum:.1f}s total (background continuous)")
    print("  each folder: one file per camera + the segment .txt")


if __name__ == "__main__":
    main()
