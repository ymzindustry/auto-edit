#!/home/ymz/.hermes/profiles/videoeditor/venv_stt/bin/python
"""
fragmentation_workflow.py — a folder of videos + one master audio -> fragmentation/

    python3 scripts/workflows/fragmentation_workflow.py <folder> [options]

WHAT IT DOES
  1. takes the master audio: the single .wav of the folder, else the audio of main.mp4;
  2. locates every video inside that audio by cross-correlation (--video-offset to force);
  3. removes the silences FIRST, then enhances only the speech: DeepFilterNet + EQ +
     de-esser + compressor + limiter + loudnorm;
  4. cuts the timeline: each segment takes its picture from the video(s) covering that
     instant and its audio from the master, so sound and picture never drift;
  5. transcribes each segment (optional) and writes fragmentation/ with:
     segment_N/ (one file per camera + .txt), order.txt, sync.json, transcription.txt,
     preview_<H>p.mp4.

OPTIONS
  --wav FILE            master audio (needed only when the folder holds several .wav)
  --main FILE           video whose audio is the master when there is no .wav (main.mp4)
  --sr HZ               sample rate everywhere (48000)
  --lufs N              voice loudness target (-16 speech, -14 loud, -20 dynamic)
  --chunk-sec S         DeepFilterNet chunk length, lower = less RAM (30)
  --skip-enhance        no denoiser, the raw master is used as it is
  --video-offset N=S    force where video N starts in the master (seconds)
  --offset S            force the same offset on every video
  --reuse-offsets FILE  reuse a previous sync.json instead of recomputing
  --canvas WxH          cap on each camera's size, 'native' keeps the original (native)
  --to-1080p            convert every camera to 1080p-class frames
  --noise dB|auto       silence threshold (auto = noise floor + margin)
  --min-silence S       shortest pause treated as a silence (0.3)
  --margin S            speech kept before/after each cut (0.3)
  --tail-pad S          extra sound kept after the LAST word (0.6): without it the
                        final syllable can be clipped by the edge-trim test
  --min-speech S        bursts shorter than this are not segments (0.1)
  --no-pre-cut          enhance the whole recording, then cut (old order)
  --gap black|skip      a hole with no video becomes black frames, or is removed
  --min-gap S           holes shorter than this are absorbed into the neighbours (0.1)
  --prefer cover|tight  which video wins when two cover the same instant
  --numbering ...       how segment folders are named
  --keep-numbering      do not renumber after the empty segments are dropped
  --fps N               output frame rate (default: the fastest camera)
  --stt-model M         Whisper model, small by default
  --language L          language of the speech (fr)
  --stt-*               Whisper device / compute type / beam / VAD
  --no-transcribe       no transcription: every segment is kept
  --min-chars N         drop a segment whose text is shorter than this (3)
  --crf N               H.264 quality of the segments (18)
  --preset P            encoder effort (veryfast)
  --preview-height N    height of the preview (720)
  --preview-fps N       frame rate of the preview (30)
  --preview-crf N       quality of the preview (28)
  --preview-cam PFX     camera shown in the preview (main)
  --no-preview          do not build the preview
  --preview-only        only rebuild the preview from an existing fragmentation/

EXAMPLES
    python3 scripts/workflows/fragmentation_workflow.py ../video-projects/shoulder
    python3 scripts/workflows/fragmentation_workflow.py shoulder --no-transcribe
    python3 scripts/workflows/fragmentation_workflow.py shoulder --noise -30 --margin 0.2
    python3 scripts/workflows/fragmentation_workflow.py shoulder --preview-only
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
PROFILE = os.path.dirname(SCRIPTS)
_VENV_DIR = os.path.join(PROFILE, "venv_stt")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (os.environ.get("_WK_REEXEC") != "1" and os.path.exists(_VENV_PY)
        and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR)):
    os.environ["_WK_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import glob
import json
import re
import shutil
import subprocess
import tempfile

import imageio_ffmpeg

sys.path.insert(0, SCRIPTS)
import segments as seglib
import transcription_overview as txover

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
VENV_AUDIO = os.environ.get("VENV_AUDIO", os.path.join(PROFILE, "venv_audio", "bin", "python"))
VENV_STT = os.environ.get("VENV_STT", os.path.join(PROFILE, "venv_stt", "bin", "python"))

STT_MODELS = ["tiny", "tiny.en", "base", "base.en", "small", "small.en",
              "medium", "medium.en", "large-v1", "large-v2", "large-v3", "large",
              "turbo", "large-v3-turbo", "distil-large-v2", "distil-large-v3",
              "distil-small.en", "distil-medium.en"]

HALLUCINATIONS = [
    r"sous-titres?\s+(r[ée]alis|par|d[ée]but|fin)",
    r"amara\.org",
    r"merci\s+d[\s'’]?avoir\s+regard",
    r"abonne[z\-]vous",
    r"^\s*\[.*\]\s*$",
    r"^\s*[.…\-\s]*\s*$",
]


def run(cmd: list) -> None:
    print("  $ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def is_noise(text: str, min_chars: int) -> bool:
    """True if the segment holds nothing worth keeping (silence / hallucination)."""
    t = (text or "").strip()
    if len(t) < min_chars:
        return True
    low = t.lower()
    return any(re.search(p, low) for p in HALLUCINATIONS)


def probe_ok(path: str) -> bool:
    """True when ffmpeg can open the file (a truncated copy has no moov atom)."""
    res = subprocess.run([FFMPEG, "-v", "error", "-t", "0.1", "-i", path,
                          "-f", "null", "-"], capture_output=True, text=True)
    return res.returncode == 0 and "moov atom not found" not in res.stderr


def find_main_video(folder: str, name: str):
    """The fallback video whose audio becomes the master (default main.mp4)."""
    want = os.path.splitext(name)[0].lower()
    for f in sorted(glob.glob(os.path.join(folder, "*.mp4"))):
        if os.path.splitext(os.path.basename(f))[0].lower() == want:
            return f
    return None


def extract_audio(video: str, out: str, sr: int) -> None:
    """Pull a video's own audio track out as mono 16-bit WAV."""
    r = subprocess.run([FFMPEG, "-y", "-i", video, "-vn", "-ac", "1", "-ar", str(sr),
                        "-c:a", "pcm_s16le", out], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("Could not extract the audio of " + video + ":\n" + r.stderr[-1500:])


def build_preview(stage_dir: str, height: int = 720, fps: float = 30, crf: int = 28,
                  cam: str | None = None) -> str:
    """Call scripts/preview.py — the ONE place that builds a preview.

    All the kept segments are glued back together, in order, with the enhanced audio,
    at 720p/30 fps: a few hundred KB instead of a few GB. That is the file to watch to
    judge the CUT (silence cut too tight, or a pause that was kept). The segments
    themselves are untouched — rebuilding the preview never re-cuts anything.
    """
    out = os.path.join(stage_dir, f"preview_{height}p.mp4")
    venv_py = os.path.join(PROFILE, "venv_video", "bin", "python")
    cmd = [venv_py if os.path.exists(venv_py) else sys.executable,
           os.path.join(SCRIPTS, "preview.py"), stage_dir,
           "--output", out, "--height", str(height), "--fps", f"{fps:g}",
           "--crf", str(crf)]
    if cam:
        cmd += ["--cam", cam]
    env = {k: v for k, v in os.environ.items() if k != "_WK_REEXEC"}
    print("      preview " + " ".join(cmd[1:]))
    r = subprocess.run(cmd, cwd=PROFILE, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print("  ! preview failed (the segments are fine):\n" + (r.stderr or "")[-800:])
        return ""
    size = os.path.getsize(out) / 1e6 if os.path.isfile(out) else 0.0
    print(f"      preview: {size:.1f} MB  ({out})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Enhance + sync + cut + transcribe a folder of videos and one "
                    "master audio -> <folder>/fragmentation/")
    ap.add_argument("folder", help="Working folder: one .wav (or main.mp4) + one or "
                                   "more .mp4 from the same take")

    ap.add_argument("--wav", default=None,
                    help="Master audio to use (default: the single .wav of the folder)")
    ap.add_argument("--main", default="main.mp4",
                    help="Video whose audio becomes the master when there is no .wav "
                         "(default main.mp4)")
    ap.add_argument("--sr", type=int, default=48000,
                    help="Sample rate of the whole pipeline (default 48000; 44100 if "
                         "the source is 44.1 kHz)")
    ap.add_argument("--lufs", type=float, default=-16,
                    help="Loudness of the voice in LUFS: -16 speech/YouTube (default), "
                         "-14 louder, -20 quieter and more dynamic")
    ap.add_argument("--chunk-sec", type=float, default=30.0,
                    help="Denoising chunk length in seconds: lower = less RAM "
                         "(default 30; raise only on a machine with more memory)")
    ap.add_argument("--skip-enhance", action="store_true",
                    help="Skip the denoiser: convert the master to mono 48 kHz only "
                         "(fast, for re-runs and tests — no cleanup)")

    ap.add_argument("--video-offset", action="append", default=[], metavar="NAME=SECONDS",
                    help="Force one video's offset in seconds into the master instead of "
                         "auto-detecting it; repeatable, e.g. camB.mp4=12.63")
    ap.add_argument("--offset", type=float, default=None,
                    help="Force the SAME offset on every video (seconds); only sensible "
                         "when there is a single video")
    ap.add_argument("--reuse-offsets", default=None,
                    help="Reuse a previous offsets JSON and skip the detection")

    ap.add_argument("--canvas", default="native", metavar="WxH",
                    help="Cap on the size of every camera: 'native' (default) = keep "
                         "each video's exact original size, or e.g. 1920x1080. Each "
                         "camera keeps its own format either way (a portrait clip stays "
                         "portrait, never letterboxed into a landscape frame)")
    ap.add_argument("--to-1080p", action="store_true",
                    help="Convert every camera to 1080p-class frames, orientation kept "
                         "(= --canvas 1920x1080). Without it the sizes are untouched")
    ap.add_argument("--noise", default="auto",
                    help="Silence threshold in dB, or 'auto' (default) to derive it from "
                         "the recording's own noise floor + 6 dB. Anything quieter is cut "
                         "BEFORE the enhancement")
    ap.add_argument("--min-silence", type=float, default=0.1,
                    help="Shortest pause (s) treated as a cut: 0.1 tight (default), "
                         "0.6-0.8 keeps more air")
    ap.add_argument("--tail-pad", type=float, default=0.6,
                    help="Extra seconds kept after the last word (default 0.6), so the "
                         "end of the sentence is not clipped")
    ap.add_argument("--margin", type=float, default=0.3,
                    help="Seconds of speech kept before/after each cut, so the cuts are "
                         "not abrupt (default 0.3; 0.6 = more relaxed)")
    ap.add_argument("--min-speech", type=float, default=0.1,
                    help="Bursts shorter than this (s) are ignored (default 0.1)")
    ap.add_argument("--no-pre-cut", action="store_true",
                    help="Enhance the WHOLE recording and cut afterwards (old behaviour) "
                         "instead of removing the silence before the enhancement")
    ap.add_argument("--gap", default="black", choices=["black", "skip"],
                    help="Stretch with NO video: 'black' frames with the voice running "
                         "(default) or 'skip' to drop it entirely")
    ap.add_argument("--min-gap", type=float, default=0.1,
                    help="Holes shorter than this (s) are absorbed into the neighbouring "
                         "video instead of becoming a black frame (default 0.1)")
    ap.add_argument("--prefer", default="cover", choices=["cover", "tight"],
                    help="Two videos cover the same instant: 'cover' (default) keeps the "
                         "one showing the most of it, 'tight' prefers the fragment whose "
                         "span fits it best")
    ap.add_argument("--numbering", default="global", choices=["per-video", "global"],
                    help="Legacy: the segment index is shared by every camera, so both "
                         "behave the same (main_3.mp4 and screenvideo_3.mp4 pair up)")
    ap.add_argument("--keep-numbering", action="store_true",
                    help="Do NOT renumber: keep the numbers as cut, which can leave "
                         "holes like _1, _2, _5 after empty segments are dropped")
    ap.add_argument("--fps", type=float, default=None,
                    help="Output frame rate (default: the highest frame rate of the videos)")

    ap.add_argument("--stt-model", default="small", choices=STT_MODELS,
                    help="Whisper size: small (default, good on CPU), tiny/base = faster "
                         "and rougher, medium = better but slow")
    ap.add_argument("--language", default="fr",
                    help="Language of the speech, e.g. fr (default), en, or 'auto'")
    ap.add_argument("--stt-device", default="auto", choices=["auto", "cpu"],
                    help="Device used by Whisper (default auto)")
    ap.add_argument("--stt-compute-type", default="int8", choices=["int8", "float32"],
                    help="int8 = fast and light (default); float32 = slightly better, slower")
    ap.add_argument("--stt-beam-size", type=int, default=5,
                    help="Search width: 5 = good quality (default), 1 = fastest")
    ap.add_argument("--stt-vad", action="store_true",
                    help="Let Whisper trim silences itself (default off: the cut did it)")
    ap.add_argument("--no-transcribe", action="store_true",
                    help="Skip transcription and keep every segment")

    ap.add_argument("--min-chars", type=int, default=3,
                    help="Drop segments whose transcript is shorter than this (default 3; "
                         "0 = keep everything)")
    ap.add_argument("--crf", type=int, default=18,
                    help="H.264 quality of the segments: 18 visually lossless (default), "
                         "23 smaller, 28 tiny — lower is better")
    ap.add_argument("--preset", default="veryfast",
                    help="Encoder effort: veryfast (default), fast, medium (smaller files, "
                         "much slower)")

    ap.add_argument("--preview-height", type=int, default=720,
                    help="Height of the preview in pixels (default 720)")
    ap.add_argument("--preview-fps", type=float, default=30.0,
                    help="Frame rate of the preview (default 30)")
    ap.add_argument("--preview-crf", type=int, default=28,
                    help="H.264 quality of the preview (default 28 = small; 23 = cleaner)")
    ap.add_argument("--preview-cam", default=None, metavar="PREFIX",
                    help="Camera shown in the preview, by file name prefix "
                         "(default: main, else the first camera of the segment)")
    ap.add_argument("--no-preview", action="store_true",
                    help="Do not build the preview file")
    ap.add_argument("--preview-only", action="store_true",
                    help="Only (re)build the preview from an existing fragmentation/ — no cut, no "
                         "enhancement, no transcription")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        raise SystemExit(f"Not a folder: {folder}")

    if args.preview_only:
        stage = os.path.join(folder, "fragmentation")
        legacy = os.path.join(folder, "wk1")
        wk1_existing = stage if os.path.isdir(stage) else (
            legacy if os.path.isdir(legacy) else folder)
        out = build_preview(wk1_existing, args.preview_height, args.preview_fps,
                            args.preview_crf, args.preview_cam)
        print(f"\nDone -> {out or 'nothing to preview'}")
        return

    print("preview   : built at the end -> fragmentation/preview_"
          f"{args.preview_height}p.mp4 ({args.preview_fps:g} fps)"
          + ("   [disabled with --no-preview]" if args.no_preview else ""))
    videos = sorted(glob.glob(os.path.join(folder, "*.mp4")))
    videos = [v for v in videos if not os.path.basename(v).startswith("preview_")]
    if not videos:
        raise SystemExit(f"No .mp4 in {folder} — nothing to cut.")
    broken = [v for v in videos if not probe_ok(v)]
    if broken:
        raise SystemExit(
            "unreadable video file(s) — the transfer is truncated or the file is corrupt:\n"
            + "\n".join(f"  {b}" for b in broken)
            + "\n  Re-transfer them (a copy whose moov atom is missing plays nowhere), "
              "or move them out of the folder.")

    wavs = sorted(glob.glob(os.path.join(folder, "*.wav")))
    if args.wav:
        master_audio, source = os.path.abspath(args.wav), "wav"
        if not os.path.isfile(master_audio):
            raise SystemExit(f"--wav not found: {master_audio}")
    elif len(wavs) == 1:
        master_audio, source = wavs[0], "wav"
    elif len(wavs) > 1:
        raise SystemExit("Several .wav in " + folder + " — pick one with --wav:\n  "
                         + "\n  ".join(os.path.basename(w) for w in wavs))
    else:
        main_video = find_main_video(folder, args.main)
        if not main_video:
            raise SystemExit(f"No .wav and no {args.main} in {folder}: give me one of "
                             f"them (or pass --main <video>).")
        master_audio, source = main_video, "main"
        print(f"! no .wav in the folder -> using the audio of "
              f"{os.path.basename(main_video)} as the master")

    frag_dir = os.path.join(folder, "fragmentation")
    os.makedirs(frag_dir, exist_ok=True)
    print(f"folder : {folder}")
    print(f"videos : {len(videos)}")
    for v in videos:
        print(f"         {os.path.basename(v)}")
    print(f"master : {os.path.basename(master_audio)}  ("
          + ("the .wav reference" if source == "wav" else "extracted + enhanced from the video")
          + ")")

    kept, dropped = 0, 0
    with tempfile.TemporaryDirectory(dir=PROFILE) as tmp:
        raw = os.path.join(tmp, "master_raw.wav")
        enhanced = os.path.join(tmp, "speech_enhanced.wav")
        audio_map = os.path.join(tmp, "speech_map.json")
        seg_tmp = os.path.join(tmp, "segments")
        txt_tmp = os.path.join(tmp, "texts")
        offsets_json = os.path.join(tmp, "offsets.json")

        print("[1/6] Preparing the master audio (raw, mono 48 kHz) ...")
        if source == "main":
            extract_audio(master_audio, raw, args.sr)
        else:
            r = subprocess.run([FFMPEG, "-y", "-i", master_audio, "-ac", "1", "-ar",
                                str(args.sr), "-c:a", "pcm_s16le", raw],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit("Could not read the master audio:\n" + r.stderr[-1500:])

        print("[2/6] Locating every video inside the master audio ...")
        if args.reuse_offsets:
            shutil.copy2(args.reuse_offsets, offsets_json)
            print(f"      reusing {args.reuse_offsets}")
        else:
            cmd = [VENV_AUDIO, os.path.join(SCRIPTS, "sync_offsets.py"), folder,
                   "--wav", raw, "--json", offsets_json]
            for spec in args.video_offset:
                cmd += ["--offset", spec]
            if args.offset is not None:
                for v in videos:
                    cmd += ["--offset", f"{os.path.basename(v)}={args.offset}"]
            run(cmd)

        with open(offsets_json, encoding="utf-8") as f:
            off = json.load(f)
        off["wav"] = raw
        with open(offsets_json, "w", encoding="utf-8") as f:
            json.dump(off, f, indent=2, ensure_ascii=False)

        if args.no_pre_cut:
            print("[3/6] Enhancing the WHOLE master audio (--no-pre-cut) ...")
            if args.skip_enhance:
                r = subprocess.run([FFMPEG, "-y", "-i", raw, "-c:a", "pcm_s16le", enhanced],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    raise SystemExit("Could not convert the master audio:\n" + r.stderr[-1500:])
                print("      --skip-enhance: no enhancement, raw audio used as is")
            else:
                run([VENV_AUDIO, os.path.join(SCRIPTS, "enhance_audio.py"),
                     raw, "-o", enhanced, "--sr", str(args.sr), "--lufs", str(args.lufs),
                     "--chunk-sec", str(args.chunk_sec)])
        else:
            print("[3/6] Removing the silence, then enhancing ONLY the speech ...")
            cmd = [VENV_AUDIO, os.path.join(SCRIPTS, "speech_master.py"), raw,
                   "-o", enhanced, "--map", audio_map, "--sr", str(args.sr),
                   "--noise", str(args.noise), "--min-silence", str(args.min_silence),
                   "--margin", str(args.margin), "--min-speech", str(args.min_speech),
                   "--tail-pad", str(args.tail_pad)]
            cmd += (["--no-enhance"] if args.skip_enhance
                    else ["--lufs", str(args.lufs), "--chunk-sec", str(args.chunk_sec)])
            run(cmd)

        print("[4/6] Cutting the timeline into segments ...")
        cmd = [VENV_AUDIO, os.path.join(SCRIPTS, "cut_timeline.py"), folder,
               "--offsets", offsets_json, "-o", seg_tmp,
               "--gap", args.gap, "--numbering", args.numbering,
               "--min-gap", str(args.min_gap), "--prefer", args.prefer,
               "--crf", str(args.crf), "--preset", args.preset]
        if args.no_pre_cut:
            cmd += ["--wav", enhanced, "--silence-wav", raw,
                    "--noise", str(args.noise), "--min-silence", str(args.min_silence),
                    "--margin", str(args.margin), "--min-speech", str(args.min_speech),
                   "--tail-pad", str(args.tail_pad)]
        else:
            cmd += ["--audio-map", audio_map]
        cmd += ["--canvas", "1920x1080" if args.to_1080p else args.canvas]
        if args.fps:
            cmd += ["--fps", str(args.fps)]
        run(cmd)

        with open(os.path.join(seg_tmp, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)

        if args.no_transcribe:
            print("[5/6] Skipping transcription (--no-transcribe).")
        else:
            print("[5/6] Transcribing segments ...")
            run([VENV_STT, os.path.join(SCRIPTS, "transcribe_segments.py"),
                 seg_tmp, "--out-dir", txt_tmp, "--one-per-folder",
                 "--model", args.stt_model, "--language", args.language,
                 "--device", args.stt_device, "--compute-type", args.stt_compute_type,
                 "--beam-size", str(args.stt_beam_size)] +
                (["--vad"] if args.stt_vad else []))

        print("[6/6] Organizing into fragmentation/ ...")
        stale = [d for d in os.listdir(frag_dir) if os.path.isdir(os.path.join(frag_dir, d))]
        for d in stale:
            shutil.rmtree(os.path.join(frag_dir, d))
        if stale:
            print(f"      cleared {len(stale)} segment folder(s) from the previous run")

        survivors = []
        for seg in manifest["segments"]:
            name = seg["name"]
            src_dir = os.path.join(seg_tmp, name)
            if not os.path.isdir(src_dir):
                continue
            txt = os.path.join(txt_tmp, name + ".txt")
            text = (open(txt, encoding="utf-8", errors="replace").read()
                    if os.path.isfile(txt) else "")
            if not args.no_transcribe and is_noise(text, args.min_chars):
                dropped += 1
                print(f"  - {name}: dropped (nothing said)")
                continue
            survivors.append({"seg": seg, "dir": src_dir, "text": text.strip()})

        order, mapping = [], []
        for i, item in enumerate(survivors, 0):
            seg = item["seg"]
            old = seg["name"]
            new = old if args.keep_numbering else f"segment_{i}"
            if new != old:
                mapping.append((old, new))

            dest = os.path.join(frag_dir, new)
            os.makedirs(dest, exist_ok=True)
            files = []
            for fl in seg["files"]:
                src_file = os.path.join(item["dir"], fl["file"])
                if not os.path.isfile(src_file):
                    continue
                new_file = (fl["file"] if args.keep_numbering
                            else re.sub(r"_\d+\.mp4$", f"_{i}.mp4", fl["file"]))
                shutil.copy2(src_file, os.path.join(dest, new_file))
                files.append({"file": new_file, "video": fl["video"],
                              "size": fl.get("size"),
                              "black": fl.get("black", False),
                              "pieces": fl.get("pieces", [])})
            with open(os.path.join(dest, new + ".txt"), "w", encoding="utf-8") as f:
                f.write(item["text"])
            seg["original_name"] = old
            seg["name"] = new
            seg["files"] = files
            order.append(new)
            kept += 1

        if mapping:
            print("      renumbered 1, 2, 3... (no holes after the empty ones were "
                  "dropped):  " + ", ".join(f"{o}->{n}" for o, n in mapping))

        seglib.write_order(frag_dir, order)
        pre_cut = None
        if not args.no_pre_cut and os.path.isfile(audio_map):
            with open(audio_map, encoding="utf-8") as f:
                pre_cut = json.load(f)
        sync_report = {"audio_source": source, "master_audio": master_audio,
                       "master_duration": manifest["master_duration"],
                       "canvas": manifest["canvas"], "fps": manifest["fps"],
                       "gaps": manifest["gaps"], "videos": off["videos"],
                       "pre_cut": pre_cut,
                       "segments": [item["seg"] for item in survivors],
                       "dropped": dropped}
        with open(os.path.join(frag_dir, "sync.json"), "w", encoding="utf-8") as f:
            json.dump(sync_report, f, indent=2, ensure_ascii=False)

        if not args.no_transcribe:
            txover.build(frag_dir)
            print("      transcription.txt written (every segment's text, in order)")

        if not args.no_preview:
            build_preview(frag_dir, args.preview_height, args.preview_fps,
                          args.preview_crf, args.preview_cam)

    print(f"\nDone -> {frag_dir}")
    print(f"  {kept} segments kept, {dropped} dropped (empty / hallucination)")
    print("  they are numbered from segment_0 with no holes (original: sync.json)")
    print("  order.txt + sync.json written (offsets, gaps, plan)")
    if not args.no_preview:
        print(f"  fragmentation/preview_{args.preview_height}p.mp4 — all the segments glued back, "
              f"{args.preview_fps:g} fps, light: watch the CUT")
    if not args.no_transcribe:
        print("  fragmentation/transcription.txt — the text of every segment, in playback order")
    print("  next: scripts/workflows/greenscreen_workflow.py <folder>")


if __name__ == "__main__":
    main()
