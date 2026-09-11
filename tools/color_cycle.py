#!/usr/bin/env python3
"""Offline color/effect cycling test for MWM ear peripherals.

PURPOSE
-------
A human-driven rig research tool.  It walks the FULL verified command
catalog -- every simple color, palette shade, composite/fused frame, and
built-in effect -- one command at a time, transmitting each via MQTT
(Tasmota IRsend) and prompting you for a free-form observation after each
one.  The point is to sit in front of the physical ears and confirm, for
each command, exactly what they do, recording your notes to a JSON log for
later analysis.

This is the "confirm the catalog on real hardware" tool; tools/
analyze_log.py turns the resulting JSON log into a decode report.

Requires: mosquitto_pub on PATH, MQTT config at
~/.config/ir-remote-tools/mqtt.json, the _mwm library (shared bootstrap).

Usage::

    python3 tools/color_cycle.py [--log FILE] [--mqtt-json PATH]
        [--repeat N] [--repeat-delay SECS]
    python3 tools/color_cycle.py --effects [--effect-color 0x67|pal:0x0C]
        [--repeat N] [--repeat-delay SECS]
    python3 tools/color_cycle.py --incant [--incant-random N]
        [--repeat N] [--repeat-delay SECS]

``--effects`` runs a different, per-effect **issuance-method probe**: for each
built-in effect it re-seeds the desired color, then walks a battery of ways
to invoke the effect (effect-then-color, effect-with-`58`, color-then-effect,
the old `24`-reset form, and bare effect) and asks the human which methods
actually run that effect on the desired color.  Only the pulse family and
the effect-then-color order have been verified on real ears so far; this
probe turns each effect's behavior from assumption into a logged verdict.

``--incant`` runs a **decision-tree rig test** of the show incantations
(pulse fresh + reset re-color, strobe, fade, rotation, stop) across simple
and palette color combinations -- including the interchange probes where a
simple rides the palette slot and vice versa.  Colors are shown to the
observer by name ("pure blue", "sky blue", ...), never as ``pal:0xNN``.
For each family the tool prints what to watch for, then announces each step
with what should change and waits for Enter before issuing it, anchors to a
known dark state, runs the known park-shaped frame, and asks "did it work?"
(y / n / r / note).  A non-``y`` result steps through the probe's variants
(reissue, or the reset-armed corpus alternative like ``F? 24 … 48 85``)
until one works, then resets and moves on.  It never prompts twice for the
same command.  This is the automation behind docs/human-testing.md A6.
``--incant-random N`` adds N random color combos per family on top of the
fixed battery.
"""

from __future__ import annotations

import importlib.util
import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402
from _mqtt import load_mqtt, send_payload  # noqa: E402

PALETTE = mwm.PALETTE
SIMPLE_COLORS = mwm.SIMPLE_COLORS
build_frame = mwm.build_frame
irsend_payload = mwm.irsend_payload
parse_frame_hex = mwm.parse_frame_hex
EFFECT_LABELS = mwm.EFFECT_LABELS
effect_label = mwm.effect_label

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"


def _send_frame(mqtt: dict, frame: bytes, *, repeat: int = 1,
                delay: float = 0.0) -> str:
    payload = irsend_payload(frame)
    send_payload(mqtt, payload, repeat=repeat, delay=delay)
    return payload


def _send_hex(mqtt: dict, hex_str: str, *, repeat: int = 1,
              delay: float = 0.0) -> str:
    """Send one or more '+'-joined hex frames; return combined payload."""
    parts: list[str] = []
    for _ in range(repeat):
        for frame in parse_frame_hex(hex_str):
            parts.append(irsend_payload(frame))
            send_payload(mqtt, irsend_payload(frame))
        if delay > 0:
            time.sleep(delay)
    return " + ".join(parts)


# ---------------------------------------------------------------------------
# Cycle sequence definition
# ---------------------------------------------------------------------------

# Reset frame: bare 0x24 (both ears off).  Doc section 4 escape price.
_RESET = build_frame([0x24])

# Simple colors: both ears (0x61..0x67)
_SIMPLE_BOTH = [
    (name, code) for code, (name, _) in sorted(SIMPLE_COLORS.items())
]

# Simple colors: left ear only (0x69..0x6F, offset from both-ears by 8)
_SIMPLE_LEFT = [
    (name, 0x68 + (code - 0x60)) for code, (name, _) in sorted(SIMPLE_COLORS.items())
]

# Palette shades: both ears (0x00..0x1C, via 0x0E XX)
_PALETTE_BOTH = [
    (name, idx) for idx, (name, _) in sorted(PALETTE.items()) if idx <= 0x1C
]

# Palette shades: left ear only (0x80..0x9C, via 0x0E XX|80)
_PALETTE_LEFT = [
    (name, idx | 0x80) for idx, (name, _) in sorted(PALETTE.items()) if idx <= 0x1C
]

# Composite / fused commands from the verified TSV.  The two-frame forms
# (both-ears then left-only restore) are verified.  A SINGLE-frame fused
# palette composition (`0E <right> 0E <left|80>`) is structurally valid and
# uses the same two-opcode mechanism as the verified fused simple frame
# (`91 61 6A ..`), but is NOT yet confirmed on real ears -- the only prior
# attempt used a malformed length nibble (`94` in a 6-byte frame) and was
# rejected by the length rule, so its "ignored by real ears" result is
# inconclusive. See docs/human-testing.md A3a.  The malformed frame is kept
# below as an explicit NEGATIVE control (real ears reject it).
_COMPOSITE = [
    ("right-magenta-left-yellow", "90 65 99 + 90 6E B9",
     "set both magenta, then override left yellow"),
    ("right-blue-left-green-fused", "91 61 6A 06",
     "single fused frame: right blue, left green (verified)"),
    ("right-white-left-orange", "91 0E 00 5F + 91 0E 94 2F",
     "set both white, then override left orange (two frames)"),
    ("right-white-left-orange-fused", "93 0E 00 0E 94 40",
     "fused palette frame, ONE burst: right pale cyan-white, "
     "left orange-red per-ear register (UNVERIFIED -- rig test A3a)"),
    ("right-green-left-red-fused-mixed", "92 0E 19 6C 8A",
     "fused mixed frame, ONE burst: right palette pure green, "
     "left simple red (UNVERIFIED -- rig test A3a)"),
    ("right-white-left-orange-fused-MALFORMED", "94 0E 00 0E 94 11",
     "NEGATIVE CONTROL: wrong length nibble (L=4 in 6-byte frame); "
     "real ears reject it (see doc caveat)"),
]

# Built-in effects (0x48 XX)
_EFFECTS = [
    (effect_label(idx), idx) for idx in sorted(EFFECT_LABELS.keys())
]


def _build_cycle() -> list[dict]:
    seq: list[dict] = []

    def add(kind: str, name: str, hex_data: str, desc: str = "") -> None:
        seq.append({"kind": kind, "name": name, "hex": hex_data, "desc": desc})

    def reset() -> None:
        add("reset", "both off", "24")

    for name, code in _SIMPLE_BOTH:
        add("simple_both", name, f"{code:02X}", f"simple {name}, both ears")
    reset()
    for name, code in _SIMPLE_LEFT:
        add("simple_left", f"{name} (left)", f"{code:02X}",
            f"simple {name}, left ear only")
    reset()
    for name, idx in _PALETTE_BOTH:
        add("palette_both", name, f"0E {idx:02X}",
            f"palette 0x{idx:02X} {name}, both ears")
    reset()
    for name, idx in _PALETTE_LEFT:
        add("palette_left", f"{name} (left)", f"0E {idx:02X}",
            f"palette 0x{idx & 0x7F:02X} {name}, left ear only")
    reset()
    for name, hex_data, desc in _COMPOSITE:
        add("composite", name, hex_data, desc)
    reset()
    for name, idx in _EFFECTS:
        add("effect", name, f"48 {idx:02X}", f"effect {name} (0x{idx:02X})")
    reset()
    return seq


# ---------------------------------------------------------------------------
# Effect issuance-method probe (--effects)
# ---------------------------------------------------------------------------
#
# Which way of issuing an effect actually runs it "with our desired color" is
# NOT settled: the rig verified effect-then-color for the pulse family, but
# each effect program behaves differently (some run their own palette, some
# need a `58`/`D0` companion, fade-out needs a pre-existing color).  Rather
# than assume, this probe walks each effect across a battery of issuance
# methods and asks the human which ones work -- turning single-scenario
# observations into a per-effect verdict table instead of a blanket claim.
#
# Each candidate is a list of (build_frame content) sendable as one group.
# A method is recorded as "the effect runs with the desired color" only if
# the human confirms it visually; the log keeps per-method notes so we can
# distinguish "blanked", "split lobes", "own palette", "adopted color".

# The pulse/slow-pulse family needs a `58` cycle-timer companion in the same
# phrase (docs/mwm-show-protocol.md section 4: "require `58 F0`").
_PULSE_COMPANION = 0xF0


def _effect_candidates(effect_idx: int, color_content: tuple[int, ...]) -> list[dict]:
    """Effect-issuance candidates for one effect on a seed color.

    ``color_content`` is the build_frame content for the desired color
    (e.g. ``(0x67,)`` both-white, ``(0x0E, idx)`` a palette shade).
    Returns a list of dicts describing one sendable group each.
    """
    header = [0x48, effect_idx]
    companion = [0x58, _PULSE_COMPANION] if effect_idx in (0x03, 0x04) else []

    reset = [0x24]  # doc: escape/flow-control reset (blanks when standing alone)
    color = list(color_content)

    return [
        {
            "id": "A_effect_then_color",
            "desc": "effect, then seed color (current apply_effect order)",
            "group": [header, color],
        },
        {
            "id": "B_effect_comp_then_color",
            "desc": "effect + 58 companion, then seed color (pulse-style)",
            "group": [header + companion, color],
        },
        {
            "id": "C_color_then_effect",
            "desc": "seed color, then effect (OLD apply_state order)",
            "group": [color, header],
        },
        {
            "id": "D_24_effect",
            "desc": "24 reset then bare effect (old 24-prefix form)",
            "group": [reset + header],
        },
        {
            "id": "E_24_effect_color",
            "desc": "24 reset, effect, then seed color",
            "group": [reset + header, color],
        },
        {
            "id": "F_effect_alone",
            "desc": "effect alone on whatever color is showing",
            "group": [header + companion] if companion else [header],
        },
    ]


def run_effect_probe(args: argparse.Namespace) -> None:
    """Interactively test effect-issuance methods per effect.

    Sends a seed color, then for each effect walks the candidate battery,
    prompting for which methods (if any) ran the effect on the desired color.
    Writes a per-method JSON log for later analysis.
    """
    mqtt = load_mqtt(Path(args.mqtt_json))
    repeat = args.repeat
    delay = args.repeat_delay

    # Desired seed color: bare color/simple code or palette shade.
    color_spec = args.effect_color
    if color_spec.lower().startswith("pal:"):
        idx = int(color_spec.split(":", 1)[1], 0)
        color_content = (0x0E, idx)
        color_name = f"palette 0x{idx:02X}"
    else:
        code = int(color_spec, 0)
        color_content = (code,)
        color_name = f"simple 0x{code:02X}"

    # Which effects to probe: all HA-facing ones by default.
    effect_idx = [idx for _, idx in _EFFECTS]

    print("MWM effect issuance-method probe")
    print(f"MQTT transmit: {mqtt['transmit']}")
    print(f"Desired color: {color_name}  (re-seeded before each effect)")
    print(f"Effects to probe: {len(effect_idx)}")
    print("For each effect, each candidate is sent and you record what you "
          "see.  Verdict per method:\n"
          "  ok   = effect runs ON the desired color (adopts it)\n"
          "  own  = effect runs but uses its OWN palette\n"
          "  split= lobes split / one blanked\n"
          "  dead = nothing happened\n"
          "  blank= ears went dark\n"
          "Unrecognised input is kept verbatim as a free-text note.\n")

    seed_hex = build_frame(list(color_content)).hex().upper()
    entries: list[dict] = []

    try:
        for idx in effect_idx:
            label = effect_label(idx)
            print(f"\n=== effect 0x{idx:02X} ({label}) ===\n")
            # Re-seed the desired color so each effect starts from it.
            _send_frame(mqtt, build_frame(list(color_content)),
                        repeat=repeat, delay=delay)
            print(f"  seed {color_name}: {seed_hex}  (x{repeat})")

            for cand in _effect_candidates(idx, color_content):
                # Compose the group: send the frames back-to-back, whole group
                # repeated `repeat`x so a quick blip is visible to the eye.
                hex_repr = []
                for _ in range(repeat):
                    for content in cand["group"]:
                        fr = build_frame(content)
                        _send_frame(mqtt, fr, repeat=1, delay=0)
                        hex_repr.append(fr.hex().upper())
                print(f"  [{cand['id']}] {cand['desc']}")
                print(f"      { ' + '.join(hex_repr) }  (x{repeat})")
                try:
                    verdict = input("      verdict> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nInterrupted.")
                    return
                entries.append({
                    "effect_idx": idx,
                    "effect": label,
                    "color": color_name,
                    "method_id": cand["id"],
                    "method_desc": cand["desc"],
                    "hex": " + ".join(hex_repr),
                    "verdict": verdict,
                    "t": datetime.now(timezone.utc).isoformat(),
                })
    except KeyboardInterrupt:
        print("\nInterrupted.")

    log_path = Path(args.log) if args.log else Path(
        f"effect_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    log_path.write_text(json.dumps({
        "session": datetime.now(timezone.utc).isoformat(),
        "mqtt_topic": mqtt["transmit"],
        "desired_color": color_name,
        "entries": entries,
    }, indent=2) + "\n")
    print(f"\nLogged {len(entries)} verdicts to {log_path}")


# ---------------------------------------------------------------------------
# Focussed dig (--dig): disambiguate suspect scenarios from a --effects run
# ---------------------------------------------------------------------------
#
# A first probe pass surfaced confounds that a second, state-settling pass
# must resolve:
#
#   * Ear-state bleed-over.  Many A/B candidates sent IDENTICAL hex yet got
#     different verdicts (e.g. random, quick-fade, flashing-on-one-ear,
#     hard/crossfade transitions) -- the ears were still finishing the
#     previous program, so one "run" was misattributed.  Dig settles each
#     ear fully dark, waits, re-seeds the desired color, and waits again so
#     each candidate starts from a known, quiescent display.
#
#   * Slow-even-pulse (0x03) does NOT follow the pulse (0x04) contract.  In
#     the first pass A/B/C all blanked and only the 24-prefixed (E) and bare
#     (F) forms woke it, and a trailing color seemed to kill it.  Dig runs
#     an expanded battery around exactly that question.
#
#   * Side labels are unreliable for a colorblind observer.  Dig anchors
#     physical orientation ONCE with an on/off asymmetry (dark vs lit is
#     color-blind safe) and records whether the tool's "left"/"right"
#     match the user's, so analysis can correct split verdicts.
#
# Each candidate is repeated `repeat`x from a clean settle for a per-trial
# verdict; a method is only tagged CONFIRMED when the same verdict appears
# in the majority of its trials (else PARTIAL / UNSTABLE).

# Map from our side name used by the tool to the human's confirmed side.
_LEFT = "left"
_RIGHT = "right"

# Both-ear off: stand-alone reset (0x24) is the guaranteed kill, then a
# redundant simple black (0x60) so any lingering program is fully cleared.
_OFF_GROUP = [(0x24,), (0x60,)]

# Single-ear illumination anchors, built from VERIFIED frames (see
# docs/mwm-show-protocol.md section 4 "Left vs right ears" and 12.3): the
# `0x08` left-ear bit turns a both-ear one-bit color (0x60-0x67) into a
# left-only opcode (0x68-0x6F).  There is NO right-ear-only opcode, so a
# right-only state is reached by setting both ears then overriding the left
# ear back off (left-only black = 0x68).  BOTH anchors use on/off, never
# hue, so they are readable by a colorblind observer:
#
#   left_ON_anchor : both off (0x60), left lit only (0x69 blue)
#   right_ON_anchor: both white (0x67), left forced off (0x68)
#
# Each is a list of frames sent back-to-back in one burst.
_LEFT_ON_ANCHOR = [(0x60,), (0x69,)]   # both off, then left blue only
_RIGHT_ON_ANCHOR = [(0x67,), (0x68,)]  # both white, then left-ear off-only

# The suspect effects the dig pass starts from.  `3` (slow even pulse) is the
# one real contract break from the first pass; the rest are the programmes
# whose A/B candidates shared identical hex yet diverged (ear-state noise),
# plus the side-split set.  `--dig-effects` overrides this list.
_SUSPECT_EFFECTS = [3, 0, 1, 2, 16, 17, 19, 31, 128, 130, 131, 132, 134]


def _send_group(mqtt: dict, frames: list[tuple[int, ...]],
                repeat: int = 1, delay: float = 0.0) -> str:
    """Send ``frames`` back-to-back (one group) and return the hex summary."""
    parts = []
    for _ in range(repeat):
        for content in frames:
            fr = build_frame(list(content))
            _send_frame(mqtt, fr, repeat=1, delay=0)
            parts.append(fr.hex().upper())
    return " + ".join(parts)


def _settle(mqtt: dict, color_content: tuple[int, ...], color_name: str,
            repeat: int, gap: float) -> None:
    """Drive the ears to a known, quiescent state before a candidate.

    Kills any running program, lets it go dark, re-seeds the desired color,
    then waits again so the eyes recover.  ``gap`` is the settle seconds.
    """
    _send_group(mqtt, _OFF_GROUP, repeat=repeat)
    time.sleep(gap)
    _send_group(mqtt, [tuple(color_content)], repeat=repeat)
    time.sleep(gap)


def _anchor_orientation(mqtt: dict, repeat: int, gap: float) -> dict:
    """Establish, once, which physical side is the tool's "left".

    Lights each ear one at a time along with a plain spoken label ("lighting
    left ear", "lighting right ear") so the observer can orient the ears to
    the tool's naming.  The observer confirms the tool's left matched their
    physical left (or reports a swap); all later split verdicts are read
    through that single mapping.  Returns the confirmed mapping dict.
    """
    print("\n--- Orientation anchor ---")
    print("I'll light each ear one at a time and call out 'left' and 'right'")
    print("so you can orient the ears.  Focus on which PHYSICAL side is lit;")
    print("you don't need to read colors.")
    mapping = {}

    print("\n>> lighting LEFT ear  <<")
    _send_group(mqtt, _LEFT_ON_ANCHOR, repeat=repeat)
    time.sleep(gap)
    mapping["matched"] = input(
        "    did the tool's LEFT light your PHYSICAL left? [yes/no] "
    ).strip().lower() == "yes"

    print(f"\n>> lighting RIGHT ear <<")
    _send_group(mqtt, _RIGHT_ON_ANCHOR, repeat=repeat)
    time.sleep(gap)
    print("    (tool's RIGHT should be your physical right, i.e. the OTHER "
          "ear to the one that just lit as left).")

    print(f"    orientation: tool-left matches physical-left = {mapping['matched']}")
    _send_group(mqtt, _OFF_GROUP, repeat=repeat)  # leave dark
    return mapping


def _dig_candidates(effect_idx: int,
                    color_content: tuple[int, ...]) -> list[dict]:
    """Expanded, state-settling battery for one suspect effect.

    Unlike ``_effect_candidates`` this does NOT re-seed inside a candidate --
    ``run_dig_probe`` settles before each candidate -- and it adds variants
    that isolate the questions the first pass left open:

      * effect-then-color vs effect-alone vs delayed-color (does a trailing
        color really kill slow-even-pulse?),
      * 24-prefixed forms vs plain (is the 24 reset required, or does a bare
        effect suffice once the ears are settled?).
    """
    header = [0x48, effect_idx]
    comp = _PULSE_COMPANION if effect_idx in (0x03, 0x04) else None
    comp_hdr = header + [0x58, comp] if comp is not None else header
    reset = [0x24]
    color = list(color_content)

    return [
        {
            "id": "A_effect_then_color",
            "desc": "effect, then seed color (current apply_effect order)",
            "group": [header, color],
        },
        {
            "id": "B_effect_comp_then_color",
            "desc": "effect + 58 companion, then seed color",
            "group": [comp_hdr, color],
        },
        {
            "id": "E_24_effect_comp_color",
            "desc": "24 reset + effect + 58 companion, then seed color",
            "group": [reset + comp_hdr, color],
        },
        {
            "id": "F_effect_comp_alone",
            "desc": "effect + 58 companion alone (no trailing color)",
            "group": [comp_hdr],
        },
        {
            "id": "F_color_before_alone",
            "desc": "seed color first, then effect + 58 companion alone",
            "group": [color, comp_hdr],
        },
        {
            "id": "G_delayed_color",
            "desc": "effect + 58 companion, then after a settle re-issue color",
            "group": [comp_hdr],
            "trailing_color": True,
        },
        {
            "id": "H_24_effect_comp_alone",
            "desc": "24 reset + effect + 58 companion, NO trailing color",
            "group": [reset + comp_hdr],
        },
    ]


def run_dig_probe(args: argparse.Namespace) -> None:
    """State-settling, orientation-corrected repeat probe for suspect effects.

    Anchors left/right once, then for each suspect effect runs the expanded
    battery from a fully-settled dark->seed state, repeating each candidate
    `repeat`x and asking for a per-trial verdict.  A method is CONFIRMED when
    the same verdict appears in the majority of its trials, else PARTIAL /
    UNSTABLE.  Writes a JSON log + a human-readable verdict summary.
    """
    mqtt = load_mqtt(Path(args.mqtt_json))
    repeat = args.repeat
    gap = args.settle_secs

    if args.effect_color.lower().startswith("pal:"):
        idx = int(args.effect_color.split(":", 1)[1], 0)
        color_content = (0x0E, idx)
        color_name = f"palette 0x{idx:02X}"
    else:
        code = int(args.effect_color, 0)
        color_content = (code,)
        color_name = f"simple 0x{code:02X}"

    # Suspect effects: explicit list, else the defaults above.
    if args.dig_effects:
        effect_idx = [int(x, 0) for x in args.dig_effects]
    else:
        effect_idx = list(_SUSPECT_EFFECTS)

    print("MWM focussed dig probe (state-settling)")
    print(f"MQTT transmit: {mqtt['transmit']}")
    print(f"Desired color: {color_name} (re-seeded from a clean dark settle)")
    print(f"Suspect effects: {[f'0x{i:02X}' for i in effect_idx]}")
    print(f"Trials per candidate: {repeat}, settle gap: {gap}s")
    print("Verdicts: ok / own / split / dead / blank  (unrecognised = note)\n")

    try:
        mapping = _anchor_orientation(mqtt, repeat, gap)
    except (EOFError, KeyboardInterrupt):
        print("\nInterrupted.")
        return

    entries: list[dict] = []
    for idx in effect_idx:
        label = effect_label(idx)
        print(f"\n=== effect 0x{idx:02X} ({label}) ===")
        for cand in _dig_candidates(idx, color_content):
            # Repeated trials, each from a clean settle, to tag stability.
            trial_verdicts: list[str] = []
            for trial in range(1, repeat + 1):
                _settle(mqtt, color_content, color_name, repeat, gap)
                hex_sum = _send_group(mqtt, cand["group"], repeat=1, delay=0)
                if cand.get("trailing_color"):
                    # delayed-color candidate: re-issue the seed after a beat
                    time.sleep(gap)
                    _send_group(mqtt, [tuple(color_content)], repeat=1, delay=0)
                    hex_sum += f" + trai~{color_name}"
                print(f"\n  [{cand['id']}] trial {trial}/{repeat}  {cand['desc']}")
                print(f"      {hex_sum}")
                try:
                    verdict = input("      verdict> ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\nInterrupted.")
                    raise
                trial_verdicts.append(verdict or "note")

            # Tag stability: two agree on a verdict -> confirmed.
            counts = Counter(trial_verdicts)
            (top, n), *rest = counts.most_common(2)
            status = (
                f"CONFIRMED({top})" if n >= 2
                else "UNSTABLE" if rest and rest[0][1] >= 2
                else "PARTIAL"
            )
            print(f"      -> trials {trial_verdicts}  [{status}]")
            entries.append({
                "effect_idx": idx,
                "effect": label,
                "color": color_name,
                "method_id": cand["id"],
                "method_desc": cand["desc"],
                "hex": hex_sum,
                "trials": trial_verdicts,
                "status": status,
                "t": datetime.now(timezone.utc).isoformat(),
            })

    log_path = Path(args.log) if args.log else Path(
        f"dig_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    log_path.write_text(json.dumps({
        "session": datetime.now(timezone.utc).isoformat(),
        "mqtt_topic": mqtt["transmit"],
        "desired_color": color_name,
        "orientation": mapping,
        "trials_per_candidate": repeat,
        "settle_secs": gap,
        "entries": entries,
    }, indent=2) + "\n")
    print(f"\nLogged {len(entries)} candidate verdicts to {log_path}")


def _load_mwm_send():
    """Import mwm-send (dash-named, not importable) to reuse its incantation
    composition -- _incant_pulse stays the single source of truth for the
    fused pulse phrase including the simple/palette interchange probes."""
    spec = importlib.util.spec_from_file_location(
        "mwm_send", Path(__file__).resolve().parent / "mwm-send.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mwm_send"] = mod
    spec.loader.exec_module(mod)
    return mod


# Colors for the incantation sweep: simple names plus palette indices that
# cover the twins (pure blue 0x04, lime 0x1A, white 0x1C) and some off-code
# shades, so the interchange probes compare like-for-like.
_INCANT_PALETTES = [0x00, 0x04, 0x08, 0x0C, 0x19, 0x1C]
_INCANT_SIMPLES = ["blue", "green", "cyan", "red", "magenta", "yellow", "white"]


def _friendly_color(spec: str) -> str:
    """Color spec -> observer-facing name.

    ``pal:0xNN`` and ``pal:0xN`` mean nothing to a human, so palette
    indices render as their catalog names ("pure blue", "sky blue", ...)
    from docs/mwm-show-protocol.md table 13.  Simple names pass through.
    """
    if spec.startswith("pal:"):
        idx = int(spec[4:], 0)
        name, _ = PALETTE.get(idx, ("pal:0x%02X" % idx, (0, 0, 0)))
        return name
    return spec

# Reset ping (bare `24`, same as color-cycle's `_RESET`): pulls the ears back
# to a known off state before each probe step so one family's leftovers do
# not poison the next.  The corpus's verified standalone blacken is the
# `91 F1 24` form below; the bare `24` is our probe-internal anchor.
_ANCHOR_RESET = build_frame([0x24])
# The corpus's alternative "stop" -- the reset ping `91 F1 24` (201x in
# captures) that blacks both ears immediately (line 639 / table 13.x).
_RESET_PING = build_frame([0xF1, 0x24])

# Verdict codes understood by the probe driver.  Long-form text is slurped
# and matched case-insensitively so a free-typed note still works.
_VERDICT_WORKED = ("ok", "o", "yes", "y", "works", "w")
_VERDICT_RETRY = ("r", "retry", "re", "reissue", "again")
_VERDICT_BROKE = ("n", "no", "b", "bad", "fail", "failed", "f")
_VERDICT_IGNORED = ("i", "ignore", "ignored", "dead", "d", "blank", "bl", "none")
_VERDICT_MISCOLOR = ("m", "misc", "mis-color", "mis-colour", "mismatch", "own", "split")


def build_fade_armed(simple: int | None = None, palette: int | None = None,
                     cycle: int = 0x05) -> bytes:
    """Reset-bearing colored-fade phrase: ``F? 24 <color> F? 48 85 ``58 tt``.

    Doc observation (not an authoritative spec): the park's raw fade-out
    countdown is ``F? 48 85 58 tt`` (no reset), but the *colored* fades that
    follow other programs appear to lead with ``24`` (doc line: a phrase
    containing `24` may be required before new colors respond after a fade).
    This blob arms a fade with the leading reset as a retry to test whether
    the no-reset `build_fade()` needs one.  Shaped after the observed
    ``97 F1 24 67 F3 48 85 58 03`` (simple color in the slot); pass
    ``palette=pp`` for the ``0E pp`` palette form.  Exactly one of ``simple``
    / ``palette`` must be set.
    """
    assert (simple is None) != (palette is None)
    color: list[int] = [0x0E, palette] if palette is not None else [simple]
    return build_frame([0xF1, 0x24, *color, 0xF2, 0x48, 0x85, 0x58, cycle])


def build_incant_probes(mwm_send, incant_random: int = 0) -> list[dict]:
    """The incantation decision-tree probes, one entry per family.

    Each probe is:
      ``family``      the incantation family being exercised.
      ``name``        human label.
      ``watch``       what the human should watch for (printed BEFORE the
                      first prompt, so they know what a good result looks
                      like).
      ``steps``       ordered list of ``{"frames": [...], "pause_ms": N,
                      "label": "...", "expect": "..."}`` -- anchor reset
                      frames + the probe frames.  The driver announces each
                      step's ``label`` and ``expect`` (what should CHANGE
                      when it is issued) and waits for Enter before sending.
                      A step's ``checkpoint`` (True) means the human must
                      confirm/verdict at that point.
      ``retries``     list of extra framesets to step through (in order)
                      when the primary verdict is not clean (e.g. the
                      reset-armed alternative), each a ``{"frames": [...],
                      "label": "...", "expect": "..."}``.

    Offline tests assert every step/retry frame parses as a valid MWM frame
    and every probe exposes exactly one checkpoint so the human is asked
    once per logical command.
    """
    import random

    rng = random.Random() if incant_random else None
    probes: list[dict] = []

    # --- pulse ----------------------------------------------------------
    # Combos deliberately pair colors the observer can tell apart (rose
    # pink vs white, yellow vs pure blue, rose pink vs pure blue).  Sky
    # blue vs blue / pure blue vs blue are indecipherable to this observer
    # and are NOT used for differentiating rows.
    pulse_combos: list[tuple[str, str]] = [
        ("pal:0x0D", "white"),    # corpus shape: palette left, simple right
        ("pal:0x08", "white"),
        ("yellow", "pal:0x04"),   # interchange: simple left, palette right
        ("pal:0x0D", "pal:0x04"), # interchange: both palette
        ("pal:0x04", "pal:0x04"), # twins: both left
    ]
    if rng:
        for _ in range(incant_random):
            pulse_combos.append((
                f"pal:0x{rng.choice(_INCANT_PALETTES):02X}",
                rng.choice(_INCANT_SIMPLES)))
    for left, right in pulse_combos:
        fresh = mwm_send._incant_pulse(left, right, {})
        recolor = mwm_send._incant_pulse(
            "pal:0x12", "blue", {"reset": True})
        probes.append({
            "family": "pulse",
            "name": (f"pulse {_friendly_color(left)} / "
                     f"{_friendly_color(right)} -> re-color"),
            "watch": ("The two ears pulse in the combo but ALTERNATE (they "
                      "do not pulse in unison -- that is how the pulse "
                      "effect runs). Then the re-color step flips the "
                      "running pulse to PURE YELLOW / BLUE (bold, non-pink "
                      "contrast -- the headband ears are pink-tinted, so "
                      "rose-pink is hard to pick out). Look for: the two "
                      "ears alternating pulses; the re-color visibly "
                      "changing the color AND restarting the running "
                      "pulse's cycle (not ignored, not blanking)."),
            "steps": [
                {"frames": [_ANCHOR_RESET], "pause_ms": 300,
                 "label": "anchor reset",
                 "expect": "ears snap to off; nothing running yet"},
                {"frames": fresh, "pause_ms": 1200,
                 "label": (f"fresh pulse {_friendly_color(left)} / "
                           f"{_friendly_color(right)}"),
                 "checkpoint": True,
                 "expect": "both ears start pulsing, ALTERNATING (never in "
                           "unison), in this color combo"},
                {"frames": recolor, "pause_ms": 0,
                 "label": "re-color reset",
                 "checkpoint": True,
                 "expect": "the RUNNING pulse flips to PURE YELLOW / BLUE -- "
                           "the colors change and the pulse cycle RESTARTS "
                           "with the new colors (fresh timing, not kept "
                           "in phase)"},
            ],
            "retries": [
                {"frames": [_RESET_PING],
                 "label": "reset-ping then re-color",
                 "expect": "black-out ping, then the re-color to PURE "
                           "YELLOW / BLUE"},
                {"frames": [mwm_send.build_pulse(0x12, 0x61, reset=True)],
                 "label": "re-color pure-yellow/blue (explicit)",
                 "expect": "running pulse flips to PURE YELLOW / BLUE"},
            ],
        })

    # --- pulse through the dark (soft color transition) ----------------
    # The reset=true re-color restarts the running pulse with the new
    # colors -- abrupt.  This probes a soft swap instead: fade the
    # running pulse out to dark, then re-light it in the target combo.
    # The key question is whether a plain fresh pulse re-lights from the
    # faded-dark state or needs the reset/arm phrase first (the flares
    # earlier suggested dark ears ignored new programs until a reset; the
    # re-lit pulse either proves or disproves that for a pulse's own
    # fade, which decides how "soft" transitions actually have to be
    # built).
    probes.append({
        "family": "pulse",
        "name": "pulse fade-to-dark then re-light pure-yellow/blue",
        "watch": ("The abrupt re-color restarts the pulse; this probe "
                  "tries a SOFTER swap: the running pulse FADES OUT to dark, "
                  "then a fresh pulse re-lights in PURE YELLOW / BLUE. "
                  "Look for: the pulse feathering to dark; then whether the "
                  "fresh pulse lights on its own from dark, or stays dark "
                  "until an arm."),
        "steps": [
            {"frames": [_ANCHOR_RESET], "pause_ms": 300,
             "label": "anchor reset",
             "expect": "ears snap to off; nothing running yet"},
            {"frames": mwm_send._incant_pulse("pal:0x0D", "white", {}),
             "pause_ms": 0,
             "label": "fresh pulse rose pink / white",
             "checkpoint": True,
             "expect": "both ears start pulsing ALTERNATELY in rose pink / "
                       "white"},
            {"frames": [mwm_send.build_fade()],
             "pause_ms": 0,
             "label": "fade to dark",
             "checkpoint": True,
             "expect": "the running pulse fades out to dark over ~1 s "
                       "(softer than the re-color, no abrupt cut)"},
            {"frames": mwm_send._incant_pulse("pal:0x12", "blue", {}),
             "pause_ms": 0,
             "label": "fresh pulse pure-yellow/blue",
             "checkpoint": True,
             "expect": "after the fade-to-dark, does this fresh pulse "
                       "re-light ON ITS OWN in PURE YELLOW / BLUE -- or "
                       "stay dark until something arms it again?"},
        ],
        "retries": [
            {"frames": [_RESET_PING, *mwm_send._incant_pulse("pal:0x12", "blue", {})],
             "label": "reset-ping then fresh pulse",
             "expect": "black-out ping, then the fresh pulse lights in "
                       "PURE YELLOW / BLUE (the armed re-light)"},
        ],
    })

    # --- pulse re-color cadence ladder ----------------------------------
    # The 2026-09-06 gang probe answered that ``D0 42 tt`` is a continuous
    # post-swap CADENCE knob, not a fixed hold byte: the corpus 0x06
    # re-colored "y but actually pulsing faster", the 0x20 fuzz "y much
    # slower".  This ladder holds the colors FIXED (pure-yellow/blue) and
    # walks tt one field at a time -- 0x06 -> 0x10 -> 0x20 -> 0x40 -- so
    # the ONLY variable is the cadence, characterizing rate-vs-value
    # (linear, or does it floor out toward very slow?).
    probes.append({
        "family": "pulse",
        "name": "pulse re-color cadence ladder (D0 42 tt)",
        "watch": ("Reset re-color of the running pulse; colors STAY "
                  "pure-yellow/blue, only the D0 42 cycle arg moves (the "
                  "gang probe: 0x06 = \"pulsing faster\" than fresh, 0x20 "
                  "= \"much slower\"). The ladder walks 0x06 -> 0x10 -> "
                  "0x20 -> 0x40. Look for: at each step the pulse keeps "
                  "the same colors and the RATE steps from fast to very "
                  "slow -- or plateaus."),
        "steps": [
            {"frames": [_ANCHOR_RESET], "pause_ms": 300,
             "label": "anchor reset",
             "expect": "ears snap to off; nothing running yet"},
            {"frames": mwm_send._incant_pulse("pal:0x0D", "white", {}),
             "pause_ms": 1200,
             "label": "fresh pulse rose pink / white",
             "checkpoint": True,
             "expect": "both ears start pulsing ALTERNATELY in rose pink / "
                       "white"},
            {"frames": mwm_send._incant_pulse(
                "pal:0x12", "blue", {"reset": True, "cycle_mod": 0x06}),
             "pause_ms": 0,
             "label": "re-color cadence 0x06 (corpus)",
             "checkpoint": True,
             "expect": "the RUNNING pulse flips to PURE YELLOW / BLUE and "
                       "pulses FASTER than the fresh rose-pink pulse did"},
            {"frames": mwm_send._incant_pulse(
                "pal:0x12", "blue", {"reset": True, "cycle_mod": 0x10}),
             "pause_ms": 0,
             "label": "re-color cadence 0x10",
             "checkpoint": True,
             "expect": "colors unchanged; is the pulse RATE slower than the "
                       "0x06 step?"},
            {"frames": mwm_send._incant_pulse(
                "pal:0x12", "blue", {"reset": True, "cycle_mod": 0x20}),
             "pause_ms": 0,
             "label": "re-color cadence 0x20",
             "checkpoint": True,
             "expect": "colors unchanged; 0x20 was \"much slower\" on the "
                       "rig -- is it slower than the 0x10 step?"},
            {"frames": mwm_send._incant_pulse(
                "pal:0x12", "blue", {"reset": True, "cycle_mod": 0x40}),
             "pause_ms": 0,
             "label": "re-color cadence 0x40",
             "checkpoint": True,
             "expect": "colors unchanged; very slow end of the ladder -- "
                       "does the pulse nearly stop, or does the rate floor "
                       "out around 0x20?"},
        ],
        "retries": [
            {"frames": [_RESET_PING, *mwm_send._incant_pulse(
                "pal:0x12", "blue", {"reset": True, "cycle_mod": 0x40})],
             "label": "reset-ping then slow-end re-color",
             "expect": "black-out ping, then the pulse runs in PURE YELLOW / "
                       "BLUE at cadence 0x40"},
        ],
    })

    # --- strobe ----------------------------------------------------------
    # Strobe color slot is SIMPLE-encoded: palette values 0x00/0x04/0x08
    # in the slot blanked every run (twice), while simple 0x61/0x67
    # flashed.  Sweeping the remaining primaries (cyan, magenta) widens
    # "any saturated simple works in the slot"; the closing probe re-tries
    # a palette color as the two-byte ``0E pp`` pair -- the slot form the
    # rotation set-piece needed -- to see whether the strobe slot accepts
    # the same encoding (A6: no palette-colored strobe exists in corpus).
    strobe_colors = ["white", "blue", "cyan", "magenta"]
    for color in strobe_colors:
        code = (int(color[4:], 0) if color.startswith("pal:")
                else mwm_send.SIMPLE_COLOR_CODES[color])
        probes.append({
            "family": "strobe",
            "name": f"strobe color={_friendly_color(color)}",
            "watch": ("Strobe flashes into a STANDING color. Look for: "
                      "strobing flashes of the color on both ears; if the "
                      "video is 'ignored' or nothing flashes, the running-"
                      "program context is missing."),
            "steps": [
                {"frames": [_ANCHOR_RESET], "pause_ms": 300,
                 "label": "anchor reset",
                 "expect": "ears snap to off; nothing running yet"},
                {"frames": [mwm_send.build_strobe(color=code)],
                 "pause_ms": 0, "label": f"strobe {_friendly_color(color)}",
                 "checkpoint": True,
                 "expect": f"both ears flash-strobe in {_friendly_color(color)}"},
            ],
            "retries": [
{"frames": [_RESET_PING, mwm_send.build_strobe(color=code)],
                  "label": "reset-ping then strobe",
                  "expect": "black-out ping, then the strobe flashes"},
            ],
        })
    probes.append({
        "family": "strobe",
        "name": "strobe pure blue (0E-form)",
        "watch": ("Palette color carried in the strobe color slot as the "
                  "two-byte ``0E pp`` pair (``F1 24 0E 04 58 02 48 84``) "
                  "instead of a bare palette index, which blanked. Look "
                  "for: strobing flashes in pure blue -- or still nothing, "
                  "meaning the strobe slot really is simple-only."),
        "steps": [
            {"frames": [_ANCHOR_RESET], "pause_ms": 300,
             "label": "anchor reset",
             "expect": "ears snap to off; nothing running yet"},
            {"frames": [mwm_send.build_strobe(palette=0x04)],
             "pause_ms": 0, "label": "strobe pure blue (0E-form)",
             "checkpoint": True,
             "expect": "both ears flash-strobe in pure blue"},
        ],
        "retries": [
            {"frames": [_RESET_PING, mwm_send.build_strobe(palette=0x04)],
             "label": "reset-ping then 0E-form strobe",
             "expect": "black-out ping, then the strobe flashes in pure blue"},
        ],
    })

    # --- fade ------------------------------------------------------------
    for color in (["white", "blue"] +
                  [f"pal:0x{p:02X}" for p in _INCANT_PALETTES[:2]]):
        if color.startswith("pal:"):
            base = [0x0E, int(color[4:], 0)]
        else:
            base = [mwm_send.SIMPLE_COLOR_CODES[color]]
        probes.append({
            "family": "fade",
            "name": f"fade-out on {_friendly_color(color)}",
            "watch": ("Fade-out from a STANDING color. Look for: the color "
                      "gradually fading to black then off. If the raw "
                      "no-reset fade is ignored, the retry is the reset-"
                      "armed corpus form."),
            "steps": [
                {"frames": [_ANCHOR_RESET], "pause_ms": 300,
                 "label": "anchor reset",
                 "expect": "ears snap to off; nothing running yet"},
                {"frames": [mwm_send.build_frame(base)],
                 "pause_ms": 300,
                 "label": f"stand {_friendly_color(color)}",
                 "expect": f"ears light up solid in {_friendly_color(color)}"},
                {"frames": [mwm_send.build_fade(delay=0xF2, cycle=0x05)],
                 "pause_ms": 0, "label": "no-reset fade", "checkpoint": True,
                 "expect": "the color gradually fades to black then off"},
            ],
            "retries": [
                {"frames": [build_fade_armed(
                    simple=mwm_send.SIMPLE_COLOR_CODES[color]
                    if not color.startswith("pal:") else None,
                    palette=int(color[4:], 0)
                    if color.startswith("pal:") else None)],
                 "label": "reset-armed colored fade",
                 "expect": "reset/arm first, then the SAME color fades to "
                           "black -- tests whether a no-reset fade needs "
                           "the leading 24"},
            ],
        })

    # --- rotation ---------------------------------------------------------
    # The corpus set-piece ALTERNATES the two ears in the base color (it
    # does NOT sweep through colors -- every observed run showed a single
    # color flashing left twice, right once, then fading out).  The D0 3D
    # tt countdown paces the alternations: a larger tt spaces the flashes
    # WIDER, so FEWER fit before the ~1 s fade (observed tt=0x04: one flash
    # then blank; tt=0x20: nothing at all) -- the tt ladder is answered, so
    # tt stays 0x01.  Re-arming does not lengthen the piece ("kept
    # restarting with green every 0.65 seconds"), so every row below is a
    # SINGLE arm.
    # Palette colors in the color slots must use the two-byte ``0E pp``
    # pair (corpus-verified rose-pink: ``FD 24 48 11 D0 3D 01 0E 0D 0E 8D
    # FC 48 85``); a BARE palette index in the slots blanks the piece
    # (pure blue 0x04) or garbles it (pale 0x00 "only yellow").  The rows
    # here are the one-field fuzz on that validated shape: slot form
    # carried over from the corpus, palette pp varied.  They use colors
    # the observer can tell apart by identity -- rose pink vs pure blue vs
    # pale cyan-white -- never sky-blue next to blue.
    rotation_rows: list[tuple[str, int, int | None, str]] = [
        ("green", 0x62, None,
         "corpus green set-piece, single arm"),
        ("blue control (simple)", 0x61, None,
         "simple-blue in both slots (same layout as corpus green) -- the "
         "SIMPLE control"),
        ("rose pink", 0x62, 0x0D,
         "palette 0x0D via the 0E pp pair -- the EXACT corpus rose-pink "
         "shape"),
        ("pure blue", 0x62, 0x04,
         "palette 0x04 via the 0E pp pair -- BLANKED as a bare index last "
         "run"),
        ("pale cyan-white", 0x62, 0x00,
         "palette 0x00 via the 0E pp pair -- 'only yellow' as a bare index "
         "last time"),
    ]
    for label, code, pp, shape in rotation_rows:
        phrase = mwm_send.rotation_phrase(code, cycle_mod=0x01, palette=pp)
        probes.append({
            "family": "rotation",
            "name": f"rotation {label}",
            "watch": (f"Color-rotation set-piece, single arm, tt=0x01: the "
                      "ears ALTERNATE in the one color (not a color "
                      "sweep), then fade to black ~1 s later."),
            "steps": [
                {"frames": [_ANCHOR_RESET], "pause_ms": 300,
                 "label": "anchor reset",
                 "expect": "ears snap to off; nothing running yet"},
                {"frames": [phrase],
                 "pause_ms": 0,
                 "label": f"rotation {label}",
                 "checkpoint": True,
                 "expect": (f"{shape}. Look for: the ears ALTERNATING (never "
                            "in unison), ~3 flashes (left twice, right "
                            "once), then fade to black at ~1 s.")},
            ],
            "retries": [
                {"frames": [_RESET_PING, phrase],
                 "label": "reset-ping then rotation",
                 "expect": "black-out ping, then the same alternating "
                           "set-piece + fade"},
            ],
        })

    # --- stop -------------------------------------------------------------
    probes.append({
        "family": "stop",
        "name": "invoke stop",
        "watch": ("Stop whatever is running and turn both ears off. Look "
                  "for: both ears going dark/off; the effect stopping."),
        "steps": [
            {"frames": [_ANCHOR_RESET, mwm_send.rotation_phrase(
                mwm_send.SIMPLE_COLOR_CODES["green"])],
             "pause_ms": 800, "label": "arm a running rotation",
             "expect": "ears start a green color-rotation and keep "
                       "sweeping"},
            {"frames": [mwm_send.build_off()], "pause_ms": 0,
             "label": "invoke off", "checkpoint": True,
             "expect": "rotation stops, both ears go dark"},
        ],
        "retries": [
            {"frames": [_RESET_PING], "label": "reset-ping stop",
             "expect": "both ears black out immediately"},
            {"frames": [mwm_send.build_off(), _RESET_PING],
             "label": "invoke off then reset-ping",
             "expect": "off invoke, then a black-out ping as backstop"},
        ],
    })

    return probes


def _classify_verdict(text: str) -> str:
    """Map a human verdict line to a code: ok / retry / broke / ignored /
    miscolor / note (anything unrecognised is a free-form note)."""
    t = text.strip().lower()
    if not t:
        return "note"
    if t in _VERDICT_WORKED:
        return "ok"
    if t in _VERDICT_RETRY:
        return "retry"
    if t in _VERDICT_BROKE:
        return "broke"
    if t in _VERDICT_IGNORED:
        return "ignored"
    if t in _VERDICT_MISCOLOR:
        return "miscount"
    # Multi-word notes that start with a verdict word still classify.
    first = t.split()[0]
    if first in _VERDICT_WORKED:
        return "ok"
    if first in _VERDICT_RETRY:
        return "retry"
    if first in _VERDICT_BROKE:
        return "broke"
    if first in _VERDICT_IGNORED:
        return "ignored"
    if first in _VERDICT_MISCOLOR:
        return "miscount"
    return "note"


def run_incant_probe(args: argparse.Namespace) -> None:
    """Rig-test the verified incantations as a decision tree.

    For each incantation family: print the "what to watch for" description,
    wait for the observer to continue, anchor to a known dark state, send the
    known park-shaped frame, then ask "did it work?" (``y`` / ``n`` / ``r`` /
    free note).  Colors are named for the observer ("pure blue", "sky
    blue", ... -- never ``pal:0xNN``).  ``r`` reissues the same command
    (capture may have been lost); a non-``y`` result steps through the
    probe's variants in order (e.g. the reset-armed corpus alternative
    ``F? 24 … 48 85``) until one works -- each asked once.  A failed velcro
    step therefore does not poison the next family: every step anchors to a
    known dark state first (docs/human-testing.md A6).
    """
    mwm_send = _load_mwm_send()
    mqtt = load_mqtt(Path(args.mqtt_json))
    repeat = args.repeat
    probes = build_incant_probes(mwm_send, incant_random=args.incant_random)

    print("MWM incantation probe (decision tree)\n")
    print("At every prompt, did it work?")
    print("  y   yes, it worked as described")
    print("  n   no / badly / split / mis-colored (moves on to the next")
    print("      variant for this probe, if any)")
    print("  r   retry (reissue the same command)")
    print("  <text>  a free-form note recorded as-is\n")

    entries: list[dict] = []
    log_path = Path(args.log) if args.log else Path(
        f"incant_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )

    def send_frames(frames: list[bytes], pauses_after_ms: int) -> list[str]:
        hex_repr: list[str] = []
        for _ in range(repeat):
            for fr in frames:
                _send_frame(mqtt, fr, repeat=1, delay=0)
                hex_repr.append(fr.hex().upper())
        if pauses_after_ms:
            time.sleep(pauses_after_ms / 1000.0)
        return hex_repr

    def log_entry(family: str, name: str, hex_str: str,
                  phase: str, note: str) -> None:
        entries.append({
            "t": datetime.now(timezone.utc).isoformat(),
            "family": family,
            "name": name,
            "phase": phase,
            "hex": hex_str,
            "repeat": repeat,
            "notes": note,
        })

    def prompt(family: str, name: str, what: str) -> str:
        print(f"{family:8s} {name}")
        print(f"    {what}")
        try:
            return input("    > ").strip()
        except (EOFError, KeyboardInterrupt):
            raise

    try:
        for probe in probes:
            fam = probe["family"]
            print(f"\n=== {fam}: {probe['name']} ===")
            print(f"  WATCH FOR: {probe['watch']}")
            print()

            held_hex: list[str] = []
            for i, step in enumerate(probe["steps"]):
                # Announce the step and what should CHANGE when issued,
                # then wait for the observer before sending anything.
                print(f"  next: {step.get('label', '')}")
                print(f"    {step.get('expect', '')}")
                try:
                    input("    [enter] to send it")
                except (EOFError, KeyboardInterrupt):
                    raise
                step_hex = send_frames(
                    step["frames"], step.get("pause_ms", 0))
                held_hex += step_hex
                last_step = (i == len(probe["steps"]) - 1)
                if step.get("checkpoint"):
                    verdict = prompt(fam, step.get("label", ""),
                                     "did it work? [y]es / [n]o / "
                                     "[r]etry (reissue) / <note>")
                    log_entry(fam, probe["name"], "+".join(held_hex),
                              step.get("label", ""), verdict)
                    v = _classify_verdict(verdict)
                    # 'retry' (r) means REISSUE: re-send the same frames and
                    # ask again -- maybe the capture was lost.  Only a still
                    # bad result on the probe's FINAL step walks the retry
                    # variants (mid-probe steps like the pulse's fresh phase
                    # have no variants of their own).
                    while v == "retry":
                        reissue_hex = send_frames(
                            step["frames"], step.get("pause_ms", 0))
                        held_hex += reissue_hex
                        verdict = prompt(fam, step.get("label", ""),
                                         "reissued. did it work? [y]es / "
                                         "[n]o / [r]etry / <note>")
                        log_entry(fam, probe["name"], "+".join(held_hex),
                                  f"reissue: {step.get('label', '')}", verdict)
                        v = _classify_verdict(verdict)
                    # Found a working path, so skip any remaining variants.
                    if v == "ok":
                        continue
                    if not last_step:
                        continue
                    # Final step still failed: step through the probe's
                    # variants in order until one works (the point of a
                    # decision tree).  'r' reissues the CURRENT variant; 'n'
                    # moves to the next variant; on the last variant the
                    # observer just says whatever they saw.
                    if probe.get("retries"):
                        total = len(probe["retries"])
                        for vi, ret in enumerate(probe["retries"], 1):
                            print(f"    variant {vi}/{total}: {ret['label']}")
                            print(f"      {ret.get('expect', '')}")
                            _send_frame(mqtt, _ANCHOR_RESET, repeat=1,
                                        delay=0)
                            input("    [enter] to send the variant")
                            v_hex = send_frames(
                                ret["frames"], ret.get("pause_ms", 0))
                            held_hex += v_hex
                            verdict = prompt(
                                fam, f"{probe['name']} [{ret['label']}]",
                                "did it work? [y]es / [n]o / "
                                "[r]etry / <note>")
                            log_entry(fam, probe["name"],
                                      "+".join(held_hex),
                                      f"variant {vi}: {ret['label']}",
                                      verdict)
                            v = _classify_verdict(verdict)
                            while v == "retry" and vi <= total:
                                reissue_hex = send_frames(
                                    ret["frames"], ret.get("pause_ms", 0))
                                held_hex += reissue_hex
                                verdict = prompt(
                                    fam,
                                    f"{probe['name']} [{ret['label']}]",
                                    "reissued. did it work? [y]es / "
                                    "[n]o / [r]etry / <note>")
                                log_entry(
                                    fam, probe["name"],
                                    "+".join(held_hex),
                                    f"variant {vi}: {ret['label']} (reissue)",
                                    verdict)
                                v = _classify_verdict(verdict)
                            if v == "ok":
                                break
                # Only reset to a known state after the whole probe, not
                # mid-probe: the pulse's fresh phase must keep RUNNING so the
                # re-color step can re-color it.  Resetting between steps
                # would black out the ears right before the re-color.
            _send_frame(mqtt, _ANCHOR_RESET, repeat=1, delay=0)
            held_hex = []
    except (EOFError, KeyboardInterrupt):
        print("\nInterrupted.")

    log_path.write_text(json.dumps({
        "session": datetime.now(timezone.utc).isoformat(),
        "mqtt_topic": mqtt["transmit"],
        "repeat": repeat,
        "entries": entries,
    }, indent=2) + "\n")
    print(f"\nLogged {len(entries)} incantation notes to {log_path}")


def run(args: argparse.Namespace) -> None:
    mqtt = load_mqtt(Path(args.mqtt_json))
    cycle = _build_cycle()
    repeat = args.repeat
    delay = args.repeat_delay
    session_ts = datetime.now(timezone.utc).isoformat()
    log_path = Path(args.log) if args.log else Path(
        f"color_cycle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )

    print("MWM color/effect cycle test")
    print(f"MQTT transmit topic: {mqtt['transmit']}")
    print(f"Logging to: {log_path}")
    print(f"Commands in cycle: {len(cycle)}")
    print(f"Repeat: {repeat}x, delay: {delay}s")
    print("Press Enter to advance after each command; type a note + Enter "
          "to log it.\n")

    entries: list[dict] = []
    try:
        for i, cmd in enumerate(cycle, 1):
            hex_data = cmd["hex"]
            kind = cmd["kind"]

            if kind == "reset":
                payload = _send_frame(mqtt, _RESET, repeat=repeat, delay=delay)
            elif kind in ("simple_both", "simple_right"):
                frame = build_frame([int(hex_data, 16)])
                payload = _send_frame(mqtt, frame, repeat=repeat, delay=delay)
            elif kind in ("palette_both", "palette_right", "effect"):
                parts = [int(x, 16) for x in hex_data.split()]
                frame = build_frame(parts)
                payload = _send_frame(mqtt, frame, repeat=repeat, delay=delay)
            elif kind == "composite":
                payload = _send_hex(mqtt, hex_data, repeat=repeat, delay=delay)
            else:
                payload = hex_data

            print(f"[{i}/{len(cycle)}] {kind:14s} {cmd['name']:28s} "
                  f"hex={hex_data}  (x{repeat})")
            if cmd.get("desc"):
                print(f"    {cmd['desc']}")

            try:
                note = input("    note> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nInterrupted.")
                break

            entries.append({
                "t": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "name": cmd["name"],
                "hex": hex_data,
                "payload": payload,
                "repeat": repeat,
                "notes": note,
            })
            print()
    except KeyboardInterrupt:
        print("\nInterrupted.")

    log = {
        "session": session_ts,
        "mqtt_topic": mqtt["transmit"],
        "total_commands": len(entries),
        "repeat": repeat,
        "repeat_delay": delay,
        "entries": entries,
    }
    log_path.write_text(json.dumps(log, indent=2) + "\n")
    print(f"Logged {len(entries)} entries to {log_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MWM ear color/effect cycling and issuance-probe test",
    )
    parser.add_argument(
        "--log", default=None,
        help="Output JSON log file (default: color_cycle_YYYYMMDD_HHMMSS.json)",
    )
    parser.add_argument(
        "--mqtt-json", default=str(_DEFAULT_MQTT),
        help="Path to MQTT config JSON (default: ~/.config/ir-remote-tools/mqtt.json)",
    )
    parser.add_argument(
        "--repeat", type=int, default=3,
        help="Send each command N times for human visibility (default: 3)",
    )
    parser.add_argument(
        "--repeat-delay", type=float, default=0.3,
        help="Delay in seconds between repeated sends (default: 0.3)",
    )
    parser.add_argument(
        "--effects", action="store_true",
        help="Run the effect issuance-method probe instead of the color cycle",
    )
    parser.add_argument(
        "--effect-color", default="0x67",
        help="Desired seed color for the effect probe: a bare simple code "
             "like '0x67' or 'pal:0x0C' (default: 0x67 both-white)",
    )
    parser.add_argument(
        "--dig", action="store_true",
        help="Run the focussed, state-settling dig probe for the suspect "
             "effects (orientation anchor + repeat-stability) instead of "
             "the effect method probe",
    )
    parser.add_argument(
        "--dig-effects", nargs="*", default=None,
        help="Suspect effects to dig (hex, e.g. 0x03 0x00). Defaults to the "
             "suspect set from the first --effects pass",
    )
    parser.add_argument(
        "--settle-secs", type=float, default=2.0,
        help="Seconds to wait after each dark/seed settle in --dig "
             "(default: 2.0)",
    )
    parser.add_argument(
        "--incant", action="store_true",
        help="Run the incantation probe instead of the color cycle: the "
             "verified show incantations (pulse fresh + reset re-color, "
             "strobe, fade, rotation, stop) across simple/palette color "
             "combinations incl. the interchange probes",
    )
    parser.add_argument(
        "--incant-random", type=int, default=0,
        help="Extra random color combos to mix into the --incant pulse "
             "sweep on top of the fixed battery (default: 0)",
    )
    args = parser.parse_args()
    if args.dig:
        run_dig_probe(args)
    elif args.effects:
        run_effect_probe(args)
    elif args.incant:
        run_incant_probe(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
