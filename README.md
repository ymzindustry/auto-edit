# auto-edit — a pipeline for machine-made video editing

An experiment: **can a language model edit video like a human editor?**

Not with a bigger model — with a better substrate. Today's models can already reason about
a transcript, a shot list and a story. What they cannot do is *touch the pixels*: they have
no reliable vocabulary for "cut out the silence", "put the phone screen next to the face",
"push in slowly on the last sentence", and no way to check whether the result is any good.

This repository is that vocabulary. It turns editing decisions into **deterministic,
inspectable, verifiable operations** on top of ffmpeg, and it keeps every frame at the
resolution it was shot in. An agent (or a human) writes an edit as a small Python file —
a *plan* — and the pipeline executes it, reporing what it did at each step.

The bet: when models get good enough to *decide* the edit, the execution layer they need
will already exist, and it will be honest. Until then, the same layer is useful as-is: it
does the tedious 80 % (silence removal, synchronisation, subtitles, reframing, overlays,
zooms, assembly) at a speed and a consistency a human editor cannot sustain.

---

## The five stages

Each stage reads a folder, writes a folder named after itself, and never overwrites its
input. Every stage is one file, there are no aliases or side copies.

| stage | module | what it does |
|---|---|---|
| 1. fragmentation | `scripts/workflows/fragmentation_workflow.py` | cuts the footage on the silences, synchronises the cameras on the audio, transcribes, writes every segment with the same duration and one master audio track, then builds a lightweight preview |
| 2. greenscreen | `scripts/workflows/greenscreen_workflow.py` | AI segmentation (`u2net` via `rembg`) — mattes a subject onto a green background |
| 3. background | `scripts/workflows/background_workflow.py` | replaces the green with any image or video |
| 4. effects | `scripts/workflows/effects_workflow.py` | applies a **plan** of effects (overlays, push-in, side-by-side, zoom) segment by segment |
| 5. final | `scripts/workflows/final_workflow.py` | glues the segments back together and lays a music bed under the voice |

```
project/
  main.mp4  screen.mp4  audio.wav
  fragmentation/segment_0/main_0.mp4  screen_0.mp4  segment_0.txt
  fragmentation/order.txt
  greenscreen/…   background/…   effects/…   final/final.mp4
```

`order.txt` carries the playback order from stage to stage; segments are always
`segment_i/<camera>_i.mp4` + a transcript sidecar, whatever happened upstream.

## The effects engine and the plan file

`effects_workflow.py` knows **no effect**. It loads a plan — a plain Python file — and
applies it. The plan is the whole job: it carries the effects, the folder to read and the
folder to write.

```python
# scripts/plans/example.py
INPUT  = "../video-projects/my-project/background/"
OUTPUT = "../video-projects/my-project/effects/"

from effects import add_media, side_by_side, zoom

PLAN = [
    {"segments": range(0, 12),
     "effect": zoom(zoom=1.3, start=1.0, end=2.0, each=True, min=2)},

    {"segments": [13, 14, 15],
     "effect": add_media(src="../clips/bench.mp4", pct=70, blur=20,
                         fade_in=0.4, fade_out=0.4, fit="cut")},

    {"segments": "all",              # or omit "segments" with all=True in the effect
     "effect": side_by_side(position="left", height_pct=100)},
]
```

```bash
python scripts/workflows/effects_workflow.py scripts/plans/example.py --dry-run
python scripts/workflows/effects_workflow.py scripts/plans/example.py
```

An effect is one module in `scripts/effects/`, auto-discovered:

```python
def my_effect(param, ...) -> dict       # returns the descriptor stored in the plan
command(ctx, desc, src, dst) -> list    # returns the ffmpeg argv, or None to copy
local_windows(clips, indices, desc)     # optional: spread the effect over segments
merged_window(total, desc)              # optional: window on a fused range
```

Five effects ship today: `add_media` (overlay a demo clip), `push_video` (one camera
slides in and pushes the other), `side_by_side` (both cameras, nothing cropped), `zoom`
(push-in), and `corner_reveal` — a window of the camera slides into a corner, shrinks,
and comes back while a second media fills the frame (used for "here is the screen"
moments, with an optional border and a blur that fades out so the shot returns cleanly).

## Design principles

- **Native resolution in, native resolution out.** 4K stays 4K. Nothing is downscaled "to
  be safe", nothing is upscaled to fake quality. Preview files are the only exception and
  they are explicit.
- **One implementation per operation.** A stage is one file. Duplicated pipelines drift and
  the drift is invisible until a render is wasted.
- **The plan is the whole job.** Inputs, outputs and effects live in the plan, so a run is
  reproducible from one path. `--dry-run` prints the resolved plan and takes no lock, so a
  plan can be inspected while a render is running.
- **Deterministic before generative.** Every step is ffmpeg with explicit parameters. No
  model decides pixels at render time: a model may *choose* the plan, never silently
  interpolate the output.
- **Measure, don't trust.** The pipeline verifies what it produced: durations against the
  source, decode errors, zoom ramps measured frame by frame, timescale uniformity before a
  concat. "It looked fine" is not a check.
- **Outputs are anonymous.** Fused ranges are renumbered `segment_0..N`; the folder never
  leaks how the montage was built.

Two bugs found this way, both invisible in a quick eyeball check:

- `zoompan` snaps its crop to whole pixels, so a slow push-in emitted a 1 px back-jump
  every few frames — a "shaking camera". The zoom now uses `perspective` with
  `eval=frame` and cubic interpolation: a real sub-pixel warp.
- stream-copying segments preserved the source timescale (`1/90000`) while re-encoded ones
  got `1/12800`; concatenating both produced a film that plays, then freezes for the rest
  of its length. Copies are now re-muxed to a single timescale, and the concat checks it.

## Quick start

```bash
bash scripts/setup.sh                        # three venvs, everything inside the profile
python scripts/workflows/fragmentation_workflow.py path/to/project
python scripts/workflows/effects_workflow.py scripts/plans/example.py --dry-run
python scripts/workflows/effects_workflow.py scripts/plans/example.py
python scripts/workflows/final_workflow.py path/to/project assets/music.mp3
```

Requirements: Python 3.11, `ffmpeg` (bundled through `imageio-ffmpeg`), `opencv`, `numpy`,
`rembg` for the segmentation stages, `faster-whisper` for the transcription. See
`requirements.txt`.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — the contract: stage I/O, segment layout,
  window semantics, effect API, the plan format, and the pitfalls that cost real renders.
- [`docs/roadmap.md`](docs/roadmap.md) — where this goes: from deterministic execution to
  model-planned editing.
- [`scripts/README.md`](scripts/README.md) — per-stage reference and options (French; the
  code and its comments are English).

## Status and honest limits

A research prototype, used on real footage (a 4K, 11-minute two-camera shoot).

- There is **no visual preview of the story**. The pipeline can show you a segment, not
  whether the montage works.
- It has **no taste**. Pacing, humour, when to hold a shot — that is the part a model (or a
  human) still has to bring.
- It is **not a replacement for an editor** on a cinematic project; it is a replacement for
  the mechanical part of the work, and a substrate for an editing agent.
- The stages assume a talking-head shoot with a screen recording: the segmentation,
  synchronisation and silence cutting are tuned for that shape.

## License

MIT — see [`LICENSE`](LICENSE).
