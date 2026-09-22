#!/usr/bin/env python3
"""Turn a real park/hat/wand capture into a human-editable show script.

Reads ``analysis/park/frames.tsv`` (or a like-formatted TSV) and emits an
``.msh`` show-script in the format that ``tools/mwm-send.py sequence``
consumes (@ms offset lines + one beat per line, '#' comments).  Tick
column is treated as milliseconds: it is ~1 ms/tick in every capture
(EMLG0026 timecodes step 1001 ticks/s; MRDF0007 covers 2 s in 2168 ticks).

RAW CAPTURES ARE ALSO ACCEPTED DIRECTLY: any oPossum/Mouse-Ear feed whose
left column is a per-frame capture offset ("timestamp") is decoded in
place -- ``*.mwm`` hex dumps (``0018E6C3: 9C FC 24 ...``), ``*_filtered.txt``
oPossum dumps, and ``*_reader.TXT`` Mouse Ear Recorder exports
(``YYYY/MM/DD hh.mm.ss:00233258:<hex>``).  A ``.mwm``/``_filtered.txt``
line may pack several frames plus noise; only CRC-valid decoded frames
survive (they roll up under the line's tick, exactly like analyze_park).
So ``python3 tools/gen_show_script.py ../captures/Feliz_Navidad.mwm`` just
works, and a mix of TSVs and raw feeds may be listed on one command line.

Countdown-cascade runs are COLLAPSED: when consecutive frames share the
same phrase modulo the delay byte -- delay stepping down the F1..FB (or
FD..F1) range and then the immediate ``20`` go copy -- the generator
emits a single ``cue hex 20 <phrase tail>`` line anchored at the run's
GO tick, so the script stays compact and a human can edit one cue instead
of fourteen frames.  The cue carries the run's IMMEDIATE ``20``
go-variant -- the command that executes at the line's ``@ms`` -- and
``mwm-send`` generates the countdown itself, pre-rolling each member
``@GO - (d & 0x0F)*100`` ms before it (the ``F?`` countdown bytes are
low-nibble x 100 ms delays, F1=100 ms .. FF=1500 ms), so every ear that
hears ANY member still fires exactly on the moment the ``20`` lands.  The
countdown, and its exact byte sequence, is redundancy, not show content;
the ``#`` comment on the cue line records the captured countdown's start
for a human reader.  Any other frame falls through to a literal
``hex <frame>`` beat.

Multiple caught cycles of a run (e.g. three 96-chains back to back) still
collapse to ONE cue at the run's FIRST GO; ``--no-collapse`` is the
verbatim byte-faithful mode and keeps every captured frame.

Usage::

    python3 tools/gen_show_script.py \\
        [--source MRDF0008.TXT] [--start-tick 0] [--end-tick T] \\
        [--phase-compress] [--split-shows] \\
        > samples/park-show.msh
    python3 tools/gen_show_script.py ../captures/Feliz_Navidad.mwm
    python3 tools/gen_show_script.py --source Feliz_Navidad.mwm \\
        ../captures/*.mwm

    mwm-send.py --dry-run sequence samples/park-show.msh    # plan
    mwm-send.py sequence samples/park-show.msh              # send

By default the timeline is the capture's own, read faithfully: rows are
de-flattened to their countdown-byte cadence, every deliberate cue -- the
effect chains AND the 93-family/97-family/92-family command and colour
chains that the park airs the same way -- collapses to a ``cue`` kept on
the run's GO tick, and the first beat is renormalised to ``@0`` so a demo
starts doing something immediately (``--no-trim`` keeps the real
wall-clock ticks).  Every beat keeps its own tick, so the silence between
phrases -- and each member's real cadence -- survives verbatim.

The condensed DEMO modes are opt-in: ``--split-shows`` partitions at
long static idle runs into one ``.msh`` per show (the preceding static
run becomes that show's ~``--phase-ms`` LEAD-IN, replayed at the run's
own capture cadence); ``--phase-compress`` folds statics to span
~``--phase-ms`` at their real cadence; and ``--gap-cap`` clamps silent
gaps > ``--gap-ms``.

Timing is always learned from the capture, never guessed: a folded phase
replays whole cycles on a uniform grid at the run's own median cadence,
so we never add a repeat the park didn't make, and the cue replay
(``mwm-send``) pre-rolls its countdown before the same GO the capture
aired -- under the same no-trim clock, the ear hears the cue land exactly
when the park landed it.
``--start-tick``/``--end-tick`` crop the window and ``--offset`` shifts
every tick.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import mwm  # noqa: E402

DEFAULT_TSV = Path(__file__).resolve().parent.parent / "analysis" / "park" / "frames.tsv"
ROOT = DEFAULT_TSV.parent.parent.parent
_TSV_HEAD = "source\ttick\twall\thex\tlen\tkind"


def _display(path: Path) -> str:
    """The path the header shows: repo-root-relative when under the root, so
    regenerated scripts carry the SAME header line anywhere they're built."""
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)

GO = 0x20                          # immediate go copy
_SPREAD_MS = 100                   # de-flatten packed countdown members
_LOADED: list[Path] = []           # inputs this run consumed (header line)
_HEX_BYTE = re.compile(r"^[0-9A-Fa-f]{2}$")
# The general timeline is built from ``effect-command`` rows -- ramp and
# effect changes -- plus every row inside a genuine countdown run (any
# kind): the park airs its 93-family and 97-family chains mid-show exactly
# like the effect chains, and de-flattening keeps them detectable.  The
# ``55aa`` system broadcasts and ``beacon`` receiver rows never belong
# (they close a run without emitting).  Lone solid-color go copies and
# plain commands that are NOT part of a countdown run are idle smear and
# must NOT clutter the body, but they DO carry the exit sequence that puts
# ears back in their captured resting state -- so ``collapse_beat_lines``
# admits them only for rows after the last effect-command (the source's
# exit tail).
_BEAT_KINDS = {"effect-command"}
_EXIT_KINDS = {"color-command", "command"}


def _phrase_bytes(frames: list[bytes], idx: int) -> list[int]:
    """The delay-led phrase (delay byte + tail, no head, no CRC) of
    ``frames[idx]`` -- what ``cue hex`` needs to rebuild the chain.
    """
    return [frames[idx][1], *[b for b in frames[idx][2:-1]]]


def _is_tsv(path: Path) -> bool:
    """True when the file is a frames.tsv (header present)."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        if line.strip():
            return line.startswith(_TSV_HEAD)
    return False


def _raw_feed_rows(path: Path) -> list[dict] | None:
    """Decode a raw show-capture feed (``*.mwm`` hex dump, ``*_filtered.txt``
    oPossum dump, ``*_reader.TXT`` Mouse Ear Recorder export) into the same
    frames.tsv row shape, with the left column read as the per-frame tick.
    Frames packed onto one capture line are DE-FLATTENED at the countdown
    cadence (frame i -> +i*100 ms), so near-simultaneous members keep their
    real per-member timeline and a countdown run that shares a line stays a
    clean cascade candidate.  None when the filename matches no known feed
    format."""
    from analyze_park import frame_row, load_source, _describe
    stream = load_source(path, spread_ms=_SPREAD_MS)
    if stream is None:
        return None
    rows = []
    for rec in stream:
        row = frame_row(path.name, rec, _describe(rec["hex"]))
        if row["kind"] == "invalid":
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["tick"], r["hex"]))
    return rows


def _load_rows(path: Path) -> list[dict] | None:
    """TSV or raw feed -> rows; None when the format is unrecognised or the
    file is missing/empty."""
    if _is_tsv(path):
        with path.open(newline="") as fh:
            return list(csv.DictReader(fh, delimiter="\t"))
    try:
        return _raw_feed_rows(path)
    except OSError:
        return None


def _cascade_delays(hexes: list[str]) -> list[int] | None:
    """Return the delay bytes if ``hexes`` is a cascadable run.

    Consecutive frames must share the identical content modulo byte[1]
    (the delay), delays must all be members of the countdown range
    F1..FF plus the terminal ``20`` go copy, with two+ distinct delays and
    the run ending on the go copy.  The observed order is not a strict
    step down -- the park interleaves near-simultaneous pairs (F4/F5,
    F2/F3, 20/F1) -- so any ordering of the countdown set collapses
    cleanly.
    """
    if len(hexes) < 3:
        return None
    try:
        frames = [bytes.fromhex(h) for h in hexes]
    except ValueError:
        return None
    if any(len(f) < 3 for f in frames):
        return None
    delays = [f[1] for f in frames]
    # All members must be countdown delays F1..FF or the 20 go copy, with
    # at least two distinct values and the go copy present (its exact
    # position is arbitrary -- the park lands the 20/F1 pair together).
    if GO not in delays or not (set(delays) <= {GO, *range(0xF1, 0x100)}):
        return None
    if len(set(delays)) < 2:
        return None
    tail0 = frames[0][2:-1]
    if any(f[2:-1] != tail0 for f in frames):
        return None
    return delays


def _chain_index(rows: list[dict]) -> list[bool]:
    """Per-row mark of membership in a GENUINE countdown run.

    A run is a maximal stretch of consecutive same-tail rows (modulo the
    delay byte); it qualifies when ``_cascade_delays`` accepts it: 2+
    distinct countdown delays (F1..FF) and the terminal immediate ``20``
    go copy.  The park airs such chains deliberately for every family --
    the 93-command and 97-colour chains are real cues exactly like the
    effect chains -- so their members must never be mistaken for idle
    smear.
    """
    out = [False] * len(rows)
    i = 0
    n = len(rows)
    while i < n:
        tail = bytes.fromhex(rows[i]["hex"])[2:-1]
        j = i
        while (j < n and bytes.fromhex(rows[j]["hex"])[2:-1] == tail):
            j += 1
        if _cascade_delays([r["hex"] for r in rows[i:j]]) is not None:
            for k in range(i, j):
                out[k] = True
        i = j
    return out


def admitted_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split captured rows into ``(kept, skipped)`` exactly as the beat
    filter in ``collapse_beat_lines`` sees them.

    Kept = every ``effect-command`` row, every member of a genuine
    countdown chain (any kind), and every row after the last
    ``effect-command`` (the exit tail).  Skipped = ``55aa``/``beacon``
    receiver rows and lone non-chain ``color-command``/``command`` idle
    singles inside the body.  The harness compares ``mwm-send``'s emitted
    stream against ``kept``; ``skipped`` is the documented, deliberate
    drop set.
    """
    tail_from = max((i for i, r in enumerate(rows)
                     if r["kind"] == "effect-command"), default=-1)
    chain = _chain_index(rows)
    kept: list[dict] = []
    skipped: list[dict] = []
    for i, r in enumerate(rows):
        if (r["kind"] in _BEAT_KINDS or chain[i]
                or (i > tail_from and r["kind"] in _EXIT_KINDS)):
            kept.append(r)
        else:
            skipped.append(r)
    return kept, skipped


def _beat_batches(rows: list[dict]) -> list[dict]:
    """The same-tail BEAT runs ``collapse_beat_lines`` folds or emits.

    Maximal stretches of CONSECUTIVE beat rows sharing one head + phrase
    (modulo the delay byte); a row with a different tail, or any row that
    is not a beat, closes the batch.  Which rows are beats: the general
    timeline admits ``effect-command`` rows; rows of ANY kind that sit
    inside a genuine captured countdown run (a same-tail chain of 2+
    distinct countdown delays ending on the immediate ``20`` go copy) are
    deliberate cues -- the park airs the 93-family and 97-family chains
    mid-show exactly like the effect chains -- so they are beats too.  A
    lone solid-color single (the idle smear: crossfades, ``91``-family
    sync pings) still folds out of the body.  And the exit tail must not
    strand ears mid-effect when a show ends -- the sources close by
    setting a solid resting color (``both ears off``, ``both ears
    magenta``, or the white ``95``-family countdown), so after the LAST
    ``effect-command`` of the capture ``color-command``/``command`` rows
    are admitted too: the exit tail plays verbatim (a single solid go, or
    a countdown chain that collapses to one ``cue``) instead of being
    dropped.
    """
    tail_from = max((i for i, r in enumerate(rows)
                     if r["kind"] == "effect-command"), default=-1)
    in_chain = _chain_index(rows)
    batches: list[dict] = []
    batch: list[dict] = []
    hexes: list[str] = []
    tail: bytes | None = None

    def close() -> None:
        if batch:
            batches.append({"rows": batch, "hexes": hexes, "tail": tail})

    for idx, row in enumerate(rows):
        is_beat = (row["kind"] in _BEAT_KINDS
                   or in_chain[idx]
                   or (idx > tail_from and row["kind"] in _EXIT_KINDS))
        if not is_beat:
            close()
            batch, hexes, tail = [], [], None
            continue
        t = bytes.fromhex(row["hex"])[2:-1]
        if tail is None or t == tail:
            batch.append(row)
            hexes.append(row["hex"])
            tail = t
        else:
            close()
            batch, hexes, tail = [row], [row["hex"]], t
    close()
    return batches


def cascade_runs(rows: list[dict]) -> list[dict]:
    """The countdown runs ``collapse_beat_lines`` folds into one cue each.

    Every same-tail BEAT batch whose delay bytes pass ``_cascade_delays``,
    with the run geometry precomputed exactly as ``emit_run`` reads it (the
    lead = the largest non-GO delay byte, the phrase tail, and the row the
    immediate ``20`` go copy aired on -- the tick that anchors the cue)."""
    runs: list[dict] = []
    for b in _beat_batches(rows):
        delays = _cascade_delays(b["hexes"])
        if delays is None:
            continue
        signal = [d for d in delays if d != GO]
        go_k = delays.index(GO)
        runs.append({
            "rows": b["rows"],
            "delays": delays,
            "lead": max(signal),
            "tail": b["tail"],
            "go_row": b["rows"][go_k],
            "go_tick": int(b["rows"][go_k]["tick"]),
        })
    return runs


def _head_pre_roll(rows: list[dict],
                   beats: list[tuple[int, list[str]]]) -> int:
    """Recorded pre-roll of the opening countdown run, if any.

    A capture that opens with a countdown should air it.  When the first
    kept beat is a collapsed ``cue`` (its ``@`` is the run's GO tick), the
    renormaliser anchors the run's FIRST countdown member so the cue's GO
    lands at its RECORDED offset from the recording's first frame instead
    of at 0.  Returns 0 when the show opens on a plain hex beat or the
    opening GO cannot be matched back to a run."""
    if not beats or not beats[0][1] or not beats[0][1][0].startswith(
            ("cue ", "cascade ")):
        return 0
    go0 = beats[0][0]
    for r in cascade_runs(rows):
        if r["go_tick"] == go0:
            return go0 - int(r["rows"][0]["tick"])
    return 0


def collapse_beat_lines(rows: list[dict]) -> list[str]:
    """Map captured state rows to script lines (per source run).

    A cascade candidate is a maximal run of CONSECUTIVE beat rows that
    share the same head + phrase (modulo the delay byte) -- see
    ``_beat_batches`` for exactly which rows are beats.  A batch that
    satisfies ``_cascade_delays`` collapses to a single ``cue hex`` line
    anchored on the run's GO tick; otherwise each row is emitted as its own
    ``hex`` beat.
    """
    out: list[str] = []

    def emit_run(b: dict) -> None:
        delays = _cascade_delays(b["hexes"])
        if delays is not None:
            signal = [d for d in delays if d != GO]
            # The cue carries the run's IMMEDIATE ``20`` go-variant -- the
            # command that executes at the GO tick.  mwm-send generates the
            # countdown itself, pre-rolling it backward from the cue's @ms,
            # so the script never spells out the chain (its bytes are
            # redundancy, not show content); the comment records the
            # captured countdown's start for a human reader.
            go_k = delays.index(GO)
            go_tick = int(b["rows"][go_k]["tick"])
            frames_b = [bytes.fromhex(h) for h in b["hexes"]]
            phrase = " ".join(f"{x:02X}" for x in
                              _phrase_bytes(frames_b, go_k))
            # The countdown is anchored on the run's GO tick: @ms IS the
            # moment the 20 go copy fires, and every ear that hears any of
            # the auto-generated members still lands there.
            out.append(f"@{go_tick}")
            out.append(f"cue hex {phrase}")
            out.append(f"  # {len(b['hexes'])} frames collapsed to one cue "
                       f"(captured countdown from {max(signal):02X} "
                       f"-> go@{go_tick}ms)")
        else:
            for row in b["rows"]:
                out.append(f"@{int(row['tick'])}")
                out.append(f"hex {row['hex']}")
                if row["summary"]:
                    out.append(f"  # {row['summary']}")

    for b in _beat_batches(rows):
        emit_run(b)
    return out


def _group_beats(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Split script lines into ``(t_ms, [command + comment lines])`` beats."""
    beats: list[tuple[int, list[str]]] = []
    for line in lines:
        if line.startswith("@"):
            beats.append((int(line[1:]), []))
        elif beats:
            beats[-1][1].append(line)
    return beats


def _skip_prelude(beats: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
    """Drop the leading run of identical literal-``hex`` beats.

    The park meters out the same idle cue every ~4 s for minutes before a
    real change of state; those leading beats are indistinguishable from
    the wall-clock prelude and a show replayed from them "does nothing"
    for ages.  Only a run of IDENTICAL frames at the head is dropped --
    the first differing beat (a real cue) starts the show.
    """
    if not beats or not beats[0][1] or not beats[0][1][0].startswith("hex "):
        return beats
    first = beats[0][1][0]
    dropped = 0
    for _, blines in beats:
        if blines and blines[0] == first:
            dropped += 1
        else:
            break
    if dropped < 2:   # a lone head cue is a real beat, not a prelude
        return beats
    return beats[dropped:]


def _phase_key(blines: list[str]) -> str:
    """The identity of a beat for phase detection: its first command line
    (``hex``/``cue``), ignoring any comment lines below it."""
    return blines[0] if blines else ""


def _phase_cadence(run: list[tuple[int, list[str]]], period: int,
                   cycles: int) -> int | None:
    """Median ms between consecutive cycle STARTS of a static run.

    ``run`` is the phase's real beat slice (``beats[i:j+1]``), ``period``
    is 1 (one cue) or 2 (an A/B pair), ``cycles`` the whole cycles it
    holds.  The median of the capture's own inter-cycle deltas is how fast
    the park repeated the cue -- folds replay AT this cadence, so the
    park's real rhythm is learned from the data instead of guessed.
    Returns ``None`` when there is no single representative cadence.
    """
    deltas = [run[e * period][0] - run[(e - 1) * period][0]
              for e in range(1, cycles)]
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        return None
    deltas.sort()
    return deltas[len(deltas) // 2]


def _fold_run_cycles(run: list[tuple[int, list[str]]], *, period: int,
                     cycles: int, cadence: int, span_ms: int,
                     max_kept: int) -> list[tuple[int, list[str]]]:
    """Replay up to ``max_kept`` of a static run's cycles on its OWN grid.

    Whole cycles of the phase (the first beat of ``run`` is one cycle
    start) are replayed at even rhythm: cycle starts go out ``cadence`` ms
    apart, intra-cycle beat offsets come from the beat they belong to, so
    an A/B phase stays a clean A,B,A,B alternation with its real A->B
    spacing -- sampling at cycle granularity can never grab mid-cycle and
    lilt the flash.  At most ``max_kept`` cycles are kept and the run is
    capped at ~``span_ms`` total, so a several-minute idle folds to a
    ~10 s reminder.  Folding never makes a repeat DENSER than the capture
    (that is the whole point of using the real cadence): we never add a
    transmit the park did not make itself.
    """
    keep = min(cycles, max_kept)
    if cadence:
        keep = min(keep, max(1, 1 + span_ms // cadence))
    intra = [run[k][0] - run[0][0] for k in range(period)]
    pattern = [run[k][1] for k in range(period)]
    out: list[tuple[int, list[str]]] = []
    for cyc in range(keep):
        base = cadence * cyc
        for k in range(period):
            out.append((base + intra[k], pattern[k]))
    return out


def _compress_phases(beats: list[tuple[int, list[str]]], *,
                     target_ms: int = 10000,
                     max_kept: int = 8) -> list[tuple[int, list[str]]]:
    """Squeeze long static phases so a demo gets through every cue.

    A "static phase" is a run of beats repeating ONE cue (the park's
    metered idle ping) or an A/B pair (e.g. the red/green holiday cycle)
    -- a period-1 or period-2 repetition -- running *longer* than
    ``target_ms``.  Such a run is folded to span ~``target_ms`` total,
    replaying whole cycles AT THE RUN'S OWN CAPTURE CADENCE (so a repeat
    is never sent denser than the park sent it); genuine transitions
    (non-repeating beats) keep their real wall-clock timing.
    """
    out: list[tuple[int, list[str]]] = []
    i = 0
    n = len(beats)
    while i < n:
        key0 = _phase_key(beats[i][1])
        period = 1
        if i + 1 < n and _phase_key(beats[i + 1][1]) != key0:
            if i + 2 < n and _phase_key(beats[i + 2][1]) == key0:
                period = 2  # A/B/A/... alternation, e.g. red/green flashing
            else:
                out.append(beats[i])
                i += 1
                continue
        j = i + period - 1
        while j + period < n and all(
                _phase_key(beats[j + 1 + k][1]) == _phase_key(beats[i + k][1])
                for k in range(period)):
            j += period
        run_len = j - i + 1
        span = beats[j][0] - beats[i][0]
        if run_len >= 4 and span > target_ms:
            cycles = run_len // period
            cadence = _phase_cadence(beats[i:j + 1], period, cycles)
            out.extend(_fold_run_cycles(
                beats[i:j + 1], period=period, cycles=cycles,
                cadence=cadence if cadence is not None else target_ms,
                span_ms=target_ms, max_kept=max_kept))
            i = j + 1
            continue
        out.append(beats[i])
        i += 1
    return out


def _split_shows(beats: list[tuple[int, list[str]]], *,
                 min_static_ms: int = 10000, lead_in_ms: int = 10000,
                 max_kept: int = 8) -> list[tuple[list[tuple[int, list[str]]],
                                                  list[tuple[int, list[str]]],
                                                  int]]:
    """Partition a capture's beats into ``(lead_in, body, gap_ms)`` shows.

    A STATIC phase is a run of beats repeating one cue -- or an A/B pair
    (e.g. the red/green holiday cycle) -- for longer than ``min_static_ms``;
    those are the park idling between bouts of real work, so each marks a
    boundary between shows.  The idle cue becomes the NEXT show's
    ~``lead_in_ms`` LEAD-IN, replayed at the run's own capture cadence so
    the ears sit in the recorded pre-show state; the show body is every
    beat between statics at its REAL timing, and ``gap_ms`` is the real
    captured gap between the static run's last beat and the body's first.
    A static run with no show after it (the capture tail) is dropped.
    ``lead_in`` is empty when a capture starts mid-show (no preceding
    static).  Consecutive non-static beats melt into one show body.
    """
    segs: list[tuple[bool, int, int, int]] = []   # (is_static, i, j, period)
    n = len(beats)
    i = 0
    while i < n:
        key0 = _phase_key(beats[i][1])
        period = 1
        if i + 1 < n and _phase_key(beats[i + 1][1]) != key0:
            if i + 2 < n and _phase_key(beats[i + 2][1]) == key0:
                period = 2  # A/B/A/... alternation, e.g. red/green flashing
            else:
                segs.append((False, i, i, 1))
                i += 1
                continue
        j = i + period - 1
        while j + period < n and all(
                _phase_key(beats[j + 1 + k][1]) == _phase_key(beats[i + k][1])
                for k in range(period)):
            j += period
        run_len = j - i + 1
        span = beats[j][0] - beats[i][0]
        if run_len >= 4 and span > min_static_ms:
            segs.append((True, i, j, period))
        else:
            segs.append((False, i, j, period))
        i = j + 1

    show = (None, [])      # (pending (i,j,period,cadence) | None, body beats)
    out: list[tuple[list[tuple[int, list[str]]],
                    list[tuple[int, list[str]]], int]] = []
    for is_static, i, j, period in segs:
        if is_static:
            if show[1]:
                out.append(_finish_show(show, beats, lead_in_ms, max_kept))
            cycles = (j - i + 1) // period
            cadence = _phase_cadence(beats[i:j + 1], period, cycles)
            show = ((i, j, period,
                     cadence if cadence is not None else lead_in_ms), [])
            continue
        show[1].extend(beats[i:j + 1])
    if show[1]:
        out.append(_finish_show(show, beats, lead_in_ms, max_kept))
    return out


def _finish_show(show: tuple[tuple[int, int, int, int] | None,
                             list[tuple[int, list[str]]]],
                 beats: list[tuple[int, list[str]]], lead_in_ms: int,
                 max_kept: int) -> tuple[list[tuple[int, list[str]]],
                                         list[tuple[int, list[str]]], int]:
    """Build one show's ``(lead_in, body, gap_ms)`` from (pending, body)."""
    pending, body = show
    if pending is None:
        return ([], body, 0)
    si, sj, speriod, scadence = pending
    scycles = (sj - si + 1) // speriod
    lead = _fold_run_cycles(beats[si:sj + 1], period=speriod,
                            cycles=scycles, cadence=scadence,
                            span_ms=lead_in_ms, max_kept=max_kept)
    return (lead, body, body[0][0] - beats[sj][0])


def _cap_gaps(beats: list[tuple[int, list[str]]], *,
              max_gap_ms: int = 10000) -> list[tuple[int, list[str]]]:
    """Clamp long silent gaps between consecutive beats.

    A demo replayed from a real capture inherits its empty stretches --
    minutes of nothing while the park's transmitter sat idle.  Any gap
    longer than ``max_gap_ms`` is shortened to ``max_gap_ms`` (beats after
    it shift up by the excess); spacing at or under the cap is untouched,
    so genuine transition pacing always survives.
    """
    out: list[tuple[int, list[str]]] = []
    excess = 0
    prev = beats[0][0]
    out.append(beats[0])
    for t, blines in beats[1:]:
        if t - prev > max_gap_ms:
            excess += t - prev - max_gap_ms
        out.append((t - excess, blines))
        prev = t
    return out


def _retime(beats: list[tuple[int, list[str]]], *, trim: bool,
            lead_in: int, offset: int,
            head_pre_roll_ms: int = 0) -> list[tuple[int, list[str]]]:
    """Renormalise beat ticks.  With ``trim`` (default) the first kept
    beat lands at ``lead_in`` so a show starts doing something immediately;
    when the show opens with a collapsed countdown run,
    ``head_pre_roll_ms`` is that run's RECORDED pre-roll, so its first
    member anchors at ``lead_in`` and the cue's GO lands at ``lead_in`` +
    the recording's own GO offset (``head_pre_roll_ms``).  ``offset`` is
    then a manual shift applied on top (applied as ticks added)."""
    shift = beats[0][0] - lead_in - (head_pre_roll_ms if trim else 0) \
        if (trim and beats) else 0
    return [(t - shift + offset, blines) for t, blines in beats]


def _header(args, sources: list[str]) -> list[str]:
    """Common header lines describing exactly what this run produced."""
    pfx = "OFF" if args.no_collapse else "on"
    return [
        f"# generated by gen_show_script.py from "
        f"{', '.join(_display(p) for p in _LOADED)}",
        f"# sources: {', '.join(sources)}",
        *([f"# tick window: "
           f"{args.start_tick if args.start_tick is not None else 0}"
           f"..{args.end_tick if args.end_tick is not None else 'end'}"]
          if args.start_tick is not None or args.end_tick is not None else []),
        f"# ticks as ms; cue runs collapsed {pfx}; offset {args.offset} ms"
        + "; countdown cues carry the 20 go-variant (mwm-send generates "
        + "the countdown, pre-rolled backward from the cue's @ms)"
        + ("; skip-prelude on" if args.skip_prelude else "")
        + (f"; trim on (show starts @{args.lead_in}"
           + "; an opening countdown GO keeps its recorded offset)"
           if args.trim
           else "; trim off -- real wall-clock ticks kept"),
        f"# static phases: "
        + (f"runs >{args.phase_ms} ms compressed to ~{args.phase_ms} ms"
           if args.phase_compress else "kept at real wall-clock timing"),
        f"# silent gaps: "
        + (f">{args.gap_ms} ms clamped to {args.gap_ms} ms"
           if args.gap_cap else "kept at real capture length"),
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*", default=None,
                    help="captures to process: a frames.tsv, any number of "
                         "raw feeds (*.mwm hex dumps, *_filtered.txt oPossum "
                         "dumps, *_reader.TXT Mouse Ear exports), or a mix. "
                         "Default: analysis/park/frames.tsv.")
    ap.add_argument("--source", default=None,
                    help="only this source column value (for a raw feed the "
                         "source is its filename)")
    ap.add_argument("--start-tick", type=int, default=None)
    ap.add_argument("--end-tick", type=int, default=None)
    ap.add_argument("--no-collapse", action="store_true",
                    help="emit every frame as its own hex beat")
    ap.add_argument("--split-shows", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="split the capture at long static idle runs into "
                         "one .msh per show (default off: one faithful "
                         "timeline to stdout; --no-split-shows is the "
                         "default)")
    ap.add_argument("--out-prefix", default=None,
                    help="file prefix for split shows (default: the source "
                         "name); each show goes to <prefix>.showN.msh")
    ap.add_argument("--skip-prelude", action="store_true",
                    help="drop the leading run of identical idle cues")
    ap.add_argument("--trim", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="renormalise so the first kept beat is at --lead-in "
                         "ms (default on; --no-trim keeps the real "
                         "wall-clock ticks)")
    ap.add_argument("--lead-in", type=int, default=0,
                    help="tick of the first beat after --trim (ms, default 0)")
    ap.add_argument("--offset", type=int, default=0,
                    help="tick added to every emitted @ms (ms)")
    ap.add_argument("--phase-compress", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="squeeze long static phases (a beat repeating one "
                         "cue or an A/B pair for > --phase-ms) down to "
                         "span ~--phase-ms so a demo walks through every "
                         "command (default off: real wall-clock timeline; "
                         "--no-phase-compress is the default)")
    ap.add_argument("--phase-ms", type=int, default=10000,
                    help="static runs longer than this are compressed to "
                         "span ~this (ms, default 10000)")
    ap.add_argument("--gap-cap", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="clamp silent gaps between beats longer than "
                         "--gap-ms to --gap-ms (default off: every gap keeps "
                         "its real capture length; --no-gap-cap is the "
                         "default)")
    ap.add_argument("--gap-ms", type=int, default=10000,
                    help="silent gaps between beats longer than this are "
                         "clamped to this (ms, default 10000; matched to "
                         "--phase-ms so statics and silence fold alike)")
    args = ap.parse_args()

    paths = [Path(p) for p in (args.files or [DEFAULT_TSV])]
    rows: list[dict] = []
    for p in paths:
        got = _load_rows(p)
        if got is None:
            print(f"  skip {p.name}: not a frames.tsv or raw park feed",
                  file=sys.stderr)
            continue
        rows.extend(got)
        _LOADED.append(p)
        print(f"  {p.name}: {len(got)} row(s)", file=sys.stderr)
    if not rows:
        raise SystemExit("no rows from "
                         + ", ".join(str(p) for p in paths))

    if args.source:
        rows = [r for r in rows if r["source"] == args.source]
    if not rows:
        raise SystemExit(f"no rows for source {args.source!r}")
    rows = [r for r in rows
            if (args.start_tick is None
                or int(r["tick"]) >= args.start_tick)
            and (args.end_tick is None
                 or int(r["tick"]) <= args.end_tick)]
    if not rows:
        raise SystemExit("no rows within the tick window")

    sources = sorted({r["source"] for r in rows})
    if args.no_collapse:
        print("\n".join(_header(args, sources)))
        print()
        for row in rows:
            print(f"@{int(row['tick']) + args.offset}")
            print(f"hex {row['hex']}")
            if row["summary"]:
                print(f"  # {row['summary']}")
        return

    def emit(beats: list[tuple[int, list[str]]],
             out: list[str]) -> tuple[int, int]:
        beats_n = cascades_n = 0
        for t, blines in beats:
            out.append(f"@{t}")
            beats_n += 1
            for bline in blines:
                out.append(bline)
                if bline.startswith(("cue ", "cascade ")):
                    cascades_n += 1
        return beats_n, cascades_n

    header_lines = _header(args, sources)
    if args.split_shows:
        n_files = n_beats = n_cascade = 0
        for src in sources:
            sub = [r for r in rows if r["source"] == src]
            lines = collapse_beat_lines(sub)
            beats = _group_beats(lines)
            if args.skip_prelude:
                beats = _skip_prelude(beats)
            for idx, (lead, body, gap) in enumerate(
                    _split_shows(beats, min_static_ms=args.phase_ms,
                                 lead_in_ms=args.phase_ms), start=1):
                if lead:
                    shift = (lead[-1][0] + gap) - body[0][0]
                    joined = list(lead) + [(t + shift, bl) for t, bl in body]
                else:
                    joined = body
                joined = _retime(joined, trim=args.trim,
                                 lead_in=args.lead_in, offset=args.offset,
                                 head_pre_roll_ms=_head_pre_roll(sub, joined)
                                 if args.trim else 0)
                if args.gap_cap:
                    joined = _cap_gaps(joined, max_gap_ms=args.gap_ms)
                if not joined:
                    continue
                out: list[str] = [*header_lines,
                                  f"# {src}: show {idx} "
                                  f"({len(joined)} beat(s))", ""]
                beats_n, cascades_n = emit(joined, out)
                n_files += 1
                n_beats += beats_n
                n_cascade += cascades_n
                path = f"{args.out_prefix or src}.show{idx}.msh"
                with open(path, "w") as fh:
                    fh.write("\n".join(out) + "\n")
                print(f"wrote {path} "
                      f"({beats_n} beat(s), {cascades_n} cascade(s))")
        print(f"# {n_files} show file(s), {n_beats} beat(s) total, "
              f"{n_cascade} collapsed cascade(s)",
              file=__import__("sys").stderr)
        return

    print("\n".join(header_lines))
    print()
    n_beats = 0
    n_cascade = 0
    for src in sources:
        sub = [r for r in rows if r["source"] == src]
        lines = collapse_beat_lines(sub)
        beats = _group_beats(lines)
        if args.skip_prelude:
            beats = _skip_prelude(beats)
        beats = _retime(beats, trim=args.trim, lead_in=args.lead_in,
                        offset=args.offset,
                        head_pre_roll_ms=_head_pre_roll(sub, beats)
                        if args.trim else 0)
        if args.phase_compress:
            beats = _compress_phases(beats, target_ms=args.phase_ms)
        if args.gap_cap:
            beats = _cap_gaps(beats, max_gap_ms=args.gap_ms)
        if not beats:
            continue
        if src != sources[0]:
            print()
        print(f"# --- {src} ---")
        out: list[str] = []
        beats_n, cascades_n = emit(beats, out)
        n_beats += beats_n
        n_cascade += cascades_n
        print("\n".join(out))
    print(f"# {n_beats} beat(s) total; {n_cascade} collapsed cascade(s)",
          file=__import__("sys").stderr)


if __name__ == "__main__":
    main()