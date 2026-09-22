#!/usr/bin/env python3
"""Long-running MWM beacon capture for synchronization analysis.

PURPOSE
-------
A human-driven rig research tool.  It passively listens to the Tasmota IR
receiver(s) in front of the ears and logs EVERY decoded MWM beacon to a
JSONL file.  The point is to collect hours of real beacon data so you (or a
script) can study how the beacon's automous "clock tick" and demo-effect
fields actually behave on live hardware -- this is what let us confirm the
tick advances ~10.5 ticks/second mod 256 and is independent of the running
effect (see docs/mwm-show-protocol.md, "Clock-sync field").

It is meant to run for a long time in the background:

    python3 tools/capture_beacons.py --rcvs 600605,179E4E \
        --out /tmp/beacons_long.jsonl

Each output line is one JSON object:

    {"t_unix": 1787857745.481, "t_iso": "...", "receiver": "600605",
     "clock_tick": 8, "effect": 21, "raw": "99420000...",
     "delta_s": 7.889}

NOTES / CAVEATS
---------------
- Requires the paho-mqtt package.
- Requires MQTT credentials in ~/.config/ir-remote-tools/mqtt.json (never
  committed).
- The "receiver" field records which Tasmota box heard the beacon.  Two
  receivers pointed at the same ears will each log the same physical burst
  (the ~20 ms "cluster" is one beacon twice, not a burst of many) -- see
  docs/mwm-show-protocol.md.
- Tasmota's MQTT reporting is software-paced: arrival timestamps / ordering
  and the delta_s field are NOT hardware-accurate.  Trust the decoded
  clock_tick sequence, not the arrival times.
- Run a SINGLE instance (or one per receiver, writing distinct files).  If
  two subscribers to the same topic share the session, each sees only part
  of the traffic.

Usage::

    python3 tools/capture_beacons.py [--out FILE] [--mqtt-json PATH]
        [--rcvs ID,ID,...]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Shared bootstrap + MQTT glue in this same directory (see their docstrings).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402
from _mqtt import load_mqtt  # noqa: E402

describe_frame = mwm.describe_frame
decode_timings = mwm.decode_timings
decode_beacon_clock = mwm.decode_beacon_clock
tasmota_timings = mwm.tasmota_timings

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"
_LOCK = threading.Lock()
_last_arrival: dict[str, float] = {}


def _handle_line(line: str, receiver_id: str, out: Path) -> None:
    """Parse one RESULT line; append any beacons it decodes to ``out``."""
    arrival = time.time()
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return
    ir = data.get("IrReceived") if isinstance(data, dict) else None
    if not isinstance(ir, dict):
        return

    frames: list[bytes] = []
    if ir.get("Data"):
        # Tasmota's built-in decoder -- FIRST frame of a bundle only.
        try:
            frames.append(bytes.fromhex(ir["Data"].lstrip("0x")))
        except ValueError:
            pass
    if ir.get("RawData"):
        # Whole-burst timings re-decoded to recover every frame.
        try:
            frames.extend(decode_timings(tasmota_timings(ir["RawData"])))
        except (ValueError, TypeError):
            pass

    for frame in frames:
        try:
            desc = describe_frame(frame)
        except Exception:
            continue
        if desc.get("kind") != "beacon":
            continue
        tick = decode_beacon_clock(frame)
        prev = _last_arrival.get(receiver_id)
        rec = {
            "t_unix": round(arrival, 3),
            "t_iso": datetime.now(timezone.utc).isoformat(),
            "receiver": receiver_id,
            "clock_tick": tick,
            "effect": desc.get("demo_effect"),
            "raw": frame.hex().upper(),
        }
        if prev is not None:
            rec["delta_s"] = round(arrival - prev, 3)
        _last_arrival[receiver_id] = arrival
        with _LOCK:
            # append with O_SYNC-ish robustness: reopen each time so a
            # crash never loses the tail (small per-beacon cost is fine).
            with open(out, "a") as fh:
                fh.write(json.dumps(rec) + "\n")


def _stream(mqtt: dict, receiver_id: str, out: Path) -> None:
    """Subscribe to one receiver indefinitely, logging beacons.

    Waits (with a generous timeout) on the shared persistent client for
    each next message; a dropped connection simply retries.
    """
    topic = f"tele/tasmota/{receiver_id}/RESULT"
    from _mqtt import subscribe_lines

    while True:
        try:
            for line in subscribe_lines(mqtt, topic, timeout=600):
                _handle_line(line, receiver_id, out)
        except (TimeoutError, OSError, RuntimeError):
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Long-running MWM beacon capture")
    parser.add_argument("--out", default=None, help="Output JSONL file")
    parser.add_argument("--mqtt-json", default=str(_DEFAULT_MQTT))
    parser.add_argument("--rcvs", default="600605,179E4E",
                        help="Comma-separated receiver IDs")
    args = parser.parse_args()

    mqtt = load_mqtt(Path(args.mqtt_json))
    out_path = Path(args.out) if args.out else Path(
        f"beacons_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    print(f"Capturing beacons to {out_path}", flush=True)

    threads = []
    for rcv in args.rcvs.split(","):
        rcv = rcv.strip()
        if not rcv:
            continue
        t = threading.Thread(target=_stream, args=(mqtt, rcv, out_path),
                             daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print(f"\nStopped. Log: {out_path}", flush=True)


if __name__ == "__main__":
    main()
