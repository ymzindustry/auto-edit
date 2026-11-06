#!/home/ymz/.hermes/profiles/videoeditor/venv_video/bin/python
"""
make_short.py — build a vertical (9:16) short from a long horizontal video.

    python3 scripts/make_short.py INPUT.mp4 -o short.mp4 --height 1920

Each entry of PLAN is one beat: a cut of the source, a 9:16 crop anchored on the
subject, a slow sub-pixel push-in, and a caption burned in the lower third. Beats are
rendered one by one (low memory) and glued with a stream copy.

The crop window is 9:16 inside the source frame: `x` is the CENTRE of the window, as a
fraction of the frame width (0.5 = middle, 0.3 = left third), so a talking head that
sits left of the frame stays in the shot. `zoom` is the magnification reached at the end
of the beat (1.0 = none).
"""

import argparse
import os
import subprocess
import sys
import tempfile

FFMPEG_CANDIDATES = [
    "/home/ymz/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg",   # has drawtext
]


def _ffmpeg() -> str:
    """The first build that can draw text (the bundled static one cannot)."""
    for path in FFMPEG_CANDIDATES:
        if os.path.exists(path):
            return path
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = _ffmpeg()
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

PLAN = [
    # (start, duration, x centre, caption, zoom)
    (300.0, 4.5, 0.50, "Séance jambes, dos en vrac", 1.06),
    (112.0, 7.0, 0.30, "Squat Smith : 140 → 160 kg", 1.08),
    (252.0, 7.0, 0.55, "Leg press 340 kg", 1.08),
    (310.0, 6.5, 0.55, "400 kg pour l'honneur", 1.08),
]


def probe(path: str):
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    import re
    size = re.search(r"Stream #0:0.*?(\d{3,5})x(\d{3,5})", out)
    fps = re.search(r"([\d.]+) fps", out)
    return (int(size.group(1)), int(size.group(2)),
            float(fps.group(1)) if fps else 25.0)


def beat(src: str, out: str, start: float, dur: float, xc: float, caption: str,
         zoom: float, w: int, h: int, fps: float) -> None:
    W, H, _ = probe(src)
    cw = int(round(H * w / h / 2) * 2)          # width of a 9:16 window at full height
    cx = min(max(int(xc * W), cw // 2), W - cw // 2)
    x0 = cx - cw // 2

    # sub-pixel push-in: perspective warps with real interpolation (zoompan snaps to
    # whole pixels and shakes on a slow move)
    z = f"(1+{zoom - 1:.4f}*clip(in/{fps:g}/{dur:.3f},0,1))"
    xa, xb = f"W/2-W/(2*{z})", f"W/2+W/(2*{z})"
    ya, yb = f"H/2-H/(2*{z})", f"H/2+H/(2*{z})"
    # the caption goes in a FILE: inside a filter argument, commas, colons and quotes
    # would need layers of escaping (and a comma silently splits the graph)
    capfile = out + ".txt"
    import textwrap
    # a caption wider than the frame gets cut on both sides: wrap it, and keep the font
    # small enough that the longest line fits with the box padding
    lines = textwrap.wrap(caption, width=18) or [caption]
    with open(capfile, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    vf = (
        f"crop={cw}:{H}:{x0}:0,"
        f"scale={w}:{h}:flags=lanczos,"
        f"perspective=x0='{xa}':y0='{ya}':x1='{xb}':y1='{ya}'"
        f":x2='{xa}':y2='{yb}':x3='{xb}':y3='{yb}'"
        f":interpolation=cubic:eval=frame,"
        f"drawtext=fontfile={FONT}:textfile={capfile}:expansion=none:fontcolor=white"
        f":fontsize={int(h * 0.033)}:line_spacing={int(h * 0.012)}"
        f":box=1:boxcolor=black@0.55:boxborderw={int(h * 0.012)}"
        f":x=(w-text_w)/2:y=h*0.74,"
        f"format=yuv420p"
    )
    cmd = [FFMPEG, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
           "-vf", vf, "-r", f"{fps:g}",
           "-c:v", "libx264", "-preset", "medium", "-crf", "21",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
           "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    args = ap.parse_args()

    _, _, fps = probe(args.input)
    fps = min(fps, 30.0)
    print(f"source : {args.input}  ->  {args.width}x{args.height} @ {fps:g} fps")
    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(args.output)),
                                    prefix="short_") as tmp:
        parts = []
        for k, (start, dur, xc, caption, zoom) in enumerate(PLAN):
            out = os.path.join(tmp, f"b{k:02d}.mp4")
            print(f"  beat {k + 1}/{len(PLAN)}  {start:6.1f}s +{dur:.1f}s  « {caption} »")
            beat(args.input, out, start, dur, xc, caption, zoom,
                 args.width, args.height, fps)
            parts.append(out)
        listfile = os.path.join(tmp, "list.txt")
        with open(listfile, "w", encoding="utf-8") as fh:
            for p in parts:
                fh.write("file '" + p + "'\n")
        subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                        "-c", "copy", "-movflags", "+faststart", args.output], check=True)
    print(f"Done -> {args.output}")


if __name__ == "__main__":
    main()
