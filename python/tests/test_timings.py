"""Tests for mwm.timings: decoding framework-format raw timings."""

import unittest

from mwm import build_frame, decode_timings, frame_complete, frame_is_valid, raw_timings
from mwm.timings import DELTA_US, TICK_US, MAX_WIDTH_TICKS


class RoundtripTests(unittest.TestCase):
    def test_simple_frame_roundtrip(self):
        for content in ([0x60], [0x61, 0x66], [0x24, 0x48, 0x85]):
            frame = build_frame(content)
            self.assertEqual(decode_timings(raw_timings(frame)), [frame])

    def test_long_frame_roundtrip(self):
        # 10-byte beacon with the D0 clause: exercises merged runs heavily.
        content = [0x42, 0x00, 0x00, 0x48, 0x14, 0x0C, 0xCA, 0xD0, 0x0E, 0xA1]
        frame = build_frame(content)
        self.assertEqual(decode_timings(raw_timings(frame)), [frame])

    def test_55aa_roundtrip(self):
        from mwm import build_55aa

        frame = build_55aa([0x05, 0x06, 0x04, 0x01, 0x02])
        self.assertEqual(frame_is_valid(frame), (True, ""))
        self.assertEqual(decode_timings(raw_timings(frame)), [frame])

    def test_eleven_byte_cmd_roundtrip(self):
        # 11-byte command frame (88 bits): high nibble 0x9 marks a command
        # frame and its low nibble 0x8 fixes the body length (total bytes =
        # low nibble + 3, here 11 = 8 + 3). Round-trips the exact on-wire
        # frame 98FDD23500F2010220668B.
        content = [0xFD, 0xD2, 0x35, 0x00, 0xF2, 0x01, 0x02, 0x20, 0x66]
        frame = build_frame(content)
        self.assertEqual(frame.hex(), "98fdd23500f2010220668b")
        self.assertEqual(decode_timings(raw_timings(frame)), [frame])


class RobustnessTests(unittest.TestCase):
    def test_leading_gap_tolerated(self):
        frame = build_frame([0x60])
        signal = [-40000] + raw_timings(frame)
        self.assertEqual(decode_timings(signal), [frame])

    def test_multi_frame_capture_splits_on_gap(self):
        frames = [build_frame([0x60]), build_frame([0x64])]
        signal = list(raw_timings(frames[0]))
        # Strip the footer gap and re-attach it as an inter-message gap.
        signal[-1] = -30000
        signal += raw_timings(frames[1])
        self.assertEqual(decode_timings(signal), frames)

    def test_repeated_burst_decodes_all_copies(self):
        frame = build_frame([0x61, 0x66])
        signal = []
        for _ in range(3):
            runs = raw_timings(frame)
            signal.extend(runs[:-1])
            signal.append(-30000)  # each copy ends in a fresh gap
        self.assertEqual(decode_timings(signal), [frame] * 3)

    def test_truncated_message_dropped(self):
        frame = build_frame([0x24, 0x48, 0x85])
        runs = raw_timings(frame)
        truncated = runs[: len(runs) // 2]
        self.assertEqual(decode_timings(truncated), [])

    def test_corrupt_width_abandons_message(self):
        frame = build_frame([0x60])
        runs = raw_timings(frame)
        runs[2] += 5000  # matches no tick count
        self.assertEqual(decode_timings(runs), [])

    def test_bad_checksum_dropped(self):
        good = build_frame([0x60])
        bad = bytearray(good)
        bad[-1] ^= 0xFF
        self.assertEqual(decode_timings(raw_timings(bytes(bad))), [])


class ConventionTests(unittest.TestCase):
    def test_raw_timings_signed_alternating(self):
        runs = raw_timings(build_frame([0x60]))
        self.assertGreater(runs[0], 0)  # starts with a mark
        for i, value in enumerate(runs):
            if i % 2 == 0:
                self.assertGreater(value, 0)
            else:
                self.assertLess(value, 0)

    def test_merged_runs_within_tolerance(self):
        # Every run must match a whole number of ticks within kDELTA.
        runs = raw_timings(build_frame([0x42, 0x00, 0x00, 0x48, 0x14,
                                        0x0C, 0xCA, 0xD0, 0x0E, 0xA1]))
        for width in (abs(v) for v in runs[:-1]):  # skip footer gap
            ticks = round(width / TICK_US)
            self.assertTrue(
                any(
                    abs(width - t * TICK_US) <= DELTA_US
                    for t in range(1, MAX_WIDTH_TICKS + 1)
                ),
                f"width {width} matches no tick count",
            )


if __name__ == "__main__":
    unittest.main()


class EndByteRecoveryTests(unittest.TestCase):
    """Captures lose the final content byte; CRC brute-force restores it."""

    def _swallow_last_content_byte(self, frame: bytes) -> list[int]:
        # Simulate a receiver dropping byte -2 (the last content byte):
        # its stop-bit space merges into the gap, so the capture simply
        # ends with the CRC. Encode the SHORTENED byte stream as if it
        # were a genuine transmission.
        truncated = frame[:-2] + frame[-1:]
        return raw_timings(truncated)

    def test_recovers_beacon_with_swallowed_byte(self):
        # Doc section 5: 99-prefixed beacon, D0 0E XX argument swallowed.
        beacon = build_frame(
            [0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x2D, 0xD0, 0x0E, 0xA1]
        )
        self.assertEqual(beacon[0], 0x99)
        self.assertEqual(len(beacon), 12)
        frames = decode_timings(self._swallow_last_content_byte(beacon))
        self.assertEqual(frames, [beacon])

    def test_strict_validation_still_rejects_real_garbage(self):
        good = build_frame([0x60])
        corrupted = bytes([good[0], good[1], 0x00])  # breaks CRC, right length
        self.assertEqual(decode_timings(raw_timings(corrupted)), [])

    def test_full_length_frames_decode_normally(self):
        f = build_frame([0x61, 0x6A])  # two-opcode color script frame
        self.assertEqual(decode_timings(raw_timings(f)), [f])


class AutoCrcTests(unittest.TestCase):
    """Hand-typed hex often drops the trailing CRC; frame_complete / the
    auto_crc flag restore it from the length rule."""

    RESET = bytes.fromhex("9C20240D610E8858F04804D0420670")

    def test_complete_appends_missing_crc(self):
        short = self.RESET[:-1]
        self.assertEqual(frame_complete(short), self.RESET)

    def test_complete_returns_complete_frame_unchanged(self):
        self.assertEqual(frame_complete(self.RESET), self.RESET)

    def test_complete_rejects_bad_length(self):
        self.assertIsNone(frame_complete(self.RESET[:-2]))
        self.assertIsNone(frame_complete(bytes.fromhex("9C2024")))
        self.assertIsNone(frame_complete(b"\x55\xaa\x01\x02"))

    def test_complete_normalizes_spaced_hex(self):
        self.assertEqual(
            frame_complete("9C 20 24 0D 61 0E 88 58 F0 48 04 D0 42 06"),
            self.RESET,
        )

    def test_frame_is_valid_auto_accepts_short_frame(self):
        short = self.RESET[:-1]
        self.assertFalse(frame_is_valid(short)[0])
        self.assertTrue(frame_is_valid(short, auto_crc=True)[0])

    def test_frame_is_valid_auto_still_rejects_bad_length(self):
        self.assertFalse(
            frame_is_valid(self.RESET[:-2], auto_crc=True)[0]
        )

    def test_hex_verb_completes_frame_end_to_end(self):
        # The send tool's hex verb runs every frame through frame_complete.
        from mwm import parse_frame_hex

        frames = [frame_complete(f) or f for f in
                  parse_frame_hex("9C 20 24 0D 61 0E 88 58 F0 48 04 D0 42 06")]
        self.assertEqual(frames, [self.RESET])


class TailStopBitRecoveryTests(unittest.TestCase):
    """The CRC byte's stop-space merges into the omitted footer, so the
    capture ends mid-byte. The decoder must back-fill the missing stop bit
    rather than brute-forcing a wrong-but-CRC-valid byte."""

    def _footerless(self, frame: bytes) -> list[int]:
        # raw_timings ends in the merged footer gap (a wide space). A Tasmota
        # RawData capture omits that footer entirely, so the final byte's
        # stop space rides invisibly in the missing gap: the signal ends
        # mid-byte with the CRC byte's 8 data bits still present but no stop
        # bit (39 tick runs instead of a 40-bit frame). Drop the footer run to
        # reproduce that shape.
        return raw_timings(frame)[:-1]

    def test_recovers_frame_whose_crc_stop_bit_merged(self):
        frame = build_frame([0x0E, 0x0F])  # 91 0E 0F 1E golden yellow
        self.assertEqual(frame.hex().upper(), "910E0F1E")
        got = decode_timings(self._footerless(frame))
        self.assertEqual(got, [frame])
        self.assertNotIn(bytes.fromhex("910E9F0F"), got)

    def test_real_capture_91_0e_0f_1e(self):
        # RawData received from 179E4E for the transmit 91 0E 0F 1E; all 17
        # runs match the transmit exactly yet the old decoder reconstructed
        # 91 0E 9F 0F.
        from mwm.protocol import tasmota_timings

        raw = "+465-370+1230-445+825-850+830-1270+1730-365+420-1670Ij+855-1655+1255"
        runs = tasmota_timings(raw)
        got = decode_timings(runs)
        self.assertEqual(got, [bytes.fromhex("910E0F1E")])
        self.assertNotIn(bytes.fromhex("910E9F0F"), got)
