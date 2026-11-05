"""
example_media_plan.py — a plan that overlays demo clips on a talking-head montage.

    python3 scripts/workflows/effects_workflow.py --plan scripts/plans/example_media_plan.py
    python3 scripts/workflows/effects_workflow.py --plan scripts/plans/example_media_plan.py --dry-run
    python3 scripts/workflows/effects_workflow.py --plan scripts/plans/example_media_plan.py --input ../video-projects/my-project

HOW A PLAN WORKS
  One entry = one effect applied to a range of segments. "segments" takes folder names,
  positions in order.txt, a range(), or "all". Segments no entry names are copied as they
  are. fusion=True glues the range into ONE clip first, so the effect runs once and never
  restarts at a join. Each entry must own its segments: ranges cannot overlap.

AVAILABLE EFFECTS
  add_media     an image/video laid over the camera (pct, blur, fades, fit, volume)
  push_video    one camera slides in and pushes the other (direction, percent, pct)
  side_by_side  the two cameras next to each other, nothing cropped (position, gap_pct)
  zoom          push-in on the camera, from `start` to `end`, reaching `zoom` (2.0 = x2)

add_media PARAMETERS
  src         the image or video laid over the camera (relative to scripts/, so ../clips/x.mp4)
  start       seconds into the merged clip before it appears (0.2)
  end         the fade-out finishes this many seconds before the clip ends (0.2)
  fade_in     fade-in length, seconds (0.4)
  fade_out    fade-out length, seconds (0.4)
  pct         media width as a % of the frame width, aspect kept (70)
  blur        blur behind it, 0 = none, 20 = soft (20)
  volume      level of the media's own sound, 0 = silent media (0)
  fit         "cut" plays the media and cuts it at the end of the window (default);
              "speed" speeds it up so the WHOLE media fits in the window
  fusion      True = the range becomes one clip, the effect runs once (True)
  only        "_main" to treat one camera, None = every camera
  crf         H.264 quality of the re-encoded segments (18)
"""
from effects import *

P = dict(start=0.2, end=0.2, fade_in=0.4, fade_out=0.4,
         pct=70, blur=20, volume=0, fusion=True, only=None)

PLAN = [
    {"segments": range(17, 27),
     "effect": add_media(src="../clips/bench.mp4", **P)},

    {"segments": range(27, 30),
     "effect": add_media(src="../clips/dumbels.mp4", **P)},

    {"segments": range(30, 39),
     "effect": add_media(src="../clips/machine.mp4", **P)},

    {"segments": range(40, 50),
     "effect": add_media(src="../clips/gainage.mp4", **P)},

    {"segments": range(50, 61),
     "effect": add_media(src="../clips/gainage2.mp4", **P)},
]
