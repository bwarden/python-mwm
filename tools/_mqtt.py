#!/usr/bin/env python3
"""Shared MQTT helpers for the MWM rig research tools in this directory.

The tools send IR by publishing a "raw timings" payload to a Tasmota
``IRsend`` topic, and receive by subscribing to a Tasmota IR receiver's
``RESULT`` topic.  The broker address/credentials and which topics to use
live OUTSIDE this repo, in ``~/.config/ir-remote-tools/mqtt.json``
(created by setup; never commit credentials).  That file looks like::

    {
      "broker": "mqtt.wgz.org",
      "port": 1883,
      "username": "ai",
      "password": "...",
      "transmit": "cmnd/tasmota/600605/IRsend",
      "receive": "tele/tasmota/179E4E/RESULT",
      ...
    }

This module wraps the mosquitto CLIs (mosquitto_pub / mosquitto_sub) so the
tools don't each re-implement credential loading and subprocess calls.  The
mosquitto clients must be installed and on PATH.

Use at the top of a tool::

    from _mqtt import load_mqtt, send_frame, send_payload, capture_frames

For a single-shot send you typically want send_frame(); for live monitoring
(of beacons / responses) you want capture_frames() in a thread.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Credential / config loading
# ---------------------------------------------------------------------------

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"

# mosquitto_pub / broker are unreliable: name resolution ("Lookup error.")
# and TCP connects can transiently fail, so a send is retried a few times
# before we give up.  Each retry also covers a lost keepalive/conn refused.
_RETRIES = 3
_RETRY_DELAY = 0.25

# Resolve the broker host ONCE and reuse the IP for every send.  The name
# resolver here is flaky (systemd-resolved intermittently times out /
# SERVFAILs, taking seconds), which made per-send `mosquitto_pub` lookups
# both slow and failure-prone.  Pinning the IP after one successful lookup
# makes sends ~10ms and deterministic.
_IP_CACHE: dict[str, str] = {}


def load_mqtt(path: str | Path | None = None) -> dict:
    """Load the MQTT config JSON.

    ``path`` defaults to ``~/.config/ir-remote-tools/mqtt.json``.
    Returns the parsed dict (keys: broker, port, username, password,
    transmit, receive, ...).
    """
    p = Path(path) if path else _DEFAULT_MQTT
    with open(p) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Sending (mosquitto_pub -> Tasmota IRsend)
# ---------------------------------------------------------------------------

def _broker_addr(mqtt: dict) -> str:
    """Return broker host/IP, resolving the name once and caching it.

    Prefer a numeric IP so ``mosquitto_pub`` never re-runs the flaky resolver.
    Falls back to the configured hostname if resolution fails right now.
    """
    host = mqtt["broker"]
    cached = _IP_CACHE.get(host)
    if cached:
        return cached
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        raise RuntimeError(
            f"cannot resolve MQTT broker '{host}' -- check DNS/network")
    _IP_CACHE[host] = ip
    return ip


def _pub(mqtt: dict, payload: str) -> None:
    """Publish one raw payload string to the transmit topic.

    The broker host is resolved once and the IP reused (see _broker_addr);
    a transient TCP/connect failure after that is retried, and on final
    failure a clean message is raised rather than a raw subprocess traceback.
    """
    if len(payload) > 800:
        # Tasmota IRsend caps raw payloads well under this; catching an
        # over-long payload early gives a cleaner error than a cryptic one.
        raise ValueError(
            f"payload too long for Tasmota IRsend ({len(payload)} chars)")
    broker = _broker_addr(mqtt)
    cmd = ["mosquitto_pub", "-h", broker, "-p", str(mqtt["port"]),
           "-u", mqtt["username"], "-P", mqtt["password"],
           "-t", mqtt["transmit"], "-m", payload]
    for attempt in range(1, _RETRIES + 1):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return
        err = (res.stderr or "").strip()
        if attempt < _RETRIES:
            time.sleep(_RETRY_DELAY)
            continue
        raise RuntimeError(
            f"mosquitto_pub failed (attempt {attempt}/{_RETRIES}, rc "
            f"{res.returncode}) to {mqtt['broker']} ({broker}):{mqtt['port']}"
            + (f": {err}" if err else ""))


def send_payload(mqtt: dict, payload: str, repeat: int = 1,
                 delay: float = 0.0) -> None:
    """Send a raw ``IRsend`` payload string, optionally repeated.

    ``repeat`` sends it N times with ``delay`` seconds between each, which
    helps a human actually see a very short IR blip.  Returns when done.
    """
    for _ in range(repeat):
        _pub(mqtt, payload)
        if delay > 0:
            time.sleep(delay)


def send_frame(mqtt: dict, mwm, frame: bytes, repeat: int = 1,
               delay: float = 0.0) -> str:
    """Build and send an IR frame; return the exact payload used.

    ``mwm`` is the protocol library (from _bootstrap) used to encode the
    frame into Tasmota's comma-separated raw timing payload via
    ``irsend_payload()``.  ``frame`` is a bytes object (e.g. the output of
    ``mwm.build_frame(...)`` or ``mwm.parse_frame_hex(...)``).
    """
    payload = mwm.irsend_payload(frame)
    send_payload(mqtt, payload, repeat=repeat, delay=delay)
    return payload


# ---------------------------------------------------------------------------
# Receiving (mosquitto_sub -> Tasmota RESULT)
# ---------------------------------------------------------------------------

def capture_lines(mqtt: dict, receiver_id: str, duration: float) -> list[str]:
    """Subscribe to one receiver's RESULT topic for ``duration`` seconds.

    Returns the raw JSON strings (each an ``IrReceived`` message) delivered
    during the window.  NOTE: mosquitto_sub returns whatever the broker
    delivers in the window; Tasmota's reporting is software-paced and can
    clump/reorder beacons, so ordering is not trustworthy.
    """
    topic = f"tele/tasmota/{receiver_id}/RESULT"
    cmd = ["mosquitto_sub", "-h", mqtt["broker"], "-p", str(mqtt["port"]),
           "-u", mqtt["username"], "-P", mqtt["password"],
           "-t", topic, "-W", str(int(duration) + 2), "-C", "500"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=duration + 6)
        return result.stdout.splitlines()
    except (TimeoutError, subprocess.TimeoutExpired, OSError):
        return []


def decode_frame_lines(mwm, lines: list[str]) -> list[bytes]:
    """Extract decoded IR frames (as bytes) from RESULT JSON lines.

    Each Tasmota RESULT may carry:
      - ``IrReceived.Data`` -- the FIRST frame of a bundle, already decoded
        by Tasmota's built-in decoder, and
      - ``IrReceived.RawData`` -- compact/tagged timing data for the WHOLE
        burst, which we re-run through our own decoder to recover every
        frame (e.g. an A-B-A' wand push).

    Both are merged.  Returns a list of frame bytes.
    """
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
                    mwm.decode_timings(mwm.tasmota_timings(ir["RawData"]))
                )
            except (ValueError, TypeError):
                pass
    return frames
