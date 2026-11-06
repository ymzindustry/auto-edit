#!/home/ymz/.hermes/profiles/videoeditor/venv_audio/bin/python
"""
speech_master.py — Cut the SILENT parts out of the master audio FIRST, then
enhance only what is left (the speech).

WHY THIS EXISTS
    Enhancing is the expensive step (DeepFilterNet + EQ + compressor + loudnorm)
    and on a real recording most of the timeline is silence and dead air. Cleaning
    the whole file wastes that work, and the silence also drags the loudness
    measurement down. So the order is inverted:

      1. the silences are detected on the RAW recording (before any processing) —
         this is also the most honest measurement, the enhancer cannot smear it;
      2. the speech intervals are concatenated into a "speech-only" master;
      3. THAT shorter file is what gets enhanced;
      4. a map is written saying where every interval now lives:

         {"audio": "speech_enhanced.wav", "sr": 48000,
          "intervals": [{"master_start": 1.20, "master_end": 6.11, "audio_start": 0.0},
                        {"master_start": 6.50, "master_end": 9.01, "audio_start": 4.91}],
          "kept": 12.41, "removed": 17.59, "threshold_db": -35.0}

    cut_timeline.py reads that map: the intervals ARE the cut, and each segment
    takes its audio at the mapped position — so the enhanced audio stays perfectly
    in sync with the videos even though it no longer shares their timeline.

THE THRESHOLD (--noise)
    'auto' (default) measures the recording's own noise floor (5th percentile of
    the 50 ms frame levels) and puts the threshold 6 dB above it, clamped to
    [-60, -20] dB. A fixed number ('-35') is used as-is when you want to force it.
    The value actually used is printed and stored in the map.

WHAT YOU GAIN
    Only the speech is enhanced (on a talking-head recording that is usually
    30-60 % of the file) — and everything downstream (segments, transcription)
    then only deals with speech.

Usage:
    ./speech_master.py master.wav -o speech_enhanced.wav --map map.json
    ./speech_master.py master.wav -o speech.wav --map map.json --no-enhance
    ./speech_master.py master.wav -o s.wav --map map.json --noise -40 --margin 0.4
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
import json
import re
import subprocess
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cut_silences import (auto_threshold_db, compute_keep_intervals,  # noqa: E402
                          detect_silences, frame_levels_db, noise_floor_db,
                          speech_bounds)
from sync_replace_audio import to_mono_wav  # noqa: E402

PROFILE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
VENV_AUDIO = os.environ.get("VENV_AUDIO", os.path.join(PROFILE, "venv_audio", "bin", "python"))


def probe_duration(path: str) -> float:
    """Length of a media file in seconds, via ffmpeg's Duration line."""
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    out = subprocess.run([ff, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def read_wav(path: str):
    """(samples int16 as mono, sample rate) — the file must be mono PCM."""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        if w.getnchannels() != 1:
            raise SystemExit(f"{path} is not mono ({w.getnchannels()} channels)")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data, sr


def write_wav(path: str, samples: np.ndarray, sr: int) -> None:
    """Write mono 16-bit PCM — exact, sample for sample."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.ascontiguousarray(samples, dtype=np.int16).tobytes())


def fix_length(path: str, expected: float) -> float:
    """Force the file to be exactly `expected` seconds (pad with silence / trim).

    The enhancement must not change the length, otherwise every interval after the
    first would be out of sync by the drift. If it ever does (by a few ms), this
    pads/trims the tail so the map stays exact. Returns the correction in seconds.
    """
    import imageio_ffmpeg
    actual = probe_duration(path)
    diff = actual - expected
    if abs(diff) < 0.005:
        return 0.0
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    out_dir = os.path.dirname(os.path.abspath(path)) or "."
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        fixed = os.path.join(tmp, "fixed.wav")
        r = subprocess.run([ff, "-y", "-i", path, "-af", "apad",
                            "-t", f"{expected:.6f}", "-c:a", "pcm_s16le", fixed],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("Could not pad/trim the enhanced audio:\n" + r.stderr[-1500:])
        os.replace(fixed, path)
    return diff


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Remove the silent parts of a master audio, enhance only the speech.")
    ap.add_argument("input", help="Master audio (the RAW recording: .wav, or a video)")
    ap.add_argument("-o", "--output", required=True,
                    help="Enhanced SPEECH-ONLY wav to write")
    ap.add_argument("--map", required=True,
                    help="JSON map to write (intervals + where their audio now lives)")
    ap.add_argument("--sr", type=int, default=48000, help="Sample rate (default 48000)")
    # --noise : silence threshold. 'auto' (default) = the recording's own noise floor
    #           + 6 dB — robust on a hissy file, where a fixed -35 dB would find no
    #           silence at all. Pass a number to force it (e.g. -35).
    ap.add_argument("--noise", default="auto",
                    help="Silence threshold in dB, or 'auto' (default) to derive it "
                         "from the recording's own noise floor")
    ap.add_argument("--noise-offset", type=float, default=12.0,
                    help="Only used when --noise auto cannot separate the pauses from "
                         "the speech: fall back to noise floor + this many dB (default 12)")
    ap.add_argument("--min-silence", type=float, default=0.3,
                    help="Shortest pause (s) treated as a cut (default 0.3)")
    ap.add_argument("--margin", type=float, default=0.3,
                    help="Seconds of sound kept before/after each burst, so the cuts "
                         "are not abrupt (default 0.3)")
    ap.add_argument("--min-speech", type=float, default=0.1,
                    help="Bursts shorter than this (s) are ignored (default 0.1)")
    # --no-trim-edges : keep the dead air before the first speech / after the last one.
    #           By default it is DROPPED, because that dead air is usually not
    #           "silent" (room + handling + mic noise at -40/-45 dB) and would
    #           otherwise sit as a blank in front of the first segment.
    # --trim-offset / --trim-sustain : what counts as real speech — a frame must be
    #           this many dB above the noise floor (20) and stay there this long (0.3 s).
    ap.add_argument("--no-trim-edges", dest="trim_edges", action="store_false",
                    help="Keep the dead air before the first / after the last speech "
                         "(default: dropped, even when it is not silent)")
    ap.add_argument("--trim-offset", type=float, default=20.0,
                    help="A frame counts as speech when it is this many dB above the "
                         "noise floor (default 20)")
    ap.add_argument("--tail-pad", type=float, default=0.6,
                    help="Extra sound kept AFTER the last speech (default 0.6). The "
                         "sustain test below can end the last word early — its vowel "
                         "decays below the floor — and the end of the sentence then "
                         "sounds cut; this keeps the natural decay")
    ap.add_argument("--trim-sustain", type=float, default=0.3,
                    help="...and stays there this long, in seconds (default 0.3)")
    # --no-enhance : do not run DeepFilterNet/EQ/loudnorm, just keep the speech-only
    #                audio as it is (fast; used by --skip-enhance and by the tests).
    ap.add_argument("--no-enhance", action="store_true",
                    help="Do not enhance: keep the speech-only audio as it is")
    ap.add_argument("--lufs", type=float, default=-16, help="Mastering loudness (default -16)")
    ap.add_argument("--chunk-sec", type=float, default=30.0,
                    help="DeepFilterNet chunk length in seconds (default 30)")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        raise SystemExit(f"Not found: {args.input}")
    out = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    with tempfile.TemporaryDirectory(dir=PROFILE) as tmp:
        # ---- 1. raw mono 48 kHz (the reference the silences are measured on)
        raw = os.path.join(tmp, "raw.wav")
        to_mono_wav(args.input, raw, args.sr)
        samples, sr = read_wav(raw)
        duration = len(samples) / float(sr)

        # ---- 2. threshold + silence detection on the RAW audio
        if str(args.noise).strip().lower() == "auto":
            threshold = auto_threshold_db(raw, args.noise_offset)
            lev, _ = frame_levels_db(raw)
            p10, p85 = np.percentile(lev, 10), np.percentile(lev, 85) if lev.size else (0, 0)
            if p85 - p10 >= 15.0:
                how = (f"auto: midpoint between the pauses ({p10:.1f} dB) and the "
                       f"speech ({p85:.1f} dB)")
            else:
                how = f"auto: noise floor ({noise_floor_db(raw):.1f} dB) + {args.noise_offset:g} dB"
        else:
            threshold = float(args.noise)
            how = "given"
        silences = detect_silences(raw, threshold, args.min_silence)
        keeps = compute_keep_intervals(silences, duration, args.margin, args.min_speech)
        kept_total = sum(b - a for a, b in keeps)

        print(f"master      : {os.path.basename(args.input)}  {duration:.2f}s")
        print(f"threshold   : {threshold:.1f} dB ({how})")
        print(f"silence     : {sum(e - s for s, e in silences):.2f}s in "
              f"{len(silences)} pause(s) -> {len(keeps)} speech interval(s)")

        # ---- head/tail trim: everything before the first REAL speech and after the
        # last one is dead air, even when it is not "silent" — the room, the handling
        # and the mic self-noise sit ABOVE any usable silence threshold, which is why
        # a blank used to stay in front of segment 1.
        if args.trim_edges and keeps:
            floor = noise_floor_db(raw)
            first, last = speech_bounds(raw, floor, args.trim_offset, args.trim_sustain)
            if first is None:
                print("trim edges  : no clear speech found, nothing trimmed")
            else:
                head = first - args.margin
                # the TAIL gets a wider pad: a final vowel decays below the floor and
                # `last` stops before the end of the word, which cut the last syllable
                tail = min(duration, last + max(args.margin, args.tail_pad))
                before = sum(min(b, head) - a for a, b in keeps if a < head)
                after = sum(b - max(a, tail) for a, b in keeps if b > tail)
                trimmed = [(max(a, head), min(b, tail))
                           for a, b in keeps if b > head and a < tail]
                if trimmed:
                    keeps = trimmed
                    kept_total = sum(b - a for a, b in keeps)
                    print(f"trim edges  : speech from {first:.2f}s to {last:.2f}s -> "
                          f"{before:.2f}s of dead air dropped at the head and "
                          f"{after:.2f}s at the tail")

        if len(keeps) < 1 or kept_total < 1.0:
            # nothing convincing: keep everything rather than hand back an empty file
            print("! no usable speech interval found -> keeping the WHOLE recording")
            keeps = [(0.0, duration)]
            kept_total = duration

        # ---- 3. concatenate the speech intervals (sample-exact)
        pieces, intervals, cursor = [], [], 0
        for a, b in keeps:
            i0, i1 = int(round(a * sr)), int(round(b * sr))
            i0, i1 = max(0, i0), min(len(samples), i1)
            if i1 <= i0:
                continue
            pieces.append(samples[i0:i1])
            intervals.append({"master_start": round(i0 / sr, 4),
                              "master_end": round(i1 / sr, 4),
                              "audio_start": round(cursor / sr, 4)})
            cursor += i1 - i0
        speech = np.concatenate(pieces) if pieces else samples
        speech_len = len(speech) / float(sr)
        speech_wav = os.path.join(tmp, "speech.wav")
        write_wav(speech_wav, speech, sr)
        saved = duration - speech_len
        print(f"speech-only : {speech_len:.2f}s kept, {saved:.2f}s of silence removed"
              f" ({100.0 * saved / duration:.0f}% less audio to enhance)")

        # ---- 4. enhance only that
        if args.no_enhance:
            os.replace(speech_wav, out)
            print("enhancement : skipped (--no-enhance), speech-only audio kept as is")
        else:
            print(f"enhancing   : {speech_len:.2f}s (instead of {duration:.2f}s) ...")
            r = subprocess.run([VENV_AUDIO, os.path.join(SCRIPTS, "enhance_audio.py"),
                                speech_wav, "-o", out, "--sr", str(args.sr),
                                "--lufs", str(args.lufs), "--chunk-sec", str(args.chunk_sec)],
                               check=True)
            if r.returncode != 0:
                raise SystemExit("enhance_audio.py failed")
            drift = fix_length(out, speech_len)
            if drift:
                print(f"! the enhancer changed the length by {drift:+.3f}s "
                      f"-> tail padded/trimmed so the map stays exact")

        # ---- 5. the map: where each interval's audio now lives
        report = {"audio": out, "sr": sr, "intervals": intervals,
                  "kept": round(speech_len, 4), "removed": round(saved, 4),
                  "threshold_db": round(threshold, 2), "silences": len(silences)}
        with open(args.map, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"map         : {args.map}")


if __name__ == "__main__":
    main()
