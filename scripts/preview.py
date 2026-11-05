#!/home/ymz/.hermes/profiles/videoeditor/venv_video/bin/python
"""
preview.py — a LIGHT file to judge a stage output, not a deliverable.

It takes a stage folder (e.g. `shoulder/fragmentation`), picks ONE camera of each
segment, re-encodes every segment to the preview canvas, then glues them back in
`order.txt` order with the enhanced audio:

    python3 scripts/preview.py raw_data/shoulder/fragmentation
    python3 scripts/preview.py raw_data/shoulder           # the LAST stage it finds
    python3 scripts/preview.py raw_data/shoulder/fragmentation -o /tmp/pv.mp4
    python3 scripts/preview.py raw_data/shoulder/fragmentation --cam screen --height 480

Why a separate tool: the cut is judged in seconds, on a few hundred KB, while the
segments stay untouched at native resolution. Rebuilding it never re-cuts anything.

Two passes on purpose (per segment re-encode, then concat with -c copy): a single
filter_complex with one input per segment hits ffmpeg's limits on long takes and
fails when the cameras differ in size. Here every piece has exactly the same
codec/resolution/fps/pix_fmt before being copied together.
"""

import os
import sys

# Self-heal: if started with another interpreter, restart under venv_video.
HERE = os.path.dirname(os.path.abspath(__file__))          # scripts/
SCRIPTS = HERE
PROFILE = os.path.dirname(SCRIPTS)                         # profile root
_VENV_DIR = os.path.join(PROFILE, "venv_video")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
# Its OWN marker (not _WK_REEXEC): when a stage calls this script, the stage has
# already aborted its own re-exec and leaks _WK_REEXEC=1 into the environment — with
# that shared flag we would skip the re-exec and die on `import cv2` under venv_stt.
if os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR):
    try:
        import cv2  # noqa: F401
        import imageio_ffmpeg  # noqa: F401
    except ModuleNotFoundError:
        if os.environ.get("_PREVIEW_REEXEC") == "1" or not os.path.exists(_VENV_PY):
            raise SystemExit("preview.py needs venv_video (cv2 + imageio_ffmpeg)")
        os.environ["_PREVIEW_REEXEC"] = "1"
        os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import imageio_ffmpeg

sys.path.insert(0, SCRIPTS)
import segments as seglib      # noqa: E402  — stage names, order.txt, media files

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed:\n  " + " ".join(cmd) + "\n" + r.stderr[-1500:])


def probe(path: str) -> tuple:
    """(width, height, fps) of a video, read from its header (no ffprobe needed)."""
    import cv2
    cap = cv2.VideoCapture(path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
    cap.release()
    return w, h, fps


def has_audio(path: str) -> bool:
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    return "Audio:" in out


def even(n: float) -> int:
    return max(2, int(round(n)) // 2 * 2)


def pick_camera(seg_dir: str, cam: str | None) -> str:
    """The file to show for this segment: prefix `cam`, else `main`, else the first."""
    files = seglib.media_files(seg_dir)
    if not files:
        return ""
    for prefix in ([cam] if cam else ["main"]):
        hit = [f for f in files if f.startswith(prefix)]
        if hit:
            return os.path.join(seg_dir, hit[0])
    return os.path.join(seg_dir, files[0])


def find_stage_folder(path: str) -> str:
    """Accept a stage folder, a single segment folder, or a project folder."""
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        raise SystemExit(f"Not a folder: {p}")

    # a single segment was handed over -> use its container
    if os.path.basename(p.rstrip(os.sep)).startswith("segment_"):
        p = os.path.dirname(p.rstrip(os.sep))

    if seglib.segment_names(p):
        return p

    # a project folder: take the LAST stage that exists and holds segments
    for name in reversed(seglib.ALL_STAGES):
        cand = os.path.join(p, name)
        if os.path.isdir(cand) and seglib.segment_names(cand):
            return cand
    raise SystemExit(f"No segment folder in {p} (looked for {' , '.join(seglib.ALL_STAGES)})")


def probe_duration(path: str) -> float:
    """Duration in seconds, from the frame count (cv2, no ffprobe needed)."""
    import cv2
    cap = cv2.VideoCapture(path)
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    f = cap.get(cv2.CAP_PROP_FPS) or 0
    cap.release()
    return (n / f) if (n and f) else 0.0


def encode_piece(cmd: list, total: float, label: str, i: int, n: int,
                 t0: float, quiet: bool = False) -> float:
    """Run one ffmpeg encode and keep a LIVE progress line on screen.

    ffmpeg is asked for `-progress pipe:1` (machine-readable key=value lines) and its
    `out_time` is turned into a percentage, so a 4K segment that takes 20 s never
    looks like a hang. On a non-TTY (a log file, a redirect) it prints one line per
    segment instead of redrawing the same line.
    """
    tty = sys.stderr.isatty() and not quiet
    proc = subprocess.Popen(cmd + ["-nostats", "-progress", "pipe:1"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    last = -1
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("out_time="):
            continue
        stamp = line.split("=", 1)[1].split(".")[0]
        try:
            hh, mm, ss = (stamp.split(":") + ["0", "0", "0"])[:3]
            secs = int(hh) * 3600 + int(mm) * 60 + int(ss)
        except ValueError:                      # "out_time=N/A" at the very start
            continue
        # no known duration: fall back to elapsed time so the line still moves
        pct = min(99, int(100 * secs / total)) if total else \
            min(99, int((time.time() - t0) * 4))
        if tty and pct != last:
            last = pct
            sys.stderr.write(f"\r  [{i}/{n}] {label:<28.28s} {pct:3d}%  "
                             f"{time.time() - t0:5.1f}s   ")
            sys.stderr.flush()
    proc.wait()
    err = (proc.stderr.read() or "").strip()
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg failed on {label}:\n{err[-1200:]}")
    if tty:
        sys.stderr.write(f"\r  [{i}/{n}] {label:<28.28s} done  {time.time() - t0:5.1f}s"
                         f"{' ' * 12}\n")
        sys.stderr.flush()
    elif not quiet:
        print(f"  [{i}/{n}] {label}  done in {time.time() - t0:.1f}s", flush=True)
    return time.time() - t0


def build(stage_dir: str, out: str, height: int, fps_cap: float, crf: int,
          preset: str, cam: str | None, abitrate: str = "96k",
          audio_copy: bool = False, jobs: int = 1) -> str:
    names = seglib.segment_names(stage_dir)
    picks = []
    for name in names:
        seg = os.path.join(stage_dir, name)
        path = pick_camera(seg, cam)
        if path:
            picks.append(path)
        else:
            print(f"  ! {name}: no video, skipped")
    if not picks:
        raise SystemExit(f"Nothing to preview in {stage_dir}")

    dims = [probe(p) for p in picks]                     # (w, h, fps) per piece
    durations = [probe_duration(p) for p in picks]
    # never UPSCALE the frame rate of the slowest camera: duplicating frames is what
    # makes a preview look like it stutters
    fps = min(fps_cap, max(1.0, round(min(d[2] if d[2] > 0 else 25.0 for d in dims))))
    W, H = even(height * 16 / 9), even(height)
    grand = sum(durations)
    print(f"  {len(picks)} segment(s) -> {W}x{H} @ {fps:g} fps, crf {crf}, "
          f"{grand / 60:.1f} min of video to encode", flush=True)

    tmp = tempfile.mkdtemp(prefix="preview_", dir=PROFILE)
    t_start = time.time()
    try:
        pieces = [os.path.join(tmp, f"{i:04d}.mp4") for i in range(len(picks))]

        def one(i: int) -> float:
            """Encode piece `i` (0-based) to the preview canvas."""
            path, piece = picks[i], pieces[i]
            chain = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                     f"setsar=1,fps={fps:g},format=yuv420p")
            # The segments already carry the enhanced audio (mono 48 kHz): it is either
            # copied as is (--audio-copy, bit-exact) or re-encoded at a bitrate that is
            # plenty for speech (96k mono by default, 160k for cleaner).
            if not has_audio(path):
                audio = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-shortest",
                         "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "48000"]
            elif audio_copy:
                audio = ["-map", "0:a:0", "-c:a", "copy"]
            else:
                audio = ["-map", "0:a:0", "-c:a", "aac", "-b:a", abitrate,
                         "-ac", "1", "-ar", "48000"]
            label = os.path.join(os.path.basename(os.path.dirname(path)),
                                 os.path.basename(path))
            t0 = time.time()
            return encode_piece([FFMPEG, "-y", "-v", "error", "-i", path,
                                 "-vf", chain, "-map", "0:v:0", *audio,
                                 "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                                 "-pix_fmt", "yuv420p",
                                 "-video_track_timescale", "90000", piece],
                                durations[i], label, i + 1, len(picks), t0,
                                quiet=jobs > 1)

        if jobs <= 1:
            for i in range(len(picks)):
                one(i)
        else:
            # the 4K DECODE is the cost, not the encode: several segments at once is
            # the only real speed-up on a CPU-only box (measured: preset and height
            # barely move the wall clock)
            done, spent = 0, 0.0
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futs = {pool.submit(one, i): i for i in range(len(picks))}
                for fut in as_completed(futs):
                    i = futs[fut]
                    secs = fut.result()
                    done += 1
                    spent = time.time() - t_start
                    eta = spent / done * (len(picks) - done)
                    print(f"  [{done}/{len(picks)}] "
                          f"{os.path.basename(os.path.dirname(picks[i]))}/"
                          f"{os.path.basename(picks[i])}  {secs:.1f}s   "
                          f"elapsed {spent:.0f}s  eta {eta:.0f}s", flush=True)

        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for piece in pieces:
                fh.write("file '" + piece + "'\n")
        print(f"  gluing {len(pieces)} piece(s) ...", flush=True)
        run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", "-movflags", "+faststart", out])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    size = os.path.getsize(out) / 1e6 if os.path.isfile(out) else 0.0
    print(f"  done in {time.time() - t_start:.0f}s  ->  {out}")
    print(f"  {grand / 60:.1f} min, {W}x{H} @ {fps:g} fps, {size:.1f} MB")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="preview.py",
        description="Glue a stage's segments back together into ONE light preview, "
                    "to judge the cut. The segments themselves are never modified.")
    ap.add_argument("input", metavar="FOLDER",
                    help="a stage folder (e.g. shoulder/fragmentation), a project folder "
                         "(last stage found), or a single segment folder")
    ap.add_argument("-o", "--output", default=None, metavar="FILE",
                    help="output file (default: <stage>/preview_<H>p.mp4)")
    ap.add_argument("--height", type=int, default=720,
                    help="height of the preview in pixels (default 720, 480 = lighter)")
    ap.add_argument("--fps", type=float, default=30.0,
                    help="frame rate cap (default 30; a slower camera is never upsampled)")
    ap.add_argument("--crf", type=int, default=28,
                    help="H.264 quality (default 28 = small; 23 = cleaner)")
    ap.add_argument("--preset", default="veryfast",
                    help="x264 preset (default veryfast)")
    ap.add_argument("--cam", default=None, metavar="PREFIX",
                    help="camera to show, by file-name prefix (default: main, else first)")
    ap.add_argument("--abitrate", default="96k", metavar="RATE",
                    help="audio bitrate of the preview, mono 48 kHz (default 96k: "
                         "light; 160k cleaner, --audio-copy bit-exact)")
    ap.add_argument("--audio-copy", action="store_true",
                    help="copy the voice track instead of re-encoding it (bit-exact, "
                         "bigger file)")
    ap.add_argument("--jobs", "-j", type=int, default=1, metavar="N",
                    help="segments encoded in parallel (default 1). Measured on this "
                         "8-core box: one ffmpeg already uses ~750%% of the CPU, so "
                         "N>1 brings nothing and costs a little — the 4K HEVC DECODE is "
                         "the wall, not the encode")
    args = ap.parse_args()

    stage_dir = find_stage_folder(args.input)
    out = args.output or os.path.join(stage_dir, f"preview_{args.height}p.mp4")
    print(f"stage     : {stage_dir}")
    print(f"output    : {out}")
    build(stage_dir, out, args.height, args.fps, args.crf, args.preset, args.cam,
          args.abitrate, args.audio_copy, max(1, args.jobs))


if __name__ == "__main__":
    main()
