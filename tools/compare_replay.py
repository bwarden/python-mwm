#!/usr/bin/env python3
"""Compare a park replay against both the source capture and the receiver log.

The overnight replay validation in three views:

1. **Conversion** (capture -> script): re-runs the authoritative collapse
   rule from ``gen_show_script.collapse_beat_lines`` over the source rows
   and asserts the script's ``hex``/``cue hex`` body matches it
   one-for-one.  Reports what was kept, skipped (55aa heartbeat, beacons,
   idle colour/command smear) and how many capture countdown runs fold.

2. **Replay** (script expectation -> receiver log): parses the ``.msh``
   exactly as ``mwm-send`` would (incl. lead-derived countdown expansion and
   the min-gap send clamp) and aligns the receiver's decoded frames
   against that expectation, member by member.  Reports missing/unexpected
   frames, per-cascade member cadence (back-to-back at the min-gap floor
   by default, paced only with an explicit --cascade-ms), and timing
   quality: skew (linear slope of
   received-after-expected), a constant offset, and jitter (residual
   scatter) both raw and de-clumped (drops frames that rode the same
   RESULT delivery, since Tasmota's MQTT reporting is software-paced).

3. The end-of-show reset (``90 60 A6``) that ``mwm-send`` appends after a
   sequence is expected as the trailing frame unless ``--no-end-reset``.

Usage::

    python3 tools/compare_replay.py --script samples/replay/MRDF0008.TXT.msh \\
        --rx analysis/replay/rx-MRDF0008.jsonl \\
        --capture analysis/park/frames.tsv --source MRDF0008.TXT

Prints a ``RESULT: PASS|FAIL`` summary line (machine-readable) plus a
human report to stdout / ``--out``.

Requires: the shared MWM library (via _bootstrap, stdlib-only) and the
``mwm-send.py``/``gen_show_script.py`` tool modules for the authoritative
parser, cascade expansion and collapse rule.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

sys.path.insert(0, str(TOOLS))
from _bootstrap import mwm  # noqa: E402

_MIN_GAP_MS = 30.0
END_RESET_FRAMES = ("9060A6",)
_SEARCH_WINDOW = 60  # greedily skip this many expecteds to re-align


def _load_tool(name: str, path: str, attr: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return getattr(mod, attr)


_parse_show_script = _load_tool("mwm_send", "mwm-send.py",
                                "_parse_show_script")
_plan_publish_times = _load_tool("mwm_send", "mwm-send.py",
                                 "_plan_publish_times")
collapse_beat_lines = _load_tool("gen_show_script", "gen_show_script.py",
                                 "collapse_beat_lines")


# --------------------------------------------------------------------------
# 1. Conversion: capture rows -> the rule gen_show_script applies
# --------------------------------------------------------------------------

def conversion_summary(rows: list[dict]) -> dict:
    """What the conversion kept/skipped for one source, derived from the
    same ``collapse_beat_lines`` the generator runs (so the numbers and the
    exact command body always agree)."""
    lines = collapse_beat_lines(rows)
    folds = sum(1 for l in lines
                 if l.lstrip().startswith(("cue hex ", "cascade hex ")))
    folded_frames = 0
    for l in lines:
        s = l.strip()
        if s.startswith("# ") and "frames collapsed to one" in s:
            folded_frames += int(s.split()[1])
    hex_beats = sum(1 for l in lines if l.startswith("hex "))
    kinds = [r["kind"] for r in rows]
    return {"total": len(rows), "kept_rows": hex_beats + folded_frames,
            "folds": folds, "folded_frames": folded_frames,
            "hex_beats": hex_beats,
            "shown_55aa": kinds.count("55aa"), "shown_beacon": kinds.count("beacon"),
            "idle": len(rows) - hex_beats - folded_frames
                     - kinds.count("55aa") - kinds.count("beacon")}


def _script_body(path: Path) -> list[str]:
    """The script's command lines (``hex`` / ``cascade hex``), in order."""
    return [l.strip() for l in path.read_text().splitlines()
            if l.strip().startswith(("hex ", "cue ", "cascade "))]


def conversion_check(rows: list[dict], script: Path) -> tuple[bool, str]:
    expected = [l for l in collapse_beat_lines(rows)
                if l.startswith(("hex ", "cue ", "cascade "))]
    got = _script_body(script)
    if expected == got:
        return True, ""
    first = next((i for i, (a, b) in enumerate(zip(expected, got))
                  if a != b), min(len(expected), len(got)))
    e = expected[first] if first < len(expected) else "<eof>"
    g = got[first] if first < len(got) else "<eof>"
    return False, f"first divergence at command {first}: " \
                  f"expected '{e}' got '{g}'"


# --------------------------------------------------------------------------
# 2. Replay: script expectation -> receiver frames
# --------------------------------------------------------------------------

def _sent_plan(script: Path, cascade_ms: float | None, cascade_full: bool,
               min_gap_ms: float, end_reset: bool) -> list[dict]:
    """Per-member send expectation: mwm-send's own parse + plan + clamp.

    The script's beats are keyed off absolute ``@ms`` (the player's cum
    logic); a cue's members pre-roll BEFORE the beat (``@ms - (d & 0x0F)*100``
    for member ``d``, the F? low-nibble delay, the 20 go copy landing on
    ``@ms`` itself) and every
    consecutive publish is held to the sender's min-gap floor.  The
    end-of-show reset rides the tail when enabled.  Delegates straight to
    ``_plan_publish_times`` so the receiver log expectation can never drift
    from what ``--dump``/the live sender compute.
    """
    beats = _parse_show_script(script.read_text(), cascade_ms, cascade_full)
    plan: list[tuple[int, list[bytes], list[float], bool]] = []
    cum = 0
    for beat in beats:
        if beat["t_ms"] is not None:
            cum = beat["t_ms"]
        plan.append((cum, beat["frames"], beat.get("rel_ms", [0.0]),
                     beat.get("cascade", False)))
    return _plan_publish_times(plan, min_gap_ms, end_reset)


def _decode_record(rec: dict) -> list[tuple[float, str]]:
    """Decode one recorder record into (arrival_sec, hex) frames.

    RawData re-decodes the whole burst (every member); Tasmota's ``Data``
    field only ever carries the first frame, so it is used as a fallback
    when RawData is absent rather than merging duplicates.
    """
    line = rec.get("raw", "")
    js = line[line.index("{"):] if "{" in line else ""
    try:
        ir = json.loads(js).get("IrReceived")
    except (json.JSONDecodeError, AttributeError):
        return []
    if not isinstance(ir, dict):
        return []
    t = float(rec.get("t_unix", 0.0))
    raw = ir.get("RawData")
    if raw:
        try:
            outs = mwm.decode_timings(mwm.tasmota_timings(raw))
            return [(t, fr.hex().upper()) for fr in outs]
        except (ValueError, TypeError):
            pass
    if ir.get("Data"):
        try:
            return [(t, ir["Data"].lstrip("0x").upper())]
        except ValueError:
            pass
    return []


def _align(expected: list[dict], rx: list[tuple[float, str]]):
    """Greedy member-level alignment with bounded forward re-search."""
    matches: list[tuple[int, float, str]] = []   # (exp idx, arrival, hex)
    missing: list[int] = []
    unexpected: list[tuple[float, str]] = []
    i = 0
    n = len(expected)
    for t, hx in rx:
        if i < n and expected[i]["hex"] == hx:
            matches.append((i, t, hx))
            i += 1
            continue
        found = next((j for j in range(i, min(i + _SEARCH_WINDOW, n))
                      if expected[j]["hex"] == hx), None)
        if found is not None:
            for k in range(i, found):
                missing.append(k)
            matches.append((found, t, hx))
            i = found + 1
        else:
            unexpected.append((t, hx))
    for k in range(i, n):
        missing.append(k)
    return matches, missing, unexpected


def _linreg(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx) \
        if sxx else 0.0
    return slope, my - slope * mx


def _stats(xs: list[float], ys: list[float]) -> dict:
    slope, intercept = _linreg(xs, ys)
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    delta = [y - x for x, y in zip(xs, ys)]
    absr = sorted(abs(r) for r in resid)
    return {"n": len(xs), "skew_slope": slope,
            "offset_median_ms": statistics.median(delta),
            "jitter_std_ms": statistics.pstdev(resid) if len(resid) > 1 else 0,
            "jitter_p95_ms": absr[int(0.95 * len(absr)) - 1] if absr else 0,
            "jitter_max_ms": absr[-1] if absr else 0}


def _de_clump(matches: list[tuple[int, float, str]]) \
        -> list[tuple[int, float, str]]:
    """Drop frames sharing one RESULT delivery (same sub-ms arrival)."""
    bare = []
    for idx, m in enumerate(matches):
        if idx and abs(m[1] - matches[idx - 1][1]) < 0.001:
            continue
        bare.append(m)
    return bare


def _fmt_ms(v: float) -> str:
    return f"{v:>8.1f} ms"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--script", required=True, type=Path)
    ap.add_argument("--rx", type=Path, default=None,
                    help="receiver recorder JSONL (omit for a conversion-only "
                         "run)")
    ap.add_argument("--capture", type=Path, default=None,
                    help="frames.tsv for the conversion section")
    ap.add_argument("--source", default=None,
                    help="source column within --capture")
    ap.add_argument("--cascade-ms", type=float, default=None)
    ap.add_argument("--cascade-full", action="store_true")
    ap.add_argument("--min-gap-ms", type=float, default=_MIN_GAP_MS)
    ap.add_argument("--no-end-reset", action="store_true",
                    help="sender ran with --no-end-reset (no trailing "
                         "9060A6 expected)")
    ap.add_argument("--strict-end-reset", action="store_true",
                    help="a missing/unheard trailing end-of-show reset "
                         "(90 60 A6) counts against the run (default: it is "
                         "a warning -- the receiver often does not re-report "
                         "the short reset that rides ~30 ms after the last "
                         "beat, so its absence says nothing about the send)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    lines: list[str] = []
    fail = False

    def out(s: str = "") -> None:
        lines.append(s)

    out(f"# replay report: {args.script}")
    if args.rx:
        out(f"  receiver log: {args.rx}")

    # --- conversion --------------------------------------------------
    if args.capture and args.source:
        import csv
        with args.capture.open(newline="") as fh:
            conv_rows = [r for r in csv.DictReader(fh, delimiter="\t")
                         if r["source"] == args.source]
        cs = conversion_summary(conv_rows)
        out()
        out("## conversion (capture -> script)")
        out(f"  capture rows: {cs['total']}")
        out(f"  kept as beats: {cs['kept_rows']} "
            f"({cs['hex_beats']} as-is, {cs['folded_frames']} in folds)")
        out(f"  skipped: {cs['shown_55aa']}x 55aa heartbeat, "
            f"{cs['shown_beacon']}x beacon, {cs['idle']}x idle colour/command")
        out(f"  countdown runs folded: {cs['folds']} -> one cue each "
            f"(countdown derived from the lead byte, down to the 20 go copy)")
        ok, why = conversion_check(conv_rows, args.script)
        out(f"  script body == generator rule: "
            f"{'PASS' if ok else 'FAIL -- ' + why}")
        if not ok:
            fail = True

    # --- replay ------------------------------------------------------
    if not args.rx:
        out()
        out(f"RESULT: {'PASS' if not fail else 'FAIL'}")
        report = "\n".join(lines)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report + "\n")
        else:
            print(report)
        return

    expected = _sent_plan(args.script, args.cascade_ms, args.cascade_full,
                          args.min_gap_ms, not args.no_end_reset)
    rx: list[tuple[float, str]] = []
    with args.rx.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rx.extend(_decode_record(rec))

    matches, missing, unexpected = _align(expected, rx)

    reset_warning = False
    if missing and not args.strict_end_reset:
        pending = [i for i in missing
                   if expected[i]["hex"] in END_RESET_FRAMES]
        if len(pending) == len(missing):
            reset_warning = True
            missing = []

    casc_beats = sorted({e["beat"] for e in expected if e["cascade"]})
    n_reset = 1 if not args.no_end_reset else 0
    casc_members = sum(1 for e in expected if e["cascade"])
    hex_members = len(expected) - casc_members - n_reset

    out()
    out("## replay (script expectation -> receiver log)")
    out(f"  expected sends: {len(expected)} member(s) = "
        f"{len(casc_beats)} cue beat(s) ({casc_members} auto-countdown "
        f"members at their pre-roll times) "
        f"+ {hex_members} hex member(s)"
        f"{' + 1 end-reset' if n_reset else ''}")
    out(f"  received frames: {len(rx)}")
    out(f"  matched: {len(matches)}   missing: {len(missing)}   "
        f"unexpected: {len(unexpected)}")
    if reset_warning:
        out("  (trailing end-of-show reset not loopback-visible: the "
            "receiver did not re-report 90 60 A6 -- treated as a warning)")

    if matches:
        xs = [expected[i]["t"] for i, _, _ in matches]
        ys = [t * 1000.0 for _, t, _ in matches]
        ref, base_y = xs[0], ys[0]
        st = _stats([x - ref for x in xs], [y - base_y for y in ys])
        bare = _de_clump(matches)
        if len(bare) > 2:
            bxs = [expected[i]["t"] - ref for i, _, _ in bare]
            bys = [t * 1000.0 - base_y for _, t, _ in bare]
            bst = _stats(bxs, bys)
        else:
            bst = st

        out(f"  timing (all {st['n']} matched):")
        out(f"    skew slope: {st['skew_slope']:.5f} "
            f"(1.0 = receiver clock keeps script pace)")
        out(f"    constant offset: {_fmt_ms(st['offset_median_ms'])} "
            f"(receiver after expected)")
        out(f"    jitter resid std/p95/max: "
            f"{st['jitter_std_ms']:.1f} / {st['jitter_p95_ms']:.1f} / "
            f"{st['jitter_max_ms']:.1f} ms "
            f"({len(bare)}/{len(matches)} de-clumped)")
        if len(matches) - len(bare) > 0:
            out(f"    de-clumped jitter std/p95/max: "
                f"{bst['jitter_std_ms']:.1f} / "
                f"{bst['jitter_p95_ms']:.1f} / "
                f"{bst['jitter_max_ms']:.1f} ms")

        if casc_beats:
            n_mem = sum(1 for e in expected if e["cascade"])
            if args.cascade_ms is not None:
                cadence = f"paced --cascade-ms {args.cascade_ms:.0f} ms"
            else:
                cadence = "countdown pre-roll onto the @ms GO"
            if args.cascade_full:
                cadence += "; full 14-member FD chain"
            out(f"  cue delivery ({n_mem} members, {cadence}):")
        for bi in casc_beats:
            em = [e for e in expected if e["beat"] == bi]
            heard = {e["hex"]: t for i, t, hx in matches
                     if expected[i]["beat"] == bi for e in [expected[i]]}
            delays = ", ".join(hex(int(e["hex"][2:4], 16)) for e in em)
            n_heard = sum(1 for e in em if e["hex"] in heard)
            out(f"    beat @{em[0]['t']:8.0f} ms: {n_heard}/{len(em)} "
                f"members heard (delays {delays})")

        # GO landing for the first fully-heard cue
        for bi in casc_beats:
            em = [e for e in expected if e["beat"] == bi]
            heard = {e["hex"]: t for i, t, hx in matches
                     if expected[i]["beat"] == bi for e in [expected[i]]}
            if len(heard) != len(em):
                continue
            go = em[-1]                        # the 20 go copy rides last
            exp_master = go["t"]
            act_master = heard[go["hex"]] * 1000.0
            out(f"  cue GO (first fully-heard cue @{em[0]['t']:7.0f} ms): "
                f"expected @{exp_master:7.0f} ms got "
                f"@{act_master:7.0f} ms "
                f"(delta {act_master - exp_master:>6.1f} ms)")
            break

        if missing:
            out(f"  missing frames: {len(missing)} "
                f"(first: {', '.join(sorted({expected[i]['hex'] for i in missing}))[:80]}...)")
            fail = True
    else:
        out("  (no frames matched - receiver heard nothing?)")
        fail = True

    if unexpected:
        buckets: dict[str, int] = {}
        for _, hx in unexpected:
            buckets[hx] = buckets.get(hx, 0) + 1
        top = sorted(buckets.items(), key=lambda kv: -kv[1])[:5]
        out(f"  unexpected frames: {len(unexpected)} "
            f"(top: {', '.join(f'{h}x{n}' for h, n in top)})")

    out()
    out(f"RESULT: {'PASS' if not fail else 'FAIL'}")
    report = "\n".join(lines)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n")
    else:
        print(report)


if __name__ == "__main__":
    main()