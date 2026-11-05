#!/home/ymz/.hermes/profiles/videoeditor/venv_audio/bin/python
"""
cut_timeline.py — Cut the master audio timeline and pull the matching picture
out of the videos (several videos per segment are fine).

WHY THIS EXISTS
    The master audio (the .wav, or main.mp4's audio) is ONE continuous timeline.
    Every video was located on it by sync_offsets.py. A silence auto-cut finds the
    speech intervals on that timeline; for each interval:

      * the AUDIO comes from the master wav at that exact [start, end] — already
        enhanced, perfectly in sync, whatever the cut does;
      * the PICTURE comes from whichever video covers that time. When the interval
        straddles a video boundary (or a hole where no video was recording) the
        pieces are cut separately and glued back together, so ONE segment can hold
        SEVERAL videos;
      * a hole with no video at all becomes BLACK (--gap black, default — the audio
        keeps running, which is what you want before dropping B-roll on it later)
        or is DROPPED completely (--gap skip).

NAMING
    <video>_1, <video>_2, ... where <video> is the video the segment STARTS in and
    the counter restarts at 1 for each video (--numbering global = one running
    counter over the whole timeline instead).

OUTPUT
    <out-dir>/<name>.mp4                    one file per segment
    <out-dir>/manifest.json                 machine-readable plan (used by fragmentation_workflow)
    <out-dir>/manifest.txt                  the same, readable

Usage:
    ./cut_timeline.py <folder> --wav enhanced.wav --offsets offsets.json -o segs/
    ./cut_timeline.py <folder> --offsets offsets.json -o segs/ --gap skip
    ./cut_timeline.py <folder> --offsets offsets.json -o segs/ --numbering global
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
import shutil
import subprocess
import tempfile

import imageio_ffmpeg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cut_silences import (auto_threshold_db, compute_keep_intervals,  # noqa: E402
                          detect_silences, noise_floor_db)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
PROFILE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EPS = 1e-4          # seconds — anything shorter is not a piece


def run_ffmpeg(args: list) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + proc.stderr[-2000:])


def probe(path: str) -> dict:
    """Duration / audio channel count of a media file."""
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    a = next((l for l in out.splitlines() if "Audio:" in l), "")
    ch = 2 if "stereo" in a else (1 if "mono" in a else 0)
    if ch == 0:
        mc = re.search(r"Audio:.*?,\s*(\d+)\s*channels", a)
        ch = int(mc.group(1)) if mc else 1
    return {"duration": dur, "channels": ch}


def plan_pieces(a: float, b: float, vids: list, min_gap: float = 0.1,
                prefer: str = "cover") -> list:
    """Split [a, b] (master timeline) into pieces taken from the videos.

    Holes shorter than `min_gap` are not worth a black frame: they are absorbed
    into the neighbouring video (a few milliseconds of missing frames, e.g. when
    a video's detected offset lands a hair after the segment start).

    When two videos cover the same instant, `prefer` decides:
      * "cover" (default): the one showing the MOST of the interval (so a video
        that recorded the whole session wins everywhere);
      * "tight": the one whose own span fits the interval the most closely — a
        fragment wins over the whole-session video it overlaps.
    """
    cuts = {a, b}
    for v in vids:
        s, e = v["offset"], v["offset"] + v["duration"]
        for t in (s, e):
            if a + EPS < t < b - EPS:
                cuts.add(t)
    bounds = sorted(cuts)

    def key(v, x, y):
        # max() picks the winner -> negate what should be MINIMISED
        ov = min(y, v["offset"] + v["duration"]) - max(x, v["offset"])
        if prefer == "tight":
            return (-(v["duration"] - ov), ov, -v["offset"])
        return (ov, -v["offset"])

    pieces = []
    for x, y in zip(bounds, bounds[1:]):
        if y - x <= EPS:
            continue
        covering = [v for v in vids
                    if v["offset"] - EPS <= x + EPS and y <= v["offset"] + v["duration"] + EPS]
        if covering:
            best = max(covering, key=lambda v: key(v, x, y))
            piece = {"kind": "video", "video": best["name"], "path": best["path"],
                     "master_start": x, "master_end": y,
                     "local_start": x - best["offset"], "local_end": y - best["offset"]}
        else:
            piece = {"kind": "gap", "video": None, "path": None,
                     "master_start": x, "master_end": y,
                     "local_start": None, "local_end": None}
        # glue contiguous pieces that come from the same video
        prev = pieces[-1] if pieces else None
        if (prev and prev["kind"] == piece["kind"] == "video"
                and prev["video"] == piece["video"]
                and abs(prev["local_end"] - piece["local_start"]) < 1e-3):
            prev["master_end"] = piece["master_end"]
            prev["local_end"] = piece["local_end"]
        elif (prev and prev["kind"] == piece["kind"] == "gap"
                and abs(prev["master_end"] - piece["master_start"]) < 1e-3):
            prev["master_end"] = piece["master_end"]
        else:
            pieces.append(piece)

    # Absorb sub-frame holes: a detected offset landing a few ms after the segment
    # start must NOT emit a 5 ms black flash. A short hole is given to the video
    # next to it when that video has the frames; otherwise it is dropped (a few ms
    # lost at a segment edge is invisible, a black flash is not).
    by_name = {v["name"]: v for v in vids}
    out = []
    for p in pieces:
        if p["kind"] == "video":
            prev = out[-1] if out else None
            if prev and prev["kind"] == "gap":
                glen = prev["master_end"] - prev["master_start"]
                if glen < min_gap:
                    grown = min(glen, max(p["local_start"], 0.0))
                    if grown > EPS:
                        p["master_start"] -= grown
                        p["local_start"] -= grown
                        prev["master_end"] -= grown
                        glen -= grown
                    if glen < min_gap:
                        out.pop()          # absorbed, or dropped
            out.append(p)
            continue

        glen = p["master_end"] - p["master_start"]
        prev = out[-1] if out else None
        if glen < min_gap and prev and prev["kind"] == "video":
            vid = by_name[prev["video"]]
            grown = min(glen, max(vid["duration"] - prev["local_end"], 0.0))
            if grown > EPS:
                prev["master_end"] += grown
                prev["local_end"] += grown
                p["master_start"] += grown
                glen -= grown
            if glen < min_gap:
                continue
        out.append(p)
    return out


def audio_span(audio_map, ms: float, me: float) -> tuple:
    """Translate a master-timeline span into the AUDIO file's own timeline.

    With no map the audio file IS the timeline, so nothing changes. With a map
    (speech_master.py cut the silence out of the audio before enhancing it) the
    span is shifted by the interval's offset and clamped inside that interval, so
    a piece can never bleed into the next interval's audio.
    """
    if not audio_map:
        return ms, me
    for it in audio_map["intervals"]:
        if it["master_start"] - EPS <= ms < it["master_end"] + EPS:
            shift = it["audio_start"] - it["master_start"]
            end = min(me + shift, it["audio_start"] + (it["master_end"] - it["master_start"]))
            return ms + shift, max(end, ms + shift)
    raise SystemExit(f"The audio map has no interval covering {ms:.3f}s — the cut and "
                     f"the map disagree (re-run speech_master.py and cut_timeline.py "
                     f"with the same silence settings).")


def _capped(w: int, h: int, cap) -> tuple:
    """(w, h) fitted inside `cap` ('WxH'), aspect AND orientation kept.

    The cap is written in landscape (1920x1080); a PORTRAIT clip is compared the other
    way round, so 1080x2400 with a 1920x1080 cap gives 864x1920 (still portrait) and
    not 486x1080. With no cap the clip keeps its EXACT native size.
    """
    if not cap:
        return w, h
    mw, mh = cap
    if h > w:                       # portrait: swap the box so it stays portrait
        mw, mh = mh, mw
    if w <= mw and h <= mh:
        return w, h
    s = min(mw / w, mh / h)
    return (max(2, int(w * s) // 2 * 2), max(2, int(h * s) // 2 * 2))


def render_piece(src: str, ls: float, le: float, ms: float, me: float, out: str,
                 wav: str, canvas: tuple, fps: float, crf: int, preset: str,
                 channels: int, audio_map=None) -> None:
    """Cut [ls, le] out of `src` and give it the master audio of [ms, me]."""
    w, h = canvas
    a0, a1 = audio_span(audio_map, ms, me)
    chain = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p")
    run_ffmpeg([
        FFMPEG, "-y",
        "-ss", f"{ls:.4f}", "-to", f"{le:.4f}", "-i", src,
        "-ss", f"{a0:.4f}", "-to", f"{a1:.4f}", "-i", wav,
        "-filter_complex", f"[0:v]{chain}[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", str(channels),
        "-video_track_timescale", "90000",
        "-t", f"{a1 - a0:.4f}",
        out,
    ])


def render_gap(ms: float, me: float, out: str, wav: str, canvas: tuple,
               fps: float, crf: int, preset: str, channels: int,
               audio_map=None) -> None:
    """Black filler for a hole with no video, carrying the master audio."""
    w, h = canvas
    a0, a1 = audio_span(audio_map, ms, me)
    dur = a1 - a0
    run_ffmpeg([
        FFMPEG, "-y",
        "-f", "lavfi", "-t", f"{dur:.4f}", "-i", f"color=c=black:s={w}x{h}:r={fps}",
        "-ss", f"{a0:.4f}", "-to", f"{a1:.4f}", "-i", wav,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", str(channels),
        "-video_track_timescale", "90000",
        "-t", f"{dur:.4f}",
        out,
    ])


def concat_pieces(pieces: list, out: str, tmp: str) -> None:
    """Glue the pieces of one segment (identical params -> stream copy)."""
    if len(pieces) == 1:
        shutil.copy2(pieces[0], out)
        return
    listfile = os.path.join(tmp, "list.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for p in pieces:
            f.write("file '" + os.path.abspath(p) + "'\n")
    run_ffmpeg([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                "-c", "copy", out])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Cut the master timeline into segments, taking the picture from the videos.")
    ap.add_argument("folder", help="Folder holding the videos (and the master wav)")
    # ══ TIMELINE ══
    # (the master audio and the offsets JSON are what describe the timeline; this
    #  script does not know anything about the videos except where they start)
    # --wav        : the ENHANCED master audio = the timeline the cuts are measured
    #                on, and where every segment's audio is sliced from. Default is
    #                the "wav" field of the offsets JSON.
    # --offsets    : the JSON written by sync_offsets.py (per-video start times).
    #                Required: it is what places the videos on the timeline.
    # --out-dir    : where the segment files are written (one <name>.mp4 each).
    # --manifest   : where the JSON plan goes (default <out-dir>/manifest.json);
    #                a readable <manifest>.txt is written next to it too.

    # ══ THE CUT ══
    # --silence-wav: which audio is ANALYSED for silences. Default = --wav (the
    #                enhanced one). Point it at the RAW recording when the
    #                enhancement lifts the noise floor and silences stop being found.
    # --noise      : silence threshold in dB — anything quieter is cut. -35 default;
    #                lower it (-45) on a hissy recording, raise it (-25) to keep
    #                quiet breathing.
    # --min-silence: shortest pause treated as a cut, seconds (0.3 default; 0.6-0.8
    #                keeps more air, 0.15 cuts even short gaps).
    # --margin     : seconds kept before/after each speech burst so the cuts are not
    #                abrupt (0.3 tight, 0.6 relaxed).
    # --min-speech : bursts shorter than this (0.1 s) are not segments.
    # --gap        : a stretch with NO video at all: 'black' keeps black frames with
    #                the voice still running (default), 'skip' drops it.
    # --min-gap    : holes shorter than this (0.1 s) are absorbed into the neighbouring
    #                video, or dropped, instead of becoming a black flash.
    # --prefer     : when two videos cover the same instant: 'cover' (default) keeps
    #                the one showing most of the interval, 'tight' prefers the
    #                fragment whose span fits it best.

    # ══ OUTPUT / NAMING ══
    # --numbering  : 'per-video' (default) names segments <video>_1, <video>_2...
    #                restarting on each video; 'global' keeps one running counter.
    # --canvas     : output frame size 'WxH' (default = the biggest video, never
    #                downscales); --fps = output frame rate (default = the highest).
    # --crf        : H.264 quality of the re-encoded pieces (18 = visually lossless).
    # --preset     : encoder effort (veryfast = default on this CPU, medium = smaller
    #                files, much slower).
    ap.add_argument("--wav", default=None, help="Master (enhanced) audio — the timeline")
    ap.add_argument("--offsets", required=True, help="sync_offsets.py JSON report")
    ap.add_argument("-o", "--out-dir", required=True, help="Folder for the segments")
    ap.add_argument("--manifest", default=None,
                    help="JSON manifest path (default: <out-dir>/manifest.json)")
    # --audio-map : the JSON written by speech_master.py. When it is given the
    #               silence has ALREADY been removed from the audio before it was
    #               enhanced: the intervals of the map ARE the cut (silencedetect is
    #               not run again, and --noise/--min-silence are ignored) and every
    #               piece takes its audio at the mapped position in that shorter file.
    ap.add_argument("--audio-map", default=None,
                    help="speech_master.py JSON: the cut intervals + where their audio "
                         "lives in the (silence-free, enhanced) audio file")
    ap.add_argument("--silence-wav", default=None,
                    help="Detect the silences on THIS audio instead of --wav (e.g. the "
                         "raw recording, whose noise floor is more honest)")
    # --noise : silence threshold. 'auto' (default) = the analysed recording's own
    #           noise floor + 6 dB — robust on a hissy file, where a fixed -35 dB
    #           would find no silence at all. A number forces that value.
    ap.add_argument("--noise", default="auto",
                    help="Silence threshold in dB, or 'auto' (default) to derive it "
                         "from the analysed recording's own noise floor")
    ap.add_argument("--noise-offset", type=float, default=12.0,
                    help="With --noise auto: how many dB above the measured noise floor "
                         "still counts as silence (default 12)")
    ap.add_argument("--min-silence", type=float, default=0.3,
                    help="Shortest pause (s) treated as a cut (default 0.3)")
    ap.add_argument("--margin", type=float, default=0.3,
                    help="Seconds kept before/after each burst, so cuts are not abrupt "
                         "(default 0.3)")
    ap.add_argument("--min-speech", type=float, default=0.1,
                    help="Bursts shorter than this (s) are not segments (default 0.1)")
    ap.add_argument("--gap", default="black", choices=["black", "skip"],
                    help="Legacy: with the one-file-per-camera layout every file must "
                         "keep the same duration, so a hole is ALWAYS black inside a "
                         "segment ('skip' no longer applies there)")
    ap.add_argument("--min-gap", type=float, default=0.1,
                    help="Holes shorter than this (s) are absorbed into the neighbouring "
                         "video instead of becoming a black frame (default 0.1)")
    ap.add_argument("--prefer", default="cover", choices=["cover", "tight"],
                    help="Legacy: each camera now gets its OWN file per segment, so "
                         "there is no competition to arbitrate")
    ap.add_argument("--numbering", default="global", choices=["per-video", "global"],
                    help="Legacy: the segment index is now SHARED by every camera "
                         "(main_3.mp4 and screenvideo_3.mp4 are the same instant)")
    ap.add_argument("--canvas", default="native", metavar="WxH",
                    help="CAP on the size of every camera. 'native' (default) = each "
                         "video's exact original size; a size like 1920x1080 converts "
                         "them. Each camera keeps its own format: a portrait clip stays "
                         "portrait, it is never letterboxed into a landscape frame")
    ap.add_argument("--fps", type=float, default=None,
                    help="Output frame rate (default: the highest of the videos)")
    ap.add_argument("--crf", type=int, default=18, help="x264 quality (default 18)")
    ap.add_argument("--preset", default="veryfast", help="x264 preset (default veryfast)")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    with open(args.offsets, encoding="utf-8") as f:
        off = json.load(f)

    audio_map = None
    if args.audio_map:
        with open(args.audio_map, encoding="utf-8") as f:
            audio_map = json.load(f)

    wav = args.wav or (audio_map or {}).get("audio") or off["wav"]
    if not os.path.isfile(wav):
        raise SystemExit(f"Master audio not found: {wav}")

    vids = []
    for v in off["videos"]:
        path = v["path"] if os.path.isfile(v["path"]) else os.path.join(folder, v["name"])
        if not os.path.isfile(path):
            raise SystemExit(f"Video not found: {v['name']}")
        v = dict(v)
        v["path"] = path
        vids.append(v)
    if not vids:
        raise SystemExit("No video in the offsets report")

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = args.manifest or os.path.join(out_dir, "manifest.json")

    # ---- frame size: EVERY CAMERA KEEPS ITS OWN FORMAT. The user's rule: "je veux
    #      garder le format des vidéos, si portrait alors garde portrait". A portrait
    #      screen recording used to be letterboxed into the biggest video's landscape
    #      canvas (1080x2400 became a 3840x2160 frame full of black); now every camera
    #      is rendered at its own size, and NOTHING is converted unless --canvas asks
    #      for a cap (see _capped): no dimension exceeds it, the aspect ratio is never
    #      changed.
    biggest = max(vids, key=lambda v: v["width"] * v["height"])
    cap_arg = (args.canvas or "").strip().lower()
    cap = None if cap_arg in ("", "none", "native", "0") else \
        tuple(int(x) for x in cap_arg.replace("*", "x").split("x"))
    cam_canvas = {v["name"]: _capped(int(v["width"] or 1920),
                                     int(v["height"] or 1080), cap) for v in vids}
    canvas = cam_canvas[biggest["name"]]        # report/manifest only
    fps = args.fps or max(v["fps"] or 30.0 for v in vids)
    wav_info = probe(wav)
    channels = wav_info["channels"] or 1
    master_dur = off.get("wav_duration") or wav_info["duration"]

    print(f"folder      : {folder}")
    print(f"master audio: {os.path.basename(wav)}  ({master_dur:.2f}s, {channels}ch)")
    print(f"videos      : {len(vids)}")
    for v in vids:
        cw, ch = cam_canvas[v["name"]]
        print(f"  {v['name']}: offset {v['offset']:+.3f}s  dur {v['duration']:.2f}s  "
              f"{cw}x{ch} ({'portrait' if ch > cw else 'paysage' if cw > ch else 'carre'})"
              + ("   [LOW CONFIDENCE]" if v.get("low_confidence") else ""))
    print(f"format      : one size per camera (each keeps its own)  @ {fps:g} fps")

    # holes between consecutive videos (only informative, black filler is honest)
    gaps = []
    end = 0.0
    for v in sorted(vids, key=lambda v: v["offset"]):
        s, e = v["offset"], v["offset"] + v["duration"]
        if s > end + 0.05:
            gaps.append((max(end, 0.0), s))
        end = max(end, e)
    if gaps:
        total = sum(b - a for a, b in gaps)
        print(f"holes       : {len(gaps)} gap(s), {total:.1f}s with no video at all"
              f"  (-> {args.gap})")

    # ---- the cut itself
    if audio_map:
        # the silence was removed from the audio BEFORE the enhancement, so the map
        # already holds the cut — detect nothing here, or the two would disagree
        keeps = [(it["master_start"], it["master_end"]) for it in audio_map["intervals"]]
        removed = master_dur - sum(b - a for a, b in keeps)
        print(f"cut         : {len(keeps)} interval(s) from the audio map "
              f"({os.path.basename(wav)}) — {removed:.1f}s of silence was removed from "
              f"the audio before enhancing it")
    else:
        detect_on = args.silence_wav if args.silence_wav and os.path.isfile(args.silence_wav) else wav
        noise = args.noise
        if isinstance(noise, str) and noise.strip().lower() == "auto":
            noise = auto_threshold_db(detect_on, args.noise_offset)
            print(f"threshold   : {noise:.1f} dB (auto, from "
                  f"{os.path.basename(detect_on)}: pauses vs speech)")
        silences = detect_silences(detect_on, float(noise), args.min_silence)
        keeps = compute_keep_intervals(silences, master_dur, args.margin, args.min_speech)
        removed = sum(e - s for s, e in silences)
        print(f"silences    : {len(silences)} ({removed:.1f}s) on "
              f"{os.path.basename(detect_on)} -> {len(keeps)} segment(s)")

    manifest = {"wav": wav, "folder": folder, "canvas": list(canvas), "fps": fps,
                "channels": channels, "master_duration": master_dur,
                "gaps": [{"start": a, "end": b} for a, b in gaps],
                "segments": []}

    with tempfile.TemporaryDirectory(dir=PROFILE) as tmp:
        piece_dir = os.path.join(tmp, "pieces")
        os.makedirs(piece_dir, exist_ok=True)

        for k, (a, b) in enumerate(keeps, 0):
            # segments are numbered FROM 0 on purpose (user's choice): the first
            # segment of the playback order is segment_0, the next segment_1, and the
            # filenames follow (main_0.mp4, screenvideo_0.mp4, ...).
            # ONE FOLDER PER SEGMENT, ONE FILE PER CAMERA INSIDE IT. Every camera is
            # written for every segment, all covering the same [a, b] of the master
            # timeline, so main_3.mp4 and screenvideo_3.mp4 are the same instant in
            # two angles — same duration, same audio, in sync. A camera that was not
            # rolling gets BLACK frames there (never a shorter file: the durations
            # must stay equal, that is the whole point).
            seg_name = f"segment_{k}"
            seg_dir = os.path.join(out_dir, seg_name)
            os.makedirs(seg_dir, exist_ok=True)
            entry = {"name": seg_name, "start": round(a, 3), "end": round(b, 3),
                     "duration": round(b - a, 3), "files": []}

            for v in vids:
                stem = os.path.splitext(v["name"])[0]
                pieces = plan_pieces(a, b, [v], args.min_gap, "cover")
                pieces = [p for p in pieces
                          if p["master_end"] - p["master_start"] > EPS]
                if not pieces:                       # cannot happen, but stay explicit
                    pieces = [{"kind": "gap", "video": None, "path": None,
                               "master_start": a, "master_end": b,
                               "local_start": None, "local_end": None}]

                files = []
                for j, p in enumerate(pieces):
                    piece_file = os.path.join(piece_dir, f"s{k:03d}_{stem}_{j}.mp4")
                    if p["kind"] == "video":
                        render_piece(p["path"], p["local_start"], p["local_end"],
                                     p["master_start"], p["master_end"],
                                     piece_file, wav, cam_canvas[v["name"]], fps,
                                     args.crf, args.preset,
                                     channels, audio_map)
                    else:
                        render_gap(p["master_start"], p["master_end"], piece_file, wav,
                                   cam_canvas[v["name"]], fps, args.crf, args.preset,
                                   channels, audio_map)
                    files.append(piece_file)

                out_file = os.path.join(seg_dir, f"{stem}_{k}.mp4")
                concat_pieces(files, out_file, tmp)

                covered = sum(p["master_end"] - p["master_start"]
                              for p in pieces if p["kind"] == "video")
                entry["files"].append({
                    "file": f"{stem}_{k}.mp4", "video": v["name"],
                    "size": list(cam_canvas[v["name"]]),
                    "black": covered <= EPS,
                    "covered": round(covered, 3),
                    "pieces": [{kk: (round(vv, 3) if isinstance(vv, float) else vv)
                                for kk, vv in p.items()} for p in pieces]})

            manifest["segments"].append(entry)
            kk = "  ".join(
                f["file"] + ("(BLACK)" if f["black"] else "") for f in entry["files"])
            print(f"[{k}/{len(keeps)}] {seg_name}  {a:.2f}s-{b:.2f}s  ({b - a:.2f}s)  {kk}")

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    txt = manifest_path[:-5] + ".txt" if manifest_path.endswith(".json") else manifest_path + ".txt"
    black = 0
    with open(txt, "w", encoding="utf-8") as f:
        f.write("# one folder per segment, one file per camera, all the same instant\n")
        f.write("# segment      master start   end     dur   files (per camera)\n")
        for s in manifest["segments"]:
            for fl in s["files"]:
                if fl["black"]:
                    black += 1
            names = ", ".join(fl["file"] + (" [black]" if fl["black"] else "")
                              for fl in s["files"])
            f.write(f"{s['name']:<14} {s['start']:>10.3f} {s['end']:>8.3f} "
                    f"{s['duration']:>7.3f}   {names}\n")

    print(f"\nDone -> {out_dir}")
    print(f"  {len(manifest['segments'])} segment(s) x {len(vids)} camera(s)")
    if black:
        print(f"  {black} file(s) are all black (that camera was not rolling then)")
    print(f"  manifest: {manifest_path}")


if __name__ == "__main__":
    main()
