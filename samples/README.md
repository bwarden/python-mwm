# This is a collection of sample files for testing and reference

This directory is shared by every subproject in this repository (`web/`,
`perl/`). Both test suites read from here.

## Hand-rolled CSV files
These files contain codes I personally collected, using a Tasmota device, from remotes in my possession. The Protocol name, Bits, Data, and DataLSB fields are copied/pasted from the Tasmota IrReceived result.
  - IR Remote Control Codes - JVC.csv
  - IR Remote Control Codes - NEC.csv
  - IR Remote Control Codes - Samsung36.csv

## Capture logs from tasmota
These files are logs of MQTT subscriptions to tasmota RESULT topics, containing a variety of devices spanning multiple protocols, and some complete remote captures, good for testing the ability to match a remote from IRDB.
  - tasmota-capture.log
  - tasmota-jvc-vcr-capture.log
  - tasmota-samsung-tv-capture.log

The Perl test suite additionally scans this directory for any file matching
`tasmota*.log` and validates that every decodable frame round-trips through
the Tasmota and Pronto formats, so extra captures dropped here (e.g.
`tasmota.log`) are exercised automatically.

## MWM / Glow With The Show color commands
Reference data for generating Disney "Made With Magic" (a.k.a. Glow With The
Show) ear-hat commands with the MWM protocol. `mwm-gwts-colors.tsv` is a
machine-readable table of ready-to-transmit hex frames (single frames or
`+`-joined sequences) distilled from the DIYChristmas forum thread; the
physical-layer framing, CRC-8 checksum (poly 0x31, reflected), the
both-ears/left-ear-only modifier rules, and the 30-entry palette are
documented in `../docs/mwm-show-protocol.md` (sections 1, 2 and 4; a
byte-by-byte walkthrough of the key frames is section 12). See that file
for sources.
  - mwm-gwts-colors.tsv

## MWM paintbrush / friend-code bundles
  - viperfan91-paintbrush.txt — decoded paintbrush bundles posted by
    viperfan91 (DIYChristmas thread post-344802, the "_Fiss_ blue glow
    v1" era, read ~2026-09). Each bundle pairs the `96 19`-prefixed
    broadcast op with its `9x 24`-prefixed effect companion. Two rows
    are corrupt as transcribed and are excluded from use but kept for
    provenance.

## MWM capture corpora
Real MWM traffic used to validate the protocol reference in `../docs/mwm-show-protocol.md`:
  - MRDF0007.TXT, MRDF0008.TXT — Mouse Ear Recorder exports of two 2014
    Disneyland sessions (3810 decoded frames with timestamps).
  - headband-20260822.log — Tasmota MQTT RESULT stream of an idle ear hat's
    unsolicited demo chatter overnight (RawData timings + receiver decodes).
  - sync-analysis-20260827-104756.json, sync-analysis-20260827-104912.json —
    short clock-tick sync captures (from `../tools/sync_analysis.py`). Each
    shows both receivers reporting the identical tick per burst, wrapped in
    the same milliseconds — the MQTT-clumping artifact the docs warn about
    (do not infer burst timing from arrival).

  - EMLG000E_filtered.txt, EMLG0026_filtered.txt — 2025 EMLG park hat
    capture corpora (3400+ / 1500 decoded frames).  Source for all the
    delay-led fade/strobe cascades and the per-ear palettized set-pieces.

  - park-cascade-demo.msh and the generated park shows — `sequence`
    show-scripts (`.msh` format: `@ms` offset lines + one beat per line,
    `#` comments; `cascade` lines expand to a sparse countdown chain, one
    member every `--cascade-ms` -- default 400 ms, ~2-3 sends/s).
    park-cascade-demo is hand-curated and starts its fade at `@0` (the
    genuine ~27 s idle gap after the fade is compressed to ~3 s).  The
    generated demos cover every park capture in `analysis/park/frames.tsv`,
    split into ONE `.msh` PER SHOW at the long static idle runs
    (`gen_show_script.py` runs this way by default, `--split-shows`):

    - MRDF0008.TXT  — 5 shows (`samples/park-MRDF0008.TXT.show1..5.msh`)
    - MRDF0007.TXT  — 5 shows (`samples/park-MRDF0007.TXT.show1..5.msh`)
    - EMLG000E_filtered.txt — 4 shows (`...show1..4.msh`)
    - EMLG0026_filtered.txt — 6 shows (`...show1..6.msh`)

    Long static segments are FOLDED, never eliminated — the idle cue the
    park meters out before a show's first change of state becomes that
    show's LEAD-IN, replayed at the run's OWN capture cadence (a several-
    minute idle becomes a ~10 s reminder; folding never makes a repeat
    denser than the capture).  A static run with no show behind it (the
    capture tail) is dropped.  Show bodies keep the captured beats at
    their real timing; silent gaps longer than `--gap-ms` 10 s are
    clamped to 10 s, so statics and silence fold to the same ~10 s scale.
    Each last show ends with the capture's EXIT TAIL — the solid-color
    and plain-command rows after the final effect beat (e.g.
    `park-MRDF0008.TXT.show5`'s `both ears off` + magenta/yellow go, or
    the white countdown collapsed to a `cascade` in the MRDF0007 show5 /
    EMLG000E show4 close) — so ears settle to their captured resting
    state instead of looping the last effect forever.
    `mwm-send` plays the `.msh` VERBATIM (it never collapses or
    fills gaps).  Regenerate any/all with, e.g.::

        python3 tools/gen_show_script.py --source MRDF0008.TXT \
            --out-prefix samples/park-MRDF0008.TXT

    (writes `samples/park-MRDF0008.TXT.showN.msh`).  The old single-file
    demos are one `--no-split-shows` run away::

        python3 tools/gen_show_script.py --source MRDF0008.TXT \
            --no-split-shows > samples/park-MRDF0008.TXT.msh

    Dry-run with `mwm-send.py --dry-run --repeat 1 sequence`; a `sequence`
    send runs one pass (no `--repeat` self-doubling) and enforces a
    `--min-gap-ms`
    30 ms floor between publishes so same-tick clusters don't burst.
    What was sent scrolls to stdout (capture with `>`/`|`); the "next in"
    status is pinned to the last line on the controlling terminal and
    refreshed only when a publish happens (the terminal is quiet between
    beats).  Edit the scripts freely — that's the purpose of the format.
