#!/home/ymz/.hermes/profiles/videoeditor/venv_video/bin/python
"""
greenscreen_workflow.py — fragmentation/ -> greenscreen/ (every segment on solid green)

    python3 scripts/workflows/greenscreen_workflow.py <folder> [options]

WHAT IT DOES
  Segments the subject with an AI model (u2net family by default) and composites it on
  #00FF00, one file per camera, so the background can be replaced later by a cheap
  chroma key. This is the expensive stage: run it once. A run killed halfway restarts
  where it stopped; --redo recomputes everything.

OPTIONS
  --out DIR             output folder (default: <project>/greenscreen)
  --seg-model M         'without' = fast chroma key on the clip's own backdrop, else a
                        rembg model: u2netp (default), u2net, u2net_human_seg, isnet-*,
                        birefnet-*, ...
  --only PREFIX         only the camera files whose name starts with this prefix;
                        the others are copied unchanged (e.g. --only main)
  --max-size WxH        convert the frames before segmenting (explicit conversion)
  --redo                recompute the files already done
  --tolerance N         key tolerance
  --crf N               H.264 quality (18)

EXAMPLES
    python3 scripts/workflows/greenscreen_workflow.py ../video-projects/shoulder --only main
    python3 scripts/workflows/greenscreen_workflow.py shoulder --seg-model u2net
    python3 scripts/workflows/greenscreen_workflow.py shoulder --seg-model without
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
import shutil
import subprocess

VENV_VIDEO = os.environ.get("VENV_VIDEO", os.path.join(PROFILE, "venv_video", "bin", "python"))

sys.path.insert(0, SCRIPTS)
import segments as seglib

SEG_MODELS = ["without", "u2net", "u2netp", "u2net_human_seg", "u2net_cloth_seg",
              "u2net_custom", "isnet-general-use", "isnet-anime", "silueta", "sam",
              "birefnet-general", "birefnet-general-lite", "birefnet-portrait",
              "birefnet-dis", "birefnet-hrsod", "birefnet-cod", "birefnet-massive",
              "ben_custom", "dis_custom"]


def wanted(name: str, prefixes) -> bool:
    """True when this file must be treated (no prefix given = treat everything)."""
    return (not prefixes) or any(name.startswith(p) for p in prefixes)


def run(cmd: list) -> None:
    print("  $ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def video_size(path: str) -> tuple:
    """(width, height) of a clip — read from the header, the file is not decoded."""
    import cv2
    cap = cv2.VideoCapture(path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return w, h


FFMPEG = None


def duration(path: str) -> float:
    """Length in seconds (ffmpeg's Duration line — works on a half-written file)."""
    global FFMPEG
    if FFMPEG is None:
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    import re
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def output_ok(src: str, dst: str) -> bool:
    """True when `dst` is already a FINISHED green version of `src`.

    This is what makes a run RESUMABLE: greenscreen_workflow takes hours on this box and can be
    killed by the kernel (out of memory) or by an SSH disconnect, so on the next run
    the files already produced are kept instead of being recomputed. A file left
    half-written (an OOM kill mid-encode) has the wrong duration and is redone.
    """
    if not os.path.isfile(dst) or os.path.getsize(dst) < 10_000:
        return False
    return abs(duration(dst) - duration(src)) <= 0.15


def too_big(w: int, h: int, cap) -> bool:
    """True when the clip exceeds `cap` ('WxH'), judged in the CLIP's OWN orientation
    (a portrait clip is compared against a portrait box, so 1080x2400 passes a
    1920x1080 limit while a 3840x2160 landscape one does not). Kept for the callers
    that want to look before they leap — greenscreen_workflow itself only warns."""
    mw, mh = cap
    if h > w:
        mw, mh = mh, mw
    return w > mw or h > mh


def require_wk(folder: str, name: str) -> str:
    """Return <folder>/<name> if it is a folder, else stop with a clear message."""
    path = os.path.join(folder, name)
    if not os.path.isdir(path):
        raise SystemExit(f"Missing '{name}' folder in {folder} — run the previous workflow first.")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Put every segment of <folder>/fragmentation on a green screen -> <folder>/greenscreen.")
    ap.add_argument("input", metavar="FOLDER",
                    help="Project folder (holding fragmentation/), the src_dir folder itself, or ANY "
                         "folder that directly holds segment_i/*.mp4 + *.txt")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="Output folder for the green segments (default: greenscreen/ next to the "
                         "input's project folder)")
    ap.add_argument("--seg-model", default="u2netp", choices=SEG_MODELS,
                    help="How to cut the subject out: 'without' = fast chroma key (no AI), "
                         "u2netp (default) = fast AI, u2net = better/slower AI, "
                         "u2net_human_seg = AI tuned for people")
    ap.add_argument("--only", action="append", default=None, metavar="PREFIX",
                    help="Only treat the video files whose name STARTS WITH this prefix "
                         "(e.g. --only main). Repeat it for several prefixes. The others "
                         "are copied unchanged; without it, every video is treated.")
    ap.add_argument("--max-size", default="none", metavar="WxH",
                    help="Convert the frames to fit this size before segmenting "
                         "(default 'none' = no conversion at all). A 4K frame needs "
                         "more RAM than this machine has and gets killed (SIGKILL/OOM)")
    ap.add_argument("--redo", action="store_true",
                    help="Recompute everything, even the files already done (default: "
                         "resume — a run killed by the OOM killer keeps its work)")
    ap.add_argument("--tolerance", type=float, default=60.0,
                    help="Chroma-key tolerance, only for --seg-model without: higher keys "
                         "more shades of the backdrop, too high makes the subject see-through "
                         "(default 60)")
    ap.add_argument("--crf", type=int, default=18,
                    help="H.264 quality: 18 visually lossless (default), 23 smaller, "
                         "28 small — lower is better")
    args = ap.parse_args()

    src_dir, project, out_dir = seglib.resolve_input(args.input, "fragmentation", "greenscreen", args.out)
    os.makedirs(out_dir, exist_ok=True)

    seg_dirs = seglib.segment_dirs(src_dir)
    if not seg_dirs:
        raise SystemExit(f"No segment folder found in {src_dir} — a segment folder holds "
                         f"<name>/<name>.mp4 (run fragmentation_workflow.py first)")

    print(f"project   : {project}")
    print(f"input     : {src_dir}")
    print(f"output    : {out_dir}")
    print(f"segments  : {len(seg_dirs)}   (background -> green, model {args.seg_model})")

    if str(args.max_size).strip().lower() in ("none", "0", ""):
        heavy = []
        for sd in seg_dirs:
            for mp4 in seglib.media_files(sd):
                if wanted(mp4, args.only):
                    w, h = video_size(os.path.join(sd, mp4))
                    if w * h > 2_300_000:
                        heavy.append((f"{os.path.basename(sd)}/{mp4}", w, h))
        if heavy:
            print(f"  ! WARNING: {len(heavy)} file(s) are bigger than a 1080p frame, "
                  f"e.g. {heavy[0][0]} = {heavy[0][1]}x{heavy[0][2]}")
            print(f"  !          nothing is converted and the run continues, but such a "
                  f"frame needs more RAM than this machine has (3.8 GB): the kernel may "
                  f"kill it. Remedy: re-cut with 'fragmentation_workflow.py ... --to-1080p', or add "
                  f"'--max-size 1920x1080' here to convert now.")
    else:
        print(f"  ! --max-size {args.max_size}: frames are converted before segmenting "
              f"(you asked for it on the command line)")

    greened = copied = kept = 0
    for i, sd in enumerate(seg_dirs, 1):
        stem = os.path.basename(sd)
        media = seglib.media_files(sd)
        if not media:
            print(f"  ! {stem}: no video, skipped")
            continue
        dest = os.path.join(out_dir, stem)
        os.makedirs(dest, exist_ok=True)
        for stale in os.listdir(dest):
            if stale.startswith("."):
                os.remove(os.path.join(dest, stale))
        print(f"[{i}/{len(seg_dirs)}] {stem}  ({len(media)} camera(s))")
        for j, mp4 in enumerate(media, 1):
            src_path, dst_path = os.path.join(sd, mp4), os.path.join(dest, mp4)
            if wanted(mp4, args.only):
                if not args.redo and output_ok(src_path, dst_path):
                    print(f"    [{j}/{len(media)}] {mp4}  -> already done (resume)")
                    kept += 1
                    continue
                print(f"    [{j}/{len(media)}] {mp4}  -> GREEN")
                run([VENV_VIDEO, os.path.join(SCRIPTS, "apply_background.py"),
                     src_path, "-o", dst_path,
                     "--background", "green", "--model", args.seg_model,
                     "--tolerance", str(args.tolerance), "--crf", str(args.crf),
                     "--max-size", args.max_size])
                greened += 1
            else:
                print(f"    [{j}/{len(media)}] {mp4}  -> copied unchanged (--only)")
                shutil.copy2(src_path, dst_path)
                copied += 1
        src_txt = seglib.sidecar_txt(sd)
        if src_txt:
            shutil.copy2(src_txt, os.path.join(dest, os.path.basename(src_txt)))

    seglib.copy_order(src_dir, out_dir)
    print(f"\nDone -> {out_dir}")
    print(f"  {len(seg_dirs)} segment(s): {greened} file(s) on green, "
          f"{copied} copied unchanged, {kept} already done (resume) — same files as src_dir")
    print(f"  next: background_workflow.py {project} --background <image|video>")


if __name__ == "__main__":
    main()
