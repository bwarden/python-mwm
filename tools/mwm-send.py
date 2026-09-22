#!/usr/bin/env python3
"""Interactive MWM show-command transmitter with live beacon readback.

PURPOSE
-------
Human-driven rig tool for SENDING show commands to MWM ears and watching
what the ears do in response.  This is the counterpart to the passive
research tools (capture_beacons.py / color_cycle.py): where those log what
the ears say on their own, this lets you deliberately drive them and
correlate the send with the ears' resulting beacon chatter.

It covers the full verified command catalog from
docs/mwm-show-protocol.md section 4 + 5:
  * simple colors (both / left-only)         `90 6X` / `90 6[8-F]`
  * mixed palette shades (both / left-only)   `91 0E XX` / `91 0E XX|80`
  * fused left/right frame                     `91 6L 6R ..`
  * built-in effects                           `24 48 XX` (e.g. 0x85 fade out)
  * stored demo programs (watch the ears)      `demo 0xNN`; `demo` lists catalog
  * group addressing (color / palette by group range)
  * clock write                                `91 0C tt`
  * 55 AA system messages (games / ride shutdown)
  * arbitrary raw hex frames (wand templates etc.)

Optional --monitor: while you send, it subscribes to a receiver for a few
seconds and prints every beacon/frame the ears emit back, so you can see
the effect take hold or the ear re-beacon with its new state.

Requires: the paho-mqtt package, MQTT config at
~/.config/ir-remote-tools/mqtt.json, and the _mwm library (shared bootstrap).

Usage::

    python3 tools/mwm-send.py                          # interactive menu
    python3 tools/mwm-send.py --monitor 600605 --after 8   # monitor too
    python3 tools/mwm-send.py --no-end-reset sequence park-x.msh  # keep last state
    python3 tools/mwm-send.py --hex "91 61 6A 06" --repeat 2   # one-shot
    python3 tools/mwm-send.py --effect 0x85 --monitor 600605
    python3 tools/mwm-send.py demo 0x17             # demand a demo program
    python3 tools/mwm-send.py demo                  # catalog
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402
from _mqtt import (  # noqa: E402
    load_mqtt,
    capture_lines,
    decode_frame_lines,
    send_frame,
    send_payload,
)

_MIN_GAP_MS = 30.0   # minimum spacing between consecutive publishes

# End-of-show reset: after a `sequence` finishes, send both ears off
# (``90 60 A6``) so ears never stay lit in the show's final state.  The
# ears are remote -- we cannot see them -- so default to a guaranteed-off
# state; ``--no-end-reset`` reproduces a script's ending verbatim.
END_RESET_FRAMES = (mwm.build_frame([0x60]),)

PALETTE = mwm.PALETTE
SIMPLE_COLORS = mwm.SIMPLE_COLORS
SIMPLE_COLOR_CODES = mwm.SIMPLE_COLOR_CODES
EFFECT_LABELS = mwm.EFFECT_LABELS
DEMO_BEACONS = mwm.DEMO_BEACONS
build_frame = mwm.build_frame
build_clock_write = mwm.build_clock_write
build_group_color = mwm.build_group_color
build_group_palette = mwm.build_group_palette
build_55aa = mwm.build_55aa
build_pulse = mwm.build_pulse
build_strobe = mwm.build_strobe
build_fade = mwm.build_fade
build_cascade = mwm.build_cascade
build_sparse_cascade = mwm.build_sparse_cascade
CASCADE_DELAYS = mwm.CASCADE_DELAYS
cue_class = mwm.cue_class
build_off = mwm.build_off
rotation_phrase = mwm.rotation_phrase
irsend_payload = mwm.irsend_payload
parse_frame_hex = mwm.parse_frame_hex
frame_complete = mwm.frame_complete
describe_frame = mwm.describe_frame
effect_label = mwm.effect_label
demo_beacon_label = mwm.demo_beacon_label

_DEFAULT_MQTT = Path.home() / ".config" / "ir-remote-tools" / "mqtt.json"

EAR_OFF = 0x60
LEFT_ONLY_BASE = 0x68
GO = 0x20                                   # immediate go copy (no countdown)


# ---------------------------------------------------------------------------
# Command builders (each returns a list of frame bytes)
# ---------------------------------------------------------------------------

def _simple(ear: str, color: str) -> list[bytes]:
    code = SIMPLE_COLOR_CODES[color]
    if ear == "left":
        return [build_frame([LEFT_ONLY_BASE + code - EAR_OFF])]
    return [build_frame([code])]


def _palette(ear: str, index: int) -> list[bytes]:
    return [build_frame([0x0E, index | (0x80 if ear == "left" else 0)])]


def _color_spec(spec: str) -> tuple[str, object]:
    """Parse a color arg: simple name ('blue') or palette index ('pal:0x19')."""
    s = spec.lower()
    if s in SIMPLE_COLOR_CODES:
        return ("simple", s)
    if s.startswith("pal:"):
        idx = int(s[4:], 0)
        if 0 <= idx <= 0x1C:
            return ("palette", idx)
        raise ValueError(f"palette index out of range in '{spec}'")
    raise ValueError(
        f"unknown color '{spec}' (simple name, or 'pal:0x00'-'pal:0x1C')")


def _incant_pulse(left_spec: str, right_spec: str, opts: dict) -> list[bytes]:
    """Fused pulse phrase with per-slot simple/palette color composition.

    Each ear's color is encoded according to its kind: simple ``6X`` for
    both ears / ``6X|08`` left-only, palette ``0E pp`` both / ``0E pp|80``
    left-only.  The corpus shapes put a palette LEFT + simple RIGHT; passing
    the other encoding in either slot is the interchange probe (A6): the
    frame is still a proper fused phrase but the byte sequence departs from
    what has been seen in park captures.
    """
    lk, lv = _color_spec(left_spec)
    rk, rv = _color_spec(right_spec)
    left = (
        [0x0E, int(lv) | 0x80]
        if lk == "palette"
        else [LEFT_ONLY_BASE + SIMPLE_COLOR_CODES[lv] - EAR_OFF]
    )
    right = (
        [0x0E, int(rv)]
        if rk == "palette"
        else [SIMPLE_COLOR_CODES[rv]]
    )
    if opts.get("reset"):
        cycle_mod = opts.get("cycle_mod", 0x06)
        content = [
            0x20, 0x24, 0x0D,
            *right,
            *left,
            0x58, 0xF0,            # ~100 ms cycle timer (pulse needs 58 F0)
            0x48, 0x04,            # invoke Pulse
            0xD0, 0x42, cycle_mod,
        ]
    else:
        both_first = opts.get("both_first", False)
        prefix = [0x94, 0x26] if both_first else [0x96, 0x26]
        if both_first:
            content = [*prefix, *right, *left]
        else:
            content = [*prefix, *left, *right]
        content += [0x58, 0xF0, 0x48, 0x04]
        if opts.get("cycle_mod") is not None:
            content += [0xD0, 0x42, opts["cycle_mod"]]
        content += [0xD0, 0x45, 0x83]  # closing D0 clause
    return [build_frame(content)]


def _fused(left_spec: str, right_spec: str) -> list[bytes]:
    """One-burst fused frame: first arg = LEFT ear, second = RIGHT ear.

    A fused frame's opcodes run sequentially against both ears: a both-ear
    op first (simple `6X` or palette `0E pp`), then a left-only op
    (simple `6X|08` or palette `0E pp|80`):
      * left-simple + right-simple : `91 6R 6L|08 ..`
      * right-simple + left-palette : `92 6R 0E L|80 ..`
      * left-simple + right-palette : `92 0E R 6L|08 ..`
      * left-palette + right-palette : `93 0E R 0E L|80 ..`

    The palette-involving forms are structurally valid but UNVERIFIED on real
    ears -- see docs/human-testing.md A3a.
    """
    lk, lv = _color_spec(left_spec)
    rk, rv = _color_spec(right_spec)
    if lk == "simple" and rk == "simple":
        return [build_frame([SIMPLE_COLOR_CODES[rv],
                             LEFT_ONLY_BASE + SIMPLE_COLOR_CODES[lv] - EAR_OFF])]
    if lk == "palette" and rk == "simple":
        return [build_frame([SIMPLE_COLOR_CODES[rv], 0x0E, int(lv) | 0x80])]
    if lk == "simple" and rk == "palette":
        return [build_frame([0x0E, int(rv),
                             LEFT_ONLY_BASE + SIMPLE_COLOR_CODES[lv] - EAR_OFF])]
    return [build_frame([0x0E, int(rv), 0x0E, int(lv) | 0x80])]


def _effect(index: int) -> list[bytes]:
    # 24 lets an invocation take effect while a built-in program runs.
    return [build_frame([0x24, 0x48, index])]


def _clock_write(tick: int) -> list[bytes]:
    return [build_clock_write(tick)]


def _group_color(lo: int, hi: int, code: int) -> list[bytes]:
    return [build_group_color(lo, hi, code)]


def _group_palette(lo: int, hi: int, index: int) -> list[bytes]:
    return [build_group_palette(lo, hi, index)]


def _sys55(payload_hex: str) -> list[bytes]:
    payload = [int(x, 16) for x in payload_hex.split()]
    return [build_55aa(payload)]


def _incant(
    family: str,
    lp: int | None,
    right_simple: int | None,
    opts: dict,
) -> list[bytes]:
    """Verified show-command incantations mined from the real captures.

    Each is a single fused phrase (per-ear palette + cycle timer + effect
    invoke + closing D0 clause) whose shape -- and usually exact bytes --
    was observed in the park/hat corpus.  See python/mwm/incant.py.
    """
    if family == "pulse":
        if lp is None or right_simple is None:
            raise ValueError("pulse needs <palette-index> <simple-color>")
        return [build_pulse(lp, right_simple, **opts)]
    if family == "strobe":
        return [build_strobe(**opts)]
    if family == "stop":
        return [build_off()]
    if family == "fade":
        return [build_fade(**opts)]
    if family == "rotation":
        if right_simple is None:
            raise ValueError("rotation needs <simple-color>")
        return [rotation_phrase(right_simple, **opts)]
    raise ValueError(f"unknown incantation '{family}'")


def _cascade_content(argstr: str) -> list[int]:
    """The delay-led phrase CONTENT (delay byte + tail) of a ``cue``/``cascade``
    spec, and the authoritative validity check for the lead delay byte.

    The delay byte is content[0] in every delay-led show phrase (fade's
    ``F? 48 85 58 tt``, strobe's ``F? 24 67 58 tt 48 84``, ...).  A cue
    carries either the immediate ``20`` go-variant -- the phrase that
    executes at the cue's ``@ms``, with the countdown generated over the
    canonical chain -- or a ``F?`` delay-led phrase whose countdown derives
    from the lead: the ear that hears the frame fires at receipt + delay,
    so the countdown member set derives from it.  Accepts
    ``cue fade ...``, ``cue incant fade ...``, and a raw ``cue hex <bytes>``
    form whose first byte is a delay byte -- the corpus phrase minus its
    auto-derived 9X head (e.g. ``cue hex F1 24 0D 48 82 D0 0E FF`` reproduces
    the verified 97/9C-style hard-transition chains; the countdown derives
    its head from the content length, so ``F1 24 .. `` also matches ``24 ..``
    members).
    """
    if argstr.startswith("incant "):
        argstr = argstr[len("incant "):]
    if argstr.startswith("hex "):
        phrase = argstr[len("hex "):].split()
        if not all(_HEX_RE.match(x) for x in phrase):
            raise ValueError("cascade hex expects space-separated hex bytes")
        content = [int(x, 16) for x in phrase]
    else:
        frames = _build_frames(f"incant {argstr}")
        if not frames or len(frames) != 1:
            raise ValueError(f"cascade needs a single-incantation spec: {argstr!r}")
        content = list(frames[0][1:-1])
    if not content or content[0] not in (*range(0x20, 0x21),
                                          *range(0xF0, 0x100)):
        raise ValueError("cascade phrase does not lead with a delay byte")
    return content


def _cascade_tail(argstr: str) -> list[int]:
    """The cue phrase WITHOUT its leading delay byte (see
    ``_cascade_content``); the countdown rebuilds the chain from the lead
    byte's own set, or ``build_cascade(tail)`` for the full canonical
    corpus chain (FD..F1, then the immediate 20)."""
    return _cascade_content(argstr)[1:]


def _cascade(cmd: str) -> list[bytes]:
    """Expand a ``cue <spec>``/``cascade <spec>`` menu line over the full
    canonical countdown chain (FD..F1 then the immediate 20 go copy)."""
    return list(build_cascade(_cascade_tail(cmd)))


# ---------------------------------------------------------------------------
# Display / decode helpers
# ---------------------------------------------------------------------------

def _frame_desc(frame: bytes) -> str:
    try:
        return describe_frame(frame)["summary"]
    except Exception:
        return frame.hex().upper()


def _demo_catalog_lines() -> list[str]:
    """Render the observed demo-mode `48 ss` catalog (DEMO_BEACONS)."""
    lines = ["observed demo-mode program indices (beacon `48 ss` field):"]
    for idx, label in DEMO_BEACONS.items():
        lines.append(f"    {idx:02X}   48 {idx:02X}  {label}")
    lines.append("    (indices come from docs §5 + headband-20260822.log;")
    lines.append("     unlabelled = stored program, no verified visual yet)")
    return lines


# ---------------------------------------------------------------------------
# Monitor: listen for a few seconds and report what the ears emit
# ---------------------------------------------------------------------------

def _monitor(mqtt: dict, receiver: str, seconds: float) -> None:
    """Listen for ear response (beacons / commands) and print what the ears
    emit.  NOTE: this is passive -- a demanded demo program (24 48 xx) does
    NOT produce a beacon back; ears only beacon once they autonomously
    revert to demo mode and re-pick a program.  The `demo` verb's
    confirmation is your own eyes on the ears, not this readback."""
    print(f"\n-- listening to {receiver} for {seconds:.0f}s for ear response "
          f"(Ctrl-C to skip) --", flush=True)
    lines = capture_lines(mqtt, receiver, seconds)
    frames = decode_frame_lines(mwm, lines)
    if not frames:
        print("-- (no frames heard) --")
        return
    for fr in frames:
        print(f"  RX {fr.hex().upper():24s} -> {_frame_desc(fr)}")


# ---------------------------------------------------------------------------
# Show scripts: human-editable beat lists expanded into cascades
# ---------------------------------------------------------------------------

def _parse_show_script(text: str, cascade_ms: float | None = None,
                       cascade_full: bool = False) -> list[dict]:
    """Parse a show-script file into ordered beats.

    Format (human-editable; copy/paste/edit freely)::

        # comment lines and blank lines are ignored
        @1000                       optional: ms offset to the *next* beat
        simple left blue            one beat per line, in menu grammar
        cue fade cycle=0x16         a countdown cue: phrase + the moment its
                                    20 go copy lands ('cascade' also accepted)
        cue hex 20 24 D1 ..         a cue carrying the IMMEDIATE 20 go-variant
                                    of the phrase; the countdown is generated
                                    here and pre-rolls before this @ms

    A ``cue`` beat auto-generates its countdown.  What the line carries is
    the phrase that executes AT ``@ms``: a ``20``-led cue (what the
    generator emits) means the immediate go-variant fires there and the
    countdown is generated over the full canonical ``CASCADE_DELAYS``
    chain; a ``F?``-led cue (legacy / hand-written) derives its own
    smaller member set from the lead.  Either way, member delays run from
    the countdown start down to the immediate ``20`` go copy, and each
    member is scheduled to pre-roll BEFORE the beat's ``@ms`` at
    ``@ms - (d & 0x0F)*100`` -- the ``F?`` countdown bytes delay the ear by
    their LOW NIBBLE x 100 ms (F1=100 ms .. FF=1500 ms), so a listener
    that hears ANY member still lands exactly on the master moment the
    ``20`` fires, whether the countdown was captured replete or cut short.
    The played member order puts the go
    copy LAST (a late F1 would otherwise re-arm the ears a tenth of a
    second after the cue).  A
    ``members`` clause (``cue hex <lead> <tail> members <other delays,
    incl. 20>``) overrides the generated set with an EXACT legacy
    capture set when a hand-written script needs byte-for-byte
    reproduction.  ``--cascade-ms`` opts back into uniform pacing from the
    ``@ms`` target, and ``--cascade-full`` forces the 14-member FD-led
    capture chain regardless of the lead byte.  Beats are timed by their
    ``@ms`` offsets; a beat with no preceding ``@`` lands at the cumulative
    time of the beats before it.
    Returns a list of ``{"t_ms": int|None, "frames": [bytes],
    "rel_ms": [float], "cascade": bool}`` where ``rel_ms`` is each member's
    offset from the beat's ``@ms`` (negative = pre-roll countdown; the go
    copy rides ``@ms`` itself for the default scheduling) and ``cascade``
    marks a countdown-chain beat.
    """
    def expand(line: str, lineno: int) -> tuple[list[bytes], list[float], bool]:
        verb, _, argstr = line.partition(" ")
        if verb in ("cue", "cascade"):
            # Legacy exact-set form still parses:
            #  cue hex <lead> <tail> members <other delays, incl. 20>
            members: list[int] | None = None
            if " members " in argstr:
                argstr, _, member_str = argstr.partition(" members ")
                mbytes = member_str.split()
                if not all(_HEX_RE.match(x) for x in mbytes):
                    raise ValueError(
                        f"line {lineno}: {verb} members must be hex bytes")
                members = [int(x, 16) for x in mbytes]
            content = _cascade_content(argstr)
            tail = content[1:]
            lead = content[0]
            if members is not None:
                delays = [lead, *members]
            elif cascade_full or lead == GO:
                # A 20-led cue (what gen_show_script emits) carries ONLY the
                # immediate go-variant that fires at @ms; the countdown is
                # generated here, over the full canonical capture chain.
                delays = list(CASCADE_DELAYS)
            else:
                # A legacy/hand-written delay-led cue: derive the countdown
                # from its own lead byte (F?..F1, then the go copy).
                delays = list(range(lead, 0xF0, -1)) + [0x20]
            ordered = sorted(delays, reverse=True)   # 20 go copy transmits last
            frames = [build_frame([d, *tail]) for d in ordered]
            if cascade_ms:
                step = float(cascade_ms)
                rel = [i * step for i in range(len(ordered))]
            else:
                # The F? countdown bytes delay the ear by (d & 0x0F)*100 ms
                # (F1=100 ms .. FF=1500 ms); the 20 go copy is immediate, so
                # the whole pre-roll airs in the lead's 100..1500 ms before
                # the GO lands on @ms.
                rel = [0.0 if d == 0x20 else -((d & 0x0F) * 100.0)
                       for d in ordered]
            return frames, rel, True
        frames = _build_frames(line)
        if frames is None:
            raise ValueError(f"line {lineno}: unknown command: {line!r}")
        return frames, [0.0] * len(frames), False

    beats: list[dict] = []
    pending_ms: int | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            # '@ms' alone sets the offset for the NEXT beat; '@ms <cmd>'
            # tags the beat on the same line.
            tok, _, rest = line[1:].partition(" ")
            ms = int(tok.strip())
            if not rest.strip():
                pending_ms = ms
                continue
            line = rest.strip()
            frames, rel, cascade = expand(line, lineno)
            beats.append({"t_ms": ms, "frames": frames, "rel_ms": rel,
                          "cascade": cascade})
            pending_ms = None
            continue
        frames, rel, cascade = expand(line, lineno)
        beats.append({"t_ms": pending_ms, "frames": frames, "rel_ms": rel,
                      "cascade": cascade})
        pending_ms = None
    if pending_ms is not None:
        raise ValueError("dangling @ms offset with no following beat")
    return beats


def _format_current(elapsed_s: float, i: int, n: int, t0_ms: int,
                    member: int, nmembers: int, desc: str) -> str:
    """Live status for the beat/member currently on the air."""
    return (f"[{elapsed_s:7.1f}s] beat {i + 1}/{n} @{t0_ms:6d}ms "
            f"member {member + 1}/{nmembers}  {desc}")


def _format_next(delta_s: float, t0_ms: int, desc: str) -> str:
    """Status suffix for the UPCOMING beat (pinned on /dev/tty): the
    remaining seconds until its first member goes out, as measured when
    the last publish happened (the terminal is quiet between beats)."""
    return (f"[next in {delta_s:6.1f}s -> @{t0_ms:6d}ms] {desc}")


def _open_tty() -> typing.IO[str] | None:
    """Write handle to the controlling terminal, or None without one.

    Transient progress (the beat happening now / next in) is rewritten
    HERE, never on stdout/stderr -- so ``> log`` / ``|`` capture the stdout
    log of what *happened* and cannot sweep up the rewritable status line.
    """
    try:
        return open("/dev/tty", "w")
    except OSError:
        return None


def _member_time(t0: float, rel: float, last: float, min_gap_ms: float) -> float:
    """Absolute ms a member goes out: its re-based target ``t0 + rel``,
    clamped up to a ``--min-gap-ms`` floor against the previous publish and
    never before 0 (a show opening mid-pre-roll has no time in front of it).
    The single source of truth consumed by ``--dump``, ``compare_replay``,
    and the live send loop, so all three agree on the wire."""
    t = t0 + rel
    if t < last + min_gap_ms:
        t = last + min_gap_ms
    return max(0.0, t)


def _plan_publish_times(plan: list[tuple[int, list[bytes], list[float], bool]],
                        min_gap_ms: float, end_reset: bool) -> list[dict]:
    """Per-member go schedule exactly as the sender will transmit it.

    Mirrors the ``_run_sequence`` send loop's plan + clamp: a cue's members
    land at ``@ms + rel`` (default rel = ``-(d & 0x0F)*100``, the ``F?``
    low-nibble delay, so the countdown pre-rolls the lead's 100..1500 ms
    before its GO and every ear converges on the master moment the immediate
    ``20`` lands), each consecutive publish held to the
    ``--min-gap-ms`` floor, and a pre-roll member whose clamp (or the start
    of the show) pushes it OFF its true ``@ms + rel`` slot is dropped as
    redundant -- the cue's own GO stays anchored on ``@ms`` and is never
    preempted, so a cue that can't pre-roll (tight gaps, or members whose
    slots landed before the previous cue's GO) still fires its effect on
    time instead of blasting its whole countdown into an off-slot burst.
    ``--dump`` prints this (so a show's full transmit stream can be diffed
    against the source capture); it is also the same schedule
    ``compare_replay`` derives for the receiver log.
    """
    expected: list[dict] = []
    last = -1e300
    for bi, (t0, frames, rel, cascade) in enumerate(plan):
        for mi, fr in enumerate(frames):
            rt = rel[mi] if rel else 0.0
            t = _member_time(t0, rt, last, min_gap_ms)
            if rt < 0 and t != t0 + rt:
                continue
            last = t
            expected.append({"t": t, "hex": fr.hex().upper(), "member": mi,
                             "step": rt, "beat": bi, "cascade": cascade})
    if end_reset:
        expected.append({"t": last + min_gap_ms, "hex": END_RESET_FRAMES[0],
                         "member": 0, "step": 0.0, "beat": len(plan),
                         "cascade": False})
    return expected


def _run_sequence(mqtt: dict, beats: list[dict], args: argparse.Namespace) -> None:
    """Execute an ordered beat timeline on the transmit topic.

    Beats are keyed off absolute ``t_ms`` from the script (cumulative time
    when a beat had no explicit ``@ms``).  A cue's ``@ms`` IS its master
    moment: the immediate ``20`` go copy fires there, and the countdown
    members schedule BEFORE it (member ``d`` at ``@ms - (d & 0x0F)*100`` --
    the ``F?`` bytes are low-nibble x 100 ms delays, F1=100 ms .. FF=1500 ms)
    so every
    ear that hears ANY member -- the ears re-fire at receipt + delay --
    converges on exactly that moment even if its own countdown was cut
    short.  A ``--cascade-ms`` override paces the members uniformly FROM
    ``@ms`` instead.  Beats send on the plan's exact clock; the
    ``--min-gap-ms`` floor (default 30 ms) keeps consecutive publishes from
    collapsing into one burst, and a pre-roll member the floor (or the
    start of the show) would push OFF its true ``@ms + rel`` slot is
    dropped as redundant so the cue can never fire late or blast its
    countdown into an off-slot burst -- only members that can pre-roll in
    their own slot go out, and the GO is always kept.

    Pacing: ``--min-gap-ms`` (default 30 ms) is the smallest gap enforced
    between consecutive publishes, so beats sharing one ``@ms`` tick (a
    capture near-simultaneous cluster) go out serially instead of several
    frames at once.  Every other timing in the script is played VERBATIM:
    a gap in the ``.msh`` is a real wait (mwm-send never collapses or
    fills long silent stretches -- that rewriting belongs to
    ``gen_show_script.py`` when it builds a demo from captured data).
    What *happened* is logged to stdout, one newline line per publish
    (``> log`` / ``|`` capture exactly what was sent); the *next-beat*
    status rides the LAST line on the controlling terminal (``/dev/tty``),
    overwritten in place only when a real publish happens and erased
    before each history line scrolls so it never smears up into the
    scrollable send log.  Between beats the terminal is untouched: a
    long silent gap in the script produces no terminal activity at all.
    """
    min_gap_ms = getattr(args, "min_gap_ms", _MIN_GAP_MS)
    min_gap = min_gap_ms / 1000.0
    cum = 0
    plan: list[tuple[int, list[bytes], list[float], bool]] = []
    for beat in beats:
        if beat["t_ms"] is not None:
            cum = beat["t_ms"]
        plan.append((cum, beat["frames"], beat.get("rel_ms", [0.0]),
                     beat.get("cascade", False)))

    total = max((t + max(rel) for t, f, rel, _c in plan), default=0)
    if getattr(args, "dump", False):
        for e in _plan_publish_times(
                plan, min_gap_ms,
                not getattr(args, "no_end_reset", False)):
            print(f"@{e['t']:.0f} {e['hex']}")
        return
    print(f"  sequence: {len(plan)} beat(s), ~{total / 1000:.1f}s total")
    for t0, checked, rel, _c in plan:
        cls = cue_class(checked[0])
        marker = ""
        if cls == "PRE_BUFFER_EVENT" and rel and min(rel) < 0:
            # A countdown cue: `@ms` anchors the GO; the members pre-roll
            # before it so every ear converges on the master moment.
            marker = f"  {int(-min(rel))}ms countdown, go@{t0}ms"
        print(f"    @{t0:5d} ms  {len(checked):2d} frame(s)  {cls}{marker}  "
              f"{_frame_desc(checked[0])}")

    if args.monitor:
        print("  (monitoring after the last beat)")
    if not getattr(args, "no_end_reset", False):
        print(f"  + end-of-show reset: "
              f"{' '.join(f.hex().upper() for f in END_RESET_FRAMES)} "
              f"({_frame_desc(END_RESET_FRAMES[0])}) "
              f"[--no-end-reset to omit]")
    if args.dry_run:
        print("  (dry run: no frames sent)")
        return

    min_gap = getattr(args, "min_gap_ms", _MIN_GAP_MS) / 1000.0
    nplan = len(plan)
    base = time.monotonic()
    last_sent = -1e300
    tty = _open_tty()
    pad = ""
    next_t0: int | None = None
    next_frames: list[bytes] = []
    last_ev: tuple[int, int, int, int, int, str] | None = None
    to_stdout_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())

    def draw_status() -> None:
        """Pin the next-beat status to the LAST line on /dev/tty.

        History scrolls above it (stdout); this bar is rewritten in place
        with ``\\r``, but ONLY when a real publish happens (see ``emit``)
        -- during a wait the terminal is left completely quiet, so the
        countdown shown is the snapshot from the last send, not a live
        tick.  It is erased before each history line scrolls, so it never
        smears up into the send log."""
        nonlocal pad
        if tty is None:
            return
        now = time.monotonic()
        if last_ev is not None:
            line = _format_current(now - base, *last_ev)
        else:
            line = ""
        if next_t0 is not None:
            nxt = max(base + next_t0 / 1000.0, last_sent + min_gap)
            line += "  |  " + _format_next(
                max(0.0, nxt - now),
                next_t0, _frame_desc(next_frames[0]))
        tty.write("\r" + line + " " * max(0, len(pad) - len(line)))
        tty.flush()
        pad = line

    def emit(hist_line: str, redraw: bool = True) -> None:
        """One history scroll-line (a real publish) to stdout.

        If stdout is a terminal that shares the screen with the pinned
        status bar, erase the bar, let the line scroll, then redraw the
        bar at the bottom -- so the two never interleave in the scrollback.
        With stdout piped (``>``/``|``) the pipe gets a clean newline log
        and the terminal keeps only the status bar.  ``redraw=False``
        (the closing end-of-show reset) scrolls the line but leaves the
        bar cleared.
        """
        if tty is not None and to_stdout_tty:
            tty.write("\r" + " " * len(pad) + "\r")
            tty.flush()
        print(hist_line, flush=True)
        if tty is not None and redraw:
            draw_status()

    last_gap = -1e300   # virtual ms floor across the WHOLE show (mirrors --dump)
    for i, (t0, frames, rel, _c) in enumerate(plan):
        if i + 1 < nplan:
            next_t0, next_frames = plan[i + 1][0], plan[i + 1][1]
        else:
            next_t0 = None
        for member, payload in enumerate(irsend_payload(f) for f in frames):
            rt = rel[member] if rel else 0.0
            t = _member_time(t0, rt, last_gap, min_gap_ms)
            if rt < 0 and t != t0 + rt:
                continue   # no room to pre-roll in its true slot: the cue's GO is enough
            last_gap = t
            target = max(base + t / 1000.0, last_sent + min_gap)
            now = time.monotonic()
            while now < target:
                # Quiet wait: NO writes while nothing goes on the wire.  A
                # 0.2 s countdown tick "scrolled" in captures because every
                # rewrite landed on its own line; the only things that may
                # touch the terminal are real publishes (emit/draw_status).
                time.sleep(min(0.2, target - now))
                now = time.monotonic()
            send_payload(mqtt, payload)
            last_sent = time.monotonic()
            desc = _frame_desc(frames[member])
            last_ev = (i, nplan, t0, member, len(frames), desc)
            emit(_format_current(last_sent - base, i, nplan, t0,
                                 member, len(frames), desc))
    if tty is not None:
        tty.write("\r" + " " * len(pad) + "\r\n")
        tty.flush()
    if not args.dry_run and not getattr(args, "no_end_reset", False):
        for f in END_RESET_FRAMES:
            target = max(last_sent + min_gap, time.monotonic())
            while time.monotonic() < target:
                time.sleep(min(0.2, target - time.monotonic()))
            send_payload(mqtt, irsend_payload(f))
            last_sent = time.monotonic()
            emit(f"[{last_sent - base:7.1f}s] end-of-show reset  "
                 f"{f.hex().upper()}  ({_frame_desc(f)})", redraw=False)
    if args.monitor:
        _monitor(mqtt, args.monitor, args.after)


# ---------------------------------------------------------------------------
# The interactive REPL
# ---------------------------------------------------------------------------

def _menu(mqtt: dict, args: argparse.Namespace) -> None:
    repeat = args.repeat
    delay = args.repeat_delay
    monitor_rcv = args.monitor
    print(f"MQTT transmit: {mqtt['transmit']}"
          + (f"   monitor: {monitor_rcv}" if monitor_rcv else ""))
    print(f"repeat={repeat}x delay={delay}s   type 'q' to quit, 'help' for cmds\n")

    while True:
        try:
            raw = input("mwm> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        if raw in ("q", "quit", "exit"):
            return
        if raw in ("h", "help"):
            _print_help()
            continue

        parts = raw.split()
        verb = parts[0].lower()
        if verb == "demo" and len(parts) == 1:
            print("\n".join(_demo_catalog_lines()))
            continue

        if verb == "sequence":
            try:
                beats = _parse_show_script(
                    Path(" ".join(parts[1:])).read_text(),
                    args.cascade_ms, args.cascade_full)
            except (ValueError, OSError) as exc:
                print(f"  error: {exc}")
                continue
            _run_sequence(mqtt, beats, args)
            continue

        frames: list[bytes] | None
        try:
            if verb == "demo":
                frames = _effect(int(parts[1], 0))
            else:
                frames = _build_frames(raw)
        except ValueError as exc:
            print(f"  error: {exc}")
            continue
        if frames is None:
            print("  (unknown command; try 'help')")
            continue

        reps = _cue_repeat(frames, args)
        payloads = [irsend_payload(f) for f in frames]
        print(f"  sending {len(frames)} frame(s), {reps}x:")
        for f, p in zip(frames, payloads):
            print(f"    {f.hex().upper():24s} -> {_frame_desc(f)}")
        # frames in one logical pass back-to-back (footers space them),
        # repeated `reps` times.
        try:
            for _ in range(reps):
                for p in payloads:
                    send_payload(mqtt, p)
                if reps > 1 and delay > 0:
                    time.sleep(delay)
        except RuntimeError as exc:
            print(f"  send failed: {exc}")
        if monitor_rcv:
            _monitor(mqtt, monitor_rcv, args.after)


def _print_help() -> None:
    print("""
commands:
  simple <ear> <color>          ear=left|right|both, color=blue|green|cyan|red|magenta|yellow|white
  palette <ear> <0-1C>          ear=left|right|both; palette index 0x00-0x1C
  fused <left> <right>          one-burst fused frame; each arg is a simple
                                color name or 'pal:0x00'-'pal:0x1C'. Covers
                                simple/simple, simple/palette, palette/simple,
                                 palette/palette. Palette forms UNVERIFIED (A3a).
  effect <hex|dec>              effect program index, e.g. 0x85 fade out, 0x11 rotation
  demo <hex|dec>                demand a stored demo program (24 48 XX);
                                confirm on the EARS -- there is no beacon
                                ack (ears beacon only on auto demo re-pick)
  demo                          (bare) list the observed demo-mode program
                                catalog (DEMO_BEACONS) and usage hints
  clock <hex>                   write sync-clock tick, e.g. 0x40
  groupcolor <lo> <hi> <code>   group range + simple-color code (0x60-0x67)
  grouppalette <lo> <hi> <idx>  group range + palette index
  sys <hex bytes>               55 AA system message payload (after AA)
  incant pulse <left-color> <right-color> [reset=] [both_first=] [cycle_mod=]
                                 fused pulse phrase from the real corpus:
                                 left-ear palette + right simple color each
                                 as a simple name or 'pal:0xNN' (interchange
                                 is an open A6 probe; see docs/human-testing)
  incant strobe [color=] [timer=]  strobe-into-program phrase (white 0x02)
                                 color accepts a simple name or 'pal:0xNN'
  incant fade [cycle=] [delay=]    fade-out countdown phrase (0x05, 0xf2)
  incant rotation <color>          color-rotation set-piece w/ delayed fade;
                                 color accepts a simple name or 'pal:0xNN'
  incant stop                      stop the program: invoke off (48 1F)
cue <incant args>               expand an incantation over the full
                                 countdown chain (FD..F1 then the 20 go
                                 copy) -- the park's delay led cues
                                 ('cascade' is an accepted older alias)
                                 e.g. cue fade cycle=0x16
  sequence [<file>]                run a show-script file (@ms offsets set
                                   beat times; 'cue' lines are countdown
                                   cues whose members pre-roll BEFORE the
                                   beat -- the 20 go copy fires at @ms, mwm-send
                                   generates the countdown itself for a
                                   go-variant (20-led) cue; --cascade-ms paces
                                   from @ms, --cascade-full forces the
                                   14-member FD chain for legacy F?-led cues).
                                   See the _parse_show_script docstring for
                                   the format.  Omit the file to pipe the
                                   script on stdin.
  hex <hex>                     arbitrary raw frame(s), '+'-joined; a missing
                                 trailing CRC-8 byte is auto-computed/appended
examples:
  simple left blue
  palette both 0x0C
  fused red green
  fused blue pal:0x19
  fused pal:0x04 red
  fused pal:0x04 pal:0x19
  effect 0x85
  demo 0x17
  clock 0x40
  grouppalette 0x00 0x18 0x11
  sys 05 06 04 01 00
  hex 91 61 6A 06
  hex 9C 20 24 0D 61 0E 88 58 F0 48 04 D0 42 06   (CRC auto-appended)
  incant pulse pal:0x01 blue
  incant pulse pal:0x01 blue reset=true
  incant pulse blue pal:0x04 reset=true   (open interchange probe, A6)
  incant strobe color=pal:0x04
  incant fade delay=0x20
  incant rotation green
  incant stop
  cue fade cycle=0x16
  sequence show.msh
  ... | mwm-send sequence    # or pipe the script on stdin
""".rstrip())


_HEX_RE = re.compile(r"^[0-9a-fA-F ]+$")


def _build_frames(cmd: str) -> list[bytes] | None:
    parts = cmd.split()
    verb = parts[0].lower()
    args = parts[1:]

    if verb == "simple":
        ear, color = args[0].lower(), args[1].lower()
        if color not in SIMPLE_COLOR_CODES:
            raise ValueError(f"unknown color '{color}'")
        if ear == "both":
            return [build_frame([SIMPLE_COLOR_CODES[color]])]
        if ear in ("left", "right"):
            return _simple(ear, color)
        raise ValueError("ear must be left|right|both")

    if verb == "palette":
        ear, idx = args[0].lower(), int(args[1], 0)
        if not 0 <= idx <= 0x1C:
            raise ValueError("palette index 0x00-0x1C")
        if ear == "both":
            return [build_frame([0x0E, idx])]
        if ear in ("left", "right"):
            return _palette(ear, idx)
        raise ValueError("ear must be left|right|both")

    if verb == "fused":
        return _fused(args[0].lower(), args[1].lower())

    if verb == "effect":
        return _effect(int(args[0], 0))

    if verb == "clock":
        return _clock_write(int(args[0], 0))

    if verb == "groupcolor":
        lo, hi, code = int(args[0], 0), int(args[1], 0), int(args[2], 0)
        return _group_color(lo, hi, code)

    if verb == "grouppalette":
        lo, hi, idx = int(args[0], 0), int(args[1], 0), int(args[2], 0)
        return _group_palette(lo, hi, idx)

    if verb == "sys":
        return _sys55(" ".join(args))

    if verb in ("cue", "cascade"):
        # Same grammar as 'incant', but expanded over the full countdown
        # chain (FD..F1 then 20) instead of a single delayed frame.
        if len(args) < 1:
            raise ValueError("cue <incant args>, e.g. cue fade cycle=0x16")
        return _cascade(" ".join(args))

    if verb == "incant":
        family = args[0].lower()
        rest = args[1:]
        opts: dict = {}
        lp = right = None
        if family == "pulse":
            # Each color may be a simple name or 'pal:0xNN'; the phrase is
            # composed per-slot so simple/palette can be interchanged for
            # rig testing (the corpus shapes put a palette LEFT + simple
            # RIGHT -- swapping encodings is an open probe, see A6).
            if len(rest) < 2:
                raise ValueError("incant pulse <left-color> <right-color> [opt=val]")
            opts = {}
            for tok in rest[2:]:
                k, _, v = tok.partition("=")
                if k == "reset":
                    opts[k] = v.lower() not in ("0", "false", "no", "off")
                else:
                    opts[k] = int(v, 0)
            return _incant_pulse(rest[0], rest[1], opts)
        elif family == "stop":
            if rest:
                raise ValueError("incant stop takes no arguments")
        elif family in ("strobe", "fade"):
            # optional k=v arguments, e.g. incant strobe timer=0x03
            for tok in rest:
                k, _, v = tok.partition("=")
                if k == "color":
                    # Accept a simple name or 'pal:0xNN' -- a palette byte in
                    # the strobe color slot is an open interchange probe (A6).
                    kind, val = _color_spec(v)
                    opts[k] = val if kind == "palette" else SIMPLE_COLOR_CODES[val]
                elif k == "reset":
                    opts[k] = v.lower() not in ("0", "false", "no", "off")
                else:
                    opts[k] = int(v, 0)
        elif family == "rotation":
            if len(rest) != 1:
                raise ValueError("incant rotation <color>")
            c = rest[0].lower()
            if c in SIMPLE_COLOR_CODES:
                right = SIMPLE_COLOR_CODES[c]
            elif c.startswith("pal:"):
                # Palette byte in the rotation base-color slot: open probe (A6).
                idx = int(c[4:], 0)
                if 0 <= idx <= 0x1C:
                    right = idx
                else:
                    raise ValueError(f"palette index out of range in '{rest[0]}'")
            else:
                raise ValueError(f"unknown simple color '{rest[0]}'")
        else:
            raise ValueError(f"unknown incantation '{family}'")
        return _incant(family, lp, right, opts)

    if verb == "hex":
        if not all(_HEX_RE.match(x) for x in args):
            raise ValueError("hex expects space-separated hex bytes")
        frames = parse_frame_hex(" ".join(args))
        return [frame_complete(f) or f for f in frames]

    return None  # unknown verb


# ---------------------------------------------------------------------------
# One-shot (non-interactive) path
# ---------------------------------------------------------------------------

def _one_shot(mqtt: dict, args: argparse.Namespace) -> None:
    if args.verb == "sequence":
        try:
            text = (sys.stdin.read() if not args.args
                    or args.args[0] == "-"
                    else Path(args.args[0]).read_text())
            beats = _parse_show_script(text, args.cascade_ms,
                                       args.cascade_full)
        except (ValueError, OSError) as exc:
            print(f"sequence: {exc}", file=sys.stderr)
            sys.exit(2)
        for _ in range(args.repeat):
            _run_sequence(mqtt, beats, args)
            if args.repeat > 1:
                time.sleep(args.repeat_delay)
        return
    verb = args.verb
    if verb == "demo" and not args.args:
        print("\n".join(_demo_catalog_lines()))
        sys.exit(0)
    argstr = " ".join(args.args)
    if verb == "demo":
        frames = _effect(int(args.args[0], 0))
    else:
        frames = _build_frames(f"{verb} {argstr}")
    if frames is None:
        print(f"unknown command '{verb}'", file=sys.stderr)
        sys.exit(2)
    payloads = [irsend_payload(f) for f in frames]
    reps = _cue_repeat(frames, args)
    print(f"sending {len(frames)} frame(s), {reps}x:")
    for f in frames:
        print(f"  {f.hex().upper():24s} -> {_frame_desc(f)}")
    try:
        for _ in range(reps):
            for p in payloads:
                send_payload(mqtt, p)
            if reps > 1 and args.repeat_delay > 0:
                time.sleep(args.repeat_delay)
    except RuntimeError as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.monitor:
        _monitor(mqtt, args.monitor, args.after)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _repeat_default(verb: str | None) -> int:
    """--repeat when the user did not pass it: classic verbs repeat 2x,
    but a timed ``sequence`` show already encodes its pacing, so it plays a
    single pass (replaying a multi-minute show back-to-back doubles every
    burst)."""
    return 1 if verb == "sequence" else 2


def _cue_repeat(frames: list[bytes], args: argparse.Namespace) -> int:
    """How many copies of this command's frames to send.

    A countdown cue already carries its own FEC: every member is a
    differently-timed copy of the same state change, so re-airing the
    chain transmits the same countdown values twice for no extra
    redundancy.  ``cue``/``cascade`` commands therefore play the chain
    once by default.  Lone classic frames keep the 2x repeat, and an
    explicit ``--repeat`` still wins.
    """
    if (not args.repeat_explicit and len(frames) > 1
            and cue_class(frames[0]) == "PRE_BUFFER_EVENT"):
        return 1
    return args.repeat


def main() -> None:
    p = argparse.ArgumentParser(description="MWM show-command sender")
    p.add_argument("--mqtt-json", default=str(_DEFAULT_MQTT),
                   help="MQTT config JSON path")
    p.add_argument("--repeat", type=int, default=None,
                   help="times to send each frame (default 2; a `sequence` "
                        "show runs a single pass unless --repeat is set, and "
                        "a `cue`/`cascade` countdown also sends its chain "
                        "once -- the members already are the FEC)")
    p.add_argument("--repeat-delay", type=float, default=0.3,
                   help="delay between repeats (default 0.3)")
    p.add_argument("--cascade-ms", type=float, default=None,
                   help="pace a cue's members this many ms apart FROM its @ms "
                        "target (default: countdown pre-roll -- member d fires "
                        "@ms - (d & 0x0F)*100 ms, the F? low-nibble delay, so "
                        "every ear that hears any member lands on the GO; "
                        "uniform pacing from @ms is the older airing style)")
    p.add_argument("--cascade-full", action="store_true",
                   help="expand a cue over the full 14-member capture chain "
                        "(FD..F1 then 20) -- already the default for a "
                        "20-led cue (the generator's go-variant form) and "
                        "for the menu 'cue' verb; this forces it even for "
                        "a legacy F?-led cue")
    p.add_argument("--min-gap-ms", type=float, default=_MIN_GAP_MS,
                   help="minimum spacing enforced between consecutive "
                        "publishes so same-tick clusters do not fire as one "
                        f"burst (default {_MIN_GAP_MS:g})")
    p.add_argument("--dry-run", action="store_true",
                   help="plan a sequence/show without transmitting")
    p.add_argument("--dump", action="store_true",
                   help="print the exact @ms HEX transmit stream a sequence "
                        "would send (every cue member at its pre-roll time or "
                        "--cascade-ms pacing, clamped to the min-gap floor, "
                        "plus the end-of-show reset unless --no-end-reset) "
                        "and exit -- for diffing the send stream against the "
                        "source capture")
    p.add_argument("--monitor", default=None,
                   help="receiver id to listen on after a send (e.g. 600605)")
    p.add_argument("--after", type=float, default=5.0,
                   help="seconds to listen after sending when monitoring")
    p.add_argument("--no-end-reset", action="store_true",
                   help="do not send the end-of-show reset (90 60 A6, both "
                        "ears off) after a `sequence` finishes (default: "
                        "reset, so ears never stay lit in the final state -- "
                        "the ears are remote and unobserved)")
    # One-shot form: mwm-send.py EFFECT 0x85  OR  mwm-send.py hex "91 61 6A"
    p.add_argument("verb", nargs="?", help="command verb (interactive if omitted)")
    p.add_argument("args", nargs="*", help="command arguments")
    args = p.parse_args()
    args.repeat_explicit = args.repeat is not None
    if args.repeat is None:
        args.repeat = _repeat_default(args.verb)

    mqtt = load_mqtt(Path(args.mqtt_json))
    if args.verb:
        _one_shot(mqtt, args)
    else:
        _menu(mqtt, args)


if __name__ == "__main__":
    main()
