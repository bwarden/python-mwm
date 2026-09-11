"""Tests for mwm.decode: phrase description and ear-state tracking."""

import unittest

from mwm.decode import (
    CUE_AMBIENT,
    CUE_GROUP_PICKER,
    CUE_IMMEDIATE,
    CUE_OTHER,
    CUE_PRE_BUFFER,
    DEMO_BEACONS,
    EFFECT_COMPANION,
    EFFECTS,
    EarStateTracker,
    cue_class,
    demo_beacon_label,
    describe_55aa,
    describe_bundle,
    describe_content,
    describe_frame,
    effect_label,
    LIGHT_EFFECTS,
)
from mwm.incant import build_cascade, build_fade, build_pulse, build_strobe
from mwm.protocol import build_frame, build_clock_write, build_group_color


def frame_hex(content):
    return build_frame(content).hex().upper()


class EffectLabelTests(unittest.TestCase):
    def test_known_labels(self):
        self.assertEqual(effect_label(0x85), "fade out")
        self.assertEqual(effect_label(0x83), "crossfading transitions")
        self.assertEqual(effect_label(0x80), "power-on display (demo mode entry)")

    def test_unlabeled_sequences(self):
        self.assertEqual(effect_label(0x88), "color sequence 0x88")
        self.assertEqual(effect_label(0x2B), "effect 0x2b")

    def test_effect_003_categorised_as_single_flash(self):
        # Dig probe (2026-09-03): issued as a bare `48 03` on the seed color,
        # 0x03 shows ONE brief flash then holds the color -- it is not an
        # ongoing "even pulse".  The HA-facing catalog therefore categorises
        # it as "Single flash" (distinct from 0x04 "Pulse", the ongoing one).
        # The firmware name under the raw label stays "slow even pulse", but
        # the user-pickable effect is the one-shot classification.
        self.assertEqual(LIGHT_EFFECTS["Single flash"], 0x03)
        self.assertEqual(LIGHT_EFFECTS["Pulse"], 0x04)
        self.assertIsNot(LIGHT_EFFECTS["Single flash"], LIGHT_EFFECTS["Pulse"])
        self.assertEqual(effect_label(0x03), "slow even pulse")

    def test_effect_catalog_single_source_of_truth(self):
        # The integration's effect_list / services.yaml draw from EFFECTS, so
        # it must mirror LIGHT_EFFECTS exactly and carry the companion rule.
        self.assertEqual(set(EFFECTS), set(LIGHT_EFFECTS))
        for name, index in LIGHT_EFFECTS.items():
            entry = EFFECTS[name]
            self.assertEqual(entry["index"], index)
            self.assertEqual(
                entry["companion"], EFFECT_COMPANION.get(index)
            )
        self.assertEqual(EFFECTS["Pulse"]["companion"], 0xF0)
        self.assertIsNone(EFFECTS["Single flash"]["companion"])
        self.assertEqual(set(EFFECT_COMPANION), {0x04})


class DescribeContentTests(unittest.TestCase):
    def test_both_ears_color(self):
        desc = describe_content([0x66])
        self.assertEqual(desc["kind"], "color-command")
        self.assertIn("both ears yellow", desc["summary"])

    def test_fused_phrase_tokens(self):
        desc = describe_content([0x61, 0x66])
        self.assertEqual(len(desc["tokens"]), 2)

    def test_effect_invocation(self):
        desc = describe_content([0x24, 0x48, 0x85])
        self.assertEqual(desc["kind"], "effect-command")
        self.assertIs(desc["effect"], 0x85)
        self.assertIn("invoke fade out", desc["summary"])
        self.assertIn("reset/override", desc["summary"])

    def test_delay_and_timer_opcodes(self):
        desc = describe_content([0xF5, 0x58, 0x32])
        self.assertIn("delay ~500 ms", desc["summary"])
        self.assertIn("cycle timer 0x32", desc["summary"])

    def test_d0_modifier_consumes_two_args(self):
        desc = describe_content([0xD0, 0x42, 0x1E])
        self.assertEqual(desc["tokens"], ["D0 modifier (0x42,0x1e)"])

    def test_unknown_byte_tokenized_not_crash(self):
        desc = describe_content([0xEE])
        self.assertTrue(desc["summary"].startswith("unknown"))


class BeaconTests(unittest.TestCase):
    LIVE_BEACON = [0x42, 0x00, 0x00, 0x48, 0x14, 0x0C, 0xCA, 0xD0, 0x0E, 0xA1]
    PARK_BEACON = [0x42, 0x00, 0x00, 0x48, 0x17, 0x0C, 0x40]

    def test_live_variant_recognised(self):
        desc = describe_content(self.LIVE_BEACON)
        self.assertEqual(desc["kind"], "beacon")
        # ss=0x14 is in the demo catalog as an observed-but-unlabelled program.
        self.assertIn(
            "idle beacon (demo effect running: "
            "stored demo program 0x14 (unlabelled))",
            desc["summary"],
        )

    def test_park_variant_recognised(self):
        desc = describe_content(self.PARK_BEACON)
        self.assertEqual(desc["kind"], "beacon")
        self.assertIn("stored demo program 0x17 (unlabelled)", desc["summary"])

    def test_named_demo_effect_labelled(self):
        body = [0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x40]
        self.assertIn("color sequence (dominates demo mode)",
                      describe_content(body)["summary"])

    def test_demo_catalog_entry_labelled(self):
        # 0x16 was already named in EFFECT_LABELS and keeps that name.
        self.assertEqual(DEMO_BEACONS[0x16], "color cycle with blue return")

    def test_unknown_demo_index_falls_back_to_effect_label(self):
        # An index never seen cycling in demo mode still gets the invoked-
        # effect naming, via demo_beacon_label()'s fallback.
        self.assertEqual(demo_beacon_label(0x99), effect_label(0x99))
        self.assertEqual(demo_beacon_label(0x16), "color cycle with blue return")
        # And the beacon summary uses the same fallback.
        body = [0x42, 0x00, 0x00, 0x48, 0x99, 0x0C, 0x40]
        self.assertIn("demo effect running: effect 0x99",
                      describe_content(body)["summary"])

    def test_near_beacon_rejected(self):
        # Same shape but constant block disturbed -> not a beacon.
        body = list(self.LIVE_BEACON)
        body[1] = 0x01
        desc = describe_content(body)
        self.assertNotEqual(desc["kind"], "beacon")


class FrameDescriptionTests(unittest.TestCase):
    def test_invalid_frame_reported(self):
        desc = describe_frame("90 60 FF")
        self.assertEqual(desc["kind"], "invalid")

    def test_color_command_from_wire_hex(self):
        desc = describe_frame(frame_hex([0x60]))
        self.assertEqual(desc["kind"], "color-command")
        self.assertIn("both ears off", desc["summary"])

    def test_game_message(self):
        frame = bytes([0x55, 0xAA, 0x05, 0x06, 0x04, 0x01, 0x02,
                       sum([0x05, 0x06, 0x04, 0x01, 0x02]) % 256])
        desc = describe_55aa(frame)
        self.assertEqual(desc["summary"], "interactive game: laser tag")

    def test_shutdown_message(self):
        frame = bytes.fromhex("55AA08C413FF01EDAFF F7A".replace(" ", ""))
        desc = describe_frame(frame.hex())
        self.assertEqual(desc["summary"], "ride shutdown command")

    def test_show_timecode_fether_long_form(self):
        # Jon Fether post 259733: 0x19 prefix, trailing HH MM SS.
        desc = describe_frame("55 AA 19 04 01 02 0E 01 00 00 35 64")
        self.assertEqual(desc["timecode"], (0, 0, 53))
        self.assertEqual(desc["summary"], "show timecode 00:00:53")

    def test_show_timecode_opossum_form(self):
        # oPossum's dumps use the 0x09 prefix with the same HH MM SS tail.
        desc = describe_frame("55 AA 09 04 01 01 21 03 00 01 1C 50")
        self.assertEqual(desc["timecode"], (0, 1, 28))
        self.assertEqual(desc["summary"], "show timecode 00:01:28")

    def test_show_timecode_start_marker(self):
        desc = describe_frame("55 AA 09 04 01 01 15 01 00 00 00 25")
        self.assertEqual(desc["timecode"], (0, 0, 0))
        self.assertEqual(desc["summary"], "show timecode start (00:00:00)")

    def test_short_55aa_is_not_a_timecode(self):
        # 6-byte content form: no HH MM SS tail, stays a generic broadcast.
        desc = describe_frame("55 AA 16 01 01 01 15 02 30")
        self.assertEqual(desc["kind"], "55aa")
        self.assertNotIn("timecode", desc)
        self.assertIn("system broadcast", desc["summary"])


class TrackerTests(unittest.TestCase):
    def test_color_script_read_per_slot(self):
        # NOTE: rig session 2026-08-23 showed multi-byte phrases EXECUTE
        # sequentially (last opcode wins on real ears). The tracker still
        # reads slots independently for room-state display; revisit when
        # genuine receiver traffic clarifies who sends such phrases.
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x61, 0x66]))
        self.assertEqual(tracker.snapshot(), "left yellow, right blue")
        self.assertIsNone(tracker.effect)

    def test_left_only_frames_touch_left_slot_only(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x61]))          # both blue
        tracker.feed_frame(build_frame([0x6E]))        # left-only yellow
        self.assertEqual(tracker.snapshot(), "left yellow, right blue")
        tracker.feed_frame(build_frame([0x68]))        # left-only off
        self.assertEqual(tracker.snapshot(), "left off, right blue")

    def test_short_palette_forms_read_per_side(self):
        tracker = EarStateTracker()
        tracker.feed_frame(build_frame([0x0E, 0x80 | 0x01]))  # left sky blue
        self.assertEqual(tracker.snapshot(), "left sky blue, right off")
        tracker.feed_frame(build_frame([0x0E, 0x12]))         # both pure yellow
        self.assertEqual(tracker.snapshot(), "both ears pure yellow")

    def test_both_off_keepalive(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x61, 0x62]))
        tracker.feed_frame("90 60 A6")
        self.assertEqual(tracker.snapshot(), "both ears off")

    def test_blackout_reset(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x63, 0x63]))
        tracker.feed_frame(frame_hex([0x24]))
        self.assertEqual(tracker.snapshot(), "both ears off")

    def test_effect_recorded_and_color_kept(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x64, 0x64]))
        tracker.feed_frame(frame_hex([0x48, 0x84]))
        snap = tracker.snapshot()
        self.assertIn("both ears red", snap)
        self.assertIn("running: strobe flashes into running program", snap)

    def test_beacon_updates_only_effect(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x65, 0x65]))
        beacon = build_frame(
            [0x42, 0x00, 0x00, 0x48, 0x14, 0x0C, 0x40]
        ).hex().upper()
        desc = tracker.feed_frame(beacon)
        self.assertEqual(desc["kind"], "beacon")
        snap = tracker.snapshot()
        self.assertIn("both ears magenta", snap)
        self.assertIn("running:", snap)

    def test_palette_template_sets_both(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x19, 0x07, 0x0F, 0x16, 0x0E, 0x18, 0x04]))
        snap = tracker.snapshot()
        self.assertIn("both ears scarlet", snap)

    def test_invalid_frame_leaves_state_alone(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x64, 0x64]))
        desc = tracker.feed_frame("90 60 FF")
        self.assertEqual(desc["kind"], "invalid")
        self.assertIn("both ears red", tracker.snapshot())

    def test_last_summary_exposed(self):
        tracker = EarStateTracker()
        tracker.feed_frame(frame_hex([0x60]))
        self.assertIn("both ears off", tracker.last_summary)


if __name__ == "__main__":
    unittest.main()


class BundleTests(unittest.TestCase):
    """A-B-A' wand/ear command bundles (doc section 3)."""

    def test_recognises_phrase_companion_phrase(self):
        phrase = build_frame([0x61, 0x6A])          # two-opcode color script
        companion = build_frame(                    # pulse w/ cycle params
            [0x24, 0x58, 0xF0, 0x48, 0x04, 0xD0, 0x42, 0x0A]
        )
        out = describe_bundle([phrase, companion, phrase])
        self.assertIsNotNone(out)
        self.assertEqual(out["kind"], "bundle")
        self.assertIn("pulse", out["summary"].lower())
        self.assertEqual(out["phrase_hex"], phrase.hex().upper())
        self.assertEqual(out["companion_hex"], companion.hex().upper())
        self.assertIn("special cycle arg", out["summary"])   # 58 F0
        self.assertIn("cycle scale 10 x 200 ms", out["summary"])

    def test_bundle_detected_with_rolling_counter_tail(self):
        """Real rigs mutate a counter byte (+CRC) between A and A'."""
        base = bytes.fromhex("961908091272A2")
        phrase_a = build_frame(base + bytes([0x00]))
        phrase_a2 = build_frame(base + bytes([0xB3]))
        companion = build_frame(bytes([0x24, 0x0C, 0x72]))
        out = describe_bundle([phrase_a, companion, phrase_a2])
        self.assertIsNotNone(out)
        self.assertEqual(out["kind"], "bundle")
        self.assertEqual(out["phrase_hex"], phrase_a.hex().upper())

    def test_short_phrases_still_require_exact_match(self):
        """Tail exemption must not fuse distinct short color commands."""
        off = build_frame([0x60])          # canonical 90 60 A6
        blue = build_frame([0x61])
        self.assertIsNone(describe_bundle([off, blue, blue]))

    def test_identical_short_triple_is_a_bundle(self):
        phrase = build_frame([0x60])
        companion = build_frame([0x24, 0x58, 0xF0])
        out = describe_bundle([phrase, companion, phrase])
        self.assertIsNotNone(out)
        self.assertEqual(out["companion_hex"], companion.hex().upper())

    def test_rejects_non_bundles(self):
        a = build_frame([0x60])
        b = build_frame([0x61])
        self.assertIsNone(describe_bundle([a]))
        self.assertIsNone(describe_bundle([a, b]))
        self.assertIsNone(describe_bundle([a, a, a]))   # all identical
        self.assertIsNone(describe_bundle([a, b, b]))   # A' mismatch


class CueClassTests(unittest.TestCase):
    """The show-cue taxonomy (docs §3): delay-led countdowns are pre-buffer
    lookahead cues, the 20 go copy snaps now, group phrases pick ranges,
    and the pulse family sustains an ambient loop."""

    def test_delay_led_members_are_pre_buffer(self):
        # Fade (FD 48 85 58 16), strobe (F1 24 67 58 02 48 84) -- every
        # countdown member is a lookahead cue for the crowd.
        self.assertEqual(cue_class(build_fade(delay=0xF2)), CUE_PRE_BUFFER)
        self.assertEqual(cue_class(build_strobe()), CUE_PRE_BUFFER)
        frames = build_cascade([0x48, 0x85, 0x58, 0x64])
        self.assertEqual(cue_class(frames[0]), CUE_PRE_BUFFER)

    def test_go_copy_and_effect_invokes_are_immediate(self):
        go = build_cascade([0x48, 0x85, 0x58, 0x64])[-1]  # the 20 go copy
        self.assertEqual(cue_class(go), CUE_IMMEDIATE)
        self.assertEqual(cue_class(build_frame([0x48, 0x1F])),  # invoke off
                         CUE_IMMEDIATE)
        # bare 24 48 effect invoke rides the immediate class too
        self.assertEqual(cue_class(build_frame([0x24, 0x48, 0x85])),
                         CUE_IMMEDIATE)

    def test_group_addressed_phrases_pick_a_range(self):
        self.assertEqual(cue_class(build_group_color(0x00, 0x3F, 0x61)),
                         CUE_GROUP_PICKER)
        # the 24 0D group/override family (corpus hard transitions)
        self.assertEqual(
            cue_class(build_frame([0x24, 0x0D, 0x48, 0x82, 0xD0, 0x0E, 0xFF])),
            CUE_GROUP_PICKER)

    def test_pulse_family_is_ambient_loop(self):
        # Fused pulse and its re-color override both sustain a loop.
        self.assertEqual(cue_class(build_pulse(0x01, 0x61)), CUE_AMBIENT)
        self.assertEqual(cue_class(build_pulse(0x01, 0x61, reset=True)),
                         CUE_AMBIENT)

    def test_state_writers_are_other(self):
        self.assertEqual(cue_class(build_frame([0x61])), CUE_OTHER)
        self.assertEqual(cue_class(build_clock_write(0x40)), CUE_OTHER)

    def test_short_garbage_is_other(self):
        self.assertEqual(cue_class(b"\x90"), CUE_OTHER)
