#!/usr/bin/env python3
"""Passive MWM radio listener: capture -> decode -> human-annotate.

PURPOSE
-------
A human-driven rig research tool for the DEMO MODE / WAND side of the park:
it sits on a Tasmota IR receiver and listens for whatever the ears emit on
their own (demo-mode beacons, effect chatter) and whatever a wand or remote
pushes in (A-B-A' command bundles).  For every burst it prints the raw
frame hex plus a best-effort decode (tokens, effect indices, wand program
names, palette references), then ASKS THE HUMAN what the command actually
DID so the interaction can be named and logged.  That annotation stream is
the raw material for extending the verified command catalogue.

It is the passive/learning counterpart to capture_beacons.py (which logs
beacons untouched) and mwm-send.py (which drives the ears deliberately):
this tool decodes on the fly and grows the corpus via the observer.

Two modes -- the same annotation loop, different input:

  # live: subscribe to the receiver topic(s) and annotate as it happens
  python3 tools/annotate_captures.py --rcvs 600605,179E4E

  # piped / replayed: annotate a recorded RESULT JSONL feed instead
  mosquitto_sub -h broker -p 1883 -u ai -P '..' \
      -t tele/tasmota/600605/RESULT |
      python3 tools/annotate_captures.py -

  python3 tools/annotate_captures.py /tmp/result_session.jsonl --all

Each annotation is appended to the annotations log (--out, default
``annotations_<timestamp>.jsonl``):

    {"t_unix":..., "t_iso":..., "receiver":"600605",
     "kind":"bundle", "summary":"A-B-A' bundle: ...",
     "hex":[phrase], "triplet":[A,B,A'], "params":[...],
     "timing":[...], "colors":[...], "novel":true/false,
     "note":"wand tilt = green L/R/off cycle"}

NOTES / CAVEATS
---------------
- Frames are decoded from ``IrReceived.RawData`` with OUR decoder (the same
  path every other rig tool uses), NOT from Tasmota's built-in
  ``IrReceived.Data`` decode: that decoder only knows the FIRST message of
  an A-B-A' bundle at best, so trusting it is exactly how whole commands
  go unheard.  ``Data`` is used only as a last resort when RawData is
  missing or undecodable, and only when Tasmota itself labels the message
  ``Protocol: MWM`` and the payload still validates as a real frame.
- A-B-A' wand pushes arrive as THREE separate IR bursts (one per RESULT
  line).  The tool reassembles the triplet across lines within a short
  window and treats the completed set as ONE bundle -- one render, one
  prompt, one log row carrying all three frames -- so the observer matches
  behavior to the whole push instead of answering per burst.
- Beacons (the idle ``42 ...`` demo chatter) are condensed to a status line
  UNLESS the demo effect is one the library knows (``EFFECT_LABELS``) or has
  observed in demo mode (``DEMO_BEACONS``) -- a known demo beacon prints our
  interpretation and DOES prompt, so the observer can confirm or correct how
  that effect is read.  ``--all`` forces
  every line through a full prompt.  Wand pushes / effect / color / 55aa /
  invalid bursts prompt for a note.  Hit return for no note, ``s`` to reuse
  the previous note, ``q`` to quit.
- Cycle siblings are recognized: a burst that is the SAME command as the one
  just seen, differing only in the pacing bytes (``58 tt`` / ``59`` / ``5A``
  / ``D0 3D yy`` / ``D0 42 yy`` timing cells or the beacon clock tick), is a
  later phase of the same cycle.  It is shown as a timing/phase delta and is
  NOT re-prompted -- so a slow cadence ``D0 42 06`` sliding to ``D0 42 20``
  is surfaced as exactly the byte that moved, without interrupting the
  observer again.
- New frames that arrive while a note is being typed are superseded: when
  the note commits, the queue is collapsed to its NEWEST entry and the next
  prompt is for that burst alone (intermediate frames are dropped, not
  replayed), so the observer always reports on the most recently heard
  burst and is never interrupted mid-keystroke.
- "novel" flags decodes the library does NOT yet understand (undocumented
  wand (gg,kk) pairs, unknown opcodes, invalid frames) so the observer can
  concentrate on the unknowns that move the state of knowledge.
- Requires the mosquitto CLIs on PATH and MQTT credentials in
  ~/.config/ir-remote-tools/mqtt.json (never committed).  Run ONE live
  instance per topic, or rely on the piped form.
- Tasmota MQTT reporting is software-paced: arrival order/timestamps are
  not hardware-accurate.  The radio adds its own CRC so frames that decode
  are real; ordering of a clump is best-effort.

Usage::

    python3 tools/annotate_captures.py [--out FILE] [--mqtt-json PATH]
        [--rcvs ID,ID,...] [--all] [SOURCE]
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402
from _mqtt import load_mqtt  # noqa: E402

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"

# Captures that earn a human prompt; beacons live outside this set so the
# endless idle chatter never interrupts (condensed to a status line).
_PROMPTING_KINDS = {"bundle", "wand-command", "effect-command",
                    "color-command", "command", "55aa", "invalid"}


# ---------------------------------------------------------------------------
# Pure decode helpers (unit-tested)
# ---------------------------------------------------------------------------

def extract_frames(mwm, line: str) -> list[bytes]:
    """All IR frames in one Tasmota RESULT line, best-effort order.

    Frames come from ``IrReceived.RawData``: compact/tagged timing data for
    the WHOLE burst, which we re-run through our own MWM decoder to recover
    every message (an A-B-A' wand push lands as three bursts in one shot).
    Tasmota's built-in ``IrReceived.Data`` decoder is NOT trusted for this:
    it only recognises the first message of a bundle at best, so relying on
    it is exactly why whole commands can go unheard.  ``Data`` is used only
    as a fallback when RawData is missing or cannot be decoded, and only
    when Tasmota claims the MWM protocol AND the payload still validates as
    a real frame.  Exact consecutive duplicates within a capture are
    collapsed so a repeated frame never double-counts.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return []
    ir = data.get("IrReceived") if isinstance(data, dict) else None
    if not isinstance(ir, dict):
        return []
    frames: list[bytes] = []
    if ir.get("RawData"):
        try:
            frames = mwm.decode_timings(
                mwm.tasmota_timings(ir["RawData"]))
        except (ValueError, TypeError):
            frames = []
    if not frames:
        # RawData absent or unreadable this round: fall back to Tasmota's
        # own MWM decode, but only when it names the MWM protocol and the
        # result survives validation.
        frames = _data_fallback(mwm, ir)
    out: list[bytes] = []
    for fr in frames:
        if not out or out[-1] != fr:
            out.append(fr)
    return out


def _data_fallback(mwm, ir: dict) -> list[bytes]:
    """Validated ``Data``-field fallback, gated on the MWM protocol.

    Only a line Tasmota itself labels ``Protocol: MWM`` may contribute its
    ``Data`` field here -- any other claimed protocol is, by definition, not
    one of our ears.  Even then the payload has to parse AND pass frame
    validation before we trust it: the Tasmota decoder is the last resort,
    not the source.
    """
    proto = str(ir.get("Protocol", "")).upper()
    if proto != "MWM" or not ir.get("Data"):
        return []
    try:
        fr = bytes.fromhex(ir["Data"].lstrip("0x"))
    except ValueError:
        return []
    ok, _ = mwm.frame_is_valid(fr)
    return [fr] if ok else []


def _novel(desc: dict) -> bool:
    """True when the decoder does not really understand the frame."""
    if desc.get("kind") == "invalid":
        return True
    summary = desc.get("summary", "")
    if "undocumented" in summary:
        return True
    # A bundle summary quotes its unknown phrase bytes ("unknown 0x19");
    # the per-frame path flags those via tokens, the bundle path must too.
    if "unknown" in summary:
        return True
    return any(
        tok.strip().lower().startswith("unknown")
        for tok in desc.get("tokens", [])
    )


def burst_record(mwm, frames: list[bytes]) -> dict:
    """One human-visible record for a burst: bundle or per-frame decode.

    A-B-A' wand triplets collapse into a single bundle record; anything
    else degrades to one record entry per frame, each carrying its own
    kind/summary/novel flag.
    """
    if len(frames) == 3:
        bundle = mwm.describe_bundle(frames)
        if bundle is not None:
            return {
                "kind": "bundle",
                "summary": bundle["summary"],
                "params": bundle.get("params", []),
                "timing": bundle.get("timing", []),
                "colors": bundle.get("colors", []),
                "hex": [bundle["phrase_hex"]],
                # The whole radio set, in arrival order: phrase, companion,
                # phrase again -- the three bursts that make one push.
                "triplet": [fr.hex().upper() for fr in frames],
                "novel": _novel({"summary": bundle["summary"], "tokens": []}),
            }
    per: list[dict] = []
    for fr in frames:
        try:
            desc = mwm.describe_frame(fr)
        except Exception:
            desc = {"kind": "invalid", "summary": "decode error",
                    "tokens": []}
        per.append({
            "hex": fr.hex().upper(),
            "kind": desc.get("kind", "command"),
            "summary": desc.get("summary", ""),
            "timing": desc.get("timing", []),
            "colors": desc.get("colors", []),
            "novel": _novel(desc),
            # Beacon extras for the condensed status line.
            "clock": (mwm.decode_beacon_clock(fr)
                      if desc.get("kind") == "beacon" else None),
            "demo_effect": desc.get("demo_effect"),
        })
    return {"kind": "frames", "frames": per,
            "novel": any(p["novel"] for p in per),
            "summary": " | ".join(p["summary"] for p in per)}


# Frames of a wand/brush/ear push arrive as ONE RawData capture containing
# the whole A-B-A' set (three frames transmitted back-to-back, one IR
# message); see ``burst_record`` for how a completed set is annotated as a
# single bundle.


def command_key(rec: dict) -> str:
    """Identity of one underlying command, ignoring cycle-phase bytes.

    The key is built from everything EXCEPT the timing fields (the ``58 tt``
    / ``D0 42 tt`` bytes that pace the cycle) -- so two bursts with the same
    effect, colors, and command-kind but different timing are recognized as
    the SAME command stepped through its cycle, not new traffic.  For a
    bundle the effect lives in the params; color+effect form the key.
    """
    if rec["kind"] == "bundle":
        return "bundle:" + "|".join(rec.get("params", []))
    cells = []
    for f in rec["frames"]:
        if f["kind"] == "beacon":
            cells.append(f"beacon:{f['demo_effect']}")
        else:
            cells.append(f"{f['kind']}:{':'.join(f.get('colors', []))}")
    return "|".join(cells)


def _repeated(seen: dict, rec: dict, window_s: float) -> bool:
    """True when this burst is a fresh phase of an already-seen command.

    Tracks the last arrival time per command_key; a burst that repeats the
    same command inside ``window_s`` is a sibling within its cycle (only the
    timing bytes moved), not new traffic.
    """
    key = command_key(rec)
    now = time.monotonic()
    last = seen.get(key)
    if last is not None and (now - last) <= window_s:
        seen[key] = now
        return True
    seen[key] = now
    return False


def _render(rec: dict) -> list[str]:
    """Human-readable lines for a burst record."""
    if rec["kind"] == "bundle":
        lines = [f"  BUNDLE (complete A-B-A' set)  {rec['hex'][0]}"]
        for label, hx in zip(("A", "B", "A'"), rec.get("triplet", [])):
            lines.append(f"          {label}: {hx}")
        lines.append(f"          {rec['summary']}")
        lines += [f"          param: {p}" for p in rec["params"]]
        return lines
    lines = []
    for f in rec["frames"]:
        mark = " [NOVEL]" if f["novel"] else ""
        lines.append(
            f"  {f['hex']}  {f['kind']:17s}{f['summary']}{mark}")
        if f.get("timing"):
            lines.append(f"        timing: {' '.join(f['timing'])}")
    return lines


def _timing_delta(old, new) -> str:
    """Compare two same-command timing-cell sets; short phase-delta text.

    Accepts either bare lists of timing cells (``["D0 42 06"]``) or
    records/dicts that carry a ``timing`` key (a bundle), so the same helper
    serves both the per-frame and the bundle comparison paths.
    """
    old_t = old["timing"] if isinstance(old, dict) and "timing" in old else old
    new_t = new["timing"] if isinstance(new, dict) and "timing" in new else new
    changed = []
    for a, b in zip(old_t, new_t):
        if a != b:
            changed.append(f"{{{a} -> {b}")
    if len(old_t) != len(new_t):
        changed.append(f"[{len(old_t)} -> {len(new_t)} cells]")
    return ", ".join(changed) if changed else "unchanged"


# ---------------------------------------------------------------------------
# The annotation loop
# ---------------------------------------------------------------------------

class _Quit(Exception):
    pass


def _say(msg: str) -> None:
    print(msg, flush=True)


def _tty_input(prompt_text: str) -> str:
    """Read a note from the controlling terminal, not from stdin.

    In piped mode the RESULT feed IS stdin, so ``input()`` would silently
    consume the next record as the note.  The annotation prompt always talks
    to /dev/tty (when one exists), keeping the feed intact -- so a prompt in
    a non-interactive (no-tty) run yields an empty note instead of eating the
    next stdin record.
    """
    import os

    sys.stdout.write(prompt_text + "\n")
    sys.stdout.flush()
    try:
        with open(os.ctermid(), "r") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return ""


def annotate_line(mwm, line: str, receiver: str, out_fh, last_note,
                  include_beacons: bool = False, prev=None,
                  input_fn=_tty_input, print_fn=_say,
                  seen: dict | None = None,
                  repeat_window_s: float = 8.0):
    """Decode one RESULT line; show the decode and gather the note.

    Thin wrapper that extracts the frames and hands them to
    ``annotate_burst`` (the real annotation driver) -- kept for tests and
    single-line callers.
    """
    frames = extract_frames(mwm, line)
    if not frames:
        return last_note, True
    return annotate_burst(mwm, frames, receiver, out_fh, last_note,
                          include_beacons, prev=prev, input_fn=input_fn,
                          print_fn=print_fn, seen=seen,
                          repeat_window_s=repeat_window_s)


def annotate_burst(mwm, frames: list[bytes], receiver: str, out_fh,
                   last_note, include_beacons: bool = False, prev=None,
                   input_fn=_tty_input, print_fn=_say,
                   seen: dict | None = None,
                   repeat_window_s: float = 8.0):
    """Annotate one completed burst (a bundle set or a single frame).

    Returns ``(last_note, handled)`` where ``handled`` is False when the
    burst was only a repeated phase of an already-annotated command (so the
    caller can drop it).  Raises ``_Quit`` when the observer quits.

    Prompting rules:
      * An A-B-A' triplet arrives here as ONE completed set -- a bundle
        record -- and prompts once, so the observer matches behavior to the
        whole push rather than answering per burst.
      * A burst that is a repeat (same command, only timing bytes moved --
        a sibling within its cycle) is printed as a timing delta and does
        NOT re-prompt, unless it is a known demo beacon and it was novel.
      * Known demo beacons DO prompt, with our interpretation, so the
        observer can confirm or correct how that effect is read (known =
        ``EFFECT_LABELS`` or ``DEMO_BEACONS``).
      * Unknown-effect beacons are condensed to a status line.
      * ``include_beacons`` forces everything to a full prompt.
    """
    if seen is None:
        seen = {}
    if not frames:
        return last_note, True
    rec = burst_record(mwm, frames)
    stamp = datetime.now(timezone.utc)
    now = stamp.strftime("%H:%M:%S")

    is_beacon_run = (
        rec["kind"] == "frames"
        and all(f["kind"] == "beacon" for f in rec["frames"])
    )
    is_repeat = _repeated(seen, rec, repeat_window_s)

    # A repeat of an already-annotated command: only the phase moved, so
    # show the timing delta but keep the conversation calm unless we're in
    # full-prompt mode.
    if is_repeat:
        if include_beacons or rec.get("novel"):
            _render_burst(mwm, rec, prev, receiver, now, print_fn)
            note = _prompt_note(mwm, rec, last_note, input_fn, print_fn)
            _log(out_fh, stamp, receiver, rec, note)
            return note, True
        if is_beacon_run:
            effects = sorted({f["demo_effect"] for f in rec["frames"]})
            labels = ", ".join(f"0x{idx:02X} {mwm.demo_beacon_label(idx)}"
                               for idx in effects)
            phase = (_phase_delta(prev, rec)
                     if prev is not None else None)
            tail = f", phase {phase}" if phase else ""
            print_fn(f"  {now}  {receiver}: {len(frames)} beacon(s) "
                     f"(demo={labels}, repeat{tail})")
            return last_note, False
        phase_note = (f"  phase {_phase_delta(prev, rec)}"
                      if prev is not None else "")
        print_fn(f"  {now}  {receiver}: (repeat) "
                 f"{rec['summary']}{phase_note}")
        return last_note, False

    # Not a repeat.
    if is_beacon_run and not include_beacons:
        # Condense unknown-effect beacons to a status line, but a KNOWN demo
        # still earns a prompt so the observer can confirm the label.
        effects = sorted({f["demo_effect"] for f in rec["frames"]})
        known = all(
            e in mwm.EFFECT_LABELS or e in mwm.DEMO_BEACONS
            for e in effects
        )
        labels = ", ".join(f"0x{idx:02X} {mwm.demo_beacon_label(idx)}"
                           for idx in effects)
        if known:
            print_fn(f"  {now}  {receiver}: KNOWN demo beacon(s) "
                     f"(demo={labels})")
            _render_burst(mwm, rec, prev, receiver, now, print_fn)
            note = _prompt_note(mwm, rec, last_note, input_fn, print_fn)
            _log(out_fh, stamp, receiver, rec, note)
            return note, True
        print_fn(f"  {now}  {receiver}: {len(frames)} beacon(s) "
                 f"(demo={labels})")
        return last_note, True

    # Ordinary (non-beacon or full mode): render and prompt.
    _render_burst(mwm, rec, prev, receiver, now, print_fn)
    note = _prompt_note(mwm, rec, last_note, input_fn, print_fn)
    _log(out_fh, stamp, receiver, rec, note)
    return note, True


def _render_burst(mwm, rec, prev, receiver, now, print_fn) -> None:
    """Print the decode lines for a burst."""
    header = f"  {now}  {receiver}:"
    if rec.get("novel"):
        header += "  NOVEL decode"
    print_fn(header)
    for ln in _render(rec):
        print_fn(ln)
    if prev is not None and command_key(prev) == command_key(rec):
        delta = _phase_delta(prev, rec)
        if delta:
            print_fn(f"        (same command, phase moved: {delta})")


def _phase_delta(prev: dict, rec: dict) -> str:
    """Timing-only delta between two same-command bursts.

    Reads the timing cells per frame (a frames record keeps them per frame,
    a bundle carries them at the top level) and reports only the fields that
    changed -- the bytes that index this burst's position in the cycle.
    """
    if rec["kind"] == "bundle":
        return _timing_delta(prev, rec)
    changed = []
    pf, rf = prev.get("frames", []), rec.get("frames", [])
    for i in range(max(len(pf), len(rf))):
        a = pf[i].get("timing", []) if i < len(pf) else []
        b = rf[i].get("timing", []) if i < len(rf) else []
        d = _timing_delta(a, b)
        if d and d != "unchanged":
            changed.append(d)
    return ", ".join(changed) if changed else ""


def _prompt_note(mwm, rec, last_note, input_fn, print_fn) -> str:
    """Ask the observer what the command actually did; returns the note."""
    print_fn('    what did that command do? [enter]=no note, '
             's=same note, q=quit')
    try:
        reply = input_fn("    > ").strip()
    except (EOFError, KeyboardInterrupt):
        raise _Quit()
    if reply.lower() == "q":
        raise _Quit()
    return last_note if reply.lower() == "s" else reply


def _log(out_fh, stamp, receiver, rec, note) -> None:
    """Append one annotation row to the JSONL log."""
    out_fh.write(json.dumps({
        "t_unix": round(stamp.timestamp(), 3),
        "t_iso": stamp.isoformat(),
        "receiver": receiver,
        "kind": rec["kind"],
        "summary": rec["summary"],
        "hex": (rec["hex"] if rec["kind"] == "bundle"
                else [f["hex"] for f in rec["frames"]]),
        "triplet": (rec.get("triplet", [])
                    if rec["kind"] == "bundle" else []),
        "params": rec.get("params", []),
        "timing": (rec.get("timing", [])
                   if rec["kind"] == "bundle"
                   else [f.get("timing", []) for f in rec["frames"]]),
        "colors": (rec.get("colors", [])
                   if rec["kind"] == "bundle"
                   else [f.get("colors", []) for f in rec["frames"]]),
        "novel": rec["novel"],
        "note": note,
    }) + "\n")
    out_fh.flush()


def _prev_for(frames: list[bytes]) -> dict | None:
    """Record to remember as the last real burst (None for non-IR lines)."""
    return burst_record(mwm, frames) if frames else None


def _handle_line(mwm, line, receiver, out_fh, include_beacons,
                 last_note, prev, seen, input_fn=_tty_input,
                 print_fn=_say):
    """Decode and annotate one RESULT line; returns ``(last_note, prev)``."""
    frames = extract_frames(mwm, line)
    if not frames:
        return last_note, prev
    last_note, handled = annotate_burst(
        mwm, frames, receiver, out_fh, last_note, include_beacons,
        prev=prev, input_fn=input_fn, print_fn=print_fn, seen=seen)
    if handled:
        prev = _prev_for(frames) or prev
    return last_note, prev


def _drain_newest(lines_q) -> tuple[str, str] | None:
    """Drop every queued burst but the newest; return it (or None).

    The observer only ever reports on the most recently heard command, so
    whatever piled up in the queue while a note was being typed is
    superseded -- the next prompt is for the newest burst alone.
    """
    newest = None
    while True:
        try:
            newest = lines_q.get_nowait()
        except queue.Empty:
            return newest


def run_annotations(mwm, lines, receiver: str, out_fh, include_beacons,
                    input_fn=_tty_input, print_fn=_say):
    """Drive the annotation loop over an iterable of raw RESULT lines.

    Each line is decoded with our own RawData decoder and annotated as one
    burst -- a complete A-B-A' set inside a single line prompts once as a
    bundle (see ``burst_record``).  ``input_fn``/``print_fn`` inject the
    note prompt and output (defaults talk to the tty).
    """
    last_note: str | None = None
    prev: dict | None = None
    seen: dict = {}
    try:
        for line in lines:
            last_note, prev = _handle_line(
                mwm, line, receiver, out_fh, include_beacons,
                last_note, prev, seen, input_fn=input_fn,
                print_fn=print_fn)
    except _Quit:
        pass


# ---------------------------------------------------------------------------
# Live MQTT subscription (mosquitto_sub -C 1 loop, pushed to a queue)
# ---------------------------------------------------------------------------

def _stream(mqtt: dict, receiver_id: str, lines_q: "queue.Queue[str | None]") -> None:
    """Subscribe to one receiver indefinitely, pushing RESULT lines.

    mosquitto_sub -C 1 returns after one message, so loop to stay
    subscribed; a dropped connection simply retries after a second.
    """
    import subprocess

    topic = f"tele/tasmota/{receiver_id}/RESULT"
    cmd = ["mosquitto_sub", "-h", mqtt["broker"], "-p", str(mqtt["port"]),
           "-u", mqtt["username"], "-P", mqtt["password"],
           "-t", topic, "-C", "1"]
    while True:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=600)
            for line in result.stdout.splitlines():
                lines_q.put((receiver_id, line))
        except (TimeoutError, subprocess.TimeoutExpired, OSError):
            time.sleep(1)


def _annotate_live(mwm, mqtt: dict, rcvs: list[str], out_fh,
                   include_beacons: bool) -> None:
    lines_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
    for rcv in rcvs:
        threading.Thread(target=_stream, args=(mqtt, rcv, lines_q),
                         daemon=True).start()
    last_note: str | None = None
    prev: dict | None = None
    seen: dict = {}
    try:
        while True:
            # The observer watched the show, so their report is always about
            # the MOST RECENTLY heard burst.  Whatever piled up while the
            # previous note was being typed is superseded -- collapse the
            # queue to its newest entry (or block for the next arrival).
            newest = _drain_newest(lines_q)
            if newest is not None:
                receiver, line = newest
            else:
                receiver, line = lines_q.get()
            last_note, prev = _handle_line(
                mwm, line, receiver, out_fh, include_beacons,
                last_note, prev, seen)
    except _Quit:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture MWM radio traffic, decode it, and ask the "
                    "observer what each command actually did")
    parser.add_argument("--out", default=None,
                        help="annotation JSONL log (default "
                             "annotations_<ts>.jsonl)")
    parser.add_argument("--mqtt-json", default=str(_DEFAULT_MQTT))
    parser.add_argument("--rcvs", default="600605,179E4E",
                        help="comma-separated receiver IDs (live mode)")
    parser.add_argument("--all", action="store_true",
                        help="prompt on beacons too (default: condensed "
                             "status line only)")
    parser.add_argument("source", nargs="?",
                        help="RESULT JSONL file to annotate offline, or "
                             "'-' for stdin (omit for live listen)")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else Path(
        f"annotations_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
    with open(out_path, "a") as out_fh:
        print(f"Annotation log: {out_path}", flush=True)
        if args.source:
            if args.source == "-":
                run_annotations(mwm, sys.stdin, "stdin",
                                out_fh, args.all)
            else:
                src = Path(args.source)
                with open(src) as fh:
                    run_annotations(mwm, fh, f"replay:{src.stem}",
                                    out_fh, args.all)
        else:
            mqtt = load_mqtt(Path(args.mqtt_json))
            rcvs = [r.strip() for r in args.rcvs.split(",") if r.strip()]
            print(f"Listening on {len(rcvs)} receiver(s): {args.rcvs}. "
                  "q to quit", flush=True)
            _annotate_live(mwm, mqtt, rcvs, out_fh, args.all)


if __name__ == "__main__":
    main()