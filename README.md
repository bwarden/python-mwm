# IR Remote Tools — python-mwm

Split out of the former `ir-remote-tools` monorepo: the self-contained MWM
protocol library, the standalone rig research tools, and the shared MWM
capture corpus and protocol documentation. The TS browser UI and the
`Protocol::IR` Perl distribution now live in their own repositories; the
Home Assistant integration lives in the separate `ha-mwm-ears` repo.

## Layout

| Path        | Contents |
|-------------|----------|
| `python/`   | MWM protocol library (`python/mwm/`): framing, CRC-8/Dallas, palette tables, phrase decoder, timing codec — self-contained, stdlib-only, no Home Assistant. See `python/README.md`. |
| `tools/`    | Standalone Python rig research tools for driving/analysing real MWM ears over MQTT/Tasmota IR (no Home Assistant required). See the tools section below. |
| `samples/`  | IR captures and command tables used by the test suite and the tools (park/hat captures `EMLG*`, `MRDF*`, the MWM command table `mwm-gwts-colors.tsv`, parse-test `*.msh` scripts, hand-rolled CSVs). See `samples/README.md`. |
| `analysis/` | Derived analysis data: `analysis/park/frames.tsv` is the park frame table `tools/gen_show_script.py` reads. |
| `docs/`     | Protocol documentation: the MWM / Glow-With-The-Show show-command reference (`docs/mwm-show-protocol.md`) and the cross-port assumptions/techniques doc for our MWM libraries (`docs/library-assumptions.md`). |

## Build and test

```sh
make build    # compileall the mwm library + tests
make test     # stdlib unittest suite
make samples  # regenerate samples/park-*.msh + samples/replay/*.msh
```

## Rig research tools (`tools/`)

`tools/` holds standalone Python rig-research utilities for probing real
MWM ears over the MQTT/Tasmota IR test rig. They talk only to MQTT and the
`mwm` protocol library at `python/mwm/` — no Home Assistant is installed or
needed. They require `mosquitto_pub` / `mosquitto_sub` on PATH, the MQTT
config at `~/.config/ir-remote-tools/mqtt.json` (never committed), and are
run from the repo root or the `tools/` directory.

| Tool | Purpose |
|------|---------|
| `mwm-send.py` | Interactive full-catalog show-command sender (colors, palette, effects, timers/modifiers, group addressing, clock-write, 55AA messages, raw hex, `cascade` countdown chains, `sequence <file>` show-script runner) with optional live beacon readback (`--monitor`); sequence cascade beats go out as one tight burst -- a cue's members fire back-to-back at the `--min-gap-ms` floor, the way the park airs a packed moment (an explicit `--cascade-ms` opts back into paced airing; `--cascade-full` restores the 14-member capture chain), a `cascade hex ... members <...>` clause (as `gen_show_script` emits) rebuilds the capture's EXACT countdown set byte-for-byte, and the show is played VERBATIM; `--dump` prints the full transmit stream (`@ms HEX` per frame, reset included unless `--no-end-reset`) without sending; after the last beat an end-of-show reset (`90 60 A6`, both ears off) is published unless `--no-end-reset` (the ears are remote and unobserved, so default to a guaranteed-off state); what was sent scrolls to stdout (`>`/`|` capture it) while the live "next in" countdown is pinned to the last line on `/dev/tty`, and it enforces a `--min-gap-ms` inter-publish floor. |
| `gen_show_script.py` | Turn a captured `analysis/park/frames.tsv` into `sequence`-consumable show-scripts (`.msh`): countdown-cascade runs collapse to one `cascade hex <lead> <tail> members <set>` cue that CARRIES the capture's exact countdown set (mid-show 93-command/97-colour chains admitted as real cues, like effect chains), everything else stays a literal `hex` beat (tick ≈ ms). Raw park feeds are accepted DIRECTLY too (`*.mwm` hex dumps, `*_filtered.txt` oPossum dumps, `*_reader.TXT` Mouse Ear Recorder exports — the left column is read as the per-frame tick, CRC-valid frames only; frames packed onto ONE capture line are de-flattened at +100 ms per frame so an aired chain keeps its real cadence). By default the timeline is FAITHFUL to the capture: `--trim` on (first kept beat at `@0`, relative gaps kept), every beat keeps its own de-flattened tick, cascade collapse on with exact member sets (`members` preserves repeats — e.g. three 96-chains run together). Condensed demo modes are opt-in: `--split-shows` (one `.msh` per show, preceding static run as a lead-in), `--phase-compress` (fold statics to ~`--phase-ms` at their own cadence), `--gap-cap` (clamp silent gaps). |
| `capture_receive.py` | Passive, durable recorder for the rig's receiver RESULT topic → JSONL (`{t_unix, t_iso, raw}`, raw = the exact broker line for re-decoding). Bounded `mosquitto_sub -C 1 -W 30` one-message subscriptions re-issued each loop, so a dead/quiescent broker never wedges it. |
| `compare_capture.py` | Offline fidelity harness across every captured park show: runs `gen_show_script` on the capture, feeds the `.msh` through `mwm-send --dump`, and proves the transmit stream IS the capture's kept rows — in order, byte-for-byte, with nothing dropped except the documented classes (55aa/beacon receiver rows, lone non-chain idle singles) and nothing invented. Also guards the members-clause rebuild (collapse rule → dump must agree) and reports member-tick drift vs the capture's ~100 ms countdown cadence as INFO. `--no-collapse` audits the verbatim (wall-clock) mode too. |
| `compare_replay.py` | Two-way evidence that a replay re-produced the capture: (1) **conversion** — the `.msh` body must re-derive from `gen_show_script`'s own collapse rule over the capture rows, one-for-one; (2) **replay** — the receiver log must align with `mwm-send`'s own plan (cascade expansion — including captured `cascade hex ... members ...` sets rebuilt byte-for-byte — back-to-back member cadence at the min-gap floor, end-of-show reset) frame-for-frame: per-cascade member cadence, timing skew/offset, and jitter raw + de-clumped (Tasmota's RESULT delivery is software-paced, so clumped arrivals are labelled, not treated as hardware timing). Reports missing/unexpected and a machine-readable `RESULT: PASS|FAIL`. A trailing end-of-show reset the receiver didn't re-report is a warning, not a failure (the short `90 60 A6` riding ~30 ms after the last beat is often swallowed by the receiver's same-burst handling). |
| `replay_run.py` | Overnight, no-interaction orchestration: for every source, build the verbatim-timeline `.msh`, start the recorder, play it through `mwm-send sequence`, and compare — evidence lands in `analysis/replay/runs/<session>/` (`rx-*.jsonl`, `send-*.log`, `report-*.txt`, `summary.csv`, `manifest.json`). `--smoke` runs a 3-beat rig test; `--dry-run` is an offline conversion-only gate that validates each script BEFORE any IR goes out. |
| `capture_beacons.py` | Long-running beacon capture to a JSONL file for offline study. |
| `analyze_beacons.py` | Offline analysis of a captured beacon log: tick rates, cross-receiver agreement, effect index tracking. |
| `annotate_captures.py` | Passive demo/wand listener (or stdin RESULT replay) that decodes each burst from `RawData` and asks the human what it actually did; A-B-A' wand pushes are reassembled across RESULT lines into one complete bundle (one prompt, one log row), known demo effects prompt for a confirm/correct verdict, cycle-sibling bursts (same command, only timing moved) are collapsed to a phase delta, and logging captures verified annotations for corpus growth. |
| `color_cycle.py` | Walk the full verified command catalog one at a time, prompting for human notes; logs results for `analyze_log.py`. |
| `analyze_log.py` | Offline decode of a `color_cycle.py` log (or raw hex / timings) into a human-readable report. |
| `clock_write_experiment.py` | Focused probe of the `0C t` sync-clock-write opcode on real ears. |
| `sync_analysis.py` | Older all-in-one capture + tick-sync analysis + clock-write/group experiments (predecessor of the focused tools above). |
| `_bootstrap.py`, `_mqtt.py` | Shared helpers: locate/import the `mwm` library (at `python/mwm/`), and load MQTT config + send/capture wrappers. |

See `python/README.md` for usage of `color_cycle.py` and `analyze_log.py`.
Every tool has an intramodule docstring with `PURPOSE`/`USAGE`.

## Trademark notice

This repository exists solely to enable interoperability with
independently purchased hardware. It is an independent community project:
it is not supplied by, authorized by, affiliated with, or endorsed by The
Walt Disney Company or any other rights holder. "Made With Magic",
"Glow With The Show", and all related names and marks are trademarks of
their respective owners, referenced here only to identify interoperable
functionality.