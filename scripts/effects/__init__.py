"""
effects/ — ONE FILE PER EFFECT.

An effect is a module in this folder that provides:

    <name>(...)                 -> a DESCRIPTOR dict, built from the effect's own
                                   parameters (that is what goes in the plan:
                                   {"segments": [...], "effect": <name>(...)})
    command(ctx, desc, mp4, dst)-> the command that produces ONE camera file of ONE
                                   segment, or None when there is nothing to do
                                   (the segment is then copied unchanged)
    local_windows(...)          -> OPTIONAL: turns a window spanning several segments
                                   into each segment's local settings

Drop `effects/zoom.py` in here and `zoom(...)` is immediately usable in the plan —
there is nothing to edit in effects_workflow. Files starting with `_` are helpers and are
skipped. See `add_media.py` for a complete example.
"""

import importlib
import os
import pkgutil

HERE = os.path.dirname(os.path.abspath(__file__))


def load_effects() -> dict:
    """{name: module} for every effect module in this folder."""
    found = {}
    for info in pkgutil.iter_modules([HERE]):
        if info.name.startswith("_"):
            continue
        found[info.name] = importlib.import_module(f"{__name__}.{info.name}")
    return found


def make(desc: dict):
    """The module that knows how to render the effect described by `desc`."""
    name = (desc or {}).get("effect")
    mod = load_effects().get(name)
    if mod is None:
        raise SystemExit(f"unknown effect {name!r} — known effects: "
                         f"{', '.join(sorted(load_effects()))}")
    if not callable(getattr(mod, "command", None)):
        raise SystemExit(f"effects/{name}.py has no command() function")
    return mod


# Re-export every effect function so the plan only needs `from effects import add_media`
for _name, _mod in load_effects().items():
    _fn = getattr(_mod, _name, None)
    if callable(_fn):
        globals()[_name] = _fn
