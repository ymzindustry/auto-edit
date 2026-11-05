#!/home/ymz/.hermes/profiles/videoeditor/venv_video/bin/python
"""
insert_media.py — Pop an image or a video into a clip.

The media appears centred on top, taking --pct % of the frame width (aspect
ratio preserved). While it is on screen the main video is softly blurred, and
both the media and the blur fade in / fade out.

    main video:  ....[ blur ramps up ]==========[ blur ramps down ]....
    media     :        [ fade in ]  visible  [ fade out ]

--fade-in / --fade-out default to --fade; pass 0 to disable one of them (used by
effects_workflow when a media spans several segments: the middle segments have no fade
at all, the first one only fades in, the last one only fades out).

Input folder: one .mp4 (plus its .txt, as the workflows produce).
Output:       given by -o, else <folder>/<name>_insert.mp4

Usage:
    ./insert_media.py /f/background/segment_000 picture.png
    ./insert_media.py /f/background/segment_000 demo.mp4 --pct 60 --fade 1.5
    ./insert_media.py /f/background/segment_000 diagram.png --start 2 --end 3 --fade-in 0
"""

import os
import sys

# Self-heal: if started with another interpreter (the very common
# `python3 insert_media.py`), restart under our own venv so imports resolve.
_VENV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "venv_video")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (os.environ.get("_WK_REEXEC") != "1" and os.path.exists(_VENV_PY)
        and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR)):
    os.environ["_WK_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import glob
import re
import subprocess

import cv2
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")


def run(cmd: list) -> None:
    print("  $ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed:\n" + r.stderr[-2000:])


def probe_video(path: str):
    """-> (width, height, duration_seconds)"""
    out = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True).stderr
    m = re.search(r", (\d{2,5})x(\d{2,5})[,\s]", out)
    d = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    dur = (int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))) if d else 0.0
    return w, h, dur


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXT


def find_clip(folder: str, wanted: str | None = None) -> str:
    """The segment's own .mp4 (ignoring an 'illu.*' insert or an old _insert file).

    With `wanted` (a file name), that exact camera is picked inside the folder —
    the layout is one folder per segment with ONE FILE PER CAMERA, so insert_media
    has to be told which one to treat.
    """
    if wanted:
        path = os.path.join(folder, wanted)
        if not os.path.isfile(path):
            raise SystemExit(f"No '{wanted}' in {folder}")
        return path
    vids = sorted(v for v in glob.glob(os.path.join(folder, "*.mp4"))
                  if not os.path.basename(v).endswith("_insert.mp4")
                  and os.path.splitext(os.path.basename(v))[0].lower() != "illu")
    base = os.path.basename(os.path.normpath(folder))
    for v in vids:                                   # segment_000/segment_000.mp4
        if os.path.splitext(os.path.basename(v))[0] == base:
            return v
    if not vids:
        raise SystemExit(f"No .mp4 found in {folder}")
    if len(vids) > 1:
        raise SystemExit("Several .mp4 found, keep only one:\n  " +
                         "\n  ".join(os.path.basename(v) for v in vids))
    return vids[0]


def media_has_audio(path: str, still: bool) -> bool:
    """Does the inserted media carry an audio stream? (an image never does)"""
    if still:
        return False
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    return "Audio:" in out


def atempo_chain(factor: float) -> str:
    """atempo only accepts 0.5..2.0 per stage: chain stages for a stronger speed-up."""
    parts = []
    f = float(factor)
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.6f}")
    return ",".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Insert an image/video into a clip, blurring the clip behind it.")
    ap.add_argument("folder", help="Folder holding the .mp4 (and its .txt)")
    ap.add_argument("media", help="Image or video to insert")
    # --file : which camera of the segment to work on, when the folder holds several
    #          videos (one per camera: main_3.mp4, screenvideo_3.mp4, ...). Default:
    #          the single mp4 of the folder, or the one named like the folder.
    ap.add_argument("--file", default=None,
                    help="Which video inside the folder to treat (default: the only "
                         "one, or the one named like the folder)")
    # --pct     : how wide the inserted media is, as a % of the frame width
    #             (50 = half the picture). Aspect ratio is always kept.
    # --fade    : default fade length in seconds, used by BOTH fades.
    # --fade-in / --fade-out: override --fade; 0 = that fade does not happen at all
    #             (the media then pops in/out instantly).
    # --start   : how many seconds into the clip the media starts appearing
    #             (0 = from the first frame).
    # --end     : the fade-out must be FINISHED this many seconds before the clip
    #             ends (0 = it finishes exactly at the end). Both values are clamped
    #             into the clip and the fades are squeezed if the clip is too short.
    # --blur    : how much the main video is blurred behind the media — gblur sigma,
    #             0 = no blur, 20 = soft (default), 40 = heavy.
    # --crf     : H.264 quality of the output (18 = visually lossless).
    # -o/--output: where the result goes (default: "<name>_insert.mp4" inside the
    #             clip's own folder, so the original clip is left untouched).
    ap.add_argument("--pct", type=float, default=50,
                    help="Media width as a percentage of the frame width (default 50)")
    ap.add_argument("--fade", type=float, default=1.0,
                    help="Default fade in / fade out duration in seconds (default 1.0)")
    ap.add_argument("--fade-in", type=float, default=None,
                    help="Fade-in duration, overrides --fade; 0 = no fade-in")
    ap.add_argument("--fade-out", type=float, default=None,
                    help="Fade-out duration, overrides --fade; 0 = no fade-out")
    ap.add_argument("--start", type=float, default=0.0,
                    help="Seconds after which the media starts appearing (default 0)")
    ap.add_argument("--end", type=float, default=0.0,
                    help="The fade-out must be FINISHED this many seconds before the "
                         "clip ends (default 0)")
    ap.add_argument("--blur", type=float, default=20,
                    help="Blur strength on the main video, sigma (default 20)")
    ap.add_argument("--crf", type=int, default=18, help="x264 quality (default 18)")
    # --fit : what to do when the media is LONGER than the slot it must fill.
    #         "cut"   = play it and cut at the slot end (default, unchanged behaviour)
    #         "speed" = speed it up (video + its own sound) so the WHOLE media fits
    #                   exactly in the slot
    ap.add_argument("--fit", default="cut", choices=["cut", "speed"],
                    help="media longer than the slot: 'cut' (default) plays it and cuts, "
                         "'speed' accelerates it so all of it fits")
    # --volume : level of the INSERTED media's own sound, 0..1 (default 0.3). It is
    #            mixed UNDER the clip's voice so the two never fight; 0 = the media is
    #            silent (and the base audio is kept untouched, as before), 1 = as loud
    #            as the original recording. An image, or a media with no audio track,
    #            ignores it.
    ap.add_argument("--volume", type=float, default=0.3,
                    help="Volume of the inserted media's audio, 0..1 (default 0.3, "
                         "0 = silent)")
    ap.add_argument("-o", "--output", default=None, help="Output path")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        raise SystemExit(f"Not a folder: {folder}")
    if not os.path.isfile(args.media):
        raise SystemExit(f"Media not found: {args.media}")

    clip = find_clip(folder, args.file)
    stem = os.path.splitext(os.path.basename(clip))[0]
    out = args.output or os.path.join(folder, stem + "_insert.mp4")

    W, H, mdur = probe_video(clip)
    if not W or not mdur:
        raise SystemExit(f"Cannot read {clip}")
    still = is_image(args.media)
    media_natural = 0.0                       # unknown for a still image
    if still:
        img = cv2.imread(args.media, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise SystemExit(f"Cannot read image: {args.media}")
        mw, mh = img.shape[1], img.shape[0]
    else:
        mw, mh, media_natural = probe_video(args.media)
        if not mw:
            raise SystemExit(f"Cannot read media: {args.media}")

    # --- geometry: --pct % of the frame width, aspect ratio kept, never overflowing
    tw = W * args.pct / 100.0
    th = tw * mh / mw
    max_h = H * 0.92
    if th > max_h:
        th = max_h
        tw = th * mw / mh
    tw = max(2, int(round(tw / 2)) * 2)              # even width for yuv420p

    # --- fades: --fade-in / --fade-out override --fade; 0 disables that fade
    fi = max(0.0, args.fade if args.fade_in is None else args.fade_in)
    fo = max(0.0, args.fade if args.fade_out is None else args.fade_out)

    # --- timing: --start = when it appears, --end = the fade-out is FINISHED this
    #     many seconds before the clip ends. Anything that cannot fit inside the
    #     clip is CLAMPED (never fatal), so short segments still render:
    #       * --start past the clip      -> forced back to 0
    #       * --end with no room left    -> forced to what the clip allows
    #       * fades longer than the slot -> squeezed into the slot
    start, end = max(0.0, args.start), max(0.0, args.end)
    if start >= mdur:
        print(f"  ! clip is only {mdur:.2f}s -> --start {start:.2f}s forced to 0")
        start = 0.0
    if end > mdur - start:
        print(f"  ! --end {end:.2f}s leaves no room in a {mdur:.2f}s clip -> "
              f"forced to {max(0.0, mdur - start):.2f}s")
        end = max(0.0, mdur - start)
    out_end = mdur - end                             # fade-out complete here
    media_dur = out_end - start                      # total time on screen
    if media_dur <= 0.05:
        print(f"  ! nothing left between --start and --end -> using the whole clip")
        start, end, out_end, media_dur = 0.0, 0.0, mdur, mdur

    # both fades must happen -> squeeze them into the window (fade-in first)
    if fi + fo > media_dur:
        o_fi, o_fo = fi, fo
        fi = min(fi, media_dur)
        fo = min(fo, max(0.0, media_dur - fi))
        print(f"  ! fades {o_fi:g}s + {o_fo:g}s don't fit in {media_dur:.2f}s -> "
              f"fade-in {fi:.2f}s, fade-out {fo:.2f}s")

    # ── FIT: the media is longer than the slot it must fill ─────────────────────
    # "cut" plays it and stops at the slot end; "speed" compresses the WHOLE media into
    # the slot (setpts on the picture, atempo on its sound) so nothing is lost.
    speed = 1.0
    fit_speed = (not still and args.fit == "speed"
                 and media_natural > media_dur + 0.05)
    if fit_speed:
        speed = media_natural / media_dur
        media_in = ["-t", f"{media_natural:.3f}", "-i", args.media]
    elif still:
        media_in = ["-loop", "1", "-t", f"{media_dur:.3f}", "-i", args.media]
    else:
        media_in = ["-stream_loop", "-1", "-t", f"{media_dur:.3f}", "-i", args.media]

    print(f"clip     : {os.path.basename(clip)}  {W}x{H}  {mdur:.2f}s")
    print(f"media    : {os.path.basename(args.media)}  {'image' if still else 'video'}  "
          f"-> {tw}x{int(round(th / 2) * 2)}  ({args.pct:g}% width)")
    if fit_speed:
        print(f"fit      : media is {media_natural:.2f}s for a {media_dur:.2f}s slot "
              f"-> sped up x{speed:.2f} (whole media kept)")
    print(f"timing   : visible {start:.2f}s -> {out_end:.2f}s"
          f"   fade-in {fi:g}s, fade-out {fo:g}s (ends {end:g}s before the clip "
          f"end), blur {args.blur:g}")

    # Blurred copy of the main video, alpha faded, limited to the media's window.
    # A gblur directly on a 4K frame needs >2 GB and this box has ~2 GB free — the
    # kernel OOM-killed the encode (three times, leaving a truncated .mp4). So the
    # frame is shrunk 4x, blurred there and scaled back to the exact size: the same
    # soft look for ~16x less memory (and much faster).
    bw, bh = max(2, (W // 4) // 2 * 2), max(2, (H // 4) // 2 * 2)
    blur = (f"[tb]scale={bw}:{bh},gblur=sigma={args.blur / 4:g},"
            f"scale={W}:{H},format=rgba")
    if fi > 0:
        blur += f",fade=t=in:st={start:.3f}:d={fi:.3f}:alpha=1"
    if fo > 0:
        blur += f",fade=t=out:st={out_end - fo:.3f}:d={fo:.3f}:alpha=1"
    blur += "[bl]"

    # media: fades on its own 0-based timeline, then shifted to --start
    med = f"[1:v]scale={tw}:-2,format=rgba"
    if fit_speed:
        # speed it up FIRST: the fades below must be expressed on the compressed
        # timeline (0 -> media_dur), otherwise they would land past the end
        med += f",setpts=PTS/{speed:.6f}"
    if fi > 0:
        med += f",fade=t=in:st=0:d={fi:.3f}:alpha=1"
    if fo > 0:
        med += f",fade=t=out:st={media_dur - fo:.3f}:d={fo:.3f}:alpha=1"
    med += f",setpts=PTS+{start:.3f}/TB[med]"

    win = f"enable='between(t,{start:.3f},{out_end:.3f})'"
    fc = ("[0:v]split[base][tb];" + blur + ";"
          f"[base][bl]overlay={win}[bg];" + med + ";"
          f"[bg][med]overlay=x=(W-w)/2:y=(H-h)/2:{win}:eof_action=pass[out]")

    # ── AUDIO ────────────────────────────────────────────────────────────────────
    # The media can carry its own sound: --volume sets its level so it never fights the
    # clip's voice, it is delayed to the media's appearance, it fades WITH the picture,
    # and it is mixed UNDER the base track (amix normalize=0 keeps both levels exactly
    # where they were asked for). With --volume 0, no audio in the media, or an image,
    # the base audio is simply kept (stream copy, as before).
    mixing = args.volume > 0 and media_has_audio(args.media, still)
    acodec = ["-c:a", "copy"]
    if mixing:
        chain = (f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                 f"channel_layouts=stereo,volume={args.volume:g}")
        if fit_speed:
            chain += "," + atempo_chain(speed)
        ms = int(round(start * 1000))
        if ms:
            chain += f",adelay={ms}|{ms}"
        if fi > 0:
            chain += f",afade=t=in:st={start:.3f}:d={fi:.3f}"
        if fo > 0:
            chain += f",afade=t=out:st={out_end - fo:.3f}:d={fo:.3f}"
        fc += ";" + chain + "[ma];[0:a][ma]amix=inputs=2:duration=first:normalize=0[aout]"
        acodec = ["-c:a", "aac", "-b:a", "192k"]
        print(f"audio    : media at volume {args.volume:g}, mixed under the clip "
              f"(delayed {start:.2f}s, fades with the picture)")
    else:
        print(f"audio    : clip only (volume {args.volume:g}"
              + (", image" if still else ", media without audio") + ")")

    run([FFMPEG, "-y", "-i", clip, *media_in,
         "-filter_complex", fc, "-map", "[out]",
         *(["-map", "[aout]"] if mixing else ["-map", "0:a?"]),
         "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
         "-pix_fmt", "yuv420p", *acodec, "-movflags", "+faststart", out])

    print(f"\nDone -> {out}")


if __name__ == "__main__":
    main()
