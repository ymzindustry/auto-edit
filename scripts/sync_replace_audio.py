#!/home/ymz/.hermes/profiles/videoeditor/venv_audio/bin/python
"""
sync_replace_audio.py — Replace a video's audio with a separately-recorded
(higher quality) audio track, automatically synchronized via cross-correlation.

The video's own (lower-quality) audio is used ONLY as a timing reference: the
two recordings are cross-correlated to find the exact time offset, then the
good audio is placed at that offset on the video timeline. Where the good
audio does not cover the video (intro/outro gaps), the result is silent.

The original video stream is copied bit-for-bit (no re-encode, no quality loss).

Usage:
    python sync_replace_audio.py video.mp4 good_audio.wav -o final.mp4
    python sync_replace_audio.py video.mp4 good_audio.wav --offset 12.63   # skip auto-detect

Requirements (one venv): numpy==1.26.4, scipy, imageio-ffmpeg.
"""

import argparse
import os
import subprocess
import tempfile
import wave

import numpy as np
import imageio_ffmpeg
from scipy import signal

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 48000          # working sample rate for sync + output audio
CORR_SR = 16000     # downsampled rate for the cross-correlation (fast + precise)


def run_ffmpeg(args: list) -> None:
    """Run an ffmpeg command, raising on failure."""
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-2000:]}")


def extract_audio(video_path: str, out_path: str, sr: int = SR) -> None:
    """Extract a video's audio track as mono 48 kHz 16-bit WAV."""
    run_ffmpeg([
        FFMPEG, "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", out_path,
    ])


def to_mono_wav(input_path: str, output_path: str, sr: int = SR) -> None:
    """Convert any audio file to mono 48 kHz 16-bit WAV."""
    run_ffmpeg([
        FFMPEG, "-y", "-i", input_path,
        "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", output_path,
    ])


def read_wav(path: str):
    """Read a 16-bit PCM WAV into a float32 array in [-1, 1] and its sample rate."""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return sr, data.astype(np.float32) / 32768.0


def write_wav(path: str, samples: np.ndarray, sr: int = SR) -> None:
    """Write a float32 array in [-1, 1] as a mono 16-bit PCM WAV."""
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def video_duration(video_path: str) -> float:
    """Return the video duration in seconds (from ffmpeg's Duration line)."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", video_path],
                          capture_output=True, text=True)
    line = [l for l in proc.stderr.splitlines() if "Duration" in l][0]
    hms = line.split("Duration:")[1].split(",")[0].strip()
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def find_offset(reference_wav: str, audio_wav: str) -> tuple:
    """
    Cross-correlate two same-take recordings and return (offset_seconds, confidence).

    A positive offset means `audio_wav` starts that many seconds INTO the
    reference (the video's own audio). Confidence is the ratio of the best
    alignment to the next-best unrelated alignment (>3 is a solid match).
    """
    _, ref = read_wav(reference_wav)
    _, sig = read_wav(audio_wav)

    # downsample both to CORR_SR for speed
    ref = signal.resample_poly(ref, CORR_SR, SR)
    sig = signal.resample_poly(sig, CORR_SR, SR)

    # zero-mean, unit-energy normalization
    ref = ref - ref.mean()
    sig = sig - sig.mean()
    ref /= np.sqrt((ref ** 2).sum()) + 1e-12
    sig /= np.sqrt((sig ** 2).sum()) + 1e-12

    corr = signal.correlate(ref, sig, method="fft")
    lag_idx = int(np.argmax(corr))
    lag = lag_idx - (len(sig) - 1)          # samples at CORR_SR
    offset = lag / CORR_SR

    # confidence: best peak vs the best peak outside a small neighborhood
    peak = corr[lag_idx]
    half = CORR_SR // 8                     # +/- ~125 ms neighborhood
    lo, hi = max(0, lag_idx - half), min(len(corr), lag_idx + half)
    neighborhood = np.ones(len(corr), dtype=bool)
    neighborhood[lo:hi] = False
    second = corr[neighborhood].max() if neighborhood.any() else 0.0
    confidence = peak / max(second, 1e-9)

    return offset, confidence


def replace_audio(video_path: str, audio_path: str, output_path: str,
                  offset: float | None = None) -> str:
    """
    Replace the video's audio with `audio_path`, synced via cross-correlation.
    Returns the output path.
    """
    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        # reference = the video's own audio
        ref_wav = os.path.join(tmp, "reference.wav")
        extract_audio(video_path, ref_wav)

        # replacement audio -> mono 48 kHz
        mic_wav = os.path.join(tmp, "replacement.wav")
        to_mono_wav(audio_path, mic_wav)

        # find the offset (unless manually provided)
        if offset is None:
            offset, confidence = find_offset(ref_wav, mic_wav)
            print(f"Detected offset: {offset:.4f} s  (confidence x{confidence:.1f})")
        else:
            print(f"Using manual offset: {offset:.4f} s")

        # build the aligned track: silence(offset) + good audio + pad to video length
        _, mic = read_wav(mic_wav)
        dur = video_duration(video_path)
        total = int(dur * SR)
        off = int(round(offset * SR))

        aligned = np.zeros(total, dtype=np.float32)
        if off >= 0:
            # good audio starts `off` samples INTO the video
            if off < total:
                n = min(len(mic), total - off)
                aligned[off:off + n] = mic[:n]
            # else: offset beyond the video -> stays silent
        else:
            # good audio starts `-off` samples BEFORE the video -> skip its head
            start = -off
            if start < len(mic):
                n = min(len(mic) - start, total)
                aligned[:n] = mic[start:start + n]

        aligned_wav = os.path.join(tmp, "aligned.wav")
        write_wav(aligned_wav, aligned, SR)

        # mux: copy the original video stream + the new audio
        run_ffmpeg([
            FFMPEG, "-y", "-i", video_path, "-i", aligned_wav,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", output_path,
        ])

    return output_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync + replace a video's audio with a better track.")
    ap.add_argument("video", help="Input video (its audio is used as the timing reference)")
    ap.add_argument("audio", help="Good-quality audio to use as the replacement")
    ap.add_argument("-o", "--output", help="Output video path (default: <video>_synced.mp4)")
    ap.add_argument("--offset", type=float, default=None,
                    help="Manual offset in seconds (skip auto-detection)")
    args = ap.parse_args()

    out = args.output or (os.path.splitext(args.video)[0] + "_synced.mp4")
    replace_audio(args.video, args.audio, out, offset=args.offset)
    print(f"Final video written to: {out}")


if __name__ == "__main__":
    main()
