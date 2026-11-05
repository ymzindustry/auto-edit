#!/home/ymz/.hermes/profiles/videoeditor/venv_audio/bin/python
"""
enhance_variants.py — produce SEVERAL voice-enhancement variants of a short excerpt,
so the sound can be chosen by ear instead of by argument.

    python3 scripts/enhance_variants.py video-projects/shoulder/audio.wav
    python3 scripts/enhance_variants.py in.wav --start 0 --duration 15 --out raw_data/enh_tests

What it does, per run:
  1. cuts [start, start+duration) and normalises it to mono 48 kHz (the reference);
  2. runs DeepFilterNet3 ONCE (post-filter on) and ONCE (post-filter off) — the AI stage
     is shared by every variant, so it is paid once, not once per variant;
  3. applies a different POST chain to each variant (EQ / de-esser / compressor / air);
  4. normalises EVERY variant to the same loudness (-16 LUFS, -1.5 dBTP, two-pass) so
     the comparison is about timbre and dynamics, never about who is louder;
  5. prints LUFS, true peak, LRA and size for each, and writes an index.txt.

Nothing here is a deliverable: it exists to pick a sound. The chosen chain then goes
into enhance_audio.py so the whole pipeline uses it.

NOTE: the AI stage loads the file in one piece, so keep the excerpt short (<= 60 s).
For a long file use enhance_audio.py, which chunks it.
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = HERE
PROFILE = os.path.dirname(SCRIPTS)

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 48000


# ──────────────────────────────────────────────────────────────────────────────
#  ffmpeg helpers
# ──────────────────────────────────────────────────────────────────────────────

def run(cmd: list) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed:\n  " + " ".join(cmd) + "\n" + r.stderr[-1500:])
    return r.stderr


def cut_mono(src: str, dst: str, start: float, duration: float) -> None:
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{start:g}", "-t", f"{duration:g}",
         "-i", src, "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", dst])


def measure_loudness(path: str) -> dict:
    """Integrated loudness / true peak / LRA of a file (loudnorm pass 1)."""
    err = run([FFMPEG, "-hide_banner", "-i", path, "-af",
               "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        return {}
    import json
    return json.loads(m.group(0))


def loudnorm2(src: str, dst: str, i: float = -16.0, tp: float = -1.5, lra: float = 11.0) -> None:
    """Two-pass loudness normalisation: measure, then apply the measured values."""
    m = measure_loudness(src)
    af = f"loudnorm=I={i}:TP={tp}:LRA={lra}"
    if m and m.get("input_i") not in ("-inf", "inf", None):
        af += (f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
               f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
               f":offset={m.get('target_offset', 0)}:linear=true")
    run([FFMPEG, "-y", "-v", "error", "-i", src, "-af", af,
         "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", dst])


def post(src: str, dst: str, chain: str, i: float = -16.0) -> None:
    """Apply a filter chain, then normalise to `i` LUFS."""
    tmp = dst + ".pre.wav"
    run([FFMPEG, "-y", "-v", "error", "-i", src, "-af", chain,
         "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", tmp])
    loudnorm2(tmp, dst, i=i)
    os.remove(tmp)


# ──────────────────────────────────────────────────────────────────────────────
#  DeepFilterNet3
# ──────────────────────────────────────────────────────────────────────────────

def denoise(src: str, dst: str, post_filter: bool = True) -> None:
    import torch
    from df import enhance, init_df
    from df.io import load_audio, save_audio

    model, state, _ = init_df(post_filter=post_filter, log_level="ERROR", log_file=None)
    audio, _ = load_audio(src, sr=SR)
    if audio.dim() == 1:
        audio = audio.unsqueeze(0)
    with torch.no_grad():
        out = enhance(model, state, audio)
    save_audio(dst, out, sr=SR)


# ──────────────────────────────────────────────────────────────────────────────
#  the chains — one sound each
# ──────────────────────────────────────────────────────────────────────────────

# The chain currently used by the pipeline (scripts/enhance_audio.py): kept identical
# on purpose, it is the reference point.
EQ_PIPELINE = ("highpass=f=80,"
               "lowshelf=f=140:g=1.5,"
               "equalizer=f=250:t=q:w=1.5:g=-3,"
               "equalizer=f=3500:t=q:w=0.8:g=3,"
               "highshelf=f=10000:g=2.5")
DYN_PIPELINE = ("deesser=i=1:m=0.6:s=1,"
                "acompressor=threshold=0.125:ratio=3:attack=10:release=200:makeup=1.5,"
                "alimiter=limit=0.95")

# name, source, (chain, description)  — source: "on" = DeepFilterNet post-filter ON,
# "off" = DeepFilterNet post-filter OFF (less gating, more natural), "raw" = no AI.
VARIANTS = [
    ("02_pipeline_actuel", "on",
     (f"{EQ_PIPELINE},{DYN_PIPELINE}",
      "the chain the pipeline uses today: DeepFilterNet + EQ + de-esser + 3:1 + limiter")),
    ("03_ia_seule", "on", ("anull",
      "DeepFilterNet only: the AI output as it comes, no EQ, no compression")),
    ("04_naturel", "on",
     ("highpass=f=70,equalizer=f=3000:t=q:w=1.2:g=2,alimiter=limit=0.97",
      "natural: gentle presence, NO compressor — keeps the dynamics of the take")),
    ("05_studio", "on",
     ("highpass=f=80,lowshelf=f=150:g=2,equalizer=f=320:t=q:w=1.4:g=-2.5,"
      "equalizer=f=3800:t=q:w=0.9:g=3.5,highshelf=f=11000:g=2.5,"
      "deesser=i=1:m=0.7:s=1,"
      "acompressor=threshold=0.09:ratio=3:attack=6:release=180:makeup=1.2:knee=3,"
      "alimiter=limit=0.95",
      "studio/podcast: fuller lows, cleaned mud, strong presence, firmer 3:1 control")),
    ("06_chaud", "on",
     ("highpass=f=70,lowshelf=f=120:g=3,equalizer=f=400:t=q:w=1.5:g=-1.5,"
      "highshelf=f=9000:g=-1.5,"
      "acompressor=threshold=0.12:ratio=2:attack=15:release=250:makeup=1.3,"
      "alimiter=limit=0.95",
      "warm: boosted lows, softened highs, gentle 2:1 — intimate, no harshness")),
    ("07_clair", "on",
     ("highpass=f=90,equalizer=f=250:t=q:w=1.5:g=-2,equalizer=f=4200:t=q:w=0.8:g=4,"
      "highshelf=f=12000:g=3,deesser=i=1:m=0.8:s=1,"
      "acompressor=threshold=0.10:ratio=3:attack=8:release=150:makeup=1.2,"
      "alimiter=limit=0.95",
      "bright: mud cut, strong presence + air, harder de-esser — cuts through on a phone")),
    ("08_sans_ia", "raw",
     ("afftdn=nr=12:nf=-40:tn=1,highpass=f=80,equalizer=f=250:t=q:w=1.5:g=-2,"
      "equalizer=f=3500:t=q:w=0.8:g=3,"
      "acompressor=threshold=0.12:ratio=3:attack=10:release=200:makeup=1.3,"
      "alimiter=limit=0.95",
      "no DeepFilterNet: ffmpeg spectral denoiser + EQ + compressor (AI artefacts check)")),
    ("09_ia_sans_postfiltre", "off", ("anull",
      "DeepFilterNet with its post-filter OFF: less gating, keeps more room tone")),
]


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        prog="enhance_variants.py",
        description="Produce several voice-enhancement variants of a short excerpt, all "
                    "normalised to the same loudness, to pick a sound by ear.")
    ap.add_argument("input", help="source audio (wav/ogg/mp3/mp4...)")
    ap.add_argument("--start", type=float, default=0.0, help="start of the excerpt, s")
    ap.add_argument("--duration", type=float, default=15.0, help="length, s (default 15)")
    ap.add_argument("--out", default=None, help="output folder (default raw_data/enh_tests)")
    ap.add_argument("--lufs", type=float, default=-16.0,
                    help="loudness target shared by every variant (default -16)")
    args = ap.parse_args()

    src = os.path.abspath(args.input)
    if not os.path.isfile(src):
        raise SystemExit(f"not a file: {src}")
    if args.duration > 60:
        raise SystemExit("keep the excerpt under 60 s: the AI stage loads it in one piece")
    out = os.path.abspath(args.out or os.path.join(PROFILE, "raw_data", "enh_tests"))
    os.makedirs(out, exist_ok=True)

    raw = os.path.join(out, "_excerpt.wav")          # working copy, not a variant
    cut_mono(src, raw, args.start, args.duration)
    print(f"excerpt   : {args.start:g}s -> {args.start + args.duration:g}s  "
          f"({args.duration:g}s, mono {SR} Hz)")
    print(f"output    : {out}\n")

    # the AI stage, paid once
    df_on = os.path.join(out, "_df_postfilter.wav")
    df_off = os.path.join(out, "_df_nopost.wav")
    print("DeepFilterNet3 (post-filter on) ...", flush=True)
    denoise(raw, df_on, post_filter=True)
    print("DeepFilterNet3 (post-filter off) ...", flush=True)
    denoise(raw, df_off, post_filter=False)

    # 01 = the untouched excerpt, only loudness-matched (the A/B reference)
    loudnorm2(raw, os.path.join(out, "01_reference.wav"), i=args.lufs)

    index = ["variant                   LUFS     TP    LRA   size   chain"]
    sources = {"on": df_on, "off": df_off, "raw": raw}
    for name, source, (chain, desc) in VARIANTS:
        dst = os.path.join(out, name + ".wav")
        print(f"{name} ...", flush=True)
        src_variant = sources[source]
        if chain == "anull":                      # no post chain at all
            loudnorm2(src_variant, dst, i=args.lufs)
        else:
            post(src_variant, dst, chain, i=args.lufs)
        m = measure_loudness(dst)
        size = os.path.getsize(dst) / 1e6
        index.append(f"{name:<24} {float(m.get('input_i', 0)):6.1f}  {float(m.get('input_tp', 0)):5.1f}  "
                     f"{float(m.get('input_lra', 0)):4.1f}  {size:4.1f}M  {desc}")
        print(f"  {name}: {m.get('input_i')} LUFS, TP {m.get('input_tp')} dB, "
              f"LRA {m.get('input_lra')} LU, {size:.1f} MB")

    with open(os.path.join(out, "index.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(index) + "\n")
    print("\n" + "\n".join(index))
    print(f"\nDone -> {out}")


if __name__ == "__main__":
    main()
