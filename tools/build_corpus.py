#!/usr/bin/env python3
"""Build a deduplicated corpus of known-good MWM commands from saved captures.

PURPOSE
-------
Scan every saved long capture in samples/ (Tasmota MQTT RESULT logs, Mouse
Ear Recorder MRDF *-reader exports, and oPossum's filtered EMLG* dumps), decode
every MWM frame each contains, and collapse the repeated chatter into a single
machine-readable corpus of *known-good commands* -- the distinct frames that
real hardware actually emitted.  This corpus is the ground truth you can later
reuse to refine the commands we generate (e.g. validate an encoder's output
against what real ears / wands broadcast).

Each corpus entry is keyed by (kind, content) so identical commands seen many
times (beacons, park chatter, oPossum sweeps) collapse to one row with a count
and the set of sources that evidenced it.  Only frames that pass
``frame_is_valid`` (length rule + CRC-8/55AA additive checksum) are kept, and
only those that *decode* as something meaningful are reported.

Input modes (auto-detected by content; you can also scope with --glob):
    *.log / *.tasmota.log   Tasmota MQTT RESULT stream; each line is
                            ``topic {IrReceived:{...RawData:"..."}}`` or
                            ``... {IrReceived:{...,Protocol:"MWM",Data:...}}``.
    *_reader.TXT            Mouse Ear Recorder exports, ``time:ms:hex`` with
                            interleaved non-frame preamble bytes.
    *_filtered.txt          oPossum filtered dumps (dec2dumps), ``offset: hex``
                            where each line is already one whole frame.
    mode2-*.log             LIRC mode2 dumps, ``pulse N`` / ``space N`` runs
                            re-decoded in bulk (sign-alternating, gap-tolerant).

Output (--out JSON, default stdout):
    summary     per-source totals, distinct validated frames, kept commands.
    corpus      list of known-good command entries.

Requires: the _mwm library (shared bootstrap); no Home Assistant.

Usage::

    python3 tools/build_corpus.py
    python3 tools/build_corpus.py --out samples/mwm-known-good.json
    python3 tools/build_corpus.py --glob '*.log' --format text
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

decode_timings = mwm.decode_timings
tasmota_timings = mwm.tasmota_timings
describe_frame = mwm.describe_frame
frame_is_valid = mwm.frame_is_valid

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "samples"


# ---------------------------------------------------------------------------
# Frame scanning
# ---------------------------------------------------------------------------

def _scan_bytes(buf: bytes) -> list[bytes]:
    """Extract every valid frame from a raw byte stream.

    Frames are 0x9x- (length-rule delimited) or 55AA- (additive-checksum
    delimited).  Non-frame preamble bytes, trailing device chatter and
    concatenated frames are all handled by scanning forward greedily.  Used
    for the MRDF *-reader exports, whose lines can carry a preamble plus one
    or more frames.
    """
    out: list[bytes] = []
    i = 0
    n = len(buf)
    while i < n:
        h = buf[i]
        if (h & 0xF0) == 0x90:
            ln = (h & 0x0F) + 3
            cand = buf[i:i + ln]
            if len(cand) == ln and frame_is_valid(cand)[0]:
                out.append(cand)
                i += ln
                continue
            i += 1
            continue
        if h == 0x55 and i + 1 < n and buf[i + 1] == 0xAA:
            # no length prefix: try the longest span that checksums.
            found: bytes | None = None
            for j in range(n - 1, i + 1, -1):
                if frame_is_valid(buf[i:j + 1])[0]:
                    found = buf[i:j + 1]
                    break
            if found is not None:
                out.append(found)
                i += len(found)
                continue
            i += 1
            continue
        i += 1
    return out


# ---------------------------------------------------------------------------
# Source loaders -> (kind, frame)
# ---------------------------------------------------------------------------

def _iter_tasmota_lines(path: Path):
    """Yield bare hex frame strings decoded from a Tasmota RESULT log."""
    for line in path.read_text().splitlines():
        m = re.search(r'"RawData"\s*:\s*"([^"]+)"', line)
        if not m:
            continue
        raw = m.group(1)
        try:
            timings = tasmota_timings(raw)
        except ValueError:
            continue
        for frame in decode_timings(timings):
            yield frame.hex().upper()


def _iter_emlg_lines(path: Path):
    """Yield hex frames from an oPossum filtered dump (``offset: hex``)."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^[0-9A-Fa-f]+:\s*([0-9A-Fa-f ]+)$", line)
        if not m:
            continue
        yield re.sub(r"\s+", "", m.group(1)).upper()


def _iter_mrdf_lines(path: Path):
    """Yield hex frames from a Mouse Ear Recorder export (``time:ms:hex``).

    Byte payloads follow the last ``:`` with no leading space::

        YYYY/MM/DD hh.mm.ss:00000000:55 AA 06 01 ..
    """
    for line in path.read_text().splitlines():
        if line.startswith("//") or not line.strip():
            continue
        parts = line.split(":", 2)
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-fA-F]+", parts[1]):
            continue
        hex_str = re.sub(r"\s+", "", parts[2]).upper()
        if re.sub(r"[0-9a-fA-F]", "", hex_str).strip():
            continue
        try:
            buf = bytes.fromhex(hex_str)
        except ValueError:
            continue
        for frame in _scan_bytes(buf):
            yield frame.hex().upper()


def _iter_mode2_lines(path: Path):
    """Yield hex frames from a LIRC mode2 dump (``pulse N`` / ``space N``)."""
    runs: list[int] = []
    for line in path.read_text().splitlines():
        m = re.fullmatch(r"\s*(pulse|space)\s+(\d+)", line.strip())
        if not m:
            continue
        usec = int(m.group(2))
        runs.append(usec if m.group(1) == "pulse" else -usec)
    for frame in decode_timings(runs):
        yield frame.hex().upper()


def _load_source(path: Path) -> tuple[str, list[str]]:
    """Detect a capture's format and return (kind, [hex frames])."""
    name = path.name.lower()
    if "_filtered.txt" in name:
        return "emlg", list(_iter_emlg_lines(path))
    if "mode2" in name:
        return "mode2", list(_iter_mode2_lines(path))
    if name.endswith(".tasmota.log") or name.endswith(".log"):
        return "tasmota", list(_iter_tasmota_lines(path))
    if name.endswith(".txt"):
        return "mrdf", list(_iter_mrdf_lines(path))
    return "unknown", []


# ---------------------------------------------------------------------------
# Corpus assembly
# ---------------------------------------------------------------------------

def _frame_corpus_key(frame: bytes) -> tuple[str, str] | None:
    """Return (kind, content) key for a validated frame, or None if it does
    not decode to anything meaningful."""
    try:
        desc = describe_frame(frame)
    except Exception:
        desc = describe_frame(frame.hex().upper())
    kind = desc.get("kind")
    if kind in (None, "invalid"):
        return None
    if len(frame) >= 3 and frame[0] == 0x55 and frame[1] == 0xAA:
        content = frame[2:-1].hex()
    else:
        content = frame[1:-1].hex()
    return kind, content


def _describe(frame: bytes) -> dict:
    try:
        return describe_frame(frame)
    except Exception:
        return describe_frame(frame.hex().upper())


def build(paths: list[Path]) -> dict:
    summary: dict = {}
    counts: Counter[str] = Counter()
    # key -> (frames, sources)
    corpus: dict[tuple[str, str], dict] = {}

    for path in paths:
        kind, frames = _load_source(path)
        if not frames:
            summary[str(path)] = {"kind": kind, "decoded": 0}
            continue
        source = path.name
        for hex_str in frames:
            try:
                frame = bytes.fromhex(hex_str)
            except ValueError:
                continue
            ok, _reason = frame_is_valid(frame)
            if not ok:
                continue
            key = _frame_corpus_key(frame)
            if key is None:
                continue
            entry = corpus.setdefault(
                key, {"key": key, "hex": frame.hex().upper(),
                      "description": _describe(frame),
                      "count": 0, "sources": set()})
            entry["count"] += 1
            entry["sources"].add(source)
        counts[kind] += 1
        summary[str(path)] = {"kind": kind, "decoded": len(frames)}

    rows = []
    for key, entry in corpus.items():
        rows.append({
            "kind": key[0],
            "content": key[1],
            "hex": entry["hex"],
            "count": entry["count"],
            "description": entry["description"].get("summary", ""),
            "sources": sorted(entry["sources"]),
        })
    rows.sort(key=lambda r: (-r["count"], r["hex"]))

    return {
        "generated_by": "tools/build_corpus.py",
        "sources": summary,
        "total_validated_frames": sum(summary[p]["decoded"] for p in summary),
        "distinct_commands": len(rows),
        "corpus": rows,
    }


def _print_text(data: dict, out_path: str | None) -> None:
    lines = [f"MWM known-good command corpus — {len(data['corpus'])} commands",
             "=" * 60]
    lines.append(f"validated frames: {data['total_validated_frames']}   "
                 f"distinct: {data['distinct_commands']}")
    for src, info in sorted(data["sources"].items()):
        lines.append(f"  {info['kind']:8s} {src.split('/')[-1]:32s} "
                     f"decoded={info['decoded']}")
    lines.append("")
    for r in data["corpus"]:
        lines.append(f"[{r['count']:4d}x] {r['hex']:24s} {r['kind']:14s} "
                     f"{r['description']}")
        if len(r["sources"]) > 1:
            lines.append(f"          sources: {', '.join(r['sources'])}")
    text = "\n".join(lines) + "\n"
    if out_path:
        Path(out_path).write_text(text)
        print(f"Wrote corpus to {out_path}", file=sys.stderr)
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a known-good MWM command corpus from saved captures")
    parser.add_argument("paths", nargs="*",
                        help="capture files/dirs (default: samples/)")
    parser.add_argument("--glob", default=None,
                        help="filename glob to select within the search paths")
    parser.add_argument("--out", default=None, help="output JSON file")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    search = [Path(p) for p in args.paths] or [DEFAULT_DIR]
    paths: list[Path] = []
    for base in search:
        if base.is_dir():
            pat = args.glob or "*"
            paths.extend(sorted(base.glob(pat)))
        elif base.is_file():
            paths.append(base)

    data = build(paths)
    if args.format == "json":
        output = json.dumps(data, indent=2, default=str)
        if args.out:
            Path(args.out).write_text(output + "\n")
            print(f"Wrote corpus to {args.out}", file=sys.stderr)
        else:
            print(output)
    else:
        _print_text(data, args.out)


if __name__ == "__main__":
    main()