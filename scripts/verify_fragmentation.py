#!/home/ymz/.hermes/profiles/videoeditor/venv_audio/bin/python
"""
verify_fragmentation.py — Check that a fragmentation/ folder is actually correct.

It does not trust the logs; it re-measures the outputs:

  1. every segment of fragmentation/order.txt has its .mp4 and .txt;
  2. the segment DURATION matches the plan (sync.json);
  3. the AUDIO really is the master recording at the planned instant:
     the segment's audio is cross-correlated against the take's master audio and
     the detected offset must match the segment's planned start time;
  4. the PICTURE really comes from the right video at the right time: the
     segment's first frame is compared (MSE) against the source video's frame at
     the piece's local start, and against a deliberately wrong instant — the
     right one must match better.

Usage:
    ./verify_fragmentation.py <folder>          # the folder you passed to fragmentation_workflow.py
"""

import os
import sys

_VENV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "venv_audio")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (os.environ.get("_WK_REEXEC") != "1" and os.path.exists(_VENV_PY)
        and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR)):
    os.environ["_WK_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import json
import subprocess
import tempfile

import imageio_ffmpeg
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync_replace_audio import SR, extract_audio, find_offset  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
PROFILE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def frame(path: str, t: float, w: int, h: int):
    """One frame as an (h, w, 3) uint8 array (rawvideo, no image libs needed).

    The source frame is put through the SAME scale+pad chain the pipeline uses, so a
    4K camera and a portrait screen recording can be compared with the segment that
    was made from them (otherwise the bytes are read at the wrong width and the
    comparison is garbage).
    """
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    r = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", path,
                        "-frames:v", "1", "-vf", vf,
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    buf = np.frombuffer(r.stdout[:w * h * 3], dtype=np.uint8)
    return buf.reshape(-1, h, w, 3)[0] if buf.size == w * h * 3 else None


def mse(a, b) -> float:
    # int32, not int16: squaring an 8-bit difference overflows int16 (255**2 > 32767)
    d = a.astype(np.int32) - b.astype(np.int32)
    return float((d ** 2).mean())


def main() -> None:
    ap = argparse.ArgumentParser(prog="verify_fragmentation.py",
                                 description="Verify a fragmentation/ folder produced "
                                             "by fragmentation_workflow.py")
    ap.add_argument("folder", help="The working folder (holding fragmentation/ and the take)")
    # These two are the only knobs, and they are just tolerances for the checks:
    # --tol-audio : how far (seconds) a segment's audio may sit from where the plan
    #               says it should — the segment audio is cross-correlated against
    #               the take's master audio, so this catches a wrong slice or a
    #               sync mistake. Default 0.15 s; small AAC/frame effects stay well
    #               below it.
    # --tol-dur   : how far (seconds) a segment's real duration may differ from the
    #               planned one (re-encoding and AAC padding add a few tens of ms).
    #               Default 0.25 s.
    ap.add_argument("--tol-audio", type=float, default=0.15,
                    help="Allowed audio offset error, seconds (default 0.15)")
    ap.add_argument("--tol-dur", type=float, default=0.25,
                    help="Allowed duration error vs the plan, seconds (default 0.25)")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    for name in ("fragmentation", "wk1"):        # current name, then the legacy one
        if os.path.isdir(os.path.join(folder, name)):
            folder = os.path.join(folder, name)
            break
    frag = folder
    with open(os.path.join(frag, "sync.json"), encoding="utf-8") as f:
        sync = json.load(f)
    with open(os.path.join(frag, "order.txt"), encoding="utf-8") as f:
        order = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    master = sync["master_audio"]
    fallback = sync.get("canvas") or [1280, 720]   # each camera has its own size now
    plan = {s["name"]: s for s in sync["segments"]}
    vids = {v["name"]: v for v in sync["videos"]}
    vpath = {v["name"]: v["path"] for v in sync["videos"]}

    fails = []
    print(f"folder : {folder}")
    print(f"folder : {len(order)} segment(s) in order.txt, "
          f"{len(plan)} in the plan")
    if len(order) != len(plan):
        fails.append("order.txt and the plan disagree on the segment count")

    with tempfile.TemporaryDirectory(dir=PROFILE) as tmp:
        # the master audio as a mono 48 kHz wav (same conversion the pipeline makes).
        # extract_audio uses -vn, so it also works when the master is main.mp4.
        mwav = os.path.join(tmp, "master.wav")
        extract_audio(master, mwav, SR)

        for name in order:
            seg_dir = os.path.join(frag, name)
            seg = plan.get(name)
            if not seg:
                fails.append(f"{name}: not in the plan")
                continue
            txt = os.path.join(seg_dir, name + ".txt")
            if not os.path.isfile(txt):
                fails.append(f"{name}: missing .txt")
            files = seg.get("files") or [{"file": name + ".mp4",
                                          "pieces": seg.get("pieces", [])}]
            for fl in files:
                label = f"{name}/{fl['file']}"
                # each camera keeps its own format: compare at THAT file's size
                w, h = fl.get("size") or fallback
                mp4 = os.path.join(seg_dir, fl["file"])
                if not os.path.isfile(mp4):
                    fails.append(f"{label}: missing")
                    continue

                # ---- duration
                dur = probe_duration(mp4)
                d_err = abs(dur - seg["duration"])
                ok_dur = d_err <= args.tol_dur

                # ---- audio = the master recording at the planned time?
                seg_wav = os.path.join(tmp, "seg.wav")
                extract_audio(mp4, seg_wav, SR)
                offset, conf = find_offset(mwav, seg_wav)
                a_err = abs(offset - seg["start"])
                ok_audio = a_err <= args.tol_audio

                # ---- picture from the right video at the right local time?
                first_video = next((p for p in fl.get("pieces", [])
                                    if p["kind"] == "video"), None)
                ok_pic, pic_note = True, "no video piece (black file)"
                if first_video:
                    ls = float(first_video["local_start"])
                    lead = float(first_video["master_start"]) - float(seg["start"])
                    src = vpath[first_video["video"]]
                    src_dur = vids[first_video["video"]]["duration"]
                    seg_t = max(lead, 0.0) + 0.05
                    src_t = max(ls, 0.0) + 0.05
                    wrong_t = min(src_t + 2.5, src_dur - 0.1)
                    f_seg = frame(mp4, seg_t, w, h)
                    cands = [frame(src, src_t + d, w, h)
                             for d in (-0.12, -0.08, -0.04, 0.0, 0.04, 0.08, 0.12)]
                    f_off = frame(src, wrong_t, w, h)
                    cands = [c for c in cands if c is not None]
                    if f_seg is None or not cands or f_off is None:
                        ok_pic, pic_note = False, "could not read a frame"
                    else:
                        m_exp = min(mse(f_seg, c) for c in cands)
                        m_off = mse(f_seg, f_off)
                        ok_pic = m_exp < 900 and m_exp <= m_off
                        pic_note = (f"{first_video['video']} @ {src_t:.2f}s "
                                    f"(+{lead:.2f}s in)  MSE {m_exp:.0f} "
                                    f"(wrong instant {m_off:.0f})")

                status = "OK " if (ok_dur and ok_audio and ok_pic) else "FAIL"
                if status == "FAIL":
                    fails.append(f"{label}: dur_err {d_err:.3f}s (tol {args.tol_dur}), "
                                 f"audio_err {a_err:.3f}s (tol {args.tol_audio}), "
                                 f"picture {pic_note}")
                print(f"[{status}] {label:<26} dur {dur:6.2f} (plan {seg['duration']:6.2f}, "
                      f"err {d_err:.3f})  audio at {offset:7.3f} (plan {seg['start']:7.3f}, "
                      f"err {a_err:.3f}, x{conf:.1f})  {pic_note}")

    print()
    if fails:
        print(f"{len(fails)} problem(s):")
        for f in fails:
            print("  - " + f)
        raise SystemExit(1)
    print(f"ALL GOOD: {len(order)} segment(s) verified "
          f"(duration, audio sync against the master, and source picture)")


def probe_duration(path: str) -> float:
    import re
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


if __name__ == "__main__":
    main()
