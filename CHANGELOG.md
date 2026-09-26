# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`python/mwm.__version__` is the single source of truth for the library
version; `make release` derives the git tag from it, so a tag can never
drift from the version it claims to release.

## [Unreleased]

### Fixed

- `mwm-send.py --dump sequence` no longer requires the rig's MQTT config.
  `--dump` is the tool's offline mode — it prints the transmit stream and
  returns before any publish — but it still loaded
  `~/.config/ir-remote-tools/mqtt.json` unconditionally, so checking a show's
  transmit stream offline needed live rig credentials:

  ```
  FileNotFoundError: [Errno 2] No such file or directory:
    '.../.config/ir-remote-tools/mqtt.json'
  ```

  `compare_capture.py` and the park-source fidelity tests shell out to
  `mwm-send.py --dump`, so the test suite could only pass on a machine with the
  rig config present. The one-shot and interactive verbs have no `--dump` guard
  and always publish, so they still load the config and still fail loudly
  without it.
- CI installs `paho-mqtt` before `make test`; the suite loads the rig tool
  modules, which import it at module level.

## [0.2.0] - 2026-09-25

### Added

- LICENSE: the library and tools are licensed under LGPL-2.0 (`COPYING` holds
  the GPL-2.0 text it incorporates by reference).
- `msh-to-pronto.py`: convert a `.msh` show script to 38 kHz Pronto hex for
  offline decoder testing.
- `compare_samples_dryrun.py`: dry-run every shipped `samples/*.msh` through
  `mwm-send.py --dump` and check the transmit stream against its source
  capture rows — no invented codes, min-gap floor respected, countdown
  pre-roll never overlapping the next command.

### Changed

- **Rig tools now require the `paho-mqtt` package and no longer use the
  `mosquitto_pub`/`mosquitto_sub` CLIs.** `_mqtt.py` wraps one persistent
  paho client with a background loop thread, so a publish is a write to an
  already-open socket instead of a fresh TCP connect + MQTT handshake, and
  the broker password no longer sits in a subprocess argv. The symptom this
  fixes: a ~1 s transient stall on one countdown member's publish delayed
  the whole FEC chain, and the ears heard a beat fire ~1 s late.
  The `mwm` library itself remains stdlib-only and dependency-free.
- `gen_show_script.py` collapses a captured countdown run to a single
  `cue hex 20 <tail>` go-variant cue anchored at the run's GO tick, replacing
  the capture-lead `cue hex F4 <tail>` form. `mwm-send.py` regenerates the
  canonical `FD..F1,20` chain itself, pre-rolled backward from the cue's
  `@ms` so the `20` fires exactly on the beat. A cue no longer carries an
  arbitrary lead derived from whichever member happened to open the capture,
  and the countdown cadence now has one canonical source. The captured
  countdown's start byte and go tick stay in the `#` comment on the cue line.
- Countdown cues are exempt from `mwm-send.py`'s default 2x repeat (the
  chain already is its own FEC; re-airing it doubles every value), with an
  explicit `--repeat` still winning.
- `mwm-send.py sequence` reads its show script from stdin when no file is
  given, and accepts `-` as an explicit stdin marker, so a captured show
  pipes straight in:

  ```sh
  gen_show_script.py --no-collapse --no-trim capture.mwm | mwm-send.py sequence
  ```

- Shipped `samples/*.msh` and `samples/replay/*.msh` regenerate to the new
  cue form.

## [0.1.0] - 2026-09-21

- First tagged release, after the split out of the `ir-remote-tools`
  monorepo.
- `mwm` library: MWM / Glow-With-The-Show ear protocol framing, CRC-8/Dallas
  checksums, 30-shade palette tables, phrase/bundle decoder, and the
  tick/width timing codec. Pure stdlib, no Home Assistant.
- `mwm.unbundle`: split a bundle frame into its component frames, mirroring
  the TypeScript MWM library.
- `mwm.__version__`, exported through `__all__` and pinned to a PEP 440
  shape by `test_contract.py`.
- Rig research tools, the MWM capture corpus in `samples/`, and the protocol
  reference in `docs/mwm-show-protocol.md`.

[Unreleased]: https://github.com/bwarden/python-mwm/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/bwarden/python-mwm/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/bwarden/python-mwm/releases/tag/v0.1.0
