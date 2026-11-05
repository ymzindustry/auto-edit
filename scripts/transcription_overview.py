#!/usr/bin/env python3
"""
transcription_overview.py — the WHOLE video's speech in one file.

Reads a fragmentation/ folder (segment folders + their .txt + sync.json) and writes
`transcription.txt`: one block per segment, in playback order, with the segment's
position on the MASTER timeline (the take) and in the FINAL montage (cumulative),
its length, its word count and the cameras it holds — then the text itself. Made for
a global read-through of what is said in the video.

USAGE
    python3 transcription_overview.py <fragmentation folder or project> [--out FILE]

    python3 transcription_overview.py shoulder/fragmentation
    python3 transcription_overview.py shoulder                # its fragmentation/ if it exists
    python3 transcription_overview.py shoulder/fragmentation --out /tmp/t.txt

Called automatically at the end of fragmentation_workflow.py.
"""
import argparse
import json
import os
import re
import sys
import textwrap

RULE = "─" * 78


def hhmmss(t: float) -> str:
    """Seconds -> mm:ss.mmm (h:mm:ss.mmm past an hour)."""
    ms_total = int(round(max(0.0, float(t)) * 1000))
    ms, s = ms_total % 1000, (ms_total // 1000) % 60
    m, h = (ms_total // 60000) % 60, ms_total // 3600000
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m:02d}:{s:02d}.{ms:03d}"


FOLDERS = ("fragmentation", "wk1")          # current name, then the legacy one


def resolve_wk1(path: str) -> str:
    """Accept the stage folder itself, or the project folder that contains it."""
    path = os.path.abspath(path)
    for name in FOLDERS:
        cand = os.path.join(path, name)
        if os.path.isdir(cand):
            return cand
    return path


def segment_list(folder: str, order: list) -> list:
    """The segment folders in playback order: order.txt first, else natural sort."""
    dirs = [d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d))]
    if order:
        known = [d for d in order if d in dirs]           # order.txt wins
        return known + sorted(set(dirs) - set(known))
    return sorted(dirs, key=lambda d: [int(x) if x.isdigit() else x
                                       for x in re.split(r"(\d+)", d)])


def read_text(seg_dir: str, name: str) -> str:
    """The transcript of a segment: <folder>.txt, else the first .txt present."""
    p = os.path.join(seg_dir, name + ".txt")
    if os.path.isfile(p):
        return open(p, encoding="utf-8", errors="replace").read().strip()
    for f in sorted(os.listdir(seg_dir)):
        if f.endswith(".txt"):
            return open(os.path.join(seg_dir, f), encoding="utf-8",
                        errors="replace").read().strip()
    return ""


def build(path: str, out: str = None) -> str:
    """Write <folder>/transcription.txt and return its path."""
    folder = resolve_wk1(path)
    order = []
    p = os.path.join(folder, "order.txt")
    if os.path.isfile(p):
        order = [l.strip() for l in open(p, encoding="utf-8")
                 if l.strip() and not l.startswith("#")]

    sync = {}
    p = os.path.join(folder, "sync.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            sync = json.load(f)
    by_name = {s.get("name"): s for s in sync.get("segments", [])}

    names = segment_list(folder, order)
    rows = []
    for name in names:
        d = os.path.join(folder, name)
        text = read_text(d, name)
        info = by_name.get(name, {})
        cams = sorted({os.path.splitext(f)[0].rsplit("_", 1)[0]
                       for f in os.listdir(d) if f.endswith(".mp4")})
        rows.append({"name": name, "text": text, "cams": cams, "info": info,
                     "start": float(info.get("start") or 0.0),
                     "end": float(info.get("end") or 0.0),
                     "dur": float(info.get("duration") or 0.0),
                     "timed": "duration" in info})

    master = float(sync.get("master_duration") or 0.0)
    speech = sum(r["dur"] for r in rows if r["timed"])
    words = sum(len(r["text"].split()) for r in rows)
    project = os.path.basename(os.path.dirname(folder.rstrip("/"))) or folder
    all_cams = sorted({c for r in rows for c in r["cams"]})

    L = [f"TRANSCRIPTION — {project}",
         f"source audio : {sync.get('audio_source') or '-'}",
         f"caméras      : {len(all_cams)} ({', '.join(all_cams) or '-'})"]
    if sync.get("wav") or sync.get("master_audio"):
        L.append(f"audio maître : {sync.get('wav') or sync.get('master_audio')}"
                 + (f"   ({hhmmss(master)})" if master else ""))
    L += [f"segments     : {len(rows)} gardés, {sync.get('dropped', 0)} vide(s) "
          f"supprimé(s)",
          f"montage      : {hhmmss(speech)} de parole, {words} mots"
          + (f"  ({speech / master * 100:.0f} % de la prise)" if master else ""),
          "", RULE, ""]

    cur = 0.0
    for r in rows:
        w = len(r["text"].split())
        head = f"[{r['name']}]"
        if r["timed"]:
            head += (f"  prise {hhmmss(r['start'])}→{hhmmss(r['end'])}"
                     f"   montage {hhmmss(cur)}→{hhmmss(cur + r['dur'])}"
                     f"   {r['dur']:6.2f}s · {w:4d} mots")
        else:
            head += f"   {w:4d} mots"
        if r["cams"]:
            head += f" · {', '.join(r['cams'])}"
        L += [head] + ["    " + l for l in (textwrap.wrap(r["text"], 92) or [""])]
        L.append("")
        cur += r["dur"]

    L += [RULE, f"FIN — {len(rows)} segments, {hhmmss(speech)} de parole, "
                f"{words} mots.", ""]
    out = out or os.path.join(folder, "transcription.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build transcription.txt: the whole video's speech, segment by "
                    "segment (reading order, timings, word counts).")
    ap.add_argument("folder",
                    help="fragmentation folder (e.g. shoulder/fragmentation) or the project "
                         "contains it (e.g. video1)")
    ap.add_argument("--out", default=None,
                    help="output file (default: <folder>/transcription.txt)")
    args = ap.parse_args()
    if not os.path.isdir(args.folder):
        sys.exit(f"Not a folder: {args.folder}")
    p = build(args.folder, args.out)
    print(f"Done -> {p}")
    print(f"  {sum(1 for _ in open(p, encoding='utf-8'))} lines, "
          f"{os.path.getsize(p)} bytes")


if __name__ == "__main__":
    main()
