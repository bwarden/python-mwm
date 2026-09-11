"""Tests for mwm.incant: the verified show-command incantations.

Each builder must reproduce, byte-for-byte, a frame observed in the real
corpus (docs/mwm-show-protocol.md section 9, samples/*).  The expected hex
strings below are the exact captures the builders were derived from.
"""

import unittest

from mwm import (
    CASCADE_DELAYS,
    build_cascade,
    build_fade,
    build_off,
    build_pulse,
    build_sparse_cascade,
    build_strobe,
    frame_is_valid,
    rotation_phrase,
)


class PulseTests(unittest.TestCase):
    def test_palette_then_simple_reproduces_corpus(self):
        # 9B 96 26 0E 81 61 58 F0 48 04 D0 45 83   (53x in MRDF0007)
        # left-ear palette 0x01 (sky blue), both ears blue, then pulse.
        frame = build_pulse(0x01, 0x61)
        self.assertEqual(frame.hex().upper(), "9B96260E816158F04804D0458323")

    def test_simple_then_palette_with_modifier_reproduces_corpus(self):
        # 9E 94 26 64 0E 8D 58 F0 48 04 D0 42 16 D0 45 83  (11x)
        # both ears red, left-ear palette 0x0D (rose pink), cycle 0x16.
        frame = build_pulse(0x0D, 0x64, both_first=True, cycle_mod=0x16)
        self.assertEqual(
            frame.hex().upper(), "9E9426640E8D58F04804D04216D04583C6"
        )

    def test_reset_override_reproduces_corpus(self):
        # 9C 20 24 0D 61 0E 88 58 F0 48 04 D0 42 06 70  (4x)
        # The re-color form: 24 override lets the new colors + pulse take
        # effect while a pulse is already running. both blue, left palette
        # purple (0x08), cycle 0x06.
        frame = build_pulse(0x08, 0x61, reset=True)
        self.assertEqual(
            frame.hex().upper(), "9C20240D610E8858F04804D0420670"
        )

    def test_reset_recolor_cycle_mod_0x20(self):
        # One-field fuzz on the reset re-color: the corpus 9C2024...D04206
        # frame with ONLY the D0 42 cycle arg moved to 0x20 (targets
        # pure-yellow/blue).  Not a capture -- the sweep probes it.
        frame = build_pulse(0x12, 0x61, reset=True, cycle_mod=0x20)
        self.assertEqual(
            frame.hex().upper(), "9C20240D610E9258F04804D0422076"
        )


class OffTests(unittest.TestCase):
    def test_off_reproduces_corpus_invoke_off(self):
        # 91 48 1F (invoke off): the catalogue's explicit stop.
        frame = build_off()
        self.assertEqual(frame.hex().upper(), "91481FB2")


class StrobeTests(unittest.TestCase):
    def test_default_reproduces_corpus(self):
        # 96 F1 24 67 58 02 48 84  (2x) delay+reset, white, strobe.
        frame = build_strobe()
        self.assertEqual(frame.hex().upper(), "96F12467580248848D")

    def test_immediate_no_reset_reproduces_corpus(self):
        # 95 20 67 58 01 48 84  (5x) immediate, white, timer 01, no reset.
        frame = build_strobe(delay=0x20, reset=False, timer=0x01)
        self.assertEqual(frame.hex().upper(), "952067580148843C")

    def test_palette_form_carries_0e_pair(self):
        # A6: palette color riding the strobe slot as the two-byte 0E pp
        # pair (the form the rotation set-piece proved is required).  Not
        # a capture -- the probe sweep answers whether the strobe slot
        # accepts it.
        frame = build_strobe(palette=0x04)
        self.assertEqual(frame.hex().upper(), "97F1240E0458024884B8")
        ok, why = frame_is_valid(frame)
        self.assertTrue(ok, why)
        # no-reset variant: same slot pair, no leading reset byte.
        frame = build_strobe(palette=0x04, reset=False)
        self.assertEqual(frame.hex().upper(), "96F10E0458024884AB")
        ok, why = frame_is_valid(frame)
        self.assertTrue(ok, why)


class FadeTests(unittest.TestCase):
    def test_countdown_reproduces_corpus(self):
        # 94 F2 48 85 58 05  (16x) 200 ms delay down-count.
        frame = build_fade()
        self.assertEqual(frame.hex().upper(), "94F248855805DD")

    def test_immediate_reproduces_corpus(self):
        # 94 20 48 85 58 05  (15x) immediate copy.
        frame = build_fade(delay=0x20)
        self.assertEqual(frame.hex().upper(), "94204885580500")


class RotationTests(unittest.TestCase):
    def test_reproduces_corpus(self):
        # 9B F1 24 48 11 D0 3D 01 62 6A FA 48 85  (2x)
        # both green + left green, rotation, ~1 s fade-out.
        frame = rotation_phrase(0x62)
        self.assertEqual(frame.hex().upper(), "9BF1244811D03D01626AFA48851C")

    def test_palette_form_reproduces_corpus(self):
        # 9D FD 24 48 11 D0 3D 01 0E 0D 0E 8D FC 48 85  (11x in MRDF0007)
        # both/left rose-pink (Palette 0x0D): the 0E-form set piece --
        # leading delay FD, 0E pp slots, fade prefix FC.  Byte-identical
        # to the capture, with only the palette byte varied per row.
        frame = rotation_phrase(0x62, palette=0x0D)
        self.assertEqual(frame.hex().upper(), "9DFD244811D03D010E0D0E8DFC488516")

    def test_palette_form_other_palettes(self):
        # The sweep's one-field fuzz: same corpus shape, pp varied.
        for pp in (0x04, 0x00, 0x12):
            with self.subTest(pp=pp):
                frame = rotation_phrase(0x62, palette=pp)
                ok, why = frame_is_valid(frame)
                self.assertTrue(ok, why)
                self.assertEqual(frame[1], 0xFD, "leading delay")
                self.assertEqual(frame[8], 0x0E)
                self.assertEqual(frame[9], pp)
                self.assertEqual(frame[10], 0x0E)
                self.assertEqual(frame[11], pp | 0x80, "left-ear bit")
                self.assertEqual(frame[12], 0xFC, "fade prefix")


class CascadeTests(unittest.TestCase):
    """Countdown cascade: the same phrase stepped FD..F1 then the immediate
    20 go copy, so every ear in range fires the cue on the same beat."""

    def test_delay_order_is_descending_then_immediate(self):
        self.assertEqual(CASCADE_DELAYS[:3], [0xFD, 0xFC, 0xFB])
        self.assertEqual(CASCADE_DELAYS[-2:], [0xF1, 0x20])

    def test_strobe_stand_reproduces_capture(self):
        # EMLG000E strobe stand first member (139086):
        #   96 FD 24 67 58 19 48 84 03
        # The countdown chain then steps down to the 20 go copy.
        frames = build_cascade([0x24, 0x67, 0x58, 0x19, 0x48, 0x84])
        self.assertEqual(len(frames), len(CASCADE_DELAYS))
        self.assertEqual(frames[0].hex().upper(), "96FD24675819488403")
        for delay, frame in zip(CASCADE_DELAYS, frames):
            ok, why = frame_is_valid(frame)
            self.assertTrue(ok, why)
            # Header byte carries the delay one byte in -- the phrase tail
            # is identical across members; only the delay slot moves.
            self.assertEqual(frame[1], delay)

    def test_fade_out_cascade_reproduces_capture(self):
        # EMLG000E fade-out cascade first member (111950):
        #   94 FD 48 85 58 64 89
        frames = build_cascade([0x48, 0x85, 0x58, 0x64])
        self.assertEqual(frames[0].hex().upper(), "94FD4885586489")
        for delay, frame in zip(CASCADE_DELAYS, frames):
            ok, why = frame_is_valid(frame)
            self.assertTrue(ok, why)
            self.assertEqual(frame[1], delay)

    def test_custom_delays(self):
        frames = build_cascade([0x48, 0x85, 0x58, 0x64], delays=[0xF4, 0x20])
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0][1], 0xF4)
        self.assertEqual(frames[1][1], 0x20)
        self.assertEqual(frames[0].hex().upper(), "94F4488558647A")
        self.assertEqual(frames[1][0], 0x94, "same header as countdown members")

    def test_empty_delays_rejected(self):
        with self.assertRaises(ValueError):
            build_cascade([0x48, 0x85], delays=[])


class SparseCascadeTests(unittest.TestCase):
    """The slow-cadence cascade: fewer members, each still collapsing on
    the same fire beat, closed by the immediate 20 go copy."""

    FADE_TAIL = [0x48, 0x85, 0x58, 0x64]

    def test_interval_400_default_members(self):
        frames = build_sparse_cascade(self.FADE_TAIL)
        self.assertEqual([f[1] for f in frames],
                         [0xFD, 0xF9, 0xF5, 0xF1, 0x20])

    def test_interval_500_members(self):
        frames = build_sparse_cascade(self.FADE_TAIL, interval_ms=500)
        self.assertEqual([f[1] for f in frames], [0xFD, 0xF8, 0xF3, 0x20])

    def test_every_member_collapses_on_the_target_beat(self):
        # Ears re-fire on their last-heard member at t = receipt + delay;
        # each member k of the chain (sent at k*interval) must land from
        # the FD lead time (1300 ms) -- send time + delay == target every
        # time, even though the members are a fraction of the full chain.
        interval, target = 400, 1300
        for member, frame in enumerate(
                build_sparse_cascade(self.FADE_TAIL, interval)):
            if frame[1] == 0x20:
                break  # the immediate go copy has no delay quantum
            fire_ms = member * interval + (frame[1] & 0x0F) * 100
            self.assertEqual(fire_ms, target)

    def test_members_are_valid_and_keep_the_phrase_tail(self):
        tail_len = len(self.FADE_TAIL)
        for frame in build_sparse_cascade(self.FADE_TAIL, interval_ms=500):
            ok, why = frame_is_valid(frame)
            self.assertTrue(ok, why)
            self.assertEqual(list(frame[2:2 + tail_len]), self.FADE_TAIL)


if __name__ == "__main__":
    unittest.main()