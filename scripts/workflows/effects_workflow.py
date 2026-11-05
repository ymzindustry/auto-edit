#!/home/ymz/.hermes/profiles/videoeditor/venv_video/bin/python
"""
effects_workflow.py — <input>/ -> effects/ (a plan of effects is applied)

    python3 scripts/workflows/effects_workflow.py [--plan FILE.py] [--input FOLDER]
                                                 [--output DIR] [--resume] [--dry-run]

WHAT IT DOES
  Reads a PLAN (a .py file that builds a list of effects) and applies each entry to the
  segments it names. Segments no entry names are copied unchanged. fusion=True joins the
  target segments into ONE clip first, so the effect runs once and never restarts at a
  join. Two runs on the same output folder are refused (a lock file). Whatever the plan
  does, the output folders are renumbered segment_0, segment_1, ... in playback order:
  a fused range becomes ONE segment_N/segment_N.mp4, never "segment_17_26".

CONFIG (top of this file — DEFAULTS ONLY: a plan file usually sets INPUT and OUTPUT
itself, so `effects_workflow.py --plan my_plan.py` is enough)
  INPUT      folder to read: the project (reads background/), a stage folder, or any
             folder holding segment folders
  OUTPUT     where effects/ goes (None = next to the project)
  PLAN_FILE  the plan loaded when --plan is not given ('' = none, then --plan is required)

OPTIONS
  --plan FILE.py        the plan to apply (or give the path as the only argument): it
                        carries PLAN, and may carry INPUT and OUTPUT
  --input FOLDER        segments to read
  --output DIR          where the result goes
  --resume              keep what is already rendered (a segment whose output has the
                        right duration is skipped): use it after a killed run
  --dry-run             print the resolved plan and stop

PLAN FORMAT
  from effects import add_media, push_video, side_by_side, zoom
  PLAN = [{"segments": [17, 18], "effect": add_media(src="../clips/bench.mp4", pct=70)}]
  "segments" = folder names, positions in order.txt, a range, or "all". An effect
  with all=True (zoom) needs no "segments" key at all.
  Available effects: add_media (image/video over the camera), push_video (slide one
  camera in), side_by_side (two cameras next to each other), zoom (push-in).

EXAMPLES
    python3 scripts/workflows/effects_workflow.py --dry-run
    python3 scripts/workflows/effects_workflow.py --plan scripts/shoulder_plan.py
    python3 scripts/workflows/effects_workflow.py --plan scripts/shoulder_plan.py --input shoulder
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
PROFILE = os.path.dirname(SCRIPTS)
_VENV_DIR = os.path.join(PROFILE, "venv_video")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (os.environ.get("_WK_REEXEC") != "1" and os.path.exists(_VENV_PY)
        and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR)):
    os.environ["_WK_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import importlib.util
import re
import shutil
import subprocess
import atexit
import tempfile
import time
from datetime import datetime

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
VENV_VIDEO = os.environ.get("VENV_VIDEO", os.path.join(PROFILE, "venv_video", "bin", "python"))

sys.path.insert(0, SCRIPTS)
import segments as seglib
import effects as effects_lib
from effects import add_media, push_video

EFFECTS = effects_lib.load_effects()


INPUT = "../video-projects/shoulder/effects/"

OUTPUT = "../video-projects/shoulder/effects2/"

PLAN_FILE = "../scripts/plans/shoulder_plan2.py"


PLAN = []


def load_plan_file(path: str):
    """Execute a plan file (.py) and return (PLAN, {"input": ..., "output": ...}).

    A plan file IS the whole job: besides PLAN it may set INPUT (the folder to read) and
    OUTPUT (where the result goes), so the run is just
    `effects_workflow.py --plan my_plan.py`.

    The file is plain Python: it can `from effects import add_media, push_video`
    (scripts/ is on sys.path) and compute its segments however it likes."""
    path = _resolve(path) or path
    if not os.path.exists(path):
        raise SystemExit(f"plan file not found: {path}")
    spec = importlib.util.spec_from_file_location("effect_plan_file", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load plan file: {path}")
    mod = importlib.util.module_from_spec(spec)
    for _n, _m in EFFECTS.items():
        _fn = getattr(_m, _n, None)
        if callable(_fn):
            setattr(mod, _n, _fn)
    sys.modules["effect_plan_file"] = mod
    spec.loader.exec_module(mod)
    plan = getattr(mod, "PLAN", None)
    if not isinstance(plan, list):
        raise SystemExit(f"{path} defines no PLAN list")
    cfg = {"input": getattr(mod, "INPUT", None), "output": getattr(mod, "OUTPUT", None)}
    print(f"plan file : {path}  ({len(plan)} entry/entries)")
    return plan, cfg


def acquire_lock(out_dir: str) -> str:
    """Refuse to start a SECOND run on the same output folder.

    Two concurrent runs write the same `<segment>/<cam>.part.mp4` and destroy each
    other's file (interleaved writes, broken bitstream) while both saturate the CPU.
    A lock file carrying the pid makes that impossible; a lock whose process is gone
    is stale and simply taken over.
    """
    os.makedirs(out_dir, exist_ok=True)
    lock = os.path.join(out_dir, ".effects.lock")
    if os.path.isfile(lock):
        try:
            with open(lock, encoding="utf-8") as fh:
                pid = int((fh.read() or "0").split()[0])
        except (ValueError, OSError):
            pid = 0
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            raise SystemExit(f"another effects run is already writing to {out_dir} "
                             f"(pid {pid}).\nWait for it, or kill it with: kill {pid}")
        print(f"  (taking over a stale lock left by pid {pid})")
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n")
    return lock


def run(cmd: list) -> None:
    """Run a command; for ffmpeg, keep a live progress line on screen.

    An effect on 4K footage can take minutes (blur + overlay + x264), and a silent
    terminal is indistinguishable from a hang: ffmpeg is asked for `-progress pipe:1`
    and its encoded time / speed are redrawn on one line.
    """
    print("  $ " + " ".join(cmd), flush=True)
    if not os.path.basename(cmd[0]).startswith("ffmpeg"):
        subprocess.run(cmd, check=True)
        return
    tty = sys.stderr.isatty()
    t0 = time.time()
    proc = subprocess.Popen(cmd + ["-nostats", "-progress", "pipe:1"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    stamp, speed = "00:00", ""
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time="):
            stamp = line.split("=", 1)[1].split(".")[0]
            if tty:
                sys.stderr.write(f"\r    encoded {stamp}  {speed}  "
                                 f"({time.time() - t0:.0f}s elapsed)      ")
                sys.stderr.flush()
        elif line.startswith("speed="):
            speed = line.split("=", 1)[1]
    err = (proc.stderr.read() or "").strip()
    code = proc.wait()
    if code != 0:
        raise SystemExit(f"ffmpeg failed (exit {code}):\n" + err[-2000:])
    if tty:
        sys.stderr.write(f"\r    encoded {stamp}  done in {time.time() - t0:.0f}s"
                         f"{' ' * 20}\n")
    else:
        print(f"    encoded {stamp}  done in {time.time() - t0:.0f}s", flush=True)


RESUME = False


def already_done(dest: str, src: str) -> bool:
    """True when `dest` exists and lasts as long as `src` (so a killed run can resume)."""
    if not (RESUME and os.path.exists(dest)):
        return False
    a, b = probe_duration(dest), probe_duration(src)
    return b > 0 and abs(a - b) <= 0.2


TIMESCALE = "12800"


def copy_media(src: str, dst: str) -> None:
    """Copy a segment that no effect touches, with a UNIFORM timescale.

    A plain file copy keeps the source timescale (1/90000 for footage, 1/12800 for what
    libx264 writes), and mixing both in one folder makes the final concat break: the film
    plays, then freezes. Re-muxing is a copy, it does not re-encode anything.
    """
    run([FFMPEG, "-y", "-i", src, "-c", "copy",
         "-video_track_timescale", TIMESCALE, "-movflags", "+faststart", dst])


def probe_duration(path: str) -> float:
    out = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def _resolve(path: str) -> str:
    """Make a relative path absolute, whichever way it was written in the CONFIG.

    Both styles work, so the config stays readable:
      "video1"                 -> <profile>/video1          (from the profile root)
      "../video1/wk_light"     -> <profile>/video1/wk_light (from this script's folder)
    """
    if not path:
        return path
    if os.path.isabs(path):
        return path
    if path.startswith(".."):
        return os.path.normpath(os.path.join(SCRIPTS, path))
    for base in (PROFILE, SCRIPTS):
        if os.path.exists(os.path.join(base, path)):
            return os.path.join(base, path)
    return os.path.join(PROFILE, path)


def seg_folder(root: str, ref, names: list) -> str:
    """Segment reference -> segment folder.

    `ref` is either the folder name itself, or the 1-based position of the segment in
    the playback order (order.txt, propagated from stage to stage). Legacy
    `segment_00X` folders still resolve by name.
    """
    if isinstance(ref, str) and not ref.strip().isdigit():
        path = os.path.join(root, ref.strip())
        if os.path.isdir(path):
            return path
        raise SystemExit(f"no segment folder '{ref}' in {root}")
    n = int(ref)
    for name in (f"segment_{n:03d}", f"segment_{n}"):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            return path
    if 1 <= n <= len(names):
        return os.path.join(root, names[n - 1])
    raise SystemExit(f"no segment {n} in {root} ({len(names)} segment(s) there)")


def camera_of(name: str) -> str:
    """main_3.mp4 -> main (which camera a file belongs to)."""
    return re.sub(r"_\d+$", "", os.path.splitext(os.path.basename(name))[0])


def seg_suffix(clips: list, i: int) -> str:
    """The number of a segment ("segment_3" -> "3"); the position if it has none, so
    the labels below follow the folder numbering (0-based since the user's change)."""
    tail = clips[i]["name"].rsplit("_", 1)[-1]
    return tail if tail.isdigit() else str(i + 1)


def fused_label(clips: list, first: int, last: int) -> str:
    """segment_1 + segment_2 -> "segment_1_2", whatever the numbering starts at."""
    return f"{clips[first]['name']}_{seg_suffix(clips, last)}"


def wanted(name: str, only) -> bool:
    """True when this camera file must be treated (an effect's `only` filter).

    Same rule as greenscreen_workflow's --only: `only="main"` keeps the files whose name starts
    with "main", a list keeps several prefixes, None keeps everything.
    """
    if not only:
        return True
    prefixes = [only] if isinstance(only, str) else list(only)
    return any(name.startswith(p) for p in prefixes)


def resolve_paths(desc: dict, mod) -> dict:
    """Make an effect's PATH parameters absolute (see _resolve).

    A plan writes src="../data/deadlift.mp4" the same way the CONFIG writes INPUT, so
    the same rule applies — without this, insert_media resolves the path from whatever
    directory the script happened to be launched in and answers "Media not found".
    """
    for key in getattr(mod, "PATHS", ()):
        if desc.get(key):
            desc[key] = _resolve(str(desc[key]))
    return desc


def merge_clips(paths: list, out: str) -> None:
    """Join clips of the SAME camera into a single file.

    Stream copy when possible (instant, no quality loss — the segments of one camera
    share their codec parameters); re-encodes only if that fails.
    """
    lst = out + ".list.txt"
    os.makedirs(os.path.dirname(lst) or ".", exist_ok=True)
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", lst, "-c", "copy", out], capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", lst, "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                        "-c:a", "aac", out], check=True)
    os.remove(lst)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="effects_workflow.py",
        description="Apply an effects PLAN to a folder of segments.")
    ap.add_argument("plan_path", nargs="?", default=None, metavar="FILE.py",
                    help="the plan file (same as --plan, for a positional call)")
    ap.add_argument("--plan", default=None, metavar="FILE.py",
                    help="the .py file holding the PLAN, INPUT and OUTPUT (default: "
                         "PLAN_FILE in the CONFIG block)")
    ap.add_argument("--input", default=None, metavar="FOLDER",
                    help="segments to read (default: INPUT in the CONFIG block)")
    ap.add_argument("--output", default=None, metavar="DIR",
                    help="where out_dir goes (default: OUTPUT in the CONFIG block)")
    ap.add_argument("--resume", action="store_true",
                    help="keep what is already rendered: a segment whose output exists "
                         "with the right duration is skipped (use it after a killed run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved plan and stop before rendering anything")
    args = ap.parse_args()

    global RESUME
    RESUME = bool(args.resume)

    plan_file = args.plan or args.plan_path or (_resolve(PLAN_FILE) if PLAN_FILE else None)
    plan_cfg = {}
    if plan_file and os.path.exists(_resolve(plan_file) or plan_file):
        PLAN_ACTIVE, plan_cfg = load_plan_file(plan_file)
    elif plan_file:
        raise SystemExit(f"plan file not found: {plan_file}")
    elif PLAN:
        PLAN_ACTIVE = PLAN
        print("plan file : none (inline PLAN of this file is used)")
    else:
        import glob as _glob
        found = sorted(os.path.relpath(p, PROFILE)
                       for p in _glob.glob(os.path.join(SCRIPTS, "plans", "*.py")))
        hint = ("\n  available: " + ", ".join(found)) if found else ""
        raise SystemExit("no plan to apply: pass --plan FILE.py" + hint)

    src_cfg = args.input or plan_cfg.get("input") or INPUT
    out_cfg = args.output or plan_cfg.get("output") or OUTPUT
    if args.input:
        origin_in = "--input"
    elif plan_cfg.get("input"):
        origin_in = "plan file"
    else:
        origin_in = "CONFIG"
    if not src_cfg:
        raise SystemExit("no input folder: set INPUT in the plan file, or pass --input FOLDER")
    src = _resolve(src_cfg)
    out_arg = _resolve(out_cfg) if out_cfg else None
    print(f"settings  : input from {origin_in}"
          f"{', output from the plan' if (not args.output and plan_cfg.get('output')) else ''}")

    src_dir, project, out_dir = seglib.resolve_input(src, "background", "effects", out_arg)
    if os.path.realpath(out_dir) == os.path.realpath(src_dir):
        raise SystemExit(
            f"output folder == input folder ({src_dir}).\n"
            f"  An effect never overwrites its own input: set OUTPUT to another folder "
            f"(e.g. ../video-projects/shoulder/effects2/) or pass --output DIR.")
    os.makedirs(out_dir, exist_ok=True)

    if args.dry_run:
        lock = None                    # a dry run writes nothing, so it needs no lock
    else:
        lock = acquire_lock(out_dir)
        atexit.register(lambda: os.path.isfile(lock) and os.remove(lock))
    seg_dirs = seglib.segment_dirs(src_dir)
    if not seg_dirs:
        raise SystemExit(f"No segment folder in {src_dir} — a segment folder holds "
                         f"<name>/<name>.mp4 (run background_workflow.py first)")
    seg_names = [os.path.basename(d) for d in seg_dirs]

    clips = []
    off = 0.0
    for sd in seg_dirs:
        name = os.path.basename(sd)
        media = [os.path.join(sd, f) for f in seglib.media_files(sd)]
        dur = probe_duration(media[0]) if media else 0.0
        clips.append({"dir": sd, "name": name, "media": media,
                      "txt": seglib.sidecar_txt(sd), "dur": dur, "start": off})
        off += dur
    pos_of = {c["name"]: i for i, c in enumerate(clips)}

    print(f"project   : {project}")
    print(f"input     : {src_dir}")
    print(f"output    : {out_dir}")
    print(f"segments  : {len(clips)}   (total {off:.2f}s)")
    print(f"plan      : {len(PLAN_ACTIVE)} entry/entries")
    print(f"effects   : {', '.join(sorted(EFFECTS)) or 'none found in effects/'}")

    plan = PLAN_ACTIVE
    owner, entries = {}, []
    for k, e in enumerate(plan, 1):
        if not isinstance(e, dict) or "effect" not in e:
            raise SystemExit(f"plan entry {k}: expected "
                             f"{{'segments': [...], 'effect': <fn>(...)}} — got {e!r}")
        desc = e["effect"]
        if not isinstance(desc, dict):
            raise SystemExit(f"plan entry {k}: 'effect' must be the CALL of an effect "
                             f"function (e.g. add_media(src=...)), got {desc!r}")
        mod = effects_lib.make(desc)
        desc = resolve_paths(dict(desc), mod)
        sel = "*" if (isinstance(desc, dict) and desc.get("all")) else e.get("segments", [])
        idxs = []
        if isinstance(sel, str):
            if sel not in ("all", "*"):
                raise SystemExit(f"plan entry {k}: segments must be a list, a range, "
                                 f"or \"all\" — got {sel!r}")
            idxs = list(range(len(clips)))
            sel = []
        for n in sel:
            nm = os.path.basename(seg_folder(src_dir, n, seg_names))
            if nm not in pos_of:
                raise SystemExit(f"plan entry {k}: {nm} is not in {src_dir}")
            idxs.append(pos_of[nm])
        idxs = sorted(set(idxs))
        if getattr(mod, "local_windows", None) and len(idxs) > 1:
            span = list(range(idxs[0], idxs[-1] + 1))
            if span != idxs:
                print(f"  ({len(span) - len(idxs)} segment(s) between "
                      f"{clips[idxs[0]]['name']} and {clips[idxs[-1]]['name']} are "
                      f"covered too, so the effect stays continuous)")
            idxs = span
        for i in idxs:
            if i in owner:
                raise SystemExit(f"plan entries {owner[i]} and {k} both touch "
                                 f"{clips[i]['name']} — overlapping effects on one "
                                 f"segment are not supported yet.")
            owner[i] = k

        ent = {"k": k, "desc": desc, "name": desc["effect"], "mod": mod, "idxs": idxs,
               "fusion": bool(desc.get("fusion")),
               "pair": bool(getattr(mod, "PAIR", False))}
        local = getattr(mod, "local_windows", None)
        ent["windows"] = local(clips, idxs, desc) if local else {i: None for i in idxs}
        entries.append(ent)

        print(f"\neffect {k}: {desc['name'] if 'name' in desc else desc['effect']}"
              f"  on {[clips[i]['name'] for i in idxs]}")
        for key in sorted(desc):
            if key != "effect":
                print(f"    {key} = {desc[key]}")
        for i in idxs:
            win = ent["windows"].get(i)
            if win is None:
                print(f"  ! {clips[i]['name']}: outside the effect window, left as is")
            else:
                print(f"  {clips[i]['name']}: " +
                      ", ".join(f"{kk} {vv:.2f}s" if isinstance(vv, float) else f"{kk} {vv}"
                                for kk, vv in win.items()))

    if args.dry_run:
        print("\ndry run — plan resolved, nothing rendered")
        return
    applied = copied = fused_files = passed = 0
    merged_idx = set()
    total_segs = len(clips)

    def tick(label: str, status: str, detail: str = "") -> None:
        """One line per segment, ALWAYS: `[12/64]  pass  segment_11` when nothing runs."""
        tag = f"[{label}/{total_segs}]"
        print(f"{tag:<11} {status:<28}{detail}", flush=True)

    with tempfile.TemporaryDirectory(dir=PROFILE) as tmp:
        for ent in entries:
            if not ent["fusion"] or len(ent["idxs"]) < 2:
                continue
            first, last = ent["idxs"][0], ent["idxs"][-1]
            label = fused_label(clips, first, last)
            dest = os.path.join(out_dir, label)
            os.makedirs(dest, exist_ok=True)
            os.makedirs(tmp, exist_ok=True)
            total = sum(clips[i]["dur"] for i in range(first, last + 1))
            cams = {}
            for i in range(first, last + 1):
                for f in clips[i]["media"]:
                    cams.setdefault(camera_of(f), []).append(f)
            print(f"\n[fuse] {label}: {len(cams)} camera(s), {total:.2f}s — the effect "
                  f"runs ONCE on the whole range")
            tick(f"{first + 1}-{last + 1}", f"{ent['name']} (fused)",
                 f"{last - first + 1} segments, {total:.1f}s")
            if ent["pair"]:
                merged = {cam: os.path.join(tmp, f"{cam}_merged.mp4") for cam in cams}
                for cam, files in cams.items():
                    merge_clips(files, merged[cam])
                base = next((p for cam, p in merged.items()
                             if cam.startswith(str(ent["desc"].get("base", "")))), "")
                out_mp4 = os.path.join(
                    dest, f"{ent['desc']['base']}_{seg_suffix(clips, first)}_"
                          f"{seg_suffix(clips, last)}.mp4")
                part = out_mp4 + ".part.mp4"
                win_fn = getattr(ent["mod"], "merged_window", None)
                ctx = {"scripts": SCRIPTS, "venv_video": VENV_VIDEO,
                       "seg_dir": os.path.dirname(base), "cams": merged,
                       "window": win_fn(total, ent["desc"]) if win_fn else None,
                       "index": first, "name": label, "dur": total, "clips": clips}
                print(f"[fuse] {label}/{os.path.basename(out_mp4)}: {ent['name']}")
                cmd = ent["mod"].command(ctx, ent["desc"], base, part)
                if cmd:
                    run(cmd)
                    os.replace(part, out_mp4)
                    applied += 1
                merged_idx.update(range(first, last + 1))
                continue
            for cam, files in cams.items():
                merged_in = os.path.join(tmp, f"{cam}_merged.mp4")
                merge_clips(files, merged_in)
                out_mp4 = os.path.join(
                    dest, f"{cam}_{seg_suffix(clips, first)}_{seg_suffix(clips, last)}.mp4")
                if not wanted(os.path.basename(files[0]), ent["desc"].get("only")):
                    print(f"[fuse] {label}/{os.path.basename(out_mp4)}: copied unchanged "
                          f"(only={ent['desc']['only']!r})")
                    shutil.copy2(merged_in, out_mp4)
                    copied += 1
                    fused_files += 1
                    continue
                win_fn = getattr(ent["mod"], "merged_window", None)
                ctx = {"scripts": SCRIPTS, "venv_video": VENV_VIDEO,
                       "seg_dir": os.path.dirname(merged_in),
                       "window": win_fn(total, ent["desc"]) if win_fn else None,
                       "index": first, "name": label, "dur": total, "clips": clips}
                part = out_mp4 + ".part.mp4"
                cmd = ent["mod"].command(ctx, ent["desc"], merged_in, part)
                if cmd:
                    print(f"[fuse] {label}/{os.path.basename(out_mp4)}: {ent['name']}")
                    run(cmd)
                    os.replace(part, out_mp4)
                    applied += 1
                else:
                    shutil.copy2(merged_in, out_mp4)
                    copied += 1
                fused_files += 1
            txts = [clips[i]["txt"] for i in range(first, last + 1) if clips[i]["txt"]]
            if txts:
                with open(os.path.join(dest, label + ".txt"), "w", encoding="utf-8") as fo:
                    for t in txts:
                        with open(t, encoding="utf-8") as fi:
                            fo.write(fi.read().rstrip() + "\n")
            merged_idx.update(range(first, last + 1))

        for i, c in enumerate(clips):
            if i in merged_idx:
                continue
            dest = os.path.join(out_dir, c["name"])
            os.makedirs(dest, exist_ok=True)
            os.makedirs(tmp, exist_ok=True)
            ent = next((e for e in entries if e["k"] == owner.get(i)), None)

            if ent is not None and ent["pair"]:
                cams = {camera_of(f): f for f in c["media"]}
                base = next((p for cam, p in cams.items()
                             if cam.startswith(str(ent["desc"].get("base", "")))), "")
                out_mp4 = os.path.join(
                    dest, f"{ent['desc']['base']}_{seg_suffix(clips, i)}.mp4")
                part = out_mp4 + ".part.mp4"
                ctx = {"scripts": SCRIPTS, "venv_video": VENV_VIDEO, "seg_dir": c["dir"],
                       "cams": cams, "window": ent["windows"].get(i), "index": i,
                       "name": c["name"], "dur": c["dur"], "clips": clips}
                tick(i + 1, f"{ent['name']}",
                     f"{c['name']} -> {os.path.basename(out_mp4)}")
                cmd = ent["mod"].command(ctx, ent["desc"], base, part)
                if cmd:
                    run(cmd)
                    os.replace(part, out_mp4)
                    applied += 1
                if c["txt"]:
                    shutil.copy2(c["txt"], os.path.join(dest, os.path.basename(c["txt"])))
                continue

            if not c["media"]:
                tick(i + 1, "pass (no media)", c["name"])
                passed += 1
            for mp4 in c["media"]:
                dest_mp4 = os.path.join(dest, os.path.basename(mp4))
                if already_done(dest_mp4, mp4):
                    tick(i + 1, "kept", f"{c['name']}/{os.path.basename(mp4)}")
                    copied += 1
                    continue
                cmd = None
                if ent is not None:
                    if not wanted(os.path.basename(mp4), ent["desc"].get("only")):
                        tick(i + 1, "pass (only)",
                             f"{c['name']}/{os.path.basename(mp4)} "
                             f"only={ent['desc']['only']!r}")
                        copy_media(mp4, dest_mp4)
                        copied += 1
                        continue
                    ctx = {"scripts": SCRIPTS, "venv_video": VENV_VIDEO, "seg_dir": c["dir"],
                           "window": ent["windows"].get(i), "index": i, "name": c["name"],
                           "dur": c["dur"], "clips": clips}
                    cmd = ent["mod"].command(ctx, ent["desc"], mp4, dest_mp4)
                if cmd:
                    tick(i + 1, f"{ent['name']}",
                         f"{c['name']}/{os.path.basename(mp4)}")
                    run(cmd)
                    applied += 1
                else:
                    tick(i + 1, "pass", f"{c['name']}/{os.path.basename(mp4)}")
                    passed += 1
                    copy_media(mp4, dest_mp4)
                    copied += 1
            if c["txt"]:
                shutil.copy2(c["txt"], os.path.join(dest, os.path.basename(c["txt"])))

    fused_ranges = [(e["idxs"][0], e["idxs"][-1],
                     fused_label(clips, e["idxs"][0], e["idxs"][-1]))
                    for e in entries if e["fusion"] and len(e["idxs"]) > 1]
    names_out, i = [], 0
    while i < len(clips):
        rng = next((r for r in fused_ranges if r[0] == i), None)
        if rng:
            names_out.append(rng[2])
            i = rng[1] + 1
        else:
            names_out.append(clips[i]["name"])
            i += 1
    seglib.write_order(out_dir, names_out)
    names_out = seglib.renumber_outputs(out_dir)

    print(f"\nDone -> {out_dir}")
    print(f"  {applied} file(s) processed by an effect, {copied} copied unchanged "
          f"({passed} segment(s) passed through untouched)")
    print(f"  {len(names_out)} folder(s) out of {len(clips)} segments in"
          + (f" ({len(fused_ranges)} fused range(s), order.txt rewritten)"
             if fused_ranges else " (one file per camera)"))


if __name__ == "__main__":
    main()
