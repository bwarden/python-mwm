# MWM ear rig — human testing plan

This is the manual, human-in-the-loop testing plan for the MWM / Glow-With-
The-Show ear rig. Each entry says **what to do**, **what to expect on real
ears**, and **what we hope to learn** from it. It complements the passive
research tools (`tools/capture_beacons.py`, `tools/analyze_beacons.py`) and
the all-in-one `tools/sync_analysis.py`; use `tools/mwm-send.py` and
`tools/color_cycle.py` for the drive tests below.

Referenced docs:
- `docs/mwm-show-protocol.md` — full instruction set, opcode tables, phrase
  templates, confidence labels (`[T]`=thread/theory, `[P]`=park capture,
  `[R]`=rig-verified, `[H]`=hypothesis), plus physical layer, framing, CRC-8
  (the merged successor of the former `mwm-gwts-protocol.md`).
- `samples/mwm-gwts-colors.tsv` — verified color frames.

## Rig setup (pre-flight)

- One set of ears in front of the rig. Only ONE pair — the protocol
  research treats a single set as the unit under test.
- Broker/creds in `~/.config/ir-remote-tools/mqtt.json`; receivers
  600605 / 179E4E.
- Tools run from the repo root or `tools/`; they need `mosquitto_pub` /
  `mosquitto_sub` on PATH and the `_mwm` library (via `tools/_bootstrap.py`).
- `python3 tools/mwm-send.py --monitor 600605` gives you an interactive
  sender that also prints whatever the ears beacon back for ~5 s after each
  send — use this for most drive tests.  The `hex` verb auto-appends the
  trailing CRC-8 byte when you drop it (e.g. `hex 91 48 1F` is sent as
  `91 48 1F B2`), so docs hex can be pasted as-is even when written without
  the checksum.

General safety: each IR command is short; a single ear set in demo mode will
re-beacon on its own every ~7-15 s and that is expected. Power-cycle the ears
(battery pull) only if they go dark and don't come back — the deep-sleep
state cannot be IR-woken.

---

## A. Confirm the drive commands (highest value)

The integration now composes differing left/right pairs as a **fused**
2-byte frame (`91 <right> <left-only(left)>`). This must be verified on
real ears because the older code used a coordinated two-burst sequence, and
some decoders are strict about deviant lengths.

### A1. Fused left/right frame [R → re-confirm after refactor]
- **Tool:** `python3 tools/mwm-send.py` then `fused red green`
- **Expect:** left ear solid red, right ear solid green, in one burst, stable
  for a couple seconds.
- **Learn:** whether the single fused frame reliably sets both ears
  (per-side control in one shot) or whether the ears only decode the first
 /last opcode. This validates the `_state_frames` refactor in `ears.py`.
- **Variants:** `fused blue yellow`, `fused white magenta`, then the
  equal-pair control `simple both blue`.

### A2. Simple colors, both and left-only [R → guard the flipped labels]
- **Tool:** `mwm-send.py` → `simple both <c>`, `simple left <c>` for each
  of blue/green/cyan/red/magenta/yellow/white.
- **Expect:** both-ears sets both; left-only changes only the left ear.
- **Learn:** the `69`-`6F` single-ear side assignment is **RESOLVED**: the
  `0x08`/`0x80` override slot is the physical **LEFT** ear. Confirmed on
  hardware with the tag in back (the wearer's left ear answered the frame's
  old "right" command), and independently by viperfan91's friend-code chart
  (`samples/viperfan91-paintbrush.txt`), whose flashing ear — the same
  override slot — is labeled "left". The old `[T]` dispute (Jon's decode
  implied right, djred2000 listed left) is settled in djred2000's favour;
  earlier docs/code used the frame's own viewer-facing "right" label. This
  test guards the flipped labels rather than re-resolving the assignment.

### A3. Left-ear palette pick composition [R → open]
- **Tool:** `simple both white` (clear), then drive a *left* palette shade
  through the integration. Direct tool path: `mwm-send.py` only sends
  verified frames; the integration's left palette logic lives in `ears.py`
  (both + left-only override, or degrade to both-ears form).
- **Learn:** whether left palette picks that fall back to "both ears" are an
  acceptable UX (protocol limitation: palette shades are inherently
  both-ears) and whether the both+restore composition is perceptible.

### A3a. Fused PALETTE left/right frame — UNVERIFIED [open]
The left color wheel previously sent *successive* frames (both-ears seed of
the other ear's color, then a left-only override of the picked color), so
the left ear flashed to the dragged color and back (or was clobbered when
it held a simple color). The integration now emits a single *fused* frame
for left picks, but the palette-involving fused forms were never confirmed
on real ears -- this rig test verifies them.
- **Tool:** `python3 tools/mwm-send.py` interactive, or one-shot
  `fused <left> <right>` (each arg a simple name or `pal:0x19`). Cover all
  four combos:
  - `fused blue pal:0x04` — left simple / right palette
  - `fused pal:0x19 red`  — left palette / right simple
  - `fused blue red`      — left simple / right simple
  - `fused pal:0x19 pal:0x04` — left palette / right palette
  (or `mwm-send.py` → `hex 93 0E 00 0E 94 40` to send a single fused frame).
- **Frames to send and expected result (start from `simple both white`):**
  - `93 0E 00 0E 94 40` — fused palette+palette in ONE burst: the first op
    `0E 00` seeds the right ear pale-cyan-white, the second `0E 94`
    (`0x14|0x80`) overrides the left ear to orange-red. If the ears honor a
    two-opcode fused frame with palette opcodes, both set in one shot with
    no flash.
  - `92 0E 19 6C 8A` — fused palette + simple in ONE burst: the first op
    `0E 19` seeds the right ear pure green, the second `6C` (`0x6C` = left
    red) overrides the left ear to simple red. Tests mixing the 2-byte
    palette opcode with the 1-byte simple left-only opcode.
  - `94 0E 00 0E 94 11` — NEGATIVE control: wrong length nibble
    (`L=4` in a 6-byte frame; `93` is the correct header). Real ears
    reject this on the length rule, so "ignored" here does NOT mean fused
    palette is unsupported.
- **Learn:** whether a correctly-formed fused palette frame drives per-side
  palette colors in one burst. The integration already emits these fused
  forms for left picks (`ears.py _state_frames`); this rig test should
  confirm real ears honor them so a left pick stops flashing/clobbering
  the other ear entirely. If they *don't* work on hardware, it falls back
  to the old two-frame both+restore.

### A3b. Simple vs palette "twins" via `select_color` [open]
The measured palette has shades within a few LSBs of a simple one-bit color
(simple `0x61` blue vs palette `0x04` pure blue, green vs `0x1A` lime green,
white vs `0x1C` white). They are DISTINCT commands, and the card's `select_color`
action is the only path that sends them explicitly -- the color wheel snaps
near-identical shades to the simple opcode (`nearest_entry`'s
`_SIMPLE_EQUIV_DELTA` band) because a viewer cannot tell a 0xFE from a 0xFF.
- **Tool:** the `mwm_ears.select_color` action (Actions dev tool, or the card
  swatches) with `color: "simple:0x61"` / `"palette:4"` on the same side.
- **Expect:** each twin pair commands the same-looking ear; the Actions tool
  call must be distinguishable in the receiver decode log by frame bytes
  (single `0x0E XX` opcode vs `91 0E XX` + `|80` left-only form).
- **Learn:** whether the twins are truly indistinguishable on hardware (and
  if so, whether collapsing them to the simple opcode costs anything). Pairs:
  blue/pure-blue, green/lime-green, cyan, red/pure-red, magenta,
  yellow/pure-yellow, white.

### A4. Effects catalog walk [T/P → confirm each]
- **Tool:** `python3 tools/color_cycle.py --log effects.json` (walks simple,
  palette, composite, and every built-in effect, prompting for notes), or
  `mwm-send.py` → `effect <index>`.
- **Expect:** each `LIGHT_EFFECTS` label (Fade out, Fade up, Single flash,
  Pulse, Strobe flash, Hard transitions, Crossfade transitions,
  Color rotation, Flashing sequence, Quick four-color rotation, Random,
  Blackout) produces the described behavior.
- **Learn:** which of the `48 XX` programs are confirmed on the rig vs only
  "seen" in park capture `[P]`. Records TRUE/observed notes per effect for
  `tools/analyze_log.py`.

### A5. Off is sticky (the beacon race debounce) [R → regression]
- **Tool:** `mwm-send.py` → `simple both blue`, wait ~5 s (let a beacon
  arrive), then `mwm-send.py` → `hex 90 60` (both off).
- **Expect after the `BEACON_STALE_AFTER_COMMAND_S` (15 s) fix:** the OFF
  sticks — the entity stays off even though a stale beacon may have already
  been in flight. After the 15 s window, a *genuinely* re-woken ear shows on.
- **Learn:** that the race fix (`ears.py`) survives a real beacon arriving
  right after an explicit off, and that rooms genuinely re-light after the
  window.

### A6. Incantations: effect phrases over the rig [R → confirm; color
interchange open]
The show incantation builders (`python/mwm/incant.py`, wired through
`tools/mwm-send.py incant …`) reproduce the park/hat/wand *fused* phrases
byte-for-byte.  The rig test below does two things: (1) confirm each
incantation actually drives the ears (they are `[R]`-labeled only from the
beacon side so far, not from effect *behavior*), and (2) probe the **color
interchange** question — every color slot accepts BOTH a simple name and a
`pal:0xNN` palette index.  The corpus only ever shows a *palette* LEFT ear +
*simple* RIGHT ear in these phrases; putting a simple in the left slot or a
palette in the right slot produces a structurally-valid fused phrase that
has never been seen in a real capture, so we do not know yet whether real
ears honor it.
- **Tool:** `python3 tools/color_cycle.py --incant --log incants.json`.
  It is a *decision tree*, not a blind sweep:
  - Colors are shown **by name** ("pure blue", "sky blue", ...) — never
    `pal:0xNN`, which means nothing to a human observer.
  - Before each family it prints **what to watch for** (the overall
    expectation).  Then every step is **announced before it is issued**:
    "next: <step> — <what should change>", and it waits for Enter before
    sending.  So the re-color step is never a surprise: you always know
    what command is coming and what should happen.
  - Each probe **starts** by anchoring to a known dark state (the `24`
    reset) and then runs the known park-shaped frame, so a failed step
    does not poison the one after.  The tool only resets between probes,
    never mid-probe — the pulse probe keeps the fresh pulse RUNNING so the
    re-color step can re-color a live pulse (a mid-probe reset would
    black out the ears right before it).
  - Verdict prompt is "did it work?" — `y` (yes, worked as described),
    `n` (no / badly / split / mis-colored), `r` (reissue the same command
    — capture may have been lost), or any free-form note.  The pulse probe
    asks twice: once for the fresh pulse, once for the re-color.  A
    non-`y` verdict on a probe's final step **steps through the probe's
    variants in order** (each announced and asked once — e.g. the
    reset-armed corpus fade ``F? 24 … 48 85`` vs the raw no-reset
    ``48 85`` countdown) until one works.
  - Watch receipts live with `python3 tools/mwm-send.py --monitor 600605`.
  - `--incant-random N` mixes N palette/simple combos into the pulse sweep.
- **Expect (observed so far):** a bare no-reset `fade` and the bare `stop`
  did *not* work when sent after the strobes — the ears went dark and
  nothing else ran until a reset was issued.  Corpus marked by old fade-in
  and rotation phrases leading with `24` suggests a reset/arm phrase may be
  required before new colors respond; the reset-armed fade retry encodes
  exactly that.  The pulse `reset=true` re-color of a *running* pulse is
  the other open question — and it works: the running pulse flips color
  and the pulse cycle **restarts** with the new colors.  The pulse's two
  ears **alternate** (they do not pulse in unison — that is the effect's
  normal cadence).
- **Effect color slots take a bare byte OR a ``0E pp`` pair:** the strobe
  and rotation slots flashed for simple bytes but *blanked or garbled* for
  BARE palette indices (`0x00`/`0x04`/`0x08` twice) — the gap was the
  ENCODING, not the hue.  The corpus rotation set-piece proves it: palette
  shades ride the two-byte ``0E pp`` form
  (`9D FD 24 48 11 D0 3D 01 0E 0D 0E 8D FC 48 85`).  The sweep now sends
  rotation palette rows in that EXACT corpus shape with only `pp` varied
  (one-field fuzz — rose pink `0x0D`, pure blue `0x04`, pale cyan-white
  `0x00`) plus one strobe row in the same ``0E pp`` form (A6: no
  palette-colored strobe in the corpus).  Gang probe 2026-09-06: both
  slots confirmed to carry the ``0E pp`` pair — rotation pure blue `0x04`
  and pale cyan-white `0x00` (the bare-index blanks) flashed in the
  corpus shape, and the strobe ``0E pp`` row answered too, so the
  palette-color slots are two-byte, not simple-only.
  Bare-index flashes (seen earlier in both slots) are the sans-encoding
  equivalent and blank/garbled.
- **Rotation ALTERNATES the ears in the base color, it does not sweep:**
  every confirmed run showed one color flashing left twice, right once,
  then fading out at ~1 s.  The `D0 3D tt` countdown paces the alternations
  and is a WIDTH knob, not a length knob — the tt ladder is ANSWERED:
  tt=0x02 flashed twice then blank, tt=0x04 flashed once, tt=0x20 showed
  nothing.  tt stays 0x01; every row is a single arm.
- **Fade-out leaves a dark gap; recovery is the next effect's abrupt
  start:** after a fade (countdown, colored, or the pulse's fade-to-dark)
  the ears stayed dark in several combos until a fresh effect command lit
  them again — no automatic cascade into the next effect, no fade-to-next.
  That gap is the open "soft transition" question the fade-to-dark and
  slow-cycle re-color probes chip at.
- **Observer is colorblind:** sky blue vs blue and pure blue vs blue are
  indecipherable, so every differentiating row uses only strongly
  contrasting colors (rose pink vs pure blue vs pale cyan-white, yellow vs
  pure blue, purple vs white).
- **Reset re-color whence:** `D0 42 tt` is the re-color *cadence* knob —
  not a fixed hold byte.  The corpus re-color used `0x06` ("y but
  actually pulsing faster" — the observed pulse quickens over a swap),
  while the fuzz `0x20` was confirmed "y much slower".  A continuous
  field, so the sweep walks a ladder of `tt` values (below) to
  characterize the mapping.
- **Soft color transition probe:** the reset re-color is abrupt.  A `pulse`
  row fades the running pulse out to dark, then re-lights it in the target
  combo — confirmed that a fresh plain pulse re-lights from the faded-dark
  state on its own (no reset/arm needed), but the re-light IS the next
  effect starting, so there is still a visible dark gap between the two.
- **The fixed sweep (the tool walks all of these):**
  - `pulse pal:0xNN <simple>` fresh, then `reset=true` *while still
    pulsing*, re-coloring to **pure-yellow / blue** (a bold wheel-contrast
    pair — the headband ears are pink-tinted, so rose-pink/white would be
    hard to pick out) — restarts the running pulse in the new colors
    instead of being ignored.
  - `pulse` fade-to-dark then re-light (soft transition) — the same combo,
    through the observed dark gap.
  - `pulse` re-color cadence ladder (`reset=true`, `D0 42 tt` walked one
    field at a time through 0x06 / 0x10 / 0x20 / 0x40) — post-swap pulse
    rate as a function of `tt`.  The 2026-09-06 pair (0x06 = "pulsing
    faster", 0x20 = "much slower") is the two-point sample this ladder
    characterizes: does the rate step linearly with `tt`, or floor out?
  - `strobe color=<simple>` — strobe into a standing color (cyan/magenta
    added to widen the simple-slot evidence), plus a `strobe` row carrying
    pure blue as the ``0E pp`` pair (is the strobe slot simple-only?).
  - `fade` (countdown) and the reset-armed ``F? 24 … 48 85`` retry.
  - `rotation <simple|0E-form palette>` — single-arm, tt=0x01; palette rows
    in the corpus-exact ``FD 24 48 11 D0 3D 01 0E pp 0E (pp|80) FC 48 85``
    shape (the tt ladder is answered and removed).
  - `stop` — invoke off, plus the bare reset-ping alternative stop.
- **Interchange probes (the A6 question — each is a valid frame that is
  NOT from any capture; expect "ignored" to be a live possibility):**
  - `pulse <simple> pal:0xNN` — simple LEFT, palette RIGHT.
  - `pulse pal:0xNN pal:0xMM` — both-ear palette.
  - `pulse pal:0x04 pal:0x04` — twins, both left.
  - `strobe` pure-blue ``0E pp`` row — palette color riding the strobe slot
    in the two-byte form (bare palette indices are no longer sent: the
    rotation set-piece proved they were the wrong ENCODING).
- **Learn:** whether every effect color slot accepts the ``0E pp`` pair
  (rotation: yes, corpus-proven; strobe: probed now; simple-only slots
  would be the odd ones out).  Also whether `D0 42 tt` changes the post-
  re-color cadence (`0x20` row probes it), and whether a faded-out pulse
  re-lights without an arm (confirmed: yes, as the next effect).

---

## B. Sync / clock research

### B1. Free-running clock tick from autonomous ears [R → deepen]
- **Tool:** `python3 tools/capture_beacons.py --out beacons.jsonl` for a few
  minutes, then `python3 tools/analyze_beacons.py beacons.jsonl`.
- **Expect:** the `0C t` tick advances ~10.5 ticks/s, linear modulo 256,
  both receivers agree per burst, and the tick is independent of the
  cycling `48 ss` demo effect.
- **Learn:** reconfirm the tick model against a longer fresh capture and
  check for drift/edge jumps at the modulo wrap.

### B2. Clock-write under a controlled (show/master) state — pending setup [H]
- **Tool:** `python3 tools/clock_write_experiment.py` (and
  `mwm-send.py` → `clock <hex>`).
- **Caveat from the docs:** in autonomous demo mode there is no single clean
  clock trajectory to write against — a clock-write is uninterpretable. This
  test is only meaningful with the ears parked in a known show / master-
  clocked state (i.e. after a real show wand phrase, not demo mode).
- **Learn:** whether writing `0C t` resets playback phase to a chosen tick
  (phase alignment across ears). Blocked until a controlled master state
  can be produced on the rig.

### B3. Group addressing [T → confirm frame intent]
- **Tool:** `mwm-send.py` → `grouppalette <lo> <hi> <idx>` /
  `groupcolor <lo> <hi> <code>` (ears pick a random group id 00-7F at
  power-up).
- **Expect:** frames are CRC-correct (validated) and, ideally, that a range
  covering the powered ears' groups drives them.
- **Learn:** whether the range semantics (`A0 rr` + `26` close) actually
  address ears by their random group id, or whether group ids need discovery
  first. Useful for later "isolate this one ear" control.

### B4. Delayed execution (`F?`) — countdown to effect start [H → new probe]
- **Tool:** `mwm-send.py --hex "94 F8 48 85 58 0F <crc>" --repeat 1`
  (an `F8` copy = execute in ~800 ms) against an immediate copy
  `94 20 48 85 58 0F <crc>`, with `--monitor` on a receiver. Pad `F8` so `<crc>`
  is the CRC-8 of the whole frame; verify CRC-validity via `--repeat 1` + the
  receiver decode.
- **Expect:** the `F?` delayed copy starts the effect ~`F`×100 ms after the
  frame lands; the `20` immediate copy starts it at once. Pick a visually
  distinct effect (0x85 fade out, 0x04 pulse) so the start is clean to time.
- **Learn:** whether ears actually gate *execution* on the `F?` delay field
  (rather than only echoing it back). This is the minimal countdown-to-start
  hook: if a delayed copy fires on time, a controller can anchor a chosen
  effect start to a measured wall-clock offset — turning the clock question
  from "re-phase an already-running effect" (needs the unknown epoch) into
  "start a fresh effect at a predictable time".

### B5. Mid-effect re-send: phase continuity vs retrigger [H → new probe]
- **Tool:** while an effect is visibly mid-run,
  `mwm-send.py --hex "<same effect frame>"` several times a few seconds
  apart, watching the effect for discontinuities and logging the beacon
  tick trajectory (`tools/capture_beacons.py` alongside).
- **Learn:** whether re-issuing the same command re-phases from zero (a
  visible jump/restart — then constantly sending to "stay in sync" must be
  clock-anchored too) or continues the current phase (idempotent — safe to
  re-send freely). This is the direct probe for "can we constantly send
  commands synchronized to the running effect's current state": we can read
  (`0C t`) and write (`91 0C t`) the tick, but we have NOT yet shown the
  ears schedule execution on that register, nor that a clock-write re-phases
  them (B2 is still blocked). Until B4/B5 confirm delay-gating and phase
  behavior, "synchronized to current state" is only achievable empirically
  (send → observe → adjust), not by absolute clock-state.

---

## C. Adoption / diagnostics (integration behavior)

### C1. Foreign-command adoption + suspension [R → re-confirm]
- **Tool:** point `mwm-send.py` (or a bare `hex`) at the ears while watching
  the HA *Assumed Ear State* / *Last Phrase* sensors, after the integration
  has driven the room.
- **Expect:** an overheard foreign wand frame is adopted into the displayed
  state and pauses the integration's repeats until your next action; idle
  beacons update the effect label without touching `desired_on`.
- **Learn:** the adoption/suspension UX is sound and the beacon does not
  clobber a deliberate off (ties into A5).

---

## What we most want to learn (priority order)

1. ~~The `69`-`6F` single-ear side assignment — right (Jon) vs left
   (djred2000) — disputed; resolution gates per-side control. (A2)~~
   **RESOLVED: physical LEFT** (override slot; see A2). This re-labeling is
   what the current library/tool/docs now use.
2. Whether the **fused pair frame** reliably drives per-side colors on real
   ears. (A1)
2a. Whether a **correctly-formed fused PALETTE frame** drives palette per-side
    in one burst (removing the left-pick flash/clobber). (A3a)
3. Which **effect programs** are actually rig-confirmed. (A4)
3a. Whether the **incantation effect phrases** drive real ears and whether
    their color slots accept **simple and palette interchangeably** (a
    palette byte in a simple slot and vice versa is a structurally-valid
    frame never seen in a capture). (A6)
4. Whether the **off-race debounce** behaves on real hardware. (A5)
5. Whether **group addressing** ranges work as documented. (B3)
6. (blocked) Whether a **clock-write** can phase-align ears once a controlled
   master state exists. (B2)
7. Whether the `F?` delay actually gates **execution** time (countdown to
   effect start) and whether re-issuing an effect **re-phases** it — the two
   unknowns that gate "constantly send commands synchronized to the running
   effect." (B4, B5)

## Recording results

- Drive tests: `color_cycle.py --log <name>.json` writes structured notes
  (`t`, `kind`, `name`, `hex`, `payload`, `notes`); decode them with
  `tools/analyze_log.py --log <name>.json`.
- Beacon/clock tests: `capture_beacons.py` writes JSONL; feed it to
  `analyze_beacons.py`.
- Demo/remote learning: `annotate_captures.py` listens to a receiver (or
  replays a piped RESULT feed) and for every non-beacon burst prints the
  raw frame + best-effort decode, then asks the observer what the command
  actually did; each answer is logged to `annotations_<ts>.jsonl` alongside
  the decoded summary, the timing/color fingerprints, and a "novel" flag for
  decodes the library does not yet understand (undocumented wand `gg/kk`
  pairs, unknown opcodes, invalid frames).
  - Frames are decoded from `IrReceived.RawData` with our own library — not
    from Tasmota's built-in `IrReceived.Data` (which at best only knows the
    FIRST message of an A-B-A' bundle, so relying on it silently drops whole
    pushes).  `Data` is accepted only as a fallback when `RawData` is
    missing and Tasmota labels the message `Protocol: MWM` with a
    validity-checked payload.
  - A-B-A' wand pushes arrive as three separate IR bursts (one per RESULT
    line); the tool reassembles the triplet and treats the completed set as
    ONE bundle — one prompt and one log row carrying all three frames
    (`triplet`) — so the observer matches behavior to the whole push.
  - Known demo beacons (whose effect is in `EFFECT_LABELS`) DO prompt,
    printing our interpretation so the observer can confirm or correct it;
    unknown-effect beacons are condensed to a status line.  `--all` forces
    every line through a full prompt.
  - Bursts that are the same command stepped to a different point in its
    cycle (only the `58`/`D0 42` timing bytes or clock tick moved) are
    recognized as repeats: shown as a timing/phase delta and never
    re-prompted, so the observer can watch how the pacing bytes index the
    cycle without re-answering the same command.
  - New frames that arrive while a note is being typed are superseded —
    when the note commits the queue is collapsed to its newest entry and
    the next prompt is for that burst alone; nothing interrupts
    mid-keystroke.
  - Run it while demo mode plays or a wand is waved; replay a recorded
    session with `annotate_captures.py --all log.jsonl`.
- When a test changes what we believe, update the confidence label
  (`[T]`→`[R]`, or annotate `[H]`→confirmed) in the referenced docs, in the
  same commit, per `docs/` source/confidence conventions.
