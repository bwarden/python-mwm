#!/usr/bin/env python3
"""Non-interactive shutdown-command latency probe on the MWM rig.

Each "shutdown" command type is sent to the ears, then the receiver is
listened on to measure how long until the ears start beaconing again.  A
command normally makes ears fall silent ~2 min (128-134 s measured) before
they resume demo beacon chatter; all shutdown types on this hardware behave
the same way (no command produced a longer "dead" window in testing, see
docs/mwm-show-protocol.md section 8).

The probe is fully non-interactive and logs a JSON result:

    python3 tools/shutdown_latency.py --out shutdown_latency.json
    python3 tools/shutdown_latency.py --max-wait 210

It runs:
  1. a CONTROL pass (no send) to measure the ambient beacon cadence, so we
     know the baseline gap a shutdown must exceed to be significant,
  2. one pass per shutdown type: send the command, then listen for the
     first decoded beacon, recording seconds-since-send.

Shutdown types (default):
  off_90_60    `90 60`   canonical both-ears off (keep-alive; reverts ~2 min)
  off_48_1F    `48 1F`   off effect
  off_48_81    `48 81`   power-off display sequence (pretend offline ~2 min)

The ride shutdown (`55 AA`) was thought to kill ears ~6 min; on this
hardware it behaves like the others (~2 min beacon silence, display left on)
-- see docs/mwm-show-protocol.md.  It is still EXCLUDED unless
`--include-ride` is given, with a loud warning, as a conservative default.

`--include-park` additionally probes the `55 AA` frames heard in the park
captures (EMLG000E / MRDF0007/8) -- the show-timecode families (`09 04 ...`
HH:MM:SS and the longer `19 04 ...` form) and the periodic system broadcast
(`16 01 01 01 15 02 30`).  Unlike the ride shutdown, there is no evidence
yet that ears process *any* of these; the docs call 55 AA a parallel/
ancestor protocol "heard constantly in parks" with an unknown consumer.
Sending them here measures whether they cause the same ~2 min beacon
silence (processed) or leave the ambient cadence untouched (ignored).
Each pass's subscription window is `--max-wait` seconds.

Decoder note (shared gotcha in docs/library-assumptions.md): beacons arrive
as a ~20 ms cluster on the air; the receiver logs each physical burst once
per Tasmota box, so "time to first beacon" is the time to the first decoded
beacon line from the receiver, and the *gap* after it (not the inter-cluster
jitter) is the meaningful reacquisition signal.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import mwm  # noqa: E402
from _mqtt import capture_lines, load_mqtt, send_frame  # noqa: E402

describe_frame = mwm.describe_frame
decode_timings = mwm.decode_timings
tasmota_timings = mwm.tasmota_timings
build_frame = mwm.build_frame

# Shutdown command types: (id, human label, frame bytes, risk).
_RIDE_FRAME = bytes.fromhex("55AA08C413FF01EDAFFF7A")  # 55AA ride shutdown
# Park 55 AA families heard in the ocean-side captures (EMLG000E /
# MRDF0007/8), sent verbatim as captured.  No evidence ears process these;
# each probe measures beacon-silence to find out.  See module docstring.
_PARK_FRAMES = [
    {
        "id": "park_55aa_tc_short",
        "label": "park timecode 09 04 (HH:MM:SS 00:00:00)",
        "frame": bytes.fromhex("55AA09040101150100000025"),
    },
    {
        "id": "park_55aa_tc_long",
        "label": "park timecode 19 04 (HH:MM:SS 00:26:58)",
        "frame": bytes.fromhex("55AA190401011501001A3A89"),
    },
    {
        "id": "park_55aa_broadcast",
        "label": "park broadcast 16 01 01 01 15 02 30",
        "frame": bytes.fromhex("55AA16010101150230"),
    },
]
_TYPES = [
    {
        "id": "control",
        "label": "no command (ambient control)",
        "frame": None,
        "risk": False,
    },
    {
        "id": "off_90_60",
        "label": "canonical both-ears off (90 60)",
        "frame": build_frame([0x60]),
        "risk": False,
    },
    {
        "id": "off_48_1F",
        "label": "off effect (48 1F)",
        "frame": build_frame([0x48, 0x1F]),
        "risk": False,
    },
    {
        "id": "off_48_81",
        "label": "power-off sequence (48 81)",
        "frame": build_frame([0x48, 0x81]),
        "risk": False,
    },
    {
        "id": "ride_55aa",
        "label": "ride shutdown (55 AA) -- beacon silence ~2 min, display stays on",
        "frame": _RIDE_FRAME,
        "risk": True,
    },
]
_TYPES.extend({**p, "risk": True, "park": True} for p in _PARK_FRAMES)


def is_beacon_frame(frame: bytes) -> bool:
    """True if a decoded frame looks like an idle beacon.

    Uses ``describe_frame`` (which unwraps the `99`-led bundle and reports
    ``kind == "beacon"`` for the demo-mode idle beacon) rather than
    ``describe_content``, which only sees the raw content bytes as an
    ``effect-command``.
    """
    try:
        return describe_frame(frame).get("kind") == "beacon"
    except Exception:
        return False


def decode_lines(mwm, lines: list[str]) -> list[bytes]:
    """Decode every frame in a burst, mirroring capture_beacons.py."""
    frames: list[bytes] = []
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        ir = data.get("IrReceived") if isinstance(data, dict) else None
        if not isinstance(ir, dict):
            continue
        if ir.get("Data"):
            try:
                frames.append(bytes.fromhex(ir["Data"].lstrip("0x")))
            except ValueError:
                pass
        if ir.get("RawData"):
            try:
                frames.extend(
                    decode_timings(tasmota_timings(ir["RawData"]))
                )
            except (ValueError, TypeError):
                pass
    return frames


def run_pass(mqtt: dict, receiver_id: str, spec: dict, max_wait: float) -> dict:
    """Send one shutdown command and measure seconds to the first beacon."""
    t0 = time.monotonic()
    if spec["frame"] is not None:
        frame_hex = spec["frame"].hex().upper()
        send_frame(mqtt, mwm, spec["frame"])
        print(f"    sent {frame_hex}")
    else:
        frame_hex = None
        print("    (control: no send, measuring ambient beacons)")

    first_beacon_rel: float | None = None
    first_frame_rel: float | None = None
    total_frames = 0
    then = time.monotonic()
    reported = 0.0
    while time.monotonic() - then < max_wait:
        elapsed = time.monotonic() - then
        window = min(8.0, max_wait - elapsed)
        if window <= 0:
            break
        lines = capture_lines(mqtt, receiver_id, duration=window)
        now = time.monotonic()
        frames_this = decode_lines(mwm, lines)
        total_frames += len(lines)
        for fr in frames_this:
            rel = now - t0
            if first_frame_rel is None:
                first_frame_rel = rel
            if is_beacon_frame(fr) and first_beacon_rel is None:
                first_beacon_rel = rel
                print(f"    [t={rel:6.1f}s] BEACON seen: {fr.hex().upper()}")
        # Progress every ~15s of listening so we can follow along.
        if elapsed - reported >= 15:
            reported = elapsed
            status = ("BEACON+rc%ds" % round(now - t0)
                      if first_beacon_rel is not None
                      else "still listening%s" % "")
            print(f"    [t={elapsed:6.1f}s] {status} "
                  f"(raw lines this window: {len(lines)})", flush=True)
        if first_beacon_rel is not None:
            print(f"    -> first beacon {first_beacon_rel:.1f}s after send")
            break

    return {
        "id": spec["id"],
        "label": spec["label"].split(" --")[0],
        "risk": spec["risk"],
        "hex": frame_hex,
        "send_cmd": frame_hex,
        "first_beacon_after_send_s": (
            round(first_beacon_rel, 2) if first_beacon_rel is not None else None
        ),
        "first_any_frame_after_send_s": (
            round(first_frame_rel, 2) if first_frame_rel is not None else None
        ),
        "raw_lines_seen": total_frames,
        "within_max_wait": first_beacon_rel is not None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure time-to-first-beacon after MWM shutdown commands.",
    )
    parser.add_argument(
        "--mqtt-json", default=str(Path.home() / ".config/ir-remote-tools/mqtt.json"),
        help="Path to MQTT config JSON",
    )
    parser.add_argument(
        "--out", default="shutdown_latency.json",
        help="Output JSON log (default: shutdown_latency.json)",
    )
    parser.add_argument(
        "--max-wait", type=float, default=150.0,
        help="Seconds to listen for a beacon after each command (default: 150)",
    )
    parser.add_argument(
        "--include-ride", action="store_true",
        help="Include the 55 AA ride shutdown (beacon silence ~2 min); "
             "off by default as a conservative choice",)
    parser.add_argument(
        "--include-park", action="store_true",
        help="Also probe the 55 AA park timecode/broadcast families "
             "(show timecodes 09/19 04 and the 16 01 periodic broadcast); "
             "no evidence ears process them -- this measures it",)
    parser.add_argument(
        "--receiver", default=None,
        help="Tasmota box ID to listen on for beacons (default: the transmit "
             "box, where the ears are directly in front of the IR receiver)",
    )
    args = parser.parse_args()

    mqtt = load_mqtt(args.mqtt_json)
    if args.receiver:
        receiver_id = args.receiver
        receive_topic = f"tele/tasmota/{receiver_id}/RESULT"
    else:
        # Ears sit directly in front of the transmit box's IR receiver, so it
        # sees everything; the configured 179E4E receiver is across the room.
        receiver_id = mqtt["transmit"].split("/")[2]
        receive_topic = f"tele/tasmota/{receiver_id}/RESULT"
    print(f"MWM shutdown latency probe (non-interactive)")
    print(f"  transmit: {mqtt['transmit']}")
    print(f"  listen:   {receive_topic}")
    print(f"  max-wait per pass: {args.max_wait}s")
    print(f"  counting ride shutdown: {args.include_ride}")
    print(f"  counting park 55 AA families: {args.include_park}")
    print(f"  (Ctrl-C to abort a pass; result logged only if it finishes)")

    types = [t for t in _TYPES
             if (t.get("park") and args.include_park)
             or (t["risk"] and not t.get("park") and args.include_ride)
             or not (t["risk"] or t.get("park"))]
    if args.include_ride:
        print("  WARNING: ride shutdown sends the 55 AA frame; on this "
              "hardware it behaves like the others (~2 min beacon silence)")
    if args.include_park:
        print("  WARNING: park 55 AA frames have no evidence ears process "
              "them; this pass measures beacon silence / ignition")

    results = []
    for spec in types:
        print(f"\n--- {spec['label']} ---", flush=True)
        res = run_pass(mqtt, receiver_id, spec, args.max_wait)
        results.append(res)
        got = res["first_beacon_after_send_s"]
        print(f"    => first beacon: {got if got is not None else 'NOT SEEN in window'}s", flush=True)

    out = {
        "session": datetime.now(timezone.utc).isoformat(),
        "mqtt_topic": mqtt["transmit"],
        "receiver_topic": receive_topic,
        "max_wait_s": args.max_wait,
        "notes": (
            "time-to-first-beacon after each shutdown command; control is "
            "the ambient cadence (no send). >=max_wait means not reacquired "
            "within the window."
        ),
        "results": results,
    }
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nWrote {len(results)} passes to {args.out}")
    print("\nSummary (seconds to first beacon after send):")
    print(f"  {'command':<28} {'s_to_first_beacon':>20}")
    for r in results:
        v = r["first_beacon_after_send_s"]
        print(f"  {r['id']:<28} {str(v if v is not None else 'n/a'):>20}")


if __name__ == "__main__":
    main()