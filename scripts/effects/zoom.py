"""
zoom — a slow push-in on the camera, from `start` to `end`, reaching `zoom` at the end.

    zoom(zoom=1.2, start=1.0, end=1.0)
    zoom(zoom=2.0, start=0.5, end=0.5, each=True)

`zoom=2` means: at the end of the move the picture shows an area half as wide and half as
tall as the original, i.e. everything looks twice as big. The move starts `start` seconds
INTO the first segment of the range and finishes `end` seconds BEFORE the last one ends,
so with fusion=True the push-in is one continuous movement over the whole range.
"""

NAME = "zoom"


def zoom(zoom=1.2, start=1.0, end=1.0, crf=18, preset="medium", fusion=False,
         only=None, each=False, min=0.0, all=False) -> dict:
    """Describe the push-in — every parameter here belongs to THIS effect:

    zoom   : final magnification, 1.0 = no zoom, 1.2 = +20 %, 2.0 = twice as big
    start  : seconds into the first segment before the move begins
    end    : the move is finished this many seconds before the last segment ends
    crf    : H.264 quality of the re-encoded segments (18 = visually lossless)
    preset : x264 effort (medium; veryfast for a quick test)
    fusion : True = the range is joined into ONE clip first, so the move runs once
    only   : camera files to treat, by file-name prefix (None = every camera)
    each   : False (default) = ONE continuous move over the whole range: it starts
             `start` after the first segment and finishes `end` before the last one.
             True = the move RESTARTS on every segment (each one goes 1 -> zoom between
             its own `start` and its own `dur - end`); fusion is then ignored, since a
             fused range has no joins left to restart on.
    min    : a segment SHORTER than this many seconds is left untouched (0 = no
             threshold). Most useful with each=True, where every segment carries its own
             move; with each=False the move is continuous over the range.
    all    : True = every segment of the stage, without listing them in the plan entry:
             {"effect": zoom(zoom=1.3, each=True, min=2, all=True)} is a complete entry.
    """
    if float(zoom) < 1.0:
        raise SystemExit("zoom: zoom must be >= 1.0 (1.0 = no zoom)")
    if float(start) < 0 or float(end) < 0:
        raise SystemExit("zoom: start and end are seconds, they cannot be negative")
    return {"effect": NAME, "zoom": float(zoom), "start": float(start),
            "end": float(end), "crf": int(crf), "preset": str(preset),
            "fusion": bool(fusion) and not each, "only": only, "each": bool(each),
            "min": float(min), "all": bool(all)}


def merged_window(total: float, desc: dict):
    """The move on a FUSED range: from `start` to `total - end` seconds.

    A fused range is ONE clip, so `min` applies to the range as a whole: return None
    (nothing to do, the range is copied) when it is shorter than the threshold.
    """
    if desc.get("min") and total < desc["min"]:
        print(f"  ! zoom: the range is {total:.2f}s, shorter than min={desc['min']:g}s — "
              f"left untouched")
        return None
    if desc["start"] + desc["end"] >= total:
        raise SystemExit(f"zoom: start {desc['start']}s + end {desc['end']}s do not fit "
                         f"in the {total:.2f}s merged clip")
    return {"start": desc["start"], "end": desc["end"]}


def local_windows(clips: list, indices: list, desc: dict) -> dict:
    """Global window -> the local start/end of every targeted segment.

    each=False: the move starts on the first segment of the range and ends on the last
    one; a segment in the middle is zoomed from its first frame to its last, so the
    motion is continuous across the joins.
    each=True: every segment gets the SAME local window, so the move restarts from 1.0
    on each of them.
    """
    if desc.get("each"):
        out = {}
        for i in indices:
            dur = clips[i]["dur"]
            if desc.get("min") and dur <= desc["min"]:
                out[i] = None
                continue
            s, e = desc["start"], desc["end"]
            if s + e >= dur:
                print(f"  ! zoom: {clips[i]['name']} is only {dur:.2f}s, start+end "
                      f"({s:g}+{e:g}) do not fit — the move covers the whole segment")
                s = e = 0.0
            out[i] = {"start": s, "end": e}
        return out

    keep = [i for i in indices
            if not (desc.get("min") and clips[i]["dur"] <= desc["min"])]
    if not keep:
        return {i: None for i in indices}
    if len(keep) != len(indices):
        for i in indices:
            if i not in keep:
                print(f"  ! zoom: {clips[i]['name']} is shorter than min="
                      f"{desc['min']:g}s — left untouched")
    t0 = clips[keep[0]]["start"] + desc["start"]
    t1 = clips[keep[-1]]["start"] + clips[keep[-1]]["dur"] - desc["end"]
    if t1 <= t0:
        raise SystemExit(f"zoom: the move would end before it starts "
                         f"({t0:.2f}s -> {t1:.2f}s)")
    out = {}
    for i in indices:
        if i not in keep:
            out[i] = None
            continue
        c = clips[i]
        s0, s1 = c["start"], c["start"] + c["dur"]
        if t1 <= s0 or t0 >= s1:
            out[i] = None
            continue
        out[i] = {"start": max(0.0, t0 - s0), "end": max(0.0, s1 - t1)}
    return out


def _probe(path: str) -> tuple:
    """(width, height, fps) of the file, read from its header."""
    import cv2
    cap = cv2.VideoCapture(path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
    cap.release()
    return w, h, fps


def command(ctx: dict, desc: dict, mp4: str, dest: str):
    """The command zooming ONE segment (or one fused range)."""
    import os
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()

    win = ctx.get("window")
    if win is None:
        return None
    dur = float(ctx["dur"])
    w, h, fps = _probe(mp4)
    if not w or not h:
        raise SystemExit(f"zoom: cannot read {mp4}")

    s = max(0.0, float(win["start"]))
    e = dur - max(0.0, float(win["end"]))
    target = float(desc["zoom"])

    if e <= s:
        return None
    if target <= 1.0001:
        return None

    span = e - s
    t = "in/{fps:g}".format(fps=fps)
    expr = (f"if(lte({t},{s:.6f}),1,"
            f"if(gte({t},{e:.6f}),{target:.6f},"
            f"1+({target:.6f}-1)*(({t}-{s:.6f})/{span:.6f})))")

    # perspective does a real sub-pixel warp; zoompan snaps the crop to whole pixels, so a
    # slow push-in comes out as a 1 px back-jump every few frames — the "shaking camera".
    xa = f"W/2-W/(2*({expr}))"
    xb = f"W/2+W/(2*({expr}))"
    ya = f"H/2-H/(2*({expr}))"
    yb = f"H/2+H/(2*({expr}))"
    fc = (f"[0:v]perspective=x0='{xa}':y0='{ya}':x1='{xb}':y1='{ya}'"
          f":x2='{xa}':y2='{yb}':x3='{xb}':y3='{yb}'"
          f":interpolation=cubic:eval=frame,format=yuv420p[out]")

    print(f"  {os.path.basename(mp4)} {w}x{h} @ {fps:g} fps  {dur:.2f}s — "
          f"zoom 1.00 -> {target:g} from {s:.2f}s to {e:.2f}s")

    return [ff, "-y", "-i", mp4, "-filter_complex", fc,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", desc["preset"], "-crf", str(desc["crf"]),
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", dest]
