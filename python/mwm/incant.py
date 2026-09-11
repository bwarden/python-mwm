"""Verified show-command incantations mined from the real captures.

PURPOSE
-------
The park / hat / wand corpus (samples/MRDF*, EMLG*, headband, measured by
tools/build_corpus.py + tools/analyze_shape.py) shows that real MWM show
controllers do NOT send a bare effect and a bare color.  They send single,
*fused* phrases: close-group + per-ear color(s) + cycle timer + effect
invocation + D0/D1 pacing modifiers, all in one frame.  This module encodes
the specific frame shapes that appear in the corpus so both the rig tool
(tools/mwm-send.py) and the Home Assistant integration can emit commands
built in the same verified shape instead of the naive `48 XX` + separate
color frame.

Each builder documents the corpus frame(s) it was derived from (hex + count
+ what describe_frame says).  Colors are supplied as protocol codes
(simple 0x61..0x67 via mwm.SIMPLE_COLOR_CODES, palette index 0x00..0x1C via
mwm.PALETTE).  Per-ear forms use the per-ear register bit (0x80 on the
palette byte) exactly as the corpus does -- left ear only; there is no
right-only single form in the wild corpus.

Requires: nothing beyond the protocol module this package already imports.
"""

from __future__ import annotations

from .protocol import EAR_OFF_CODE, LEFT_ONLY_BASE, build_frame

# The D0 (0x45, 0x83) clause that closes the park's per-color effect phrases.
D0_COLOR_EFFECT = 0xD0, 0x45, 0x83

# The park's countdown cascade: the SAME phrase re-sent with the leading
# "delay" byte stepped down FD->F1, then the immediate (0x20) go copy.  Ears
# each execute their last-heard member at t = receipt + delay, so every ear
# in range fires the cue simultaneously -- the show's fundamental beat
# primitive (EMLG000E 139086-140484 strobe stand, 111950-112853 fade-out).
CASCADE_DELAYS = [*range(0xFD, 0xF0, -1), 0x20]  # FD..F1 then 20 (immediate)


def build_pulse(
    left_pp: int,
    right_simple: int,
    *,
    both_first: bool = False,
    cycle_mod: int | None = None,
    reset: bool = False,
) -> bytes:
    """Fused pulse phrase: per-ear palette + simple other ear + ``58 F0`` +
    ``48 04`` + closing ``D0 45 83``.

    ``left_pp`` is the palette index for the *left* ear (per-ear register
    bit ORed in), ``right_simple`` the simple-color code for the right ear
    (0x61..0x67).  ``both_first`` selects the other observed op ordering:
    simple-both first then per-ear palette.  ``cycle_mod`` adds the
    ``D0 42 tt`` cycle-rate clause between the invoke and the close.

    ``reset`` selects the override form (``20 24 0D`` prefix, no
    ``D0 45 83`` close) that show controllers use to RE-COLOR an already
    running pulse -- a plain fused phrase is ignored while the program
    runs.  The override phrase's order is fixed (simple right, then per-ear
    palette) and its cycle clause defaults to 0x06.

    Derived from (real park frames):
      * ``9B 96 26 0E 81 61 58 F0 48 04 D0 45 83 23`` (53x) -- palette sky
        blue, both blue, pulse.  ``both_first=False``, prefix 0x96.
      * ``9E 94 26 64 0E 8D 58 F0 48 04 D0 42 16 D0 45 83 C6`` (11x) -- both
        red, left rose pink, pulse at ``D0 42 16``.  ``both_first=True``,
        prefix 0x94.
      * ``9C 20 24 0D 61 0E 88 58 F0 48 04 D0 42 06 70`` (4x) --
        reset=true: both blue, left palette purple, cycle 0x06.
    """
    if reset:
        return build_frame([
            0x20, 0x24, 0x0D,
            right_simple,                  # both solid
            0x0E, left_pp | 0x80,          # per-ear palette register (left)
            0x58, 0xF0,                    # ~100 ms cycle timer (pulse needs 58 F0)
            0x48, 0x04,                    # invoke Pulse
            0xD0, 0x42, cycle_mod if cycle_mod is not None else 0x06,
        ])
    if both_first:
        content = [
            0x94, 0x26,
            right_simple,                 # both solid
            0x0E, left_pp | 0x80,         # per-ear palette register (left)
            0x58, 0xF0,                   # ~100 ms cycle timer (pulse needs 58 F0)
            0x48, 0x04,                   # invoke Pulse
        ]
    else:
        content = [
            0x96, 0x26,
            0x0E, left_pp | 0x80,         # per-ear palette register (left)
            right_simple,                 # both / other-ear solid
            0x58, 0xF0,
            0x48, 0x04,
        ]
    if cycle_mod is not None:
        content += [0xD0, 0x42, cycle_mod]
    content += list(D0_COLOR_EFFECT)
    return build_frame(content)


def build_off() -> bytes:
    """Stop everything: ``48 1F`` invoke off.

    Ends a running program and turns the ears off.  The show's other stop is
    a bare reset ping ``91 F? 24`` (both ears black immediately).

    Derived from:
      * ``91 48 1F B2`` (invoke off) -- the catalogue's explicit stop.
    """
    return build_frame([0x48, 0x1F])


def build_strobe(
    color: int = 0x67,
    timer: int = 0x02,
    *,
    delay: int | None = 0xF1,
    reset: bool = True,
    palette: int | None = None,
) -> bytes:
    """Fused strobe phrase: ``[delay] [reset] <color> ``58`` timer ``48 84``.

    “Strobe flashes into running program” is issued against a standing
    color, so the color rides inside the phrase (0x67 white in the corpus).
    ``delay`` is a ``F1``-``FF`` countdown byte or ``None`` to omit;
    ``reset`` toggles the leading ``24`` override.  ``palette`` switches
    the color slot to the two-byte ``0E pp`` form instead of a single
    simple byte (A6 shape -- the other effect slots accept it, so the
    strobe slot is probed the same way).

    Derived from:
      * ``96 F1 24 67 58 02 48 84 8D`` (2x) -- delay 100 ms + reset, white,
        strobe.
      * ``95 20 67 58 01 48 84 3C`` (5x) -- immediate, white, timer 01,
        strobe (no reset).
    """
    content: list[int] = []
    if delay is not None:
        content.append(delay)
    if reset:
        content.append(0x24)
    if palette is not None:
        content += [0x0E, palette]
    else:
        content.append(color)
    content += [0x58, timer, 0x48, 0x84]
    return build_frame(content)


def build_fade(cycle: int = 0x05, delay: int = 0xF2) -> bytes:
    """Fade-out countdown phrase: ``delay ``48 85`` ``58`` cycle``.

    The park workhorse (1375 occurrences across the corpus): ``F?`` delay
    then ``48 85`` fade-out stretched by ``58 tt``.  Delay 0x20 = immediate
    copy, which is the final FEC countdown member.

    Derived from:
      * ``94 F2 48 85 58 05 DD`` (16x) -- 200 ms delay, cycle 0x05.
      * ``94 20 48 85 58 05 00`` (15x) -- immediate copy, cycle 0x05.
    """
    return build_frame([delay, 0x48, 0x85, 0x58, cycle])


def build_cascade(tail: list[int], delays: list[int] | None = None) -> list[bytes]:
    """Emit a park countdown cascade: one frame per ``delays`` member.

    ``tail`` is the phrase content AFTER the leading delay byte (for a fade:
    ``[0x48, 0x85, 0x58, cycle]``; for a strobe:
    ``[0x24, 0x67, 0x58, timer, 0x48, 0x84]``).  Each returned frame is the
    same phrase with the delay byte set to one member of ``delays``
    (default ``CASCADE_DELAYS`` = FD..F1 then the immediate ``20`` go copy).

    Ears run the cue at receipt+timer on their last-heard member, so the
    whole cascade collapses onto one simultaneous beat for every ear in
    range -- the park's fundamental show primitive.  A build_* that already
    takes ``delay=`` (e.g. ``build_fade``) can be cascaded the same way by
    passing a list of ``delays``.

    Derived from:
      * ``96 FD..F1 24 67 58 19 48 84`` -- EMLEG000E strobe stand (139086)
      * ``94 FD..F1 48 85 58 64``     -- EMLEG000E fade-out cascade (111950)
    """
    if delays is None:
        delays = list(CASCADE_DELAYS)
    if not delays:
        raise ValueError("cascade requires at least one delay byte")
    return [build_frame([d, *tail]) for d in delays]


def build_sparse_cascade(
    tail: list[int],
    interval_ms: int = 400,
    target_ms: int = 1300,
) -> list[bytes]:
    """Sparse countdown cascade: the same phrase, sent less often.

    The full 14-member chain (``build_cascade``) is a byte-for-byte
    reproduction of what the park rig broadcast, kept for faithful capture
    replay.  But when *we* generate the countdown there is no need to
    re-send the phrase on the ~110 ms capture cadence: ears run their
    last-heard member at t = receipt + delay, and every member of the chain
    collapses onto one beat (FD = 1300 ms after the first member).  So one
    member every ``interval_ms`` suffices, each with the delay byte that
    keeps every member firing on the same ``target_ms`` beat, closed by the
    immediate 20 go copy::

        interval 400 ms -> FD F9 F5 F1 20   (5 frames, ~3 sends/s)
        interval 500 ms -> FD F8 F3 20      (4 frames, ~2.7 sends/s)

    Every member is one of the exact verified delay phrases from
    ``CASCADE_DELAYS``, so the collapse is identical to the full chain's --
    at roughly a third of the on-air traffic and with the transmitter quiet
    between members.
    """
    delays: list[int] = []
    t = 0
    while t < target_ms:
        nib = round((target_ms - t) / 100)
        delays.append(0xF0 | max(1, min(13, nib)))
        t += interval_ms
    delays.append(0x20)
    return [build_frame([d, *tail]) for d in delays]


def rotation_phrase(
    simple: int,
    cycle_mod: int = 0x01,
    *,
    left: int | None = None,
    palette: int | None = None,
) -> bytes:
    """Color-rotation set piece: reset + ``48 11`` + ``D0 3D tt`` +
    colors + fade tail.

    The park's set piece ALTERNATES the ears in the base color (not a
    sweep of colors): ``D0 3D tt`` paces the alternations, and once the
    ``FA 48 85`` fade tail fires the piece ends ~1 s after arming.  A
    larger ``tt`` spaces the flashes WIDER, so fewer fit before the fade
    (observed: green tt=01 flashes Lx2/Rx1 then fades; tt=04 flashes once;
    tt=20 shows nothing).  ``left`` overrides the left-ear code alone;
    ``palette`` switches BOTH slots to the two-byte ``0E pp`` form.

    Derived from (real park frames):
      * ``9B F1 24 48 11 D0 3D 01 62 6A FA 48 85 1C`` (2x) -- both green /
        left green, rotation, fade out at ~1000 ms.
      * ``9D FD 24 48 11 D0 3D 01 0E 0D 0E 8D FC 48 85 16`` (11x) -- both
        rose-pink/Palette 0x0D, rotation, fade at ~FC countdown.
    """
    if palette is not None:
        # Corpus 0E-form: leading delay FD, slots 0E pp / 0E (pp|80),
        # fade prefix FC (the exact rose-pink set-piece, only pp varies).
        return build_frame([
            0xFD, 0x24,
            0x48, 0x11, 0xD0, 0x3D, cycle_mod,
            0x0E, palette, 0x0E, palette | 0x80,
            0xFC, 0x48, 0x85,
        ])
    left_code = left if left is not None else (
        LEFT_ONLY_BASE + simple - EAR_OFF_CODE)
    return build_frame([
        0xF1, 0x24,
        0x48, 0x11, 0xD0, 0x3D, cycle_mod,
        simple, left_code,
        0xFA, 0x48, 0x85,
    ])