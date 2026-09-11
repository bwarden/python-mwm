#!/usr/bin/env python3
"""Analyze a captured beacon log for synchronization behavior.

PURPOSE
-------
The companion analysis to tools/capture_beacons.py.  Given a JSONL log
produced by that tool (default path /tmp/beacons_long.jsonl), this prints
per-receiver statistics about the beacon clock tick and a chronological
decoded sequence, so you can eyeball whether the tick advances linearly.

That is how we verified the core fact documented in
docs/mwm-show-protocol.md ("Clock-sync field"): the beacon tick is a
free-running ~10.5 Hz 8-bit counter that wraps modulo 256 every ~25 s and
is independent of the current demo effect.

KEY INSIGHT ON THE DATA
-----------------------
- The per-beacon tick DELTA (mod 256) between consecutive decoded beacons
  is meaningful: ~10.5 * (seconds actually elapsed).
- The MQTT-reported dt between messages is NOT reliable (Tasmota clumps and
  reorders).  So read the tick values, not the arrival deltas.

DESIGN NOTES
------------
- Ticks advance mod 256, so "advance" is computed with ((b - a) & 0xFF),
  never simple subtraction.  The rate is total_ticks / wall_span.
- Records are deduplicated by raw frame so the same physical beacon seen
  by two receivers isn't counted twice within the chronological list.
- Two receivers may report different rates simply because each catches a
  different SUBSET of beacons (positioning), not because the clock differs.

Usage::

    python3 tools/analyze_beacons.py /tmp/beacons_long.jsonl [--rcv 600605]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def tick_delta(a: int, b: int) -> int:
    """Signed advance from tick a to tick b over one 8-bit wrap window."""
    return (b - a) & 0xFF


def analyze(records: list[dict], rcv_filter: str | None = None) -> list[dict]:
    """Print per-receiver rate stats + a chronological decoded sequence."""
    if rcv_filter:
        records = [r for r in records if r["receiver"] == rcv_filter]
    records = sorted(records, key=lambda r: r["t_unix"])

    # Deduplicate: both receivers report the same physical beacon.  Collapse
    # records matching the same raw frame within 1 s of each other.
    dedup: list[dict] = []
    for r in records:
        if (dedup and r["raw"] == dedup[-1]["raw"]
                and r["t_unix"] - dedup[-1]["t_unix"] < 1.0):
            continue
        dedup.append(r)

    print(f"{'receiver':<8} {'n':>3}  {'first':>5} {'last':>5}  "
          f"{'rate/s':>7}  {'note'}")
    print("-" * 60)

    per_rcv: dict[str, list[dict]] = defaultdict(list)
    for r in dedup:
        per_rcv[r["receiver"]].append(r)
    for rcv, lst in sorted(per_rcv.items()):
        if rcv_filter and rcv != rcv_filter:
            continue
        lst.sort(key=lambda r: r["t_unix"])
        if len(lst) < 2:
            print(f"{rcv:<8} {len(lst):>3}  {'-':>5} {'-':>5}  "
                  f"{'-':>7}  insufficient")
            continue
        total_ticks = sum(
            tick_delta(lst[i-1]["clock_tick"], lst[i]["clock_tick"])
            for i in range(1, len(lst))
        )
        span_s = lst[-1]["t_unix"] - lst[0]["t_unix"]
        rate = total_ticks / span_s if span_s > 0 else 0
        ticks_per_beacon = total_ticks / (len(lst) - 1) if len(lst) > 1 else 0
        print(f"{rcv:<8} {len(lst):>3}  {lst[0]['clock_tick']:02X} "
              f"{lst[-1]['clock_tick']:02X}  {rate:7.2f}  "
              f"(~{ticks_per_beacon:.1f} ticks/beacon, span {span_s:.0f}s)")

    if not rcv_filter:
        print("\nChronological dedup'd sequence (all receivers):")
        for r in dedup:
            print(f"  {r['t_iso'][11:23]}  {r['receiver']:<8} "
                  f"tick={r['clock_tick']:02X} eff={r['effect']}  {r['raw']}")

    return dedup


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze beacon clock sync log")
    parser.add_argument("log", help="JSONL beacon capture log")
    parser.add_argument("--rcv", default=None, help="Filter to one receiver ID")
    args = parser.parse_args()

    records = []
    with open(args.log) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not records:
        print("No records found.", file=sys.stderr)
        sys.exit(1)
    analyze(records, args.rcv)


if __name__ == "__main__":
    main()
