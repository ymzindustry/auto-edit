#!/home/ymz/.hermes/profiles/videoeditor/venv_video/bin/python
"""
final_workflow.py — effects/ -> final/final.mp4 (segments glued, music underneath)

    python3 scripts/workflows/final_workflow.py <folder> <music> [options]

WHAT IT DOES
  Glues every segment of effects/ in playback order (stream copy when the files are
  homogeneous) and lays a music bed under the voice: looped, normalised, faded in and out,
  mixed low so it never fights the speech.

OPTIONS
  <music>               music file, looped to cover the video
  -o, --output FILE     output path (default: <folder>/final/final.mp4)
  --volume N            music level relative to the voice (0.18; 0.08 = very discreet)
  --fade N              music fade-in / fade-out, seconds (2)
  --crf N               quality if a re-encode is needed (18)

EXAMPLES
    python3 scripts/workflows/final_workflow.py shoulder ../music.mp3
    python3 scripts/workflows/final_workflow.py shoulder ../music.mp3 --volume 0.1 --fade 3
"""

import os
import re
import subprocess
import sys
import tempfile

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
import math

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

TARGET_TIMESCALE = "12800"

sys.path.insert(0, SCRIPTS)
import segments as seglib


def run(cmd: list) -> None:
    print("  $ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed:\n" + r.stderr[-2000:])


def probe(path: str) -> str:
    return subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, text=True).stderr


def duration(path: str) -> float:
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", probe(path))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def time_base(path: str) -> str:
    """The video track timescale ("1/90000"). Two files with different timescales CANNOT
    be joined with -c copy: the timestamps no longer line up and the result plays, then
    freezes for the rest of the film."""
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Video:.*?(\d+)k? tbn", out)
    return m.group(1) if m else "?"


def normalise_timebase(src: str, dst: str) -> None:
    """Re-mux a segment to a fixed timescale WITHOUT re-encoding (a copy, so it is quick)."""
    run([FFMPEG, "-y", "-i", src, "-c", "copy",
         "-video_track_timescale", TARGET_TIMESCALE, "-movflags", "+faststart", dst])


def signature(path: str):
    """What has to match for a stream-copy concat to be safe."""
    out = probe(path)
    v = next((l for l in out.splitlines() if "Video:" in l), "")
    a = next((l for l in out.splitlines() if "Audio:" in l), "")

    def g(pat, s):
        m = re.search(pat, s)
        return m.group(1) if m else "?"

    return (g(r"Video:\s*(\w+)", v), g(r"(\d{2,5}x\d{2,5})", v), g(r"([\d.]+)\s*fps", v),
            g(r"(yuv\w+)", v),
            g(r"Audio:\s*(\w+)", a), g(r"(\d{4,6})\s*Hz", a), g(r"Hz,\s*([\w.]+)", a))


def concat(segments: list, out: str, crf: int) -> None:
    sigs = {signature(s) for s in segments}
    bases = {time_base(s) for s in segments}
    if len(sigs) == 1 and len(bases) == 1:
        print("  segments are homogeneous -> concat with stream copy (no re-encode)")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         dir=PROFILE) as f:
            for s in segments:
                f.write("file '" + os.path.abspath(s) + "'\n")
            listfile = f.name
        try:
            run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                 "-c", "copy", "-movflags", "+faststart", out])
        finally:
            os.remove(listfile)
    elif len(sigs) == 1:
        print(f"  segments share the codec but not the timescale {sorted(bases)}")
        print(f"  -> re-muxing each one to 1/{TARGET_TIMESCALE} (copy, no re-encode)")
        with tempfile.TemporaryDirectory(dir=PROFILE) as tmp:
            fixed, listfile = [], os.path.join(tmp, "list.txt")
            for k, s in enumerate(segments):
                d = os.path.join(tmp, f"n{k:03d}.mp4")
                normalise_timebase(s, d)
                fixed.append(d)
            with open(listfile, "w", encoding="utf-8") as fh:
                for s in fixed:
                    fh.write("file '" + s + "'\n")
            run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                 "-c", "copy", "-movflags", "+faststart", out])
    else:
        print("  segments differ -> concat with a re-encode")
        cmd = [FFMPEG, "-y"]
        for s in segments:
            cmd += ["-i", s]
        n = len(segments)
        chain = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        cmd += ["-filter_complex", f"{chain}concat=n={n}:v=1:a=1[v][a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", out]
        run(cmd)


def music_gain_db(music_path: str, target_peak: float = -3.0) -> float:
    """dB to add so the music's peak lands at `target_peak`.

    Without this, --volume would mean very different things depending on how hot
    the source file is (a quiet bed at volume 0.18 can end up inaudible).
    """
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", music_path,
                          "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", out)
    return target_peak - float(m.group(1)) if m else 0.0


def add_music(video: str, music: str, out: str, volume: float, fade: float) -> None:
    dur = duration(video)
    fade_out = max(0.0, dur - fade)
    norm = music_gain_db(music)
    vol_gain = 20.0 * math.log10(volume) if volume > 0 else -120.0
    gain = norm + vol_gain
    print(f"  music level: {norm:+.1f} dB to normalise (peak -> -3 dBFS), "
          f"{vol_gain:+.1f} dB for volume {volume:g}  =>  {gain:+.1f} dB total")
    graph = (f"[1:a]volume={gain:.2f}dB,afade=t=in:st=0:d={fade},"
             f"afade=t=out:st={fade_out:.3f}:d={fade}[m];"
             f"[0:a][m]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    run([FFMPEG, "-y", "-i", video, "-stream_loop", "-1", "-i", music,
         "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", out])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Glue the src_dir segments together and lay a music bed under them.")
    ap.add_argument("input", metavar="FOLDER",
                    help="Project folder (holding effects/), the src_dir folder itself, or ANY "
                         "folder that directly holds segment_i/*.mp4")
    ap.add_argument("music", help="Music file (mp3/wav/...), looped to cover the video")
    ap.add_argument("-o", "--output", default=None,
                    help="Output path (default: <folder>/final/final.mp4)")
    ap.add_argument("--volume", type=float, default=0.18,
                    help="Music level relative to the voice: 0.18 default, 0.08 barely "
                         "there, 0.30 clearly present")
    ap.add_argument("--fade", type=float, default=2.0,
                    help="Music fade-in/fade-out length in seconds (default 2)")
    ap.add_argument("--crf", type=int, default=18,
                    help="Quality if a re-encode is needed: 18 visually lossless (default), "
                         "23 smaller. Ignored when the segments are identical")
    args = ap.parse_args()

    src_dir, project, out_dir = seglib.resolve_input(args.input, "effects", "final")
    if not os.path.isfile(args.music):
        raise SystemExit(f"Music not found: {args.music}")

    seg_dirs = seglib.segment_dirs(src_dir)
    cams = {}
    for sd in seg_dirs:
        for f in seglib.media_files(sd):
            stem = re.sub(r"_\d+$", "", os.path.splitext(f)[0])
            cams.setdefault(stem, []).append(os.path.join(sd, f))
    if not cams:
        raise SystemExit(f"No segment folder with a video found in {src_dir}")

    os.makedirs(out_dir, exist_ok=True)
    single = len(cams) == 1

    print(f"project   : {project}")
    print(f"input     : {src_dir}")
    print(f"output    : {out_dir}")
    for cam, segs in cams.items():
        print(f"camera    : {cam:<16} {len(segs)} segment(s), "
              f"{sum(duration(s) for s in segs):.2f}s")
    print(f"music     : {os.path.basename(args.music)}   volume {args.volume:g}, "
          f"fade {args.fade:g}s")

    outputs = []
    with tempfile.TemporaryDirectory(dir=PROFILE) as tmp:
        for cam, segs in cams.items():
            out = (args.output if (args.output and single)
                   else os.path.join(out_dir, "final.mp4" if single
                                     else f"final_{cam}.mp4"))
            print(f"  gluing {cam}: {len(segs)} segment(s) -> {os.path.basename(out)}")
            merged = os.path.join(tmp, f"merged_{cam}.mp4")
            concat(segs, merged, args.crf)
            print(f"  music  {cam}: mixing {os.path.basename(args.music)} ...")
            add_music(merged, args.music, out, args.volume, args.fade)
            outputs.append(out)

    print()
    for out in outputs:
        print(f"Done -> {out}   ({duration(out):.2f}s)")


if __name__ == "__main__":
    main()
