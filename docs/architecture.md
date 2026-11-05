# Architecture — pipeline contract (segments, order, effects)

Read this before extending a stage or writing an effect.

## Stage I/O

Every stage takes a folder and writes a folder NAMED AFTER ITSELF inside it
(`fragmentation/`, `greenscreen/`, `background/`, `effects/`, `final/`).
`resolve_input(path, "background", "effects")` is deliberately permissive — it accepts:

| given | read |
|---|---|
| `<proj>` with `<proj>/background/` | `<proj>/background` |
| `<proj>/background` itself | itself |
| any folder whose subfolders hold video | that folder |
| `<proj>/background/segment_2` | its parent |
| a project still using `wk1..wk5` | that legacy folder (read-only) |

`looks_like_container()` decides case 3 with "a subfolder that directly holds media".
That heuristic is why a stray video at a stage root poisons discovery — a finished
`*_final.mp4` left inside a project folder is read as a third camera.

## Segments

```
wk1/segment_0/main_0.mp4      # one file per camera, same duration, same audio
wk1/segment_0/screen_0.mp4
wk1/segment_0/segment_0.txt   # transcript sidecar
wk1/order.txt                 # playback order, one folder per line
wk1/sync.json                 # offsets, confidences, gaps, per-camera plan
```

- Folders are numbered from `segment_0`; after empty segments are dropped the survivors
  are renumbered with no holes (`--keep-numbering` disables that, `original_name` in
  `sync.json` keeps the old index).
- A camera that was not rolling gets black frames, never a shorter file: equal durations
  are what keeps the angles paired.
- `order.txt` starts with a `#` comment line; the parser skips `#` and unknown names.
  The comment names the script that wrote it — keep that in sync with any rename.
- One segment may hold several cameras and even several source videos glued together
  when the cut straddles a boundary.
- **Output numbering is rewritten by the stage, not inherited.** `segments.renumber_outputs(root)`
  renames every output folder to `segment_0..N` in `order.txt` order (two-phase via a
  temp name, so no collision), renames the media inside (`<camera>_<i>.mp4`) and the
  `.txt` sidecar (`segment_<i>.txt`), then rewrites `order.txt`. Fused ranges therefore
  never leak into a folder name. Run the same helper on a folder produced by an older
  code path to normalise it in place — it reports each `old -> new` line.

## Effects

Reaching an effect's module-level helpers (`local_windows`, `merged_window`) in a test
takes `importlib.import_module("effects.zoom")`: the package `__init__` re-exports each
effect and OVERWRITES the module attribute, so `import effects.zoom as z` and
`from effects import zoom` both hand back the FUNCTION, and `z.local_windows` fails.

An effect is one module in `scripts/effects/`:

```python
def my_effect(param, ...) -> dict      # returns the descriptor stored in the plan
command(ctx, desc, src, dst) -> list   # ffmpeg argv, or None to copy unchanged
# optional:
local_windows(clips, indices, desc)     # spread over segments -> per-segment windows
merged_window(total, desc)              # window on a fused range
PAIR = True                             # consumes the segment's TWO camera files
```

`ctx` carries `scripts`, `venv_video`, `seg_dir`, `cams` (pair effects), `window`,
`index`, `name`, `dur`, `clips`. Effects are auto-discovered by `pkgutil`: dropping the
file registers it, the engine never imports it by hand.

Window semantics (the easy place to invert a sign):

- `start` = seconds into the FIRST segment of the range;
- `end` = seconds BEFORE THE END of the LAST segment, so absolute end = `dur - end`;
- `fusion=True` merges the range into one clip and calls the effect once (no restart at
  the joins, one output folder, `order.txt` rewritten);
- without `fusion`, an effect that spans segments must cover every segment in between,
  otherwise the overlay pops out during the middle one.

### Plan file

**The plan file is the whole job.** `effects_workflow.py --plan <file.py>` (the path also
works as the only positional argument) executes the file with every effect function
injected into its namespace and reads `PLAN`, plus optional `INPUT` (folder to read) and
`OUTPUT` (where the result goes). Plans live in `scripts/plans/`, where the user keeps
several variants and edits `INPUT`/`OUTPUT`/`PLAN` himself — read those values, never
re-derive them. Resolution order is CLI (`--input`/`--output`) > plan file > the stage's
CONFIG constants, and the run should print which one each value came from. The CONFIG's
`PLAN_FILE` is only the default plan; when neither it nor `--plan` gives one, the engine
must **stop with the list of available plans**, not fall back to an empty plan — a silent
empty PLAN looks like a successful run that changed nothing. `"segments"` accepts a list,
a `range`, a folder name, or `"all"`, and may be omitted entirely for an effect built
with `all=True`. `--dry-run` prints entries and the resolved windows without rendering
and takes **no lock** (so a plan can be inspected while a render is running); read its
`input :` / `output :` lines before every expensive run — that is what catches a wrong
folder before an hour of encoding.

### Two useful effects, and their semantics

- `side_by_side(base, element, position="left", height_pct=100, gap_pct=1, pad="black")`
  — no crop, no slide: the canvas WIDENS to `element + gap + base`, base keeps its native
  height, element is scaled to `height_pct` of it (portrait stays portrait), audio is the
  base's track. This is "screen next to the camera", not "screen on top of it".
- `push_video(base, element, direction, percent, pct, front, fade_in)` — `pct` is the
  element size across the entry axis; `percent` is the SHARE OF THE ELEMENT that lands on
  the base, so `push = element_size - overlap`: `0` = side by side, `100` = base not moved.
  The base gets cropped by construction — prefer `side_by_side` when nothing may be cut.
- `add_media(src, ..., fit="cut"|"speed")` — overlays an image/video on the camera.
  `fit` only matters when the media is LONGER than its window: `"cut"` (default) plays it
  and stops at the window end; `"speed"` compresses the whole media into the window
  (`setpts` on the picture, chained `atempo` on its own sound) so none of it is lost.
  A shorter media keeps looping in both modes. Report the computed factor so the user can
  sanity-check it (a 60 s clip in an 18 s window = x3.3: visible, not ridiculous).
- `zoom(zoom, start, end, crf, preset, fusion, only, each, min, all)` — linear push-in:
  **the move ALWAYS spans `start` → `dur - end`** (1.0 until `start`, ramp to `zoom` at
  `dur - end`, held after); nothing else controls its pacing. `each=True` restarts the
  move on every segment (fusion is then forced off). `min` is **only a selection filter**:
  the effect applies to segments **strictly longer** than `min` (a 2.00 s segment with
  `min=2` is skipped — compare with `<=`), everything shorter is copied untouched; on a
  fused range it applies to the range. `all=True` makes the entry itself complete — the
  plan entry may then have NO `"segments"` key and the engine selects every segment
  (`{"effect": zoom(zoom=1.3, each=True, min=2, all=True)}`).
  **Do not add a `duration` knob.** One was written to "fix" the pacing complaint below
  and the user rejected it twice: `start`/`end` are the contract, `min` is a filter, and
  an effect must not reinterpret either.
  **Pacing is the #1 complaint, not the filter.** With `start=0, end=0` the ramp fills the
  whole segment, so a 48 s clip moves 0,3 × over 48 s ≈ 0,6 %/s: the first seconds are
  visually frozen and the shot only "starts moving" near the end — the user reports it as
  "les vidéos bugent, figées au début puis ça se débloque", and short segments look fine,
  which is why only "certain" clips seem broken. Measure the z trajectory at several
  instants BEFORE hunting a bug, and diff consecutive frames (a real freeze gives
  identical frames, a slow ramp does not). If the trajectory matches the formula the
  maths is right and the move is simply too slow: report the numbers and let HIM choose
  `start`/`end`/`zoom` — do not rewrite the filter.
  **Do the push with `perspective` (`eval=frame`, `interpolation=cubic`), NOT `zoompan`.**
  zoompan snaps the crop to whole pixels, so a slow push-in emits a 1 px back-jump every
  few frames: the user reports a "shaking camera" ("l'effet zoom a un effet bizarre,
  comme si la caméra tremblait"). perspective warps with sub-pixel accuracy at the same
  native size. Measured the same way (phase correlation between consecutive frames, 4K,
  ramp over 33 s): zoompan shows a periodic −0.27 px spike every 6 frames, perspective a
  monotone +0.08 px per frame. Quantising z to even-crop widths does NOT help, and a ×32
  quantisation makes it worse (3 px steps).
  Implemented with `perspective` (`x0..y3` from `W/2 ∓ W/(2*z)`, `z` from `in/fps`),
  output keeps the native size. A window of `None` MUST make `command()` return None — the
  engine reads that as "copy this segment", so a fallback to the default window would
  silently zoom the very segments the effect was told to skip.
  Verify it numerically, not by eye: extract a frame and compare it with the SAME frame
  at t=0 cropped to the centre `w/z × h/z`; the matching z is the measured zoom (a correct
  ramp reads 1.00 at the start, the target at the end, and the same value on both sides
  of a fused join). Measure at three instants and print the expected value next to the
  measured one. Cost at 3840×2160 / 50 fps, `preset=medium`: ~11 fps of output, so ~15 s
  of wall clock for a 3.3 s clip and ~50 min for 26 segments / 684 s — quote that before
  starting a full pass.

## Final assembly

- **Every copy in a segment folder must keep ONE timescale.** `-c copy` preserves the source
  timescale (1/90000 for camera footage, 1/12800 for what libx264 writes), so a folder
  mixing re-encoded and copied segments produces a concat that plays, then FREEZES for the
  rest of the film ("au début ça marche puis ça freez jusqu'à la fin"). Two fixes, both
  cheap: re-mux every copy with `-c copy -video_track_timescale 12800` (no re-encode), and
  make the concat's homogeneity signature include the video timescale — differ only by
  timescale → normalise then copy; differ by codec/size/fps/pix_fmt → re-encode.
  Diagnose with `ffprobe -show_entries stream=time_base` across the folder before blaming
  the player. Verify a concat by decoding windows near the start, middle and end AND by
  diffing two consecutive frames there (identical frames = a real freeze).

## Audio

One master track for the whole montage: the `.wav`, or `main.mp4`'s extracted audio when
no wav exists. Every output file carries exactly ONE audio stream taken from the enhanced
master at the mapped instant (`-map 1:a`, never a mix of the camera tracks). Camera audio
is used only for the cross-correlation that finds the offset, then discarded — mixing
them would double the voice and echo.
