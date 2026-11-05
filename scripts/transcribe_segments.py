#!/home/ymz/.hermes/profiles/videoeditor/venv_stt/bin/python
"""
transcribe_segments.py — Associate each video segment with the text it contains.

Takes a folder full of video segments (e.g. `segment_000.mp4`, `segment_001.mp4`,
... as produced by `cut_silences.py`) and, for each one, extracts the audio and
transcribes the spoken words, then writes the result next to the segment.

Backend: **faster-whisper** (CTranslate2 Whisper) — free, offline, fast on CPU,
accurate for French. The model is downloaded once on first run (cached after).

Outputs (inside the given folder):
    transcriptions.json         -> { "segment_000.mp4": "text", ... }  (manifest)
    segment_000.txt, ...        -> one text file per segment
    transcriptions.csv          -> segment,text  (only with --csv)

Usage:
    python3 transcribe_segments.py /path/to/segments
    python3 transcribe_segments.py /path/to/segments --model small --language fr
    python3 transcribe_segments.py /path/to/segments --model base --no-txt

Options:
    --model        tiny | base | small | medium | large-v3   (default small)
    --language     fr | en | auto | any Whisper language     (default fr)
    --device       auto | cpu                                (default auto)
    --compute-type int8 | float32                            (default int8)
    --ext          video file extension to scan              (default mp4)
    --txt          write one .txt per segment (default ON; use --no-txt)
    --csv          also write transcriptions.csv
"""

import argparse
import csv
import glob
import json
import os
import subprocess
import sys
import tempfile

import imageio_ffmpeg
from faster_whisper import WhisperModel

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def extract_audio(video_path: str, wav_path: str, sr: int = 16000) -> None:
    """Extract 16 kHz mono PCM audio from a video, for Whisper."""
    subprocess.run([
        FFMPEG, "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le",
        wav_path,
    ], check=True, capture_output=True, text=True)


def transcribe(model: WhisperModel, wav_path: str, language: str,
               beam_size: int = 5, vad_filter: bool = False) -> str:
    """Return the full transcript of an audio file as one string."""
    segments, _info = model.transcribe(
        wav_path, language=language, beam_size=beam_size, vad_filter=vad_filter)
    return " ".join(s.text.strip() for s in segments).strip()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Transcribe the speech in each video segment of a folder.")
    ap.add_argument("folder", help="Folder containing the segment videos")
    ap.add_argument("--out-dir", default=None,
                    help="Folder for the text outputs (default: same as the segments folder)")
    # --model      : Whisper size. 'small' (default) is the sweet spot on CPU;
    #                tiny/base = faster and rougher, medium = better but slow.
    # --language   : language of the speech, e.g. fr (default), en, or 'auto'.
    # --device     : 'auto' (default) or force 'cpu'.
    # --compute-type: 'int8' = faster and less RAM (default); 'float32' = slightly
    #                better, slower.
    # --beam-size  : decoding search width. 5 (default) = good quality, 1 = fastest.
    # --vad        : let Silero VAD trim the silences before transcribing (off by
    #                default: the silence cut already happened upstream).
    # --ext        : extension of the segment files to transcribe (mp4 default).
    # --txt/--no-txt: write one .txt next to each segment (ON by default).
    # --csv        : also write transcriptions.csv (all texts in one table).
    ap.add_argument("--model", default="small",
                    choices=["tiny", "tiny.en", "base", "base.en", "small", "small.en",
                             "medium", "medium.en", "large-v1", "large-v2", "large-v3", "large",
                             "turbo", "large-v3-turbo", "distil-large-v2", "distil-large-v3",
                             "distil-small.en", "distil-medium.en"],
                    help="Whisper model (default small)")
    ap.add_argument("--language", default="fr",
                    help="Language code, or 'auto' (default fr)")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu"],
                    help="Inference device (default auto)")
    ap.add_argument("--compute-type", default="int8", choices=["int8", "float32"],
                    help="Quantization (int8 = faster + less RAM)")
    ap.add_argument("--beam-size", type=int, default=5,
                    help="Beam size for decoding (default 5)")
    ap.add_argument("--vad", action="store_true",
                    help="Enable Silero VAD to trim silence (default off)")
    ap.add_argument("--ext", default="mp4", help="Segment file extension (default mp4)")
    ap.add_argument("--one-per-folder", action="store_true",
                    help="The folder holds one SUB-FOLDER per segment (one video per "
                         "camera inside): transcribe the first file of each, name the "
                         "text after the sub-folder (segment_3.txt)")
    ap.add_argument("--txt", dest="write_txt", action="store_true", default=True,
                    help="Write one .txt per segment (default ON)")
    ap.add_argument("--no-txt", dest="write_txt", action="store_false",
                    help="Do not write individual .txt files")
    ap.add_argument("--csv", action="store_true", help="Also write transcriptions.csv")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        sys.exit(f"Folder not found: {folder}")

    out_dir = os.path.abspath(args.out_dir) if args.out_dir else folder
    os.makedirs(out_dir, exist_ok=True)

    if args.one_per_folder:
        # Nested layout: one folder per segment, one video per camera inside it. All
        # the cameras of a segment share the same audio, so transcribing each of them
        # would be the same work twice — take the FIRST file of the folder and name
        # the transcript after the FOLDER (segment_3.txt).
        jobs = []
        for d in sorted(os.listdir(folder)):
            sub = os.path.join(folder, d)
            if not os.path.isdir(sub):
                continue
            files = sorted(glob.glob(os.path.join(sub, f"*.{args.ext}")))
            if files:
                jobs.append((d, files[0]))
    else:
        jobs = [(os.path.splitext(os.path.basename(s))[0], s)
                for s in sorted(glob.glob(os.path.join(folder, f"*.{args.ext}")))]
    if not jobs:
        sys.exit(f"No *.{args.ext} files found in {folder}"
                 + (" (or in its sub-folders)" if args.one_per_folder else ""))
    print(f"Found {len(jobs)} segments.")

    language = None if args.language.lower() == "auto" else args.language
    print(f"Loading Whisper model '{args.model}' "
          f"({args.device}/{args.compute_type}) ...")
    model = WhisperModel(args.model, device=args.device,
                         compute_type=args.compute_type)

    results = {}
    for i, (name, seg) in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {name} ...", flush=True)
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            wav = os.path.join(tmp, "audio.wav")
            extract_audio(seg, wav)
            text = transcribe(model, wav, language, args.beam_size, args.vad)
        results[name] = text
        print(f"    -> {text}", flush=True)

    # JSON manifest (segment -> text)
    json_path = os.path.join(out_dir, "transcriptions.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # individual .txt per segment
    n_txt = 0
    if args.write_txt:
        for name, text in results.items():
            txt_path = os.path.join(out_dir, os.path.splitext(name)[0] + ".txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            n_txt += 1

    # optional CSV
    if args.csv:
        csv_path = os.path.join(out_dir, "transcriptions.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["segment", "text"])
            for name, text in results.items():
                w.writerow([name, text])

    print(f"\nDone: {len(results)} segments transcribed.")
    print(f"  manifest: {json_path}")
    if n_txt:
        print(f"  txt files: {n_txt} written (*.txt)")
    if args.csv:
        print(f"  csv: {csv_path}")


if __name__ == "__main__":
    main()
