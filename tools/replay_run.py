#!/usr/bin/env python3
"""Overnight, no-interaction replay: build -> replay -> compare the 4 park
captures (or any selection) and park the evidence under analysis/replay/.

For every source in --sources (default: all four park captures) it:

  1. resolves ``samples/replay/<source>.msh`` (build with
     ``gen_show_script.py --no-trim`` so every beat
     keeps the capture's own absolute tick, byte-faithful; every
     collapse carries the capture's exact countdown member set in its
     ``cascade hex ... members ...`` clause, rebuilt by ``mwm-send``
     byte-for-byte);
  2. starts the passive receiver recorder behind a fresh ``rx*.jsonl``;
  3. plays the script verbatim through ``mwm-send sequence`` with the
     given cascade/reset flags, teeing the send log;
  4. stops the recorder, runs ``compare_replay.py`` (conversion + replay)
     and records the outcome in a per-session ``summary.csv``.

Offline ``--dry-run`` skips the rig entirely and only runs the conversion
check (capture -> script body), so it validates the scripts before the
ears are lit.

Usage::

    python3 tools/replay_run.py --dry-run          # offline conversion check
    python3 tools/replay_run.py --smoke            # tiny script, rig + compare
    python3 tools/replay_run.py --session park1   # default: back-to-back cue members
    nohup python3 tools/replay_run.py > analysis/replay/overnight.log 2>&1 &

``--dry-run`` is the default-up-front sanity gate; a normal run transmits
real IR.  Results land in ``analysis/replay/runs/<session>/``
(``report-<source>.txt``, ``rx-<source>.jsonl``, ``send-<source>.log``,
``summary.csv``, ``manifest.json``).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re as _re
import shlex
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
SOURCES = ["MRDF0008.TXT", "MRDF0007.TXT", "EMLG000E_filtered.txt",
           "EMLG0026_filtered.txt"]
DEFAULT_CAPTURE = ROOT / "analysis/park/frames.tsv"
DEFAULT_SCRIPTS = ROOT / "samples/replay"
DEFAULT_CONFIG = Path.home() / ".config/ir-remote-tools/mqtt.json"


def _run(cmd: list[str], *, log: Path | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    """Run a tool, teeing stdout to ``log``, returning the result."""
    print("  run: " + " ".join(shlex.quote(str(c)) for c in cmd))
    sink = log.open("ab") if log else open("/dev/null", "wb")
    try:
        with sink:
            proc = subprocess.Popen(cmd, stdout=sink,
                                    stderr=subprocess.STDOUT)
            rc = proc.wait()
    except OSError as exc:
        raise SystemExit(f"failed to run {cmd}: {exc}")
    if rc and check:
        raise SystemExit(f"command failed ({rc}): "
                         + " ".join(shlex.quote(str(c)) for c in cmd))
    return rc


def smoke_msh(src: Path) -> list[str]:
    """A 3-beat smoke script from the source's own commands: three DISTINCT
    cues (preferring a cascade when the capture has one) so the rig hears
    both single hex beats and a sparse countdown chain."""
    cands = [l.strip() for l in src.read_text().splitlines()
             if l.strip().startswith(("hex ", "cascade "))]
    if not cands:
        cands = ["hex 9060A6", "hex 91F1249C", "hex 244885"]
    body: list[str] = []
    for c in cands:
        if c not in body:
            body.append(c)
        if len(body) == 3:
            break
    if not any(c.startswith("cascade ") for c in body):
        cascade = next((c for c in cands if c.startswith("cascade ")), None)
        if cascade and cascade not in body:
            body[2] = cascade
    ticks = ["@0", "@1000", "@2000"]
    return [f"{t}\n{b}" for t, b in zip(ticks, body[:3])]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", nargs="*", default=SOURCES)
    ap.add_argument("--scripts", type=Path, default=DEFAULT_SCRIPTS)
    ap.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    ap.add_argument("--mqtt-json", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--session", default=None)
    ap.add_argument("--cascade-ms", type=float, default=None)
    ap.add_argument("--cascade-full", action="store_true")
    ap.add_argument("--no-end-reset", action="store_true")
    ap.add_argument("--min-gap-ms", type=float, default=30.0)
    ap.add_argument("--strict-end-reset", action="store_true",
                    help="count an unheard trailing end-of-show reset "
                         "against the run (default: warning only)")
    ap.add_argument("--post-gap", type=float, default=3.0,
                    help="seconds to keep listening after the last beat so "
                         "late MQTT deliveries flush before recorder stop")
    ap.add_argument("--smoke", action="store_true",
                    help="replay a tiny 3-beat smoke script instead of the "
                         "full source (rig sanity gate)")
    ap.add_argument("--dry-run", action="store_true",
                    help="offline conversion check only (no transmitter, no "
                         "receiver subscriptions)")
    args = ap.parse_args()

    session = args.session or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    runs = ROOT / "analysis/replay/runs" / session
    runs.mkdir(parents=True, exist_ok=True)
    body: list[str] = [
        "# replay validation run",
        f"# session: {session}",
        f"# started: {_dt.datetime.now(_dt.timezone.utc).isoformat()}",
        f"# dry-run: {args.dry_run}   smoke: {args.smoke}",
        f"# cascade-ms: {args.cascade_ms}   cascade-full: {args.cascade_full}"
        f"   min-gap-ms: {args.min_gap_ms}"
        f"   end-reset: {not args.no_end_reset}",
    ]
    print("\n".join(body))

    if args.smoke:
        src = next((s for s in args.sources
                    if (args.scripts / f"{s}.msh").exists()), SOURCES[0])
        smoke = runs / "smoke.msh"
        smoke.write_text("\n".join(smoke_msh(args.scripts / f"{src}.msh"))
                         + "\n")
        targets = [("smoke", smoke)]
    else:
        targets = [(s, args.scripts / f"{s}.msh") for s in args.sources]

    summary: list[dict] = []
    for label, srcfile in targets:
        if not srcfile.exists():
            raise SystemExit(f"missing script: {srcfile}")
        print(f"\n== {label} ==")

        rx_log = runs / f"rx-{label}.jsonl"
        send_log = runs / f"send-{label}.log"
        report = runs / f"report-{label}.txt"
        rx_log.unlink(missing_ok=True)

        rec_proc = None
        if not args.dry_run:
            rec_proc = subprocess.Popen(
                [sys.executable, str(TOOLS / "capture_receive.py"),
                 "--mqtt-json", str(args.mqtt_json), "--out", str(rx_log)],
                stdout=subprocess.DEVNULL,
                stderr=open(runs / f"recorder-{label}.log", "w"))

        sender = [sys.executable, str(TOOLS / "mwm-send.py"),
                  "--mqtt-json", str(args.mqtt_json),
                  "--min-gap-ms", str(args.min_gap_ms)]
        if args.cascade_ms is not None:
            sender += ["--cascade-ms", str(args.cascade_ms)]
        if args.dry_run:
            sender.append("--dry-run")
        if args.cascade_full:
            sender.append("--cascade-full")
        if args.no_end_reset:
            sender.append("--no-end-reset")
        sender += ["sequence", str(srcfile)]
        _run(sender, log=send_log)

        if rec_proc is not None and not args.dry_run:
            time.sleep(args.post_gap)
            rec_proc.terminate()
            try:
                rec_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                rec_proc.kill()
                rec_proc.wait()

        cmp = [sys.executable, str(TOOLS / "compare_replay.py"),
               "--script", str(srcfile),
               "--min-gap-ms", str(args.min_gap_ms), "--out", str(report)]
        if args.cascade_ms is not None:
            cmp += ["--cascade-ms", str(args.cascade_ms)]
        if not args.smoke:
            cmp += ["--capture", str(args.capture), "--source", label]
        if not args.dry_run:
            cmp += ["--rx", str(rx_log)]
        if args.cascade_full:
            cmp.append("--cascade-full")
        if args.no_end_reset:
            cmp.append("--no-end-reset")
        if args.strict_end_reset:
            cmp.append("--strict-end-reset")
        _run(cmp, log=runs / f"compare-{label}.run.log")

        rep = report.read_text()
        result = next((l for l in rep.splitlines()
                       if l.startswith("RESULT: ")), "RESULT: UNKNOWN")
        result = result.split(": ", 1)[1]
        row = {"session": session, "source": label,
               "script": str(srcfile), "result": result}
        for pat, key in (("expected sends:", "expected"),
                         ("received frames:", "received"),
                         ("matched:", "matched"),
                         ("missing:", "missing"),
                         ("unexpected:", "unexpected"),
                         ("skew slope:", "skew"),
                         ("constant offset:", "offset_ms"),
                         ("jitter resid std", "jitter_std"),
                         ("de-clumped jitter std", "dc_jitter_std")):
            line = next((l for l in rep.splitlines() if pat in l), None)
            if line is None:
                continue
            seg = line[line.find(pat) + len(pat):]
            for tag in ("/p95/max:", "/ max:"):
                if tag in seg:
                    seg = seg.split(tag, 1)[1]
                    break
            nums = _re.findall(r"-?\d+(?:\.\d+)?", seg)
            row[key] = nums[0] if nums else seg.strip()
        summary.append(row)

    if summary:
        with (runs / "summary.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
            w.writeheader()
            w.writerows(summary)
        manifest = {
            "session": session,
            "started_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "args": {k: (str(v) if isinstance(v, Path) else v)
                     for k, v in vars(args).items()},
            "results": summary,
        }
        (runs / "manifest.json").write_text(json.dumps(manifest, indent=2)
                                            + "\n")
    print("\n## summary")
    if summary:
        print("  ".join(summary[0].keys()))
        for r in summary:
            print("  ".join(str(r.get(k, "")) for k in summary[0].keys()))
    print(f"\nsession dir: {runs}")


if __name__ == "__main__":
    main()