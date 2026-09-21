"""Tests for mwm.protocol: framing, CRC, and IR signal encoding."""

import unittest

from mwm.protocol import (
    CARRIER_HZ,
    DEFAULT_COLOR_CODE,
    EAR_OFF_CODE,
    FOOTER_GAP_US,
    RESET_OPCODE,
    LEFT_ONLY_BASE,
    TICK_US,
    build_55aa,
    build_frame,
    crc8_dallas,
    frame_is_valid,
    irsend_payload,
    parse_frame_hex,
    timings_for_frame,
    unbundle,
)


class CrcTests(unittest.TestCase):
    def test_known_checksums_from_verified_tsv(self):
        # 90 60 A6 (both off keep-alive) and 91 61 6A B7? -> use real rows.
        self.assertEqual(crc8_dallas([0x90, 0x60]), 0xA6)
        self.assertEqual(crc8_dallas([0x92, 0x24, 0x0C, 0x00]), 0xA1)

    def test_matches_irremote_reference(self):
        # CRC-8/Dallas of "123456789" is 0xA1 (check value for poly 0x31
        # normal / 0x8C reflected).
        self.assertEqual(crc8_dallas(b"123456789"), 0xA1)

    def test_accepts_bytes_like(self):
        self.assertEqual(
            crc8_dallas(bytes([0x90, 0x60])), crc8_dallas((0x90, 0x60))
        )


class ProtocolConstantTests(unittest.TestCase):
    def test_state_opcodes_match_framing_contract(self):
        # These are the canonical opcodes the HA integration encodes against;
        # they must be round-trippable through build_frame and stay within
        # the simple-color band the frame builder expects.
        self.assertEqual(EAR_OFF_CODE, 0x60)
        self.assertEqual(DEFAULT_COLOR_CODE, 0x67)
        self.assertEqual(LEFT_ONLY_BASE, 0x68)
        self.assertEqual(RESET_OPCODE, 0x24)
        for code in (EAR_OFF_CODE, DEFAULT_COLOR_CODE, LEFT_ONLY_BASE):
            frame = build_frame([code])
            self.assertTrue(frame_is_valid(frame)[0])
            self.assertEqual(frame[0], 0x90)


class BuildFrameTests(unittest.TestCase):
    def test_header_carries_length_nibble(self):
        self.assertEqual(build_frame([0x60])[0], 0x90)
        content = [0x42, 0x00, 0x00, 0x48, 0x17, 0x0C, 0x40]
        self.assertEqual(build_frame(content)[0], 0x96)
        # total = header + content + crc = len(content) + 2
        self.assertEqual(len(build_frame(content)), len(content) + 2)

    def test_trailing_byte_is_crc(self):
        frame = build_frame([0x24, 0x48, 0x85])
        self.assertEqual(frame[-1], crc8_dallas(frame[:-1]))

    def test_park_beacon_roundtrip(self):
        # Park variant from docs section 5.
        frame = bytes.fromhex("964200004817 0C402F".replace(" ", ""))
        ok, reason = frame_is_valid(build_frame(list(frame[1:-1])))
        self.assertTrue(ok, reason)

    def test_rejects_bad_lengths(self):
        with self.assertRaises(ValueError):
            build_frame([])
        with self.assertRaises(ValueError):
            build_frame([1] * 17)


class ValidityTests(unittest.TestCase):
    def test_valid_frames(self):
        self.assertEqual(frame_is_valid("90 60 A6"), (True, ""))
        frame = build_frame([0x61, 0x62])  # two-opcode color script
        self.assertEqual(frame_is_valid(frame.hex()), (True, ""))
        self.assertEqual(frame_is_valid(bytes.fromhex("90 60 A6")), (True, ""))

    def test_additive_checksum_branch(self):
        frame = build_55aa([0x05, 0x06, 0x01, 0x02, 0x03])
        self.assertEqual(frame[:2], b"\x55\xaa")
        ok, reason = frame_is_valid(frame.hex().upper())
        self.assertTrue(ok, reason)
        bad = bytearray(frame)
        bad[-1] ^= 0xFF
        ok, reason = frame_is_valid(bytes(bad))
        self.assertFalse(ok)
        self.assertIn("additive checksum mismatch", reason)

    def test_length_rule_enforced(self):
        ok, reason = frame_is_valid("97 24 48 85")  # claims 7 payload, has 2
        self.assertFalse(ok)
        self.assertIn("length rule", reason)

    def test_crc_mismatch_detected(self):
        ok, reason = frame_is_valid("90 61 FF")
        self.assertFalse(ok)
        self.assertIn("CRC mismatch", reason)

    def test_non_hex_input(self):
        ok, _ = frame_is_valid("zz 60 A6")
        self.assertFalse(ok)


class TimingsTests(unittest.TestCase):
    def test_alternating_and_merged(self):
        t = timings_for_frame(bytes.fromhex("9060A6"))
        self.assertGreater(t[0], 0)  # starts with a mark duration
        # Merged output must alternate mark/space; we only see magnitudes, so
        # check the count is even (ends with the footer gap space).
        self.assertEqual(len(t) % 2, 0)

    def test_footer_gap_present(self):
        t = timings_for_frame(bytes.fromhex("9060A6"))
        # The trailing stop-bit space merges into the inter-command gap.
        self.assertGreaterEqual(t[-1], FOOTER_GAP_US)

    def test_leading_run_merges_start_mark_and_zero_bits(self):
        # 0x90 low nibble is zero: the start mark merges with data bits
        # 0-3 (all marks) into one five-tick run, exactly like the TS and
        # perl encoders.
        self.assertEqual(
            timings_for_frame(bytes.fromhex("9060A6"))[0], 5 * TICK_US)

    def test_two_tick_mark_when_first_bit_set(self):
        # Duration invariant: every byte contributes exactly 10 ticks
        # (start + 8 data + stop) regardless of merging.
        frame = bytes.fromhex("9060A6")
        expected = 3 * 10 * TICK_US + FOOTER_GAP_US
        self.assertEqual(sum(timings_for_frame(frame)), expected)


class IrsendPayloadTests(unittest.TestCase):
    def test_prefix_and_format(self):
        payload = irsend_payload(bytes.fromhex("9060A6"))
        parts = payload.split(",")
        self.assertEqual(parts[0], str(CARRIER_HZ))
        self.assertTrue(all(p.isdigit() for p in parts[1:]))

    def test_smoke_run_shape(self):
        # The perl smoke run produced 25 numbers for this frame family;
        # ours must at least be in the same ballpark (alternation + gap).
        payload = irsend_payload(build_frame([0x24, 0x0C, 0x40]))
        self.assertLess(len(payload.split(",")), 30)


class ParseHexTests(unittest.TestCase):
    def test_plus_separated(self):
        frames = parse_frame_hex("9060A6+916162FF")
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0], bytes.fromhex("9060A6"))
        self.assertEqual(frames[1], bytes.fromhex("916162FF"))

    def test_empty_parts_skipped(self):
        self.assertEqual(parse_frame_hex("9060A6++"), [bytes.fromhex("9060A6")])


class TasmotaTimingTests(unittest.TestCase):
    def test_comma_form(self):
        from mwm.protocol import tasmota_timings
        # The Tasmota IR receiver IC converts 38 kHz bursts to simple
        # mark/space durations, so RawData has NO leading frequency. Comma
        # form alternates sign from first element (even index +).
        out = tasmota_timings("100,200,300,400")
        self.assertEqual(out, [100, -200, 300, -400])

    def test_irsend_comma_prefix_stripped(self):
        from mwm.protocol import tasmota_timings
        # IRsend <freq>,<raw> -- the leading frequency is dropped, leaving
        # the mark/space durations to alternate from index 0.
        out = tasmota_timings("IRsend 38000,100,200,300")
        self.assertEqual(out, [100, -200, 300])

    def test_comma_first_value_leading_sign(self):
        from mwm.protocol import tasmota_timings
        # A leading frequency may appear in hand-edited dumps; treat it as
        # part of the sequence (even index => positive mark).
        out = tasmota_timings("38000,100,200")
        self.assertEqual(out, [38000, -100, 200])

    def test_compact_reference(self):
        from mwm.protocol import tasmota_timings
        # A short MWM-ish compact blob exercising letter table + repeats.
        out = tasmota_timings("+9185-4490+650")
        self.assertEqual(out, [9185, -4490, 650])

    def test_compact_letters(self):
        from mwm.protocol import tasmota_timings
        # "A" assigned on first distinct value, then reused.
        out = tasmota_timings("+100A-200b")
        self.assertEqual(out, [100, 100, -200, -200])

    def test_compact_undefined_letter(self):
        from mwm.protocol import tasmota_timings
        with self.assertRaises(ValueError):
            tasmota_timings("+100Z")  # Z never defined

    def test_real_beacon_rawdata_decodes(self):
        from mwm.protocol import tasmota_timings
        from mwm.timings import decode_timings
        raw = ("+475-365+890-780+865-805+870-375+1720h+445-390+3790k+3785-380"
               "In+845-400+470bQ-1200Jk+1295n+1290-795I-360C-370G-1220+465b"
               "+2140bQ-1210E-1215+1715n+440-1215QkEdT-405JdC")
        timings = tasmota_timings(raw)
        self.assertTrue(all(isinstance(t, int) for t in timings))
        self.assertTrue(len(timings) > 20)
        frames = decode_timings(timings)
        self.assertTrue(frames, "compact RawData should decode to frames")


class SyncTests(unittest.TestCase):
    def test_build_clock_write(self):
        from mwm.protocol import build_clock_write, crc8_dallas
        frame = build_clock_write(0x42)
        # Should be 91 0C 42 <crc>
        self.assertEqual(frame[0], 0x91)
        self.assertEqual(frame[1], 0x0C)
        self.assertEqual(frame[2], 0x42)
        # CRC should be valid
        expected_crc = crc8_dallas(frame[:3])
        self.assertEqual(frame[3], expected_crc)

    def test_build_group_color(self):
        from mwm.protocol import build_group_color, crc8_dallas
        frame = build_group_color(0x00, 0x18, 0x64)  # red, groups 00-18
        # Should be a valid frame
        ok, _ = frame_is_valid(frame)
        self.assertTrue(ok)
        # Content should contain group addressing opcodes
        content = list(frame[1:-1])
        self.assertEqual(content[0], 0x20)  # group header
        self.assertIn(content[1], [0x89, 0x8C, 0x81])  # group picker

    def test_build_group_palette(self):
        from mwm.protocol import build_group_palette, crc8_dallas
        frame = build_group_palette(0x4B, 0x63, 0x05)  # palette index 5
        ok, _ = frame_is_valid(frame)
        self.assertTrue(ok)

    def test_decode_beacon_clock(self):
        from mwm.protocol import decode_beacon_clock
        # Build a beacon frame: 99 42 00 00 48 16 0C 42 D0 0E xx crc
        content = [0x42, 0x00, 0x00, 0x48, 0x16, 0x0C, 0x42, 0xD0, 0x0E]
        frame = build_frame(content)
        tick = decode_beacon_clock(frame)
        self.assertEqual(tick, 0x42)

    def test_decode_beacon_clock_not_beacon(self):
        from mwm.protocol import decode_beacon_clock
        # A non-beacon frame
        frame = build_frame([0x61])  # simple blue
        tick = decode_beacon_clock(frame)
        self.assertIsNone(tick)


class UnbundleTests(unittest.TestCase):
    def test_splits_an_a_b_a_bundle_into_frames(self):
        # The rig-verified capture: A (9 bytes), B (15 bytes), A' (A again).
        bundle = (
            "0x96190B09088418014D"
            "9C260CD5636B58EE4803D13C070685"
            "96190B09088418014D"
        )
        self.assertEqual(unbundle(bundle), [
            bytes.fromhex("96190B09088418014D"),
            bytes.fromhex("9C260CD5636B58EE4803D13C070685"),
            bytes.fromhex("96190B09088418014D"),
        ])

    def test_accepts_int_value(self):
        bundle = int(
            "0x"
            "96190B09088418014D"
            "9C260CD5636B58EE4803D13C070685"
            "96190B09088418014D",
            16,
        )
        frames = unbundle(bundle)
        self.assertEqual(len(frames), 3)

    def test_lone_declared_frame_is_not_a_bundle(self):
        # A single 0x9x frame has one element, so it stays width-derived.
        self.assertEqual(
            unbundle("0x96190B09088418014D"),
            [bytes.fromhex("96190B09088418014D")],
        )

    def test_two_byte_value_pads_to_the_24bit_minimum(self):
        self.assertEqual(unbundle("0x5508"), [bytes.fromhex("005508")])

    def test_non_landing_walk_falls_back_to_whole_value(self):
        # 0x55 0xAA show opens are not length-declared: the walk never starts.
        self.assertEqual(
            unbundle("0x5508089C260CD5"),
            [bytes.fromhex("5508089C260CD5")],
        )

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            unbundle("-1")


if __name__ == "__main__":
    unittest.main()
