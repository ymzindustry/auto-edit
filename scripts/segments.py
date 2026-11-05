#!/usr/bin/env python3
"""
segments.py — Shared segment discovery for the four segment stages.

A "segment folder" is a sub-folder of a stage folder that holds at least one video
(`.mp4` by default). The layout is ONE FOLDER PER SEGMENT with ONE FILE PER CAMERA
inside it:

    fragmentation/segment_3/main_3.mp4            <- the same instant of the timeline,
    fragmentation/segment_3/screenvideo_3.mp4     <- same duration, same audio, in sync
    fragmentation/segment_3/segment_3.txt         <- the transcript of that instant

playback order = <root>/order.txt when it exists (written by fragmentation_workflow.py), else a
natural sort. The transcript (.txt) is shared by the cameras of a segment: it is
named after the folder.

Used by greenscreen_workflow.py, background_workflow.py,
effects_workflow.py and final_workflow.py.

Run it directly to see the playback order (and therefore the numbers to use in
effects_workflow's plan):

    ./segments.py /path/to/folder/background

PERMISSIVE INPUTS: every stage takes either the project folder (`shoulder`, which
holds fragmentation/, greenscreen/, …), the stage folder itself
(`shoulder/fragmentation`), or ANY folder that directly contains
`segment_i/` folders full of `.mp4` + `.txt` — the name does not matter, so a stage can
be run on its own, in any order, without the previous ones. The old `wk1..wk5` names
are still accepted for reading. See `resolve_input()`.
"""

import os
import re
import shutil

ORDER_FILE = "order.txt"
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v")


def media_files(seg_dir: str) -> list:
    """The video files of one segment folder, sorted (one per camera).

    Scratch files are NEVER a camera: a killed encoding leaves the `.ab_tmp.mp4` the
    encoder was writing, `effects_workflow` leaves a `<name>_insert.mp4` / `.part.mp4`, and
    stage 1 leaves `preview_720p.mp4` beside the segments. Counting any of them as a
    camera would break the layout (and the final montage).
    """
    try:
        names = os.listdir(seg_dir)
    except OSError:
        return []
    return sorted(n for n in names
                  if n.lower().endswith(VIDEO_EXTS)
                  and not n.startswith(".")
                  and not n.lower().endswith("_insert.mp4")
                  and not n.lower().startswith("preview")
                  and ".part." not in n.lower())


def sidecar_txt(seg_dir: str) -> str:
    """The transcript of a segment: <folder>.txt, else the first .txt present."""
    name = os.path.basename(os.path.normpath(seg_dir))
    direct = os.path.join(seg_dir, name + ".txt")
    if os.path.isfile(direct):
        return direct
    txts = sorted(n for n in os.listdir(seg_dir) if n.lower().endswith(".txt"))
    return os.path.join(seg_dir, txts[0]) if txts else ""


def natural_key(name: str):
    """Sort key that compares digit runs as numbers (IMG_x_2 < IMG_x_10)."""
    return [int(tok) if tok.isdigit() else tok.lower()
            for tok in re.split(r"(\d+)", name)]


# THE STAGE FOLDERS, IN PIPELINE ORDER. Each stage writes its own name inside the
# project folder: fragmentation/ -> greenscreen/ -> background/ -> effects/ -> final/.
STAGES = ("fragmentation", "greenscreen", "background", "effects", "final")

# The old `wk1..wk5` names are still READ (existing projects keep working) but no
# stage writes them any more.
LEGACY = dict(zip(STAGES, ("wk1", "wk2", "wk3", "wk4", "wk5")))
ALL_STAGES = STAGES + tuple(LEGACY.values())


def looks_like_container(path: str) -> bool:
    """True when `path` DIRECTLY holds segment folders with videos inside.

    That is the real definition of a usable stage input: a folder whose sub-folders
    are `segment_i/` with `.mp4` + `.txt` in them. It says nothing about its name, so
    any folder can be fed to a stage — you are not forced to use the stage names
    """
    try:
        entries = os.listdir(path)
    except OSError:
        return False
    return any(media_files(os.path.join(path, e)) for e in entries
               if os.path.isdir(os.path.join(path, e)))


def resolve_input(path: str, in_name: str, out_name: str, out_arg: str | None = None):
    """Be PERMISSIVE about what a stage is given. Returns (container, project, out_dir).

        <proj>/<in_name>            -> container = that folder;   out = <proj>/<out_name>
        <proj>  (with <in_name>/)   -> container = <proj>/<in_name>; out = <proj>/<out_name>
        <any>/<dir> holding segment_*/  -> container = that dir;   out = <parent>/<out_name>
        <any>/<dir>/segment_3       -> its container is used (handy for a one-off)
        anything + out_arg          -> the output goes to out_arg instead

    So a stage runs on ANY folder that holds `segment_i/*.mp4` + `.txt`, whatever its
    name, and the stages no longer have to be run in order inside one project folder.
    """
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        raise SystemExit(f"Not a folder: {p}")
    base = os.path.basename(p.rstrip(os.sep))
    if base.startswith("segment_") and looks_like_container(os.path.dirname(p)):
        p = os.path.dirname(p)                          # a single segment was handed over
        base = os.path.basename(p)
    if base == in_name or base == LEGACY.get(in_name) or looks_like_container(p):
        container, project = p, os.path.dirname(p.rstrip(os.sep))
    else:
        candidate = os.path.join(p, in_name)
        legacy = LEGACY.get(in_name)
        if not os.path.isdir(candidate) and legacy and os.path.isdir(os.path.join(p, legacy)):
            candidate = os.path.join(p, legacy)          # project from before the rename
            print(f"  (reading the legacy folder '{legacy}' instead of '{in_name}')")
        if not os.path.isdir(candidate):
            raise SystemExit(
                f"{p} contains no segment folder (a segment folder holds "
                f"<name>/<name>.mp4) and no '{in_name}/' to read.\n"
                f"Give me either the folder holding the segments themselves, or a "
                f"project folder with '{in_name}/' (or the legacy "
                f"'{LEGACY.get(in_name, in_name)}/') inside it.")
        container, project = candidate, p
    out = os.path.abspath(out_arg) if out_arg else os.path.join(project, out_name)
    return container, project, out


def segment_names(root: str) -> list:
    """Segment folder names inside `root`, in playback order."""
    order = os.path.join(root, ORDER_FILE)
    if os.path.isfile(order):
        names = []
        with open(order, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and os.path.isdir(os.path.join(root, line)):
                    names.append(line)
        if names:
            return names

    names = [d for d in os.listdir(root)
             if os.path.isdir(os.path.join(root, d))
             and media_files(os.path.join(root, d))]
    return sorted(names, key=natural_key)


def segment_dirs(root: str) -> list:
    """Absolute paths of the segment folders inside `root`, in playback order."""
    return [os.path.join(root, n) for n in segment_names(root)]


def write_order(root: str, names: list) -> str:
    """Persist the playback order of `root` (so the next stage keeps it)."""
    path = os.path.join(root, ORDER_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# playback order — one segment folder per line "
                "(written by fragmentation_workflow.py; do not reorder by hand unless you mean it)\n")
        for n in names:
            f.write(n + "\n")
    return path


def renumber_outputs(root: str) -> list:
    """Rename every segment folder of `root` to segment_0, segment_1, ... in order.

    A stage that fuses segments writes folders like `segment_17_26/`; the montage must
    not care. The folders AND the files inside are renamed together (`main_17_26.mp4`
    -> `main_3.mp4`), so the layout stays `<name>/<camera>_<index>.mp4`, and order.txt
    is rewritten. Returns the new names, in playback order.
    """
    names = segment_names(root)
    if not names:
        return []
    tmp_map = []
    for i, name in enumerate(names):
        tmp = os.path.join(root, f".renumber_{i}")
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        os.rename(os.path.join(root, name), tmp)
        tmp_map.append((i, name, tmp))

    final_names = []
    for i, old, tmp in tmp_map:
        new = f"segment_{i}"
        dest = os.path.join(root, new)
        os.rename(tmp, dest)
        for entry in sorted(os.listdir(dest)):
            src = os.path.join(dest, entry)
            if not os.path.isfile(src):
                continue
            stem, ext = os.path.splitext(entry)
            if ext.lower() not in VIDEO_EXTS and ext.lower() != ".txt":
                continue
            if ext.lower() == ".txt":
                target = os.path.join(dest, new + ".txt")
            else:
                prefix = re.sub(r"(_\d+)+$", "", stem) or stem
                target = os.path.join(dest, f"{prefix}_{i}{ext}")
            if src != target:
                os.replace(src, target)
        final_names.append(new)
        if old != new:
            print(f"  {old} -> {new}")
    write_order(root, final_names)
    return final_names


def copy_order(src_root: str, dst_root: str) -> None:
    """Carry order.txt from one stage to the next when it exists."""
    src = os.path.join(src_root, ORDER_FILE)
    if os.path.isfile(src):
        names = segment_names(src_root)
        write_order(dst_root, names)


if __name__ == "__main__":
    # Small utility: show the playback order of a wk folder. It is also the answer
    # to "which number do I put in the effects plan?" — the first column
    # is exactly what a "segments": [1, 2] entry refers to.
    import argparse
    ap = argparse.ArgumentParser(
        description="List the segment folders of a stage folder in playback order.")
    ap.add_argument("folder", help="A stage folder (e.g. <project>/background, <project>/fragmentation)")
    args = ap.parse_args()

    root = os.path.abspath(args.folder)
    if not os.path.isdir(root):
        raise SystemExit(f"Not a folder: {root}")
    names = segment_names(root)
    if not names:
        raise SystemExit(f"No segment folder in {root} (a segment folder holds "
                         f"<name>/<name>.mp4)")
    src = "order.txt" if os.path.isfile(os.path.join(root, ORDER_FILE)) else "name sort"
    print(f"{len(names)} segment(s) in playback order (from {src}):")
    for i, n in enumerate(names, 1):
        print(f"  {i:3d}  {n}")
