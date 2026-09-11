"""MWM show-message framing and IR signal encoding.

Frame layout (docs/mwm-show-protocol.md section 2):

    +--------+---------------------------+--------+
    |  0x9L  | content (L+1 bytes)       | crc8   |
    +--------+---------------------------+--------+
    total length = L + 3 bytes

CRC-8/Dallas (poly 0x8C reflected, init 0).  ``55 AA`` system messages use an
additive checksum over the payload (bytes after AA) mod 256 instead.

Signal: 2400 bps UART/IRDA-SIR over a 38 kHz carrier -- per byte a start mark
tick, eight data bits LSB-first with space=1, a stop space tick; equal adjacent
levels merge into single runs; the message ends with the ~30 ms inter-command
gap.  This mirrors web/src/lib/protocol/mwm.ts toPronto().

Sources of truth:
    docs/mwm-show-protocol.md         -- Frame layout, length rule, CRC-8,
        55 AA additive checksum, and timing constants (TICK_US,
        FOOTER_GAP_US, CARRIER_HZ); mark/space widths from the rig.
    web/src/lib/protocol/mwm.ts       -- TypeScript reference for toPronto()
        and timings_for_frame(); this module mirrors its tick-merging and
        sign convention.
    IRremoteESP8266 (decodeMWM)        -- Upstream algorithm confirming
        2400 bps UART encoding and carrier frequency.
"""

from __future__ import annotations

import re

TICK_US = 417
FOOTER_GAP_US = 30000
CARRIER_HZ = 38000

MAX_CONTENT = 16

# Framing / state opcodes surfaced for integration consumers (docs/
# mwm-show-protocol.md section 4).  These are the canonical forms the HA
# integration (and CLI tools) encode against; keeping them in the library
# ensures a consumer never re-derives protocol values.
EAR_OFF_CODE = 0x60      # simple-color "off" opcode (both ears)
LEFT_ONLY_BASE = 0x68   # left-ear-only simple-color base (0x68 + code)
DEFAULT_COLOR_CODE = 0x67  # canonical default ear color (white)
RESET_OPCODE = 0x24      # flow-control reset/override


def crc8_dallas(data: bytes | list[int] | tuple[int, ...]) -> int:
    """CRC-8/Dallas (polynomial 0x8C reflected, init 0x00)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc & 0xFF


def build_frame(content: list[int]) -> bytes:
    """Build one show message from its content bytes (header + CRC added)."""
    n = len(content)
    if not 1 <= n <= MAX_CONTENT:
        raise ValueError(f"content must be 1..{MAX_CONTENT} bytes, got {n}")
    frame = [0x90 | (n - 1), *content]
    frame.append(crc8_dallas(frame))
    return bytes(frame)


def build_55aa(payload: list[int], prefix: list[int] | None = None) -> bytes:
    """Build a 55 AA system message with the additive payload checksum."""
    cs = sum(payload) % 256
    return bytes([0x55, 0xAA, *payload, cs])


def _normalize(hex_or_bytes: str | bytes) -> bytes:
    if isinstance(hex_or_bytes, bytes):
        return hex_or_bytes
    packed = hex_or_bytes.replace(" ", "").replace("0x", "").replace("0X", "")
    if len(packed) % 2:
        raise ValueError("odd number of hex digits")
    try:
        return bytes.fromhex(packed)
    except ValueError as err:
        raise ValueError(f"non-hex input: {hex_or_bytes!r}") from err


def frame_complete(hex_or_bytes: str | bytes) -> bytes | None:
    """Return `hex_or_bytes` completed to a full valid frame, or None.

    Appends the missing CRC-8 when the input is exactly one byte short of
    the length rule (low nibble of byte 0 == total - 3), i.e. a hand-typed
    frame that dropped its trailing checksum.  A frame that already has its
    CRC is returned unchanged; anything else (bad length, non-0x9x header,
    system message) returns None.
    """
    try:
        data = _normalize(hex_or_bytes)
    except ValueError:
        return None
    if not data or (data[0] & 0xF0) != 0x90:
        return None
    length_nibble = data[0] & 0x0F
    if length_nibble == len(data) - 2:
        return data + bytes([crc8_dallas(data)])
    if length_nibble == len(data) - 3:
        return data
    return None


def frame_is_valid(
    hex_or_bytes: str | bytes, *, auto_crc: bool = False
) -> tuple[bool, str]:
    """Validate framing; returns (ok, reason).

    Accepts spaced or packed hex. Show messages must satisfy the length rule
    (low nibble of byte 0 == total - 3) and CRC-8/Dallas. 55 AA system
    messages validate via the additive payload checksum.

    With ``auto_crc=True``, a frame that is exactly one byte short of the
    length rule is completed by computing and appending the missing CRC-8
    before validating -- hand-typed hex from the docs often drops the
    trailing checksum, so this lets ``hex 9C 20 24 0D … D0 42 06`` validate
    as if the full ``…06 70`` frame were provided.
    """
    try:
        data = _normalize(hex_or_bytes)
    except ValueError as err:
        return False, str(err)
    if not data:
        return False, "empty"
    if len(data) >= 3 and data[0] == 0x55 and data[1] == 0xAA:
        want = sum(data[2:-1]) % 256
        if data[-1] != want:
            return False, f"additive checksum mismatch: got {data[-1]:02X} want {want:02X}"
        return True, ""
    if auto_crc:
        completed = frame_complete(data)
        if completed is None:
            completed = data
        data = completed
    hdr = data[0]
    if (hdr & 0xF0) != 0x90:
        return False, f"header {hdr:02X} is not 0x9x"
    length_nibble = hdr & 0x0F
    if length_nibble != len(data) - 3:
        return False, (
            f"length rule violated: L={length_nibble} but total={len(data)} "
            f"(frame is header 0x9L + content (L+1) + CRC-8, so total must be "
            f"{length_nibble}+3={length_nibble + 3})"
        )
    want = crc8_dallas(data[:-1])
    if data[-1] != want:
        return False, f"CRC mismatch: got {data[-1]:02X} want {want:02X}"
    return True, ""


def timings_for_frame(frame: bytes) -> list[int]:
    """Absolute-duration burst sequence (alternating mark/space, us).

    Starts with a mark, ends with the inter-command gap space.
    """
    flat: list[int] = []
    for byte in frame:
        flat.append(TICK_US)  # start bit: mark
        for i in range(8):
            bit = (byte >> i) & 1
            flat.append(-TICK_US if bit else TICK_US)  # space = 1
        flat.append(-TICK_US)  # stop bit: space
    flat.append(-FOOTER_GAP_US)

    merged: list[int] = []
    for value in flat:
        if merged and (merged[-1] < 0) == (value < 0):
            merged[-1] += value
        else:
            merged.append(value)
    return [abs(v) for v in merged]


def irsend_payload(frame: bytes) -> str:
    """Tasmota IRsend raw command payload for transmitting this frame."""
    timings = timings_for_frame(frame)
    return ",".join([str(CARRIER_HZ), *(str(t) for t in timings)])


def raw_timings(frame: bytes) -> list[int]:
    """Signed-microsecond runs in the HA infrared framework convention.

    Positive values are marks (carrier on), negative values spaces,
    strictly alternating, ending with the inter-command gap space. This is
    the format expected by InfraredCommand.get_raw_timings().
    """
    return [
        value if index % 2 == 0 else -value
        for index, value in enumerate(timings_for_frame(frame))
    ]


def parse_frame_hex(text: str) -> list[bytes]:
    """Split a '+'-joined hex sequence into individual frames."""
    frames = []
    for part in text.split("+"):
        packed = part.strip().replace(" ", "").replace("0x", "").replace("0X", "")
        if packed:
            frames.append(bytes.fromhex(packed))
    return frames


# ---------------------------------------------------------------------------
# Tasmota RawData parser (compact letter-coded timings)
# ---------------------------------------------------------------------------

def _tasmota_signed(text: str) -> list[int]:
    """Decode Tasmota RawData into signed-microsecond timings.

    Accepts compact letter-coded form (``+9185-4490+650...jH``), comma form,
    or the letter-coded equivalent. The compact encoding assigns the letters
    A-Z to the first 26 distinct timing magnitudes in order of first
    appearance; a repeated value is written as that letter, uppercase for a
    mark (signal HIGH/positive) and lowercase for a space (LOW/negative).
    Magnitudes are multiples of 5 us. Explicit ``+/-N`` tokens are always
    written out numerically.

    Port of Protocol::IR::Format::Tasmota::_decode_compact
    (perl/lib/Protocol/IR/Format/Tasmota.pm), which mirrors the encoder used
    by Tasmota's ``RawData`` field.

    Raises ValueError on undefined letters or empty input.
    """
    tokens = re.findall(r"([+\-]\d+|[A-Za-z])", text)
    if not tokens:
        raise ValueError("Tasmota compact format contains no timing data")

    values: list[int] = []
    rev: dict[str, int] = {}
    count = 0
    for tok in tokens:
        m = re.match(r"^([+\-])(\d+)$", tok)
        if m:
            mag = int(m.group(2))
            if mag not in rev and count < 26:
                rev[chr(ord("A") + count)] = mag
                count += 1
            sign = 1 if m.group(1) == "+" else -1
            values.append(sign * mag)
        else:
            key = tok.upper()
            if key not in rev:
                raise ValueError(
                    f"Tasmota compact format references undefined timing letter '{tok}'"
                )
            mag = rev[key]
            sign = 1 if tok.isupper() else -1
            values.append(sign * mag)
    if not values:
        raise ValueError("Tasmota compact format contains no timing data")
    return values


def tasmota_timings(rawdata: str) -> list[int]:
    """Convert a Tasmota IR timing field to signed-microsecond runs.

    Handles compact letter-coded, comma-separated, and ``IRsend <freq>,...``
    forms. Numbers are assigned positive for marks (even index) and negative
    for spaces (odd index), matching the IR framework's alternate-sign
    convention consumed by decode_timings.

    The two inputs have different carriers:
    - ``RawData``/receive payloads come from the IR receiver IC, which has
      already converted 38 kHz bursts into simple mark/space durations, so
      they carry NO leading frequency.
    - ``IRsend <freq>,<timings>`` transmit commands drive the ESP8266 PWM to
      an actual IR LED, so the leading ``<freq>`` is required (``0`` = the
      default 38 kHz). It is stripped here.
    """
    text = rawdata.strip()
    text = re.sub(r"^IRsend\s+\d+,?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^RawData\s*[=:]\s*\"?", "", text, flags=re.IGNORECASE)
    text = text.strip().strip('"')
    if not text:
        raise ValueError("No Tasmota RawData provided")
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        return [
            (1 if i % 2 == 0 else -1) * int(p)
            for i, p in enumerate(parts)
        ]
    return _tasmota_signed(text)


# ---------------------------------------------------------------------------
# Synchronization primitives
# ---------------------------------------------------------------------------

def build_clock_write(tick: int) -> bytes:
    """Build a clock-sync frame: 91 0C <tick> <crc>.

    Writes tick value to the sync clock register on all ears in range.
    Tick is an 8-bit value (0x00-0xFF). The ears use this to align
    playback phase -- repeating a wand button changes only the tick,
    and idle beacons update it constantly.

    docs/mwm-show-protocol.md section 3 (Clock-sync field).
    """
    return build_frame([0x0C, tick & 0xFF])


def build_group_color(
    group_start: int,
    group_end: int,
    color_code: int,
) -> bytes:
    """Build a group-addressed color command.

    Frames ears in the range [group_start, group_end] with the given
    simple color code (0x60-0x67). Each ear picks a random group id
    00-7F at power-up; this targets a contiguous slice.

    Format: 97 20 89 A0 <end> 26 <color> F2 <crc_hi> <crc_lo>
    (docs/mwm-show-protocol.md section 4, Group addressing).

    group_start is implicit (0x00 for the first group phrase, or the
    previous group_end+1 for subsequent phrases).
    """
    content = [
        0x20,           # group phrase header
        0x89,           # group picker (first group uses 89)
        0xA0, group_end & 0x7F,  # range bounds
        0x26,           # range close
        color_code & 0xFF,
    ]
    return build_frame(content)


def build_group_palette(
    group_start: int,
    group_end: int,
    palette_index: int,
) -> bytes:
    """Build a group-addressed palette color command.

    Like build_group_color but uses the mixed palette (0x0E XX).
    palette_index is 0x00-0x1D.
    """
    content = [
        0x20,           # group phrase header
        0x81,           # palette group picker
        0xA0, group_end & 0x7F,
        0x26,           # range close
        0x0E, palette_index & 0x7F,
    ]
    return build_frame(content)


def decode_beacon_clock(frame: bytes) -> int | None:
    """Extract the clock tick from a beacon frame.

    Returns the 8-bit tick value, or None if the frame is not a valid
    beacon. The tick is at content[5] (byte 6 of the content body,
    after the 0x99 header).
    """
    if len(frame) < 2:
        return None
    header = frame[0]
    if (header & 0xF0) != 0x90:
        return None
    content = list(frame[1:-1])  # strip header and CRC
    if len(content) < 7:
        return None
    # Beacon structure: 42 00 00 48 ss 0C t [D0 0E ??]
    if content[:3] != [0x42, 0x00, 0x00]:
        return None
    if content[5] != 0x0C:
        return None
    return content[6]
