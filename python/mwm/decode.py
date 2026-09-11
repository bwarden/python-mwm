"""Decode MWM show messages into human-readable descriptions.

Implements the instruction-set tables from docs/mwm-show-protocol.md section
4 plus the observed phrase templates (section 5): idle/demo beacons, static
color commands, effect invocations, timers/modifiers, group addressing, FEC
countdowns, and 55 AA system messages.

Sources of truth:
    docs/mwm-show-protocol.md section 4  -- Effects table (labels, indices)
        verified by Thread and Park experiments.
    docs/mwm-show-protocol.md section 5  -- Wand/paintbrush phrase templates
        (96 19 gg kk vv tt ww cc) with known (gg, kk) pairs and static
        color template layout.
    samples/mwm-gwts-colors.tsv          -- Verified frame examples
        confirming beacon structure and palette command forms.
    DIYC forum post #259750 (oPossum)     -- Original frame measurements,
        palette RGB values, and wand-command decoding.
    DIYC thread (jonfether, RobG, et al.) -- Simple color opcodes, beacon
        variants (park/live), companion block parsing.
"""

from __future__ import annotations

from .palette import EAR_STATE_OFF, PALETTE, SIMPLE_COLORS

# docs/mwm-show-protocol.md section 4, effects table ([T]hread/[P]ark
# verified entries only).
EFFECT_LABELS: dict[int, str] = {
    0x00: "random effect",
    0x01: "quick smooth fade out",
    0x02: "fade through current color to black",
    0x03: "slow even pulse",
    0x04: "pulse",
    0x08: "quick four-color rotation",
    0x0D: "color sequence",
    0x0F: "flashing sequence",
    0x10: "quick flashing on one ear",
    0x11: "color rotation",
    0x13: "color sequence",
    0x16: "color cycle with blue return",
    0x1A: "power-on blinks and fade",
    0x1F: "off",
    0x80: "power-on display (demo mode entry)",
    0x81: "power-off display sequence",
    0x82: "hard color transitions",
    0x83: "crossfading transitions",
    0x84: "strobe flashes into running program",
    0x85: "fade out",
    0x86: "fade up",
}

# Demo-mode index catalog: the `48 ss` values the idle/demo beacon reports
# as the program an ear has re-picked to run autonomously (docs §5 and
# samples/headband-20260822.log, 2026-08-22/23).  Distinct from the
# invokable `48 XX` effect catalog above: these are the indices actually
# observed CYCLING in live demo mode of their own accord, plus where each
# also has an invoked-effect name it shares it.  Observed cycling order in
# the headband stream (first appearance): 16 -> 18 -> 88 -> 14 -> 00 ->
# 15 -> 17; `48 88` dominates demo mode ([H] hat-observed), `48 17` also
# appears in park captures (`96 42 00 00 48 17 0C 40 2F`, [P]).  Entries
# only numbered (no name) are stored show programs with no verified label
# yet -- they are catalogued so a decoder can say "demo program x, on the
# hat" instead of inventing a name.
DEMO_BEACONS: dict[int, str] = {
    0x00: "random effect",
    0x14: "stored demo program 0x14 (unlabelled)",
    0x15: "stored demo program 0x15 (unlabelled)",
    0x16: "color cycle with blue return",
    0x17: "stored demo program 0x17 (unlabelled)",
    0x18: "stored demo program 0x18 (unlabelled)",
    0x88: "color sequence (dominates demo mode)",
}

# Curated effect catalog surfaced to Home Assistant (in the mwm_ears HA
# repo: light.py effect_list, the set_state action's effect selector):
# HA-facing labels mapped to the stored effect program indices.  A superset
# of EFFECT_LABELS -- the added demo-mode indices (0x00..0x1F plus 0x82..0x86)
# are rig-verified entry points into the built-in show programs
# (docs/mwm-show-protocol.md section 4, [T]hread/[P]ark verified).
LIGHT_EFFECTS: dict[str, int] = {
    "Fade out": 0x85,
    "Fade up": 0x86,
    "Single flash": 0x03,
    "Pulse": 0x04,
    "Strobe flash": 0x84,
    "Hard transitions": 0x82,
    "Crossfade transitions": 0x83,
    "Color rotation": 0x11,
    "Flashing sequence": 0x0F,
    "Quick four-color rotation": 0x08,
    "Random effect": 0x00,
    "Blackout": 0x1F,
}

# The 58 tt cycle-timer companion an effect program needs in the SAME
# phrase to run at all (docs/mwm-show-protocol.md section 4): ``48 04`` is
# the rig-verified pulse that "requires `58 F0`"; without the companion it
# degrades.  ``48 03`` (Single flash) deliberately has NO companion: a
# state-settling dig probe (2026-09-03) showed ``48 03 58 F0`` followed by a
# color is fatal (nothing runs), whereas plain ``48 03`` + color works.  So
# only ``0x04`` gets a companion.
EFFECT_COMPANION: dict[int, int] = {
    0x04: 0xF0,  # Pulse
}

# Full effect catalog for integration consumers: each HA-facing effect name
# maps to its stored program index plus the `58` companion byte it needs
# (None when no companion is required).  This is the single source of truth
# both services.yaml and the light platform's effect_list draw from, so the
# effect list and its framing requirements never live in the integration.
EFFECTS: dict[str, dict] = {
    name: {"index": index, "companion": EFFECT_COMPANION.get(index)}
    for name, index in LIGHT_EFFECTS.items()
}


def effect_label(index: int) -> str:
    """Human name for an invoked effect index, labeled or not."""
    if index in EFFECT_LABELS:
        return EFFECT_LABELS[index]
    if 0x87 <= index <= 0x8F:
        return f"color sequence {index:#04x}"
    return f"effect {index:#04x}"


def demo_beacon_label(index: int) -> str:
    """Human name for a demo-mode beacon's reported program index.

    Prefers the observed demo catalog (DEMO_BEACONS), which knows which
    indices truly cycle in demo mode; falls back to the invoked-effect
    naming when a beacon reports an index never seen cycling.
    """
    if index in DEMO_BEACONS:
        return DEMO_BEACONS[index]
    return effect_label(index)


def _color_name(simple_code: int) -> str:
    return SIMPLE_COLORS[simple_code][0] if simple_code in SIMPLE_COLORS else "off"


def _palette_desc(code: int) -> str:
    high = bool(code & 0x80)
    masked = code & 0x7F
    base = "off" if masked == 0x1D else (
        PALETTE[masked][0] if masked in PALETTE else f"palette[{masked:#04x}]"
    )
    return f"{base}{', per-ear register' if high else ''}"


def _time_cell(label: str, value: list[int]) -> str:
    """One timing field as a comparison-safe string, e.g. '58-0x20'."""
    hs = " ".join(f"{v:02X}" for v in value)
    return f"{label} {hs}"


def extract_timing_fields(content: list[int] | bytes) -> list[str]:
    """Timing-bearing opcodes in a phrase body, in order.

    These are the bytes that pace an effect's cycle (the ``58 tt`` /
    ``59 aa bb`` / ``5A a b c`` timers and the ``D0 mm yy`` modifiers).
    Two frames that run the SAME command at DIFFERENT points in the cycle
    differ exactly here (plus the beacon clock tick) -- so this is the
    machine-readable fingerprint for grouping siblings of one effect.
    Fields that appear more than once (stored in the list) preserve their
    order; callers that want identity use the list as-is.
    """
    body = list(content)
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        b = body[i]
        if b == 0x58 and i + 1 < n:
            out.append(_time_cell("58", body[i + 1 : i + 2]))
            i += 2
        elif b == 0x59 and i + 2 < n:
            out.append(_time_cell("59", body[i + 1 : i + 3]))
            i += 3
        elif b == 0x5A and i + 3 < n:
            out.append(_time_cell("5A", body[i + 1 : i + 4]))
            i += 4
        elif b == 0xD0 and i + 2 < n and body[i + 1] in (0x3D, 0x42):
            out.append(_time_cell("D0", body[i + 1 : i + 3]))
            i += 3
        else:
            i += 1
    return out


def extract_color_fields(content: list[int] | bytes) -> list[str]:
    """Color-bearing opcodes in a phrase body, in order.

    Simple ``6X`` / left-only ``6X`` and palette ``0E pp`` selectors.  Two
    frames that differ in color but share everything else are separate
    commands, so this (with timing) disambiguates "same hue, next phase"
    from "different color entirely".
    """
    body = list(content)
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        b = body[i]
        if 0x60 <= b <= 0x6F:
            out.append(f"color {b:02X}")
            i += 1
        elif b == 0x0E and i + 1 < n and (body[i + 1] & 0x7F) <= 0x1D:
            out.append(f"palette {body[i + 1] & 0x7F:02X}")
            i += 2
        else:
            i += 1
    return out


def _walk_tokens(content: list[int] | bytes) -> tuple[list[str], int | None]:
    """Tokenize a phrase body; returns (tokens, last_invoked_effect)."""
    tokens: list[str] = []
    effect: int | None = None
    i, n = 0, len(content)

    def push(text: str) -> None:
        tokens.append(text)

    while i < n:
        b = content[i]

        if b == 0x24:
            push("reset/override")
            i += 1
        elif b == 0x20:
            push("start immediately")
            i += 1
        elif b == 0x25:
            push("stop motion")
            i += 1
        elif b == 0x26:
            push("close group range")
            i += 1

        elif b in (0x60, 0x68):
            push("both ears off" if b == 0x60 else "left ear off")
            i += 1
        elif 0x61 <= b <= 0x67:
            push(f"both ears {_color_name(b)}")
            i += 1
        elif 0x69 <= b <= 0x6F:
            push(f"left ear {_color_name(0x60 + b - 0x68)}")
            i += 1

        elif b == 0x0E and i + 1 < n:
            push(f"palette color {_palette_desc(content[i + 1])}")
            i += 2

        elif b == 0x48 and i + 1 < n:
            effect = content[i + 1]
            push(f"invoke {effect_label(effect)}")
            i += 2

        elif b == 0x58 and i + 1 < n:
            push(f"cycle timer {content[i + 1]:#04x} (~100 ms/count)")
            i += 2
        elif b == 0x59 and i + 2 < n:
            push(f"timer 200 ms granularity ({content[i + 1]:#04x},{content[i + 2]:#04x})")
            i += 3
        elif b == 0x5A and i + 3 < n:
            a, bb, c = content[i + 1 : i + 4]
            extra = f", period={a * 400 / bb:.1f}s" if c == 0x55 else ""
            push(f"fractional period ({a:#04x},{bb:#04x},{c:#04x}{extra})")
            i += 4
        elif b == 0x5B and i + 4 < n:
            push("four-arg group-select timer")
            i += 5
        elif b == 0xD0 and i + 2 < n:
            push(f"D0 modifier ({content[i + 1]:#04x},{content[i + 2]:#04x})")
            i += 3
        elif b == 0xD1 and i + 3 < n:
            push(
                "D1 modifier "
                f"({content[i + 1]:#04x},{content[i + 2]:#04x},{content[i + 3]:#04x})"
            )
            i += 4
        elif b == 0xD2 and i + 4 < n:
            push(
                "D2 modifier ("
                f"{content[i + 1]:#04x},{content[i + 2]:#04x},"
                f"{content[i + 3]:#04x},{content[i + 4]:#04x})"
            )
            i += 5

        elif 0xF1 <= b <= 0xFF:
            push(f"delay ~{(b - 0xF0) * 100} ms")
            i += 1
        elif b == 0xF0:
            push("F0 pulse-effect modifier")
            i += 1
        elif 0xF2 <= b <= 0xFD:
            # Countdown copies FD..F1 prefix a retransmission run; F2 overlaps
            # the delay table, so classify by position instead -- treat as
            # countdown when seen among other FEC markers.
            push(f"FEC countdown copy {b:#04x}")
            i += 1

        elif b == 0x0C and i + 1 < n:
            push(f"clock tick {content[i + 1]:#04x}")
            i += 2

        elif b == 0xA0 and i + 1 < n:
            push(f"group range bound {content[i + 1]:#04x}")
            i += 2
        elif b in (0x81, 0x89, 0x8C):
            push("group picker")
            i += 1

        elif b == 0x42:
            # Device state/id block opening every observed beacon.
            push("device state block (beacon)")
            i += 1

        else:
            push(f"unknown {b:#04x}")
            i += 1

    return tokens, effect


def _is_beacon(content: list[int] | bytes) -> bool:
    """Idle/demo beacon: `42 00 00 48 ss 0C t [D0 0E ?]`.

    The 7-byte park variant lacks the D0 modifier clause; the 10-byte live
    variant carries it. Byte 6 (the clock tick) varies freely.
    """
    body = list(content)
    if len(body) == 7:
        return (
            body[:4] == [0x42, 0x00, 0x00, 0x48] and body[5] == 0x0C
        )
    if len(body) == 10:
        return (
            body[:4] == [0x42, 0x00, 0x00, 0x48]
            and body[5] == 0x0C
            and body[7] == 0xD0
            and body[8] == 0x0E
        )
    return False


# Wand/paintbrush push phrases: `96 19 gg kk vv tt ww cc` (+CRC). The
# (gg, kk) pair names the program; documented rows from the protocol
# reference (docs/mwm-show-protocol.md, Calvin 2014 table). Static
# color templates additionally carry a palette byte `pp`.
WAND_PROGRAMS = {
    (0x0B, 0x36): "both yellow, dim/bright alternating",
    (0x0B, 0x12): "both green, dim/bright",
    (0x0D, 0x3F): "fade up white, hold ~2 s, down",
    (0x0E, 0x31): "blue/yellow alternating ears",
    (0x0F, 0x39): "blue/white flashing sway",
    (0x10, 0x12): "both green pulsating",
    (0x10, 0x24): "pulsating red",
    (0x11, 0x1B): "light blue L/R/off cycle",
    (0x11, 0x12): "green L/R/off cycle",
}

# Static wand color templates: fixed leading (gg, kk, length). Body
# layout: `96 19 gg kk 16 pp scope ..` -- pp at index 5, scope byte at
# index 6 (even = both ears, odd = left-only).
_WAND_STATIC = {
    (0x07, 0x0F, 8),
    (0x0B, 0x09, 8),
    (0x0D, 0x2D, 8),
}


def _describe_wand(body: list[int]) -> dict:
    """Decode a `96 19 ...` wand/paintbrush phrase body."""
    gg, kk = body[2], body[3]
    desc: dict = {
        "kind": "wand-command",
        "group": gg,
        "program": kk,
        "palette": None,
        # Consumers (tracker, adoption) read these unconditionally on
        # every describe_content result.
        "tokens": [],
        "effect": None,
    }
    known = WAND_PROGRAMS.get((gg, kk))
    if (gg, kk, len(body)) in _WAND_STATIC:
        pp = body[5]
        scope = "left" if body[6] & 0x01 else "both"
        desc["palette"] = {"index": pp & 0x7F, "scope": scope}
        name = PALETTE.get(pp & 0x7F, ("unknown", None))[0]
        desc["summary"] = (
            f"wand static color: {name} ({scope} ears, shade {pp:#04x})"
        )
        return desc
    if known:
        desc["summary"] = f"wand program: {known}"
    else:
        desc["summary"] = (
            f"wand program {gg:02X}/{kk:02X} "
            "(undocumented; clock/variant bytes vary per push)"
        )
    return desc


def describe_content(content: list[int] | bytes) -> dict:
    """Describe a phrase body (frame bytes between header and CRC)."""
    body = list(content)
    if len(body) >= 2 and body[0] == 0x96 and body[1] == 0x19:
        return _describe_wand(body)
    if _is_beacon(body):
        clock_tick = body[6] if len(body) > 6 else None
        summary = (
            "idle beacon (demo effect running: "
            f"{demo_beacon_label(body[4])})"
        )
        if clock_tick is not None:
            summary += f" [clock={clock_tick:02X}]"
        return {
            "kind": "beacon",
            "summary": summary,
            "demo_effect": body[4],
            "clock_tick": clock_tick,
            # Timing/color fingerprints let a caller tell "same command, next
            # cycle phase" from genuinely different traffic: the beacon's
            # effect index stays while ONLY the clock tick and timing bytes
            # move across a cycle.
            "timing": extract_timing_fields(body),
            "colors": extract_color_fields(body),
            "tokens": [],
            "effect": None,
        }
    tokens, effect = _walk_tokens(body)
    kind = (
        "effect-command" if effect is not None
        else "color-command" if any("ear" in t for t in tokens)
        else "command"
    )
    return {
        "kind": kind,
        "summary": "; ".join(tokens),
        "tokens": tokens,
        "effect": effect,
        "timing": extract_timing_fields(body),
        "colors": extract_color_fields(body),
    }


def describe_55aa(data: bytes) -> dict:
    """Describe a 55 AA system message (full frame including checksum)."""
    payload = data[2:-1]
    games = {0: "demo cycle", 1: "blue solo", 2: "red memorisation",
             3: "yellow memorisation", 4: "laser tag"}
    if len(payload) >= 3 and payload[0] == 0x05 and payload[1] == 0x06:
        game = payload[2]
        name = games.get(game, f"game {game:#04x}")
        return {"kind": "55aa", "summary": f"interactive game: {name}"}
    if payload[:2] == b"\x08\xc4":
        return {"kind": "55aa", "summary": "ride shutdown command"}
    # Show timecodes (Jon Fether post 259733; confirmed against oPossum's
    # EMLG dumps, post 259752): a 9-byte content block whose final three
    # bytes are HH MM SS in plain decimal, e.g. ... 00 02 05 = 00h02m05s.
    # Fether's examples use an 0x19 length prefix, oPossum's 0x09; both are
    # the same layout. 0x00:00:00 is the start marker.
    if len(payload) == 9:
        hh, mm, ss = payload[-3], payload[-2], payload[-1]
        if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
            if hh == 0 and mm == 0 and ss == 0:
                summary = "show timecode start (00:00:00)"
            else:
                summary = f"show timecode {hh:02d}:{mm:02d}:{ss:02d}"
            return {"kind": "55aa", "summary": summary,
                    "timecode": (hh, mm, ss)}
    return {
        "kind": "55aa",
        "summary": "system broadcast (" + " ".join(
            f"{b:02X}" for b in payload[:8]
        ) + ("..." if len(payload) > 8 else "") + ")",
    }


def describe_bundle(frames: list[bytes]) -> dict | None:
    """Recognize an A-B-A' command bundle (doc section 3).

    Wands, paintbrushes and ears transmit every command three times:
    [phrase][companion][phrase]. First and third carry the same command;
    real hardware mutates a rolling counter near the phrase tail (re-CRCing
    the frame -- rig captures show e.g. ...A200D0 vs ...A2B300), so A and A'
    must match in length, header, and body while the final counter+CRC pair
    may differ. The middle message is a parameter block for the effect named
    by its embedded `48 XX` (cycle durations `58 tt`, scalers `D0 42 tt`,
    palette refs `0E xx`, clock writes `0C t`). Returns None otherwise.
    """
    if len(frames) != 3 or frames[0] == frames[1]:
        return None
    first, third = frames[0], frames[2]
    if len(first) != len(third) or first[0] != third[0]:
        return None
    # Long phrases may differ in the trailing counter+CRC pair (rolling
    # tick on real hardware); short ones have no counter, so they must
    # match outright.
    tail = -2 if len(first) >= 5 else None
    if first[:tail] != third[:tail]:
        return None
    phrase, companion = frames[0], frames[1]
    body = companion[1:-1]
    parts: list[str] = []
    i = 0
    while i < len(body) - 1:
        op = body[i]
        if op == 0x48:
            parts.append(f"effect: {effect_label(body[i + 1])}")
            i += 2
        elif op == 0x58:
            tt = body[i + 1]
            parts.append(
                "special cycle arg"
                if tt in (0xEE, 0xF0)
                else f"cycle ~{tt * 100} ms"
            )
            i += 2
        elif op == 0xD0 and i + 2 < len(body) and body[i + 1] == 0x42:
            parts.append(f"cycle scale {body[i + 2]} x 200 ms")
            i += 3
        elif op == 0x0C:
            parts.append(f"clock tick 0x{body[i + 1]:02X}")
            i += 2
        else:
            i += 1
    phrase_body = phrase[1:-1]
    return {
        "kind": "bundle",
        "phrase_hex": phrase.hex().upper(),
        "companion_hex": companion.hex().upper(),
        # Structured companion parameters, parallel to the rendered
        # "parameters" text so entities can expose machine-readable rows.
        "params": parts,
        # Timing/color fingerprints of the phrase itself: two bundles whose
        # EFFECT (indices+params) is identical but whose timing fields
        # differ are the SAME command pushed at different points in its
        # cycle -- the signal that teaches pacing, not a new command.
        "timing": extract_timing_fields(phrase_body),
        "colors": extract_color_fields(phrase_body),
        "summary": (
            f"A-B-A' bundle: {describe_frame(phrase)['summary']} "
            f"[parameters: {'; '.join(parts) if parts else 'opaque companion'}]"
        ),
    }


def describe_frame(frame: str | bytes) -> dict:
    """Validate and describe one complete MWM frame (hex or bytes)."""
    from .protocol import frame_is_valid

    ok, reason = frame_is_valid(frame)
    if isinstance(frame, str):
        packed = bytes.fromhex(
            frame.replace(" ", "").replace("0x", "").replace("0X", "")
        )
    else:
        packed = bytes(frame)
    if not ok:
        return {"kind": "invalid", "summary": f"invalid frame: {reason}",
                "tokens": [], "effect": None}
    if len(packed) >= 3 and packed[0] == 0x55 and packed[1] == 0xAA:
        return describe_55aa(packed)
    return describe_content(packed[1:-1])


class EarStateTracker:
    """Best-effort inference of what paired ears are currently doing.

    Feeds decoded frames; tracks assumed per-ear colors and the running
    effect so diagnostic surfaces can answer "what mode are the lights in?".
    Beacon frames update the assumed running effect but never the colors,
    since beacons describe autonomous demo behavior rather than commands.
    """

    def __init__(self) -> None:
        self._left: str = EAR_STATE_OFF
        self._right: str = EAR_STATE_OFF
        self._effect: str | None = None
        self._clock_tick: int | None = None
        self.last_summary: str = ""

    @property
    def left(self) -> str:
        return self._left

    @property
    def right(self) -> str:
        return self._right

    @property
    def effect(self) -> str | None:
        return self._effect

    @property
    def clock_tick(self) -> int | None:
        """Most recent clock tick from a beacon, or None."""
        return self._clock_tick

    def feed_frame(self, frame: str | bytes) -> dict:
        desc = describe_frame(frame)
        self.last_summary = desc["summary"]
        if desc["kind"] == "invalid":
            return desc
        if desc["kind"] == "beacon":
            self._effect = effect_label(desc["demo_effect"])
            if "clock_tick" in desc:
                self._clock_tick = desc["clock_tick"]
            return desc

        if desc["kind"] == "55aa":
            return desc

        packed = bytes.fromhex(
            frame.replace(" ", "").replace("0x", "").replace("0X", "")
        ) if isinstance(frame, str) else bytes(frame)
        content = packed[1:-1]

        # Two-opcode color script: read per-slot for display. NOTE rig
        # 2026-08-23: real ears execute these sequentially (last wins);
        # revisit if genuine traffic ever uses this shape.
        if len(content) == 2 and packed[0] & 0x0F == 1 and (
            0x60 <= content[0] <= 0x67 and 0x60 <= content[1] <= 0x67
        ):
            self._left = _color_name(content[1]) if content[1] != 0x60 else EAR_STATE_OFF
            self._right = _color_name(content[0]) if content[0] != 0x60 else EAR_STATE_OFF
            self._effect = None
            return desc

        # Short palette forms from samples/mwm-gwts-colors.tsv:
        # `0E pp` sets both ears, `0E pp|80` the left ear only.
        if (
            len(content) == 2 and content[0] == 0x0E
            and content[1] & 0x7F <= 0x1D
        ):
            pp = content[1] & 0x7F
            name = "off" if pp == 0x1D else (
                PALETTE.get(pp, ("unknown", None))[0]
            )
            if content[1] & 0x80:
                self._left = name
            else:
                self._left = self._right = name
            self._effect = None
            return desc

        # Long palette phrase template: 19 07 0F 16 pp 18 04 (park caps).
        if (
            len(content) == 7 and content[0] == 0x19 and content[4] & 0x7F <= 0x1D
            and content[5] == 0x18 and content[6] == 0x04
        ):
            pp = content[4] & 0x7F
            name = "off" if pp == 0x1D else (
                PALETTE.get(pp, ("unknown", None))[0]
            )
            if content[4] & 0x80:  # TSV left-only form (91 0E pp|80)
                self._left = name
            else:
                self._left = self._right = name
            self._effect = None
            return desc

        # Bare both-ears color or blackout phrases.
        if any(t.startswith("both ears") for t in desc["tokens"]) or (
            len(content) == 1 and content[0] in (0x24, 0x60)
        ):
            for tok in desc["tokens"]:
                if tok.startswith("both ears"):
                    color = tok.removeprefix("both ears ")
                    self._left = self._right = color
                    self._effect = None
                    break
            else:
                if content[0] in (0x24, 0x60):
                    self._left = self._right = EAR_STATE_OFF
                    self._effect = None

        # Left-only simple forms (`90 68..6F`, TSV color-X-left rows):
        # touch ONLY the left slot -- composing our own TX, we rely on
        # the right ear keeping its color through these.
        elif len(content) == 1 and 0x68 <= content[0] <= 0x6F:
            self._left = (
                EAR_STATE_OFF if content[0] == 0x68
                else _color_name(0x60 + content[0] - 0x68)
            )
            self._effect = None

        if desc["effect"] is not None:
            self._effect = effect_label(desc["effect"])

        return desc

    def snapshot(self) -> str:
        """One-line best-effort description of current ear state."""
        parts = []
        both_off = (
            self._left == EAR_STATE_OFF and self._right == EAR_STATE_OFF
        )
        if both_off and self._effect:
            # Beacons indicate the ears are actively running a demo effect;
            # the color slots are uninformative in this mode.
            parts.append(f"both ears active: {self._effect}")
        elif self._left == self._right:
            parts.append(f"both ears {self._left}")
        else:
            parts.append(f"left {self._left}")
            parts.append(f"right {self._right}")
        if self._effect and not both_off:
            parts.append(f"running: {self._effect}")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Show-cue taxonomy (docs/mwm-show-protocol.md section 3): how a show
# controller cue classifies operationally, so a show-script runner can treat
# a whole countdown block as ONE master event instead of the raw member
# fan-out.  The token names follow the Gemini-derived taxonomy (PRE_BUFFER /
# IMMEDIATE / GROUP_PICKER / AMBIENT); the frame-shape rules below are
# cross-checked against the corpus's documented opcode families.
# ---------------------------------------------------------------------------

CUE_PRE_BUFFER = "PRE_BUFFER_EVENT"
CUE_IMMEDIATE = "IMMEDIATE_EVENT"
CUE_GROUP_PICKER = "GROUP_PICKER_CUE"
CUE_AMBIENT = "AMBIENT_LOOP_BEAT"
CUE_OTHER = "OTHER"

_CUE_GROUP_HEADS = ((0x20, 0x89), (0x24, 0x0D))  # group picker / range bounds


def _has_ambient_timing(content: list[int]) -> bool:
    """True when the phrase carries the pulse/ambient timing clause -- the
    ``58 F0`` invoke followed by the ``48 04`` cycle open (pulse family;
    the strobe's ``48 84`` and the fade's bare ``58 tt`` do not match)."""
    for i in range(len(content) - 2):
        if content[i] == 0x58 and content[i + 1] == 0xF0:
            for j in range(i + 2, len(content) - 1):
                if content[j] == 0x48 and content[j + 1] == 0x04:
                    return True
    return False


def cue_class(frame: bytes) -> str:
    """Operational class of a show cue, per docs/mwm-show-protocol.md §3:

    PRE_BUFFER_EVENT    delay-led countdown member (F1..FD): a lookahead cue
                        that sets the crowd's absolute fire time at
                        receipt+delay.  A whole countdown run is ONE master
                        event -- the interpreter schedules the cue and the
                        members carry the target (collapse @ + 1300).
    IMMEDIATE_EVENT     the ``20`` go copy, or a bare ``48``/``24 48``
                        effect invoke: snaps the ears' state now.
    GROUP_PICKER_CUE    group-addressed phrase (``20 89 A0..26`` range
                        bounds or ``24 0D`` override): assigns a contiguous
                        ear range to a state while others stay put.
    AMBIENT_LOOP_BEAT   the fused pulse/ambient family (``58 F0 .. 48 04``
                        timing clause): a sustained per-ear loop.
    OTHER               static colors, palette shades, clock writes, 55AA.
    """
    if len(frame) < 3:
        return CUE_OTHER
    content = list(frame[1:-1])
    if not content:
        return CUE_OTHER
    if _has_ambient_timing(content):
        return CUE_AMBIENT
    if tuple(content[:2]) in _CUE_GROUP_HEADS:
        return CUE_GROUP_PICKER
    if content[0] in range(0xF1, 0xFE):
        return CUE_PRE_BUFFER
    if content[0] == 0x20:
        return CUE_IMMEDIATE
    if content[0] == 0x48 or (
            content[0] == 0x24 and len(content) > 1 and content[1] == 0x48):
        return CUE_IMMEDIATE
    return CUE_OTHER
