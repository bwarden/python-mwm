#!/usr/bin/env python3
"""Detect and decode A-B-A' command bundles in the MWM capture corpus.

PURPOSE
-------
Offline (no rig, no MQTT) analysis of the A-B-A' transmission pattern that
wands, paintbrushes and ears emit for every button push (see docs/
mwm-show-protocol.md section 3).  A button push is transmitted three times:

    A           the command phrase itself
    B           a companion/parameter phrase (effect index, timers, clock)
    A'          an exact repeat of A (modulo a rolling counter+CRC tail)

The park recordings (samples/MRDF0007.TXT, samples/MRDF0008.TXT) capture
the whole 2400-baud IR stream with per-frame millisecond offsets, so the
triple is recoverable by temporal clustering rather than by scanning for
literally-consecutive frames (show transmitters interleave other messages
between the three copies).

This tool:
  1. loads the timestamped frame stream(s),
  2. detects A-B-A' triples with a time-windowed matcher and hands each
     triple to _mwm.describe_bundle() for decoding,
  3. reports every bundle with its A phrase, B companion, A' repeat, the
     inter-frame gaps, and the decoded parameters,
and, given --analyze, aggregates the decoded data (which companion B frames
pair with which A phrases, which effect indices/opcodes the companions
carry, timing distribution) so patterns across the corpus surface.

Requires: the _mwm library (shared bootstrap); no Home Assistant.

Usage::

    python3 tools/analyze_bundles.py samples/MRDF0007.TXT samples/MRDF0008.TXT
    python3 tools/analyze_bundles.py samples/MRDF0007.TXT --analyze
    python3 tools/analyze_bundles.py samples/MRDF0007.TXT --format json \
        --out bundles.json
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

describe_bundle = mwm.describe_bundle
describe_frame = mwm.describe_frame

# ---------------------------------------------------------------------------
# Capture loaders
# ---------------------------------------------------------------------------

_MRDF_RE = re.compile(r"^([\d/]+ [\d:.]+):([0-9A-Fa-f]{8}):(.*)$")


def load_mrdf(path: Path) -> list[dict]:
    """Load a "Mouse Ear Recorder Text Data File" (park recap) capture.

    Each entry carries the recorded wall-clock timestamp, a 32-bit
    millisecond offset, and one (or more '+'-joined) hex frames.  Returns a
    list of ``{ms, wall, hex}`` dicts.
    """
    entries: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        m = _MRDF_RE.match(line)
        if not m:
            continue
        wall, ms_hex, hexpart = m.groups()
        ms = int(ms_hex, 16)
        for part in hexpart.split("+"):
            packed = part.strip().replace(" ", "")
            if packed:
                entries.append({"ms": ms, "wall": wall, "hex": packed.upper()})
    return entries


def load_stream(paths: list[Path]) -> list[dict]:
    """Load all given captures into one time-ordered (ms, hex) stream.

    Where two files share a millisecond counter (each recorder restarts it),
    the later file's offsets are shifted so the merged stream stays
    monotonic.
    """
    streams = [load_mrdf(p) for p in paths]
    merged: list[dict] = []
    shift = 0
    last_ms = -1
    for stream in streams:
        start = stream[0]["ms"] if stream else 0
        for entry in stream:
            ms = entry["ms"] + shift
            if ms < last_ms:
                # Clock jumped backwards across captures: push this whole
                # stream past the previous end.
                shift += last_ms - ms + 1
                ms = entry["ms"] + shift
            entry["ms"] = ms
            last_ms = ms
            merged.append(entry)
    merged.sort(key=lambda e: e["ms"])
    return merged


# ---------------------------------------------------------------------------
# A-B-A' detection
# ---------------------------------------------------------------------------

def _is_phrase(hex_str: str) -> bool:
    """A plausible MWM phrase (0x9x/0xF0x header), not a 55 AA sysmsg."""
    try:
        b = bytes.fromhex(hex_str)
    except ValueError:
        return False
    if not b:
        return False
    hdr = b[0]
    return (hdr & 0xF0) == 0x90 or (hdr & 0xF0) == 0xF0


def _matches_repeat(a: bytes, c: bytes) -> bool:
    """Whether two frames look like A and A' (repeat allow a rolling tail).

    Mirrors _mwm.describe_bundle's criterion: same length and header, same
    body except possibly the trailing counter+CRC pair on longer phrases.
    Park captures repeat A exactly (A == A'); real pole/rig hardware keeps a
    rolling counter, so the trailing counter+CRC pair may also differ.
    """
    if len(a) != len(c) or a[0] != c[0]:
        return False
    if a == c:
        return True
    tail = -2 if len(a) >= 5 else None
    return a[:tail] == c[:tail]


def detect_bundles(
    stream: list[dict],
    *,
    b_max_ms: int = 150,
    a_max_ms: int = 600,
) -> list[dict]:
    """Find A-B-A' triples in an ordered, timestamped frame stream.

    For each frame considered as a candidate ``A`` (a phrase, not a 55 AA
    system message), look:
      * forward up to ``b_max_ms`` for a *different* companion ``B``, then
      * forward from ``B`` up to ``a_max_ms`` for a repeat ``A'``.
    The first valid (A, B, A') triple wins for that A; overlapping triples
    are skipped so one button push is reported once.
    """
    n = len(stream)
    bundles: list[dict] = []
    used: set[int] = set()

    for i in range(n - 2):
        if i in used:
            continue
        a_hex = stream[i]["hex"]
        if _is_phrase(a_hex) and bytes.fromhex(a_hex)[0] != 0x55:
            a = bytes.fromhex(a_hex)
        else:
            continue
        a_ms = stream[i]["ms"]

        for j in range(i + 1, n):
            if stream[j]["ms"] - a_ms > b_max_ms:
                break
            if j in used:
                continue
            if stream[j]["hex"] == a_hex:
                continue
            if not _is_phrase(stream[j]["hex"]):
                continue
            b = bytes.fromhex(stream[j]["hex"])
            if b[0] == 0x55:
                continue
            b_ms = stream[j]["ms"]

            for k in range(j + 1, n):
                if stream[k]["ms"] - b_ms > a_max_ms:
                    break
                if k in used:
                    continue
                c = bytes.fromhex(stream[k]["hex"])
                if _matches_repeat(a, c):
                    desc = describe_bundle([a, b, c])
                    if desc is None:
                        continue
                    bundles.append({
                        "a_hex": a_hex,
                        "b_hex": stream[j]["hex"],
                        "ap_hex": stream[k]["hex"],
                        "gap_ab_ms": b_ms - a_ms,
                        "gap_bap_ms": stream[k]["ms"] - b_ms,
                        "wall": stream[i]["wall"],
                        "ms": a_ms,
                        "summary": desc["summary"],
                        "params": desc["params"],
                        "a_desc": describe_frame(a),
                    })
                    used.update((i, j, k))
                    break
    return bundles


def _match_len(a: str, b: str) -> int:
    return abs(len(a) - len(b))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(bundles: list[dict]) -> dict:
    """Aggregate patterns across the decoded A-B-A' bundles."""
    n = len(bundles)
    a_types = Counter(b["a_hex"] for b in bundles)
    a_kinds = Counter(b["a_desc"]["summary"] for b in bundles)
    b_types = Counter(b["b_hex"] for b in bundles)

    # Companion B -> (count) per distinct A command, to surface which
    # effect/parameter blocks recur for which button.
    pairings: dict[str, Counter] = defaultdict(Counter)
    for b in bundles:
        pairings[b["a_hex"]][b["b_hex"]] += 1

    # Parameter opcodes inside the companions.
    param_tokens = Counter()
    for b in bundles:
        for p in b["params"]:
            param_tokens[p] += 1

    gaps = [b["gap_ab_ms"] for b in bundles] + [b["gap_bap_ms"] for b in bundles]
    if gaps:
        gaps.sort()
        median = gaps[len(gaps) // 2]
        spacing = {
            "min_ms": gaps[0],
            "median_ms": median,
            "max_ms": gaps[-1],
        }
    else:
        spacing = {}

    return {
        "bundle_count": n,
        "distinct_a_phrases": len(a_types),
        "distinct_b_companions": len(b_types),
        "a_phrases": {
            h: {"count": c, "name": next(
                (b["a_desc"]["summary"] for b in bundles if b["a_hex"] == h),
                "?")}
            for h, c in a_types.most_common()
        },
        "companions": {
            h: c for h, c in b_types.most_common()
        },
        "pairings": {
            a: dict(pairings[a].most_common()) for a in pairings
        },
        "param_ops": dict(param_tokens.most_common()),
        "spacing_ms": spacing,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt_bundles_text(bundles: list[dict]) -> str:
    lines = [f"MWM A-B-A' bundles — {len(bundles)} detected", "=" * 72]
    for b in bundles:
        lines.append(
            f"\n[{b['wall']}] A->B {b['gap_ab_ms']:+d}ms  B->A' "
            f"{b['gap_bap_ms']:+d}ms"
        )
        lines.append(f"  A  {b['a_hex']}")
        lines.append(f"  A' {b['ap_hex']}")
        lines.append(f"  B  {b['b_hex']}")
        lines.append(f"  A: {b['a_desc']['summary']}")
        if b["params"]:
            lines.append(f"  B parameters: {'; '.join(b['params'])}")
        else:
            lines.append("  B parameters: (opaque companion)")
    return "\n".join(lines) + "\n"


def _fmt_analysis_text(an: dict) -> str:
    lines = ["MWM A-B-A' bundle analysis", "=" * 72]
    lines.append(
        f"bundles: {an['bundle_count']}  distinct A: {an['distinct_a_phrases']}  "
        f"distinct companions B: {an['distinct_b_companions']}"
    )
    sp = an["spacing_ms"]
    if sp:
        lines.append(
            f"A-B/A'-A' spacing: min {sp['min_ms']}ms, median {sp['median_ms']}ms, "
            f"max {sp['max_ms']}ms"
        )
    lines.append("\nA phrase -> occurrences:")
    for h, info in an["a_phrases"].items():
        lines.append(f"  {info['count']:4d}  {h}  ({info['name']})")
    lines.append("\nCompanion B -> occurrences:")
    for h, c in an["companions"].items():
        lines.append(f"  {c:4d}  {h}")
    lines.append("\nCompanion parameters (opcodes):")
    for p, c in an["param_ops"].items():
        lines.append(f"  {c:4d}  {p}")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> None:
    if not args.files:
        print("Error: provide at least one capture file", file=sys.stderr)
        sys.exit(1)
    stream = load_stream([Path(p) for p in args.files])
    bundles = detect_bundles(
        stream,
        b_max_ms=args.b_max_ms,
        a_max_ms=args.a_max_ms,
    )

    if args.analyze:
        result = {"bundles": bundles, "analysis": analyze(bundles)}
    else:
        result = {"bundles": bundles}

    if args.format == "json":
        out = json.dumps(result, indent=2, default=str)
        if args.out:
            Path(args.out).write_text(out + "\n")
            print(f"Wrote {len(bundles)} bundles to {args.out}", file=sys.stderr)
        else:
            print(out)
    else:
        text = _fmt_bundles_text(bundles)
        if args.analyze:
            text += "\n\n" + _fmt_analysis_text(analyze(bundles))
        if args.out:
            Path(args.out).write_text(text + "\n")
            print(f"Wrote {len(bundles)} bundles to {args.out}", file=sys.stderr)
        else:
            print(text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect and decode A-B-A' MWM command bundles",
    )
    parser.add_argument("files", nargs="+", help="MRDF park capture file(s)")
    parser.add_argument("--analyze", action="store_true",
                        help="Aggregate patterns across the decoded bundles")
    parser.add_argument("--b-max-ms", type=int, default=150,
                        help="Max A->B gap in ms (default: 150)")
    parser.add_argument("--a-max-ms", type=int, default=600,
                        help="Max B->A' gap in ms (default: 600)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--out", default=None, help="Output file")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
