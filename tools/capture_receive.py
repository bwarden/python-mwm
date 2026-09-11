#!/usr/bin/env python3
"""Passive, durable recorder for the rig receiver's RESULT topic.

Long-running listener for the overnight replay validation: subscribes to
the configured receive topic (``tele/tasmota/<id>/RESULT`` by default,
or ``--topic``) and appends every ``IrReceived`` line to a JSONL file,
one record per broker delivery, stamped with the local arrival
wall-clock time.

Record format (JSONL, one object per line)::

    {"t_unix": 1769000000.123, "t_iso": "...", "raw": "tele/tasmota/179E4E/RESULT {…IrReceived…}"}

``raw`` is the exact broker line (topic + JSON payload) so downstream
tools can re-decode with the shared MWM library.

Hangs are handled by a bounded subscription: ``mosquitto_sub -C 1 -W 30``
starts a fresh one-message subscription each loop, so a dead/quiescent
broker does not wedge the recorder forever.  Delivery is
software-paced (Tasmota clumps/reorders), which the comparison tool must
treat as an artifact, not hardware timing.

Usage::

    python3 tools/capture_receive.py --out analysis/replay/rx-20260909.jsonl
    python3 tools/capture_receive.py --topic tele/tasmota/179E4E/RESULT

Requires: mosquitto_sub on PATH and the MQTT config at
~/.config/ir-remote-tools/mqtt.json (or --mqtt-json).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mqtt import load_mqtt  # noqa: E402


def _subscribe(mqtt: dict, topic: str, timeout: int = 30) -> list[str]:
    """One bounded one-message subscription; returns the raw lines seen."""
    cmd = ["mosquitto_sub", "-h", mqtt["broker"], "-p", str(mqtt["port"]),
           "-u", mqtt["username"], "-P", mqtt["password"],
           "-t", topic, "-C", "1", "-W", str(timeout)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout + 5)
    except (TimeoutError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"  capture: subscription failed ({exc}); retrying",
              file=sys.stderr)
        return []
    if result.returncode and result.stderr.strip():
        print(f"  capture: mosquitto_sub: {result.stderr.strip()}",
              file=sys.stderr)
    return result.stdout.splitlines()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mqtt-json",
                    default=str(Path.home() / ".config/ir-remote-tools"
                                / "mqtt.json"))
    ap.add_argument("--topic", default=None,
                    help="RESULT topic to subscribe to (default: the "
                         "receive key in the MQTT config)")
    ap.add_argument("--out", required=True,
                    help="JSONL file to append received records to")
    ap.add_argument("--max-lines", type=int, default=0,
                    help="stop after this many records (0 = run until "
                         "interrupted)")
    args = ap.parse_args()

    mqtt = load_mqtt(Path(args.mqtt_json))
    topic = args.topic or mqtt.get("receive")
    if not topic:
        print("no receive topic configured and --topic not given",
              file=sys.stderr)
        sys.exit(2)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"capture: subscribing to {topic} -> {path}", file=sys.stderr)

    n = 0
    try:
        with path.open("a") as fh:
            while not args.max_lines or n < args.max_lines:
                for line in _subscribe(mqtt, topic):
                    if not line.strip():
                        continue
                    t = time.time()
                    rec = {"t_unix": t,
                           "t_iso": _dt.datetime.now(
                               _dt.timezone.utc).isoformat(),
                           "raw": line}
                    fh.write(json.dumps(rec) + "\n")
                    fh.flush()
                    n += 1
                    if not n % 20:
                        print(f"capture: {n} record(s) so far",
                              file=sys.stderr)
    except KeyboardInterrupt:
        pass
    print(f"capture: done, {n} record(s) appended", file=sys.stderr)


if __name__ == "__main__":
    main()