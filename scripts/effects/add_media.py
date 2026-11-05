"""
add_media — put an image or a video ON TOP of the segment(s), fading in and out.

ONE plan entry = one media inserted across a RANGE of segments, and the media behaves
as a SINGLE continuous appearance that spans the videos:

    SEGMENT 1              SEGMENT 2            SEGMENT 3
    |------|-------|       |------|------|      |------|------|
           ^                                     ^           ^
       fades in                                (already      fades out,
       at "start"                              on screen)    finished "end"s
       seconds in                                            before the end

Between the two, the media stays put: it does NOT restart and does NOT re-fade at the
joins. The main video is blurred behind it over exactly the same window.
"""

NAME = "add_media"

# Keys of the descriptor that are FILE PATHS: the engine makes them absolute with the
# same rule as the CONFIG's INPUT/OUTPUT (profile root, or "../" = from scripts/), so a
# plan can write src="../data/x.mp4" without wondering where it is run from.
PATHS = ("src",)


def add_media(src, fade_in=1.0, fade_out=None, start=1.0, end=1.0,
              pct=50, blur=20, crf=18, fusion=False, only=None, volume=0.3,
              fit="cut") -> dict:
    """Describe an insert — every parameter here belongs to THIS effect:

    src      : the image or video to put on top (a path; relative to the profile works)
    fade_in  : length of the fade-in, in seconds (0 = it pops in, no fade)
    fade_out : length of the fade-out; None = same as fade_in
    start    : how many seconds INTO the first segment of the range it appears
    end      : the fade-out must be FINISHED this many seconds before the last ends
    pct      : media width as a % of the frame width (aspect ratio always kept)
    blur     : blur applied to the main video behind it (0 = none, 20 = soft, 40 = heavy)
    crf      : H.264 quality of the re-encoded segment (18 = visually lossless)
    fusion   : True = the target segments are JOINED INTO A SINGLE CLIP and the effect
               runs ONCE on that whole clip. The inserted media then plays continuously
               (no restart and no cut at the segment joins) and the result is written as
               ONE folder for the whole range. False (default) = the effect is applied
               segment by segment, and the window maths keeps it continuous across the
               joins (each segment is still re-encoded on its own).
    only     : WHICH CAMERA files to treat, by PREFIX of the file name — exactly like
               greenscreen_workflow's --only. A segment holds one file per camera (main_3.mp4,
               secreenvideo_3.mp4, ...): only="main" applies the effect to the `main_*`
               files and COPIES THE OTHERS UNCHANGED; a list (["main", "cam2"]) keeps
               several; None (default) treats every camera.
    volume   : level of the INSERTED media's own sound, 0..1 (default 0.3). It is mixed
               UNDER the clip's voice so the two never fight; 0 = silent media (the clip
               audio is then kept as it is), 1 = as loud as the original recording.
    fit      : what happens when the media is LONGER than the slot it must fill:
               "cut" (default) plays it and cuts at the end of the slot;
               "speed" SPEEDS IT UP (picture + its own sound) so the WHOLE media fits
               exactly in the slot — the right choice for a demo clip that is 60 s long
               and has to fit in 18 s. A media shorter than the slot keeps looping.
    """
    if not src:
        raise ValueError("add_media needs src=<image or video>")
    return {"effect": NAME, "src": str(src), "fade_in": float(fade_in),
            "fade_out": None if fade_out is None else float(fade_out),
            "start": float(start), "end": float(end), "pct": float(pct),
            "blur": float(blur), "crf": int(crf), "fusion": bool(fusion),
            "fit": str(fit), "only": only, "volume": float(volume)}


def merged_window(total: float, desc: dict) -> dict:
    """The window when the range is FUSED into one clip of `total` seconds.

    No per-segment maths any more: the media appears `start` seconds after the merged
    clip begins and its fade-out finishes `end` seconds before that clip ends — so it
    is really played once, from beginning to end. Both values keep the same meaning as
    in the plan ("end" = measured from the END of the clip, which is what insert_media
    expects too).
    """
    fo = desc["fade_in"] if desc["fade_out"] is None else desc["fade_out"]
    if desc["start"] + desc["end"] >= total:
        raise SystemExit(f"add_media: start {desc['start']}s + end {desc['end']}s do not "
                         f"fit in the {total:.2f}s merged clip")
    return {"start": desc["start"], "end": desc["end"],
            "fade_in": desc["fade_in"], "fade_out": fo}


def local_windows(clips: list, indices: list, desc: dict) -> dict:
    """Global window -> the LOCAL settings of every target segment.

    Returns {segment index: {"start", "end", "fade_in", "fade_out"} or None when the
    segment falls outside the window}. `start`/`end` are local seconds inside the
    segment (insert_media works in local time), and the fades only happen once: on the
    first and on the last segment of the range.
    """
    fi = desc["fade_in"]
    fo = fi if desc["fade_out"] is None else desc["fade_out"]
    T0 = clips[indices[0]]["start"] + desc["start"]
    T1 = clips[indices[-1]]["start"] + clips[indices[-1]]["dur"] - desc["end"]
    if T1 <= T0:
        raise SystemExit(f"add_media: the fade-out would land before the fade-in "
                         f"({T0:.2f}s -> {T1:.2f}s)")

    out = {}
    for i in indices:
        c = clips[i]
        s0, s1 = c["start"], c["start"] + c["dur"]
        if T1 <= s0 or T0 >= s1:
            out[i] = None                       # outside the window: left untouched
            continue
        local_start = max(0.0, T0 - s0)
        local_out = min(T1, s1) - s0
        out[i] = {"start": local_start,
                  "end": c["dur"] - local_out,
                  "fade_in": fi if T0 >= s0 else 0.0,     # no re-fade on the middle
                  "fade_out": fo if T1 <= s1 else 0.0}    # only the last one fades out
    return out


def command(ctx: dict, desc: dict, mp4: str, dest_mp4: str):
    """The command producing one camera of one segment (None = nothing to do)."""
    import os
    win = ctx.get("window")
    if not win:
        return None
    return [ctx["venv_video"], os.path.join(ctx["scripts"], "insert_media.py"),
            ctx["seg_dir"], desc["src"], "-o", dest_mp4,
            "--file", os.path.basename(mp4),
            "--start", f"{win['start']:.3f}", "--end", f"{win['end']:.3f}",
            "--fade-in", f"{win['fade_in']:.3f}", "--fade-out", f"{win['fade_out']:.3f}",
            "--pct", f"{desc['pct']:g}", "--blur", f"{desc['blur']:g}",
            "--volume", f"{desc.get('volume', 0.3):g}",
            "--fit", str(desc.get("fit", "cut")),
            "--crf", str(desc["crf"])]
