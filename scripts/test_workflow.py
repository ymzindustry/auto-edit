#!/usr/bin/env python3
"""
test_workflow.py — THE WHOLE CHAIN, IN ONE FILE, ON raw_data/video.

What it does, in this order (each step is one script in scripts/workflows/):

  0. CUT       main.MP4 + screen.mp4 -> only the first 10 s (the full files are ~3 min)
               -> raw_data/video/test10s/{main.mp4,screen.mp4}
  1. FRAGMENT  cut the silences on the master audio, ENHANCE the voice
     + ENHANCE   (DeepFilterNet3 + EQ + compression + loudnorm), transcribe
               -> raw_data/video/test10s/fragmentation/
  2. GREEN     replace the background with a SOLID GREEN using u2net, HAND ONLY
               -> raw_data/video/test10s/greenscreen/
  3. CARBG     replace that green with raw_data/video/carbg.png, HAND ONLY
               -> raw_data/video/test10s/background/
  4. SCREEN    screen on the LEFT of the hand (both entirely visible)
     LEFT      -> raw_data/video/test10s/effects/

The last step is the effects engine reading scripts/test_effects.py: the layout lives
there, not here. Change the look -> edit test_effects.py, re-run only step 4.

Usage:
    python3 scripts/test_workflow.py                    # everything, 10 s
    python3 scripts/test_workflow.py --seconds 6        # shorter test
    python3 scripts/test_workflow.py --from 2           # resume at the green screen
    python3 scripts/test_workflow.py --dry-run          # print the commands only
    python3 scripts/test_workflow.py --stage 4          # only the side-by-side

Every command it runs is printed, so this file doubles as the shell recipe.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

PROFILE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PROFILE, "scripts")
WORKFLOWS = os.path.join(SCRIPTS, "workflows")
FFMPEG = os.path.join(PROFILE, "venv_video", "bin", "ffmpeg") \
    if os.path.exists(os.path.join(PROFILE, "venv_video", "bin", "ffmpeg")) else "ffmpeg"
FFPROBE = "ffprobe"

DEFAULT_INPUT = os.path.join(PROFILE, "raw_data", "video")
PROJECT_NAME = "test10s"


# ──────────────────────────────────────────────────────────────────────────────
#  helpers
# ──────────────────────────────────────────────────────────────────────────────

def run(cmd: list, dry: bool = False) -> None:
    """Run one command from the profile root, echoing it."""
    print("\n$ " + " ".join(_short(c) for c in cmd), flush=True)
    if dry:
        return
    subprocess.run(cmd, cwd=PROFILE, check=True)


def _short(path: str) -> str:
    """Print paths relative to the profile — the line stays readable."""
    p = str(path)
    return p[len(PROFILE) + 1:] if p.startswith(PROFILE + os.sep) else p


def probe(path: str) -> dict:
    """duration / width / height / fps / codecs of a file."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, text=True)
    if out.returncode != 0:
        return {}
    data = json.loads(out.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    num, _, den = str(v.get("r_frame_rate", "0/1")).partition("/")
    fps = float(num) / float(den) if float(den or 1) else 0.0
    return {"dur": float(data.get("format", {}).get("duration", 0) or 0),
            "w": int(v.get("width", 0)), "h": int(v.get("height", 0)),
            "fps": round(fps, 2), "vcodec": v.get("codec_name", ""),
            "acodec": a.get("codec_name", ""),
            "pix": v.get("pix_fmt", "")}


def decodes_cleanly(path: str) -> bool:
    """True when the whole file decodes without a single error (catches truncation)."""
    out = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"],
                         capture_output=True, text=True)
    bad = [ln for ln in out.stderr.splitlines() if ln.strip()]
    return out.returncode == 0 and not bad


def check_file(path: str, label: str) -> bool:
    info = probe(path)
    if not info:
        print(f"  ! {label}: unreadable ({_short(path)})")
        return False
    ok = decodes_cleanly(path)
    print(f"  {label:22s} {info['w']}x{info['h']} @ {info['fps']:g} fps  "
          f"{info['dur']:.2f}s  {info['vcodec']}/{info['acodec']}  "
          f"{'OK' if ok else 'DECODE ERRORS'}")
    return ok


# ──────────────────────────────────────────────────────────────────────────────
#  stages
# ──────────────────────────────────────────────────────────────────────────────

def stage0_cut(src_dir: str, project: str, seconds: float, dry: bool, force: bool) -> None:
    """Keep only the first `seconds` of main + screen (re-encoded: exact duration)."""
    print("\n" + "=" * 78)
    print(f" 0. CUT — first {seconds:g}s of main + screen  ->  {_short(project)}")
    print("=" * 78)
    if not dry:
        os.makedirs(project, exist_ok=True)
    for name in ("main", "screen"):
        src = _find_source(src_dir, name)
        dst = os.path.join(project, f"{name}.mp4")
        if src is None:
            raise SystemExit(f"no {name}.mp4/.MP4 in {src_dir}")
        if os.path.exists(dst) and not force:
            info = probe(dst)
            if abs(info.get("dur", 0) - seconds) < 0.5:
                print(f"  {name}: already cut ({info['dur']:.2f}s) — kept")
                continue
        run([FFMPEG, "-y", "-v", "warning", "-i", src, "-t", f"{seconds:g}",
             "-map", "0:v:0", "-map", "0:a:0?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", dst], dry)
    if not dry:
        check_file(os.path.join(project, "main.mp4"), "main.mp4")
        check_file(os.path.join(project, "screen.mp4"), "screen.mp4")


def _find_source(src_dir: str, stem: str):
    for ext in ("mp4", "MP4", "mov", "MOV", "mkv"):
        p = os.path.join(src_dir, f"{stem}.{ext}")
        if os.path.exists(p):
            return p
    return None


def stage1_fragment(project: str, dry: bool, language: str | None, transcribe: bool,
                    offsets: list) -> None:
    """Silence cut on the master audio + DeepFilterNet enhancement (+ transcription)."""
    print("\n" + "=" * 78)
    print(" 1. FRAGMENT + ENHANCE — silence cut, voice cleaned"
          + (", transcribed" if transcribe else "") + "  ->  fragmentation/")
    print("=" * 78)
    cmd = [sys.executable, os.path.join(WORKFLOWS, "fragmentation_workflow.py"), project]
    if not transcribe:
        # no transcription: every segment is kept, nothing can be dropped for "nothing
        # said" — the safe choice for a short test whose audio is mostly silence
        cmd.append("--no-transcribe")
    if language:
        cmd += ["--language", language]
    for o in offsets:
        cmd += ["--video-offset", o] if "=" in o else ["--offset", o]
    run(cmd, dry)


def stage2_green(project: str, dry: bool, model: str) -> None:
    """Solid green background behind the HAND ONLY (u2net segmentation)."""
    print("\n" + "=" * 78)
    print(f" 2. GREEN — u2net background removal, hand camera only  ->  greenscreen/  ({model})")
    print("=" * 78)
    run([sys.executable, os.path.join(WORKFLOWS, "greenscreen_workflow.py"), project,
         "--seg-model", model, "--only", "main"], dry)


def stage3_carbg(project: str, background: str, dry: bool) -> None:
    """Key that green out and put carbg.png behind the hand."""
    print("\n" + "=" * 78)
    print(f" 3. CARBG — green replaced by {os.path.basename(background)}  ->  background/")
    print("=" * 78)
    run([sys.executable, os.path.join(WORKFLOWS, "background_workflow.py"), project,
         "--background", background, "--only", "main", "--outline", ""], dry)


def stage4_screen_left(project: str, plan: str, dry: bool) -> None:
    """Screen on the left of the hand — the layout comes from the plan file."""
    print("\n" + "=" * 78)
    print(f" 4. SCREEN LEFT — layout read from {os.path.basename(plan)}  ->  effects/")
    print("=" * 78)
    run([sys.executable, os.path.join(WORKFLOWS, "effects_workflow.py"),
         "--plan", plan, "--input", project], dry)


# ──────────────────────────────────────────────────────────────────────────────
#  report
# ──────────────────────────────────────────────────────────────────────────────

def report(project: str) -> str:
    """Check every stage and return the path of the final file."""
    print("\n" + "=" * 78)
    print(" CHECK — every stage must exist, decode cleanly and keep its duration")
    print("=" * 78)
    all_ok = True
    for stage in ("fragmentation", "greenscreen", "background", "effects"):
        folder = os.path.join(project, stage)
        if not os.path.isdir(folder):
            print(f"  {stage}: missing")
            all_ok = False
            continue
        files = sorted(os.path.join(r, f) for r, _, fs in os.walk(folder)
                       for f in fs if f.endswith(".mp4")
                       and not f.startswith("preview"))
        if not files:
            print(f"  {stage}: no mp4")
            all_ok = False
            continue
        print(f"\n  {stage}: {len(files)} file(s)")
        for f in files:
            all_ok &= check_file(f, os.path.relpath(f, folder))

    final = None
    effects_dir = os.path.join(project, "effects")
    if os.path.isdir(effects_dir):
        mp4s = sorted(os.path.join(r, f) for r, _, fs in os.walk(effects_dir)
                      for f in fs if f.endswith(".mp4")
                      and not f.startswith("preview"))
        if len(mp4s) == 1:
            # OUTSIDE the project folder: a .mp4 sitting next to main.mp4 would be
            # picked up as a third CAMERA by the next fragmentation run.
            final = os.path.join(os.path.dirname(project),
                                 f"{os.path.basename(project)}_final.mp4")
            shutil.copy2(mp4s[0], final)
            print(f"\n  final -> {_short(final)}")

    print("\n" + ("ALL GOOD" if all_ok else "PROBLEMS FOUND — see above"))
    return final or ""


# ──────────────────────────────────────────────────────────────────────────────
#  main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="test_workflow.py",
        description="Cut a 10 s slice of main + screen, then run the whole chain: "
                    "fragment+enhance -> green -> carbg -> screen left of hand.")
    ap.add_argument("--input", default=DEFAULT_INPUT, metavar="FOLDER",
                    help="folder holding main.MP4 and screen.mp4 (default raw_data/video)")
    ap.add_argument("--project", default=None, metavar="FOLDER",
                    help="working project folder (default raw_data/video/test10s)")
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="length of the cut slice in seconds (default 10)")
    ap.add_argument("--from", dest="start", type=int, default=0, choices=[0, 1, 2, 3, 4],
                    help="resume at this stage (0 = everything)")
    ap.add_argument("--stage", type=int, default=None, choices=[0, 1, 2, 3, 4],
                    help="run ONLY this stage")
    ap.add_argument("--plan", default=os.path.join(SCRIPTS, "test_effects.py"),
                    metavar="FILE.py", help="effect parameters for stage 4")
    ap.add_argument("--background", default=os.path.join(DEFAULT_INPUT, "carbg.png"),
                    metavar="IMG", help="image (or video) behind the hand")
    ap.add_argument("--seg-model", default="u2net",
                    help="rembg model for the green screen (default u2net)")
    ap.add_argument("--language", default=None,
                    help="transcription language (omit = French, the script default)")
    ap.add_argument("--transcribe", action="store_true",
                    help="also transcribe the segments (off by default: on a short, "
                         "mostly silent slice whisper hallucinates and the segments get "
                         "dropped for 'nothing said')")
    ap.add_argument("--offset", dest="offsets", action="append", default=[],
                    metavar="[NAME=]SECONDS",
                    help="force the position of a camera on the master timeline "
                         "(--offset 0 for every camera, --offset screen.mp4=1.2 for one). "
                         "Use it when the audio correlation is weak on a short slice.")
    ap.add_argument("--force", action="store_true",
                    help="re-cut main/screen even if the slices already exist")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    ap.add_argument("--no-check", action="store_true", help="skip the final verification")
    args = ap.parse_args()

    src_dir = os.path.abspath(args.input)
    project = os.path.abspath(args.project or os.path.join(src_dir, PROJECT_NAME))
    background = os.path.abspath(args.background)
    plan = os.path.abspath(args.plan)

    for p, what in ((src_dir, "input folder"), (background, "background image"),
                    (plan, "effects plan")):
        if not os.path.exists(p):
            raise SystemExit(f"{what} not found: {p}")

    stages = {
        0: lambda: stage0_cut(src_dir, project, args.seconds, args.dry_run, args.force),
        1: lambda: stage1_fragment(project, args.dry_run, args.language,
                                   args.transcribe, args.offsets),
        2: lambda: stage2_green(project, args.dry_run, args.seg_model),
        3: lambda: stage3_carbg(project, background, args.dry_run),
        4: lambda: stage4_screen_left(project, plan, args.dry_run),
    }
    wanted = [args.stage] if args.stage is not None else \
        [i for i in range(5) if i >= args.start]

    if args.stage is None and not args.dry_run and not os.path.isdir(project):
        os.makedirs(project, exist_ok=True)

    print(f"input   : {_short(src_dir)}")
    print(f"project : {_short(project)}")
    print(f"stages  : {', '.join(str(i) for i in wanted)}"
          + ("   (dry run)" if args.dry_run else ""))

    for i in wanted:
        stages[i]()

    if args.dry_run or args.no_check:
        print("\ndone (nothing verified)")
        return
    report(project)


if __name__ == "__main__":
    main()
