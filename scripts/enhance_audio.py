#!/home/ymz/.hermes/profiles/videoeditor/venv_audio/bin/python
"""
enhance_audio.py — Studio mastering for a voice/speech recording.

Pipeline:

  1. Mono conversion        -> 48 kHz, 16-bit (clean, single channel).
  2. DeepFilterNet3         -> neural noise removal + speech enhancement.
  3. Studio vocal EQ        -> high-pass rumble, warmth, cut mud, presence, air.
  4. De-esser               -> tame harsh "s"/"t" sibilance.
  5. Gentle compressor      -> even out the level ("adapted" consistent volume).
  6. Safety limiter         -> no clipping on peaks.
  7. Loudness normalization -> two-pass to the target LUFS (default -16, YouTube).

Usage:
    ./enhance_audio.py input.wav -o output.wav
    ./enhance_audio.py input.wav --lufs -14        # louder, for music-heavy edits
    ./enhance_audio.py input.wav --lufs -16        # speech/podcast (default)

Also called by workflow.py as:
    <venv_audio>/python enhance_audio.py <input> -o <output> --sr <rate>

Requirements (venv_audio): torch==2.0.1, torchaudio==2.0.2, deepfilternet==0.5.6,
numpy==1.26.4, scipy, imageio-ffmpeg.
"""

import argparse
import json
import os
import re
import subprocess
import tempfile

import imageio_ffmpeg
import torch
from df import enhance, init_df
from df.io import load_audio, save_audio

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# Studio vocal EQ: rumble cut -> warmth -> mud cut -> presence -> air.
EQ_CHAIN = (
    "highpass=f=80,"
    "lowshelf=f=140:g=1.5,"
    "equalizer=f=250:t=q:w=1.5:g=-3,"
    "equalizer=f=3500:t=q:w=0.8:g=3,"
    "highshelf=f=10000:g=2.5"
)

# Dynamics: de-esser + gentle compressor (glue) + safety limiter.
DYNAMICS = (
    "deesser=i=1:m=0.6:s=1,"
    "acompressor=threshold=0.125:ratio=3:attack=10:release=200:makeup=1.5,"
    "alimiter=limit=0.95"
)


def run_ffmpeg(args: list) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-2000:]}")


def to_mono_wav(input_path: str, output_path: str, sr: int = 48000) -> None:
    """Normalize the input to mono 48 kHz 16-bit WAV."""
    run_ffmpeg([
        FFMPEG, "-y", "-i", input_path,
        "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", output_path,
    ])


def denoise_enhance(input_path: str, output_path: str, sr: int = 48000,
                    chunk_sec: float = 30.0, overlap_sec: float = 1.0) -> None:
    """DeepFilterNet3 in overlapping chunks.

    df.enhance() builds the STFT/features for the WHOLE file at once, so a long
    recording (e.g. 9 min) needs several GB of RAM and gets OOM-killed. Here we
    denoise chunk by chunk and overlap-add with a cross-fade, which keeps memory
    flat and makes the chunk boundaries inaudible.
    """
    model, df_state, _ = init_df(post_filter=True, log_level="ERROR", log_file=None)
    audio, _ = load_audio(input_path, sr=sr)
    if audio.dim() == 1:
        audio = audio.unsqueeze(0)

    total = audio.shape[-1]
    chunk = int(chunk_sec * sr)
    overlap = int(overlap_sec * sr)

    if total <= chunk:
        enhanced = enhance(model, df_state, audio)
    else:
        step = max(1, chunk - overlap)
        enhanced = torch.zeros_like(audio)
        weight = torch.zeros_like(audio)
        pos = 0
        while pos < total:
            end = min(pos + chunk, total)
            seg = audio[..., pos:end]
            if seg.shape[-1] < sr // 2:                 # <0.5 s tail: keep as-is
                piece = seg
            else:
                piece = enhance(model, df_state, seg)
            n = piece.shape[-1]
            w = torch.ones(n, dtype=piece.dtype, device=piece.device)
            if pos > 0:                                  # fade in over the overlap
                f = min(overlap, n)
                w[:f] = torch.linspace(0.0, 1.0, f)
            if end < total:                              # fade out over the overlap
                f = min(overlap, n)
                w[n - f:] *= torch.linspace(1.0, 0.0, f)
            enhanced[..., pos:pos + n] += piece * w
            weight[..., pos:pos + n] += w
            if end >= total:
                break
            pos += step
        enhanced = enhanced / weight.clamp(min=1e-6)

    save_audio(output_path, enhanced, sr=sr)


def _sanitize(value: str, default: str) -> str:
    """Guard loudnorm measured values against -inf/inf/nan (e.g. a silent input)."""
    try:
        f = float(value)
        if f in (float("-inf"), float("inf")) or f != f:  # also catches NaN
            return default
    except (TypeError, ValueError):
        return default
    return value


def apply_master(input_path: str, output_path: str, sr: int, lufs: float) -> None:
    """EQ -> de-esser -> compressor -> limiter -> two-pass loudness normalization."""
    base = f"{EQ_CHAIN},{DYNAMICS}"

    # Pass 1: measure the input's loudness (JSON printed to stderr).
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-y", "-i", input_path,
         "-af", f"{base},loudnorm=I={lufs}:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    data = {}
    m = re.search(r"\{.*\}", proc.stderr, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = {}

    final_chain = (
        f"{base},loudnorm=I={lufs}:TP=-1.5:LRA=11:"
        f"measured_I={_sanitize(data.get('input_i', '-23'), '-23')}:"
        f"measured_TP={_sanitize(data.get('input_tp', '-1.5'), '-1.5')}:"
        f"measured_LRA={_sanitize(data.get('input_lra', '11'), '11')}:"
        f"measured_thresh={_sanitize(data.get('input_thresh', '-30'), '-30')}:linear=true"
    )

    # Pass 2: apply with the measured values (linear = clean single gain).
    run_ffmpeg([
        FFMPEG, "-y", "-i", input_path, "-af", final_chain,
        "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", output_path,
    ])


def enhance_audio(input_path: str, output_path: str, sr: int = 48000, lufs: float = -16,
                  chunk_sec: float = 30.0) -> str:
    """Full enhancement: denoise + enhance (DeepFilterNet3) then studio master. Returns output path."""
    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        mono = os.path.join(tmp, "mono.wav")
        denoised = os.path.join(tmp, "denoised.wav")
        to_mono_wav(input_path, mono, sr)
        denoise_enhance(mono, denoised, sr, chunk_sec=chunk_sec)
        apply_master(denoised, output_path, sr, lufs)
    return output_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Studio-master a voice recording.")
    ap.add_argument("input", help="Input audio file (wav / mp3 / m4a / ...)")
    ap.add_argument("-o", "--output", help="Output WAV path (default: <input>_enhanced.wav)")
    # --sr       : output sample rate. 48000 is the video standard; 44100 only if
    #              the whole project is 44.1 kHz.
    # --lufs     : loudness target of the mastered voice. -16 = speech/YouTube
    #              (default), -14 = louder, -20 = quieter and more dynamic.
    # --chunk-sec: DeepFilterNet is fed chunks of this length, in seconds. SMALLER =
    #              less RAM (this host OOM-kills above ~1 GB), 30 s is safe; raise it
    #              only on a machine with more memory (it is also a bit faster).
    ap.add_argument("--sr", type=int, default=48000,
                    help="Output sample rate (default 48000; 44100 for a 44.1 kHz project)")
    ap.add_argument("--lufs", type=float, default=-16,
                    help="Target loudness in LUFS (default -16; -14 = louder)")
    ap.add_argument("--chunk-sec", type=float, default=30.0,
                    help="DeepFilterNet chunk length, seconds (lower = less RAM, default 30)")
    args = ap.parse_args()

    out = args.output or (os.path.splitext(args.input)[0] + "_enhanced.wav")
    enhance_audio(args.input, out, args.sr, args.lufs, args.chunk_sec)
    print(f"Enhanced audio written to: {out}")


if __name__ == "__main__":
    main()
