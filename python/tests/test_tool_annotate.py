"""Offline tests for the annotate_captures capture/decode/annotate loop.

Loads ``tools/annotate_captures.py`` via importlib (it does not import
Home Assistant) and exercises the burst extraction, bundle recognition,
novelty flags, cycle-sibling grouping (same command, only timing moved),
known-demo-beacon prompting, and the annotations log schema against
synthetic Tasmota RESULT lines, so the passive learning loop stays honest
without a rig or IR hardware.
"""

import importlib.util
import io
import json
import queue
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _load():
    spec = importlib.util.spec_from_file_location(
        "annotate_captures", TOOLS / "annotate_captures.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["annotate_captures"] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load()
mwm = A.mwm

BLUE = mwm.build_frame([0x61])
RED = mwm.build_frame([0x64])
GREEN = mwm.build_frame([0x65])
PULSE = mwm.build_frame(
    [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x06])


def _result(data_hex: str | None = None, raw: str | None = None,
            frames=None, protocol: str = "MWM") -> str:
    """A Tasmota RESULT JSON line: IrReceived with RawData and/or Data.

    ``frames`` builds the RawData timings for one frame or a list of frames
    (matching how the rig really delivers a burst).  ``data_hex`` + the
    ``protocol`` gate exercise the Data fallback path.
    """
    ir: dict = {}
    if frames is not None:
        seq = frames if isinstance(frames, (list, tuple)) else [frames]
        ir["RawData"] = ",".join(_raw_payload(f) for f in seq)
    if raw is not None:
        ir["RawData"] = raw
    if data_hex is not None:
        ir["Data"] = "0x" + data_hex
        ir["Protocol"] = protocol
    return json.dumps({"IrReceived": ir})


def _raw_payload(frame: bytes) -> str:
    """RawData timings that decode back to ``frame`` (no carrier prefix)."""
    return mwm.irsend_payload(frame).split(",", 1)[1]


class ExtractFramesTests(unittest.TestCase):
    def test_rawdata_frames_round_trip(self):
        line = _result(frames=[BLUE])
        self.assertEqual(A.extract_frames(mwm, line), [BLUE])

    def test_rawdata_only_recovers_triplet(self):
        companion = mwm.build_frame([0x58, 0xF0, 0xD0, 0x42, 0x06])
        line = _result(frames=[PULSE, companion, PULSE])
        self.assertEqual(
            A.extract_frames(mwm, line),
            [PULSE, companion, PULSE])

    def test_rawdata_wins_and_data_ignored(self):
        # A real Tasmota IrReceived carries BOTH fields: the decoded Data
        # (first bundle message) and the RawData (whole burst).  Our own
        # RawData decode is the source of truth; Data must not double it.
        line = _result(data_hex=RED.hex(), frames=[BLUE])
        self.assertEqual(A.extract_frames(mwm, line), [BLUE])

    def test_data_fallback_only_for_mwm_protocol(self):
        # No RawData: fall back to Tasmota's MWM decode when it is claimed.
        line = _result(data_hex=BLUE.hex(), protocol="MWM")
        self.assertEqual(A.extract_frames(mwm, line), [BLUE])

    def test_data_fallback_rejects_foreign_protocol(self):
        # NEC/unknown decodes on the ear channel are not MWM frames.
        line = _result(data_hex=BLUE.hex(), protocol="NEC")
        self.assertEqual(A.extract_frames(mwm, line), [])

    def test_data_fallback_rejects_invalid_frame(self):
        bad = BLUE[:-1] + bytes([0xFF])  # breaks the CRC
        self.assertFalse(mwm.frame_is_valid(bad)[0])
        line = _result(data_hex=bad.hex(), protocol="MWM")
        self.assertEqual(A.extract_frames(mwm, line), [])

    def test_rawdata_triplet_survives_dedupe(self):
        a = PULSE.hex().upper()
        companion = mwm.build_frame([0x58, 0xF0, 0xD0, 0x42, 0x06])
        a2 = mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x07])
        raw = ",".join([_raw_payload(PULSE),
                        _raw_payload(companion),
                        _raw_payload(a2)])
        frames = A.extract_frames(mwm, _result(raw=raw))
        self.assertEqual(
            [f.hex().upper() for f in frames],
            [a, companion.hex().upper(), a2.hex().upper()])

    def test_irrelevant_reserved_data_ignored(self):
        # Without a Protocol field the Data decode is not trusted at all.
        self.assertEqual(
            A.extract_frames(mwm, json.dumps({"IrReceived": {"Data": "0x01"}})),
            [])

    def test_non_ir_line_yields_nothing(self):
        self.assertEqual(A.extract_frames(mwm, "garbage"), [])
        self.assertEqual(A.extract_frames(mwm, json.dumps({"topic": 1})), [])
        self.assertEqual(A.extract_frames(mwm, json.dumps({})), [])


class BurstRecordTests(unittest.TestCase):
    def test_aba_triplet_becomes_bundle(self):
        companion = mwm.build_frame([0x58, 0xF0, 0xD0, 0x42, 0x06])
        a_prime = PULSE[:-2] + bytes([0x00, 0x00])
        rec = A.burst_record(mwm, [PULSE, companion, a_prime])
        self.assertEqual(rec["kind"], "bundle")
        self.assertIn("A-B-A'", rec["summary"])
        self.assertIn("cycle scale 6 x 200 ms", rec["params"])
        self.assertEqual(rec["hex"], [PULSE.hex().upper()])
        self.assertFalse(rec["novel"])

    def test_mismatched_triplet_is_frames(self):
        rec = A.burst_record(mwm, [BLUE, RED, PULSE])
        self.assertEqual(rec["kind"], "frames")
        self.assertEqual(len(rec["frames"]), 3)
        self.assertTrue(all(f["kind"] == "color-command"
                            for f in rec["frames"][:2]))

    def test_single_color_frame_record(self):
        rec = A.burst_record(mwm, [BLUE])
        self.assertEqual(rec["kind"], "frames")
        self.assertEqual(rec["frames"][0]["summary"],
                         "both ears blue")
        self.assertFalse(rec["frames"][0]["novel"])

    def test_beacon_extras_captured(self):
        beacon = mwm.build_frame([0x42, 0x00, 0x00, 0x48, 0x04,
                                  0x0C, 0x1A])
        rec = A.burst_record(mwm, [beacon])
        f = rec["frames"][0]
        self.assertEqual(f["kind"], "beacon")
        self.assertEqual(f["clock"], 0x1A)
        self.assertEqual(f["demo_effect"], 0x04)


class NoveltyTests(unittest.TestCase):
    def test_invalid_frame_is_novel(self):
        # Good header/length, deliberately corrupted CRC byte.
        bad = BLUE[:-1] + bytes([0xFF])
        self.assertFalse(mwm.frame_is_valid(bad)[0])
        rec = A.burst_record(mwm, [bad])
        self.assertTrue(rec["novel"])
        self.assertEqual(rec["frames"][0]["kind"], "invalid")

    def test_known_wand_program_not_novel(self):
        wand = mwm.build_frame([0x96, 0x19, 0x11, 0x12, 0, 0, 0, 0])
        rec = A.burst_record(mwm, [wand])
        self.assertFalse(rec["novel"])
        self.assertIn("green L/R/off cycle", rec["frames"][0]["summary"])

    def test_undocumented_wand_program_is_novel(self):
        wand = mwm.build_frame([0x96, 0x19, 0x55, 0x55, 0, 0, 0, 0])
        rec = A.burst_record(mwm, [wand])
        self.assertTrue(rec["novel"])
        self.assertIn("undocumented", rec["frames"][0]["summary"])


class AnnotateTests(unittest.TestCase):
    def test_unknown_effect_beacon_condensed_not_logged(self):
        # demo_effect 0x55 is NOT in EFFECT_LABELS -> unknown -> condensed.
        beacon = mwm.build_frame([0x42, 0x00, 0x00, 0x48, 0x55, 0x0C, 0x1A])
        out = io.StringIO()
        quiet = lambda _msg: None  # noqa: E731
        last, handled = A.annotate_line(
            mwm, _result(frames=beacon), "600605", out, "prev",
            print_fn=quiet)
        self.assertEqual((last, handled), ("prev", True))  # no prompt, handled
        self.assertEqual(out.getvalue(), "")  # nothing logged

    def test_known_demo_beacon_prompts_with_interpretation(self):
        # demo_effect 0x04 IS known (pulse) -> must prompt so the observer
        # can confirm/correct our reading, even in the default (non --all) mode.
        beacon = mwm.build_frame([0x42, 0x00, 0x00, 0x48, 0x04, 0x0C, 0x1A])
        out = io.StringIO()
        lines = []
        seen_out = A.annotate_line(
            mwm, _result(frames=beacon), "600605", out, None,
            input_fn=lambda _: "confirmed pulse", print_fn=lines.append,
            seen={})
        self.assertEqual(seen_out, ("confirmed pulse", True))
        self.assertIn("pulse", " ".join(lines))  # our interpretation shown
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(row["note"], "confirmed pulse")
        self.assertEqual(row["kind"], "frames")

    def test_demo_catalog_beacon_prompts_with_interpretation(self):
        # demo_effect 0x88 is NOT in EFFECT_LABELS but IS catalogued in
        # DEMO_BEACONS (observed dominating live demo mode) -> must prompt
        # so the observer can put a verified visual on the unlabelled index.
        beacon = mwm.build_frame([0x42, 0x00, 0x00, 0x48, 0x88, 0x0C, 0x1A])
        out = io.StringIO()
        lines = []
        seen_out = A.annotate_line(
            mwm, _result(frames=beacon), "600605", out, None,
            input_fn=lambda _: "dominating border cycle", print_fn=lines.append,
            seen={})
        self.assertEqual(seen_out, ("dominating border cycle", True))
        shown = " ".join(lines).lower()
        self.assertTrue(
            any(w in shown for w in ("dominates", "color sequence")),
            f"interpretation not shown: {shown}",
        )
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(row["note"], "dominating border cycle")

    def test_repeat_phase_dropped_not_prompted(self):
        # Two beacons of the SAME known demo effect (0x04), different clock
        # tick only: the second is a sibling phase (timing-only delta) and
        # arrives within the repeat window -> dropped, not re-prompted.
        beacon_a = mwm.build_frame(
            [0x42, 0x00, 0x00, 0x48, 0x04, 0x0C, 0x1A])
        beacon_b = mwm.build_frame(
            [0x42, 0x00, 0x00, 0x48, 0x04, 0x0C, 0x2B])
        out = io.StringIO()
        seen: dict = {}
        prompts = {"n": 0}
        def prompt(_):  # should never be reached for a dropped repeat
            prompts["n"] += 1
            return ""
        last, handled = A.annotate_line(
            mwm, _result(frames=beacon_a), "600605", out, None,
            input_fn=prompt, seen=seen)
        self.assertTrue(handled)
        self.assertEqual(prompts["n"], 1)  # first known beacon did prompt

        # Second beacon within the 8s repeat window: same command, new tick.
        last, handled = A.annotate_line(
            mwm, _result(frames=beacon_b), "600605", out, last,
            input_fn=prompt, seen=seen)
        self.assertFalse(handled)  # repeat dropped, no prompt
        self.assertEqual(prompts["n"], 1)  # still only the first prompt

    def test_note_recorded_in_log(self):
        out = io.StringIO()
        last, handled = A.annotate_line(
            mwm, _result(frames=BLUE), "600605", out, None,
            input_fn=lambda _: "wand = set both blue")
        self.assertEqual((last, handled), ("wand = set both blue", True))
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(row["kind"], "frames")
        self.assertEqual(row["note"], "wand = set both blue")
        self.assertEqual(row["hex"], [BLUE.hex().upper()])
        self.assertEqual(row["receiver"], "600605")

    def test_s_reuses_last_note(self):
        out = io.StringIO()
        last, handled = A.annotate_line(
            mwm, _result(frames=BLUE), "600605", out, "repeat x",
            input_fn=lambda _: "s")
        self.assertEqual((last, handled), ("repeat x", True))
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(row["note"], "repeat x")


class CycleGroupingAndPhaseTests(unittest.TestCase):
    """Same command stepped through its cycle must group, and only the
    timing bytes that moved are reported -- that is how the timing knob is
    learned without re-prompting every phase."""

    def _rec(self, frame: bytes) -> dict:
        return A.burst_record(mwm, [frame])

    def test_same_command_different_timing_share_key(self):
        # Identical command, only the D0 42 cadence byte differs: the two
        # bursts are the same key (one cycle, different phase), never novel.
        f1 = mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x06])
        f2 = mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x20])
        self.assertEqual(A.command_key(self._rec(f1)),
                         A.command_key(self._rec(f2)))
        self.assertFalse(self._rec(f2)["novel"])

    def test_different_color_is_different_command(self):
        f1 = mwm.build_frame([0x61])
        f2 = mwm.build_frame([0x64])
        self.assertNotEqual(A.command_key(self._rec(f1)),
                            A.command_key(self._rec(f2)))

    def test_phase_delta_reports_only_moved_timing_cell(self):
        f1 = mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x06])
        f2 = mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x20])
        rec1, rec2 = self._rec(f1), self._rec(f2)
        delta = A._phase_delta(rec1, rec2)
        self.assertIn("D0 42 06", delta)
        self.assertIn("D0 42 20", delta)
        self.assertNotIn("unchanged", delta)

    def test_same_phase_yields_no_delta(self):
        rec = self._rec(mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x06]))
        self.assertEqual(A._phase_delta(rec, rec), "")

    def test_phase_burst_renders_phase_line(self):
        f1 = mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x06])
        f2 = mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x20])
        out = io.StringIO()
        seen: dict = {}
        last, handled = A.annotate_line(
            mwm, _result(frames=f1), "600605", out, None,
            input_fn=lambda _: "pulse at fast cadence 06", seen=seen)
        self.assertTrue(handled)
        lines = []
        last, handled = A.annotate_line(
            mwm, _result(frames=f2), "600605", out, last,
            input_fn=lambda _: "", print_fn=lines.append, seen=seen,
            prev=A.burst_record(mwm, [f1]))
        # Same command, new phase -> dropped (no prompt) but the phase line
        # shows exactly the byte that moved.
        self.assertFalse(handled)
        self.assertTrue(any("phase" in l and "D0 42" in l for l in lines))

    def test_log_row_carries_timing_and_colors(self):
        out = io.StringIO()
        A.annotate_line(
            mwm, _result(frames=BLUE), "600605", out, None,
            input_fn=lambda _: "")
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(row["timing"], [[]])       # plain color: no cells
        self.assertEqual(row["colors"], [["color 61"]])
        self.assertIn("blue", row["summary"])        # human name in summary
        out = io.StringIO()
        A.annotate_line(
            mwm, _result(frames=PULSE), "600605", out, None,
            input_fn=lambda _: "")
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertIn("D0 42 06", row["timing"][0])


class BundleTests(unittest.TestCase):
    """An A-B-A' wand push is ONE IR transmission: a single RawData message
    carries all three frames, and it must annotate as a single bundle --
    one render, one prompt, one log row."""

    def setUp(self):
        self.comp = mwm.build_frame([0x58, 0xF0, 0xD0, 0x42, 0x06])
        # A legitimate A' repeat: the frame still passes CRC so it
        # round-trips through RawData (a broken-CRC tail never decodes).
        self.prime = mwm.build_frame(
            [0x24, 0x48, 0x04, 0x58, 0xF0, 0xD0, 0x42, 0x3E])
        self.assertTrue(mwm.frame_is_valid(self.prime)[0])
        self.assertIsNotNone(mwm.describe_bundle([PULSE, self.comp, self.prime]))

    def test_one_rawdata_message_prompts_once(self):
        # All three frames arrive inside ONE line's RawData; the whole
        # push must prompt a single time as a bundle, not three times.
        line = _result(frames=[PULSE, self.comp, self.prime])
        out = io.StringIO()
        notes = []
        def note(_):
            notes.append("does this prompt")
            return "wand green L/R/off cycle"
        def quiet(_):
            pass
        A.run_annotations(mwm, [line], "600605", out, False,
                          input_fn=note, print_fn=quiet)
        self.assertEqual(notes, ["does this prompt"])
        rows = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "bundle")
        self.assertEqual(rows[0]["note"], "wand green L/R/off cycle")

    def test_bundle_annotation_logs_triplet(self):
        out = io.StringIO()
        A.annotate_burst(
            mwm, [PULSE, self.comp, self.prime], "600605", out, None,
            input_fn=lambda _: "wb wand tilt = green L/R/off cycle")
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(row["kind"], "bundle")
        self.assertEqual(row["note"], "wb wand tilt = green L/R/off cycle")
        self.assertEqual(row["triplet"],
                         [f.hex().upper() for f in (PULSE, self.comp,
                                                    self.prime)])


class LiveQueueTests(unittest.TestCase):
    """Live mode: during a note prompt more bursts pile up, but the observer
    only reports on the most recently heard command -- the queue must
    collapse to the newest burst, dropping the rest."""

    def test_drain_newest_keeps_only_newest(self):
        q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.assertIsNone(A._drain_newest(q))
        q.put(("600605", "line-A"))
        q.put(("600605", "line-B"))
        q.put(("179E4E", "line-C"))
        self.assertEqual(A._drain_newest(q), ("179E4E", "line-C"))
        self.assertIsNone(A._drain_newest(q))
        self.assertTrue(q.empty())


if __name__ == "__main__":
    unittest.main()
