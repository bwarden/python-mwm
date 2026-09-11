"""Offline tests for the mwm-send rig tool's command composition.

Loads ``tools/mwm-send.py`` via importlib (it does not import Home
Assistant) and exercises the incantation color interchange probes and the
auto-CRC completion so they stay honest without a rig or IR hardware.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _load():
    spec = importlib.util.spec_from_file_location("mwm_send", TOOLS / "mwm-send.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mwm_send"] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load()

RESET_PULSE = bytes.fromhex("9C20240D610E8858F04804D0420670")


class PulseCompositionTests(unittest.TestCase):
    def test_corpus_palette_left_simple_right(self):
        # Reproduces the verified reset frame byte-for-byte.
        self.assertEqual(S._incant_pulse("pal:0x08", "blue", {"reset": True}),
                         [RESET_PULSE])

    def test_both_first_with_cycle_mod_matches_builder(self):
        got = S._incant_pulse("pal:0x0D", "red",
                              {"both_first": True, "cycle_mod": 0x16})[0]
        self.assertEqual(got, S.build_pulse(0x0D, 0x64,
                                            both_first=True, cycle_mod=0x16))

    def test_default_matches_builder(self):
        got = S._incant_pulse("pal:0x01", "blue", {})[0]
        self.assertEqual(got, S.build_pulse(0x01, 0x61))

    def test_interchange_simple_left_palette_right_reset(self):
        # The color encodings may be swapped per slot (the open A6 probe):
        # a left-only simple op + a both-ear palette op in the override form.
        got = S._incant_pulse("blue", "pal:0x04", {"reset": True})[0]
        self.assertTrue(got.startswith(bytes.fromhex("9C20240D0E04")))
        self.assertTrue(S.mwm.frame_is_valid(got)[0])

    def test_interchange_both_palette(self):
        got = S._incant_pulse("pal:0x19", "pal:0x04", {})[0]
        self.assertTrue(got.startswith(bytes.fromhex("9C96260E990E04")))
        self.assertTrue(S.mwm.frame_is_valid(got)[0])

    def test_rejects_unknown_color(self):
        with self.assertRaises(ValueError):
            S._incant_pulse("burgundy", "blue", {})
        with self.assertRaises(ValueError):
            S._incant_pulse("pal:0x7F", "blue", {})


class CascadesAndSequenceTests(unittest.TestCase):
    """The park's countdown-cascade primitive and the show-script parser."""

    def test_cascade_verb_expands_to_countdown_chain(self):
        frames = S._build_frames("cascade fade cycle=0x16")
        # FD..F1 (13) + the 20 immediate go copy.
        self.assertEqual(len(frames), 14)
        self.assertEqual(frames[0][1], 0xFD)
        self.assertEqual(frames[-2][1], 0xF1)
        self.assertEqual(frames[-1][1], 0x20)
        for fr in frames:
            ok, why = S.mwm.frame_is_valid(fr)
            self.assertTrue(ok, why)
        # First member must match the EMLG000E fade-out cascade exactly.
        self.assertEqual(frames[0].hex().upper(), "94FD48855816CD")

    def test_cascade_accepts_incant_prefix(self):
        a = S._build_frames("cascade fade cycle=0x16")
        b = S._build_frames("cascade incant fade cycle=0x16")
        self.assertEqual(a, b)

    def test_cascade_requires_delay_led_phrase(self):
        with self.assertRaises(ValueError):
            S._build_frames("cascade stop")
        with self.assertRaises(ValueError):
            S._build_frames("cascade simple left blue")
        with self.assertRaises(ValueError):
            S._build_frames("cascade hex 24 0D 48 82 D0 0E FF")  # no delay byte

    def test_cascade_hex_reproduces_hard_transition_chain(self):
        # EMLG0026 member F1 -> FD, and the immediate 20 go copy:
        frames = S._build_frames("cascade hex F1 24 0D 48 82 D0 0E FF")
        self.assertEqual(len(frames), 14)
        self.assertEqual(frames[-1].hex().upper(), "9720240D4882D00EFF40")
        self.assertEqual(frames[-2].hex().upper(), "97F1240D4882D00EFF8C")
        for fr in frames:
            ok, why = S.mwm.frame_is_valid(fr)
            self.assertTrue(ok, why)

    def test_parse_show_script_inline_and_standalone_offsets(self):
        script = (
            "# comment\n"
            "@0\n"
            "cascade fade cycle=0x16\n"
            "@800 cascade fade cycle=0x05\n"
            "simple left blue\n"
        )
        beats = S._parse_show_script(script)
        self.assertEqual(len(beats), 3)
        self.assertEqual(beats[0]["t_ms"], 0)
        # Script-path cues expand to the LEAD-DERIVED countdown (the lead
        # F2 down to F1, then the go copy): members pre-roll -(d & 0x0F)*100
        # ms (the F? low-nibble delay) and the 20 go copy rides the @ms
        # anchor itself.
        self.assertEqual(len(beats[0]["frames"]), 3)
        self.assertEqual([f[1] for f in beats[0]["frames"]],
                         [0xF2, 0xF1, 0x20])
        self.assertEqual(beats[0]["rel_ms"],
                         [-200.0, -100.0, 0.0])
        self.assertTrue(beats[0]["cascade"])
        self.assertEqual(beats[1]["t_ms"], 800)
        self.assertEqual(len(beats[1]["frames"]), 3)
        self.assertIsNone(beats[2]["t_ms"])
        self.assertEqual(len(beats[2]["frames"]), 1)
        self.assertEqual(beats[2]["rel_ms"], [0.0])
        self.assertFalse(beats[2]["cascade"])

    def test_parse_show_script_cascade_full_expands_14(self):
        # --cascade-full restores the capture-faithful 14-member chain,
        # FD..F1 pre-rolling before the go copy at the @ms anchor.
        beats = S._parse_show_script(
            "@0\ncascade fade cycle=0x16\n", cascade_full=True)
        self.assertEqual(len(beats[0]["frames"]), 14)
        self.assertEqual(beats[0]["frames"][-1][1], 0x20)
        self.assertEqual(beats[0]["rel_ms"][-1], 0.0)
        self.assertEqual(len(beats[0]["rel_ms"]), 14)
        self.assertTrue(all(r < 0 for r in beats[0]["rel_ms"][:-1]))

    def test_parse_show_script_cascade_opt_in_pacing(self):
        # An explicit --cascade-ms opts into uniform member pacing FROM the
        # @ms anchor instead of the default pre-roll countdown.
        beats = S._parse_show_script(
            "@0\ncascade fade cycle=0x16\n", cascade_ms=400.0)
        self.assertEqual(len(beats[0]["frames"]), 3)
        self.assertEqual(beats[0]["rel_ms"], [0.0, 400.0, 800.0])

    def test_parse_show_script_unknown_line_raises(self):
        with self.assertRaises(ValueError):
            S._parse_show_script("not-a-verb nothing\n")
        with self.assertRaises(ValueError):
            S._parse_show_script("@1000\n")  # dangling offset

    def test_parse_show_script_members_rebuilds_exact_set(self):
        # gen_show_script preserves a capture's countdown member set in a
        # `members` clause; the rebuild is the EXACT lead+members, never a
        # sparse/full canonical chain, byte-identical to the captured hex.
        # Order normalises to descending delay (go copy LAST, so a late F1
        # can never re-arm the ears after the cue); rel follows the same
        # pre-roll contract as any cue.
        beats = S._parse_show_script(
            "@0\ncascade hex F4 D0 42 08 members F3 F2 F1 20\n")
        self.assertEqual(len(beats), 1)
        beat = beats[0]
        self.assertTrue(beat["cascade"])
        self.assertEqual([f[1] for f in beat["frames"]],
                         [0xF4, 0xF3, 0xF2, 0xF1, 0x20])
        self.assertEqual(beat["rel_ms"],
                         [-400.0, -300.0, -200.0, -100.0, 0.0])
        # Every member rebuild == the captured frame, CRC included.
        self.assertEqual(
            [f.hex().upper() for f in beat["frames"]],
            [S.mwm.build_frame([d, 0xD0, 0x42, 0x08]).hex().upper()
             for d in [0xF4, 0xF3, 0xF2, 0xF1, 0x20]])
        for fr in beat["frames"]:
            ok, why = S.mwm.frame_is_valid(fr)
            self.assertTrue(ok, why)

    def test_parse_show_script_members_preserves_duplicate_lead(self):
        # A repeated delay byte (three 96-chains run together) must survive
        # the rebuild: members can include a byte equal to the lead.  The
        # lead fires first, then the members normalise to descending delay
        # (go copy LAST, so a late F1 can never re-arm the ears after the
        # cue), so the F4 in the members clause survives as a second,
        # duplicate F4 right behind the lead.
        beats = S._parse_show_script(
            "@0\ncascade hex F4 24 65 F3 64 F3 24 members "
            "F3 F2 F1 20 F4\n")
        self.assertEqual([f[1] for f in beats[0]["frames"]],
                         [0xF4, 0xF4, 0xF3, 0xF2, 0xF1, 0x20])
        for fr in beats[0]["frames"]:
            ok, why = S.mwm.frame_is_valid(fr)
            self.assertTrue(ok, why)

    def test_parse_show_script_members_bad_hex_raises(self):
        with self.assertRaises(ValueError):
            S._parse_show_script("@0\ncascade hex F4 D0 42 08 members F3 X2\n")

    def test_sequence_plan_prints_and_dry_runs(self):
        script = (
            "@0\n"
            "cascade fade cycle=0x16\n"
            "@800\n"
            "simple left blue\n"
        )
        beats = S._parse_show_script(script)
        recorder = S._run_sequence
        import io
        import types
        args = types.SimpleNamespace(
            dry_run=True, monitor=None, after=1.0, cascade_ms=110.0)
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            recorder(None, beats, args)  # dry run: mqtt unused
        text = out.getvalue()
        self.assertIn("2 beat(s)", text)
        self.assertIn("0 ms   3 frame(s)", text)
        self.assertIn("200ms countdown, go@0ms", text)
        self.assertIn("800 ms   1 frame(s)", text)
        self.assertIn("(dry run: no frames sent)", text)
        # A single frame beat must not ride the cascade cadence (its plan
        # slot is exactly the @ offset); the sparse cascade beat's members
        # collapse onto its @ (back-to-back, step 0) so the cue stays tight.
        self.assertIn("~0.8s total", text)


class SequenceLiveTimingTests(unittest.TestCase):
    """Live-status formatting and sequence pacing defaults."""

    def test_format_current_counts_members(self):
        line = S._format_current(12.3, 2, 10, 412070, 0, 3, "fade out")
        self.assertIn("beat 3/10", line)
        self.assertIn("@412070", line)
        self.assertIn("member 1/3", line)
        self.assertIn("fade out", line)

    def test_format_next_announces_upcoming_beat(self):
        line = S._format_next(2.5, 142286, "strobe 0x0F")
        self.assertIn("next in    2.5s -> @142286ms", line)
        self.assertIn("strobe 0x0F", line)

    def test_default_repeat_resolution(self):
        self.assertEqual(S._repeat_default(None), 2)

    def test_default_repeat_for_sequence_is_one(self):
        self.assertEqual(S._repeat_default("sequence"), 1)

    def test_min_gap_constant_default(self):
        self.assertEqual(S._MIN_GAP_MS, 30.0)

    def test_end_reset_frames_are_both_ears_off(self):
        # The end-of-show reset must be the canonical both-ears-off frame
        # (90 60 A6), so ears never stay lit in a shows final state.
        self.assertEqual(S.END_RESET_FRAMES, (bytes.fromhex("9060A6"),))

    def test_run_sequence_logs_to_stdout_and_transients_to_tty(self):
        # stdout must stay a clean newline-terminated log of what happened
        # (capturable by >/|); the rewritable now/next status goes only to
        # the controlling terminal (/dev/tty), never into the pipes.
        import contextlib
        import io
        import types
        import unittest.mock as mock

        class FakeClock:
            def __init__(self):
                self.t = 0.0

            def monotonic(self):
                return self.t

            def sleep(self, s):
                self.t += s

        script = "@0\ncascade fade cycle=0x16\n@800\nsimple left blue\n"
        beats = S._parse_show_script(script)
        args = types.SimpleNamespace(
            dry_run=False, monitor=None, after=1.0, min_gap_ms=30.0,
            cascade_ms=None, cascade_full=False, no_end_reset=False)
        out, tty = io.StringIO(), io.StringIO()
        with mock.patch.object(S, "send_payload") as mock_send, \
             mock.patch.object(S, "time", FakeClock()), \
             mock.patch.object(S, "_open_tty", return_value=tty):
            with contextlib.redirect_stdout(out):
                S._run_sequence(None, beats, args)
        log = out.getvalue()
        self.assertNotIn("\r", log)  # stdout is a clean scrollable log
        # The countdown pre-rolls of the @0 cue have no room before the show
        # start (their true @ms+rel slots are negative) so they're dropped:
        # only the GO fires on the anchor, then simple @800 + the reset.
        self.assertEqual(log.count("\n"), 7)  # 3 plan + reset note + 3 sends
        self.assertEqual(len(mock_send.mock_calls), 3)
        self.assertIn("end-of-show reset  9060A6", log)
        trans = tty.getvalue()
        self.assertIn("\r[", trans)  # transient now/next rides the tty line
        self.assertTrue(trans.endswith("\r\n"))  # cleared after the run

    def test_no_end_reset_keeps_verbatim_ending(self):
        # --no-end-reset must leave the script's last beat the final IR
        # (no extra frame lands on the wire).
        import contextlib
        import io
        import types
        import unittest.mock as mock

        class FakeClock:
            def __init__(self):
                self.t = 0.0

            def monotonic(self):
                return self.t

            def sleep(self, s):
                self.t += s

        script = "@0\ncascade fade cycle=0x16\n@800\nsimple left blue\n"
        beats = S._parse_show_script(script)
        args = types.SimpleNamespace(
            dry_run=False, monitor=None, after=1.0, min_gap_ms=30.0,
            cascade_ms=None, cascade_full=False, no_end_reset=True)
        out, tty = io.StringIO(), io.StringIO()
        with mock.patch.object(S, "send_payload") as mock_send, \
             mock.patch.object(S, "time", FakeClock()), \
             mock.patch.object(S, "_open_tty", return_value=tty):
            with contextlib.redirect_stdout(out):
                S._run_sequence(None, beats, args)
        log = out.getvalue()
        # Same cue as above: the countdown members can't pre-roll before the
        # show start so only the GO + simple beat go out, and no reset.
        self.assertEqual(len(mock_send.mock_calls), 2)
        self.assertEqual(log.count("\n"), 5)
        self.assertNotIn("end-of-show reset", log)


class HexAutoCrcTests(unittest.TestCase):
    def test_build_frames_appends_missing_crc(self):
        frames = S._build_frames("hex 9C 20 24 0D 61 0E 88 58 F0 48 04 D0 42 06")
        self.assertEqual(frames, [RESET_PULSE])

    def test_build_frames_leaves_complete_frame_alone(self):
        frames = S._build_frames(f"hex {RESET_PULSE.hex()}")
        self.assertEqual(frames, [RESET_PULSE])

    def test_build_frames_does_not_mangle_garbage(self):
        frames = S._build_frames("hex 9C 20 24")
        self.assertEqual(frames, [bytes.fromhex("9C2024")])


def _load_color_cycle():
    spec = importlib.util.spec_from_file_location(
        "color_cycle", TOOLS / "color_cycle.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["color_cycle"] = mod
    spec.loader.exec_module(mod)
    return mod


class IncantProbeSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = _load_color_cycle()

    def test_every_probe_frame_is_valid(self):
        probes = self.cc.build_incant_probes(S, incant_random=2)
        self.assertGreater(len(probes), 10)
        # Every step and every retry must be a parseable valid MWM frame.
        for probe in probes:
            things = list(probe["steps"]) + list(probe.get("retries", []))
            for thing in things:
                for fr in thing["frames"]:
                    ok, why = S.mwm.frame_is_valid(fr)
                    self.assertTrue(
                        ok,
                        f"{probe['family']} {probe['name']} -> "
                        f"{fr.hex().upper()}: {why}")
            # Every probe exposes a final checkpoint (the observer always
            # gets to verdict the outcome).  The pulse probe additionally
            # checks the fresh phase mid-probe before the re-color runs on
            # the still-pulsing ears; no OTHER family has mid-probe
            # checkpoints (the rest are silent anchor/setup steps).
            cps = [s for s in probe["steps"] if s.get("checkpoint")]
            self.assertGreaterEqual(len(cps), 1,
                                    f"{probe['family']} has no checkpoint")
            self.assertTrue(probe["steps"][-1].get("checkpoint"),
                            f"{probe['family']} last step is a checkpoint")
            if probe["family"] != "pulse":
                self.assertEqual(len(cps), 1,
                                 f"{probe['family']} should have one "
                                 "checkpoint")
            self.assertIn("watch", probe)
            self.assertTrue(probe["watch"])

    def test_pulse_probes_include_interchange_and_reset(self):
        probes = self.cc.build_incant_probes(S)
        pulses = [p for p in probes if p["family"] == "pulse"]
        # 5 combo probes + the fade-to-dark soft-transition probe + the
        # slow-cycle re-color fuzz.
        self.assertEqual(len(pulses), 7)
        combos = [p for p in pulses if "-> re-color" in p["name"]]
        self.assertEqual(len(combos), 5)
        for probe in combos:
            labels = [s.get("label") for s in probe["steps"]]
            # Anchor reset, fresh pulse, then re-color reset.
            self.assertEqual(labels[0], "anchor reset")
            self.assertIn("fresh pulse", labels[1])
            self.assertEqual(labels[2], "re-color reset")
            recolor = probe["steps"][2]["frames"][0]
            self.assertTrue(recolor.startswith(bytes.fromhex("9C20")))
            # The reset-armed retry must parse as valid too.
            for ret in probe.get("retries", []):
                for fr in ret["frames"]:
                    ok, _ = S.mwm.frame_is_valid(fr)
                    self.assertTrue(ok)
            self.assertIn("re-color", probe["name"])
            # Observed pulse behavior: the ears alternate, and the
            # reset re-color restarts the running pulse's cycle.
            self.assertIn("ALTERNATING", probe["steps"][1]["expect"])
            self.assertIn("RESTARTS", probe["steps"][2]["expect"])
        soft = [p for p in pulses if "fade-to-dark" in p["name"]]
        self.assertEqual(len(soft), 1)
        labels = [s.get("label") for s in soft[0]["steps"]]
        self.assertEqual(labels, ["anchor reset",
                                  "fresh pulse rose pink / white",
                                  "fade to dark",
                                  "fresh pulse pure-yellow/blue"])
        self.assertEqual(len([s for s in soft[0]["steps"]
                              if s.get("checkpoint")]), 3)
        ladder = [p for p in pulses if "cadence ladder" in p["name"]]
        self.assertEqual(len(ladder), 1)
        lp = ladder[0]
        # Cadence characterization: colors held at pure-yellow/blue, D0 42 tt
        # walked one field at a time -- 0x06 -> 0x10 -> 0x20 -> 0x40 -- so
        # the only variable is the post-swap pulse rate.
        self.assertEqual(lp["steps"][2]["label"], "re-color cadence 0x06 (corpus)")
        expected = [0x06, 0x10, 0x20, 0x40]
        for idx, tt in enumerate(expected, 2):  # steps 2..5 are the ladder
            step = lp["steps"][idx]
            self.assertTrue(step.get("checkpoint"))
            recolor = step["frames"][0]
            ok, why = S.mwm.frame_is_valid(recolor)
            self.assertTrue(ok, why)
            marker = bytes.fromhex(f"D042{tt:02X}")
            self.assertIn(marker, recolor,
                          f"step {idx} must carry D0 42 {tt:02X}")
            # Identical frame to the combos' re-color except the cycle arg.
            self.assertTrue(recolor.startswith(bytes.fromhex("9C20")))
        rose = lp["steps"][1]["frames"][0]
        self.assertNotIn(bytes.fromhex("D042"), rose,
                         "fresh pulse must not ship a cycle modifier")

    def test_rotation_probes_0e_form_and_control(self):
        probes = self.cc.build_incant_probes(S)
        rotations = [p for p in probes if p["family"] == "rotation"]
        # tt=01 baseline + simple blue control + three palette rows.
        self.assertEqual(len(rotations), 5)
        # The tt ladder is answered (larger tt = fewer flashes before the
        # fade, tt=0x20 nothing) -- no ladder rows remain.
        self.assertFalse([p for p in rotations if "tt=0x" in p["name"]])
        palettes = {"rose pink": 0x0D, "pure blue": 0x04,
                    "pale cyan-white": 0x00}
        for probe in rotations:
            sweep = probe["steps"][1]
            # Single-arm: the earlier 0.65 s re-arm restarted the sweep at
            # the base color every time, so there is no sustain here.
            self.assertNotIn("sustain_ms", sweep)
            self.assertNotIn("sustain_gap_ms", sweep)
            phrase = sweep["frames"][0]
            ok, why = S.mwm.frame_is_valid(phrase)
            self.assertTrue(ok, f"{probe['name']}: {why}")
            # D0 3D 01 rides at frame index 7 (after hdr F1/FD 24 48 11 D0 3D).
            self.assertEqual(phrase[7], 0x01, f"{probe['name']} D0 3D arg")
            if "green" in probe["name"]:
                self.assertEqual(phrase[1:3], bytes.fromhex("F124"))
                self.assertEqual(phrase[8], 0x62, probe["name"])
                self.assertEqual(phrase[9], 0x6A, probe["name"])
            elif "blue control" in probe["name"]:
                self.assertEqual(phrase[1:3], bytes.fromhex("F124"))
                self.assertEqual(phrase[8], 0x61, probe["name"])
                self.assertEqual(phrase[9], 0x69, probe["name"])
            else:
                pp = next(v for k, v in palettes.items()
                          if k in probe["name"])
                # Corpus 0E-form: FD 24 48 11 D0 3D 01 0E pp 0E (pp|80)
                # FC 48 85 -- the rose-pink set-piece, only pp varies.
                self.assertEqual(phrase[1], 0xFD, probe["name"])
                self.assertEqual(phrase[8], 0x0E, probe["name"])
                self.assertEqual(phrase[9], pp, probe["name"])
                self.assertEqual(phrase[10], 0x0E, probe["name"])
                self.assertEqual(phrase[11], pp | 0x80, probe["name"])
                self.assertEqual(phrase[12], 0xFC, probe["name"])
        rose = next(p for p in rotations if "rose pink" in p["name"])
        self.assertEqual(
            rose["steps"][1]["frames"][0][1:15].hex(),
            "fd244811d03d010e0d0e8dfc4885",
            "rose-pink row must be byte-identical to the corpus 0E-form")

    def test_incant_random_extends_sweep(self):
        base = self.cc.build_incant_probes(S)
        widened = self.cc.build_incant_probes(S, incant_random=3)
        self.assertEqual(len(widened) - len(base), 3)

    def test_all_families_present(self):
        probes = self.cc.build_incant_probes(S)
        families = {p["family"] for p in probes}
        self.assertEqual(families, {"pulse", "strobe", "fade",
                                    "rotation", "stop"})

    def test_fade_retry_is_reset_armed(self):
        probes = self.cc.build_incant_probes(S)
        fades = [p for p in probes if p["family"] == "fade"]
        self.assertTrue(fades)
        for fade in fades:
            self.assertTrue(fade["retries"])
            armed = fade["retries"][0]["frames"][0]
            ok, why = S.mwm.frame_is_valid(armed)
            self.assertTrue(ok, why)
            self.assertEqual(armed[1:3], bytes.fromhex("F124"),
                             "fade retry should lead with the reset")
            self.assertIn(0x48, armed)  # effect invoke

    def test_fade_armed_palette_form(self):
        # palette color must ride the 0E pp slot, not a rose-pink fallback.
        simple = self.cc.build_fade_armed(simple=0x64)
        pal = self.cc.build_fade_armed(palette=0x12)
        ok, _ = S.mwm.frame_is_valid(simple)
        self.assertTrue(ok)
        ok, _ = S.mwm.frame_is_valid(pal)
        self.assertTrue(ok)
        self.assertEqual(pal[3:5], bytes.fromhex("0E12"))
        self.assertEqual(simple[3], 0x64)
        with self.assertRaises(AssertionError):
            self.cc.build_fade_armed()

    def test_pulse_recolor_is_yellow_blue(self):
        # The re-color target is pure-yellow/blue -- bold non-pink
        # contrast (headband ears are pink-tinted so rose-pink is lost).
        probes = self.cc.build_incant_probes(S)
        combos = [p for p in probes
                  if p["family"] == "pulse" and "-> re-color" in p["name"]]
        self.assertEqual(len(combos), 5)
        for probe in combos:
            recolor = probe["steps"][2]["frames"][0]
            # reset=true fused form: 20 24 0D <right simple> 0E <left|80> ...
            self.assertIn(0x12 | 0x80, recolor)   # left palette 0x12 (yellow)
            self.assertIn(0x61, recolor)          # right simple blue
            self.assertIn("YELLOW", probe["watch"])
        # the explicit retry uses the same pair
        explicit = combos[0]["retries"][1]["frames"][0]
        self.assertIn(0x12 | 0x80, explicit)
        self.assertIn(0x61, explicit)

    def test_probe_names_are_friendly(self):
        # pal:0xNN means nothing to a human observer; every observer-facing
        # string must use the catalog color names instead.
        probes = self.cc.build_incant_probes(S, incant_random=2)
        all_text: list[str] = []
        for probe in probes:
            strings = [probe["name"], probe["watch"]]
            strings += [s.get("label", "") for s in probe["steps"]]
            strings += [s.get("expect", "") for s in probe["steps"]]
            for ret in probe.get("retries", []):
                strings += [ret["label"], ret.get("expect", "")]
            for s in strings:
                self.assertNotIn("pal:", s,
                                 f"{probe['family']}: {s!r} leaks pal:")
            all_text += strings
        # palette shades appear by name across the battery.
        names = " ".join(all_text)
        self.assertIn("pure blue", names)          # pal:0x04
        self.assertIn("rose pink", names)          # pal:0x0D

    def test_every_step_has_expect_text(self):
        probes = self.cc.build_incant_probes(S, incant_random=2)
        for probe in probes:
            for s in probe["steps"]:
                self.assertTrue(s.get("expect"),
                                f"{probe['family']} step {s.get('label')} "
                                "missing expect text")
            for ret in probe.get("retries", []):
                self.assertTrue(ret.get("expect"),
                                f"{probe['family']} retry {ret['label']} "
                                "missing expect text")


if __name__ == "__main__":
    unittest.main()
