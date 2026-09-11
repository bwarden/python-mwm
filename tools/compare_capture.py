#!/usr/bin/env python3
"""Fidelity harness: does mwm-send's transmit stream match the capture?

End-to-end check across every real capture: run ``gen_show_script.py`` on
the capture, run the resulting ``.msh`` through ``mwm-send.py --dump``
(the exact per-member @ms HEX stream it would transmit), and verify that
stream against both the sender's own pipeline and the capture it came from.

Four assertions per capture (default mode):

1. **Self-consistency** -- the subprocess ``--dump`` output is IDENTICAL to
   the in-process parse + ``_plan_publish_times`` schedule (the same code
   path the live sender and ``compare_replay`` use).  Guards the wired
   pipeline against parse/schedule drift.

2. **Cue contract** -- every ``cue hex <phrase>`` line the generator wrote
   expands in mwm-send to exactly the lead-derived countdown
   ``[build_frame([d,*tail]) for d in range(lead,0xF1-1..0xF0-1)+[20]]``
   anchored at the line's ``@ms``.  Guards parser/countdown regressions.

3. **Capture fidelity** -- every non-chain kept row appears in the stream
   exactly as captured, and every captured countdown run is represented by
   a cue whose lead byte and tail match the run's own, whose GO frame is
   actually transmitted within ``_GO_TOLERANCE_MS`` of the run's GO tick
   (the min-gap floor can legitimately serialise near-simultaneous cascades
   a step or two late).

4. **Known drops** -- every skipped row is one of the documented classes
   (``55aa`` heartbeat, beacon receiver rows, lone non-chain idle singles);
   a skipped row that WOULD be a beat (effect-command, chain member,
   exit-tail frame) is a regression.

Timing is reported per capture as the GO skew (the mm between when each
cue's ``20`` is due -- the run's captured GO tick -- and when the stream
actually transmits it); the countdown pre-roll itself is redundancy, so
its per-member cadence is not scored.  ``--no-collapse`` mode drops cues
entirely and instead requires the stream to equal the capture's kept rows
byte-for-byte and in order.

Usage::

    python3 tools/compare_capture.py ../captures/*.mwm
    python3 tools/compare_capture.py --no-collapse ../captures/Feliz_Navidad.mwm
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import statistics
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

sys.path.insert(0, str(TOOLS))
from gen_show_script import (  # noqa: E402
    GO, _BEAT_KINDS, _EXIT_KINDS, _chain_index, _group_beats, _raw_feed_rows,
    admitted_rows, cascade_runs,
)

_MODE = "default"

# Min-gap serialization (--min-gap-ms 30) can push a cue's GO up to two
# clamp steps (60 ms) late when near-simultaneous cascades collide; allow a
# headroom of a full step beyond that so real passes aren't flaky while a
# genuinely dropped GO still fails.
_GO_TOLERANCE_MS = 120.0


def _load_tool(mod: str, path: str, attr: str):
    """Import an attribute from a dash-named tools/ module."""
    spec = importlib.util.spec_from_file_location(mod, TOOLS / path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod] = m
    spec.loader.exec_module(m)   # type: ignore[union-attr]
    return getattr(m, attr)


_parse_show_script = _load_tool("mwm_send", "mwm-send.py", "_parse_show_script")
_plan_publish_times = _load_tool("mwm_send", "mwm-send.py",
                                 "_plan_publish_times")
_mwm = _load_tool("mwm_send", "mwm-send.py", "mwm")
build_frame = _mwm.build_frame

_CUE_RE = re.compile(r"^(?:cue|cascade) hex ([0-9A-Fa-f ]+)$")


def _gen_script(capture: Path, args: argparse.Namespace,
                out_dir: Path) -> Path:
    """Run the real generator on the capture -> .msh (decked flags)."""
    script = out_dir / f"{capture.stem}.msh"
    cmd = [sys.executable, str(TOOLS / "gen_show_script.py"), str(capture)]
    if args.no_trim:
        cmd.append("--no-trim")
    if args.no_collapse:
        cmd.append("--no-collapse")
    if args.skip_prelude:
        cmd.append("--skip-prelude")
    with script.open("w") as fh:
        subprocess.run(cmd, stdout=fh, stderr=subprocess.DEVNULL, check=True)
    return script


def _dump_stream(script: Path) -> list[tuple[float, str]]:
    """mwm-send's exact transmit stream: (@ms, HEX) per frame, no reset."""
    cmd = [sys.executable, str(TOOLS / "mwm-send.py"), "--dump",
           "--no-end-reset", "sequence", str(script)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out: list[tuple[float, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        ms, _, hx = line[1:].partition(" ")   # line is "@<t> <HEX>"
        if not ms:
            continue
        out.append((float(ms), hx.upper()))
    return out


def _schedule_beats(script: Path) -> list[dict]:
    """In-process parse + cum plan + publish times (the sender's own rules)."""
    beats = _parse_show_script(script.read_text())
    plan: list[tuple[int, list[bytes], list[float], bool]] = []
    cum = 0
    for beat in beats:
        if beat["t_ms"] is not None:
            cum = beat["t_ms"]
        plan.append((cum, beat["frames"], beat.get("rel_ms", [0.0]),
                     beat.get("cascade", False)))
    return plan, beats


def _cue_expected(text: str) -> list[tuple[int, list[bytes]]]:
    """Every ``cue hex`` line in the script -> (anchor, member frames) built
    independently from the phrase's lead byte (the contract mwm-send must
    honour)."""
    out: list[tuple[int, list[bytes]]] = []
    for t, blines in _group_beats(text.splitlines()):
        m = _CUE_RE.match(blines[0].strip()) if blines else None
        if not m:
            continue
        phrase = [int(x, 16) for x in m.group(1).split()]
        lead, tail = phrase[0], phrase[1:]
        delays = [*range(lead, 0xF0, -1), GO]
        out.append((t, [build_frame([d, *tail]) for d in delays]))
    return out


def _no_collapse_check(stream, kept, fails, args) -> str:
    emitted = [hx for _, hx in stream]
    cap_kept_hex = [r["hex"] for r in kept]
    if emitted != cap_kept_hex:
        n = min(len(emitted), len(cap_kept_hex))
        first = next((i for i in range(n)
                      if emitted[i] != cap_kept_hex[i]), n)
        fails.append(
            f"stream mismatch at frame {first}: expected "
            f"{cap_kept_hex[first] if first < len(cap_kept_hex) else '<eof>'!r} "
            f"got {emitted[first] if first < len(emitted) else '<eof>'!r} "
            f"(emit {len(emitted)} vs capture-kept {len(cap_kept_hex)})")
    cap_t0 = int(kept[0]["tick"])
    emit_t0 = stream[0][0] if stream else 0.0
    deltas = [abs((t - emit_t0) - (int(row["tick"]) - cap_t0))
              for (t, hx), row in zip(stream, kept)]
    med = statistics.median(deltas) if deltas else 0.0
    p95 = sorted(deltas)[int(0.95 * len(deltas)) - 1] if deltas else 0.0
    return (f"member-tick |delta| "
            f"med {med:.0f} ms / p95 {p95:.0f} ms")


def _fidelity(rows: list[dict], script: Path, stream: list[tuple[float, str]],
              no_collapse: bool = False
              ) -> tuple[list[str], str, list[str]]:
    """Run the four assertions against (capture rows, generated script,
    transmit stream).  Returns (fails, timing, skip_kinds); shared by the
    CLI harness and the unit tests so both score the exact same contract."""
    kept, skipped = admitted_rows(rows)
    fails: list[str] = []
    if not kept:
        return ["no kept beats"], "  -", []

    if no_collapse:
        timing = _no_collapse_check(stream, kept, fails, no_collapse)
    else:
        plan, beats = _schedule_beats(script)
        expected = _plan_publish_times(plan, 30.0, False)
        emitted = [(e["t"], e["hex"]) for e in expected]
        # (1) subprocess --dump == in-process schedule
        if stream != emitted:
            n = min(len(stream), len(emitted))
            first = next((i for i in range(n) if stream[i] != emitted[i]), n)
            fails.append(
                f"--dump != in-process schedule at row {first} "
                f"(dump {stream[first] if first < len(stream) else '<eof>'!r} "
                f"vs schedule "
                f"{emitted[first] if first < len(emitted) else '<eof>'!r})")
        # (2) cue contract: every generated cue expands to the lead-derived
        # countdown, anchored where the generator wrote it.
        cues = _cue_expected(script.read_text())
        beat_by_cue = [b for b in beats if b.get("cascade")]
        if len(cues) != len(beat_by_cue):
            fails.append(
                f"{len(cues)} cue line(s) in the script vs "
                f"{len(beat_by_cue)} parsed cue beat(s)")
        else:
            for (anchor, frames), beat in zip(cues, beat_by_cue):
                if beat["frames"] != frames:
                    fails.append(
                        f"cue @{anchor}ms expands to "
                        f"{' '.join(f.hex() for f in beat['frames'])} not "
                        f"{' '.join(f.hex() for f in frames)} (lead-derived "
                        f"countdown regression)")
        # (3a) every NON-CHAIN kept row appears exactly as captured (the
        # countdown runs collapse to lead-derived cues; their original
        # member hexes are legitimately replaced)
        emitted_mult = {}
        for _, hx in stream:
            emitted_mult[hx] = emitted_mult.get(hx, 0) + 1
        runs = cascade_runs(rows)   # collapse over the FULL rows: the
        # generator batches across non-beat rows (55aa), so RUNS must be
        # built the same way or merged-batch runs get misreported "missing".
        in_runs = {id(r): r for run in runs for r in run["rows"]}
        kept_mult: dict[str, int] = {}
        for r in kept:
            if id(r) in in_runs:
                continue
            kept_mult[r["hex"]] = kept_mult.get(r["hex"], 0) + 1
        missing = [(hx, n - have) for hx, n in kept_mult.items()
                   if (have := emitted_mult.get(hx, 0)) < n]
        if missing:
            fails.append("captured non-chain frames missing from stream: "
                         + ", ".join(f"{hx}x{c}" for hx, c in missing[:5]))
        # (3b) every captured countdown run -> a cue with the same lead/tail
        # and a transmitted GO within _GO_TOLERANCE_MS of the run's tick (the
        # cue's anchor).  The min-gap floor serializes near-simultaneous
        # cascades, so the GO may legitimately land a step or two late.
        stream_go: dict[str, list[int]] = {}
        for t, hx in stream:
            if bytes.fromhex(hx)[1] == GO:
                stream_go.setdefault(hx, []).append(round(t))
        if len(runs) != len(cues):
            fails.append(f"{len(runs)} captured countdown run(s) vs "
                         f"{len(cues)} cue(s) generated")
        go_delta: list[float] = []
        for run, (anchor, frames) in zip(runs, cues):
            if frames[0][1] != run["lead"] or frames[0][2:-1] != run["tail"]:
                fails.append(
                    f"cue @{anchor}ms lead {frames[0][1]:02X} tail "
                    f"{frames[0][2:-1].hex()} != run's lead {run['lead']:02X} "
                    f"tail {run['tail'].hex()}")
            want = build_frame([GO, *run["tail"]]).hex().upper()
            hits = [t for t in stream_go.get(want, [])
                    if abs(t - anchor) <= _GO_TOLERANCE_MS]
            if not hits:
                fails.append(f"run lead {run['lead']:02X} has no GO "
                             f"(delay 0x20, tail {run['tail'].hex()}) "
                             f"within {_GO_TOLERANCE_MS:.0f} ms of cue anchor "
                             f"@{anchor}ms")
            else:
                go_delta.append(min(abs(t - anchor) for t in hits))
        med = statistics.median(go_delta) if go_delta else 0.0
        p95 = sorted(go_delta)[int(0.95 * len(go_delta)) - 1] if go_delta else 0.0
        timing = f"GO skew med {med:.0f} ms / p95 {p95:.0f} ms"

    # documented skips only: the generator drops receiver rows (55aa/beacon)
    # AND lone non-chain idle singles (color-command/command frames outside
    # any countdown run).  A skipped row is a regression only if it WOULD be
    # a beat (effect-command, chain member, exit-tail frame).
    chain = _chain_index(rows)
    ids = {id(r): i for i, r in enumerate(rows)}
    last_effect = max((i for i, r in enumerate(rows)
                       if r["kind"] in _BEAT_KINDS), default=-1)
    unexpected_skip = [s["hex"] for s in skipped
                       if s["kind"] in _BEAT_KINDS
                       or chain[ids[id(s)]]
                       or (ids[id(s)] > last_effect
                           and s["kind"] in _EXIT_KINDS)]
    if unexpected_skip:
        fails.append(f"{len(unexpected_skip)} would-be-beat row(s) skipped: "
                     + ", ".join(unexpected_skip[:5]))
    skip_kinds = sorted(set(r["kind"] for r in skipped))
    return fails, timing, skip_kinds


def _check(capture: Path, args: argparse.Namespace, out_dir: Path) -> str:
    rows = _raw_feed_rows(capture)
    if not rows:
        return (f"{capture.name:48s} SKIP  (no decodable park feed rows "
                f"-- not an .mwm capture?)")
    kept, skipped = admitted_rows(rows)
    if not kept:
        return f"{capture.name:48s} SKIP  (no kept beats)"

    script = _gen_script(capture, args, out_dir)
    stream = _dump_stream(script)
    fails, timing, skip_kinds = _fidelity(rows, script, stream,
                                          args.no_collapse)

    status = "PASS" if not fails else "FAIL"
    misc = (f"skipped {len(skipped)} [{', '.join(skip_kinds) or '-'}]"
            if skipped else "no skipped rows")
    return (f"{capture.name:48s} {status:4s}  emit {len(stream)} "
            f"({misc}); {timing}"
            + ("" if not fails else "   -- " + "; ".join(fails)))


def main() -> None:
    global _MODE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--out-dir", type=Path,
                    default=TOOLS.parent / "analysis" / "replay" / "runs" /
                    "captures",
                    help="staging dir for the generated .msh files")
    ap.add_argument("--no-collapse", action="store_true",
                    help="byte-faithful mode: no cue collapsing")
    ap.add_argument("--no-trim", action="store_true")
    ap.add_argument("--skip-prelude", action="store_true")
    args = ap.parse_args()
    _MODE = "no-collapse" if args.no_collapse else "default"

    args.out_dir.mkdir(parents=True, exist_ok=True)
    results: list[str] = []
    npass = nfail = nskip = 0
    for cap in sorted(args.captures):
        r = _check(cap, args, args.out_dir)
        results.append(r)
        # status token sits in the fixed-width name field + status column;
        # parse past the name (capture names may contain spaces).
        tok = r[48:].lstrip().split(maxsplit=1)[0]
        if tok == "PASS":
            npass += 1
        elif tok == "FAIL":
            nfail += 1
        else:
            nskip += 1
    print("\n".join(results))
    print("-" * 78)
    print(f"RESULT: {npass} PASS, {nfail} FAIL, {nskip} SKIP "
          f"(mode: {_MODE})")
    sys.exit(1 if nfail else 0)


if __name__ == "__main__":
    main()