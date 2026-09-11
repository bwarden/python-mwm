"""Decode raw IR timings (signed microseconds) into MWM frames.

Python port of web/src/lib/protocol/mwm.ts decodeMWM (itself mirroring
IRremoteESP8266's decodeMWM): 2400 bps UART over 38 kHz -- per byte a start
mark tick, eight data bits LSB-first with space=1, a stop space tick; up to
kMAX_WIDTH consecutive equal ticks merge into one measured run, +/- kDELTA
us tolerance. Frames are 3..18 bytes with the length rule (low nibble of
byte 0 == total - 3) or the 55 AA show-command signature.

Deviation from the TS port: captures containing several messages separated
by inter-message gaps are SPLIT and every complete frame is returned, since
wand pushes and our own repeated bursts arrive as one signal containing
multiple copies. The TS decoder concatenates across gaps and relies on the
strict length rule to reject the result.

Sources of truth:
    web/src/lib/protocol/mwm.ts       -- TypeScript decodeMWM(); this module
        mirrors its tick-merging, tolerance constants, and CRC validation.
    IRremoteESP8266 decodeMWM          -- Upstream C++ algorithm confirming
        the 2400 bps UART encoding, tick-merging (kMAX_WIDTH=9), and
        end-bit-swallowing recovery.
    docs/mwm-show-protocol.md          -- Frame length rule, CRC-8/Dallas,
        inter-message gap structure, and physical-layer timing measurements
        from the rig (TICK_US=417, DELTA_US tolerance, gap thresholds).
"""

from __future__ import annotations

from .protocol import crc8_dallas

TICK_US = 417
DELTA_US = 150
MAX_WIDTH_TICKS = 9
MAX_GAP_US = 20000
MIN_BITS = 24
MAX_BITS = 144


def _match_ticks(width_us: float) -> int:
    """Number of whole ticks covered by a measured width, 0 if none."""
    for ticks in range(MAX_WIDTH_TICKS, 0, -1):
        if ticks * TICK_US - DELTA_US <= width_us <= ticks * TICK_US + DELTA_US:
            return ticks
    return 0


def _is_inter_message_gap(width_us: float) -> bool:
    return width_us > MAX_GAP_US - DELTA_US or (
        width_us > MAX_WIDTH_TICKS * TICK_US + DELTA_US
    )


def _validate_frame(state: list[int]) -> bytes | None:
    """Apply the implied-length rules to decoded bytes."""
    bits = len(state) * 8
    if not MIN_BITS <= bits <= MAX_BITS or not state:
        return None
    header = state[0]
    if (header & 0xF0) == 0x90 or (header & 0xF0) == 0xF0:
        payload = header & 0x0F
        # (payload+2) bytes = end-bit-swallowed read; recovered below.
        if bits not in ((payload + 3) * 8, (payload + 2) * 8):
            return None
    elif header == 0x55 and len(state) > 1 and state[1] == 0xAA:
        pass  # show/system message: length carried by capture itself
    else:
        return None
    frame = bytes(state)
    if len(frame) >= 3 and frame[:2] == b"\x55\xaa":
        if sum(frame[2:-1]) % 256 != frame[-1]:
            return None
        return frame
    # End-bit swallowing (doc section 2): captures routinely lose the last
    # CONTENT byte -- its final zero bits scrunch into the stop bit and the
    # gap -- while the true CRC survives as the captured last byte. The
    # length nibble says one byte is missing; brute-force it against the
    # CRC. A random 256-search yields ~1 false positive on average, which
    # downstream consumers tolerate far better than losing every beacon.
    if (header & 0xF0) in (0x90, 0xF0):
        want_total = (header & 0x0F) + 3
        if len(frame) == want_total - 1:
            prefix, crc = bytes(state[:-1]), state[-1]
            for cand in range(256):
                if crc8_dallas(prefix + bytes([cand])) == crc:
                    return prefix + bytes([cand]) + bytes([crc])
            return None
    if frame[-1] != crc8_dallas(frame[:-1]):
        return None
    return frame



def decode_timings(timings: list[int]) -> list[bytes]:
    """Decode signed-us runs (positive mark, negative space) into frames.

    Accepts captures with or without a leading gap. Returns every complete,
    checksum-valid frame found; partial or corrupt messages are dropped.
    """
    # Normalise: drop leading/trailing spaces (leading gap, missing footer).
    runs = [(1 if t > 0 else -1, abs(int(t))) for t in timings]
    while runs and runs[0][0] < 0:
        runs.pop(0)

    frames: list[bytes] = []
    state: list[int] = []
    data = 0
    data_bits = 0
    frame_bits = 0

    def finalize() -> None:
        nonlocal state, data, data_bits, frame_bits
        if state:
            frame = _validate_frame(state)
            if frame is not None:
                frames.append(frame)
        state = []
        data = 0
        data_bits = 0
        frame_bits = 0

    def _backfill_tail() -> None:
        """Complete the in-progress byte with spaces (stop bit + trailing
        1-bits), mirroring the inter-message-gap handling below.

        Both a wide gap run and the clean end of a Tasmota capture swallow
        a message's final stop space (and any trailing 1-data-bits) into
        the footer, so the last byte can end mid-way with its remaining
        space-valued levels invisible -- back-fill them. `_validate_frame`
        keeps any forged bits out via the length rule and checksum.
        """
        nonlocal data, frame_bits
        while frame_bits % 10 != 0:
            if frame_bits % 10 == 9:  # stop bit
                state.append(data & 0xFF)
                data = 0
                frame_bits += 1
                break
            data >>= 1  # data bit, space = 1
            data |= 0x80
            frame_bits += 1

    for sign, width in runs:
        if sign < 0 and _is_inter_message_gap(width):
            # Trailing space-valued levels ride inside the ~30 ms
            # inter-message gap: both our encoder and IRremoteESP8266
            # captures merge a message's final 1-bits and stop spaces into
            # it. Back-fill the remainder of the current byte with spaces,
            # then treat the rest of the run as a separator. Forged bits
            # are kept out by the length rule and checksum validation.
            _backfill_tail()
            finalize()
            continue
        ticks = _match_ticks(width)
        if ticks == 0:
            finalize()
            continue
        level_is_space = sign < 0
        for _ in range(ticks):
            if frame_bits % 10 == 0:  # start bit
                if level_is_space:
                    # Mark expected; abandon current message.
                    finalize()
                    break
            elif frame_bits % 10 == 9:  # stop bit
                if not level_is_space:
                    finalize()
                    break
                state.append(data & 0xFF)
                data = 0
            else:  # data bit, LSB-first, space = 1
                data >>= 1
                if level_is_space:
                    data |= 0x80
                data_bits += 1
            frame_bits += 1
        else:
            continue

    # Clean end of capture: if the signal ends mid-byte, the final stop
    # space (and any trailing 1-bits) were swallowed by the omitted footer
    # rather than appearing as a gap run -- back-fill them the same way so
    # the trailing CRC/content byte is not dropped into the end-bit-swallow
    # recovery and mis-reconstructed.
    _backfill_tail()
    finalize()
    return frames
