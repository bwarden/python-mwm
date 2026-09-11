#!/usr/bin/env python3
"""Offline analysis tool for MWM IR command logs.

PURPOSE
-------
Offline (no rig, no MQTT) decoder for MWM command captures.  Given a JSON
log from tools/color_cycle.py, or a list of hex frames, or raw timing
arrays, it runs each through the _mwm decoder and prints what every frame
"means" -- the same human-readable decode you'd want to double-check before
or after sending commands.

Input modes (pick one):
    --log FILE      A JSON log from color_cycle.py (uses each entry's
                    ``hex`` field; also re-decodes the MQTT ``payload``).
    --frames FILE   Plain-text hex frame strings, one per line (blank lines
                    and '#...' comments ignored).
    --timings FILE  JSON list of integer timing arrays (microseconds).

Output: JSON array to --out (default stdout), or a human table with
--format text.

Requires: the _mwm library (shared bootstrap); no Home Assistant.

Usage::

    python3 tools/analyze_log.py --log color_cycle.json
    python3 tools/analyze_log.py --log color_cycle.json --format text
    python3 tools/analyze_log.py --frames captures.txt
    python3 tools/analyze_log.py --timings raw_captures.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402

build_frame = mwm.build_frame
irsend_payload = mwm.irsend_payload
parse_frame_hex = mwm.parse_frame_hex
describe_frame = mwm.describe_frame
describe_bundle = mwm.describe_bundle
decode_timings = mwm.decode_timings


# ---------------------------------------------------------------------------
# Analysis engine
# ---------------------------------------------------------------------------

def _analyze_hex(hex_str: str) -> list[dict]:
    frames = parse_frame_hex(hex_str)
    if len(frames) > 1:
        bundle = describe_bundle(frames)
        return [{
            "hex": hex_str,
            "frames": [{"hex": f.hex(), "description": describe_frame(f)}
                       for f in frames],
            "bundle": bundle,
        }]
    if len(frames) == 1:
        frame = frames[0]
        return [{"hex": frame.hex(), "description": describe_frame(frame)}]
    return [{"hex": hex_str,
             "description": {"kind": "parse_error",
                             "summary": "no frames parsed from input"}}]


def _analyze_timings(timings: list[int]) -> list[dict]:
    frames = decode_timings(timings)
    if not frames:
        return [{"timings": timings,
                 "description": {"kind": "decode_error",
                                 "summary": "no frames decoded from timings"}}]
    return [
        {"frame_index": i, "hex": frame.hex(),
         "description": describe_frame(frame)}
        for i, frame in enumerate(frames)
    ]


def _analyze_payload_string(payload: str) -> list[dict]:
    """Decode a Tasmota IRsend payload string ('{Protocol}...{timings}')."""
    parts = payload.split(",")
    timings = [int(x) for x in parts if x.strip().lstrip("-").isdigit()]
    if not timings:
        return [{"payload": payload,
                 "description": {"kind": "parse_error",
                                 "summary": "no numeric timings in payload"}}]
    return _analyze_timings(timings)


# ---------------------------------------------------------------------------
# Input loaders
# ---------------------------------------------------------------------------

def _load_log(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data.get("entries", []) if isinstance(data, dict) else data


def _load_frames(path: Path) -> list[str]:
    return [
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _load_timings(path: Path) -> list[list[int]]:
    data = json.loads(path.read_text())
    if isinstance(data, list) and data and isinstance(data[0], list):
        return data
    if isinstance(data, dict) and "timings" in data:
        return [data["timings"]]
    return []


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_text(records: list[dict], out_path: str | None) -> None:
    lines = [f"MWM Command Analysis — {len(records)} records", "=" * 60]
    for rec in records:
        ts = rec.get("timestamp", "")
        name = rec.get("input_name", "")
        kind = rec.get("input_kind", "")
        hex_str = rec.get("hex", "")
        notes = rec.get("notes", "")
        line_num = rec.get("line", rec.get("capture", ""))

        if ts:
            lines.append(f"\n[{ts}] {kind} — {name}")
        elif line_num:
            lines.append(f"\n[record {line_num}]")
        else:
            lines.append(f"\n[{name}]" if name else "\n[record]")

        if hex_str:
            lines.append(f"  hex: {hex_str}")

        for item in rec.get("analysis", []):
            desc = item.get("description", {})
            frames = item.get("frames")
            bundle = item.get("bundle")
            if frames:
                lines.append(f"  bundle ({len(frames)} frames):")
                if bundle:
                    lines.append(f"    bundle desc: {bundle.get('summary', '?')}")
                for fi in frames:
                    fd = fi.get("description", {})
                    lines.append(f"    frame {fi.get('hex', '?')}: "
                                 f"{fd.get('summary', fd.get('kind', '?'))}")
            elif desc:
                status = "OK" if desc.get("kind") != "invalid" else "INVALID"
                lines.append(f"  [{status}] {desc.get('summary', desc.get('kind', '?'))}")
            else:
                lines.append("  [NO DECODE]")

        if notes:
            lines.append(f"  notes: {notes}")

    text = "\n".join(lines) + "\n"
    if out_path:
        Path(out_path).write_text(text)
        print(f"Wrote {len(records)} records to {out_path}", file=sys.stderr)
    else:
        print(text)


def run(args: argparse.Namespace) -> None:
    records: list[dict] = []

    if args.log:
        for entry in _load_log(Path(args.log)):
            hex_str = entry.get("hex", "")
            payload = entry.get("payload", "")
            analysis = _analyze_hex(hex_str) if hex_str else []
            if payload and not analysis:
                analysis = _analyze_payload_string(payload)
            records.append({
                "timestamp": entry.get("t", ""),
                "input_kind": entry.get("kind") or entry.get("family",
                                                             "unknown"),
                "input_name": entry.get("name", ""),
                "hex": hex_str,
                "notes": entry.get("notes", ""),
                "analysis": analysis,
            })
    elif args.frames:
        for i, hex_str in enumerate(_load_frames(Path(args.frames)), 1):
            records.append({"line": i, "hex": hex_str,
                            "analysis": _analyze_hex(hex_str)})
    elif args.timings:
        for i, timings in enumerate(_load_timings(Path(args.timings)), 1):
            records.append({"capture": i, "timings": timings[:20],
                            "timing_count": len(timings),
                            "analysis": _analyze_timings(timings)})
    else:
        print("Error: provide --log, --frames, or --timings", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        output = json.dumps(records, indent=2, default=str)
        if args.out:
            Path(args.out).write_text(output + "\n")
            print(f"Wrote {len(records)} records to {args.out}", file=sys.stderr)
        else:
            print(output)
    else:
        _print_text(records, args.out)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline analysis of MWM IR command logs",
    )
    parser.add_argument("--log", default=None, help="JSON log from color_cycle.py")
    parser.add_argument("--frames", default=None,
                        help="Plain-text hex frame strings (one per line)")
    parser.add_argument("--timings", default=None,
                        help="JSON list of timing arrays")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--out", default=None,
                        help="Output file (default: stdout)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
