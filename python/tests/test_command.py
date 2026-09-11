"""Tests for the MwmCommand envelope handed to infrared emitters."""

import unittest

from mwm import CARRIER_HZ, MwmCommand, build_frame, raw_timings


class MwmCommandTests(unittest.TestCase):
    def test_carrier_is_38khz(self):
        cmd = MwmCommand(build_frame([0x60]))
        self.assertEqual(cmd.modulation, CARRIER_HZ)
        self.assertEqual(cmd.modulation, 38000)

    def test_raw_timings_passthrough(self):
        frame = build_frame([0x61, 0x66])
        cmd = MwmCommand(frame)
        self.assertEqual(cmd.get_raw_timings(), raw_timings(frame))
        self.assertEqual(cmd.frame, frame)

    def test_repeat_count_default_zero(self):
        self.assertEqual(MwmCommand(build_frame([0x60])).repeat_count, 0)
        self.assertEqual(
            MwmCommand(build_frame([0x60]), repeat_count=2).repeat_count, 2
        )

    def test_repr_shows_hex(self):
        frame = build_frame([0x60])
        self.assertIn(frame.hex().upper(), repr(MwmCommand(frame)))


if __name__ == "__main__":
    unittest.main()
