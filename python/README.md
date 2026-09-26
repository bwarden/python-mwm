# MWM protocol library: `mwm`

Self-contained Python implementation of the Disney "Made With Magic" /
Glow-With-The-Show ear protocol: framing, CRC-8/Dallas, palette tables,
phrase decoder, and timing codec. Pure stdlib, no dependencies, no Home
Assistant.

> **Disclaimer:** Independent community project for interoperability with
> independently purchased hardware. Not supplied by, authorized by,
> affiliated with, or endorsed by Disney. "Made With Magic", "Glow With The
> Show", and all related names and marks are trademarks of their respective
> owners.

## Layout

| Path | Purpose |
|------|---------|
| `mwm/` | the protocol library (framing, timings codec, decoder, palette) |
| `tests/` | stdlib `unittest` suite for the library |
| `tools/` (repo root) | rig research tools that consume this library over MQTT |

The library is the shared decoding/encoding core used by the rig tools in
`../tools/`. The Home Assistant custom integration (`mwm_ears`) lives in
the separate **ha-mwm-ears** repository and consumes the same protocol.

## Tests

Stdlib only — Home Assistant is not installed or needed here:

```
make test   # from the repo root
cd python && PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Running `make test` at the repo root first runs a `compileall` syntax check
of the library and tests (`build`), then the suite.

## Consuming the library

With this directory on `PYTHONPATH` the package imports as `mwm`:

```python
from mwm import build_frame, describe_frame, decode_timings, raw_timings
from mwm.palette import PALETTE, SIMPLE_COLORS, color_palette
from mwm.decode import describe_bundle, EarStateTracker, effect_label
```

- `mwm.protocol` — framing/CRC-8, group/palette builders, clock-write,
  `irsend_payload` raw-timing serialization.
- `mwm.timings` — the tick/width codec and Tasmota raw-timing helpers.
- `mwm.decode` — phrase/bundle description and ear-state tracking.
- `mwm.palette` — the 30-shade palette, one-bit simple colors, and hue-based
  nearest-color snapping.

Documentation of the wire format itself is shared at
`docs/mwm-show-protocol.md` (this repo), and the cross-port assumptions for
the TS/Perl/Python MWM libraries at `docs/library-assumptions.md`.