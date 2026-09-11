#!/usr/bin/env python3
"""Suppress ears, then find which wake command restores dense beaconing.

The ears beacon densely when idle/demoing (~every 7-13 s).  After certain
commands they fall quiet ~2 min (see docs/mwm-show-protocol.md: `48 81`
"power-off display sequence, pretend offline ~2 min"; `48 00` "random
effect (can leave ears dead ~2 min)").  This tool:

  1. Sends a SUPPRESS command (default `48 81`).
  2. Listens until it confirms a suppression gap (no beacon for
     `--confirm-quiet` seconds), so we know they are genuinely quiet (not
     merely between beacons).
  3. For each WAKE command in `--wake` (a comma-separated list of hex),
     sends it, then listens up to `--probe` seconds and records the
     time-to-first-beacon.  A short time-to-first AND resumed dense cadence
     => that command "woke" them.
  4. Ends by optionally re-sending a "make sure they're demoing again"
     command (`--restore`, default `48 80`) and logs the JSON.

Fully non-interactive with continuous progress output:

    python3 tools/suppress_wake.py \
        --suppress 4881 \
        --wake 24,9080,481A,9061 \
        --out /tmp/suppress_wake.json
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


def decode_beacons(raw_lines: list[str]) -> list[str]:
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


def listen_once(mqtt: dict, receiver_id: str, seconds: float) -> list[tuple[float, str]]:
    t0 = time.monotonic()
    hits: list[tuple[float, str]] = []
    while time.monotonic() - t0 < seconds:
        window = min(6.0, seconds - (time.monotonic() - t0))
        if window <= 0:
            break
        lines = capture_lines(mqtt, receiver_id, duration=window)
        now = time.monotonic()
        for hx in decode_beacons(lines):
            hits.append((format(now - t0, ".1f"), hx))
    return hits


def confirm_quiet(mqtt: dict, receiver_id: str, quiet_secs: float,
                  no_quiet_after: float, max_wait: float) -> bool:
    """Decide whether a suppressor actually quieted the ears.

    Watches the beacon stream after the suppress command.  Returns True as
    soon as a silence of ``quiet_secs`` is observed (suppression confirmed).
    Returns False early if beacons keep arriving at the dense cadence --
    once ``no_quiet_after`` seconds of continuous beaconing pass with no
    ``quiet_secs`` gap, we treat the command as not-suppressing and stop,
    rather than spinning for the full ``max_wait``.
    """
    start = time.monotonic()
    last_beacon = time.monotonic()
    first_seen = None
    while time.monotonic() - start < max_wait:
        lines = capture_lines(mqtt, receiver_id, duration=6)
        now = time.monotonic()
        for hx in decode_beacons(lines):
            if first_seen is None:
                first_seen = now - start
            last_beacon = now
        gap = now - last_beacon
        if gap >= quiet_secs:
            print(f"    SUPPRESSED: {gap:.0f}s silence since last beacon "
                  f"(first beacon in window at "
                  f"{first_seen if first_seen is not None else 'n/a'}s)",
                  flush=True)
            return True
        elapsed = now - start
        if first_seen is not None and elapsed - first_seen >= no_quiet_after:
            print(f"    not suppressed: beacons flowing for "
                  f"{elapsed - first_seen:.0f}s with no {quiet_secs:.0f}s gap",
                  flush=True)
            return False
    print(f"    WARNING: inconclusive within {max_wait}s", flush=True)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="suppress_wake.json")
    ap.add_argument("--suppress", default="4881",
                    help="Command that puts ears into a quiet period")
    ap.add_argument("--wake", default="24,9080,481A,9061",
                    help="Comma-separated wake-command hex list to try")
    ap.add_argument("--confirm-quiet", type=float, default=30.0,
                    help="Silence (s) treated as real suppression")
    ap.add_argument("--no-quiet-after", type=float, default=60.0,
                    help="Seconds of steady beaconing with no quiet gap after "
                         "which we call the command not-suppressing and stop")
    ap.add_argument("--confirm-max", type=float, default=150.0,
                    help="Max (s) to wait for the suppression to take hold")
    ap.add_argument("--probe", type=float, default=25.0,
                    help="Seconds to listen after each wake command")
    ap.add_argument("--pause", type=float, default=4.0,
                    help="Seconds between wake commands (let them settle)")
    ap.add_argument("--restore", default="4880",
                    help="Command sent at the end to restore demoing")
    ap.add_argument("--receiver", default=None)
    ap.add_argument("--mqtt-json", default=None)
    args = ap.parse_args()

    mqtt = load_mqtt(args.mqtt_json)
    receiver_id = args.receiver or mqtt["transmit"].split("/")[2]
    receive_topic = f"tele/tasmota/{receiver_id}/RESULT"
    wake_list = [w.strip() for w in args.wake.split(",") if w.strip()]

    print(f"Suppress-wake probe -- listen {receive_topic}")
    print(f"  suppress: {args.suppress}")
    print(f"  wake candidates: {wake_list}")
    results = []

    # 1) Suppress.
    print(f"\n>>> Suppress: send {args.suppress} ...")
    send_frame(mqtt, mwm, bytes.fromhex(args.suppress))
    quiet = confirm_quiet(mqtt, receiver_id, args.confirm_quiet,
                          args.no_quiet_after, args.confirm_max)
    results.append({"phase": "suppress", "cmd": args.suppress,
                    "confirmed_quiet": quiet})

    # 2) Try each wake command.
    for wcmd in wake_list:
        print(f"\n>>> Wake candidate {wcmd}: send then listen {args.probe}s ...")
        t_beg = time.monotonic()
        send_frame(mqtt, mwm, bytes.fromhex(wcmd))
        time.sleep(1.0)
        _ = time.monotonic() - t_beg
        hits = listen_once(mqtt, receiver_id, args.probe)
        first = float(hits[0][0]) if hits else None
        dense = len(hits) >= 3
        print(f"    sent {wcmd}: first beacon {first if first is not None else 'n/a'}s, "
              f"{len(hits)} beacon(s) in {args.probe}s "
              f"({'DENSE (woke)' if dense else 'quiet/partial'})", flush=True)
        results.append({"phase": "wake", "cmd": wcmd,
                        "time_to_first_s": first, "num_beacons": len(hits),
                        "woke": dense})
        if args.pause:
            time.sleep(args.pause)

    # 3) Restore demoing.
    print(f"\n>>> Restore: send {args.restore} ...")
    send_frame(mqtt, mwm, bytes.fromhex(args.restore))

    out = {
        "session": datetime.now(timezone.utc).isoformat(),
        "receiver_topic": receive_topic,
        "suppress": args.suppress,
        "wake_list": wake_list,
        "confirm_quiet_s": args.confirm_quiet,
        "probe_s": args.probe,
        "restore": args.restore,
        "results": results,
    }
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    print("Summary:")
    for r in results:
        if r["phase"] == "suppress":
            print(f"  suppress {r['cmd']}: quiet_confirmed={r['confirmed_quiet']}")
        else:
            label = "WOKE" if r["woke"] else "no"
            print(f"  wake {r['cmd']}: first={r['time_to_first_s']}s "
                  f"beacons={r['num_beacons']} -> {label}")


if __name__ == "__main__":
    main()