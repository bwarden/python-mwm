#!/usr/bin/env python3
"""Live clock-write phase experiment for MWM ear peripherals.

PURPOSE
-------
Research tool to test whether sending a ``91 0C <tick>`` clock-write frame
(see docs/mwm-show-protocol.md, "Clock write" + "Clock-sync field")
perturbs the beacon tick trajectory of an actively-beaconing ear: i.e. can
a controller actually WRITE the ear's shared clock to re-align its phase?

HOW IT WORKS
------------
1. Capture a few baseline beacons from the ear (via one receiver) to
   establish the free-running tick trajectory (~10.5 ticks/s, mod 256,
   beacons 7-15 s apart => ~70-150 ticks/beacon).
2. Send a single clock-write frame with a TARGET tick chosen to be far off
   the natural trajectory (default: +0x80 from the predicted natural tick).
3. Capture for a short observation window and report whether the next
   beacon's tick jumped toward the target (write accepted), stayed natural
   trajectory (write ignored), or was accompanied by a reset/silence.

VERIFIED OUTCOME (2026-08-27) -- READ BEFORE RUNNING
----------------------------------------------------
The clock-write is NOT cleanly testable while the ears are in their
autonomous/demo "standalone-sync" mode, for three reasons:

* In that mode the ears self-run a color cycling effect and beacon
  independently; there is no single controlled clock trajectory to write
  against.
* Tasmota/MQTT clump and reorder beacons, so the arrival deltas this tool
  relies on for "predicted natural tick" are untrustworthy.
* A passing "morning experiment" produced >30 s of silence after a write,
  but we could not attribute it to the write vs. the demo mode's own
  cadence.

CONCLUSION: to test clock-write meaningfully you must first park the ears
in a known show/clocked state (a real show master or a controlled trigger),
not leave them in autonomous demo mode.  This tool is kept because it is
the exact harness to re-run once the ears are in such a state.  It does NO
harm (sends one frame) but its interpretive output is uninformative in
demo mode.

Usage::

    python3 tools/clock_write_experiment.py [--target HEX] [--offset HEX]
        [--baseline SECS] [--observe SECS] [--receiver ID] [--mqtt-json PATH]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402
from _mqtt import load_mqtt, capture_lines, decode_frame_lines, send_frame  # noqa: E402

build_clock_write = mwm.build_clock_write
describe_frame = mwm.describe_frame
decode_beacon_clock = mwm.decode_beacon_clock

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"

# Best-known free-run rate (ticks/second), from >1 hr of capture.
TICKS_PER_SEC = 10.5


def _capture_beacons(mqtt: dict, receiver_id: str, duration: float) -> list[dict]:
    """Capture beacon frames for ``duration`` seconds from ``receiver_id``."""
    lines = capture_lines(mqtt, receiver_id, duration)
    beacons: list[dict] = []
    for frame in decode_frame_lines(mwm, lines):
        try:
            desc = describe_frame(frame)
        except Exception:
            continue
        if desc.get("kind") != "beacon":
            continue
        beacons.append({
            "t_unix": time.time(),
            "tick": decode_beacon_clock(frame),
            "effect": desc.get("demo_effect"),
            "raw": frame.hex().upper(),
        })
    return sorted(beacons, key=lambda b: b["t_unix"])


def _predict(prev_tick: int, now_t: float, prev_t: float) -> int:
    """Predicted current tick given the ~10.5 Hz free-run, mod-256 wrap."""
    delta = int(round((now_t - prev_t) * TICKS_PER_SEC))
    return (prev_tick + delta) & 0xFF


def report(beacons: list[dict]) -> None:
    print(f"  {'t':>10}  {'tick':>4}  {'pred':>4}  {'dev':>5}  eff  raw")
    for i, b in enumerate(beacons):
        if i == 0:
            pred = dev = "-"
        else:
            pred = _predict(beacons[i-1]["tick"], b["t_unix"],
                            beacons[i-1]["t_unix"])
            dev = ((b["tick"] - pred + 128) & 0xFF) - 128
        print(f"  {b['t_unix']:>10.1f}  {b['tick']:>02X}  {pred:>4}  {dev:>5}  "
              f"{b['effect'] or '-':>3}  {b['raw']}")


def run(args: argparse.Namespace) -> None:
    mqtt = load_mqtt(Path(args.mqtt_json))
    rcv = args.receiver

    print("MWM clock-write phase experiment")
    print(f"receiver: {rcv}")
    print()

    print(f"== baseline capture ({args.baseline}s) ==")
    base = _capture_beacons(mqtt, rcv, args.baseline)
    if not base:
        print("  no beacons captured -- ears idle/off? aborting")
        return
    report(base)

    last = base[-1]
    now = time.time()
    pred_natural = _predict(last["tick"], now, last["t_unix"])

    if args.target is not None:
        target = args.target & 0xFF
    else:
        target = (pred_natural + args.offset) & 0xFF
    print(f"\nlast baseline tick=0x{last['tick']:02X} at t={last['t_unix']:.1f}")
    print(f"natural tick now would be ~0x{pred_natural:02X}; target=0x{target:02X} "
          f"(dev {(target - pred_natural + 128) & 0xFF - 128:+d})")

    frame = build_clock_write(target)
    print(f"\n== sending clock-write {frame.hex().upper()} ==")
    send_frame(mqtt, mwm, frame)
    time.sleep(0.5)

    print(f"\n== post-write observation ({args.observe}s) ==")
    post = _capture_beacons(mqtt, rcv, args.observe)
    report(post)
    if not post:
        print("  no beacons observed after write (uninformative in demo mode)")

    print("\n== interpretation (see docstring: unreliable in demo mode) ==")
    if not post:
        return
    first = post[0]
    dev_natural = ((first["tick"] - pred_natural + 128) & 0xFF) - 128
    dev_target = ((first["tick"] - target + 128) & 0xFF) - 128
    print(f"  first post tick=0x{first['tick']:02X} vs natural pred "
          f"0x{pred_natural:02X} (dev {dev_natural:+d}) vs target "
          f"0x{target:02X} (dev {dev_target:+d})")
    if abs(dev_target) <= 4:
        print("  >>> tick JUMPED to target: clock-write ACCEPTED (phase reset)")
    elif abs(dev_natural) <= 6:
        print("  >>> tick on natural trajectory: clock-write IGNORED")
    else:
        print("  >>> tick moved to an unexpected value; demo-mode noise "
              "expected -- park the ears in a show state first")


def main() -> None:
    p = argparse.ArgumentParser(description="MWM clock-write phase experiment")
    p.add_argument("--target", type=lambda s: int(s, 16), default=None,
                   help="fixed target tick hex to write (default: derived)")
    p.add_argument("--offset", type=lambda s: int(s, 16), default=0x80,
                   help="hex offset from natural tick (default 80 = +128)")
    p.add_argument("--receiver", default="600605",
                   help="receiver MQTT id to monitor (default 600605)")
    p.add_argument("--baseline", type=int, default=25,
                   help="baseline capture seconds (default 25)")
    p.add_argument("--observe", type=int, default=30,
                   help="post-write observe seconds (default 30)")
    p.add_argument("--mqtt-json", default=str(_DEFAULT_MQTT),
                   help="MQTT config JSON path")
    run(p.parse_args())


if __name__ == "__main__":
    main()
