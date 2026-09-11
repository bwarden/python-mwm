#!/usr/bin/env python3
"""Synchronization analysis for MWM ear peripherals.

PURPOSE
-------
Human-driven rig research tool.  It captures beacons from multiple IR
receivers (600605 and 179E4E) and analyzes clock-tick synchronization
between them, optionally running clock-write and group-addressing probes to
see whether the ears can be synced on command.

This is the older, all-in-one precursor to the more focused tools in this
directory:
    capture_beacons.py         long-running capture to a JSONL file
    analyze_beacons.py         tick-rate / cross-receiver analysis offline
    clock_write_experiment.py  the clock-write probe in isolation
    mwm-send.py                interactive full-catalog sender
Keep those split tools for day-to-day use; this one stays for the combined
capture+analyze+experiment flow and is kept because it demonstrates the
per-beacon clock/effect extraction (BeaconCapture) in one place.

Requires: mosquitto_sub / mosquitto_pub on PATH, MQTT config at
~/.config/ir-remote-tools/mqtt.json, and the _mwm library (shared bootstrap).

Usage::

    python3 tools/sync_analysis.py [--duration SECS] [--mqtt-json PATH]
    python3 tools/sync_analysis.py --experiments
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mqtt import load_mqtt, _pub  # noqa: E402
from _bootstrap import mwm  # noqa: E402

build_frame = mwm.build_frame
irsend_payload = mwm.irsend_payload
describe_frame = mwm.describe_frame
decode_timings = mwm.decode_timings
build_clock_write = mwm.build_clock_write
decode_beacon_clock = mwm.decode_beacon_clock
tasmota_timings = mwm.tasmota_timings
crc8_dallas = mwm.crc8_dallas

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"


def _send_frame(mqtt: dict, frame: bytes) -> str:
    payload = irsend_payload(frame)
    _pub(mqtt, payload)
    return payload


# ---------------------------------------------------------------------------
# Beacon capture and analysis
# ---------------------------------------------------------------------------

class BeaconCapture:
    """Capture beacons from a specific receiver topic."""

    def __init__(self, mqtt: dict, receiver_id: str, label: str):
        self.mqtt = mqtt
        self.receiver_id = receiver_id
        self.label = label
        self.beacons: list[dict] = []
        self._stop = threading.Event()

    def start(self, duration: float) -> None:
        """Capture beacons for duration seconds."""
        from _mqtt import capture_lines
        for line in capture_lines(self.mqtt, self.receiver_id, duration):
            self._process_line(line)

    def _process_line(self, line: str) -> None:
        """Parse MQTT message and extract beacon data.

        Two sources of frames are merged:
        - The decoded MWM frame in ``IrReceived.Data`` (Tasmota's built-in
          decoder -- captures only the FIRST message of a bundle).
        - The full ``RawData`` timings (compact letter-coded), run through
          tasmota_timings() then decode_timings() to recover EVERY message
          in a bundle (e.g. an A-B-A' wand push).
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        frames: list[bytes] = []

        # Shape 1: decoded MWM frame from Tasmota's built-in decoder.
        ir = data.get("IrReceived") if isinstance(data, dict) else None
        if isinstance(ir, dict) and ir.get("Data"):
            try:
                frames.append(bytes.fromhex(ir["Data"].lstrip("0x")))
            except ValueError:
                pass

        # Shape 2: full RawData compact/comma timings (captures every message).
        if isinstance(ir, dict) and ir.get("RawData"):
            try:
                timings = tasmota_timings(ir["RawData"])
                frames.extend(decode_timings(timings))
            except (ValueError, TypeError):
                pass

        # Shape 3: numeric timings from a template bridge.
        for key in ("Timings", "timings", "raw_timings"):
            seq = data.get(key) if isinstance(data, dict) else None
            if not isinstance(seq, (list, tuple)):
                continue
            try:
                nums = [int(x) for x in seq if isinstance(x, (int, float, str))]
            except (ValueError, TypeError):
                continue
            if nums:
                frames.extend(decode_timings(nums))

        for frame in frames:
            try:
                desc = describe_frame(frame)
            except Exception:
                continue
            if desc.get("kind") == "beacon":
                clock_tick = decode_beacon_clock(frame)
                effect = desc.get("demo_effect")
                self.beacons.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "clock_tick": clock_tick,
                    "effect": effect,
                    "raw": frame.hex().upper(),
                })


def analyze_clock_sync(beacons_600605: list[dict], beacons_179E4E: list[dict]) -> dict:
    """Analyze clock tick synchronization between two receivers."""
    analysis = {
        "receiver_600605_count": len(beacons_600605),
        "receiver_179E4E_count": len(beacons_179E4E),
        "clock_ticks_600605": [b["clock_tick"] for b in beacons_600605],
        "clock_ticks_179E4E": [b["clock_tick"] for b in beacons_179E4E],
    }

    if not beacons_600605 or not beacons_179E4E:
        analysis["sync"] = "insufficient data"
        return analysis

    # Compare clock ticks at similar timestamps
    ticks_600605 = [b["clock_tick"] for b in beacons_600605]
    ticks_179E4E = [b["clock_tick"] for b in beacons_179E4E]

    analysis["tick_range_600605"] = f"{min(ticks_600605):02X} - {max(ticks_600605):02X}"
    analysis["tick_range_179E4E"] = f"{min(ticks_179E4E):02X} - {max(ticks_179E4E):02X}"

    # NOTE: Tasmota/MQTT reporting is IEEE-software-paced, clumped and
    # reordered, so raw arrival-wall-time alignment here is only a rough
    # guide.  Prefer tools/analyze_beacons.py, which accounts for that.
    if len(ticks_600605) > 1 and len(ticks_179E4E) > 1:
        tick_diffs_600605 = [ticks_600605[i+1] - ticks_600605[i]
                             for i in range(len(ticks_600605)-1)]
        tick_diffs_179E4E = [ticks_179E4E[i+1] - ticks_179E4E[i]
                             for i in range(len(ticks_179E4E)-1)]

        analysis["tick_progression_600605"] = tick_diffs_600605[:5]
        analysis["tick_progression_179E4E"] = tick_diffs_179E4E[:5]

        if tick_diffs_600605 and tick_diffs_179E4E:
            avg_diff_600605 = sum(tick_diffs_600605) / len(tick_diffs_600605)
            avg_diff_179E4E = sum(tick_diffs_179E4E) / len(tick_diffs_179E4E)
            analysis["avg_tick_rate_600605"] = avg_diff_600605
            analysis["avg_tick_rate_179E4E"] = avg_diff_179E4E

            if abs(avg_diff_600605 - avg_diff_179E4E) < 2:
                analysis["sync"] = "likely synchronized (similar tick rates)"
            else:
                analysis["sync"] = "likely not synchronized (different tick rates)"

    return analysis


# ---------------------------------------------------------------------------
# Clock tick manipulation experiments
# ---------------------------------------------------------------------------

def experiment_clock_write(mqtt: dict) -> None:
    """Test writing specific clock ticks to see if ears synchronize."""
    print("\n=== Clock Write Experiment ===")
    print("Sending commands with different clock tick values...")

    # First, reset both ears
    reset_frame = build_frame([0x24])
    print("Resetting ears...")
    _send_frame(mqtt, reset_frame)
    time.sleep(1)

    # Send color command with clock tick 0x00
    clock_00 = build_clock_write(0x00)
    print(f"Sending clock=0x00: {clock_00.hex()}")
    _send_frame(mqtt, clock_00)
    time.sleep(0.5)

    # Send blue command
    blue_frame = build_frame([0x61])
    print(f"Sending blue: {blue_frame.hex()}")
    _send_frame(mqtt, blue_frame)
    time.sleep(2)

    # Send color command with clock tick 0x80
    clock_80 = build_clock_write(0x80)
    print(f"Sending clock=0x80: {clock_80.hex()}")
    _send_frame(mqtt, clock_80)
    time.sleep(0.5)

    # Send blue command again
    print(f"Sending blue again: {blue_frame.hex()}")
    _send_frame(mqtt, blue_frame)
    time.sleep(2)

    # Reset
    print("Resetting...")
    _send_frame(mqtt, reset_frame)


def experiment_group_addressing(mqtt: dict) -> None:
    """Test group addressing to coordinate multiple ears."""
    print("\n=== Group Addressing Experiment ===")
    print("Testing group addressing commands...")

    # Reset ears
    reset_frame = build_frame([0x24])
    print("Resetting ears...")
    _send_frame(mqtt, reset_frame)
    time.sleep(1)

    # Try a group addressing command from the protocol docs
    # 98 20 D2 35 00 F2 01 02 20 66 1D (PRNG seed)
    group_seed = bytes([0x98, 0x20, 0xD2, 0x35, 0x00, 0xF2, 0x01, 0x02, 0x20, 0x66])
    crc_seed = crc8_dallas(group_seed)
    group_seed_with_crc = group_seed + bytes([crc_seed])
    print(f"Sending group PRNG seed: {group_seed_with_crc.hex()}")
    _send_frame(mqtt, group_seed_with_crc)
    time.sleep(0.5)

    # Try a group-addressed color command
    # 97 20 89 A0 19 26 6E F2 66 F8 (yellow, groups 00-18)
    group_color = bytes([0x97, 0x20, 0x89, 0xA0, 0x19, 0x26, 0x6E, 0xF2, 0x66])
    crc_color = crc8_dallas(group_color)
    group_color_with_crc = group_color + bytes([crc_color])
    print(f"Sending group color (yellow, groups 00-18): {group_color_with_crc.hex()}")
    _send_frame(mqtt, group_color_with_crc)
    time.sleep(2)

    # Reset
    print("Resetting...")
    _send_frame(mqtt, reset_frame)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    mqtt = load_mqtt(Path(args.mqtt_json))

    print("MWM Synchronization Analysis")
    print(f"MQTT broker: {mqtt['broker']}")
    print(f"Transmit topic: {mqtt['transmit']}")
    print(f"Duration: {args.duration}s")
    print()

    # Capture beacons from both receivers
    print("=== Beacon Capture ===")
    print(f"Capturing from 600605 and 179E4E for {args.duration}s...")

    capture_600605 = BeaconCapture(mqtt, "600605", "Primary")
    capture_179E4E = BeaconCapture(mqtt, "179E4E", "Secondary")

    # Run captures in parallel
    thread_600605 = threading.Thread(
        target=capture_600605.start, args=(args.duration,)
    )
    thread_179E4E = threading.Thread(
        target=capture_179E4E.start, args=(args.duration,)
    )

    thread_600605.start()
    thread_179E4E.start()

    thread_600605.join()
    thread_179E4E.join()

    print(f"Captured {len(capture_600605.beacons)} beacons from 600605")
    print(f"Captured {len(capture_179E4E.beacons)} beacons from 179E4E")

    # Analyze clock sync
    print("\n=== Clock Sync Analysis ===")
    analysis = analyze_clock_sync(capture_600605.beacons, capture_179E4E.beacons)
    for key, value in analysis.items():
        print(f"  {key}: {value}")

    # Run experiments if requested
    if args.experiments:
        print("\n=== Running Experiments ===")
        experiment_clock_write(mqtt)
        time.sleep(2)
        experiment_group_addressing(mqtt)

    # Save results
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration": args.duration,
        "beacons_600605": capture_600605.beacons,
        "beacons_179E4E": capture_179E4E.beacons,
        "analysis": analysis,
    }

    output_path = Path(args.out) if args.out else Path(
        f"sync_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    output_path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nResults saved to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MWM synchronization analysis",
    )
    parser.add_argument(
        "--duration", type=int, default=30,
        help="Capture duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--mqtt-json", default=str(_DEFAULT_MQTT),
        help="Path to MQTT config JSON (default: ~/.config/ir-remote-tools/mqtt.json)",
    )
    parser.add_argument(
        "--experiments", action="store_true",
        help="Run clock write and group addressing experiments",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output JSON file (default: sync_analysis_YYYYMMDD_HHMMSS.json)",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
