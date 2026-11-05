"""
push_video — push v1 aside to let v2 in, with a crop and an overlap you control.

The TWO cameras of a segment are composed into ONE file (this effect consumes both):

    direction="right"   v2 enters from the RIGHT,  v1 slides LEFT
    direction="left"    v2 enters from the LEFT,   v1 slides RIGHT
    direction="down"    v2 enters from the BOTTOM, v1 slides UP
    direction="up"      v2 enters from the TOP,    v1 slides DOWN

* v2 is scaled so that it is COMPLETELY VISIBLE: it fits inside `pct` % of the frame
  measured ACROSS the entry axis (its width for left/right, its height for up/down),
  and takes the full length of the other axis;
* v1 is then pushed by exactly the size of v2 MINUS the overlap, so the far side of v1
  goes out of frame — that is v1 being "cropped" by `percent` % of itself;
* `percent` = how much v2 and v1 OVERLAP, in % of v1 (10 = v2 covers the last 10 % of
  v1). 0 = no overlap at all, v2 simply sits beside v1;
* front=True  -> v2 is ON TOP of v1 inside the overlap band (v2 overlaps v1);
  front=False -> v1 stays ON TOP (v2 slides UNDER v1);
* fade_in / fade_out / start / end / volume behave like in add_media: v2 fades in and
  out, and its sound is mixed UNDER v1's voice.

`base` and `element` are PREFIXES of the two camera file names of the segment:
base="main", element="screen" (or "secreen", whatever your file is called).
The frame of the result is v1's frame, and the result REPLACES both files — the effects/ folder keeps
one file for that segment, as the two angles are now one picture.
"""

NAME = "push_video"
PAIR = True          # needs TWO camera files of the segment and produces ONE


def push_video(base, element, direction="right", percent=10.0, pct=45.0, front=True,
               fade_in=0.4, fade_out=None, start=0.0, end=0.0, crf=18, volume=0.0,
               fusion=False) -> dict:
    """Describe the push — every parameter here belongs to THIS effect:

    base     : prefix of the MAIN video's name (v1) — the one that gets pushed
    element  : prefix of the video to call in (v2) — the one that must stay fully visible
    direction: which side v2 enters from: "right" (default), "left", "up", "down"
    percent  : share OF V2 that lies on v1, in % of v2 (25 = a quarter of v2 covers
               v1). It is what sets how far v1 is pushed: push = v2 size - overlap.
               0 = side by side (no overlap), 100 = v1 not pushed (v2 lands on it)
    pct      : size of v2 across the entry axis, in % of the frame (45 = 45 %);
               the other axis keeps its aspect, and v2 always fits entirely
    front    : True = v2 covers v1 in the overlap band; False = v2 slides under v1
    fade_in  : ALSO the length of the SLIDE: v2 starts coming in at `start` and is
               completely on screen at `start + fade_in`. 0 = it appears instantly.
    fade_out : how long v2 takes to leave again; None = same as fade_in
    start    : seconds into the first segment of the range where v2 comes in
    end      : the push is undone this many seconds before the last segment ends
    crf      : H.264 quality of the re-encoded result (18 = visually lossless)
    volume   : level of v2's OWN sound, 0..1 — **0 by default**, because both cameras
               record the SAME take (same source): mixing them would double the voice
               and echo. Raise it only if v2 carries its own separate audio.
    fusion   : True = join the segments into ONE clip first (no cut at the joins)
    """
    if not base or not element:
        raise SystemExit("push_video needs base=<prefix of v1> and element=<prefix of v2>")
    if direction not in ("right", "left", "up", "down"):
        raise SystemExit("push_video: direction must be right, left, up or down")
    return {"effect": NAME, "base": str(base), "element": str(element),
            "direction": direction, "percent": float(percent), "pct": float(pct),
            "front": bool(front), "fade_in": float(fade_in),
            "fade_out": None if fade_out is None else float(fade_out),
            "start": float(start), "end": float(end), "crf": int(crf),
            "volume": float(volume), "fusion": bool(fusion)}


def merged_window(total: float, desc: dict) -> dict:
    """When the range is fused: v2 comes in `start` after the merged clip begins and
    the push is undone `end` seconds before it ends."""
    fo = desc["fade_in"] if desc["fade_out"] is None else desc["fade_out"]
    return {"start": desc["start"], "end": desc["end"],
            "fade_in": desc["fade_in"], "fade_out": fo}


def local_windows(clips: list, indices: list, desc: dict) -> dict:
    """Same range maths as add_media: the push only starts on the first segment of the
    range and is only undone on the last one (no jump at the joins)."""
    fi = desc["fade_in"]
    fo = fi if desc["fade_out"] is None else desc["fade_out"]
    T0 = clips[indices[0]]["start"] + desc["start"]
    T1 = clips[indices[-1]]["start"] + clips[indices[-1]]["dur"] - desc["end"]
    if T1 <= T0:
        raise SystemExit(f"push_video: the push would be undone before it starts "
                         f"({T0:.2f}s -> {T1:.2f}s)")
    out = {}
    for i in indices:
        c = clips[i]
        s0, s1 = c["start"], c["start"] + c["dur"]
        if T1 <= s0 or T0 >= s1:
            out[i] = None
            continue
        local_start = max(0.0, T0 - s0)
        local_out = min(T1, s1) - s0
        out[i] = {"start": local_start, "end": c["dur"] - local_out,
                  "fade_in": fi if T0 >= s0 else 0.0,
                  "fade_out": fo if T1 <= s1 else 0.0}
    return out


def _match(cams: dict, prefix, want: str) -> str:
    hits = [p for cam, p in cams.items() if cam.startswith(str(prefix))]
    if len(hits) > 1:
        raise SystemExit(f"push_video: {want}={prefix!r} matches several cameras "
                         f"({', '.join(sorted(hits))}) — use a longer prefix")
    return hits[0] if hits else ""


def _size(path: str) -> tuple:
    """(width, height) of a file, read from its header."""
    import cv2
    cap = cv2.VideoCapture(path)
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return w, h


def _duration(path: str) -> float:
    import re
    import subprocess
    import imageio_ffmpeg
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def _has_audio(path: str) -> bool:
    import subprocess
    import imageio_ffmpeg
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    return "Audio:" in out


def _fit_box(path: str, box_w: float, box_h: float) -> tuple:
    """The size v2 takes so it is ENTIRELY visible inside the box, aspect kept."""
    w, h = _size(path)
    if w <= 0 or h <= 0:
        return int(box_w), int(box_h)
    s = min(box_w / w, box_h / h)
    return (max(2, int(w * s) // 2 * 2), max(2, int(h * s) // 2 * 2))


def command(ctx: dict, desc: dict, mp4: str, dest: str):
    """The command composing ONE segment (or one fused range) into a single picture."""
    import os
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()

    cams = ctx["cams"]                      # {camera: path} for this segment/range
    v1 = _match(cams, desc["base"], "base")
    v2 = _match(cams, desc["element"], "element")
    if not v1 or not v2:
        raise SystemExit(f"push_video: cannot find a camera starting with "
                         f"base={desc['base']!r} AND one with element={desc['element']!r} "
                         f"in {', '.join(sorted(cams))}")

    win = ctx.get("window") or {"start": desc["start"], "end": desc["end"],
                                "fade_in": desc["fade_in"],
                                "fade_out": desc["fade_in"] if desc["fade_out"] is None
                                else desc["fade_out"]}
    dur = float(ctx["dur"])
    # in the plan (and in every window this engine passes around) `end` is how many
    # seconds BEFORE THE END of the clip the push is undone — so the absolute end time
    # is dur - end. Mixing the two conventions gave an empty window (between(t,.2,.2)).
    st = win["start"]
    en = dur - win["end"]
    if en <= st:
        raise SystemExit(f"push_video: the push would be undone before it starts "
                         f"({st:.2f}s -> {en:.2f}s, clip {dur:.2f}s)")
    W, H = _size(v1)
    horiz = desc["direction"] in ("right", "left")
    across = W if horiz else H                     # the axis v2 travels along
    # v2 entirely visible: pct% of the frame across the entry axis, full length on the
    # other axis (so a portrait screen recording stays portrait)
    if horiz:
        w2, h2 = _fit_box(v2, W * desc["pct"] / 100.0, H)
    else:
        w2, h2 = _fit_box(v2, W, H * desc["pct"] / 100.0)

    # `percent` is a share OF V2 (not of v1): 25 means a quarter of v2 lies on top of
    # v1. It is the overlap, so it is also what decides how far v1 is pushed:
    # push = v2's size - overlap. percent=0 -> side by side, percent=100 -> v1 not
    # pushed at all (v2 lands entirely on it, just sliding in).
    v2_across = w2 if horiz else h2
    overlap = int(round(v2_across * desc["percent"] / 100.0))
    shift = max(0, v2_across - overlap)                 # how far v1 is pushed

    # ── THE SLIDE ────────────────────────────────────────────────────────────────
    # v2 does not pop in: from `start` it SLIDES in and, `fade_in` seconds later (the
    # same knob as its alpha fade), it is COMPLETELY on screen — v1 having slid away by
    # the same amount. P is the progress 0..1, animated in AND back out at the end.
    slide = max(0.0, win["fade_in"])
    if slide > 0:
        P = (f"min(min(max((t-{st:.3f})/{slide:.3f},0),1),"
             f"min(max(({en:.3f}-t)/{slide:.3f},0),1))")
    else:
        P = f"if(between(t,{st:.3f},{en:.3f}),1,0)"

    if horiz:
        CW, CH = W, H
        if desc["direction"] == "right":        # v2 in from the right, v1 to the left
            v1_x, v2_x = f"-{shift}*{P}", f"{W}-{w2}*{P}"
        else:                                   # v2 in from the left, v1 to the right
            v1_x, v2_x = f"{shift}*{P}", f"-{w2}+{w2}*{P}"
        v1_y, v2_y = "0", f"(H-{h2})/2"
    else:
        CW, CH = W, H
        if desc["direction"] == "down":         # v2 in from the bottom, v1 upwards
            v1_y, v2_y = f"-{shift}*{P}", f"{H}-{h2}*{P}"
        else:                                   # v2 in from the top, v1 downwards
            v1_y, v2_y = f"{shift}*{P}", f"-{h2}+{h2}*{P}"
        v1_x, v2_x = "0", f"(W-{w2})/2"

    fo = win["fade_out"]
    v2chain = f"[1:v]scale={w2}:{h2},setsar=1,format=rgba"
    if win["fade_in"] > 0:
        v2chain += f",fade=t=in:st=0:d={win['fade_in']:.3f}:alpha=1"
    if fo > 0:
        v2chain += f",fade=t=out:st={max(0.0, en - st - fo):.3f}:d={fo:.3f}:alpha=1"
    v2chain += f",setpts=PTS+{st:.3f}/TB[v2]"

    on_top = "[bgv1][v2]" if desc["front"] else "[v2][bgv1]"
    fc = (f"[0:v]scale={W}:{H},setsar=1[v1];"
          f"{v2chain};"
          f"color=c=black:s={CW}x{CH}:r=30:d={dur:.3f}[bg];"
          f"[bg][v1]overlay=x='{v1_x}':y='{v1_y}'[bgv1];"
          f"{on_top}overlay=x='{v2_x}':y='{v2_y}':"
          f"enable='between(t,{st:.3f},{en:.3f})':eof_action=pass,"
          f"format=yuv420p[out]")

    mixing = desc["volume"] > 0 and _has_audio(v1) and _has_audio(v2)
    if mixing:
        ms = int(round(st * 1000))
        fc += (f";[1:a]aformat=sample_fmts=fltp:sample_rates=48000:"
               f"channel_layouts=stereo,volume={desc['volume']:g}"
               + (f",adelay={ms}|{ms}" if ms else "")
               + f"[ma];[0:a][ma]amix=inputs=2:duration=first:normalize=0[aout]")

    print(f"  v1 {os.path.basename(v1)} {W}x{H}  <- slides {shift}px "
          f"({desc['direction']}) in {slide:g}s, v2 {os.path.basename(v2)} {w2}x{h2} "
          f"({desc['pct']:g}% across), overlap {overlap}px "
          f"({desc['percent']:g}% of v2), {'in front' if desc['front'] else 'behind'}"
          f", v2 sound {desc['volume']:g}")
    return [ff, "-y", "-i", v1, "-i", v2, "-filter_complex", fc, "-map", "[out]",
            *(["-map", "[aout]"] if mixing else ["-map", "0:a?"]),
            "-c:v", "libx264", "-preset", "medium", "-crf", str(desc["crf"]),
            "-pix_fmt", "yuv420p",
            *(["-c:a", "aac", "-b:a", "192k"] if mixing else ["-c:a", "copy"]),
            "-movflags", "+faststart", dest]
