#!/usr/bin/env python3
"""Sequence-aware park capture analysis: decode-everything + transitions.

PURPOSE
-------
Companion to analyze_shape.py / analyze_bundles.py that works at the level
of *ordered per-message events*, which build_corpus() deliberately discards.
It answers two questions programmatically (no AI round-trips):

  1. For every decoded message in a park feed: WHAT is it actually doing
     and WHEN did it fire?
  2. Between adjacent decoded messages: what immediately PRECEDES /
     FOLLOWS what, so complex transitions surface empirically -- e.g. how
     a fade-down (`48 85`) phrase is exited, how colors sweep through the
     corpus, and how a command's effect re-appears in the next idle beacon
     (`99 42 00 00 48 ss 0C t D0 0E crc`).

The outputs are flat TSV files (one row per message / per pair) so the
decoded stream and every transition stay reusable for further passes
without re-running the decoder.

FEED FORMATS / PARSE RULES (shared with build_corpus.py)
--------------------------------------------------------
  *_reader.TXT   Mouse Ear Recorder exports
                 ``YYYY/MM/DD hh.mm.ss:00233258:<hex>``
                 - parts[0] wall clock (kept as ``wall``)
                 - parts[1] 8-hex-digit session ms offset -> ``tick`` (ms)
                   VERIFIED against MRDF0007 wall clock: tick advances ~1000
                   per wall-second, so ticks are genuine ms for *_reader.TXT.
                 - payload hex after the 2nd ``:``; one line may pack TWO
                   frames plus noise (``81 7F 7F ... 55 AA ...``), so the
                   hex blob is run through build_corpus._scan_bytes()
  *_filtered.txt oPossum filtered dumps
                 ``002323E0: 55 AA 09 04 01 01 15 01 00 00 00 25``
                 - leading field = per-frame offset -> ``tick``
                 - UNIT UNDOCUMENTED; the periodic 55AA ping spacing
                   (~0x3E3 ticks) is the cadence reference, so gap/window
                   maths in these files is only *relative* bucketting.
                 - one frame per line (still routed through _scan_bytes).
  mode2-*.log    skipped by this tool (rig capture, not park).

``tick`` units therefore differ PER SOURCE (ms for MRDF, unknown for EMLG).
Transitions are computed WITHIN one source only -- no pairs are formed
across files, and ``gap_tick`` never mixes units.

WHAT COUNTS AS A "MESSAGE"
--------------------------
Only CRC/checksum-valid frames that describe_frame() decodes (kind is one
of effect-command / color-command / command / wand-command / beacon /
55aa). Invalid or undecodable lines never appear; nothing is hand-tuned.

OUTPUTS (written to --out, default <repo>/analysis/park)
--------------------------------------------------------
  frames.tsv      one row per decoded message:
                  source,tick,wall,hex,len,kind,shape,summary,effect,
                  demo_effect,clock_tick,colors,timing
                  - shape = masked token stream (analyze_shape._frame_shape)
                  - colors/timing = extract_color_fields/extract_timing_fields
                    joins (only present on commandy frames)
                  - demo_effect/clock_tick = the beacon's running `48 ss`
                    and `0C t` (idle demos cycle on their own; the beacon
                    is NOT proof a preceding command took effect)
  transitions.tsv  one row per adjacent pair of decoded messages:
                  source,prev_tick,prev_kind,prev_shape,prev_hex,
                  gap_tick,burst,next_tick,next_kind,next_shape,next_hex
                  - burst=1 when gap_tick <= --window (tick units; for
                    MRDF that is ms)
  transition-counts.tsv  aggregated (burst,prev_shape,next_shape) -> n,
                  sorted by n desc; set membership = the empirical
                  transition library (fade exits, sweep chains, ...)
  beacon-after-command.tsv  for every commandy frame, the NEXT idle beacon
                  in that source and its demo_effect index -> n. Use to
                  ask "what does a fade-down lead to" against real beacon
                  state, keeping in mind demo effects autonomously cycle.

METHODOLOGY NOTES / GOTCHAS
---------------------------
- No cross-source transitions; no interpolation of EMLG tick units.
- A "transition" is ANY two consecutively decoded frames; long silences
  show up as huge gap_tick rather than being suppressed. burst flags the
  close pairs that almost certainly belong to one physical burst.
- The 55AA timecodes flood MRDF (every line interleaves `... 09 04 ...` /
  `... 06 01 ...`), so their shape dominates count rows -- filter on
  ``prev_kind != 55aa`` / ``next_kind != 55aa`` when looking at ear-side
  transitions.
- describe_content sees a command's parsed intents (tokens); effect labels
  come from the library's catalogue (docs/mwm-show-protocol.md section 4).

USAGE
-----
    python3 tools/analyze_park.py
    python3 tools/analyze_park.py samples/MRDF0007.TXT samples/EMLG000E_filtered.txt
    python3 tools/analyze_park.py --out /tmp/park --window 2000 --min-count 2
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402
from build_corpus import _scan_bytes  # noqa: E402
from analyze_shape import _frame_shape  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PARK_DEFAULTS = [
    REPO / "samples" / "MRDF0007.TXT",
    REPO / "samples" / "MRDF0008.TXT",
    REPO / "samples" / "EMLG000E_filtered.txt",
    REPO / "samples" / "EMLG0026_filtered.txt",
]

_MRDF_RE = re.compile(r"^([\d/]+ [\d:.]+):([0-9A-Fa-f]{8}):(.*)$")
_EMLG_RE = re.compile(r"^([0-9A-Fa-f]+):\s*([0-9A-Fa-f ]+)$")


def _frames_from_hex(hex_blob: str) -> list[bytes]:
    """Decode a raw hex payload (space-padded) into validated frames."""
    clean = re.sub(r"[^0-9a-fA-F]", "", hex_blob)
    if not clean:
        return []
    try:
        return _scan_bytes(bytes.fromhex(clean))
    except ValueError:
        return []


def load_source(path: Path, spread_ms: int = 0) -> list[dict] | None:
    """Return ordered stream of {source,tick,wall,hex} for one park feed.

    With ``spread_ms > 0`` the frames packed onto ONE capture line (the
    recorder stamps the whole burst with a single offset, e.g. the
    near-simultaneous countdown members of ``0018E8D7: 9C F9 .. 9C F8 ..``)
    are de-flattened: frame i of the line gets ``tick + i*spread_ms``, in
    the line's document order.  Each member keeps its countdown-byte
    cadence (F9->F8->F7 are one 100 ms step apart) so runs that share a
    line stay a real, per-member timeline instead of all collapsing onto
    one tick and losing their ordering.  Default 0 keeps a line's frames
    at one tick, exactly as before.

    None if the filename matches no known park feed format.
    """
    name = path.name.lower()
    stream: list[dict] = []

    if name.endswith(".txt") and not name.endswith("_filtered.txt"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("#"):
                continue
            m = _MRDF_RE.match(line)
            if not m:
                continue
            wall, ms_hex, payload = m.groups()
            tick = int(ms_hex, 16)
            for i, fr in enumerate(_frames_from_hex(payload)):
                stream.append({"tick": tick + i * spread_ms, "wall": wall,
                               "hex": fr.hex().upper()})

    elif "_filtered.txt" in name or name.endswith("mwm"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            m = _EMLG_RE.match(line)
            if not m:
                continue
            off_hex, payload = m.groups()
            tick = int(off_hex, 16)
            for i, fr in enumerate(_frames_from_hex(payload)):
                stream.append({"tick": tick + i * spread_ms, "wall": "",
                               "hex": fr.hex().upper()})

    else:
        return None

    stream.sort(key=lambda r: (r["tick"], r["hex"]))
    return stream


BEACON_KIND = "beacon"
COMMAND_KINDS = {"effect-command", "color-command", "command", "wand-command"}

_TSV_HEADERS = {
    "frames": "source\ttick\twall\thex\tlen\tkind\tshape\tsummary\teffect\t"
              "demo_effect\tclock_tick\tcolors\ttiming\n",
    "transitions": "source\tprev_tick\tprev_kind\tprev_shape\tprev_hex\t"
                   "gap_tick\tburst\tnext_tick\tnext_kind\tnext_shape\tnext_hex\n",
    "transition-counts": "burst\tprev_shape\tnext_shape\tn\n",
    "beacon-after-command": "cmd_shape\tcmd_kind\tbeacon_demo\tn\n",
}


def _describe(frame_hex: str) -> dict:
    try:
        return mwm.describe_frame(frame_hex)
    except Exception:
        return {"kind": None, "summary": "", "tokens": [], "effect": None}


def frame_row(src: str, rec: dict, desc: dict) -> list[list[str]] | list[dict]:
    """Flatten one message into a frames.tsv row dict."""
    kind = desc.get("kind")
    shape = " ".join(_frame_shape(bytes.fromhex(rec["hex"]))) if kind else ""
    colors = "|".join(desc.get("colors") or [])
    timing = "|".join(desc.get("timing") or [])
    return {
        "source": src,
        "tick": rec["tick"],
        "wall": rec["wall"],
        "hex": rec["hex"],
        "len": len(rec["hex"]) // 2,
        "kind": kind or "invalid",
        "shape": shape,
        "summary": (desc.get("summary") or "").replace("\t", " "),
        "effect": "" if desc.get("effect") is None else str(desc["effect"]),
        "demo_effect": "",
        "clock_tick": "" if desc.get("clock_tick") is None else str(desc["clock_tick"]),
        "colors": colors,
        "timing": timing,
    }


def process(streams: list[tuple[str, list[dict]]], window: int) -> dict:
    """Run decode + transition analysis; return list-of-dict results."""
    frames: list[dict] = []
    transitions: list[dict] = []
    beacon_counts: Counter = Counter()

    for src, recs in streams:
        beats: list[dict] = []
        for rec in recs:
            desc = _describe(rec["hex"])
            row = frame_row(src, rec, desc)
            if row["kind"] == "invalid":
                continue
            row["kind"] = desc.get("kind") or "invalid"
            if desc.get("kind") == BEACON_KIND:
                row["demo_effect"] = desc.get("demo_effect") or ""
            frames.append(row)
            shape = row["shape"]
            if desc.get("kind") == BEACON_KIND:
                shape = f"beacon(demo={row['demo_effect']})"
            beat = {"tick": rec["tick"], "kind": row["kind"], "shape": shape,
                    "hex": rec["hex"], "demo": row["demo_effect"]}
            beats.append(beat)

        for i in range(1, len(beats)):
            a, b = beats[i - 1], beats[i]
            if a["kind"] == "invalid" or b["kind"] == "invalid":
                continue
            gap = b["tick"] - a["tick"]
            transitions.append({
                "source": src,
                "prev_tick": a["tick"], "prev_kind": a["kind"], "prev_shape": a["shape"],
                "prev_hex": a["hex"],
                "gap_tick": gap, "burst": 1 if gap <= window else 0,
                "next_tick": b["tick"], "next_kind": b["kind"], "next_shape": b["shape"],
                "next_hex": b["hex"],
            })

        # beacon-after-command: nearest *later* idle beacon (reverse scan
        # so a beacon already running after a command is the one counted).
        next_demo: list[str | None] = [None] * len(beats)
        nxt = None
        for i in range(len(beats) - 1, -1, -1):
            if beats[i]["kind"] == BEACON_KIND:
                nxt = beats[i].get("demo") or None
            next_demo[i] = nxt
        for i, beat in enumerate(beats):
            if beat["kind"] in COMMAND_KINDS and next_demo[i]:
                d = next_demo[i]
                beacon_counts[(beat["shape"], beat["kind"], d)] += 1

    counts: Counter = Counter(
        (t["burst"], t["prev_shape"], t["next_shape"]) for t in transitions
        if t["prev_shape"] and t["next_shape"]
    )
    return {
        "frames": frames,
        "transitions": transitions,
        "counts": counts,
        "beacons": beacon_counts,
    }


def write_tsv(path: Path, header: str, rows: list[dict], order: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        fh.write(header)
        for r in rows:
            fh.write("\t".join(str(r.get(k, "")) for k in order) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Sequence-aware park capture analysis (decode-all + transitions) -> TSV.",
    )
    ap.add_argument("files", nargs="*", default=None,
                    help="Park feeds (MRDF *_reader.TXT, oPossum *_filtered.txt). "
                         "Default: the four park captures in samples/.")
    ap.add_argument("--out", default=str(REPO / "analysis" / "park"),
                    help="Output directory (default: <repo>/analysis/park).")
    ap.add_argument("--window", type=int, default=2000,
                    help="Burst window in tick units for the burst flag "
                         "(default: 2000; for MRDF this is ms).")
    ap.add_argument("--spread-ms", type=int, default=0,
                    help="de-flatten frames packed onto one capture line: "
                         "frame i gets +i*spread_ms ms (default 0: one tick "
                         "per line, as captured; pass e.g. 100 so packed "
                         "countdown members keep their ~100 ms cadence)")
    ap.add_argument("--min-count", type=int, default=1,
                    help="Drop transition-count rows below this n (default: 1).")
    args = ap.parse_args()

    paths = [Path(p) for p in (args.files or PARK_DEFAULTS)]
    streams: list[tuple[str, list[dict]]] = []
    for p in paths:
        st = load_source(p, args.spread_ms)
        if st is None:
            print(f"  skip {p.name}: not a MRDF/*_filtered.txt park feed")
            continue
        streams.append((p.name, st))
        print(f"  {p.name:<28} {len(st)} decoded frames")
    if not streams:
        print("No park feeds loaded; nothing to do.")
        sys.exit(1)

    res = process(streams, args.window)
    out = Path(args.out)
    write_tsv(out / "frames.tsv", _TSV_HEADERS["frames"], res["frames"],
              ["source", "tick", "wall", "hex", "len", "kind", "shape", "summary",
               "effect", "demo_effect", "clock_tick", "colors", "timing"])
    write_tsv(out / "transitions.tsv", _TSV_HEADERS["transitions"], res["transitions"],
              ["source", "prev_tick", "prev_kind", "prev_shape", "prev_hex",
               "gap_tick", "burst", "next_tick", "next_kind", "next_shape", "next_hex"])
    crows = [{"burst": b, "prev_shape": p, "next_shape": n, "n": c}
             for (b, p, n), c in sorted(res["counts"].items(), key=lambda kv: -kv[1])
             if c >= args.min_count]
    write_tsv(out / "transition-counts.tsv", _TSV_HEADERS["transition-counts"],
              crows, ["burst", "prev_shape", "next_shape", "n"])
    brows = [{"cmd_shape": p, "cmd_kind": k, "beacon_demo": b, "n": c}
             for (p, k, b), c in sorted(res["beacons"].items(), key=lambda kv: -kv[1])
             if c >= args.min_count]
    write_tsv(out / "beacon-after-command.tsv", _TSV_HEADERS["beacon-after-command"],
              brows, ["cmd_shape", "cmd_kind", "beacon_demo", "n"])

    print(f"\n  frames:      {len(res['frames'])} rows  -> {out / 'frames.tsv'}")
    print(f"  transitions: {len(res['transitions'])} rows -> {out / 'transitions.tsv'}")
    print(f"  count pairs: {len(crows)} (>= {args.min_count}) -> "
          f"{out / 'transition-counts.tsv'}")
    print(f"  beacon-after-command (cmd->demo): {len(brows)} -> "
          f"{out / 'beacon-after-command.tsv'}")


if __name__ == "__main__":
    main()
