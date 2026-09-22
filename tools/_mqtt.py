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

This module wraps paho-mqtt (a persistent MQTT client with a background
loop thread) so the tools share one connection and never spawn a
``mosquitto_pub``/``mosquitto_sub`` process.  Spawning a fresh CLI per
publish was the old design and had two problems that matter to this rig:
every frame paid a new TCP connect + MQTT handshake (a transient stall
delayed a single countdown member by ~1 s, which the ears hear as a
stutter), and the broker password sat in the subprocess argv for anyone
reading ``/proc``.  A persistent client connects once, resolves DNS once,
and sends each publish on the already-open socket; paho reconnects with
backoff when the broker drops us.

Use at the top of a tool::

    from _mqtt import load_mqtt, send_frame, send_payload, capture_lines

Requires the ``paho-mqtt`` package.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqttc

# ---------------------------------------------------------------------------
# Credential / config loading
# ---------------------------------------------------------------------------

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"

# A publish is retried briefly if the connection is still coming up or a
# transient broker blip dropped us (paho reconnects in the loop thread).
_RETRIES = 3
_RETRY_DELAY = 0.25

# capture_lines / subscribe_lines stop draining a topic's buffer after this
# many messages, mirroring mosquitto_sub's old -C 500 bound.
_MAX_CAPTURE = 500


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
# Persistent client (one connection per broker, shared by publish + receive)
# ---------------------------------------------------------------------------

class _MQTT:
    """One paho client plus a per-topic buffer of received lines."""

    def __init__(self, config: dict):
        self.config = config
        self.client = mqttc.Client()
        if config.get("username"):
            self.client.username_pw_set(config["username"],
                                        config.get("password") or None)
        self.client.reconnect_delay_set(min_delay=1, max_delay=5)
        self.client.on_message = self._on_message
        self._buf: dict[str, deque[str]] = {}
        self._lock = threading.Lock()
        self.client.connect(config["broker"], config["port"], keepalive=60)
        self.client.loop_start()

    def _on_message(self, _client, _userdata, msg) -> None:
        line = msg.payload.decode("utf-8", "replace")
        with self._lock:
            self._buf.setdefault(msg.topic, deque()).append(line)

    def drain(self, topic: str, limit: int = _MAX_CAPTURE) -> list[str]:
        """Take up to ``limit`` buffered lines for ``topic`` (FIFO)."""
        with self._lock:
            d = self._buf.get(topic)
            if not d:
                return []
            out = []
            while d and len(out) < limit:
                out.append(d.popleft())
            return out


_CLIENTS: dict[tuple[str, int, str], _MQTT] = {}


def _client(mqtt: dict) -> _MQTT:
    """Return the cached persistent client for this broker/credentials."""
    key = (mqtt["broker"], mqtt["port"], str(mqtt.get("username")))
    cli = _CLIENTS.get(key)
    if cli is None:
        cli = _MQTT(mqtt)
        _CLIENTS[key] = cli
    return cli


# ---------------------------------------------------------------------------
# Sending (persistent client -> Tasmota IRsend)
# ---------------------------------------------------------------------------

def _pub(mqtt: dict, payload: str) -> None:
    """Publish one raw payload string to the transmit topic.

    Runs on the persistent connection, so a send is a write to an open
    socket rather than a fresh process + TCP handshake.  If the publish
    does not make the wire quickly (connection still coming up, or a
    transient blip), it is retried with a short delay.
    """
    if len(payload) > 800:
        # Tasmota IRsend caps raw payloads well under this; catching an
        # over-long payload early gives a cleaner error than a cryptic one.
        raise ValueError(
            f"payload too long for Tasmota IRsend ({len(payload)} chars)")
    cli = _client(mqtt)
    topic = mqtt["transmit"]
    for attempt in range(1, _RETRIES + 1):
        info = cli.client.publish(topic, payload, qos=0)
        if info.rc == mqttc.MQTT_ERR_SUCCESS:
            try:
                info.wait_for_publish(5.0)
            except (ValueError, RuntimeError):
                pass
            if info.is_published():
                return
        if attempt < _RETRIES:
            time.sleep(_RETRY_DELAY)
            continue
        raise RuntimeError(
            f"MQTT publish failed (attempt {attempt}/{_RETRIES}) to "
            f"{mqtt['broker']}:{mqtt['port']} topic '{topic}'")


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
# Receiving (persistent subscription -> Tasmota RESULT)
# ---------------------------------------------------------------------------

def capture_lines(mqtt: dict, receiver_id: str, duration: float) -> list[str]:
    """Subscribe to one receiver's RESULT topic for ``duration`` seconds.

    Returns the raw JSON strings (each an ``IrReceived`` message) delivered
    during the window.  NOTE: Tasmota's reporting is software-paced and can
    clump/reorder beacons, so ordering is not trustworthy.
    """
    topic = f"tele/tasmota/{receiver_id}/RESULT"
    cli = _client(mqtt)
    cli.client.subscribe(topic, qos=0)
    lines: list[str] = []
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline and len(lines) < _MAX_CAPTURE:
            lines.extend(cli.drain(topic, _MAX_CAPTURE - len(lines)))
            time.sleep(0.05)
    finally:
        cli.client.unsubscribe(topic)
    return lines


def subscribe_lines(mqtt: dict, topic: str, timeout: float = 30.0) -> list[str]:
    """Subscribe to ``topic``; block up to ``timeout`` for at least one line.

    Returns whatever arrived during the wait (empty on timeout).  Used by
    long-running listener loops (``capture_receive`` / ``-stream`` callers)
    that want the anchored "wait for the next message" behaviour of the old
    ``mosquitto_sub -C 1 -W <timeout>`` with a persistent client.
    """
    cli = _client(mqtt)
    cli.client.subscribe(topic, qos=0)
    deadline = time.monotonic() + timeout
    try:
        while True:
            lines = cli.drain(topic)
            if lines:
                return lines
            if time.monotonic() >= deadline:
                return []
            time.sleep(0.05)
    finally:
        cli.client.unsubscribe(topic)


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