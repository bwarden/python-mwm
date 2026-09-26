# Agent notes

## Commit message prefixes

Commits are prefixed with the functional area they affect: `python:`,
`tools:`, `samples:`, `docs:`, `analysis:`, `repo:`. Mixed changes take the
dominant area's prefix.

## Layout

This repo holds the MWM protocol library (`python/mwm/`, stdlib-only) and
the standalone rig research tools (`tools/`), with shared `samples/`,
`docs/`, and `analysis/`. Run python tests via `make test` (`cd python &&
PYTHONPATH=. python -m unittest discover -s tests`); platform/HA modules are
not installed here — only the library is unit-tested.

- MQTT IR test rig: broker, credentials, and the Tasmota transmitter/receiver
  topics live in `~/.config/ir-remote-tools/mqtt.json` (outside this repo).
  Transmit raw codings on the `transmit` topic and hear back timings +
  IRremoteESP8266 decodes on the `receive` topic.
- The MWM / Glow-With-The-Show protocol reference is
  `docs/mwm-show-protocol.md`; verified command frames are in
  `samples/mwm-gwts-colors.tsv`.
- Assumptions and decoding/encoding techniques shared by the MWM library
  ports are in `docs/library-assumptions.md`; the timing gotchas below are a
  distillation of it. When changing a decoder here, keep the TS (`web-ui`
  repo) and Perl (`perl-protocol-ir` repo) ports consistent.

## Releases

`python/mwm/__init__.py`'s `__version__` is the version source of truth;
`make release` derives the tag from it, so bump the version and add the
`CHANGELOG.md` section *before* running it (it refuses a dirty tree, a
duplicate tag, or a changelog with no matching section). Release notes live in
`CHANGELOG.md`; the tag is the pin vendored consumers (`ha-mwm-ears`) use. CI
(`.github/workflows/test.yml`) only runs `make test` — it never publishes.

## Replay validation (tools + overnight runs)

`tools/replay_run.py` plays a captured park show back verbatim and checks
both directions: `.msh` body vs the generator's own collapse rule, and the
rig's receiver log vs `mwm-send`'s own send plan (countdown pre-roll,
off-slot member drop, min-gap clamp, end-of-show reset `9060A6`). Evidence
is ephemeral by
design -- `analysis/replay/runs/` and `*.log` there are gitignored; only
`samples/replay/*.msh` is tracked. Findings from the first overnight run
(2026-09-08, all four captures): every planned member was emitted at the
correct cadence (skew ≈ 1.000 over 12-32 min runs, incl. long silent-gap
pauses), and every frame the receiver reported decoded to exactly the
expected frame; the ~4-13% "missing" frames cluster in brief receiver-deaf
streaks (1-4 s, ESP8266 RESULT-loop/WiFi stalls) not send errors. End-of-show
resets flake at loopback too; `compare_replay` downgrades an unheard
trailing `9060A6` to a warning unless `--strict-end-reset`. `mwm-send`
emits that reset after every sequence unless `--no-end-reset` -- get the
flag right in offline/dry-run replays, and expect scripts to finish with
the reset frame in the send plan.


## MWM timing gotcha: trailing spaces merge into the inter-message gap

Any code that DECODES MWM timing data (rawbuf / mode2 / framework
signed-us runs) must handle this, in every language variant (TS, Perl,
Python have all been bitten): the encoder merges each byte's stop space --
and any immediately preceding space-valued levels (trailing 1-bits) --
into the ~30 ms kMWMMinGap footer. A message can therefore end mid-byte
with one or more logical space levels riding invisibly at the head of the
wide gap run instead of appearing as explicit short runs. Decoders must
back-fill those levels from the gap (data bits = 1s, then the stop bit)
before treating the remainder as a separator; checksum validation keeps
the back-fill honest for truncated captures. The mirror-image TX-side bug
is forgetting that raw_timings()/toPronto output ENDS with the merged
big-space run, never a tidy per-byte stop.