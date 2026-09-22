# MWM / Glow-With-The-Show protocol reference

Status: working, single canonical reference for the MWM ("Made With Magic" /
Glow With The Show / GWTS) IR protocol, covering physical layer, framing,
frame checksum, the full show-command instruction set, phrase templates,
companion frames, system messages, a byte-by-byte breakdown of known frames,
and the tools/corpus used to derive it. This is the merged successor of the
former `docs/mwm-gwts-protocol.md` and `docs/mwm-show-protocol.md`; all of the
former's content (palette table, command-generation recipe, physical/timing
notes, color hardware verification, sources) now lives here.

Audience: humans and code assistants. Every claim is labeled with its
source and confidence:

- **[T]** from the DIYChristmas forum thread (community reverse engineering,
  some of it contradictory or unverified),
- **[P]** derived from the 2014 Disneyland park recordings
  (`samples/MRDF0007.TXT`, `MRDF0008.TXT`; see "Corpus statistics"),
- **[R]** verified interactively on our MQTT IR test rig
  (transmit via Tasmota `IRsend`, receive via two Tasmota IR receivers;
  frames in `samples/mwm-gwts-colors.tsv`),
- **[H]** hypothesis consistent with all current data but not yet proven.

Companion material:

- `samples/MRDF0007.TXT`, `samples/MRDF0008.TXT` - Mouse Ear Recorder park
  captures, 2014-11-22 (3810 decoded frames with timestamps).
- `samples/headband-20260822.log` - Tasmota MQTT event stream of an idle
  ear hat's unsolicited demo chatter (RawData timings + decodes).
- `samples/mwm-gwts-colors.tsv` - 81 verified frames: simple one-bit colors,
  the 30-shade palette with RGB values, left-ear modifiers, fused frames,
  and multi-burst sequences. Machine-readable ready-to-transmit hex; the
  framing/CRC that generated the `generated-crc8-rule` rows is in
  section 2 below, and every `diyc-*` row is reproduced with sources.
- `samples/viperfan91-paintbrush.txt` - decoded paintbrush/friend-code
  bundles from viperfan91 (diyc post-344802), with effect menus and the two
  rows that are corrupt as transcribed.
- `web/src/lib/protocol/mwm.ts` - TypeScript framing reference (`toPronto`,
  `decodeMWM`): turn the hex frames below into Pronto hex / raw timings.
- `perl/bin/ir-mwm-send` - Perl transmitter used for rig testing.
- `perl/bin/mwm-probe` - interactive probe harness: walks a candidate catalog
  (verified TSV rows + generated hypotheses from section 4), transmits with
  repeats, records verdicts/annotations to an analysable JSONL session log.
- `tools/` - Python rig research harness (no Home Assistant): interactive
  full-catalog sender + beacon readback (`tools/mwm-send.py`), beacon
  capture/analysis (`capture_beacons.py`, `analyze_beacons.py`), color-cycle
  probing and offline log decoding (`color_cycle.py`, `analyze_log.py`),
  and the passive capture->decode->human-annotate listener for demo/wand
  traffic (`annotate_captures.py`). See the root `README.md`.
- Thread: [Glow with *OUR* Show and MSP430G2553 discussion]
  (https://doityourselfchristmas.com/forum/index.php?threads/glow-with-our-show-and-msp430g2553-discussion.25142/)
  (479 posts scraped 2026-08-22; key posts: #121 Jon Fether transmitter +
  FX/F? semantics, #post-259750 oPossum palettes, khyizang p.19-20 opcode
  analysis, djred2000 p.20 consolidation, jstorms D1 sequences).

## 1. Physical layer [T,R]

2400 baud UART/IRDA-SIR serial over a 38 kHz carrier: 1 start bit (mark),
8 data bits (LSB-first, space = 1), 1 stop bit (space).

| Property      | Value                                    |
|---------------|------------------------------------------|
| Bit rate      | 2400 baud -> 417 us per bit ("tick")     |
| Carrier       | 38 kHz                                   |
| Start bit     | mark                                     |
| Data bits     | 8, LSB first, **space = 1**, mark = 0    |
| Stop bit      | space                                    |
| Idle          | mark                                     |
| Inter-message gap | ~30 ms wide space run                |

A byte therefore occupies 10 ticks. Runs of equal bits merge on air; decoders
must expand the measured width-runs back into ticks (widths 1..9 ticks are
legal inside a frame). This is what the MWM protocol handler in
`web/src/lib/protocol/mwm.ts` implements; use it to convert the hex frames
below into Pronto hex / raw timings.

Decoder constants (implementation values from `python/.../_mwm/timings.py`
and `protocol.py`; only the 417 us tick and the 9-tick max width trace to the
reference docs / upstream):

| Constant           | Value                | Meaning                                   |
|--------------------|----------------------|-------------------------------------------|
| `TICK_US`          | 417                  | per-bit / per-tick width in us            |
| `DELTA_US`         | 150                  | decoder width-run tolerance               |
| `MAX_WIDTH_TICKS`  | 9                    | widest legal in-frame run                 |
| `MAX_GAP_US`       | 20000                | wider than this (post-run) is the inter-message gap |

## 2. Frame format

### Show messages [T,P,R]

```
+--------+---------------------------+--------+
| 0x9L   | content (L+1 bytes)       | crc8   |
+--------+---------------------------+--------+
total length = L + 3 bytes
```

- The high nibble of byte 0 is always `9`.
- The low nibble `L` counts as follows: a `96` phrase carries 7 content
  bytes, `99` carries 10, up to `9F` carrying 16. Equivalently
  `total = L + 3`. A 3-byte frame uses `0x90`, a 4-byte frame `0x91`,
  a 9-byte frame `0x96`.
- The trailing checksum is CRC-8/Dallas over all preceding bytes (header and
  payload, excluding the checksum itself). Validated below.
- **Caveat:** at least one hand-crafted fused frame from the thread
  (`94 0E 00 0E 94 11`, see TSV) carries `L = 4` in a 6-byte frame where the
  usual rule gives 3 — lenient decoders accept it, strict ones (including
  IRremoteESP8266's) reject it, and real ears ignore it (rig-verified). Prefer
  emitting frames that satisfy `L = total - 3`.

### CRC-8 [P,R]

Reflected CRC-8 with polynomial `0x31` (reflected form `0x8C`), initial value
`0x00`, final XOR `0x00`, both input bytes and result reflected. Parameters as
pinned down on page 3 of the thread. Verified against all 23 known-good frames
available from the thread and this repo's fixtures (16 one-bit colors, 3
oPossum palette frames, 3 Tasmota captures, RobG's 9-byte park decode), and
against the park corpus.

```python
def mwm_crc8(data):          # data: everything except the trailing checksum
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc               # 0x8C is the reflected form of poly 0x31
```

Of 2546 `9x` frames in the park recordings, 1889 (74%) satisfy both the
length rule and the CRC; failures correlate with truncated receptions at
capture boundaries. Individual families pass 93-99%. The CRC is definitively
CRC-8/Dallas, not the additive checksum used by `55 AA` messages.

Known-good examples [R]:

```
90 60 A6   both ears off           (crc8(90 60) = A6)
96 42 00 00 48 18 0C BE 1B        (park frame, crc8 = 1B)
```

### End-bit swallowing caveat

Receivers that sample run-widths (IRremoteESP8266, Tasmota) lose trailing
content bytes whose final data bits are 0: those bits scrunch into the stop
bit and inter-frame gap. A captured `99 ... D0 0E 83` (11 bytes) is really a
12-byte frame with one missing byte before the CRC. When decoding captures,
use the length nibble to detect short reads and recover the missing bytes by
brute-forcing candidates against the CRC. Upstream fix in progress in
IRremoteESP8266; our own timing decoder is unaffected.

### Capture artefact: trailing spaces hide inside the gap [R,H]

Encoders emit each byte's stop bit as a 1-tick space, but consecutive
equal levels MERGE: a frame's final space-valued levels -- the stop bit
itself and any trailing 1-data-bits before it -- fuse with the ~30 ms
inter-command gap into one long space run (e.g. `...+417 -30834` rather
than `...+417 -834 -30000`). Decoders must therefore treat a wide
trailing space as "gap + up to N missing space levels of the current
byte" and back-fill them (data bits are 1s by definition; the length rule
and CRC catch over-eager back-fill on truncated captures). Every decoder
variant in this repo (TypeScript, Perl, Python) has needed this fix.

## 3. Bundles and redundancy

### A-B-A' button pushes [T,P]

Wands, paintbrushes and ears themselves transmit each command three times:
`[phrase] [companion phrase] [phrase again]`. First and third are identical;
the middle one carries effect parameters (see section 6).

### Show FEC countdown [T,P]

Show transmitters repeat a phrase with a decreasing `F?` delay prefix so all
ears execute simultaneously despite packet loss:

```
94 FD 48 85 58 0F CC
94 FC 48 85 58 0F 01
...
94 F1 48 85 58 0F ED
94 20 48 85 58 0F 7E   <- immediate copy (0x20)
```

An ear executes when its received copy's delay expires, synchronizing the
audience even at zone fringes. `F?` semantics: delay = low-nibble x 100 ms
(`F1`=100 ms ... `FF`=1500 ms); `F0` is special (pulse effects); `20` means
immediate. One source describes `F?` oppositely [T]; the countdown dumps
above settle it.

### Countdown cascade primitive [T]

The countdown chain is the park's fundamental show primitive and is
encoded once as `mwm.build_cascade(tail, delays=None)` with the default
member set `CASCADE_DELAYS = FD, FC, FB, FA, F9, F8, F7, F6, F5, F4, F3,
F2, F1, 20` (`24` ``build_cascade`` frames may take `delay=` lists for the
same effect).

Real chains never run in the tidy step-down order: the park interleaves
near-simultaneous pairs, so the *set* of countdown bytes is what matters,
each repeated phrase differing only in the delay byte.  Verified chain
families (head + phrase, delay byte set to each member):

```
97 F? 24 0D 48 82 D0 0E FF      hard color transitions, group (EMLG0026;
                                 also 9C F? 24 48 82 D0 0E FF D0 46 A0 F3 48 85)
9D F? 24 48 82 D0 0E FE D0 45 A9 58 04 48 84    hard transition (MRDF)
9E F? 29 14 15 12 19 04 48 82 D0 0E FE D0 46 A0  hard transition (MRDF)
96 F? 24 67 58 tt 48 84          strobe flashes (timer tt per stand)
94 F? 48 85 58 tt               fade out (the park workhorse)
```

The wheel-side tool exposes it as `cue` which accepts a menu incant
(`cue fade cycle=0x16`), a raw delay-led `hex` phrase
(`cue hex F1 24 0D 48 82 D0 0E FF`; `cascade` is an accepted older alias),
or an `incant`-prefixed form; a `sequence <file>` runs a whole show-script
(`@ms` offsets, one beat per line, `#` comments).  A cue's `@ms` is its
master moment: the `20` go copy fires there, and the countdown members
air in the lead's 100..1500 ms BEFORE it -- each member `d` is scheduled
at `@ms - (d & 0x0F)*100`, its low-nibble delay, holding each consecutive
publish to the `--min-gap-ms` floor.  Ears re-fire on their last-heard
member at t = receipt + delay, so a listener that hears ANY member still
collapses onto the same `@ms` beat the full 14-member broadcast would.
Where a member's slot is already past (no room before the previous cue's
transmission), that member is dropped -- the worst case is a cue that
airs its `20` alone, on time.  An explicit `--cascade-ms` opts into
uniform pacing FROM `@ms` (a handful of sends per second), and
`--cascade-full` forces the full 14-member `FD..F1,20` chain regardless
of the lead byte.  A cue may also lead with the immediate `20` go copy
directly (`cue hex 20 <tail>`): `mwm-send` treats that as the go-variant
and generates the same canonical `FD..F1,20` countdown pre-rolled
backward from `@ms`, so the phrase never has to encode a cadence.  This
is the form `gen_show_script` emits.

A cue line's phrase is the delay-led tail; the countdown member set is
derived, never stored.  Hand-written scripts may still pin the captured
set explicitly as a `members` clause -- `cue hex F4 D0 42 08 members F3
F2 F1 20` -- and `mwm-send` rebuilds those members byte-for-byte (each a
`build_frame` of its own delay byte + the phrase tail), preserving repeats
and the lead's own value.  Generated scripts instead carry the compact
`20` go-variant above, so a show replays the capture's GO moments exactly
while the countdown cadence comes from one canonical source; the `#`
collapsed-cue comment records the captured countdown's start byte and go
tick for inspection.  `--dump` prints the resulting transmit stream
(`@ms HEX` per frame, end-of-show reset included unless `--no-end-reset`)
for offline diffing against the capture (see `tools/compare_capture.py`).

Runner pacing guards: consecutive
publishes never land closer than `--min-gap-ms` (default 30 ms), so beats
that share one `@ms` tick (a capture near-simultaneous cluster) go out
serially instead of several IR transmissions at once; and a `sequence`
show runs a **single pass** (its `@ms` timeline *is* the repetition --
`--repeat 2` would replay the whole multi-minute show).  `gen_show_script.py`
defaults to a FAITHFUL timeline: real wall-clock ticks (no trim, no gap
clamp) and every beat on its own de-flattened tick -- a countdown run
collapses to ONE `cue` carried at the run's GO tick, and the cue replay
pre-rolls the members before that same GO, so each master moment fires
exactly when the capture aired it while the real inter-GO pauses between
phrases survive verbatim (the countdown start and GO tick are kept in the
`#` comment).
Condensed demo output is opt-in: static beats (one cue repeating, or an
A/B pair like the red/green holiday cycle) longer than `--phase-ms` 10 s
can be folded to a ~10 s reminder at the run's OWN capture cadence
(`--phase-compress`, never denser than the park sent it), silent gaps
longer than `--gap-ms` 10 s can be clamped (`--gap-cap`), and
`--split-shows` partitions the capture into ONE `.msh` PER SHOW, each
opening with that pre-show idle cue as its lead-in -- so a demo walks
through the genuine commands quickly while `mwm-send` plays the `.msh`
VERBATIM and never collapses or fills gaps.  What *happened* is
logged to stdout (one newline line per send), so `> log` / `|` capture
exactly the transmitted commands; the live countdown to the next beat is
pinned to the LAST line on the controlling terminal (`/dev/tty`),
rewritten in place every ~0.2 s (it is erased before each history line
scrolls, so it never smears up into the send log), and never written to
stdout/stderr.

Show cues read as a **lookahead cue sheet**: a script is a sequence of
master events, and the interpreter classifies each beat from its frame
shape (`cue_class` in the library):

| class                  | shape                                      | behaviour                                                        |
|------------------------|--------------------------------------------|------------------------------------------------------------------|
| `PRE_BUFFER_EVENT`     | delay-led countdown member (`F1`..`FD`)       | lookahead cue: sets the crowd's absolute fire time at receipt + delay.  A whole run is ONE master event -- the script holds a single `cue` line, `@ms` anchors the GO, the members pre-roll before it (the generated countdown ends in the `20`), and the audience collapses on the master beat at `@ms`. |
| `IMMEDIATE_EVENT`      | `20` go copy; bare `48`/`24 48` effect invoke | snaps the ears' state now (hard override).                      |
| `GROUP_PICKER_CUE`     | `20 89 A0..26` range bounds; `24 0D` override | assigns a contiguous ear range to a state while others stay.    |
| `AMBIENT_LOOP_BEAT`    | pulse family (`58 F0 .. 48 04` timing clause) | sustained per-ear loop, not a one-shot beat.                    |
| `OTHER`                | static colors, palette shades, clock, 55AA   | state writers / sync.                                            |

The `sequence` runner reports each beat as one row with its class and the
master-beat marker (e.g. `@ 0 ms ... PRE_BUFFER_EVENT master-beat@1300ms`),
so consecutive beats can be paced by collapse to collapse.

`tools/gen_show_script.py` turns a captured `analysis/park/frames.tsv`
(or a raw park feed directly) into such a script, collapsing countdown
runs to one `cue hex 20 <tail>` go-variant whose countdown `mwm-send`
regenerates itself over the canonical `FD..F1,20` chain (the capture's
start byte and go tick are kept in the `#` comment), including 93-family
`command` and 97-family `colour-command` chains aired mid-show, real cues
admitted exactly like effect chains; the `20` go-variant anchors the
whole chain at the captured GO tick, whatever lead the capture used.
Frames the recorder packed onto ONE capture line are de-flattened at
+100 ms per frame in document order, so an aired chain keeps its real
countdown cadence instead of collapsing onto a single stamp with a
lexicographic tie-break.  The body timeline is built from
`effect-command` rows plus every member of a genuine countdown chain; the
remaining idle smear
(solid-color crossfades, lone non-chain commands) stays folded out of a
show -- but the source's EXIT TAIL (the `color-command`/`command` rows
after its last effect-command) is admitted too: a show must not strand
ears mid-effect, so it ends by settling them to the captured resting state
(`both ears off`, a solid color go copy, or the white countdown that
collapses to a single cascade).  `55aa` system broadcasts and beacons are
never emitted: they are the park sitting in demo mode, not state.
Default timeline is FAITHFUL to the capture: `--trim` on (show starts at
`@0`, every relative gap preserved; a show that OPENS with a collapsed
countdown cue anchors on the countdown's first captured member instead,
so that cue's GO lands at the recording's own GO offset and the opening
countdown airs rather than being cut) and every beat keeps its own
de-flattened tick.  The condensed demo modes are opt-in:
`--no-trim` keeps the real wall-clock ticks; `--split-shows` partitions
the capture into one `.msh` PER
SHOW, cutting at the long static idle runs -- the idle cue the park
meters out before a show's first change of state becomes that show's
LEAD-IN, replayed on the run's own capture cadence (a several-minute idle
becomes a ~10 s reminder), middle A/B loops and the trailing idle run
after the last real cue fold the same way, and a static with no show
behind it is dropped; `--phase-compress` folds statics in one timeline;
`--gap-cap` clamps silent gaps; `--skip-prelude`
drops the head run outright, `--start-tick`/`--end-tick` crop the
window.  The generated park shows in `samples/` commit to faithful
replay: the `samples/replay/*.msh` capture re-runs keep every beat at its
own real wall-clock tick (`--no-trim` -- the real clock
the overnight validation re-played);
`samples/park-cascade-demo.msh`
is hand-curated and starts with the fade at `@0` (its genuine ~27 s idle
gap after the fade is compressed to ~3 s for the demo).


### Clock-sync field [H,R]

Bytes `[0C t]` appear in wand phrases and ear beacons. Repeating a wand
button changes only `t` (a `96 19 10 24 0C <t> .. 2E 03 <crc>` phrase),
and idle beacons update it constantly.

Rig verification 2026-08-27 (`tools/capture_beacons.py` +
`tools/analyze_beacons.py`, ears in front of 600605/179E4E):
- `t` is a **linear 8-bit clock advancing ~10 ticks/second** (measured
  10.1-10.9/s across two independent effect cycles), wrapping modulo 256
  every ~25.6 s.
- Both receivers report the identical beacon (same tick, same `D0 0E ??`
  modifier byte, ~20 ms apart in arrival), so the tick is a property of the
  ear's autonomous stream, not of a receiver.
- The tick rate is stable across effect changes (effect index 0x15 -> 0x16
  did not perturb it).

Working model: `t` is a tick written into a shared clock register so
playback stays phase-aligned across ears. Because we can both read the
current tick from any beacon and write a specific tick (the `0C t` opcode),
a controller can synchronize its own transmissions to the ear stream's
phase (e.g. align a delayed `F?`/`58`/`D0`-paced effect to a chosen phase).

Open question: none of this confirms the ears actually SCHEDULE execution
off the register. Two things remain unverified: (a) that the `F?` delay
field gates when an ear executes a received copy (vs. only being echoed),
and (b) that a clock-write re-phases a running effect (or that re-issuing a
command retriggers vs. continues in phase). Until (a)/(b) are measured,
"synchronized to the running effect's current state" is only achievable
empirically (send, observe the beacon tick, adjust) -- see human-testing.md
B4/B5, which use a countdown/delayed copy and mid-effect re-sends to probe
exactly this.

Caveat on the "write" test: idle ears do NOT go silent after ~120 s; they
hand off to a demo / standalone / auto-sync-with-friends mode in which they
self-run a color-cycling effect and beacon independently. In that mode
there is no single clean clock trajectory to write against (several ears
beacon, and MQTT reports them clumped and out of real order), so a
clock-write has no unambiguous "did the phase reset?" answer. A controlled
clock-write test would need the ears parked in a known show/master clocked
state first, not left in autonomous demo mode.

Do not infer "bursts" from arrival timing. Tasmota's MQTT reporting is
software-paced and its timestamps/ordering are not hardware-accurate, so a
cluster of frames landing in one second tells you nothing about whether one
or several ears emitted, or whether one ear emitted several frames. The
reliable signal is the tick itself: with one rig of ears, beacons arrive
(with whatever real spacing) and the tick advances ~10.5/s through the
payload value, independent of the current effect. Evaluate the clock by the
decoded tick sequence, never by MQTT arrival deltas.

## 4. Instruction set

A phrase body is a small program: opcodes with arguments executed left to
right. Tables list opcode, arguments, and meaning.

### Colors, simple palette (1 byte) [R]

| Code      | Meaning                                                     |
|-----------|-------------------------------------------------------------|
| `60`      | both ears off                                               |
| `61`-`67` | both ears solid: blue green cyan red magenta yellow white   |
| `68`      | single-ear off                                              |
| `69`-`6F` | single-ear colors - physical **left** ear only (override `0x08` of `61`-`67`). Side assignment now RESOLVED: the `0x08`/`0x80` slot is the wearer's left ear (hardware tag-in-back test + viperfan91 chart); settles the old Jon-vs-djred2000 dispute in djred2000's favour. |

One-bit colors: payload byte `0x60 + i` selects one of 8 primary colors
(i: 0=off, 1=blue, 2=green, 3=cyan, 4=red, 5=magenta, 6=yellow, 7=white).
OR `0x08` into the byte to affect **only the left ear**:

| frame      | effect             |
|------------|---------------------|
| `90 61 F8` | both ears blue      |
| `90 69 3A` | left ear blue only  |

### Colors, mixed palette (2 bytes `0E XX`) [R]

| Code            | Meaning                                            |
|-----------------|----------------------------------------------------|
| `0E 00`         | palette index 0x00 — pale cyan-white (NOT white; see table below) |
| `0E 01`-`0E 1B` | palette colors (index 0x1C = white, pure)          |
| `0E 1C`         | white (pure)                                       |
| `0E 1D`         | off                                                |
| `0E 80`-`0E 9C` | high-bit mirror of the palette (per-ear register)  |
| `0E 9D`         | off (per-ear register)                             |

Palette colors use payload bytes `0x0E <index>` to select one of 30 predefined
colors (full table below); OR `0x80` into the index byte to affect only the
left ear:

| frame         | effect                    |
|---------------|---------------------------|
| `91 0E 14 A3` | both ears orange-red      |
| `91 0E 94 2F` | left ear orange-red only  |

Verified pairs [R] (djred2000 fade recipes): `0E 15` red / `0E 95`,
`0E 19` green / `0E 99`, `0E 04` blue / `0E 84`. The plain (`0E pp`) form
drives the physical right ear, the `0x80` (`0E pp|80`) form the physical
left; djred2000's LEFT-ear fades use the high-bit form after clearing with
`0E 1D`; RIGHT-ear fades use the plain form.

### Left vs right ears [R]

There is **no right-ear-only command**: the `0x08`/`0x80` bit above a
both-ear color selects the **left** ear only. To set different colors per
ear, set both ears to the right color first, then override the left ear:

```
90 65 99        both ears magenta
90 6E B9        left ear yellow      => right magenta, left yellow
```

A fused form sets each ear independently in one frame, demonstrated with
one-bit colors: `91 <c_right> <c_left> <crc>`, e.g. `91 61 6A 06` =
right blue, left green (`0x6A` = green | 0x08). The first byte (`60`-`67`)
brings both ears to the right color; the second byte (`68`-`6F`) overrides
only the left ear. Multi-byte phrases run opcodes sequentially against both
ears; a both-ear opcode followed by a left-only opcode achieves per-side
control in one burst. Equal pairs need only the single both-ears form
(including canonical `90 60` off).

A **fused palette** equivalent is structurally possible but UNVERIFIED on
hardware: `93 0E <right> 0E <left|80> <crc>` seeds the right ear with a
palette shade, then overrides the left ear per-ear-register in ONE burst
(and `92 0E <right> 6L <crc>` mixes a palette-right with a simple
left-only). The only prior attempt (`94 0E 00 0E 94 11`) used a wrong
length nibble and was rejected by the length rule, so its "real ears ignore
it" result is inconclusive. See docs/human-testing.md A3a before relying on
it.

**Resolution of the old `[T]` side-assignment dispute** (Jon's decode
implied right, djred2000 listed left): the `0x08`/`0x80` override slot is
the wearer's physical **LEFT** ear. Confirmed directly on rig with the tag
in back (the wearer's left ear answered the frame's old "right" command)
and corroborated by viperfan91's friend-code chart (`samples/
viperfan91-paintbrush.txt`), whose flashing ear -- the same override slot --
is labeled "left". Earlier docs/code labeled the slot "right" (a viewer-
facing frame of reference); everything now uses the wearer's frame.

### Palette index table (measured by oPossum)

PWM duty-cycle approximations, relative rather than absolute; preview on real
ears for perceptual accuracy. RGB values as posted.

> **Naming note.** These are the authoritative oPossum measurements and name
> (mirrored verbatim by `python/.../_mwm/palette.py`, which must match the
> TSV). In particular index `0x00` is **pale cyan-white** (`ACFEFE`), and the
> pure white is index `0x1C` (`FEFEFE`). The `samples/mwm-gwts-colors.tsv`
> row descriptions call `0x00` "white" loosely; treat the RGB/hex and the
> table below as correct, not the loose row names. The one-bit color
> `0x67` is the true white.

| idx | RGB    | label           | idx | RGB    | label             |
|-----|--------|-----------------|-----|--------|-------------------|
| 00  | ACFEFE | pale cyan-white | 10  | FF560A | orange            |
| 01  | 1F90FE | sky blue        | 11  | FF7701 | bright orange     |
| 02  | 1F4DFE | azure blue      | 12  | FFFF00 | pure yellow       |
| 03  | 1F00FE | blue-violet     | 13  | FF4400 | red-orange        |
| 04  | 0000FE | pure blue       | 14  | FF1100 | orange-red        |
| 05  | FFCBFE | pale pink       | 15  | FF0000 | pure red          |
| 06  | AC4DFE | violet          | 16  | 00FEFF | cyan              |
| 07  | 6126FF | indigo          | 17  | 00FE6B | spring green      |
| 08  | 7701AB | purple          | 18  | 00FE2C | green-cyan        |
| 09  | FFACFE | orchid pink     | 19  | 00FE00 | pure green        |
| 0A  | FF2CFF | magenta         | 1A  | 01FF00 | lime green        |
| 0B  | FE0DFF | fuchsia         | 1B  | DBFFCA | pale green-white  |
| 0C  | FF00CA | rose magenta    | 1C  | FEFEFE | white             |
| 0D  | FF0061 | rose pink       | 1D  | 000000 | black/off         |
| 0E  | FF0011 | scarlet         |     |        |                   |

### Flow control [T]

| Opcode | Meaning                                                         |
|--------|-----------------------------------------------------------------|
| `24`   | reset/override. Alone: both ears black immediately. Preceding other opcodes: lets them take effect while a built-in effect runs. Required to switch away from some built-in functions. |
| `20`   | start immediately (~0 ms); also the final FEC copy marker       |
| `25`   | stop motion of running effects                                  |
| `26`   | similar to `24`; closes group ranges (`A0 rr 26`)               |

### Effects `48 XX` [T,P]

Stored effect programs invoked by index. Frequency column = occurrences in
khyizang's park subset.

| Code    | Effect                                                   | freq |
|---------|----------------------------------------------------------|------|
| `48 00` | random effect (can leave ears dead ~2 min)               |  -   |
| `48 01` | quick smooth fade out                                    |  45  |
| `48 02` | fade through current color to black                      | exp  |
| `48 03` | slow even pulse; pairs with `58 F0`                      | 217  |
| `48 04` | pulse; `58 F0` + optional `D0 42 tt` cycle-rate modifier | 458  |
| `48 08` | quick rotation through four colors                       | 108  |
| `48 0D` | color sequence                                           | 126  |
| `48 0E` | (seen)                                                   |  55  |
| `48 0F` | flashing sequence                                        | 208  |
| `48 10` | quick flashing on one ear                                |   7  |
| `48 11` | color rotation                                           | 372  |
| `48 13` | color sequence                                           | 142  |
| `48 15`,`48 17` | (seen in live demo cycling) [H]                  |  -   |
| `48 1A` | power-on blinks + fade                                   |   2  |
| `48 1C` | (seen)                                                   |  77  |
| `48 1D` | (seen)                                                   |  16  |
| `48 1E` | (seen)                                                   |  63  |
| `48 1F` | off                                                      |  -   |
| `48 80` | power-on display sequence, enter demo mode               | exp  |
| `48 81` | power-off display sequence, pretend offline ~2 min       | exp  |
| `48 82` | hard (non-crossfaded) color transitions                  | 369  |
| `48 83` | crossfading transitions                                  | 585  |
| `48 84` | strobe flashes into running program                      | 326  |
| `48 85` | fade out                                                 | 2014 |
| `48 86` | fade up                                                  | 593  |
| `48 87`-`8F` | various color sequences                             | misc |
| `48 EC` | (seen)                                                   |  38  |
| `48 EE` | (seen)                                                   |  22  |

("exp" = experimentally confirmed valid, no park hits.)

Usage notes [T]:

- `48 85` needs an active color. An `F1`-`FF` byte before it delays the
  fade start; `58 tt` after it stretches the duration. Afterwards a phrase
  containing `24` may be required before new colors respond.
- `48 86` works any time, symmetrically; no reset needed afterwards.
- `48 03`/`48 04` require `58 F0`; `D0 42 tt` then scales the cycle
  (~200 ms per count).  Rig-verified 2026-09-06 as a continuous cadence
  knob, not a fixed byte: the corpus `0x06` re-color left the pulse
  running FASTER than a fresh pulse ("y but actually pulsing faster"),
  the fuzz `0x20` much slower ("y much slower") — so `D0 42 tt` maps to
  the post-swap pulse rate.

### Timers and modifiers [T]

| Opcode       | Meaning                                                       |
|--------------|---------------------------------------------------------------|
| `58 tt`      | cycle duration for a `48` effect, ~100 ms per count (`58 32` ~= 4.9 s measured); strobe flashes burn TWO counts per visible blink (on+off): `58 01` measured 48 flashes/10 s, `58 02` = 25/10 s (period ~200 ms x tt); `58 EE`/`58 F0` special |
| `59 aa bb`   | like `58` at 200 ms granularity; first arg alters the effect  |
| `5A a b c`   | fractional period: with `c=55`, period = a x 400 ms / b       |
| `5B ...`     | four-arg timer seen inside group-select phrases               |
| `D0 xx yy`   | two-arg modifier; `D0 42 tt` = 200 ms/count cycle scaler      |
| `D1 aa bb cc`| three-arg modifier, usually followed by a `D0` clause         |
| `D2 .. .. ..`| four-arg variant                                              |

The `5x` family times individual effects; `Dx` clauses modify effect
parameters and pacing. jstorms' show sequences use `D1 ?? ?? ?? D0 ?? ?? ??
` constructs.

### Clock write [H]

| Opcode | Meaning                                        |
|--------|------------------------------------------------|
| `0C t` | write tick `t` to the sync clock register      |

See section 3 (Clock-sync field).

### Group addressing [T]

Each ear picks a random group id `00`-`7F` at power-up. Show phrases assign
colors per group range:

```
98 20 D2 35 00 F2 01 02 20 66 1D   pick number (PRNG seed?)
97 20 89    A0 19 26    6E F2 66 F8    yellow, groups 00-18 (uninitialised)
98 20 8C 19 A0 32 26    69 F2 61 F5    blue,   groups 19-31
98 20 8C 32 A0 4B 26    6B F2 63 C1    cyan,   groups 32-4B
98 20 8C 4B A0 64 26    6C F2 64 B0    red,    groups 4B-63
99 20 81    A0 64 26    0E 8D F2 0E 0D DF   pink (palette!), groups 64-7F
```

`89`/`8C`/`81` act as group pickers, `A0 rr` bounds the range, `26` closes
it. Simple and palette colors mix freely. The rarer `8C ?? 83 ??` pair also
partitions audiences (~3.5% of show phrases).

## 5. Phrase templates observed

> **Encoding principle — commands are indices, not self-describing.**
> Across every device class, the air carries *table indices*, never embedded
> values: palette colors are selected by `0E <index>` into the ear's internal
> 30-color ROM (section 4, "Palette index table"), the one-bit `60`-`6F`
> forms pick from a fixed 8-color set, effects are invoked by `48 <index>`
> (section 4, "Effects `48 XX`") or by the compact `96 19 gg kk vv tt ww`
> variant table below, and the demo beacon reports the *index of the
> internally-running demo program* plus a clock tick.  A frame is therefore
> always a COMPLETE, executable command (a real ear would act on it), but it
> is NOT self-describing: the RGB/effect semantics live in each ear's
> firmware, so a decoder can only say "palette index 0x14 (orange-red)" or
> "effect 04 (pulse)" because observation established that mapping.  The
> measured tables in this file, `samples/mwm-gwts-colors.tsv` and
> `EFFECT_LABELS` are precisely that dereference layer.  Consequence for
> learning: no capture can ever be understood by decoding alone -- pairing
> with observed behaviour (the annotate tool's human observer, or a rig such
> as `mwm-send.py` + real ears) is what turns an index into a meaning.

### Ear idle/demo beacon [P,H]

Live headband stream (`samples/headband-20260822.log`, 2026-08-22/23,
unsolicited transmissions every 7-12 s, matching Jon's 8-12 s
anti-collision estimate):

```
99 42 00 00 48 ss 0C t D0 0E crc        (12 bytes; captures often miss 1)
```

- `42 00 00` constant across every beacon and across devices/days [H:
  device state/id block].
- `48 ss` = the demo effect currently running. Observed cycling:
  16 -> 16 -> 18 -> 88 -> 88 -> 14 -> 00 -> 88 x4 -> 15 -> 88 x3 ->
  18 x4 -> 17 x2 -> 16 -> 14 x3 -> 16 x3. Mostly `48 88` (a color-sequence
  index), occasionally `48 00` (random). The hat re-picks its demo effect
  every few minutes. The library's `DEMO_BEACONS` catalog (and the
  `demo_beacon_label()` fallback in `python/mwm/decode.py`) is the
  dereference layer for these indices, with the observed live-cycling set
  and counts from `samples/headband-20260822.log`.
- `0C t` clock tick, changes every transmission.
- `D0 0E ??` modifier clause whose last argument is usually swallowed by
  the receiver's end-bit bug; recovered via CRC brute-force (values seen:
  A1, 92, 2E, 1D, 37, 01, 17, D5, 0E...).
- Park variant without the `D0` clause: `96 42 00 00 48 17 0C 40 2F` [P].

Park recorder also caught `9A 9A 90 26 48 83 D0 0E 80 D0 42 1E 59` -
same family with richer modifiers [P].

### Wand/paintbrush push [T,P]

Command phrase templates (varying byte underlined conceptually):
`96 19 gg kk vv tt ww crc` where one position carries the clock/effect
variant per push. Documented paint-the-night effects [T] (Calvin, 2014):

| Visual effect                              | Command phrase              |
|--------------------------------------------|-----------------------------|
| both yellow, dim/bright alternating        | `96 19 0B 36 06 D4 30 01 7F` |
| both green, dim/bright                     | `96 19 0B 12 10 97 BE 01 8F` |
| fade up white, hold ~2 s, down             | `96 19 0D 3F 20 9E 30 11 81` |
| blue/yellow alternating ears               | `96 19 0E 31 0A 1C 30 04 9E` |
| blue/white flashing sway                   | `96 19 0F 39 0C BE 30 05 79` |
| both green pulsating                       | `96 19 10 12 0E 68 30 03 9C` |
| light blue L/R/off cycle                   | `96 19 11 1B 10 D0 30 01 50` |
| green L/R/off cycle                        | `96 19 11 12 04 7B 42 01 85` |
| pulsating red (3 pushes vary clock byte)   | `96 19 10 24 0C -- 2E 03`   |

> **Note on the trailing byte.** These are as-recorded captures; their final
> byte is often NOT a valid CRC over the preceding bytes (see CRC + end-bit
> swallowing, section 2). The payload fields are meaningful; treat the last
> byte as a possibly-mangled checksum from reception, not ground truth.

### Static color commands (rig-verified set) [R]

Our verified library (full table with checksums in
`samples/mwm-gwts-colors.tsv`):

- `96 19 07 0F 16 pp 18 04 ..` - palette color `pp` both/left ears
- `96 19 0B 09 08 pp 18 01 ..` - palette group 2
- `96 19 0D 2D 16 pp 30 11 ..` - palette group 3
- `91 61 6A ..`                - fused right-blue/left-green
- `90 60 A6`                   - both off (keep-alive)

All 30 palette shades, all simple colors both ears, left-ear modifiers and
several multi-part sequences were confirmed visually on genuine ears; every
ear reaction matched the intended frame exactly.

### Hardware verification (2026-08-22, MQTT IR test rig)

Every simple/palette color-family command was transmitted to a real ear-hat
set and the visible response checked:

- All 16 one-bit color frames (8 colors x both/left ears): correct.
- All 30 palette entries on both ears: correct sequence and shades.
- Left-ear palette modifier (`| 0x80` into the index byte): verified with
  indices 04/12/15/16/19/1D while the right ear held another color.
- Two-step per-ear override (both magenta then left yellow; both white
  then left orange-red): correct.
- Well-formed fused frame `91 61 6A 06`: works (right blue / left green).
- Deviant fused frame `94 0E 00 0E 94 11`: ignored by real ears.
- RobG's 9-byte park show frame: no visible reaction on ears (park/show
  infrastructure, not an ear command).

Practical note: the ears' receiver occasionally drops a single frame even
at close range. Real wands transmit commands repeatedly per button press;
scripts should do the same (2-3 repeats make delivery reliable).

### Show FEC run [T,P]

Same phrase repeated with countdown prefixes FD..F1 then 20 (section 3).

## 6. Companion frames [P,H]

The middle message of an A-B-A' bundle (see section 3). Templates seen
(constant positions marked):

| Template                                   | Context                    |
|--------------------------------------------|----------------------------|
| `99 24 58 vv 48 0D D0 3E 32 ee ee crc`     | dim/bright color effects   |
| `9B 26 0C vv 67 69 58 EE 48 04 D0 3D 0B ..`| wand group 1 commands      |
| `9B 24 65 6D D0 32 DF 5A 0A 08 0B 48 91 ..`| wand group 3 commands      |
| `98 24 0D 63 6B 48 10 D0 42 08 ..`         | L/R/off cycle effects      |

They read like parameter blocks for the effect named by their embedded
`48 XX`: durations (`58`, `D0`), palette entries (`0E`), etc. Treat them as
required halves of a complete command until proven optional.

## 7. System messages: `55 AA` [T]

Parallel/ancestor protocol (Magical Moments pins, Pal Mickey). Not MWM
phrases; heard constantly in parks.

Games: `55 AA 05 06 <game> <p1> <p2> <cs>` with additive checksum (sum of
bytes after `AA`, mod 256). Games: `00` demo cycle, `01` blue solo,
`02` red memorisation, `03` yellow memorisation, `04` laser tag.

Ride shutdown (attractions kill frame; see verified note below):

```
55 AA 08 C4 13 FF 01 ED AF FF 7A
```

Byte-by-byte (additive checksum, not CRC-8 — 55AA frames use their own rule):

| # | byte | hex   | meaning                                        |
|---|------|-------|------------------------------------------------|
| 1 | `55` | 0x55  | system-message sync byte 1                     |
| 2 | `AA` | 0xAA  | system-message sync byte 2                     |
| 3 | `08` | 0x08  | length (bytes after this header, incl. cs)     |
| 4 | `C4` | 0xC4  | command/opcode (ride shutdown)                 |
| 5 | `13` | 0x13  | parameter                                      |
| 6 | `FF` | 0xFF  | parameter                                      |
| 7 | `01` | 0x01  | parameter                                      |
| 8 | `ED` | 0xED  | parameter                                      |
| 9 | `AF` | 0xAF  | parameter                                      |
|10 | `FF` | 0xFF  | parameter                                      |
|11 | `7A` | 0x7A  | additive checksum: sum(bytes 3..10) mod 256    |

> **Checksum verification [R].** The additive checksum spans bytes **3..10**
> (i.e. the length byte and every byte after it), matching the games rule
> "sum of bytes after `AA`". sum(08 C4 13 FF 01 ED AF FF) = 1146, 1146 mod
> 256 = 0x7A. An earlier draft caption reading "sum(bytes 4..10)" (→ 0x72)
> was WRONG and rejected on hardware: `55AA..AFFF72` was ignored, while
> `55AA..AFFF7A` is accepted.
>
> **Wording equivalence.** Earlier forum notes (Jon Fether, post 259733)
> wrote this as "sum of ALL bytes including `55 AA`, then `+1`". The two are
> the same result: `0x55 + 0xAA = 0xFF`, so `(sum_after + 0xFF) + 1 ≡
> sum_after (mod 256)` — the `+1` exactly cancels the `+0xFF`. Computing
> over bytes after `AA` (no `+1`) is the form used here and in the library.

> **Verified behavior on the test rig [R, 2026-09-03].** Despite the
> attraction lore (~6 min dead ears, battery-pull abort), on hardware this
> frame produced only the standard ~2 min beacon silence (first beacon back
> at ~132 s) and did NOT turn the displayed color off. `48 80` wakes the
> ears early out of it (first beacon ~8 s). Treat it as a beacon/politeness
> silencer like `48 81`, not a true long shutdown.

Timecode-like `55 AA` frames broadcast continuously; consumer unknown
(captioning?). The park corpus contains 722 such frames.

Other park noise: `8x` frames (`83 7F 7F 7F 7F 00`, 171 captured) from
unknown non-MWM devices.

## 8. Device behavior notes [T,P,H]

- After obeying a wand phrase the ears fall silent, play the sequence
  ~2 min, then resume demo chatter [T].
- Demo mode cycles through stored effects every few minutes [H, live
  stream].
- Beacons repeat at irregular 7-12 s intervals (anti-collision) [T,H].
- Ears cannot be powered on by IR; off-state is a deep MSP430 sleep [T].
- `48 81` pretends offline ~2 min; `55 AA` shutdown above is the ride kill.
- Beacon/politeness silence [R, 2026-09-03]: after any accepted command the
  ears stop beaconing ~2 min (measured 128-134 s), then resume. This is
  NOT an on/off thing -- beaconing stops while the display keeps running.
  Verified durations: `9060A6` off ~128-134 s; `91481FB2` off effect ~128 s;
  `914881BC` power-off ~131 s; `55AA08C413FF01EDAFFF7A` ~132 s. Only
  `9060A6` also turns the displayed color dark; the others silence beacons
  with the display left on. `90 62`-style plain color commands do NOT
  suppress beaconing (beacons keep flowing). `48 80` (`914880E2`) is the
  reliable wake -- it restores both beaconing (first beacon ~8-16 s) and the
  display from any of the above; `24` reset, `48 1A` and solid color
  commands do not wake beaconing early. `48 80` also flashes the ears
  left/right a couple of times on receipt.
- The ~2 min silence for ANY commanded state is extendable [R, 2026-09-03]:
  re-sending the same command (before the window ends is safest) restarts a
  fresh full ~2 min silence.  Measured: `9060A6` silence ended at 128 s, a
  re-send then held it quiet for another 130 s (resumed at 259 s), so a
  commanded state (including `9060A6` off) can be held open-ended by
  repeating the frame periodically.  Because a single IR send can be
  dropped by the receiver debounce, send each as an A-B-A' bundle (repeat=2,
  ~0.35 s apart) so the command reliably lands before relying on it.
- Real ears ignore frames violating the length rule (tested with deviant
  `94 0E 00 0E 94 11` - no reaction) [R]. Length validation is strict.

## 9. Corpus statistics [P]

Six MWM sources fully decoded (`tools/build_corpus.py` + `tools/analyze_shape.py`):
**9332 validated frames, 5898 distinct**.

| Source                          | Frames | Notes                                       |
|---------------------------------|-------:|---------------------------------------------|
| `MRDF0007.TXT` (park 2014)      |   3515 | show commands + sync pings                  |
| `EMLG000E_filtered.txt` (park)  |   3397 | show commands + sync pings                  |
| `MRDF0008.TXT` (park 2014)      |    715 | short second session                        |
| `EMLG0026_filtered.txt` (park)  |   1467 | show commands                               |
| `headband-20260822.log` (rig)   |    217 | hand-held captures                          |
| `mode2-capture.log` (rig)       |     21 | raw timings fed through `decode_timings`    |

(Tasmota rig logs whose `IRremoteESP8266` decode user-dictionary labels a
non-MWM protocol -- JVC, NEC, Samsung, KELON, MIDEA -- stay excluded as
mixed-protocol noise.)

Top command *shapes* (head + masked opcode sequence, occurrence-weighted):

| Shape                                             | Occ. | Example                      |
|---------------------------------------------------|-----:|------------------------------|
| `94 F? 48 85 58 tt`  fade-out countdown           |  372 | `94F248855805DD`             |
| `91 F? 24`  reset ping                            |  201 | `91F1249C`                   |
| `92 F? 48 85`  fade-out, no timer                 |  187 | `92F148859E`                 |
| `9B 96 26 0E pp colB tt 48 04 D0 45 83`  pulse    |   53 | `9B96260E816158F04804D0458323` |
| `9E 94 26 colB 0E pp 58 F0 48 04 D0 42 tt D0 45 83` pulse (both-first) | 11 | `9E9426640E8D58F04804D04216D04583C6` |
| `96 F? 24 67 58 tt 48 84`  strobe into program    |   96 | `96F12467580248848D`         |
| `9E 91 90 25 29 02 15 19 48 82 D0 xx xx D0 xx xx` hard transitions loop | 104 | `9E919025290215194882D00EFFD005FF3F` |
| `9B F? 24 48 11 D0 3D tt cc lc FA 48 85`  rotation + fade | 40 | `9BF1244811D03D01626AFA48851C` |
| `97 F? 24 67 F? 48 85 58 tt`  colored fade        |   73 | `97F12467F348855803D9`       |
| `98 F? 24 0E pp F? 48 85 58 tt`  paletted fade    |   72 | `98F1240E13F24885580F32`     |

Effect usage (occurrence-weighted across all six sources): fade-out `48 85`
1375, pulse `48 04` 464, hard transitions `48 82` 385, fade-up `48 86` 340,
crossfade `48 83` 320, strobe `48 84` 293, color rotation `48 11` 136, slow
even pulse `48 03` 112.

Real pulse incantations observed (the pattern is **left-ear palette shade +
right/both simple color, fused into one phrase** -- the dominant form is a
simple both + per-ear palette register in the *same* phrase, not a separate
fused `91 6R 6L` override):

```
9B 96 26 0E 81 61 58 F0 48 04 D0 45 83 23   53x  left sky-blue, both blue
9E 94 26 64 0E 8D 58 F0 48 04 D0 42 16 D0 45 83 C6   11x  both red, left rose-pink
9E 95 26 61 0E 97 58 F0 48 04 D0 42 16 D0 45 83 E7   61 occ / 26 distinct  both blue, left palette 0x17
```

## 10. Open questions

1. Single-ear side assignment for `69`-`6F` and for masked/unmasked `0E XX`.
2. Exact semantics of `42 00 00` in beacons (device id? state vector?).
3. Clock-tick rate and epoch for `[0C t]`; whether ears schedule on it.
4. Whether companion frames are mandatory or optimisable.
5. Full `48 XX` catalog (indices `02 12 14 16 18 8A 8C 8D 8E 8F 90 91`...).
6. `D1`/`D2` argument semantics beyond pacing observations.
7. Group-picker grammar (`89`/`8C`/`81` + `A0` ranges + PRNG seed phrase).
8. Why `48 88` dominates demo mode; what sequence it names.
9. Recovering swallowed end bytes reliably (CRC search space when >1 lost).
10. `F0` exact behavior outside `58 F0`.

## 11. Encoder/decoder roadmap

1. Decoder: timings -> ticks -> bits -> frames (exists in web);
   add length-aware tail recovery via CRC brute force; split bundles on
   >=10 ms gaps; classify A/B/A' triples.
2. Disassembler: map phrase bodies onto the instruction tables above;
   annotate unknown opcodes verbatim.
3. Encoder: compose phrases from primitives (color, effect, timer, delay,
   group), auto-length header, CRC append, optional FEC countdown run and
   companion-frame generation; emit Pronto/raw/Tasmota IRSend.
4. Conformance: replay every frame in `mwm-gwts-colors.tsv` and the park
   corpora through decoder->encoder->decoder round trips; hardware-verify a
   matrix of composed effects on the MQTT rig.

## 12. Byte-by-byte worked examples [R]

The known frame set from `samples/mwm-gwts-colors.tsv`, with each byte's
meaning laid out for human review. Every frame below carries a valid CRC
(verified); the trailing byte is always the checksum. The "byte meaning"
column reads the payload opcodes against section 4's instruction tables.
Start with 12.1, which is the smallest complete frame.

### 12.1 Recipe: generating a new command

1. Pick color family and index/byte (tables in section 4).
2. Apply the ear modifier if needed: `| 0x08` for one-bit bytes, `| 0x80`
   for palette indices.
3. Prepend the length header `0x90 | (total_bytes - 3)`.
4. Append `mwm_crc8(header ++ payload)` as the final byte.

Example — left ear only, palette pure yellow:

```
payload  = 0E 92            (0x12 | 0x80)
header   = 91               (4 bytes total -> nibble 1)
checksum = mwm_crc8(91 0E 92) = F2
frame    = 91 0E 92 F2
```

### 12.2 Simple both-ears color: `90 61 F8` (both blue)

| # | byte | hex | meaning                                          |
|---|------|-----|--------------------------------------------------|
| 1 | `90` | 0x90 | length header: `9` + length nibble `0` => 3-byte frame (`total = 0 + 3`) |
| 2 | `61` | 0x61 | both-ears color: `0x60 + 1` = blue (no `0x08` left-ear bit) |
| 3 | `F8` | 0xF8 | CRC-8/Dallas over `90 61` |

### 12.3 Left-ear-only color: `90 69 3A` (left blue only)

| # | byte | hex | meaning                                          |
|---|------|-----|--------------------------------------------------|
| 1 | `90` | 0x90 | length header: 3-byte frame (`total = 0 + 3`)    |
| 2 | `69` | 0x69 | blue `0x61` OR `0x08` left-ear bit => left ear blue only |
| 3 | `3A` | 0x3A | CRC-8 over `90 69`                               |

### 12.4 Simple both-ears off: `90 60 A6`

| # | byte | hex | meaning                                          |
|---|------|-----|--------------------------------------------------|
| 1 | `90` | 0x90 | length header: 3-byte frame                      |
| 2 | `60` | 0x60 | both ears off (`0x60 + 0`)                       |
| 3 | `A6` | 0xA6 | CRC-8 over `90 60`                               |

The canonical off frame doubles as a keep-alive in the integration.

### 12.5 Both-ears palette color: `91 0E 14 A3` (both orange-red)

| # | byte | hex | meaning                                          |
|---|------|-----|--------------------------------------------------|
| 1 | `91` | 0x91 | length header: length nibble `1` => 4-byte frame (`total = 1 + 3`) |
| 2 | `0E` | 0x0E | palette opcode (2-byte `0E <index>` form)        |
| 3 | `14` | 0x14 | palette index 0x14 = orange-red (both ears; no `0x80`) |
| 4 | `A3` | 0xA3 | CRC-8 over `91 0E 14`                            |

### 12.6 Left-ear palette color: `91 0E 94 2F` (left orange-red only)

| # | byte | hex | meaning                                          |
|---|------|-----|--------------------------------------------------|
| 1 | `91` | 0x91 | length header: 4-byte frame                      |
| 2 | `0E` | 0x0E | palette opcode                                   |
| 3 | `94` | 0x94 | palette index 0x14 OR `0x80` left-ear bit => left ear orange-red only |
| 4 | `2F` | 0x2F | CRC-8 over `91 0E 94`                            |

### 12.7 Fused left/right pair: `91 61 6A 06` (right blue / left green)

| # | byte | hex | meaning                                          |
|---|------|-----|--------------------------------------------------|
| 1 | `91` | 0x91 | length header: 4-byte frame                      |
| 2 | `61` | 0x61 | both ears blue (`0x60+1`) — sets the right color |
| 3 | `6A` | 0x6A | left-ear green (`0x62` green OR `0x08`) — overrides left only |
| 4 | `06` | 0x06 | CRC-8 over `91 61 6A`                            |

### 12.8 Multi-burst per-ear override: `90 65 99` then `90 6E B9` (right magenta / left yellow)

Two frames, because there is no right-ear-only command (section 4, "Left vs
right ears"):

| frame       | byte | hex | meaning                                          |
|-------------|------|-----|--------------------------------------------------|
| `90 65 99`  | 1    | `90` | both-ears form                                  |
|             | 2    | `65` | both ears magenta (`0x60 + 5`)                   |
|             | 3    | `99` | CRC-8 over `90 65`                               |
| `90 6E B9`  | 1    | `90` | both-ears form                                  |
|             | 2    | `6E` | left-ear yellow (`0x66` yellow OR `0x08`)        |
|             | 3    | `B9` | CRC-8 over `90 6E`                               |

### 12.9 Paletted both+restore override: `91 0E 00 5F` then `91 0E 94 2F` (right pale-cyan-white / left orange-red)

Palette shades are inherently both-ears, so a left palette pick composes a
both-ears frame then a left-only restore. (The TSV row `right-white-left-
orange` calls `0E 00` "white"; per the palette table it is pale cyan-white —
pure white would be `0E 1C`.)

| frame        | byte | hex | meaning                                            |
|--------------|------|-----|----------------------------------------------------|
| `91 0E 00 5F`| 1    | `91` | 4-byte frame                                      |
|              | 2    | `0E` | palette opcode                                    |
|              | 3    | `00` | palette index 0x00 = pale cyan-white (both ears)   |
|              | 4    | `5F` | CRC-8 over `91 0E 00`                              |
| `91 0E 94 2F`| 1    | `91` | 4-byte frame                                      |
|              | 2    | `0E` | palette opcode                                    |
|              | 3    | `94` | index 0x14 OR `0x80` => left ear orange-red only  |
|              | 4    | `2F` | CRC-8 over `91 0E 94`                              |

### 12.10 Ear idle/demo beacon: `99 42 00 00 48 16 0C 08 D0 0E 92 8F`

The canonical 12-byte beacon (`99` = length nibble 9 => `total = 9 + 3 = 12`).
Working values were chosen for the per-transmission fields; the constant
blocks are as described in section 5.

| # | byte  | hex | meaning                                                |
|---|-------|-----|--------------------------------------------------------|
| 1 | `99`  | 0x99 | length header: `9` + `9` => 12-byte frame              |
| 2 | `42`  | 0x42 | device-state/id block (constant across beacons) [H]    |
| 3 | `00`  | 0x00 | device-state/id block (constant) [H]                   |
| 4 | `00`  | 0x00 | device-state/id block (constant) [H]                   |
| 5 | `48`  | 0x48 | effect opcode                                          |
| 6 | `16`  | 0x16 | current demo effect index (numeric; cycles over time)          |
| 7 | `0C`  | 0x0C | clock-write opcode                                     |
| 8 | `08`  | 0x08 | clock tick `t` (advances ~10/s, mod 256)               |
| 9 | `D0`  | 0xD0 | two-arg modifier opcode                                |
|10 | `0E`  | 0x0E | modifier parameter                                     |
|11 | `92`  | 0x92 | modifier parameter (often swallowed by end-bit bug)    |
|12 | `8F`  | 0x8F | CRC-8 over bytes 1..11                                 |

### 12.11 RobG park show frame: `96 42 00 00 48 18 0C BE 1B`

| # | byte | hex | meaning                                          |
|---|------|-----|--------------------------------------------------|
| 1 | `96` | 0x96 | length header: `9` + `6` => 9-byte frame (`total = 6 + 3`) |
| 2 | `42` | 0x42 | device-state/id block (constant) [H]             |
| 3 | `00` | 0x00 | device-state/id block (constant) [H]             |
| 4 | `00` | 0x00 | device-state/id block (constant) [H]             |
| 5 | `48` | 0x48 | effect opcode                                    |
| 6 | `18` | 0x18 | effect index (numeric; one of the observed demo/`48` effects) |
| 7 | `0C` | 0x0C | clock-write opcode                               |
| 8 | `BE` | 0xBE | clock tick `t`                                   |
| 9 | `1B` | 0x1B | CRC-8 over bytes 1..8                            |

### 12.12 Show FEC countdown: `94 F1 48 85 58 0F ED`

A delayed fade-out copy (section 3, "Show FEC countdown"). CRC verified.

| # | byte | hex | meaning                                          |
|---|------|-----|--------------------------------------------------|
| 1 | `94` | 0x94 | length header: `9` + `4` => 7-byte frame (`total = 4 + 3`) |
| 2 | `F1` | 0xF1 | FEC delay prefix: low nibble x 100 ms => 100 ms delay |
| 3 | `48` | 0x48 | effect opcode                                    |
| 4 | `85` | 0x85 | fade out (`48 85`)                               |
| 5 | `58` | 0x58 | cycle-duration modifier opcode                   |
| 6 | `0F` | 0x0F | duration ~1.5 s (~100 ms/count)                  |
| 7 | `ED` | 0xED | CRC-8 over bytes 1..6                            |

### 12.13 Fused palette left/right (UNVERIFIED): `93 0E 00 0E 94 40`

A single-burst palette fused frame: `0E 00` seeds both ears with a palette
shade, then `0E 94` overrides the left ear via the per-ear-register bit,
all in ONE frame. UNVERIFIED on real ears -- see docs/human-testing.md A3a.
A mixed form (`92 0E <right> 6L`) mixes a palette-right with a simple
left-only opcode.

| # | byte | hex | meaning                                            |
|---|------|-----|----------------------------------------------------|
| 1 | `93` | 0x93 | length header: `9` + `3` => 6-byte frame (`total = 3 + 3`) |
| 2 | `0E` | 0x0E | palette opcode                                     |
| 3 | `00` | 0x00 | palette index 0x00 (both ears) = pale cyan-white    |
| 4 | `0E` | 0x0E | palette opcode (left-only override)                |
| 5 | `94` | 0x94 | index 0x14 OR `0x80` => left ear orange-red only    |
| 6 | `40` | 0x40 | CRC-8 over bytes 1..5                              |

(The earlier `` `94 0E 00 0E 94 11` `` thread example used the wrong length
nibble `94`; the correct header here is `93`.)

## 13. Verified show incantations [P]

Beyond the bare `48 XX` effect invocation (section 4), the park captures
reveal composite *show phrases* -- countdown fades, color-rotation set
pieces, strobe-into-program, and fused per-ear pulse phrases -- that the
show controllers transmit as one coherent unit.  These are encoded in
`python/mwm/incant.py` (the Python port; the TS/Perl ports model the
decoder/encoder framing described here if they later add a show-player),
and emitted by `tools/mwm-send.py` via the `incant` verb and by HA
`mwm_ears.set_state` via the `incantation` field.

Each builder reproduces, byte-for-byte, a real captured frame; the `n`
counts are from the six-source corpus (section 9).

### 13.1 Fused pulse: `build_pulse(left_palette, right_simple)`

Makes a left ear glow one palette shade while the right (and both) ears
carry a simple color, with the pulse effect invoked and concluded by the
`D0 45 83` close clause -- all in one frame.  `n` = exact-frame occurrences
in the six-source corpus; the `pulse` *family* (`eff(04)`) totals 464.

| Source hex (exact capture)                                     | n  | Meaning                                    |
|---------------------------------------------------------------|----|--------------------------------------------|
| `9B 96 26 0E 81 61 58 F0 48 04 D0 45 83 23`                   | 53 | left sky blue (palette 0x01), both blue    |
| `9E 94 26 64 0E 8D 58 F0 48 04 D0 42 16 D0 45 83 C6`           | 11 | both red, left rose pink + cycle 0x16      |
| `9E 95 26 61 0E 97 58 F0 48 04 D0 42 16 D0 45 83 E7`           | 61 occ / 26 distinct | both blue, left palette 0x17 + cycle 0x16 |

The shape masks to: `X 96 26 paletteL colB tt 58 F0 48 04 [D0 42 tt] D0 45 83`
(the `9E 94 26` prefix is the both-ear simple-color form; `9B 96 26` is the
per-ear-register opener).  The left-palette + simple-other form
(`color = left-palette,colB(xx)`) totals 54; the both-simple-first form
(`colB(xx),left-palette`) totals 61.  Default emission order is
left-palette first; `both_first=True` swaps to the both-simple +
per-ear-palette register that the second/third rows use.

`reset=True` emits the override phrase the park uses to **re-color a
running pulse** (a plain fused phrase is ignored while the program runs;
the `24` override must lead the same frame, not precede it):

| Source hex (exact capture)                                    | n | Meaning                                   |
|--------------------------------------------------------------|---|-------------------------------------------|
| `9C 20 24 0D 61 0E 88 58 F0 48 04 D0 42 06 70`                | 4 | both blue, left palette purple + cycle 06 |

`build_off()` emits `91 48 1F B2` (catalogue "invoke off") to stop the
program. Alternative stops seen in the corpus: bare reset ping
`91 F1 24 9C` (both ears black immediately), `90 60 A6` (both off).

### 13.2 Strobe into running program: `build_strobe()`

Delay `F1`/reset/override, white solid, short cycle `58 02`, then
`48 84` strobe-into-program.  The strobe family (`48 84`) totals 293 for
`96 F1 24 67 58 02 48 84 8D`; the immediate variant
`95 20 67 58 01 48 84 3C` is the no-reset `20`-start form.

Measured on real ears (2026-09): each visible flash is a full on+off at
two `58 tt` counts (~100 ms/count), so the flash period is ~200 ms x tt --
`58 01` = 48 flashes/10 s, `58 02` = 25/10 s.

### 13.3 Fade-out countdown: `build_fade()`

The park workhorse (1375 `48 85` invocations): `F?` delay prefixes a
`48 85` fade-out, `58 tt` stretches the duration.

| Source hex (exact capture)                                | n   | Meaning                                    |
|-----------------------------------------------------------|-----|--------------------------------------------|
| `94 F2 48 85 58 05 DD`                                    | 16  | 200 ms delay, ~0.5 s duration              |
| `94 20 48 85 58 05 00`                                    | 15  | immediate copy, ~0.5 s duration            |

### 13.4 Color rotation set piece: `rotation_phrase(color)`

`F1 24` + `48 11` color rotation + `D0 3D tt` pace + color codes + `FA`/
`FC` + `48 85`, i.e. the piece ALTERNATES the two ears in the base color
(observed: left twice, right once, then fade-out ~1 s) — it does NOT sweep
through colors.

`9B F1 24 48 11 D0 3D 01 62 6A FA 48 85 1C` (exact-frame n=2; the
`48 11` rotation-family total is 136): both green (`62`) and left green
(`6A` = `68`+2), rotations centrally controlled, fade out at ~1 s.

Palette shades ride the two-byte ``0E pp`` pair in the color slots, not a
bare index:

`9D FD 24 48 11 D0 3D 01 0E 0D 0E 8D FC 48 85 16` (n=11): both/left
rose-pink (Palette `0x0D`), leading delay `FD`, fade prefix `FC`.

`D0 3D tt` paces the alternations and is a WIDTH knob, not a length knob:
larger `tt` spaces the flashes wider so FEWER fit before the built-in fade
(observer tt ladder: tt=0x02 two flashes then blank, tt=0x04 one flash,
tt=0x20 nothing at all); the corpus and all probe rows use tt=0x01.

## Sources

- Palette table, ear-modifier rules, fused-frame examples:
  [oPossum, post 259750](https://doityourselfchristmas.com/forum/index.php?threads/glow-with-our-show-and-msp430g2553-discussion.25142/page-6#post-259750)
  (thread page 6).
- One-bit color tables (Jon Fether's analysis, quoted by another poster):
  thread page 2. Jon Fether's original writeup posts were later edited down;
  the quoted tables above are what survives in-thread.
- CRC parameters (order 8, poly 0x31, init/xorout 0x00, reflected in/out):
  thread page 3.
- The originally referenced post
  ([page 22 #post-399985](https://doityourselfchristmas.com/forum/index.php?threads/glow-with-our-show-and-msp430g2553-discussion.25142/page-22#post-399985),
  MousieMagic) was edited down to read just "Cleanup" on Nov 16, 2015; its
  content is covered by the sources above.
