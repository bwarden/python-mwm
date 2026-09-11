#!/usr/bin/env python3
"""Measure how long each shutdown command silences beaconing.

The ears beacon every ~7-13 s when awake.  After a received command they go
quiet; each shutdown command has its OWN silence duration (simple off ~2 min,
the park command can be much longer).  For each command in ``--cmds``:

  1. WAIT for a beacon with NO sends -- confirms the ears are genuinely
     online before we touch them.  (If we sent while they were already in a
     quiet state the measurement would be meaningless.)
  2. Send the command (no fancy resets).
  3. Wait UP TO ``--max-wait`` for the FIRST beacon, recording how long after
     send it was, and stopping the moment it arrives.
     - first beacon promptly   => command did not suppress
     - first beacon ~120 s     => off-style suppression
     - first beacon much later => longer park-style suppression
  4. Wait again for a beacon (ears back online) before the next command, so
     every command starts from the same clean "online" point.

Fully non-interactive:

    python3 tools/shutdown_suppress.py --cmds 9060A6 --out suppress.json
    python3 tools/shutdown_suppress.py \
        --cmds 9060A6,91481FB2,914881BC --out suppress.json

IMPORTANT: pass the FULL frame including its trailer CRC byte (as the
protocol doc lists them, e.g. ``9060A6``, never ``9060``).  The ears reject
a frame missing its CRC, so an incomplete hex like ``9060`` transmits but
is ignored -- appearing as "did not suppress".  Use ``mwm.build_frame`` on
the content bytes (``mwm.build_frame([0x60])`` == ``9060A6``) if you only
have the raw command bytes.

The park drawn-shutdown frame (55 AA ...) silences far longer and is only
aborted by a battery pull; leave it out if you don't want to risk it.
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
crc8_dallas = mwm.crc8_dallas


def ensure_full_frame(hex_str: str) -> bytes:
    """Return a complete, valid-with-CRC frame for ``hex_str``.

    Confident framing: a `9x` show message whose low nibble L declares L+1
    content bytes must be L+3 bytes total (header + content + CRC).  If only
    L+2 bytes are given (no trailer CRC), append the required CRC-8/Dallas
    byte.  Does nothing if the frame is already the right length (CRC
    appended) or is a 55AA system message.
    """
    data = bytes.fromhex(hex_str.replace("0x", "").replace(" ", ""))
    if len(data) >= 2 and data[0] == 0x55 and data[1] == 0xAA:
        return data  # 55AA: additive checksum already expected at the end
    hdr = data[0]
    if (hdr & 0xF0) == 0x90:
        L = hdr & 0x0F
        expect = L + 3
        if len(data) == expect - 1:  # one byte short -> missing CRC
            data = bytes([*data, crc8_dallas(data)])
        elif len(data) == expect:
            ok, reason = mwm.frame_is_valid(data)
            if not ok:
                raise ValueError(f"frame not valid: {hex_str} ({reason})")
        else:
            raise ValueError(
                f"bad {hdr:02X} length: low-nibble L={L} wants {expect} bytes "
                f"total, got {len(data)} for {hex_str}")
    else:
        ok, reason = mwm.frame_is_valid(data)
        if not ok:
            raise ValueError(f"unrecognised frame {hex_str}: {reason}")
    return data


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


def wait_first_beacon(mqtt: dict, receiver_id: str, max_wait: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_wait:
        window = min(8.0, max_wait - (time.monotonic() - t0))
        if window <= 0:
            break
        if decode_beacons(capture_lines(mqtt, receiver_id, duration=window)):
            return time.monotonic() - t0
    return None


def wait_online(mqtt: dict, receiver_id: str, timeout: float) -> float:
    """Wait (no sends) until a beacon confirms the ears are online."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if decode_beacons(capture_lines(mqtt, receiver_id, duration=10)):
            return time.monotonic() - t0
    raise TimeoutError(f"no beacon within {timeout:.0f}s (ears not online)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cmds", required=True,
                    help="Comma-separated hex commands to test")
    ap.add_argument("--out", default="shutdown_suppress.json")
    ap.add_argument("--max-wait", type=float, default=1800.0,
                    help="Max seconds to wait for the first beacon")
    ap.add_argument("--online-timeout", type=float, default=240.0,
                    help="Max seconds to wait for a beacon (ears online) "
                         "before each command and after the previous one")
    ap.add_argument("--gap", type=float, default=0.0,
                    help="Pause (s) between commands")
    ap.add_argument("--receiver", default=None)
    ap.add_argument("--mqtt-json", default=None)
    args = ap.parse_args()

    mqtt = load_mqtt(args.mqtt_json)
    receiver_id = args.receiver or mqtt["transmit"].split("/")[2]
    cmds = [c.strip() for c in args.cmds.split(",") if c.strip()]
    print(f"Shutdown-suppression probe on {receiver_id}")
    print(f"  commands: {cmds}")
    print(f"  max wait for first beacon per command: {args.max_wait:.0f}s")

    results = []
    for i, hx in enumerate(cmds):
        # Wait for the ears to be genuinely online (no sends) before touching
        # them, so every command starts from the same clean baseline.
        print(f"\n=== {hx} ===", flush=True)
        try:
            on = wait_online(mqtt, receiver_id, args.online_timeout)
            print(f"  ears online (beacon after {on:.0f}s); sending...",
                  flush=True)
        except TimeoutError as e:
            print(f"  SKIP: {e}", flush=True)
            results.append({"cmd": hx, "first_beacon_s": None,
                            "note": "never came online before send"})
            continue

        frame = ensure_full_frame(hx)
        send_frame(mqtt, mwm, frame)
        print(f"  sent {frame.hex().upper()}; waiting up to "
              f"{args.max_wait:.0f}s for first beacon...", flush=True)
        t = wait_first_beacon(mqtt, receiver_id, args.max_wait)
        if t is None:
            note = f"> {args.max_wait:.0f}s"
            print(f"  NO beacon within {args.max_wait:.0f}s", flush=True)
        else:
            verdict = "did not suppress" if t < 120 else "suppressed"
            note = verdict
            print(f"  first beacon at t={t:.1f}s  -> {verdict}", flush=True)
        results.append({"cmd": hx, "first_beacon_s": (
            round(t, 1) if t is not None else None), "note": note})

        if i < len(cmds) - 1 and args.gap:
            print(f"(gap {args.gap:.0f}s) ...", flush=True)
            time.sleep(args.gap)

    out = {
        "session": datetime.now(timezone.utc).isoformat(),
        "receiver": receiver_id,
        "max_wait_s": args.max_wait,
        "results": results,
    }
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    print("\nSummary (time to first beacon after each command):")
    for r in results:
        v = r["first_beacon_s"]
        print(f"  {r['cmd']:<24} {str(v) if v is not None else 'n/a':>9}s  {r['note']}")


if __name__ == "__main__":
    main()