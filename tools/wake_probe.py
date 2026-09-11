#!/usr/bin/env python3
"""Find which command wakes the ears early out of a beacon-suppression.

After a shutdown command the ears stop beaconing for a known duration (e.g.
`9060A6` ~130 s).  The ears will come back on their own at that natural time.
A "wake" command should restore beaconing much sooner than the natural
resume.  This tool:

  1. Sends the SUPPRESS command and waits until it has heard NO beacon for
     ``--confirm-quiet`` seconds (confirmed suppressed).
  2. For each WAKE candidate (content bytes passed to ``mwm.build_frame`` so
     the CRC is always correct), sends it and watches up to ``--probe``
     seconds for an early beacon.  A beacon arriving well inside the
     natural-silence window (special-cased as ``--early-s``, default 60 s)
     means that candidate woke the ears early.
  3. Between candidates, if one woke the ears, re-suppresses to reset the
     clock so every candidate starts from the same suppressed baseline.
  4. Logs a JSON summary.

Fully non-interactive:

    python3 tools/wake_probe.py --suppress 60 --wake "67|61" --out wake.json

(``--suppress 60`` means build_frame([0x60]) == 9060A6.  wake is a
pipe-separated list of candidates, each a comma-separated list of content
bytes: "67" -> 9067.. white; "48,1A" -> 91481A.. power-on blinks.)
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

decode_timings = mwm.decode_timings
describe_frame = mwm.describe_frame
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


def confirm_quiet(mqtt: dict, receiver_id: str, quiet_secs: float,
                  max_wait: float) -> bool:
    """Return True once ``quiet_secs`` of no-beacon elapses; False if beacons
    keep arriving through ``max_wait`` (already awake)."""
    start = time.monotonic()
    last_beacon = time.monotonic()
    first_beacon_since_start = None
    while time.monotonic() - start < max_wait:
        lines = capture_lines(mqtt, receiver_id, duration=6)
        now = time.monotonic()
        if decode_beacons(lines):
            last_beacon = now
            if first_beacon_since_start is None:
                first_beacon_since_start = now - start
        if now - last_beacon >= quiet_secs:
            # quiet confirmed only if NO beacon arrived the whole span
            print(f"  suppressed: {now - last_beacon:.0f}s silence "
                  f"after suppress", flush=True)
            return first_beacon_since_start is None
    print(f"  NOT suppressed (beacons kept coming {max_wait:.0f}s)", flush=True)
    return False


def wait_beacon_early(mqtt: dict, receiver_id: str, window: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < window:
        w = min(6.0, window - (time.monotonic() - t0))
        if w <= 0:
            break
        if decode_beacons(capture_lines(mqtt, receiver_id, duration=w)):
            return time.monotonic() - t0
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--suppress", required=True,
                    help="Content bytes of the shutdown command, e.g. 60")
    ap.add_argument("--wake", required=True,
                    help="Pipe-separated wake candidates, each comma-"
                         "separated content bytes, e.g. \"67|61|48,1A\"")
    ap.add_argument("--confirm-quiet", type=float, default=45.0,
                    help="No-beacon span treated as 'suppressed'")
    ap.add_argument("--suppress-timeout", type=float, default=160.0,
                    help="Max wait for suppression to hold")
    ap.add_argument("--probe", type=float, default=35.0,
                    help="Seconds to watch for an early beacon after a wake")
    ap.add_argument("--early-s", type=float, default=60.0,
                    help="A beacon sooner than this = woke early (natural "
                         "resume is ~130 s)")
    ap.add_argument("--pause", type=float, default=3.0,
                    help="Seconds between wake attempts")
    ap.add_argument("--out", default="wake_probe.json")
    ap.add_argument("--receiver", default=None)
    ap.add_argument("--mqtt-json", default=None)
    args = ap.parse_args()

    mqtt = load_mqtt(args.mqtt_json)
    receiver_id = args.receiver or mqtt["transmit"].split("/")[2]
    suppress_content = [int(x, 16) for x in args.suppress.split(",") if x]
    wake_list = [[int(x, 16) for x in w.split(",") if x]
                 for w in args.wake.split("|") if w]

    print(f"Wake probe on {receiver_id}")
    print(f"  suppress: {mwm.build_frame(suppress_content).hex().upper()}")
    results = []

    # Suppress.
    print(f"\n>>> send suppress; confirm quiet", flush=True)
    send_frame(mqtt, mwm, mwm.build_frame(suppress_content))
    quiet = confirm_quiet(mqtt, receiver_id, args.confirm_quiet,
                          args.suppress_timeout)
    results.append({"phase": "suppress", "cmd": args.suppress,
                    "confirmed_quiet": quiet})
    if not quiet:
        print("  ears not suppressed; aborting wake tests (nothing to wake)",
              flush=True)
        Path(args.out).write_text(json.dumps({"results": results}, indent=2))
        return

    # Try each wake candidate, re-suppressing between them.
    for wb in wake_list:
        frame = mwm.build_frame(wb)
        print(f"\n>>> wake {wb} -> {frame.hex().upper()}", flush=True)
        send_frame(mqtt, mwm, frame)
        t = wait_beacon_early(mqtt, receiver_id, args.probe)
        woke = t is not None and t < args.early_s
        print(f"  first beacon {t if t is not None else 'n/a'}s  "
              f"-> {'WOKE EARLY' if woke else 'no wake'}", flush=True)
        results.append({"phase": "wake", "cmd_bytes": wb,
                        "frame": frame.hex().upper(),
                        "time_to_beacon_s": t, "woke_early": woke})
        if args.pause:
            time.sleep(args.pause)
        # Reset the suppressed baseline for the next candidate only if this
        # one didn't already leave us awake; simplest is to re-suppress.
        print(f"  re-suppressing baseline...", flush=True)
        send_frame(mqtt, mwm, mwm.build_frame(suppress_content))
        time.sleep(1)
        confirm_quiet(mqtt, receiver_id, args.confirm_quiet / 2,
                      args.suppress_timeout)

    out = {
        "session": datetime.now(timezone.utc).isoformat(),
        "receiver_topic": f"tele/tasmota/{receiver_id}/RESULT",
        "suppress": mwm.build_frame(suppress_content).hex().upper(),
        "early_s": args.early_s,
        "results": results,
    }
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    print("Summary:")
    for r in results:
        if r["phase"] == "suppress":
            print(f"  suppress {r['cmd']}: quiet={r['confirmed_quiet']}")
        else:
            print(f"  wake {r['frame']}: "
                  f"beacon@ {r['time_to_beacon_s']}s "
                  f"-> {'WOKE EARLY' if r['woke_early'] else 'no'}")


if __name__ == "__main__":
    main()