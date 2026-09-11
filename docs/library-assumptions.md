# Library assumptions and techniques

Status: working. A companion to `docs/mwm-show-protocol.md` (which documents
**what** the MWM / Glow-With-The-Show protocol is). This file documents **how
our libraries** — the three coordinated MWM implementations — decode and
encode the signal, the assumptions they rely on, the shared technique for
dealing with Tasmota's capture artefacts, and the invariants that keep the
three ports consistent.

The three implementations are deliberately parallel governments of one
behavior:

| Port  | Decode                                             | Encode                                          | Location |
|-------|----------------------------------------------------|-------------------------------------------------|----------|
| TS    | `decodeMWM`                                        | `toPronto` (+ `timingsForFrame`)                | `web/src/lib/protocol/mwm.ts` |
| Perl  | `_decode_mwm` -> `decode_timing`                   | `to_pronto`                                     | `perl/lib/Protocol/IR/Proto/MWM.pm` |
| Python| `decode_timings`                                   | `timings_for_frame` / `raw_timings`             | `python/custom_components/mwm_ears/_mwm/timings.py`, `protocol.py` |

The `docs/mwm-show-protocol.md` source-of-truth labels apply here too: **[R]**
rig-verified, **[P]** park-derived, **[T]** community thread, **[H]**
hypothesis. Almost every numeric constant below is **[R]** (measured/validated
on the MQTT test rig against real ears).

---

## 1. Physical-layer model (shared)

All three ports start from the same abstract signal (2400 bps UART over a
38 kHz carrier):
1 start mark tick, 8 data bits **LSB-first**, **space = 1**, 1 stop space
tick, per byte; equal adjacent levels merge into single runs; the message
ends in the ~30 ms inter-command gap.

Everything downstream is measured in *ticks* — a logical bit is one
`TICK_US` (417 µs) unit, and a decoded byte always occupies exactly 10 ticks
(start + 8 data + stop). Runs of equal ticks merge on air, so a decoder
expands a measured width back into a whole number of ticks (1..9 legal).

Sign convention (differences across the ports are cosmetic, not semantic):

- **TS/Perl decode** consume IRrecv's *run-length timing array* (`rawbuf`):
  entry 0 is a leading gap, then alternating mark/space.
- **Python decode** consumes *signed microseconds*: positive = mark, negative
  = space (`timings.py::decode_timings`), the HA infrared-framework
  convention. This is the only real input-format difference between the ports.
- **Encode** everywhere starts with a positive (mark) tick per start bit,
  negative (space) for space-valued levels, and merges same-sign runs.

### 1.1 Constants (identical across all three ports) [R]

| Constant      | TS `k…` | Perl `$k…` | Python            | Value       | Meaning |
|---------------|---------|------------|-------------------|-------------|---------|
| tick          | `kTick` | `$kTick`   | `TICK_US`         | 417 µs      | per logical bit |
| max run width | `kMaxWidth` | `$kMaxWidth` | `MAX_WIDTH_TICKS` | 9 ticks | widest legal in-frame run |
| tolerance     | `kDelta` | `$kDelta`  | `DELTA_US`        | 150 µs      | `+/-` width tolerance |
| inter-msg gap | `kMaxGap` | `$kMaxGap` | `MAX_GAP_US`      | 20 000 µs   | wider space = inter-message separator |
| footer gap    | `kFooterGap` | `$kFooterGap` | `FOOTER_GAP_US` | 30 000 µs | delay appended on encode (`kMWMMinGap`) |
| min bits      | `kMinBits` | `$kMinBits` | `MIN_BITS`        | 24         | 3 bytes |
| max bits      | `kMaxBits` | `$kMaxBits` | `MAX_BITS`        | 144        | `(15+3)*8`, payload-nibble cap |
| state cap     | `kStateSizeMax` | `$kStateSizeMax` | (loop bound) | 55 bytes | upstream IR buffer cap |
| carrier       | `38000`   | `38000`     | `CARRIER_HZ`      | 38 kHz     | IR carrier |

The three decoders also share a byte-boundary budget: a byte is 10 ticks, and
`frameBits % 10` identifies the current slot (0 = start bit, 9 = stop bit,
1..8 = data bits LSB-first).

### 1.2 Width matching technique

A measured run-width is converted to a whole number of ticks by trying widths
from `MAX_WIDTH_TICKS` down to `1` and accepting the first whose expected
duration is within `+/- DELTA_US` of the measurement (`timings.py::_match_ticks`,
`MWM.pm::_get_rc_level`, the `matchWidth` helper in `mwm.ts`). It mirrors
IRremoteESP8266's `IRrecv::match(kMWMTick, 0, 0, kMWMDelta, kMWMMaxWidth)`.

A width that matches no whole tick count is treated as noise and tears down
the current message (`finalize` / `undef`), never guessed.

---

## 2. Frame validity: two competing rules

A decoded byte string must pass the *implied-length* checks. There are two
families, distinguished by the leading bytes:

1. **Show message `0x9L …`** — low nibble `L` of byte 0 equals `total - 3`
   bytes (`docs/mwm-show-protocol.md` §2). The trailing byte is **CRC-8/Dallas**
   over everything before it (poly `0x8C` reflected, init/final `0x00`).
2. **System message `0x55 0xAA …`** — length carried by the capture itself;
   trailing byte is the **additive checksum** (`sum(payload) mod 256`) [T].
   No CRC applies.

Every port implements `validate`/`_validate_frame`/`frame_is_valid` around
both rules, and each encodes with the matching checksum builder
(`crc8_dallas` / `build_55aa` in `protocol.py`, `crc8()`/`_crc8` in `mwm.ts`
and `MWM.pm`).

### 2.1 CRC-8/Dallas (shared helper)

```python
def crc8_dallas(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc
```

Identical logic lives in `web/src/lib/protocol/mwm.ts::crc8` and
`Protocol::IR::Proto::MWM::_crc8` (which mirrors `Protocol::IR::MWMProbe`).
The Perl/TS copies are kept self-contained (no cross-module call) deliberately,
so each port is a drop-in; the `_crc8` duplicate of `MWMProbe` is by design.

---

## 3. Watch out: three timing gotchas (every port has been bitten)

These capture-side artefacts are *the* reason the three ports need care, and
why the tail-recovery logic exists. All three are documented in the protocol
ref; this section is the decoder-first view.

### 3.1 Trailing spaces merge into the inter-message gap

The **encoder** merges each byte's stop space — and any *space-valued* levels
immediately before it (trailing 1-data-bits) — into the ~30 ms gap footer:

```
...+417 -834 -30000        (two 1-bits + stop space would be -1251 then gap)
```

becomes

```
...+417 -30834             (merged into one wide space run)
```

`raw_timings()`/`toPronto`/`timings_for_frame` output therefore *always end
with a merged big-space run*, never a tidy per-byte stop.

**Consequence:** a decoder that turns a width into ticks sees the *remaining
space-valued levels of the final byte* riding invisibly at the head of the
wide gap run. Decoding must back-fill those levels from the gap — data bits
are `1`s by definition, then the stop bit — before treating the remainder as
a separator. This is `timings.py::_backfill_tail` and the equivalent
`if (!$done && $frameBits % 10 != 0) { … }` block in the TS/Perl decoders.

The back-fill only ever fires when the natural decode came up short
(`!done && frameBits % 10 != 0`), and the result is re-validated (length rule
**and**, for a back-filled trailing byte, the checksum) so a truncated/forged
tail cannot silently decode.

### 3.2 End-bit swallowing (missing content byte)

Receivers that sample run-widths (IRremoteESP8266, Tasmota) can **lose a whole
trailing content byte** whose final data bits are `0`: those bits scrunch into
the stop bit and the inter-frame gap (protocol ref §2 "End-bit swallowing").
The captured frame ends one byte short of the length rule, but the *true CRC*
survives as the captured last byte.

Recovery technique: when `len(frame) == (data_header_length + 3) - 1` (the
length nibble says one byte is missing), brute-force the single missing byte
`0..255` against the CRC and accept the match
(`timings.py::_validate_frame`, protocol ref §2). A random 256-search yields
~1 false positive on average — tolerable far better than dropping beacons.

The **Python** timing decoder handles this case; the TS/Perl decoders are the
IRremoteESP8266-architecture ports where this exact path is the responsibility
of the cross-check between `bits` and the length nibble. See §5 for how strict
TS/Perl are about it vs. how the Python HA port is intentionally lenient.

### 3.3 Footerless captures (Tasmota RawData)

The HA consumer works only from **Tasmota `RawData` timings**, and *ignores
the `Data` field* (which is result of the on-device non-strict decoder and is
not reliable). A Tasmota receive record omits the ~30 ms inter-command gap, so
a message's final stop space (and trailing 1-bits) can be missing entirely —
the signal simply ends mid-byte. The decoder cleans this up with the same
back-fill as §3.1, applied at the *clean end of capture* rather than at a gap
run (`timings.py::decode_timings` calls `_backfill_tail()` after the loop;
the TS/Perl ports likewise attempt natural-first then tail back-fill).

The combined rule for all three decoders:

- Back-fill spaces (1s) for the remainder of the in-progress byte.
- Accept a naturally-valid frame as-is (complete frames, e.g. a `55 08 08`
  show capture with trailing interference, must NOT be folded into a
  fabricated extra byte).
- Accept a back-filled frame only when length **and** checksum agree.

This is why the tail recovery must be *natural-first*: validating the un-back-
filled state before attempting back-fill stops the recovery from inventing a
4th byte for an otherwise-complete 3-byte frame.

---

## 4. Encoding technique (shared across ports)

`timings_for_frame` / `toPronto` / `to_pronto` are three spellings of one
algorithm:

1. Split the data word into bytes (most-significant first), minimum 3 bytes.
2. For each byte emit a flat signed-tick sequence:
   `+417` (start mark), then `+417`/`-417` per data bit (`space = 1`, LSB
   first), then `-417` (stop space).
3. Append `-FOOTER_GAP_US` (30 ms) as the trailing inter-command space.
4. Merge adjacent same-sign runs into single wider runs.
5. Emit either Pronto hex (scaling µs to carrier pulses at 38 kHz) or,
   in Python, the HA framework signed-µs `raw_timings()` list.

The merged footer (§3.1) is a deliberate, inherent part of step 4 — encoders
never emit a tidy trailing stop; the wide space *is* the termination.

Python additionally provides `irsend_payload(frame)` (Tasmota `IRsend
<freq>,<timings>`-ready) and signed-µs `raw_timings` for the HA `Command`
envelope.

---

## 5. Deliberate divergence: Python is lenient, TS/Perl are strict

This is an intentional, documented difference, not a bug to "fix" ([R] the
two consumers have different jobs):

- **TS / Perl** mirror IRremoteESP8266's `decodeMWM` on the *run-length*
  architecture: strict length check, no brute-force content recovery in the
  common path. A mismatched-length frame is rejected rather than silently
  misdecoded (the "strict decoder" in the protocol ref).
- **Python** (`decode_timings`) is the HA consumer: it SPLITS multi-message
  captures at inter-message gaps and returns *every* checksum-valid frame,
  and it runs the end-bit / tail recovery (§3) so wand pushes and our own
  repeated bursts — which arrive as one signal containing several copies —
  are each decoded. The TS decoder instead concatenates across gaps and relies
  on the strict length rule to reject the concatenation.

Both behaviors produce identical results for a single clean frame; they only
diverge on multi-message and truncated captures, where each port's leniency is
chosen to fit its role.

---

## 6. Cross-language consistency invariants

Keep these true when editing any one port, or a bug silently re-enters a
sibling:

1. `TICK_US / kTick = 417`, `DELTA_US / kDelta = 150`,
   `MAX_WIDTH_TICKS / kMaxWidth = 9`, `MAX_GAP_US / kMaxGap = 20000`,
   `FOOTER_GAP_US / kFooterGap = 30000`, carrier `38000`.
2. Decode ordering: **natural-first, then tail back-fill** — never back-fill
   a frame that already validates as-is.
3. Back-filled tail spaces are `1`s (space = 1), and a back-filled trailing
   byte must pass the checksum (CRC-8 for `0x9x`, additive for `55 AA`).
4. CRC-8/Dallas is poly `0x8C` reflected, init/final `0x00`, over all bytes
   *except* the trailing checksum.
5. Encoders always end with the merged wide footer space — never a tidy stop
   bit.
6. The `samples/mwm-gwts-colors.tsv` fixture and its RGB values are the ground
   truth for the palette tables in `python/.../_mwm/palette.py` (which must
   match the TSV descriptions exactly), per protocol ref §4 palette note.

---

## 7. Why it works even though several inputs are "contradictory"

The community thread [T] contains contradictory claims (e.g. the old
single-ear side-assignment dispute for `69`–`6F` — now RESOLVED to physical
LEFT by hardware/viperfan91, see protocol ref §4 — and the `F?` delay
polarity) that the libraries do not lean on. The decoder/encoder only
depend on the **rig-verified** core:
2400 bps UART framing, the length rule, CRC-8/Dallas, and the 30 ms footer.
Where behavior is genuinely ambiguous or unverified, the code prefers the
conservative, hardware-verified path (see `docs/mwm-show-protocol.md` open
questions, §10). `[H]` hypotheses — clock-write scheduling, `42 00 00` identity
block, companion-frame necessity — are never load-bearing for frame validity.
