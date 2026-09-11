"""MWM ("Made With Magic" / Glow With The Show) IR protocol library.

Pure-Python port of the framing, encoding, and decoding rules documented in
docs/mwm-show-protocol.md. Vendored inside the integration so the component
is self-contained (no pip dependency); the Home Assistant platforms import
from here.

Sources of truth (all in this repo):
    docs/mwm-show-protocol.md     -- protocol reference (framing, opcodes,
                                      effects table, wand phrase templates,
                                      beacon structure, timing rules).
    samples/mwm-gwts-colors.tsv   -- rig-verified color frames: simple
                                      one-bit colors, 30-shade palette
                                      RGB values, fused frames, bundle
                                      examples.  Original measurements by
                                      oPossum (DIYC forum post #259750);
                                      CRC-8 validated.
    web/src/lib/protocol/mwm.ts   -- TypeScript framing reference (toPronto,
                                      decodeMWM) used by the browser UI.
    IRremoteESP8266               -- upstream decodeMWM algorithm; our
                                      timings.py mirrors its tick-merging
                                      and end-bit-recovery logic.
"""

from .palette import (
    EAR_STATE_OFF,
    PALETTE,
    SIMPLE_COLORS,
    SIMPLE_COLOR_CODES,
    color_palette,
    nearest_entry,
    parse_color,
)
from .protocol import (
    CARRIER_HZ,
    FOOTER_GAP_US,
    TICK_US,
    DEFAULT_COLOR_CODE,
    EAR_OFF_CODE,
    RESET_OPCODE,
    LEFT_ONLY_BASE,
    build_55aa,
    build_clock_write,
    build_frame,
    build_group_color,
    build_group_palette,
    crc8_dallas,
    decode_beacon_clock,
    frame_complete,
    frame_is_valid,
    irsend_payload,
    parse_frame_hex,
    raw_timings,
    tasmota_timings,
    timings_for_frame,
)
from .timings import decode_timings
from .incant import (
    CASCADE_DELAYS,
    build_cascade,
    build_fade,
    build_off,
    build_pulse,
    build_sparse_cascade,
    build_strobe,
    rotation_phrase,
)
from .command import MwmCommand
from .decode import (
    EFFECTS,
    EFFECT_LABELS,
    EFFECT_COMPANION,
    LIGHT_EFFECTS,
    DEMO_BEACONS,
    EarStateTracker,
    cue_class,
    demo_beacon_label,
    describe_55aa,
    describe_content,
    describe_bundle,
    describe_frame,
    effect_label,
)

__all__ = [
    "CARRIER_HZ",
    "FOOTER_GAP_US",
    "TICK_US",
    "DEFAULT_COLOR_CODE",
    "EAR_OFF_CODE",
    "RESET_OPCODE",
    "LEFT_ONLY_BASE",
    "build_55aa",
    "build_clock_write",
    "build_frame",
    "build_group_color",
    "build_group_palette",
    "crc8_dallas",
    "decode_beacon_clock",
    "frame_complete",
    "frame_is_valid",
    "irsend_payload",
    "parse_frame_hex",
    "raw_timings",
    "tasmota_timings",
    "timings_for_frame",
    "decode_timings",
    "CASCADE_DELAYS",
    "build_cascade",
    "build_fade",
    "build_off",
    "build_pulse",
    "build_sparse_cascade",
    "build_strobe",
    "rotation_phrase",
    "MwmCommand",
    "PALETTE",
    "SIMPLE_COLORS",
    "SIMPLE_COLOR_CODES",
    "EAR_STATE_OFF",
    "nearest_entry",
    "parse_color",
    "color_palette",
    "EFFECTS",
    "EFFECT_LABELS",
    "EFFECT_COMPANION",
    "LIGHT_EFFECTS",
    "DEMO_BEACONS",
    "EarStateTracker",
    "cue_class",
    "demo_beacon_label",
    "describe_55aa",
    "describe_content",
    "describe_bundle",
    "describe_frame",
    "effect_label",
]
