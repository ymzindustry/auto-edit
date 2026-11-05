#!/home/ymz/.hermes/profiles/videoeditor/venv_audio/bin/python
"""
cut_silences.py — Remove silent / dead-air sections from a video (auto-cut),
and optionally save each kept segment as a separate file.

It detects silence in the AUDIO track (ffmpeg `silencedetect`), removes the
silent sections, and keeps a small margin (default 0.5 s) around every kept
speech segment so the cuts are not too abrupt.

Each kept segment is re-encoded once (frame-accurate cuts, frame rate
preserved), saved into a segments folder (if enabled), then concatenated
(losslessly) into the final cut video.

Usage:
    python cut_silences.py input.mp4 -o output.mp4
    python cut_silences.py input.mp4 -o output.mp4 --segments-dir out_segments
    python cut_silences.py input.mp4 --noise -35 --min-silence 0.5 --margin 0.5 -o out.mp4

Notes:
    - `--noise`        : silence threshold in dB (default -35).
    - `--min-silence`  : shortest pause (s) treated as a cut (default 0.5).
    - `--margin`       : seconds kept before/after each speech segment (default 0.5).
    - `--segments-dir` : folder for the individual segments (default: <output>_segments).
    - `--no-segments`  : do not save individual segments, only the cut video.

Requirements: imageio-ffmpeg (provides the ffmpeg binary).
"""

import os
import sys

# Self-heal: `python3 cut_silences.py` (the habit) OR `./cut_silences.py` — neither
# uses this script's own venv, so re-exec into it when the imports are missing.
# Guarded by __name__ so that IMPORTING this module (cut_timeline.py does) never
# restarts the importing program.
_VENV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "venv_audio")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (__name__ == "__main__" and os.environ.get("_WK_REEXEC") != "1"
        and os.path.exists(_VENV_PY)
        and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR)):
    os.environ["_WK_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import re
import shutil
import subprocess
import tempfile

import imageio_ffmpeg
import numpy as np

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def frame_levels_db(path: str, win: float = 0.05):
    """Per-frame levels in dBFS (win seconds each) — the raw material for gating."""
    import wave as _wave
    try:
        with _wave.open(path, "rb") as w:
            sr = w.getframerate()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except Exception:
        return np.zeros(0), 48000
    n = max(1, int(win * sr))
    usable = len(data) // n
    if usable < 1:
        return np.zeros(0), sr
    frames = data[:usable * n].astype(np.float32).reshape(usable, n)
    rms = np.sqrt((frames ** 2).mean(axis=1)) / 32768.0
    return 20.0 * np.log10(np.maximum(rms, 1e-6)), sr


def speech_bounds(path: str, floor_db: float | None = None, offset_db: float = 20.0,
                  sustain: float = 0.3, win: float = 0.05) -> tuple:
    """(first_speech, last_speech) in seconds — where the REAL speech starts and ends.

    The head of a recording is often not "silent": the room, the handling and the
    mic self-noise sit at -40/-45 dB, which is ABOVE any sensible silence
    threshold, so the detector sees sound and the dead air stays in front of the
    first segment (the user's complaint: *"au tout début, avant de parler, y'a un
    blanc qui est resté"*). A speech presence gate fixes it: a frame counts as
    speech only when it is `offset_db` dB above the noise floor AND stays there for
    `sustain` seconds.

    Returns (None, None) when nothing qualifies.
    """
    levels, sr = frame_levels_db(path, win)
    if levels.size == 0:
        return None, None
    floor = noise_floor_db(path, win) if floor_db is None else floor_db
    loud = levels > (floor + offset_db)
    need = max(1, int(round(sustain / win)))
    # first run of `need` consecutive loud frames
    run = 0
    first = None
    for i, ok in enumerate(loud):
        run = run + 1 if ok else 0
        if run >= need:
            first = (i - need + 1) * win
            break
    if first is None:
        return None, None
    run = 0
    last = None
    for i in range(len(loud) - 1, -1, -1):
        run = run + 1 if loud[i] else 0
        if run >= need:
            last = (i + need) * win
            break
    return first, last


def noise_floor_db(path: str, win: float = 0.05) -> float:
    """The recording's OWN noise floor: 5th percentile of the 50 ms frame levels (dBFS).

    Speech occupies only part of the frames, so the low percentile lands in the quiet
    stretches — that IS the noise floor. Used to pick a silence threshold that works
    on a hissy recording, where a fixed -35 dB finds no silence at all.
    """
    import wave as _wave
    try:
        with _wave.open(path, "rb") as w:
            sr = w.getframerate()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except Exception:
        return -60.0
    n = max(1, int(win * sr))
    usable = len(data) // n
    if usable < 4:
        return -60.0
    frames = data[:usable * n].astype(np.float32).reshape(usable, n)
    rms = np.sqrt((frames ** 2).mean(axis=1)) / 32768.0
    level = 20.0 * np.log10(np.maximum(rms, 1e-6))          # floor at -120 dB
    return max(float(np.percentile(level, 5)), -70.0)


def auto_threshold_db(path: str, offset_db: float = 12.0, win: float = 0.05) -> float:
    """Silence threshold derived from the file itself.

    Two families of recordings need different answers, so measure the contrast:
      * a clear separation between the pauses and the speech (p85 - p10 >= 15 dB)
        -> put the threshold at the MIDPOINT of the two populations;
      * no clear separation (continuous speech, or a very noisy room)
        -> fall back to the noise floor + `offset_db`.
    Both are clamped into [-60, -20] dB. On the user's own file this moved the
    threshold from -36 dB (which left one 90 s "segment") to -29 dB (14 segments).
    """
    levels, _ = frame_levels_db(path, win)
    if levels.size < 4:
        return -35.0
    floor = max(float(np.percentile(levels, 5)), -70.0)
    p10 = float(np.percentile(levels, 10))
    p85 = float(np.percentile(levels, 85))
    thr = (p10 + p85) / 2.0 if (p85 - p10) >= 15.0 else floor + offset_db
    return float(min(max(thr, -60.0), -20.0))


def run_ffmpeg(args: list) -> subprocess.CompletedProcess:
    """Run ffmpeg, raising on failure."""
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-2000:]}")
    return proc


def video_duration(path: str) -> float:
    """Return the video duration in seconds (parsed from ffmpeg)."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, text=True)
    line = [l for l in proc.stderr.splitlines() if "Duration" in l][0]
    hms = line.split("Duration:")[1].split(",")[0].strip()
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def video_fps(path: str) -> float:
    """Return the source frame rate (falls back to 30.0)."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, text=True)
    for line in proc.stderr.splitlines():
        if "Video" in line:
            m = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
            if m:
                return float(m.group(1))
    return 30.0


def detect_silences(path: str, noise_db: float, min_silence: float) -> list:
    """
    Return a list of (start, end) silence intervals (seconds), using ffmpeg's
    silencedetect filter. Only the audio track is decoded (`-vn`).
    """
    proc = run_ffmpeg([
        FFMPEG, "-i", path, "-vn",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ])

    silences = []
    start = None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            start = float(m.group(1))
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    return silences


def compute_keep_intervals(silences: list, duration: float,
                           margin: float, min_speech: float = 0.1) -> list:
    """
    Invert silence intervals into speech intervals, pad each by `margin`,
    and merge any that overlap after padding.
    """
    # speech = the gaps between silences
    keeps = []
    prev_end = 0.0
    for s, e in silences:
        if s - prev_end > min_speech:
            keeps.append((prev_end, s))
        prev_end = e
    if duration - prev_end > min_speech:
        keeps.append((prev_end, duration))

    # pad by margin (clamped to [0, duration])
    padded = [(max(0.0, a - margin), min(duration, b + margin)) for a, b in keeps]

    # merge overlaps
    merged = []
    for a, b in padded:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def extract_segment(input_path: str, output_path: str, start: float,
                    end: float, fps: int, crf: int) -> None:
    """Re-encode one [start, end] interval as a standalone mp4."""
    run_ffmpeg([
        FFMPEG, "-y", "-ss", str(start), "-to", str(end), "-i", input_path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ])


def cut_silences(input_path: str, output_path: str,
                 noise_db: float = -35.0, min_silence: float = 0.5,
                 margin: float = 0.5, crf: int = 18,
                 segments_dir: str | None = None) -> str:
    """Remove silent sections. Returns the output path."""
    duration = video_duration(input_path)
    fps = round(video_fps(input_path))
    silences = detect_silences(input_path, noise_db, min_silence)
    keeps = compute_keep_intervals(silences, duration, margin)

    removed = sum(e - s for s, e in silences)
    print(f"Detected {len(silences)} silent section(s) "
          f"({removed:.1f}s total), keeping {len(keeps)} segment(s).")

    # Nothing meaningful to cut -> just copy
    full_span = (len(keeps) == 1 and
                 abs(keeps[0][0]) < 1e-6 and
                 abs(keeps[0][1] - duration) < 1e-6)
    if full_span:
        print("No silence to remove; copying input unchanged.")
        run_ffmpeg([FFMPEG, "-y", "-i", input_path, "-c", "copy", output_path])
        if segments_dir:
            os.makedirs(segments_dir, exist_ok=True)
            shutil.copy2(input_path, os.path.join(segments_dir, "segment_000.mp4"))
        return output_path

    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        # 1) extract each kept segment
        seg_files = []
        for i, (a, b) in enumerate(keeps):
            seg = os.path.join(tmp, f"seg_{i:03d}.mp4")
            extract_segment(input_path, seg, a, b, fps, crf)
            seg_files.append(seg)

        # 2) concatenate (lossless copy)
        listfile = os.path.join(tmp, "list.txt")
        with open(listfile, "w") as f:
            for seg in seg_files:
                f.write(f"file '{seg}'\n")
        run_ffmpeg([
            FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
            "-c", "copy", output_path,
        ])

        # 3) save individual segments (optional)
        if segments_dir:
            os.makedirs(segments_dir, exist_ok=True)
            manifest = os.path.join(segments_dir, "segments.txt")
            with open(manifest, "w") as m:
                m.write("# index  file  start(s)  end(s)  duration(s)\n")
                for i, (seg, (a, b)) in enumerate(zip(seg_files, keeps)):
                    dest = os.path.join(segments_dir, f"segment_{i:03d}.mp4")
                    shutil.copy2(seg, dest)
                    m.write(f"{i}  segment_{i:03d}.mp4  {a:.3f}  {b:.3f}  {b - a:.3f}\n")
                    print(f"  saved {dest}  ({b - a:.2f}s)")

    return output_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Cut silent/dead-air sections from a video.")
    ap.add_argument("input", help="Input video")
    ap.add_argument("-o", "--output", help="Output video path (default: <input>_cut.mp4)")
    # --noise      : SILENCE THRESHOLD in dB — anything quieter is considered dead
    #                air and cut. -35 is a good default for speech; lower it (-45)
    #                on a hissy recording where silences are missed, raise it (-25)
    #                if quiet breathing is being kept.
    # --min-silence: a pause must last at least this long (seconds) to be cut.
    #                0.5 default; 0.15 cuts even short gaps, 0.8 keeps more air.
    # --margin     : how much is kept before/after each speech burst, in seconds,
    #                so the cuts are not abrupt (0.5 default).
    # --crf        : H.264 quality of the re-encoded segments (18 = visually lossless).
    # --segments-dir: where the individual segments are saved (default <output>_segments).
    # --no-segments: do not save individual segments, only the concatenated cut video.
    ap.add_argument("--noise", type=float, default=-35.0,
                    help="Silence threshold in dB: quieter than this is cut (default -35)")
    ap.add_argument("--min-silence", type=float, default=0.5,
                    help="Minimum silence duration in seconds to cut (default 0.5)")
    ap.add_argument("--margin", type=float, default=0.5,
                    help="Seconds kept before/after each speech segment (default 0.5)")
    ap.add_argument("--crf", type=int, default=18, help="x264 quality (lower = better, default 18)")
    ap.add_argument("--segments-dir", default=None,
                    help="Folder for individual segments (default: <output>_segments)")
    ap.add_argument("--no-segments", action="store_true",
                    help="Do not save individual segments, only the cut video")
    args = ap.parse_args()

    out = args.output or (os.path.splitext(args.input)[0] + "_cut.mp4")
    seg_dir = None
    if not args.no_segments:
        seg_dir = args.segments_dir or (os.path.splitext(out)[0] + "_segments")

    cut_silences(args.input, out, args.noise, args.min_silence,
                 args.margin, args.crf, seg_dir)
    print(f"Done -> {out}")
    if seg_dir:
        print(f"Segments -> {seg_dir}")


if __name__ == "__main__":
    main()
