#!/usr/bin/env python3
"""Measure the *shape* of real MWM commands in the capture corpus.

PURPOSE
-------
build_corpus.py collapses the noisy captures into distinct known-good frames;
analyze_shape.py adds the composition layer: for every distinct frame it
produces the token stream (via _mwm.describe_frame), masks each token's
argument values down to a *shape*, and then aggregates:

  * which phrase shapes real hardware actually emits (head byte + opcode
    sequence, with delays/timers/modifiers collapsed to placeholders) --
    the templates we should mimic when generating commands,
  * how colors and effects are combined in a single phrase -- the "magic
    incantation" patterns (close-group + color + timer + invoke + D0/D1
    pacing clauses) that park / wand captures evidence,
  * which color forms (both-solid / left-only / both-palette / per-ear
    palette / fused) ride alongside which effects and modifiers,
  * the frequency-weighted vs distinct-weighted usage so both the noisy
    show streams and the one-off controller taps are visible.

Everything is computed from the validated, checksum-clean frames only.

Requires: the _mwm library (shared bootstrap); no Home Assistant.

Usage::

    python3 tools/analyze_shape.py
    python3 tools/analyze_shape.py --files samples/EMLG000E_filtered.txt \
        samples/EMLG0026_filtered.txt --top 15
    python3 tools/analyze_shape.py --out /tmp/shape.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402
from build_corpus import build, DEFAULT_DIR  # noqa: E402

describe_frame = mwm.describe_frame


# ---------------------------------------------------------------------------
# Token -> shape: mask argument values, keep the opcode identity
# ---------------------------------------------------------------------------

def _token_shape(tok: str) -> str:
    """Reduce a describe_frame token to its opcode-level shape.

    Numeric arguments (colors, delays, timers, palette entries, group
    bounds, D0/D1/D2 payload bytes, effect indices) are masked to ``xx`` so
    variants of one instruction collapse into a single shape slot.
    """
    if tok.startswith("unknown "):
        return "op(" + tok.split(" ")[1].upper() + ")"
    m = re.match(r"^(D[0-2]) modifier \(([0-9a-fx, ]*)\)$", tok)
    if m:
        n = m.group(2).count(",") + 1
        return f"{m.group(1)}({'xx,' * (n - 1)}xx)"
    if tok.startswith("cycle timer "):
        return "timer(xx)"
    if tok.startswith("delay ~"):
        return "delay(xx)"
    if tok == "invoke pulse":
        return "eff(04)"
    if tok == "invoke fade out":
        return "eff(85)"
    if tok == "invoke fade up":
        return "eff(86)"
    m = re.match(r"^invoke effect (0x[0-9a-f]+)$", tok)
    if m:
        return "eff(" + m.group(1).upper()[2:].rjust(2, "0") + ")"
    m = re.match(r"^invoke (.*)$", tok)
    if m:
        return "eff(" + m.group(1) + ")"
    if tok.startswith("palette color ") and tok.endswith(", per-ear register"):
        return "paletteL"
    if tok.startswith("palette color "):
        return "paletteB"
    color = {
        "both ears off": "colB(00)",
        "both ears solid": "colB(6x)",
        "both ears blue": "colB(01)",
        "both ears green": "colB(02)",
        "both ears cyan": "colB(03)",
        "both ears red": "colB(04)",
        "both ears magenta": "colB(05)",
        "both ears yellow": "colB(06)",
        "both ears white": "colB(07)",
        "both ears blue-violet": "colB(p)",
        "single-ear off": "colL(00)",
        "left ear blue": "colL(01)",
        "left ear green": "colL(02)",
        "left ear cyan": "colL(03)",
        "left ear red": "colL(04)",
        "left ear magenta": "colL(05)",
        "left ear yellow": "colL(06)",
        "left ear white": "colL(07)",
        "left ear blue-violet": "colL(p)",
        "right ear blue": "colR(01)",
        "right ear green": "colR(02)",
        "right ear cyan": "colR(03)",
        "right ear red": "colR(04)",
        "right ear magenta": "colR(05)",
        "right ear yellow": "colR(06)",
        "right ear white": "colR(07)",
    }
    if tok in color:
        return color[tok]
    return {
        "close group range": "group-close",
        "group picker": "group-pick",
        "starting group range bound": "group-begin",
        "reset/override": "reset",
        "start immediately": "immediate",
        "stop motion": "stop",
        "clock write": "clock(xx)",
        "assert per-ear register": "per-ear-on",
        "relinquish per-ear register": "per-ear-off",
    }.get(tok, "?" + tok)


def _frame_shape(frame: bytes) -> list[str]:
    """Unmasked token stream -> shape vector (head first)."""
    d = describe_frame(frame)
    if not d or d.get("kind") in (None, "invalid") or not d.get("tokens"):
        return []
    head = "head(%.2X)" % frame[0]
    return [head] + [_token_shape(t) for t in d["tokens"]]


# ---------------------------------------------------------------------------
# Color-form + effect classification
# ---------------------------------------------------------------------------

_COLORS_EARS = {
    "colB": "both-solid", "colL": "left-solid", "colR": "right-solid",
    "paletteB": "both-palette", "paletteL": "left-palette",
}


def classify(r: dict) -> dict:
    """Return color-form / effect / timer-bucket for a corpus row."""
    shape = r["_shape"]
    color_forms = [s for s in shape if s.startswith("col") or
                   s in ("paletteB", "paletteL")]
    color_form = ",".join(_COLORS_EARS.get(s, s) for s in color_forms) or "none"
    effects = [s for s in shape if s.startswith("eff(")]
    effect = effects[0] if effects else "none"
    timers = [s for s in shape if s.startswith("timer(")]
    timer = timers[0] if timers else "none"
    mods = [s for s in shape if s.startswith(("D0(", "D1(", "D2("))]
    return {"color_form": color_form, "effect": effect, "timer": timer,
            "mods": tuple(mods)}


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(data: dict, top: int) -> dict:
    rows = data["corpus"]
    for r in rows:
        frame = bytes.fromhex(r["hex"])
        r["_shape"] = _frame_shape(frame)
        r["_cls"] = classify(r)

    # --- phrase shapes -------------------------------------------------
    shape_occ: Counter[tuple] = Counter()
    shape_distinct: Counter[tuple] = Counter()
    shape_example: dict[tuple, str] = {}
    for r in rows:
        shp = tuple(r["_shape"])
        if not shp:
            continue
        shape_occ[shp] += r["count"]
        shape_distinct[shp] += 1
        shape_example.setdefault(shp, r["hex"])

    def shape_block(counter, total, header):
        lines = [header]
        for shp, c in counter.most_common(top):
            pct = 100.0 * c / total
            lines.append(f"  {c:5d} ({pct:5.1f}%)  "
                         f"{' '.join(shp)}"
                         f"  e.g. {shape_example[shp]}")
        return "\n".join(lines)

    occ_total = sum(shape_occ.values())
    dist_total = sum(shape_distinct.values())

    # --- color-form x effect ------------------------------------------
    ce_occ: Counter[tuple] = Counter()
    ce_distinct: Counter[tuple] = Counter()
    ce_example: dict[tuple, str] = {}
    inc_pool: list[dict] = []
    for r in rows:
        c = r["_cls"]
        if c["color_form"] == "none":
            continue
        key = (c["color_form"], c["effect"], c["timer"])
        ce_occ[key] += r["count"]
        ce_distinct[key] += 1
        ce_example.setdefault(key, r["hex"])
        inc_pool.append({**r, "color_form": c["color_form"], "effect": c["effect"],
                         "timer": c["timer"], "mods": c["mods"]})

    # --- effect index frequency (occurrence weighted) -----------------
    eff_occ: Counter[str] = Counter()
    for r in rows:
        e = r["_cls"]["effect"]
        if e != "none":
            eff_occ[e] += r["count"]

    # --- timer / modifier clauses -------------------------------------
    timer_occ: Counter[str] = Counter()
    mod_occ: Counter[tuple] = Counter()
    for r in rows:
        timer_occ[r["_cls"]["timer"]] += r["count"]
        if r["_cls"]["mods"]:
            mod_occ[r["_cls"]["mods"]] += r["count"]

    return {
        "summary": {
            "sources": {str(k): v["decoded"]
                        for k, v in data["sources"].items()},
            "total_validated_frames": data["total_validated_frames"],
            "distinct_commands": data["distinct_commands"],
        },
        "phrase_shapes": {
            "header": "Phrase shape (head + masked opcode sequence), "
                      "occurrence- and distinct-weighted",
            "by_occurrence": [{"shape": " ".join(s), "count": c,
                               "pct": round(100.0 * c / occ_total, 1),
                               "example": shape_example[s]}
                              for s, c in shape_occ.most_common(top)],
            "by_distinct": [{"shape": " ".join(s), "count": c,
                             "pct": round(100.0 * c / dist_total, 1),
                             "example": shape_example[s]}
                            for s, c in shape_distinct.most_common(top)],
        },
        "incantations": {
            "header": "Color-form x effect x timer -> occurrences (occurrence "
                      "weighted)",
            "groups": [{"color_form": k[0], "effect": k[1], "timer": k[2],
                        "count": v, "distinct": ce_distinct[k],
                        "example": ce_example[k]}
                       for k, v in ce_occ.most_common(top)],
        },
        "effect_frequency": [{"effect": e, "count": c}
                             for e, c in eff_occ.most_common()],
        "timer_frequency": [{"timer": t, "count": c}
                            for t, c in timer_occ.most_common() if t != "none"],
        "modifier_frequency": [{"mods": " ".join(m), "count": c}
                               for m, c in mod_occ.most_common(top)],
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render(an: dict) -> str:
    s = an["summary"]
    lines = ["MWM command-shape analysis", "=" * 72]
    lines.append(
        f"frames: {s['total_validated_frames']}  distinct: {s['distinct_commands']}"
    )
    for src, n in sorted(s["sources"].items()):
        lines.append(f"  {Path(src).name:32s} decoded={n}")
    lines.append("")

    ps = an["phrase_shapes"]
    lines.append(ps["header"])
    lines.append("-- by occurrence --")
    for g in ps["by_occurrence"]:
        lines.append(f"  {g['count']:5d} ({g['pct']:5.1f}%)  "
                     f"{g['shape']}  e.g. {g['example']}")
    lines.append("-- by distinct command --")
    for g in ps["by_distinct"]:
        lines.append(f"  {g['count']:5d} ({g['pct']:5.1f}%)  "
                     f"{g['shape']}  e.g. {g['example']}")
    lines.append("")

    inc = an["incantations"]
    lines.append(inc["header"])
    for g in inc["groups"]:
        lines.append(
            f"  {g['count']:5d}  color={g['color_form']:14s}  "
            f"{g['effect']:12s}  timer={g['timer']:8s}  "
            f"n={g['distinct']:3d}  e.g. {g['example']}")
    lines.append("")

    lines.append("Effect index frequency (occurrence weighted):")
    for g in an["effect_frequency"]:
        lines.append(f"  {g['count']:5d}  {g['effect']}")
    if an["timer_frequency"]:
        lines.append("Timer clause frequency:")
        for g in an["timer_frequency"]:
            lines.append(f"  {g['count']:5d}  {g['timer']}")
    if an["modifier_frequency"]:
        lines.append("Modifier (D0/D1/D2) clause frequency:")
        for g in an["modifier_frequency"]:
            lines.append(f"  {g['count']:5d}  {g['mods']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure the shape of MWM commands in the capture corpus")
    parser.add_argument("files", nargs="*", help="capture files (default: samples/)")
    parser.add_argument("--glob", default=None)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    search = [Path(p) for p in args.files] or [DEFAULT_DIR]
    paths: list[Path] = []
    for base in search:
        if base.is_dir():
            paths.extend(sorted(base.glob(args.glob or "*")))
        elif base.is_file():
            paths.append(base)

    data = build(paths)
    an = analyze(data, args.top)

    if args.format == "json":
        out = json.dumps(an, indent=2, default=str)
        if args.out:
            Path(args.out).write_text(out + "\n")
            print(f"Wrote shape analysis to {args.out}", file=sys.stderr)
        else:
            print(out)
    else:
        text = render(an)
        if args.out:
            Path(args.out).write_text(text + "\n")
            print(f"Wrote shape analysis to {args.out}", file=sys.stderr)
        else:
            print(text)


if __name__ == "__main__":
    main()