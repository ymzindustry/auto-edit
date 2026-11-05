#!/home/ymz/.hermes/profiles/videoeditor/venv_audio/bin/python
"""
sync_offsets.py — Locate every video INSIDE the master audio (one timeline).

Input: a folder holding
    * ONE .wav  — the good, continuous recording of the take (the MASTER), and
    * N   .mp4  — recorded separately, whose own audio comes from the SAME take.
The shared source is what makes the videos comparable to the wav: each video's
audio track is cross-correlated against the wav and the peak gives the exact
second where that video starts in the master timeline.

    master wav   |--------|--------|--------|--------|--------|--------|
    video A       [===]                       -> offset 0.8 s
    video B                [=====]            -> offset 11.2 s
    video C                          [====]   -> offset 22.4 s

prints a JSON report (and writes it with --json):

    {"wav": "...", "wav_duration": 31.2, "sr": 48000,
     "videos": [{"name": "A.mp4", "path": "...", "offset": 0.812, "confidence": 41.2,
                 "duration": 10.03, "width": 1920, "height": 1080, "fps": 30.0,
                 "low_confidence": false}, ...]}

offset > 0 : the video starts that many seconds INTO the wav.
offset < 0 : the video starts BEFORE the wav (its head predates the recording;
             it gets clipped to the wav's start when the timeline is built).
confidence : best alignment / best unrelated alignment (>3 = solid match).

Usage:
    ./sync_offsets.py <folder>
    ./sync_offsets.py <folder> --wav master.wav --json offsets.json
    ./sync_offsets.py <folder> --offset A.mp4=12.63     # force one video's offset
"""

import os
import sys

# Self-heal: if started with another interpreter, restart under our own venv.
_VENV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "venv_audio")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (os.environ.get("_WK_REEXEC") != "1" and os.path.exists(_VENV_PY)
        and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR)):
    os.environ["_WK_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import glob
import json
import re
import subprocess
import tempfile

import imageio_ffmpeg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync_replace_audio import (SR, extract_audio, find_offset, to_mono_wav,  # noqa: E402
                                video_duration)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
PROFILE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def probe_video(path: str) -> tuple:
    """(width, height, fps) of the video stream; falls back to (0, 0, 30.0)."""
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    line = next((l for l in out.splitlines() if "Video:" in l), "")
    m = re.search(r"(\d{2,5})x(\d{2,5})", line)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    f = re.search(r"([\d.]+)\s*fps", line)
    return w, h, (float(f.group(1)) if f else 30.0)


def find_wav(folder: str, explicit: str | None) -> str:
    """The master wav: --wav, else the only .wav of the folder, else stop."""
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit(f"--wav not found: {explicit}")
        return os.path.abspath(explicit)
    wavs = sorted(glob.glob(os.path.join(folder, "*.wav")))
    if len(wavs) == 1:
        return wavs[0]
    if not wavs:
        raise SystemExit(f"No .wav in {folder} — pass one with --wav, or use "
                         f"fragmentation_workflow.py which falls back to main.mp4's audio.")
    raise SystemExit("Several .wav in " + folder + " — pick one with --wav:\n  "
                     + "\n  ".join(os.path.basename(w) for w in wavs))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Find where each .mp4 of a folder starts inside the master .wav.")
    ap.add_argument("folder", help="Folder holding the master .wav and the .mp4 files")
    # --wav   : which file is the MASTER (the timeline everything is measured
    #           against). Needed only when the folder holds several .wav files;
    #           with exactly one it is picked automatically.
    # --video : check only this video (repeatable) instead of every .mp4 of the
    #           folder — handy to re-measure one offset without redoing all of them.
    # --offset: force a video's start time in seconds INTO the master, e.g.
    #           --offset camB.mp4=12.63, instead of the automatic cross-correlation.
    #           Use it when the report flags [LOW CONFIDENCE] (too much noise, music,
    #           or the video's audio is not really from the same take).
    # --json  : also save the report as a file (fragmentation_workflow passes it to cut_timeline).
    # --min-confidence: below this ratio the match is flagged low_confidence. 3 is
    #           the default; raised for real speech, never trust it on a periodic
    #           test tone (a tone correlates equally well every period).
    ap.add_argument("--wav", default=None,
                    help="File used as the master timeline (default: the single .wav)")
    ap.add_argument("--video", action="append", default=None,
                    help="Only measure this video (basename or path); repeatable")
    ap.add_argument("--offset", action="append", default=None, metavar="NAME=SECONDS",
                    help="Force a video's start instead of auto-detecting it, e.g. "
                         "camB.mp4=12.63; repeatable")
    ap.add_argument("--json", default=None, help="Also write the JSON report to this path")
    ap.add_argument("--min-confidence", type=float, default=3.0,
                    help="Matches weaker than this ratio are flagged low_confidence "
                         "(default 3)")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        raise SystemExit(f"Not a folder: {folder}")

    wav = find_wav(folder, args.wav)

    if args.video:
        videos = [v if os.path.isfile(v) else os.path.join(folder, v) for v in args.video]
    else:
        videos = sorted(glob.glob(os.path.join(folder, "*.mp4")))
    if not videos:
        raise SystemExit(f"No .mp4 found in {folder}")
    for v in videos:
        if not os.path.isfile(v):
            raise SystemExit(f"Video not found: {v}")

    forced = {}
    for spec in (args.offset or []):
        if "=" not in spec:
            raise SystemExit(f"--offset expects NAME=SECONDS, got {spec!r}")
        name, sec = spec.split("=", 1)
        forced[os.path.basename(name)] = float(sec)

    report = {"wav": wav, "wav_duration": 0.0, "sr": SR, "videos": []}

    with tempfile.TemporaryDirectory(dir=PROFILE) as tmp:
        master = os.path.join(tmp, "master.wav")
        print(f"master audio : {os.path.basename(wav)}", flush=True)
        to_mono_wav(wav, master, SR)
        import wave
        with wave.open(master, "rb") as w:
            report["wav_duration"] = w.getnframes() / float(w.getframerate())
        print(f"master length: {report['wav_duration']:.2f}s "
              f"(correlation at {SR // 3} Hz)\n", flush=True)

        for i, video in enumerate(videos, 1):
            name = os.path.basename(video)
            ref = os.path.join(tmp, f"v{i:03d}.wav")
            extract_audio(video, ref, SR)

            if name in forced:
                offset, confidence = forced[name], float("inf")
                print(f"[{i}/{len(videos)}] {name}: forced offset {offset:+.3f}s")
            else:
                offset, confidence = find_offset(master, ref)
                print(f"[{i}/{len(videos)}] {name}: offset {offset:+.3f}s  "
                      f"(confidence x{confidence:.1f})")

            w, h, fps = probe_video(video)
            entry = {
                "name": name,
                "path": os.path.abspath(video),
                "offset": round(float(offset), 4),
                "confidence": (None if confidence == float("inf")
                               else round(float(confidence), 2)),
                "duration": round(float(video_duration(video)), 3),
                "width": int(w), "height": int(h), "fps": round(float(fps), 4),
                "low_confidence": bool(confidence < args.min_confidence),
            }
            report["videos"].append(entry)
            if entry["low_confidence"]:
                print(f"    ! weak match — check this video (or force --offset {name}=SEC)")

    report["videos"].sort(key=lambda e: e["offset"])

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
