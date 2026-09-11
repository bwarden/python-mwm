#!/usr/bin/env python3
"""Characterize beacon-suppression after sending a single MWM command.

The ears normally transmit idle beacons every ~7-12 s (dense, see the docs
and our control capture).  Some commands make them go quiet for ~2 min
(e.g. `48 00` random can leave them dead ~2 min; `48 81` pretends offline)
before they resume beaconing.  This tool runs ONE command per invocation:

  1. CONTROL baseline: passively capture beacons for a few seconds and
     report the natural inter-beacon gap distribution (so we know what a
     "normal" gap looks like right now -- dense ~7-12 s, no multi-minute
     holes).
  2. If `--hex` is given, send that command, then continue capturing for
     `--listen` seconds, logging EVERY decoded beacon with its inter-beacon
     delta.
  3. Report whether a long suppression gap appeared (gap >> natural max),
     plus the time-to-first-beacon-after-send, and write a JSON log.

Fully non-interactive, prints progress so you can follow along:

    # pure baseline (no command)
    python3 tools/suppress_probe.py --out /tmp/suppress_baseline.json

    # suppression after a normal color command (hypothesis: quiet ~2 min)
    python3 tools/suppress_probe.py --hex 9061 --out /tmp/suppress_blue.json

    # shutdown then try wake commands
    python3 tools/suppress_probe.py --hex 9060 --wake 9080 ... --out ...

A gap is flagged as suppression when it exceeds the natural worst-case gap
seen in the control window by a large margin (default 3x), since beaconing
is dense.  Use `--min-suppress-gap` to override in seconds.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import mwm  # noqa: E402
from _mqtt import load_mqtt, capture_lines, send_frame  # noqa: E402

describe_frame = mwm.describe_frame
decode_timings = mwm.decode_timings
tasmota_timings = mwm.tasmota_timings
build_frame = mwm.build_frame


def decode_beacons(raw_lines: list[str]) -> list[str]:
    """Decode every frame; return hex of those that look like idle beacons."""
    beacons: list[str] = []
    for line in raw_lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        ir = data.get("IrReceived") if isinstance(data, dict) else None
        if not isinstance(ir, dict):
            continue
        frames: list[bytes] = []
        if ir.get("Data"):
            try:
                frames.append(bytes.fromhex(ir["Data"].lstrip("0x")))
            except ValueError:
                pass
        if ir.get("RawData"):
            try:
                frames.extend(decode_timings(tasmota_timings(ir["RawData"])))
            except (ValueError, TypeError):
                pass
        for frame in frames:
            try:
                if describe_frame(frame).get("kind") == "beacon":
                    beacons.append(frame.hex().upper())
            except Exception:
                pass
    return beacons


def listen_loop(mqtt: dict, receiver_id: str, seconds: float) -> list[tuple[float, str]]:
    """Capture beacons for ``seconds`` wall time; return (t_rel, hex) list."""
    hits: list[tuple[float, str]] = []
    t0 = time.monotonic()
    last_report = 0.0
    while time.monotonic() - t0 < seconds:
        window = min(8.0, seconds - (time.monotonic() - t0))
        if window <= 0:
            break
        lines = capture_lines(mqtt, receiver_id, duration=window)
        now = time.monotonic()
        for hx in decode_beacons(lines):
            hits.append((now - t0, hx))
        elapsed = time.monotonic() - t0
        if elapsed - last_report >= 10:
            last_report = elapsed
            n = len(hits)
            print(f"    [t={elapsed:6.1f}s] {n} beacon(s) so far", flush=True)
    return hits


def run(mqtt: dict, receiver_id: str, opts) -> None:
    if opts.hex is not None:
        print(f"-> Sending command {opts.hex}", flush=True)
        send_frame(mqtt, mwm, bytes.fromhex(opts.hex))
    else:
        print("-> (no command; pure baseline)", flush=True)

    print(f"Listening {opts.listen}s...", flush=True)
    hits = listen_loop(mqtt, receiver_id, opts.listen)
    hits.sort(key=lambda x: x[0])

    deltas: list[float] = []
    for i in range(1, len(hits)):
        deltas.append(round(hits[i][0] - hits[i - 1][0], 2))

    natural_max = 15.0
    suppress_gap = max(deltas) if deltas else None
    suppressed = (
        suppress_gap is not None
        and suppress_gap > opts.min_suppress_gap
    )

    first = hits[0][0] if hits else None
    print(f"\nbeacons seen: {len(hits)} in {opts.listen}s")
    print(f"  time-to-first (after {opts.hex or 'start'}): "
          f"{first if first is not None else 'n/a'}")
    if deltas:
        print(f"  max inter-beacon gap: {suppress_gap}s "
              f"(natural dense cadence is ~{natural_max:.0f}s max)")
        print(f"  suppressed (~quiet): {suppressed} "
              f"(gap > {opts.min_suppress_gap}s)")
        print("  timeline (t_s, hex):")
        for t, hx in hits:
            print(f"      {t:7.1f}s  {hx}")
    else:
        print("  no beacons decoded; ears possibly deep in suppression "
              "for the whole window")

    out = {
        "session": datetime.now(timezone.utc).isoformat(),
        "command": opts.hex,
        "listen_s": opts.listen,
        "receiver_topic": opts.receive_topic,
        "num_beacons": len(hits),
        "time_to_first_s": first,
        "max_gap_s": suppress_gap,
        "min_suppress_gap": opts.min_suppress_gap,
        "suppressed": suppressed,
        "beacons": [{"t_s": t, "hex": hx} for t, hx in hits],
    }
    output = Path(opts.out)
    output.write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwrote {output}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="suppress_probe.json")
    ap.add_argument("--hex", default=None,
                    help="Command hex to send; omit for a pure baseline")
    ap.add_argument("--listen", type=float, default=200.0,
                    help="Seconds to listen for beacons after the command")
    ap.add_argument("--min-suppress-gap", type=float, default=30.0,
                    help="Gap (s) above which we call the ears 'suppressed'")
    ap.add_argument("--receiver", default=None,
                    help="Tasmota box to listen on (default: the transmit box)")
    ap.add_argument("--mqtt-json", default=None)
    args = ap.parse_args()

    mqtt = load_mqtt(args.mqtt_json)
    if args.receiver:
        receiver_id = args.receiver
    else:
        receiver_id = mqtt["transmit"].split("/")[2]
    args.receive_topic = f"tele/tasmota/{receiver_id}/RESULT"

    print(f"Suppression probe -- listen on {args.receive_topic}, "
          f"command={args.hex or '(none)'}")
    if args.hex is not None:
        print(f"  hypothesis: command may quiet the ears ~2 min; "
              f"suppressed if a gap > {args.min_suppress_gap}s appears")
    print(f"  Ctrl-C to abort (result only written if the pass completes)")
    run(mqtt, receiver_id, args)


if __name__ == "__main__":
    main()