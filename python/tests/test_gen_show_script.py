"""Offline tests for the gen_show_script.py cue-collapse logic + the
mwm-send cue expansion and capture-fidelity contract."""

import argparse
import csv
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))


def _load():
    spec = importlib.util.spec_from_file_location(
        "gen_show_script", TOOLS / "gen_show_script.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_show_script"] = mod
    spec.loader.exec_module(mod)
    return mod


G = _load()


def _load_mwm():
    spec = importlib.util.spec_from_file_location(
        "mwm_send_test", TOOLS / "mwm-send.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mwm_send_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_compare():
    spec = importlib.util.spec_from_file_location(
        "compare_capture_test", TOOLS / "compare_capture.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["compare_capture_test"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load_mwm()
CC = _load_compare()


def _load_park():
    spec = importlib.util.spec_from_file_location(
        "analyze_park", TOOLS / "analyze_park.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["analyze_park"] = mod
    spec.loader.exec_module(mod)
    return mod


PA = _load_park()


def _row(tick: int, hex_: str, kind="effect-command", summary="") -> dict:
    return {"tick": str(tick), "hex": hex_, "kind": kind, "summary": summary}


def _make_run(delays: list[int]) -> list[str]:
    """A real, CRC-complete same-tail chain; byte[1] is the delay."""
    return [G.mwm.build_frame([d, 0x48, 0x85, 0x58, 0x64]).hex().upper()
            for d in delays]


def _expand_cue(line: str) -> list[str]:
    """Reconstruct the member frames a cue line stands for: mwm-send's
    lead-derived countdown (the lead byte down to F1, then the 20 go copy).
    """
    _verb, _, arg = line.strip().partition(" ")
    if arg.startswith("hex "):
        arg = arg[len("hex "):]
    phrase = arg.split()
    lead = int(phrase[0], 16)
    tail = [int(x, 16) for x in phrase[1:]]
    return [G.mwm.build_frame([d, *tail]).hex().upper()
            for d in [*range(lead, 0xF0, -1), 0x20]]


class CascadeRunTests(unittest.TestCase):
    """The countdown-chain detector collapses real park runs."""

    FADE = [
        "94FD48855864AE", "94FC48855864E3", "94FB4885586432",
        "94FA488558647F", "94F9488558641D", "94F848855864D0",
        "94F74885586413", "94F648855864D8", "94F5488558643E",
        "94F44885586401", "94F3488558645C", "94F24885586493",
        "94F14885586448", "94204885586420",
    ]

    def test_real_fade_run_collapses(self):
        delays = G._cascade_delays(self.FADE)
        self.assertIsNotNone(delays)
        self.assertIn(0x20, delays)
        self.assertEqual(sorted(d for d in delays if d != 0x20),
                         list(range(0xF1, 0xFE)))

    def test_paired_interleaved_run_collapses(self):
        # Park interleave: F4/F5, F2/F3, F1/20 arrive as near pairs.
        inter = ["94F9488558641D", "94F44885586401", "94F5488558643E",
                 "94204885586420", "94F14885586448"]
        self.assertIsNotNone(G._cascade_delays(inter))

    def test_missing_go_copy_rejected(self):
        no_go = self.FADE[:-1]
        self.assertIsNone(G._cascade_delays(no_go))

    def test_short_run_rejected(self):
        self.assertIsNone(G._cascade_delays(self.FADE[:2]))

    def test_different_tail_rejected(self):
        mixed = [self.FADE[0], "94FC4885586401", self.FADE[2]]
        self.assertIsNone(G._cascade_delays(mixed))

    def test_phrase_bytes_rebuild_delay_led_tail(self):
        fb = [bytes.fromhex(h) for h in self.FADE]
        # idx of the lowest signal delay (F1) -> delay F1 + tail.
        idx = G._cascade_delays(self.FADE).index(0xF1)
        self.assertEqual(G._phrase_bytes(fb, idx),
                         [0xF1, 0x48, 0x85, 0x58, 0x64])

    def test_duplicate_lead_run_collapses_to_single_canonical_cue(self):
        # The park can air the same delay byte twice -- e.g. three 96-chains
        # run together collapse into ONE run of F4 F3 F2 F1 20 F4 F3 F2 F1
        # 20 F4 F3 F2 F1 20.  The multi-cycle run is ONE cue (the countdown's
        # member set is redundancy): it collapses to a single canonical
        # F4..F1,20 cue anchored at the run's FIRST GO tick, not to a
        # members-clause that reproduces every repeated cycle.
        run = _make_run([0xF4, 0xF3, 0xF2, 0xF1, 0x20,
                         0xF4, 0xF3, 0xF2, 0xF1, 0x20])
        self.assertEqual(G._cascade_delays(run),
                         [0xF4, 0xF3, 0xF2, 0xF1, 0x20,
                          0xF4, 0xF3, 0xF2, 0xF1, 0x20])
        rows = [_row(i * 100, h) for i, h in enumerate(run)]
        lines = G.collapse_beat_lines(rows)
        self.assertEqual(sum(l.startswith("@") for l in lines), 1)
        self.assertEqual(lines[0], "@400")       # the run's FIRST GO tick
        self.assertEqual(lines[1], "cue hex F4 48 85 58 64")
        self.assertEqual(_expand_cue(lines[1]),
                         [G.mwm.build_frame([d, 0x48, 0x85, 0x58, 0x64])
                          .hex().upper()
                          for d in [0xF4, 0xF3, 0xF2, 0xF1, 0x20]])

    def test_cue_anchored_at_run_go_tick(self):
        # The cue's @ms IS the run's GO: the moment the 20 go copy fires.
        # Earlier members pre-roll BEFORE it (they are redundancy), so the
        # line sits on the GO row's tick, not the run start's.
        run = _make_run([0xFC, 0xFB, 0xFA, 0xF9, 0x20])
        rows = [_row(i * 100, h) for i, h in enumerate(run)]
        lines = G.collapse_beat_lines(rows)
        self.assertEqual(sum(l.startswith("@") for l in lines), 1)
        self.assertEqual(lines[0], "@400")
        self.assertEqual(lines[1], "cue hex FC 48 85 58 64")

    def test_cascade_runs_reports_run_geometry(self):
        run = _make_run([0xF4, 0xF3, 0xF2, 0xF1, 0x20])
        rows = [_row(i * 100, h) for i, h in enumerate(run)]
        rs = G.cascade_runs(rows)
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["lead"], 0xF4)
        self.assertEqual(rs[0]["tail"], bytes.fromhex(run[0])[2:-1])
        self.assertEqual(rs[0]["go_tick"], 400)


class ChainAdmissionTests(unittest.TestCase):
    """Mid-show 93-command / 97-colour countdown chains are real cues (the
    park airs them exactly like effect chains), not idle smear: every
    member must be admitted and dumped byte-for-byte.  Lone non-chain
    singles before the last effect-command still drop."""

    CMD = [0x93, 0xF4, 0xD0, 0x42, 0x08]
    GCC = [0x97, 0xF4, 0x24, 0x10]

    def _rows(self, chain: list[int]) -> list[dict]:
        delays = [0xF4, 0xF3, 0xF2, 0xF1, 0x20]
        frames = [G.mwm.build_frame([d, *chain[1:]]).hex().upper()
                  for d in delays]
        return [_row(0, "94FD48855864AE"),
                *[_row(1000 + i * 100, h, "command") for i, h in enumerate(frames)],
                _row(9000, "94F24885586493")]

    def test_command_chain_admitted_and_collapsed(self):
        rows = self._rows(self.CMD)
        kept, skipped = G.admitted_rows(rows)
        self.assertEqual(len(kept), 7)
        self.assertEqual([s["kind"] for s in skipped], [])
        lines = G.collapse_beat_lines(rows)
        cues = [l for l in lines if l.startswith("cue hex ")]
        self.assertEqual(len(cues), 1)
        # The cue carries the captured chain's lead+phrase; its derived
        # countdown matches the captured chain (F4..F1,20) member for member.
        self.assertEqual(cues[0], "cue hex F4 F4 D0 42 08")
        self.assertEqual(_expand_cue(cues[0]),
                         [r["hex"] for r in rows[1:6]])

    def test_color_chain_equally_admitted(self):
        delays = [0xF4, 0xF3, 0xF2, 0xF1, 0x20]
        frames = [G.mwm.build_frame([d, *self.GCC[1:]]).hex().upper()
                  for d in delays]
        rows = [_row(0, "94FD48855864AE"),
                *[_row(1000 + i * 100, h, "color-command")
                  for i, h in enumerate(frames)]]
        kept, skipped = G.admitted_rows(rows)
        self.assertEqual([r["hex"] for r in skipped], [])
        self.assertEqual(len(kept), 6)

    def test_chain_needs_go_copy_to_admit(self):
        # F3 alone (no pair, no go) is idle smear, not a chain.
        lone = G.mwm.build_frame([0xF3, 0xD0, 0x42, 0x08]).hex().upper()
        rows = [_row(0, "94FD48855864AE"),
                _row(1000, lone, "command"),
                _row(9000, "94F24885586493")]
        kept, skipped = G.admitted_rows(rows)
        self.assertEqual(len(kept), 2)
        self.assertEqual([s["hex"] for s in skipped], [lone])

    def test_lone_idle_single_mid_show_dropped(self):
        rows = [_row(0, "94FD48855864AE"),
                _row(1000, "92F3246010", "color-command",
                     "delay ~300 ms; reset/override; both ears off"),
                _row(9000, "94F24885586493")]
        kept, skipped = G.admitted_rows(rows)
        self.assertEqual(len(kept), 2)
        self.assertEqual([s["hex"] for s in skipped], ["92F3246010"])

    def test_exit_tail_still_kept_after_last_effect(self):
        rows = [_row(0, "94FD48855864AE"),
                _row(7000, "94F24885586493"),
                _row(8000, "9220656EAD", "color-command",
                     "start immediately; both ears magenta; left ear yellow")]
        kept, skipped = G.admitted_rows(rows)
        self.assertEqual([r["hex"] for r in kept],
                         ["94FD48855864AE", "94F24885586493",
                          "9220656EAD"])
        self.assertEqual(skipped, [])


class LoadSourceSpreadTests(unittest.TestCase):
    """Two countdown members packed onto ONE capture line share a single
    recorder timestamp; ``load_source(path, spread_ms)`` de-flattens frame
    i to ``i*spread_ms`` in document order so they keep their real 100 ms
    cadence instead of collapsing onto one tick."""

    PACKED = ("00000000: 9CFC240D290315030D4883D00EFF02 "
              "9CFB240D290315030D4883D00EFF5C\n")

    def _rows(self, spread_ms: int) -> list[dict]:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "packed.mwm"
            p.write_text(self.PACKED)
            return PA.load_source(p, spread_ms=spread_ms)

    def test_default_keeps_packed_members_on_one_tick(self):
        rows = self._rows(0)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["tick"] for r in rows}, {0})
        # Both members share the recorder's single tick; the (tick, hex)
        # tie-break loses the doc order the park aired them in.
        self.assertEqual(sorted(r["hex"] for r in rows),
                         ["9CFB240D290315030D4883D00EFF5C",
                          "9CFC240D290315030D4883D00EFF02"])

    def test_spread_recovers_100ms_cadence_in_document_order(self):
        rows = self._rows(100)
        self.assertEqual([r["tick"] for r in rows], [0, 100])
        # The countdown stepping survives: F8->F7 is one 100 ms step apart,
        # exactly as the park aired them.
        self.assertEqual([bytes.fromhex(r["hex"])[1] for r in rows],
                         [0xFC, 0xFB])
        self.assertEqual([r["hex"] for r in rows],
                         ["9CFC240D290315030D4883D00EFF02",
                          "9CFB240D290315030D4883D00EFF5C"])


class CollapseBeatTests(unittest.TestCase):
    def test_collapse_splits_on_non_effect_row(self):
        rows = [_row(0, h) for h in CascadeRunTests.FADE]
        rows.insert(len(rows) // 2, _row(50, "55AA0601070316032A", "55aa"))
        lines = G.collapse_beat_lines(rows)
        cues = [l for l in lines if l.startswith("cue ")]
        # The 55aa boundary splits the run; only the tail half (F5..F1,20)
        # holds both a signal delay and the go copy, so exactly one cue.
        self.assertEqual(len(cues), 1)
        self.assertIn("cue hex ", cues[0])

    def test_unrelated_effect_commands_not_merged(self):
        b = "94FD48855864AE"
        rows = [_row(0, b), _row(1, "9C20240D610E8858F04804D0420670"),
                _row(2, b)]
        lines = G.collapse_beat_lines(rows)
        cues = [l for l in lines if l.startswith("cue ")]
        self.assertEqual(cues, [])

    def test_exit_color_commands_emitted_as_hex(self):
        # the revert-to-demo state setters -- 'both ears off' then the
        # magenta/yellow go copy -- sit in the capture's EXIT TAIL (after
        # the last effect-command) and must stay on the timeline, not drop
        rows = [_row(0, "94FD48855864AE"),
                _row(100, "92F3246010", "color-command",
                     "delay ~300 ms; reset/override; both ears off"),
                _row(200, "9220656EAD", "color-command",
                     "start immediately; both ears magenta; left ear yellow")]
        lines = G.collapse_beat_lines(rows)
        self.assertEqual(sum(l.startswith("@") for l in lines), 3)
        self.assertEqual(
            [l for l in lines if l.startswith("hex ")],
            ["hex 94FD48855864AE", "hex 92F3246010", "hex 9220656EAD"])

    def test_exit_command_delay_chain_collapses(self):
        # the 95-family white countdown (a 'command' kind) in the exit tail
        # ends in a GO and collapses like any delay-led run -- including
        # the >=1400 ms delays (0xFE, 0xFF) the real MRDF0007 tail uses
        white = ["95FF9771D005FFE1", "95FC9771D005FFB8",
                 "95F89771D005FF64", "95F39771D005FF9C",
                 "95209771D005FF01", "95F19771D005FFF2"]
        rows = [_row(0, "94FD48855864AE"),
                *[_row(100 + i * 100, h, "command")
                  for i, h in enumerate(white)]]
        lines = G.collapse_beat_lines(rows)
        self.assertEqual(sum(l.startswith("@") for l in lines), 2)
        self.assertTrue(any(l.startswith("cue hex ") for l in lines))

    def test_mid_capture_exit_rows_still_dropped(self):
        # a color-command/command row BEFORE the last effect-command is
        # idle-smear (solid-crossfade noise), not an exit: it must drop
        rows = [_row(0, "94FD48855864AE"),
                _row(100, "92F3246010", "color-command",
                     "delay ~300 ms; reset/override; both ears off"),
                _row(200, "9C20240D610E8858F04804D0420670")]
        lines = G.collapse_beat_lines(rows)
        self.assertEqual(sum(l.startswith("@") for l in lines), 2)
        self.assertNotIn("92F3246010", "".join(lines))

    def test_55aa_not_emitted(self):
        rows = [_row(0, "55AA0601070316032A", "55aa"),
                _row(1000, "55AA0601070316032A", "55aa")]
        self.assertEqual(G.collapse_beat_lines(rows), [])


class PhaseCompressTests(unittest.TestCase):
    """Long static loops (one cue, or an A/B red/green-style pair) shrink
    to ~10 s with a few reps; transitions keep their real timing."""

    RED = "hex 9E9124A4B0225FC348D013D02AFFD043FE"
    GREEN = "hex 9E91183CA4315AA748D020D02AFFD029FE"

    def test_identical_loop_compresses_to_target(self):
        beats = [(t, [self.RED]) for t in [0, 4161, 8322, 12483, 16644] * 10]
        # last real tick: 200 ticks of 4161 spacing ~ 832k ms span
        out = G._compress_phases(beats, target_ms=10000)
        # folds replay on the capture's OWN cadence (4161 ms), never denser;
        # capped at span ~10000 -> 3 reps at 4161, last at 8322
        self.assertEqual(len(out), 3)
        self.assertEqual([t for t, _ in out], [0, 4161, 8322])
        self.assertTrue(all(bl[0] == self.RED for _, bl in out))

    def test_ab_alternation_compresses_keeping_both(self):
        beats = [(i * 4161, [self.RED if i % 2 == 0 else self.GREEN])
                 for i in range(40)]
        out = G._compress_phases(beats, target_ms=10000)
        # cycle cadence 8322 -> 2 cycles fit under 10000; the final B rides
        # one intra-cycle gap past the last cycle start
        self.assertEqual(len(out), 4)
        self.assertLessEqual(out[-2][0], 10000)  # last cycle START under cap
        keys = {bl[0] for _, bl in out}
        self.assertEqual(keys, {self.RED, self.GREEN})

    def test_ab_cycle_sampling_stays_perfectly_alternating(self):
        # Sampling at cycle granularity: the fold must keep an even
        # A,B,A,B,... rhythm, never a lilted grab of individual beats.
        beats = [(i * 4161, [self.RED if i % 2 == 0 else self.GREEN])
                 for i in range(40)]
        out = G._compress_phases(beats, target_ms=10000)
        seq = [bl[0] for _, bl in out]
        self.assertEqual(seq, [self.RED, self.GREEN] * (len(seq) // 2))
        ts = [t for t, _ in out]
        # uniform grid on the real 4161 cadence: every inter-beat delta
        # equal, so the flash reads as an even alternation
        deltas = [b - a for a, b in zip(ts, ts[1:])]
        self.assertEqual(set(deltas), {4161})

    def test_split_shows_split_at_long_static_runs(self):
        # a 3-minute idle run (cue at 2 s), then a show, then the tail idle
        idle = lambda: ["hex 94FD48855864AE"]          # noqa: E731
        busy = ["hex 9EB6CD098D06D9573000110205",      # noqa: E731
                "hex 8F261009B2AC1D77", "hex 4F1488558029"]
        beats = ([(0 + i * 2000, idle()) for i in range(90)]   # static
                 + [(181000, [busy[0]]), (183000, [busy[1]]),
                    (198000, [busy[2]]), (199000, [busy[0]]),
                    (201000, [busy[1]]), (203500, [busy[2]]),
                    (220000, [busy[0]]), (221000, [busy[1]])]
                 + [(300000 + i * 3000, idle()) for i in range(50)])  # tail
        shows = G._split_shows(beats, min_static_ms=10000,
                               lead_in_ms=10000)
        self.assertEqual(len(shows), 1)              # tail static dropped
        lead, body, gap = shows[0]
        self.assertTrue(lead)                        # pre-show idle becomes lead
        # lead replays the idle cue on its real 2000 ms cadence, capped ~10 s
        self.assertLessEqual(lead[-1][0], 10000)
        self.assertEqual(lead[0][1][0], idle()[0])
        self.assertEqual(len(body), 8)               # every genuine beat kept
        self.assertEqual(body[0][0], 181000)         # real timing preserved
        # real captured gap between the lead-in's source run and the body
        self.assertEqual(gap, 181000 - 178000)

    def test_split_shows_keeps_first_show_without_lead(self):
        busy = ["hex 9EB6CD098D06D9573000110205",
                "hex 4F1488558029", "hex 8FA61009B2AC1D77"]
        beats = ([(t, [busy[k]]) for k, t in enumerate([0, 500, 2000])]
                 + [(10000 + i * 2000, [busy[1]]) for i in range(30)])
        # the head trio is distinct commands too short to be static: it IS a
        # show with an empty lead-in (capture starts mid-show); the 60 s
        # run after it is a boundary with no show behind it -> dropped
        shows = G._split_shows(beats, min_static_ms=10000, lead_in_ms=10000)
        self.assertEqual(len(shows), 1)
        lead, body, gap = shows[0]
        self.assertEqual(lead, [])
        self.assertEqual(gap, 0)
        self.assertEqual([bl[0] for _, bl in body],
                         [b for b in busy])

    def test_short_run_untouched(self):
        beats = [(0, [self.RED]), (4161, [self.RED]), (8322, [self.RED])]
        out = G._compress_phases(beats, target_ms=10000)
        self.assertEqual(out, beats)

    def test_transitions_stay_real(self):
        beats = [
            (0, [self.RED]), (4161, [self.RED]), (8322, [self.RED]),
            (15000, ["hex 94154885586416"]), (4161000, [self.GREEN]),
        ]
        out = G._compress_phases(beats, target_ms=10000)
        # the trio is too short to compress; the lone cue at 15 s is a
        # one-beat transition and the cue at 4.16e6 a one-beat phase
        self.assertEqual(out, beats)

    def test_phase_boundary_keeps_following_beat_after_run(self):
        beats = [(i * 4161, [self.RED]) for i in range(30)]
        beats.append((200000, [self.GREEN]))
        out = G._compress_phases(beats, target_ms=10000)
        self.assertEqual(out[-1], (200000, [self.GREEN]))


class GapCapTests(unittest.TestCase):
    """Long silent stretches (empty air between commands) shrink to the
    cap; everything at or under the cap keeps its real spacing."""

    CUE = "hex 94154885586416"

    def test_long_gap_clamped_preserves_later_relative_spacing(self):
        beats = [(0, [self.CUE]), (310000, [self.CUE]), (311000, [self.CUE])]
        out = G._cap_gaps(beats, max_gap_ms=30000)
        self.assertEqual([t for t, _ in out], [0, 30000, 31000])

    def test_gap_at_cap_untouched(self):
        beats = [(0, [self.CUE]), (30000, [self.CUE]), (31000, [self.CUE])]
        out = G._cap_gaps(beats, max_gap_ms=30000)
        self.assertEqual(out, beats)

    def test_consecutive_long_gaps_each_clamped(self):
        beats = [(0, [self.CUE]), (310000, [self.CUE]),
                 (620000, [self.CUE]), (630000, [self.CUE])]
        out = G._cap_gaps(beats, max_gap_ms=30000)
        # two 310 s gaps -> 30 s each (520 s of excess), the trailing
        # 10 s gap is under the cap and keeps its raw spacing
        self.assertEqual([t for t, _ in out], [0, 30000, 60000, 70000])


class GroupSkipRetimeTests(unittest.TestCase):
    """Grouping, prelude-skipping and renormalisation helpers."""

    PRELUDE = "9E919025290215194882D00EFFD005FF3F"
    CUE = "94FD48855864AE"

    def test_group_beats_keeps_command_and_comments(self):
        lines = ["@100", "hex AA", "  # note", "@200", "cue hex F1 BB"]
        beats = G._group_beats(lines)
        self.assertEqual(beats, [
            (100, ["hex AA", "  # note"]),
            (200, ["cue hex F1 BB"]),
        ])

    def test_skip_prelude_drops_leading_identical_run(self):
        beats = [(0, ["hex " + self.PRELUDE]),
                 (4161, ["hex " + self.PRELUDE]),
                 (8322, ["hex " + self.PRELUDE]),
                 (99999, ["hex " + self.CUE])]
        kept = G._skip_prelude(beats)
        self.assertEqual([t for t, _ in kept], [99999])
        self.assertEqual(kept[0][1][0], "hex " + self.CUE)

    def test_skip_prelude_keeps_lone_head_cue(self):
        beats = [(0, ["hex " + self.CUE]),
                 (99999, ["hex " + self.PRELUDE])]
        kept = G._skip_prelude(beats)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0][1][0], "hex " + self.CUE)

    def test_skip_prelude_passes_cue_head(self):
        beats = [(0, ["cue hex F1 48 85 58 64"]),
                 (99999, ["hex " + self.PRELUDE])]
        kept = G._skip_prelude(beats)
        self.assertEqual(len(kept), 2)

    def test_retime_trims_to_first_beat(self):
        beats = [(111950, ["hex " + self.CUE]),
                 (139086, ["cue hex F1 48 85"])]
        out = G._retime(beats, trim=True, lead_in=0, offset=0)
        self.assertEqual([t for t, _ in out], [0, 27136])

    def test_retime_honours_lead_in(self):
        beats = [(111950, ["hex " + self.CUE]), (139086, ["hex " + self.CUE])]
        out = G._retime(beats, trim=True, lead_in=500, offset=0)
        self.assertEqual([t for t, _ in out], [500, 27636])

    def test_retime_no_trim_keeps_absolute_ticks_plus_offset(self):
        beats = [(111950, ["hex " + self.CUE]), (139086, ["hex " + self.CUE])]
        out = G._retime(beats, trim=False, lead_in=0, offset=300)
        self.assertEqual([t for t, _ in out], [112250, 139386])

    def test_retime_keeps_opening_countdown_go_offset(self):
        # A show that opens with a collapsed countdown cue (its @ IS the
        # run's GO tick) anchors on the run's first countdown member, so
        # the GO lands at the recording's own 1100 ms offset, not at 0 --
        # the opening countdown airs.
        beats = [(1500, ["cue hex FC 48 85 58 64"]),
                 (40000, ["hex " + self.CUE])]
        out = G._retime(beats, trim=True, lead_in=0, offset=0,
                        head_pre_roll_ms=1100)
        self.assertEqual([t for t, _ in out], [1100, 39600])

    def test_head_pre_roll_matches_opening_run(self):
        rows = [
            {"tick": 1631939, "hex": "9BFD48855864", "kind": "effect-command",
             "summary": ""},
            {"tick": 1632071, "hex": "94FD48855864", "kind": "effect-command",
             "summary": ""},
            {"tick": 1633919, "hex": "9C2048855864", "kind": "effect-command",
             "summary": ""},
        ]
        lines = G.collapse_beat_lines(rows)
        beats = G._group_beats(lines)
        self.assertTrue(beats[0][1][0].startswith("cue "))
        self.assertEqual(G._head_pre_roll(rows, beats), 1980)

    def test_head_pre_roll_zero_for_hex_opening(self):
        beats = [(0, ["hex " + self.CUE])]
        self.assertEqual(G._head_pre_roll([], beats), 0)


class MwmSendCueTests(unittest.TestCase):
    """The ``cue``/``cascade`` show-script verb: lead-derived countdown,
    exact legacy ``members`` sets, uniform pacing and error handling."""

    def _beat(self, text, **kw):
        return M._parse_show_script(text, **kw)[0]

    def _delays(self, beat):
        return [f[1] for f in beat["frames"]]

    def test_cue_hex_expands_lead_derived_countdown(self):
        # F4-led cue -> F4 F3 F2 F1 20, each pre-rolling -(d & 0x0F)*100 ms
        # (the F? low-nibble delay) before the @ms target the go copy rides
        # (ears fire at receipt + delay).
        beat = self._beat("@400\ncue hex F4 48 85 58 64")
        self.assertEqual(beat["t_ms"], 400)
        self.assertTrue(beat["cascade"])
        self.assertEqual(self._delays(beat), [0xF4, 0xF3, 0xF2, 0xF1, 0x20])
        self.assertEqual(beat["rel_ms"],
                         [-400.0, -300.0, -200.0, -100.0, 0.0])
        self.assertEqual(beat["frames"][0],
                         M.build_frame([0xF4, 0x48, 0x85, 0x58, 0x64]))

    def test_cascade_alias_identical_to_cue(self):
        self.assertEqual(M._parse_show_script("@400\ncascade hex F4 48 85 58 64"),
                         M._parse_show_script("@400\ncue hex F4 48 85 58 64"))

    def test_members_clause_builds_exact_legacy_set(self):
        # ``members`` overrides the lead-derived countdown with a captured
        # exact set; the 20 go copy still transmits LAST.
        beat = self._beat("@400\ncue hex F2 24 67 58 19 48 84 members F1 20")
        self.assertEqual(self._delays(beat), [0xF2, 0xF1, 0x20])
        self.assertEqual(beat["rel_ms"], [-200.0, -100.0, 0.0])

    def test_members_order_normalised_go_last(self):
        beat = self._beat("@400\ncue hex F2 24 67 58 19 48 84 members 20 F1")
        self.assertEqual(self._delays(beat), [0xF2, 0xF1, 0x20])

    def test_cascade_ms_uniform_pacing_from_anchor(self):
        # ``--cascade-ms`` opts back into uniform pacing FROM the @ms anchor
        # (lead rides @ms; the go copy lands last at (n-1)*step).
        beat = self._beat("@400\ncue hex F2 48 85 58 64", cascade_ms=500)
        self.assertEqual(self._delays(beat), [0xF2, 0xF1, 0x20])
        self.assertEqual(beat["rel_ms"], [0.0, 500.0, 1000.0])

    def test_cascade_full_forces_fd_led_capture_chain(self):
        beat = self._beat("@400\ncue hex F2 48 85 58 64", cascade_full=True)
        self.assertEqual(self._delays(beat),
                         [*range(0xFD, 0xF0, -1), 0x20])

    def test_short_lead_cue_still_valid(self):
        # A F1-lead cue folds to just its own member + the go copy (the
        # lead-derived range narrows to F1,20; nothing pre-rolls).
        beat = self._beat("@400\ncue hex F1 48 85 58 64")
        self.assertEqual(self._delays(beat), [0xF1, 0x20])

    def test_hex_beat_not_a_cue(self):
        beat = self._beat("@100\nhex 94FD48855864AE")
        self.assertEqual(beat["t_ms"], 100)
        self.assertFalse(beat["cascade"])
        self.assertEqual(beat["rel_ms"], [0.0])
        self.assertEqual(beat["frames"][0].hex().upper(), "94FD48855864AE")

    def test_beat_without_preceding_at_is_cumulative(self):
        # The parser tags a @-less beat whose schedule the PLAN builder
        # accumulates onto the previous beat's target (t_ms None here, the
        # cumulative timing applied downstream).
        beats = M._parse_show_script("@3000\ncue hex F4 48 85 58 64\n"
                                     "hex 94FD48855864AE")
        self.assertEqual(beats[0]["t_ms"], 3000)
        self.assertIsNone(beats[1]["t_ms"])

    def test_non_delay_lead_rejected(self):
        with self.assertRaises(ValueError):
            M._parse_show_script("@400\ncue hex 30 48 85 58 64")

    def test_bad_hex_rejected(self):
        with self.assertRaises(ValueError):
            M._parse_show_script("@400\ncue hex zz 48 85 58 64")

    def test_bad_members_rejected(self):
        with self.assertRaises(ValueError):
            M._parse_show_script("@400\ncue hex F4 AA members 20 xx")

    def test_unknown_command_rejected(self):
        with self.assertRaises(ValueError):
            M._parse_show_script("@400\nbogus x")

    def test_dangling_at_without_beat_rejected(self):
        with self.assertRaises(ValueError):
            M._parse_show_script("@0")


class ParkSampleParseTests(unittest.TestCase):
    """Every shipped samples/*.msh parses through the real mwm-send parser,
    and every cue line expands to its documented lead-derived countdown."""

    def test_every_sample_parses(self):
        files = sorted((ROOT / "samples").glob("*.msh"))
        self.assertGreater(len(files), 0)
        for p in files:
            with self.subTest(p.name):
                beats = M._parse_show_script(p.read_text())
                self.assertGreater(len(beats), 0)
                for b in beats:
                    self.assertEqual(len(b["rel_ms"]), len(b["frames"]))
                    if b["cascade"]:
                        # go copy is the anchor: last, and at @ms itself.
                        self.assertEqual(b["frames"][-1][1], 0x20)
                        self.assertEqual(b["rel_ms"][-1], 0.0)

    def test_every_cue_line_matches_lead_derived_expansion(self):
        for p in sorted((ROOT / "samples").glob("*.msh")):
            with self.subTest(p.name):
                text = p.read_text()
                blocks = G._group_beats(text.splitlines())
                cue_lines = [line.strip()
                             for _, bl in blocks
                             for line in bl
                             if line.strip().split(maxsplit=1)
                             and line.strip().split(maxsplit=1)[0]
                             in ("cue", "cascade")]
                cascade = [b for b in M._parse_show_script(text)
                           if b["cascade"]]
                self.assertEqual(len(cue_lines), len(cascade))
                for line, beat in zip(cue_lines, cascade):
                    want = _expand_cue(line)
                    got = [f.hex().upper() for f in beat["frames"]]
                    self.assertEqual(got, want, p.name)


class ParkCaptureFidelityTests(unittest.TestCase):
    """For each captured park source in the tracked frames.tsv: the .msh the
    real generator emits, run through the real mwm-send schedule, must be
    functionally equivalent to the capture (compare_capture's four
    assertions, allowing countdown redundancy + min-gap serialization)."""

    FRAMES = ROOT / "analysis" / "park" / "frames.tsv"

    def _rows(self, source):
        with self.FRAMES.open() as fh:
            return [r for r in csv.DictReader(fh, delimiter="\t")
                    if r["source"] == source]

    def test_each_park_source_script_matches_capture(self):
        with self.FRAMES.open() as fh:
            sources = sorted({r["source"]
                              for r in csv.DictReader(fh, delimiter="\t")})
        self.assertGreater(len(sources), 0)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for src in sources:
                with self.subTest(src):
                    rows = self._rows(src)
                    script = out / f"{src}.msh"
                    cmd = [sys.executable,
                           str(TOOLS / "gen_show_script.py"),
                           "--source", src, str(self.FRAMES)]
                    with script.open("w") as fh:
                        subprocess.run(cmd, stdout=fh,
                                       stderr=subprocess.DEVNULL, check=True)
                    stream = CC._dump_stream(script)
                    fails, _timing, _kinds = CC._fidelity(rows, script,
                                                          stream)
                    self.assertEqual(fails, [], script.name)


class ExternalCaptureFidelityTests(unittest.TestCase):
    """The ../captures/*.mwm corpus is a local-only test set (NOT shipped):
    run the same end-to-end fidelity harness over every capture present, and
    skip gracefully when the directory isn't there (CI)."""

    def test_all_external_captures_pass(self):
        caps = sorted((ROOT.parent / "captures").glob("*.mwm"))
        if not caps:
            self.skipTest("no ../captures/*.mwm present (not shipped) "
                          "-- end-to-end capture fidelity not run")
        args = argparse.Namespace(no_trim=False,
                                  no_collapse=False, skip_prelude=False)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for cap in caps:
                with self.subTest(cap.name):
                    r = CC._check(cap, args, out)
                    tok = r[48:].lstrip().split(maxsplit=1)[0]
                    self.assertEqual(tok, "PASS", r)


if __name__ == "__main__":
    unittest.main()