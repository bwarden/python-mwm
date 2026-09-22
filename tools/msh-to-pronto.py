#!/usr/bin/env python3
"""Convert a show-script (.msh) into a list of Pronto hex strings.

Each beat of the script is expanded to its on-air MWM frame(s) exactly as
tools/mwm-send.py would send them (raw ``hex`` frames with the optional
CRC auto-complete, countdown cascades over the lead delay byte's own
member set), then each frame is emitted as one Pronto hex string -- the
``0000 006D ...`` 38 kHz form the WIG/GC/IRDB importers and the
web/perl Pronto decoders consume, for testing decoders against the frames
the rig actually broadcasts.

The pulse counts mirror perl/lib/Protocol/IR/Proto/MWM.pm's to_pronto
(and the TS mwm port): 2400 bps serial over 38 kHz, 417 us tick, ~30 ms
footer, mark/space runs merged then quantized to frequency-word periods
(freq word 0x006D = 109).

Usage:
    python3 tools/msh-to-pronto.py samples/park-cascade-demo.msh
    python3 tools/msh-to-pronto.py --raw samples/park-EMLG000E_filtered.txt.show1.msh | sort -u > frames.txt
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
from pathlib import Path

from _bootstrap import mwm

# Piping --raw into head/tee must not raise a BrokenPipeError traceback.
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

# Pronto 0000-format quantization, mirroring the Perl MWM port.
CARRIER_HZ = 38000
PERIOD_DIV = 0.241246  # us per frequency-word unit

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_HEX_BYTE_RE = re.compile(r"^[0-9a-fA-F]{2}$")


def _freq_word() -> int:
    return round(1_000_000.0 / (CARRIER_HZ * PERIOD_DIV))


def frame_to_pronto(frame: bytes) -> str:
    """Encode one on-air MWM frame as a 0000-format Pronto hex string."""
    freq_word = _freq_word()
    period_us = freq_word * PERIOD_DIV

    timings = mwm.timings_for_frame(frame)  # abs alternating mark/space, us
    n_pairs = len(timings) // 2
    pulses = [f"{round(us / period_us):04X}" for us in timings]
    tokens = ["0000", f"{freq_word:04X}", f"{n_pairs:04X}", "0000", *pulses]
    return " ".join(tokens)


def _content_and_tail(argstr: str) -> tuple[int, list[int]]:
    """Return (lead byte, tail bytes) for a cascade/cue spec."""
    if argstr.startswith("hex "):
        phrase = argstr[len("hex "):].split()
        if not all(_HEX_BYTE_RE.match(x) for x in phrase):
            raise ValueError(f"cascade/cue hex expects space-separated "
                             f"two-digit bytes, got {argstr!r}")
        content = [int(x, 16) for x in phrase]
    else:
        raise ValueError(f"cascade/cue only supports the raw 'hex <bytes>' "
                         f"form here: {argstr!r}")
    if not content or content[0] not in (*range(0x20, 0x21), *range(0xF0, 0x100)):
        raise ValueError(f"cascade phrase does not lead with a delay byte: "
                         f"{argstr!r}")
    return content[0], content[1:]


def _cascade_frames(argstr: str) -> list[bytes]:
    """Expand a cascade/cue spec to its ordered member frames, matching
    mwm-send's default scheduling (canonical CASCADE_DELAYS for a 20-led
    go-variant cue, the lead-derived set for a legacy F?-led cue, the
    exact ``members`` set when given; the 20 go copy goes last)."""
    members = None
    if " members " in argstr:
        argstr, _, member_str = argstr.partition(" members ")
        mbytes = member_str.split()
        if not all(_HEX_BYTE_RE.match(x) for x in mbytes):
            raise ValueError(f"cascade members must be hex bytes: {member_str!r}")
        members = [int(x, 16) for x in mbytes]
    lead, tail = _content_and_tail(argstr)
    if members is not None:
        delays = [lead, *members]
    elif lead == 0x20:
        delays = list(mwm.CASCADE_DELAYS)
    else:
        delays = list(range(lead, 0xF0, -1)) + [0x20]
    return [mwm.build_frame([d, *tail]) for d in sorted(delays, reverse=True)]


def _hex_frames(argstr: str) -> list[bytes]:
    """Resolve a raw ``hex <bytes>`` beat (CRC-completed if one byte short),
    matching mwm-send's _build_frames."""
    if not all(_HEX_RE.match(x) for x in argstr.split()):
        raise ValueError("hex expects hex bytes")
    frames = mwm.parse_frame_hex(argstr)
    return [mwm.frame_complete(f) or f for f in frames]


def parse_show_script(text: str):
    """Yield (t_ms, raw_line, frames) for each beat, mirroring mwm-send's
    _parse_show_script cue grammar (@ms offsets, hex and cascade/cue lines)."""
    pending_ms = None
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            tok, _, rest = line[1:].partition(" ")
            try:
                ms = int(tok.strip())
            except ValueError:
                raise ValueError(f"line {lineno}: bad @ms offset {tok!r}")
            if not rest.strip():
                pending_ms = ms
                continue
            line = rest.strip()
        else:
            ms = pending_ms
            pending_ms = None
        verb, _, argstr = line.partition(" ")
        if verb in ("cascade", "cue"):
            yield ms, line, _cascade_frames(argstr)
        elif verb == "hex":
            yield ms, line, _hex_frames(argstr)
        else:
            raise ValueError(f"line {lineno}: unsupported verb {verb!r} "
                             f"(only hex/cascade/cue are handled)")
    if pending_ms is not None:
        raise ValueError("dangling @ms offset with no following beat")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("msh", help="show-script (.msh) to convert")
    ap.add_argument("--raw", action="store_true",
                    help="print only the Pronto hex strings (no comments)")
    args = ap.parse_args(argv)

    try:
        text = Path(args.msh).read_text()
        beats = list(parse_show_script(text))
    except (ValueError, OSError) as exc:
        print(f"msh-to-pronto: {exc}", file=sys.stderr)
        return 2

    if args.raw:
        for _ms, _line, frames in beats:
            for fr in frames:
                print(frame_to_pronto(fr))
        return 0

    for ms, line, frames in beats:
        print(f"# @{ms}  {line}" if ms is not None else f"# {line}")
        for fr in frames:
            print(f"#   {fr.hex().upper():46s} {mwm.describe_frame(fr)['summary']}")
            print(frame_to_pronto(fr))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))