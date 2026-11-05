"""
side_by_side — put the two cameras of a segment NEXT TO each other, both fully visible.

This is the "screen to the LEFT of the hand" layout: no push, no crop, no slide — the
frame simply gets WIDER so that nothing is cut.

    position="left"    element (screen) on the LEFT,  base (hand) on the RIGHT
    position="right"   base (hand)    on the LEFT,    element (screen) on the RIGHT

* the canvas keeps the BASE camera's height (a 4K hand stays 2160 px tall — nothing is
  downscaled) and grows horizontally by the element's width + the gap;
* the element is scaled to `height_pct` % of the canvas height with its aspect kept, so
  a portrait screen recording stays portrait;
* the audio is the BASE camera's own track (the enhanced voice), never a mix.

`base` and `element` are PREFIXES of the two camera file names of the segment:
base="main", element="screen". The result REPLACES both files — the effects/ folder keeps one file for
that segment, as the two angles are now one picture.
"""

NAME = "side_by_side"
PAIR = True          # needs TWO camera files of the segment and produces ONE


def side_by_side(base, element, position="left", height_pct=100.0, gap_pct=1.0,
                 pad="black", crf=18, preset="medium", fusion=False) -> dict:
    """Describe the layout — every parameter here belongs to THIS effect:

    base       : prefix of the camera that sets the canvas (v1, the hand)
    element    : prefix of the camera placed beside it (v2, the screen)
    position   : "left" (screen on the left of the hand) or "right"
    height_pct : height of the element, in % of the canvas height (100 = as tall as
                 the base frame, 60 = a smaller window beside it)
    gap_pct    : black strip between the two pictures, in % of the base WIDTH
    pad        : colour of the gap / of any empty area ("black", "#101010", ...)
    crf        : H.264 quality of the re-encoded result (18 = visually lossless)
    preset     : x264 speed/size trade-off ("medium"; "veryfast" for a quick test)
    fusion     : True = join the segments into ONE clip first (no cut at the joins)
    """
    if not base or not element:
        raise SystemExit("side_by_side needs base=<prefix of v1> and element=<prefix of v2>")
    if position not in ("left", "right"):
        raise SystemExit("side_by_side: position must be left or right")
    if not 5 <= float(height_pct) <= 100:
        raise SystemExit("side_by_side: height_pct must be between 5 and 100")
    return {"effect": NAME, "base": str(base), "element": str(element),
            "position": position, "height_pct": float(height_pct),
            "gap_pct": float(gap_pct), "pad": str(pad), "crf": int(crf),
            "preset": str(preset), "fusion": bool(fusion)}


def local_windows(clips: list, indices: list, desc: dict) -> dict:
    """The layout covers each targeted segment from its first to its last frame."""
    return {i: {"start": 0.0, "end": 0.0} for i in indices}


def merged_window(total: float, desc: dict) -> dict:
    """Same thing on a fused range: the whole merged clip."""
    return {"start": 0.0, "end": 0.0}


def _match(cams: dict, prefix, want: str) -> str:
    hits = [p for cam, p in cams.items() if cam.startswith(str(prefix))]
    if len(hits) > 1:
        raise SystemExit(f"side_by_side: {want}={prefix!r} matches several cameras "
                         f"({', '.join(sorted(hits))}) — use a longer prefix")
    return hits[0] if hits else ""


def _size(path: str) -> tuple:
    """(width, height) of a file, read from its header."""
    import cv2
    cap = cv2.VideoCapture(path)
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return w, h


def _fps(path: str) -> float:
    import cv2
    cap = cv2.VideoCapture(path)
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
    cap.release()
    return fps


def _even(n: float) -> int:
    """yuv420p needs even dimensions: round down to the nearest even integer."""
    return max(2, int(round(n)) // 2 * 2)


def command(ctx: dict, desc: dict, mp4: str, dest: str):
    """The command composing ONE segment (or one fused range) into a single picture."""
    import os
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()

    cams = ctx["cams"]                      # {camera: path} for this segment/range
    v1 = _match(cams, desc["base"], "base")
    v2 = _match(cams, desc["element"], "element")
    if not v1 or not v2:
        raise SystemExit(f"side_by_side: cannot find a camera starting with "
                         f"base={desc['base']!r} AND one with element={desc['element']!r} "
                         f"in {', '.join(sorted(cams))}")

    W, H = _size(v1)                        # the canvas height comes from the base
    ew, eh = _size(v2)
    h2 = _even(H * desc["height_pct"] / 100.0)
    w2 = _even(h2 * ew / eh) if eh else h2
    gap = _even(W * desc["gap_pct"] / 100.0) if desc["gap_pct"] else 0
    CW, CH = _even(w2 + gap + W), _even(H)
    fps = _fps(v1)
    dur = float(ctx["dur"])

    if desc["position"] == "left":
        x1, x2 = w2 + gap, 0                 # base right, element left
    else:
        x1, x2 = 0, W + gap                  # base left, element right
    y2 = _even((CH - h2) / 2)                # element centred vertically

    fc = (f"[0:v]scale={W}:{H},setsar=1[b];"
          f"[1:v]scale={w2}:{h2},setsar=1[e];"
          f"color=c={desc['pad']}:s={CW}x{CH}:r={fps:g}:d={dur:.3f}[bg];"
          f"[bg][e]overlay=x={x2}:y={y2}:eof_action=pass[t];"
          f"[t][b]overlay=x={x1}:y=0:eof_action=pass,format=yuv420p[out]")

    print(f"  {os.path.basename(v1)} {W}x{H}  +  {os.path.basename(v2)} {ew}x{eh}"
          f" -> {w2}x{h2} on the {desc['position']}, gap {gap}px "
          f"-> canvas {CW}x{CH} @ {fps:g} fps")

    return [ff, "-y", "-i", v1, "-i", v2, "-filter_complex", fc,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", desc["preset"], "-crf", str(desc["crf"]),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-shortest", dest]
