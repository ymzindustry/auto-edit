"""
shoulder_plan2.py — second pass on the `shoulder` project: a slow push-in on EVERY
segment of an already-rendered folder (it is run on effects/, and writes effects2/).

    python3 scripts/workflows/effects_workflow.py --plan scripts/plans/shoulder_plan2.py
    python3 scripts/workflows/effects_workflow.py scripts/plans/shoulder_plan2.py --dry-run

WHAT THE PLAN CARRIES
  INPUT   the folder read by the run (here test_effects/)
  OUTPUT  where the result goes (here effects2/)
  PLAN    the effects themselves
  A plan is therefore the whole job: the command only needs its path. --input / --output
  still win over these two if you want a one-off.

HOW A PLAN WORKS
  One entry = one effect applied to a range of segments. "segments" takes folder names,
  positions in order.txt, a range(), or "all". Segments no entry names are copied as they
  are. fusion=True glues the range into ONE clip first, so the effect runs once and never
  restarts at a join. Each entry must own its segments: ranges cannot overlap.

ZOOM
  The move always runs from `start` (seconds into the segment) to `dur - end` (seconds
  before its last frame): start=0/end=0 means it fills the whole segment, start=1/end=2
  starts 1 s in and finishes 2 s before the end.
  min is ONLY a filter on which segments get the effect: a segment must be LONGER than
  min to be zoomed, anything else is copied untouched (min=2 -> a 2 s clip is not zoomed).
  each=True restarts the move on every segment.

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

zoom PARAMETERS
  zoom        final magnification (1.3 = +30 %)
  start       seconds into the segment before the move begins (0.5)
  end         the move finishes this many seconds before the segment ends (0.5)
  each        True = a fresh move on every segment
  min         segments shorter than this many seconds are left untouched (2)
"""
INPUT = "../video-projects/legsmedium/fragmentation/"

OUTPUT = "../video-projects/legsmedium/zoom/"

from effects import zoom

Z = dict(zoom=1.2, start=0, end=0, each=True, min=3,
         crf=18, preset="medium", only="main")

PLAN = [
    {"segments": "all",
     "effect": zoom(**Z)}

]
