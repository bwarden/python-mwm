#!/usr/bin/env python3
"""Dry-run every generated show-script against the capture it came from.

For each generated ``samples/park-*.showN.msh`` / ``samples/replay/*.msh``
(``make samples`` output), resolve the source capture from the script's
``# sources:`` header line, then compare the codes ``mwm-send`` would
actually broadcast (its ``--dump`` transmit stream) against the source
rows in ``analysis/park/frames.tsv``:

1. **Coverage** -- every emitted frame must trace to a captured frame: a
   literal ``hex`` beat matches its captured row byte-for-byte; a countdown
   cue's members are the canonical ``CASCADE_DELAYS`` chain over the
   captured run's phrase tail, and its ``20`` GO frame must appear in the
   capture within ``_GO_TOLERANCE_MS`` of the run's GO tick.

2. **No invented codes** -- the emitted set minus the generated countdown
   = the captured kept rows, nothing else.

3. **No overlap with the next command** -- consecutive publishes respect
   the ``--min-gap-ms`` floor (the sender clamps same-tick clusters apart),
   and a cue's countdown pre-roll members must NOT land after the next
   beat's ``@ms`` (a member whose true slot was pushed off by the clamp is
   dropped, not re-timed into the next command's window).

Patterns are reported rather than asserted byte-for-byte: compressed
(``--phase-compress``/``--gap-cap``) show splits retime and fold, so only
the code content should track the capture; the verbatim ``samples/replay``
scripts keep real ticks and should match closely.

Usage::

    python3 tools/compare_samples_dryrun.py
    python3 tools/compare_samples_dryrun.py samples/park-MRDF0008.TXT.show2.msh
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
ROOT = TOOLS.parent
TSV = ROOT / "analysis" / "park" / "frames.tsv"

sys.path.insert(0, str(TOOLS))
from gen_show_script import (  # noqa: E402
    GO, _EXIT_KINDS, _BEAT_KINDS, _chain_index, admitted_rows, cascade_runs,
    collapse_beat_lines,
)

MIN_GAP_MS = 30.0   # mirrors mwm-send's --min-gap-ms default
_GO_TOLERANCE_MS = 120.0

_SOURCE_RE = re.compile(r"^# sources: (.+)$")
_STREAM_RE = re.compile(r"^@(-?[0-9.]+) ([0-9A-F]+)$")


def _load_tool(name: str, path: str, attr: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)   # type: ignore[union-attr]
    return getattr(m, attr)


_parse_show_script = _load_tool("mwm_send", "mwm-send.py", "_parse_show_script")
_plan_publish_times = _load_tool("mwm_send", "mwm-send.py",
                                 "_plan_publish_times")
_mwm = _load_tool("mwm_send", "mwm-send.py", "mwm")
build_frame = _mwm.build_frame
CASCADE_DELAYS = _mwm.CASCADE_DELAYS


def _rows_for_source(source: str) -> list[dict]:
    """Frames.tsv rows for one source, in capture order."""
    import csv
    with TSV.open(newline="") as fh:
        rows = [r for r in csv.DictReader(fh, delimiter="\t")
                if r["source"] == source]
    return rows


def _source_of(script: Path) -> str | None:
    m = _SOURCE_RE.match(script.read_text(errors="replace").splitlines()[1])
    return m.group(1).strip() if m else None


def _dump_stream(script: Path) -> list[tuple[float, str]]:
    """mwm-send's exact transmit stream (@ms, HEX), end reset excluded."""
    cmd = [sys.executable, str(TOOLS / "mwm-send.py"), "--dump",
           "--no-end-reset", "sequence", str(script)]
    out: list[tuple[float, str]] = []
    for line in subprocess.run(cmd, capture_output=True, text=True,
                               check=True).stdout.splitlines():
        m = _STREAM_RE.match(line.strip())
        if m:
            out.append((float(m.group(1)), m.group(2).upper()))
    return out


def _schedule(script: Path) -> list[dict]:
    """The sender's own per-member plan (@ms, hex, team, cascade)."""
    beats = _parse_show_script(script.read_text())
    plan = []
    cum = 0
    for beat in beats:
        if beat["t_ms"] is not None:
            cum = beat["t_ms"]
        plan.append((cum, beat["frames"], beat.get("rel_ms", [0.0]),
                     beat.get("cascade", False)))
    return _plan_publish_times(plan, MIN_GAP_MS, False)


def _check(script: Path) -> str:
    src = _source_of(script)
    if src is None:
        return f"{script.name:52s} SKIP  (no '# sources:' header)"
    rows = _rows_for_source(src)
    if not rows:
        return f"{script.name:52s} SKIP  (no rows for source {src})"

    kept, _skipped = admitted_rows(rows)
    kept_hex = {r["hex"].upper() for r in kept}
    runs = cascade_runs(rows)
    run_tails = {r["tail"] for r in runs}
    run_member_hex = {r["hex"].upper() for run in runs for r in run["rows"]}
    go_frame_by_tail = {
        r["tail"]: build_frame([GO, *r["tail"]]).hex().upper() for r in runs}

    plan = _schedule(script)
    dump = _dump_stream(script)
    verbatim = script.parent.name == "replay"

    fails: list[str] = []

    # (1) plan == dump (the dry-run stream is what the sender transmits)
    got = [(e["t"], e["hex"]) for e in plan]
    if got != dump:
        n = min(len(got), len(dump))
        first = next((i for i in range(n) if got[i] != dump[i]), n)
        fails.append(f"plan != dump at {first} "
                     f"(plan {got[first] if first < len(got) else '<eof>'} vs "
                     f"dump {dump[first] if first < len(dump) else '<eof>'})")

    # (2) coverage: every emitted frame traces to the capture
    generated_tails = set()
    for e in plan:
        hx = e["hex"]
        if e["cascade"]:
            delay, tail = int(hx[2:4], 16), bytes.fromhex(hx)[2:-1]
            if e["step"] < 0:            # pre-roll member
                if tail not in run_tails or delay not in range(0xF1, 0x100):
                    fails.append(f"cascade pre-roll {hx} not a canonical "
                                 f"member of any captured run (delay {delay:02X} "
                                 f"tail {tail.hex()})")
                if e["step"] != -(delay & 0x0F) * 100:
                    fails.append(f"cascade member {hx} step {e['step']} != "
                                 f"delay-led {-(delay & 0x0F) * 100}")
                generated_tails.add(tail)
            else:                        # the 20 GO copy
                want = go_frame_by_tail.get(tail)
                if want is None or want != hx:
                    fails.append(f"GO frame {hx} (tail {tail.hex()}) matches "
                                 f"no captured countdown run")
                elif hx not in kept_hex:
                    fails.append(f"GO {hx} not present in capture rows")
        elif hx not in kept_hex:
            fails.append(f"literal beat {hx} not in capture")

    # (3) no invented codes: emitted set - generated countdown = kept rows
    generated = {e["hex"] for e in plan if e["cascade"]}
    invented = {hx for _t, hx in dump if hx not in kept_hex and hx not in generated}
    if invented:
        fails.append(f"{len(invented)} emitted frame(s) from no captured row: "
                     + ", ".join(sorted(invented)[:5]))
    missing = kept_hex - {hx for _, hx in dump} - generated
    # Run members fold into cues and are regenerated (or dropped by the
    # min-gap clamp), so only NON-run kept rows must appear literally --
    # and only verbatim replay scripts cover the whole capture; a split
    # show covers just its own slice, so its literal coverage is a pattern
    # stat, not a pass/fail line.
    missing_literal = missing - run_member_hex
    missing_nonrun = {hx for hx in missing_literal if hx[2:4] != f"{GO:02X}"}
    literal_total = len(kept_hex - run_member_hex)
    literal_match = literal_total - len(missing_literal)
    if verbatim and missing_nonrun:
        fails.append(f"{len(missing_nonrun)} captured kept row(s) not emitted: "
                     + ", ".join(sorted(missing_nonrun)[:5]))

    # (4) no time overlap with the next command
    min_gap = min((dump[i + 1][0] - dump[i][0] for i in range(len(dump) - 1)),
                  default=float("inf"))
    if min_gap < MIN_GAP_MS - 0.01:
        fails.append(f"publishes {min_gap:.0f} ms apart (below {MIN_GAP_MS:.0f} "
                     f"ms floor)")
    now = -1e300
    for e in plan:   # publishes are monotonic (the clamp never re-orders)
        if e["t"] < now:
            fails.append(f"out-of-order publish @{e['t']:.0f} after "
                         f"@{now:.0f}")
        now = e["t"]
    # per-beat: cue pre-roll must not run past the NEXT beat's GO
    for i, e in enumerate(plan[:-1]):
        nxt = plan[i + 1]
        if e["cascade"] and e["step"] < 0 and e["t"] > nxt["t"]:
            fails.append(f"pre-roll member {e['hex']} @{e['t']:.0f} lands after "
                         f"next frame @{nxt['t']:.0f}")

    # Verbatim replay scripts keep real ticks, so a run's GO must land
    # within tolerance of its captured tick; compressed show splits retime,
    # so their runs must merely be aired somewhere.
    cues = [e for e in plan if e["cascade"]]
    go_delta = []
    aired_runs = 0
    for run in runs:
        want = build_frame([GO, *run["tail"]]).hex().upper()
        hits = [t for t, hx in dump if hx == want
                and abs(t - run["go_tick"]) <= _GO_TOLERANCE_MS]
        if verbatim:
            if not hits:
                fails.append(f"run lead {run['lead']:02X} tail {run['tail'].hex()} "
                             f"has no GO within {_GO_TOLERANCE_MS:.0f} ms of "
                             f"captured tick {run['go_tick']}ms")
            else:
                go_delta.append(min(abs(t - run["go_tick"]) for t in hits))
        elif want in {hx for _, hx in dump}:
            aired_runs += 1
    skew = (f"GO skew med {statistics.median(go_delta):.0f} ms / "
            f"p95 {sorted(go_delta)[int(.95 * len(go_delta)) - 1]:.0f} ms"
            if go_delta else "GO skew n/a")
    status = "PASS" if not fails else "FAIL"
    n_gen = len(generated)
    cov = ("" if verbatim
           else f"; literal {literal_match}/{literal_total} kept, "
                f"{aired_runs}/{len(runs)} runs aired")
    return (f"{script.name:52s} {status:4s}  emit {len(dump)} "
            f"({len([1 for _, H in dump if H.endswith(f'{GO:02X}')])} "
            f"{'cues' if n_gen else 'GO'}; {len(generated)} cascade frames; "
            f"min gap {min_gap:.0f} ms); {skew}{cov}"
            + ("" if not fails else "   -- " + "; ".join(fails[:3])))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scripts", nargs="*", type=Path,
                    help="generated .msh files (default: all shipped)")
    args = ap.parse_args()
    scripts = args.scripts or sorted(
        list((ROOT / "samples").glob("park-*.show*.msh"))
        + list((ROOT / "samples" / "replay").glob("*.msh")))
    results = [f"{s.name}: source {_source_of(s) or '?'}" for s in scripts]
    out = [_check(s) for s in scripts]
    def _tok(o: str) -> str:
        return o[52:].lstrip().split(maxsplit=1)[0]
    npass = sum(1 for o in out if _tok(o) == "PASS")
    nfail = sum(1 for o in out if _tok(o) == "FAIL")
    nskip = sum(1 for o in out if _tok(o) == "SKIP")
    print("\n".join(results))
    print("\n".join(out))
    print("-" * 78)
    print(f"RESULT: {npass} PASS, {nfail} FAIL, {nskip} SKIP")
    sys.exit(1 if nfail else 0)


if __name__ == "__main__":
    main()