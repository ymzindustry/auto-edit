"""
corner_reveal — keep a window of the camera, slide it to a corner, and let a second
media take the whole frame behind it.

    t = 0                     during the move                end

    ┌──────────────┐          ┌──────────────┐        ┌──────────────┐
    │              │          │  m1 (fills)  │        │  m1 (fills)  │
    │   [ m0 ]     │   ───▶   │      [ m0 ]  │  ───▶  │              │
    │   window     │          │              │        │ [ m0 ]       │
    └──────────────┘          └──────────────┘        └──────────────┘

m0 is the SEGMENT's own camera: only a window of `w` % of the frame width and `l` % of
its height is kept, centred on (x, y). That window starts where it is in the frame and
slides to `corner`, `g` % of the frame width away from the edges, while m1 (`src`) fades
in behind and ends up filling the frame. At the end m1 is full screen and the small
window is still visible in its corner.

TIMING (one plan entry covers a RANGE of segments, and the move is continuous across the
joins — it never restarts at a cut):

    |<-- start -->|<-------- hold -------->|<-- end -->|
    0            start                    dur-end     dur
    m0 centred  m0 in the corner           starts back home

  `start` : the window IS in the corner at `start` seconds (the way out lasts `start`)
  `end`   : the window leaves the corner `end` seconds before the end and is back at its
            initial place on the last frame (the way back lasts `end`)
  start=0 -> already in the corner from the first frame; end=0 -> it never comes back.
"""

NAME = "corner_reveal"

# File paths in the descriptor: the engine makes them absolute with the same rule as the
# CONFIG's INPUT/OUTPUT, so a plan can write src="../clips/screen.mp4" and mean it.
PATHS = ("src",)

CORNERS = ("bottom_left", "bottom_right", "top_left", "top_right")


def corner_reveal(src, w=35, l=35, x=0.5, y=0.5, corner="bottom_left", g=3, z=100,
                  start=1.0, end=1.0, fade=0.8, blur=20, volume=0.0, fit="cut",
                  crf=18, preset="medium", fusion=False, only=None) -> dict:
    """Describe the reveal. Every parameter belongs to THIS effect:

    src     : m1 — the image or video that ends up filling the frame (a path)
    w, l    : size of the window kept from m0, in % of the frame width / height (35/35)
    x, y    : where that window sits at the start, as fractions of the frame (0.5, 0.5 =
              centre). The region of m0 kept is the one centred there, so nothing jumps
              on the first frame.
    corner  : where the window ends up: bottom_left (default), bottom_right, top_left,
              top_right
    g       : gap between the window and the edges, in % of the frame width (3)
    z       : size of the window, in % of the size it would have natively (100 = the
              window shows m0 pixel for pixel, 50 = half size, 150 = 1.5x). The window
              keeps the same content either way: it is only scaled.
    start   : the window is settled IN THE CORNER at this many seconds (the way out lasts
              `start`); 0 = it starts there
    end     : the window leaves the corner this many seconds before the end and is back
              at its initial place on the last frame; 0 = it never comes back
    fade    : length of m1's fade-in, in seconds (0 = it pops, no fade)
    blur    : blur applied to the camera behind m1 (0 = none, 20 = soft). It only shows
              while m1 is still transparent.
    volume  : level of m1's own sound, 0..1 (0 = silent, the clip's audio is kept). It is
              mixed UNDER the clip's voice and fades with the picture.
    fit     : when m1 is LONGER than the window: "cut" plays it and stops at the end;
              "speed" speeds it up (picture + its own sound) so the whole media fits.
              A shorter media keeps looping.
    crf     : H.264 quality of the re-encoded segment (18 = visually lossless)
    preset  : x264 speed/size trade-off
    fusion  : True = the range is joined into ONE clip and the effect runs once
    only    : camera file prefixes to treat (None = every camera)
    """
    if not src:
        raise ValueError("corner_reveal needs src=<image or video>")
    if corner not in CORNERS:
        raise ValueError(f"corner_reveal: corner must be one of {CORNERS}")
    if not (0 < w <= 100) or not (0 < l <= 100):
        raise ValueError("corner_reveal: w and l are percentages, between 1 and 100")
    if not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
        raise ValueError("corner_reveal: x and y are fractions of the frame, 0..1")
    if g < 0:
        raise ValueError("corner_reveal: g is a percentage, it cannot be negative")
    if z <= 0:
        raise ValueError("corner_reveal: z is a percentage of the window size, > 0")
    return {"effect": NAME, "src": str(src), "w": float(w), "l": float(l),
            "x": float(x), "y": float(y), "corner": str(corner), "g": float(g),
            "z": float(z), "start": float(start), "end": float(end), "fade": float(fade),
            "blur": float(blur), "volume": float(volume), "fit": str(fit),
            "crf": int(crf), "preset": str(preset), "fusion": bool(fusion),
            "only": only}


def merged_window(total: float, desc: dict) -> dict:
    """The window on a FUSED range: one clip, one move, from `start` to `total - end`."""
    if desc["start"] + desc["end"] > total:
        raise SystemExit(f"corner_reveal: start {desc['start']}s + end {desc['end']}s "
                         f"is longer than the {total:.2f}s merged clip")
    return {"offset": 0.0, "range": total, "start": desc["start"],
            "end": desc["end"], "fade": desc["fade"]}


def local_windows(clips: list, indices: list, desc: dict) -> dict:
    """Global window -> the LOCAL window of every target segment.

    A segment in the middle of the range carries the part of the move that falls inside
    it: its local start/end are derived from the global window, so the window position
    is continuous across the joins (no jump at a cut). Segments outside the window are
    left untouched (None), and the fade-in only happens on the first one.
    """
    T0 = clips[indices[0]]["start"]
    T1 = clips[indices[-1]]["start"] + clips[indices[-1]]["dur"]
    if desc["start"] + desc["end"] > T1 - T0:
        raise SystemExit(f"corner_reveal: start {desc['start']}s + end {desc['end']}s "
                         f"is longer than the {T1 - T0:.2f}s range")
    out = {}
    for i in indices:
        c = clips[i]
        # `offset` puts the segment on the range's timeline: negative once the effect has
        # already begun, which is what keeps the movement continuous across a cut.
        out[i] = {"offset": T0 - c["start"], "range": T1 - T0,
                  "start": desc["start"], "end": desc["end"],
                  "fade": desc["fade"] if T0 >= c["start"] else 0.0}
    return out


def _probe(path: str) -> tuple:
    """(width, height, fps) of a media file."""
    import cv2
    cap = cv2.VideoCapture(path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
    cap.release()
    return w, h, fps


def _is_image(path: str) -> bool:
    return path.lower().rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "webp", "bmp")


def _has_audio(path: str) -> bool:
    import subprocess
    import imageio_ffmpeg
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    return "Audio:" in out


def _atempo(factor: float) -> str:
    """atempo only accepts 0.5..2.0 per stage: chain the stages for a stronger change."""
    parts, f = [], factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.6f}")
    return ",".join(parts)


def command(ctx: dict, desc: dict, mp4: str, dest_mp4: str):
    """The ffmpeg command producing one camera of one segment (None = nothing to do)."""
    import os
    import subprocess
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()

    win = ctx.get("window")
    if not win:
        return None

    dur = float(ctx["dur"])
    W, H, fps = _probe(mp4)
    if not W or not H:
        raise SystemExit(f"corner_reveal: cannot read {mp4}")
    src = desc["src"]
    still = os.path.exists(src) and _is_image(src)
    if not os.path.exists(src):
        raise SystemExit(f"corner_reveal: media not found: {src}")

    def even(v: float) -> int:
        return max(2, int(round(v / 2.0)) * 2)

    # the region kept from m0 (native pixels), and the size it is DISPLAYED at: `z` %
    # of that region — so z=100 shows it pixel for pixel, z=50 halves it
    raw_w, raw_h = even(W * desc["w"] / 100.0), even(H * desc["l"] / 100.0)
    if raw_w >= W or raw_h >= H:
        raise SystemExit("corner_reveal: w/l keep the whole frame — nothing to slide")
    cw, ch = even(raw_w * desc["z"] / 100.0), even(raw_h * desc["z"] / 100.0)
    x0 = min(max(0, desc["x"] * W - cw / 2), W - cw)
    y0 = min(max(0, desc["y"] * H - ch / 2), H - ch)

    # the region is centred on (x,y); the displayed window is centred on the same point
    cx = min(max(0, desc["x"] * W - raw_w / 2), W - raw_w)
    cy = min(max(0, desc["y"] * H - raw_h / 2), H - raw_h)

    gap = desc["g"] * W / 100.0
    c = desc["corner"]
    x1 = gap if c.endswith("left") else W - cw - gap
    y1 = gap if c.startswith("top") else H - ch - gap
    if x1 < 0 or y1 < 0:
        raise SystemExit("corner_reveal: the gap is larger than the free space")

    off = float(win["offset"])                 # may be negative: the move began earlier
    rng = max(0.001, float(win["range"]))
    st = max(0.0, float(win.get("start", desc["start"])))
    en = max(0.0, float(win.get("end", desc["end"])))
    fade = float(win.get("fade", desc["fade"]))
    media_at = max(0.0, off)                   # m1 can only start inside this clip

    # m1: scaled to COVER the frame (aspect kept, the overflow is cropped)
    media_natural = 0.0 if still else _duration(src)
    speed = 1.0
    fit_speed = (not still and desc["fit"] == "speed"
                 and media_natural > dur + 0.05)
    if fit_speed:
        speed = media_natural / dur
        media_in = ["-t", f"{media_natural:.3f}", "-i", src]
    elif still:
        media_in = ["-loop", "1", "-t", f"{dur:.3f}", "-i", src]
    else:
        media_in = ["-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", src]

    # background: the camera, optionally blurred (shrunk 4x first: a gblur on a 4K frame
    # needs gigabytes, the same soft look costs ~16x less memory this way)
    base = "[0:v]split[base][tb];"
    if desc["blur"] > 0:
        bw, bh = max(2, (W // 4) // 2 * 2), max(2, (H // 4) // 2 * 2)
        base += (f"[tb]scale={bw}:{bh},gblur=sigma={desc['blur'] / 4:g},"
                 f"scale={W}:{H}[bgb];")
        bg_src = "[bgb]"
    else:
        base += "[tb]copy[bgb];"
        bg_src = "[bgb]"

    # m1 fades in on its own 0-based timeline, then is offset to the move's start
    m1 = f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},format=rgba"
    if fit_speed:
        m1 += f",setpts=PTS/{speed:.6f}"
    if fade > 0:
        m1 += f",fade=t=in:st=0:d={fade:.3f}:alpha=1"
    m1 += f",setpts=PTS+{media_at:.3f}/TB[m1]"

    # the moving window: the kept region of m0, sliding from (x0,y0) to (x1,y1)
    # two ramps on the range's own clock (t + off, so a cut does not reset it):
    #   out  : 0 -> 1 during the first `start` seconds  (it IS in the corner at `start`)
    #   back : 0 -> 1 during the last `end` seconds     (it is home again at the end)
    if st > 0:
        out_p = f"clip((t+{off:.3f})/{st:.3f},0,1)"
        out_e = f"({out_p})*({out_p})*(3-2*({out_p}))"
    else:
        out_e = "1"
    if en > 0:
        back_p = f"clip((t+{off:.3f}-{rng - en:.3f})/{en:.3f},0,1)"
        back_e = f"({back_p})*({back_p})*(3-2*({back_p}))"
    else:
        back_e = "0"
    ease = f"({out_e})*(1-{back_e})"
    wx = f"{x0:.2f}+({x1 - x0:.2f})*({ease})"
    wy = f"{y0:.2f}+({y1 - y0:.2f})*({ease})"

    fc = (base
          + m1 + ";"
          + f"{bg_src}[m1]overlay=0:0:format=yuv420[bg];"
          + f"[base]crop={raw_w}:{raw_h}:{cx:.0f}:{cy:.0f},"
          + f"scale={cw}:{ch}:flags=lanczos[win];"
          + f"[bg][win]overlay=x='{wx}':y='{wy}':format=yuv420[out]")

    # audio: m1's own sound is optional and always mixed UNDER the voice
    mixing = desc.get("volume", 0) > 0 and not still and _has_audio(src)
    acodec = ["-c:a", "copy"]
    if mixing:
        chain = (f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                 f"volume={desc['volume']:g}")
        if fit_speed:
            chain += "," + _atempo(speed)
        chain += f",afade=t=in:st={media_at:.3f}:d={fade:.3f}" if fade > 0 else ""
        fc += f";{chain}[ma];[0:a][ma]amix=inputs=2:duration=first:normalize=0[aout]"
        acodec = ["-c:a", "aac", "-b:a", "192k"]

    print(f"  {os.path.basename(mp4)} {W}x{H} @ {fps:g} fps  {dur:.2f}s — window "
          f"{cw}x{ch} ({desc['w']:g}x{desc['l']:g}%, z={desc['z']:g}%) -> {c} at {st:.2f}s, "
          f"back home over the last {en:.2f}s")

    return [ff, "-y", "-i", mp4, *media_in,
            "-filter_complex", fc, "-map", "[out]",
            *(["-map", "[aout]"] if mixing else ["-map", "0:a?"]),
            "-c:v", "libx264", "-preset", str(desc["preset"]), "-crf", str(desc["crf"]),
            "-pix_fmt", "yuv420p", *acodec, "-movflags", "+faststart", dest_mp4]


def _duration(path: str) -> float:
    """Duration in seconds, read from the container header."""
    import re
    import subprocess
    import imageio_ffmpeg
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
