# Roadmap — from deterministic execution to model-planned editing

The pipeline is deliberately **dumb where it counts**: every operation is ffmpeg with
explicit parameters, and every result is measurable. That is what makes it a usable
substrate for a model later — a model choosing among operations it cannot verify is a
demo, not a tool.

```
 today                                   next                                  target
 ┌──────────────────┐        ┌───────────────────────┐        ┌──────────────────────────┐
 │ human writes the │        │ model drafts the plan │        │ model edits, humans      │
 │ plan by hand     │  ───▶  │ from transcript+media │  ───▶  │ review a preview         │
 │ pipeline executes│        │ pipeline executes     │        │ pipeline executes        │
 └──────────────────┘        └───────────────────────┘        └──────────────────────────┘
```

## 1. Done — the execution layer

- Five stages, one file each, native resolution preserved.
- Plan file as the unit of work: effects + I/O + segment selection in one place.
- Effects auto-discovered from a folder, with a stable contract
  (`descriptor`, `command`, `local_windows`, `merged_window`).
- Self-verification: duration checks, decode-error counts, measured zoom ramps, timescale
  uniformity before a concat, preview build with live progress.

This is what "the tools we develop now" means concretely: the vocabulary an editing agent
will speak.

## 2. Next — the model plans, the pipeline executes

The transcript is already produced by stage 1, with timestamps and per-segment sidecars.
That is enough for a first real step:

- **Selection.** Given `segment_0.txt … segment_N.txt`, ask a model to pick the segments
  that make a 6-minute video, and to justify each cut. Today this is done by hand.
- **Effect intent.** The model writes the plan in the same Python dialect a human uses:
  `zoom(1.3, each=True, min=2)` on the sentences that land, `add_media` where the speech
  describes a movement. The pipeline refuses overlapping entries, unknown parameters and
  plans that touch no segment — cheap guardrails that keep a hallucinated plan from
  wasting an hour of 4K encoding.
- **Dry-run as the model's feedback loop.** `--dry-run` already prints the resolved plan,
  the segments and the windows: a plan can be validated before a single frame is encoded.

Prerequisite: a **structured edit description** (JSON: segments, effect, parameters,
rationale) so a model can be evaluated against a reference edit rather than prose.

## 3. Later — measurable taste

Editing quality is not magic, it decomposes:

- **Pacing** — cut length distribution vs speech rate; dead air after a cut.
- **Continuity** — frame-to-frame stability (the `zoompan` jitter bug was caught this way:
  phase correlation between consecutive frames), timescale and colour consistency.
- **Speech coverage** — every sentence either kept or deliberately cut, no truncated word.
- **Attention** — where the eye goes: subject position, overlay size, when the frame
  changes.

Each of these is a number the pipeline can compute on a preview render. A critic model
scores the numbers, the planner revises, the pipeline re-renders. That closed loop — plan,
render, measure, revise — is the only path from "it renders" to "it is good".

## 4. Then — hand-off and interoperability

- Export an **EDL / FCPXML / OTIO** timeline so a human editor can open the result in
  Resolve and take over: the machine does the assembly, the human does the taste.
- Keep every stage idempotent and folder-based, so a model can re-run one step without
  redoing the rest.

## Non-goals

- Replacing a human editor on a cinematic project.
- Generating pixels with a model at render time: the output must stay reproducible from
  the plan and the sources alone.
- A GUI. The interface is a plan file, which is exactly what a model can write.
