"""Ear color tables and nearest-color matching.

Simple one-bit colors (60-6F) are the saturated primaries; the mixed palette
(0E XX) holds 30 measured shades.

Sources of truth:
    samples/mwm-gwts-colors.tsv  -- Rig-verified palette RGB values and
        color names.  All measurements by oPossum (DIYC forum post #259750);
        names in this module MUST match the TSV "description" column exactly.
    docs/mwm-show-protocol.md    -- Protocol reference documenting the
        0x60-0x67 simple color opcodes and the 0x0E palette command form.
"""

from __future__ import annotations

# code -> (name, rgb)
SIMPLE_COLORS: dict[int, tuple[str, tuple[int, int, int]]] = {
    0x61: ("blue", (0x00, 0x00, 0xFF)),
    0x62: ("green", (0x00, 0xFF, 0x00)),
    0x63: ("cyan", (0x00, 0xFF, 0xFF)),
    0x64: ("red", (0xFF, 0x00, 0x00)),
    0x65: ("magenta", (0xFF, 0x00, 0xFF)),
    0x66: ("yellow", (0xFF, 0xFF, 0x00)),
    0x67: ("white", (0xFF, 0xFF, 0xFF)),
}

SIMPLE_COLOR_CODES = {name: code for code, (name, _) in SIMPLE_COLORS.items()}
EAR_STATE_OFF = "off"

_PALETTE_RGB: list[tuple[str, tuple[int, int, int]]] = [
    ("pale cyan-white", (0xAC, 0xFE, 0xFE)),  # 00
    ("sky blue", (0x1F, 0x90, 0xFE)),          # 01
    ("azure blue", (0x1F, 0x4D, 0xFE)),        # 02
    ("blue-violet", (0x1F, 0x00, 0xFE)),       # 03
    ("pure blue", (0x00, 0x00, 0xFE)),         # 04
    ("pale pink", (0xFF, 0xCB, 0xFE)),         # 05
    ("violet", (0xAC, 0x4D, 0xFE)),            # 06
    ("indigo", (0x61, 0x26, 0xFF)),            # 07
    ("purple", (0x77, 0x01, 0xAB)),            # 08
    ("orchid pink", (0xFF, 0xAC, 0xFE)),       # 09
    ("magenta", (0xFF, 0x2C, 0xFF)),           # 0A
    ("fuchsia", (0xFE, 0x0D, 0xFF)),           # 0B
    ("rose magenta", (0xFF, 0x00, 0xCA)),      # 0C
    ("rose pink", (0xFF, 0x00, 0x61)),         # 0D
    ("scarlet", (0xFF, 0x00, 0x11)),           # 0E
    ("golden yellow", (0xFF, 0xCB, 0x16)),     # 0F
    ("orange", (0xFF, 0x56, 0x0A)),            # 10
    ("bright orange", (0xFF, 0x77, 0x01)),     # 11
    ("pure yellow", (0xFF, 0xFF, 0x00)),       # 12
    ("red-orange", (0xFF, 0x44, 0x00)),        # 13
    ("orange-red", (0xFF, 0x11, 0x00)),        # 14
    ("pure red", (0xFF, 0x00, 0x00)),          # 15
    ("cyan", (0x00, 0xFE, 0xFF)),              # 16
    ("spring green", (0x00, 0xFE, 0x6B)),      # 17
    ("green-cyan", (0x00, 0xFE, 0x2C)),        # 18
    ("pure green", (0x00, 0xFE, 0x00)),        # 19
    ("lime green", (0x01, 0xFF, 0x00)),        # 1A
    ("pale green-white", (0xDB, 0xFF, 0xCA)),  # 1B
    ("white", (0xFE, 0xFE, 0xFE)),             # 1C
    # 1D is off/black; excluded from color matching.
]

PALETTE: dict[int, tuple[str, tuple[int, int, int]]] = {
    index: name_rgb for index, name_rgb in enumerate(_PALETTE_RGB)
}


def _hsv(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    import colorsys

    r, g, b = (v / 255.0 for v in rgb)
    h, s_, v = colorsys.rgb_to_hsv(r, g, b)
    return h * 360.0, s_, v


def _snap_cost(
    target: tuple[int, int, int], cand: tuple[int, int, int]
) -> float:
    """Perceptual snap cost: HUE dominates, saturation next, value last.

    Plain RGB distance sent dark/muted requests to wildly different
    hues (brown -> orange, gray-blue -> cyan) because every reachable
    ear shade is bright and saturated. Users read wrong hue as "far
    away"; brightness differences are tolerated far more.
    """
    th, ts, tv = _hsv(target)
    ch, cs, cv = _hsv(cand)
    dh = abs(th - ch)
    if dh > 180.0:
        dh = 360.0 - dh
    hue_term = (dh / 180.0) ** 2 * 100.0
    # Hue is meaningless near the gray axis -- let saturation match rule.
    hue_weight = min(ts, cs)
    sat_term = (ts - cs) ** 2 * 30.0
    val_term = (tv - cv) ** 2 * 20.0
    return hue_weight * hue_term + sat_term + val_term


def nearest_entry(
    rgb: tuple[int, int, int]
) -> tuple[str, int]:
    """Snap an RGB triple to the closest representable ear color.

    Returns (kind, code) where kind is "simple" or "palette".  Simple
    colors win ties because their left-only primitive is a single
    opcode (0x68-0x6F) while palette left-only needs the ``|80``
    modifier on a two-byte ``0x0E`` command.
    """
    # Prefer simple colors on cost ties (simpler left-only primitive);
    # code breaks any remaining tie deterministically.
    candidates: list[tuple[float, int, int]] = []
    for code, (_, ref) in SIMPLE_COLORS.items():
        candidates.append((_snap_cost(rgb, ref), 0, code))
    for index, (_, ref) in PALETTE.items():
        candidates.append((_snap_cost(rgb, ref), 1, index))
    preference, code = min(candidates)[1:]
    if preference == 0:
        return "simple", code
    # A palette shade won, but if it is visually no different from a simple
    # one-bit color (see _SIMPLE_EQUIV_DELTA) snap to that opcode instead:
    # simpler left-only primitive, and nothing a viewer can tell apart.  The
    # exact-command path (select_color) can still send the twin.
    simple_code = _equivalent_to_simple(PALETTE[code][1])
    if simple_code is not None:
        return "simple", simple_code
    return "palette", code


# A palette shade this close to a simple color IS that simple color to the
# human eye (and to the rig's measurement noise): the measured "pure"/lime
# variants differ from the one-bit opcodes by only a few LSBs -- lime green
# 0x1A (0x01,0xFF,0x00) vs simple green (0x00,0xFF,0x00), pure blue 0x04
# (0x00,0x00,0xFE) vs blue (0x00,0x00,0xFF), palette white 0x1C (0xFE,0xFE,
# 0xFE) vs white (0xFF,0xFF,0xFF).  Known-identical shades resolve to the
# simple opcode; visibly distinct measures (magenta 0x0A, scarlet 0x0E, ...)
# are far outside this band.
_SIMPLE_EQUIV_DELTA = 2


def _equivalent_to_simple(rgb: tuple[int, int, int]) -> int | None:
    """Return the simple opcode whose color ``rgb`` is indistinguishable."""
    for code, (_, ref) in SIMPLE_COLORS.items():
        if all(abs(a - b) <= _SIMPLE_EQUIV_DELTA for a, b in zip(rgb, ref)):
            return code
    return None


# %%
# A JSON-serializable catalog of every representable ear color, for the
# Lovelace card (frontend/mwm-ears-card.js) and any other consumer. Every
# value a user could pick -- 7 simple one-bit colors plus the 30 measured
# palette shades except the black/off entry (index 0x1D) -- is listed with
# its name, RGB triple, and the protocol code/index that selects it. All
# three side palettes (left/both/right) draw from this same list: each side
# entity can represent every color, differing only in which frame is
# transmitted, which lives in the entity, not here. Shades that LOOK near-
# identical to a simple color (e.g. palette 0x04 "pure blue" vs 0x61 "blue")
# are deliberately kept: they are distinct protocol commands and both must
# remain individually selectable.
def color_palette() -> list[dict]:
    out: list[dict] = []
    for code in sorted(SIMPLE_COLORS):
        name, rgb = SIMPLE_COLORS[code]
        out.append({"name": name, "rgb": list(rgb), "kind": "simple", "code": code})
    for index in sorted(PALETTE):
        if index == 0x1D:  # black / off, not a pickable shade
            continue
        name, rgb = PALETTE[index]
        out.append({"name": name, "rgb": list(rgb), "kind": "palette", "index": index})
    return out


# Alternate spellings accepted on INPUT only; the catalog spellings are the
# canonical output and stay unchanged (there is no gray shade in the table,
# but the matcher stays tolerant of the gray/grey variant and any future one).
_NAME_SPELLING_ALIASES = {
    "grey": "gray",
}


def _normalize_color_name(name: str) -> str:
    key = name.strip().lower()
    for alt, canon in _NAME_SPELLING_ALIASES.items():
        key = key.replace(alt, canon)
    return " ".join(key.split())


def _lookup_color_name(
    name: str, kind: str | None = None
) -> tuple[str, int] | None:
    """Resolve a catalog color name to (kind, code/index).

    Case-insensitive, spelling-tolerant (gray/grey and the like).  A bare
    name shared by BOTH tables -- "white", "cyan", "magenta" -- resolves to
    the simple one-bit opcode, since that is the natural (single-code)
    command for the shared identity; pass the kind explicitly to force a
    palette shade.
    """
    key = _normalize_color_name(name)
    if kind in (None, "simple"):
        for code, (cname, _) in SIMPLE_COLORS.items():
            if _normalize_color_name(cname) == key:
                return "simple", code
    if kind in (None, "palette"):
        for index, (cname, _) in PALETTE.items():
            if _normalize_color_name(cname) == key:
                return "palette", index
    return None


def parse_color(spec: str) -> tuple[str, int]:
    """Parse an exact-color selector into (kind, code/index).

    Accepted by the integration's ``mwm_ears.select_color`` service, which
    sends an EXACT protocol command bypassing color-wheel snapping:

    - ``"simple:0x61"`` / ``"palette:4"`` -- kind plus a decimal or ``0x``
      hex code/index;
    - ``"simple:blue"`` / ``"palette:white"`` -- kind plus a catalog name;
    - a bare catalog name such as ``"lime green"`` or ``"Pure Blue"``
      (shared names like "white"/"cyan"/"magenta" prefer the simple one-bit
      opcode, per ``_lookup_color_name``).

    Names use the catalog spelling, matched case-insensitively and tolerating
    spelling variants (e.g. ``gray``/``grey``).  Raises ValueError for
    unknown or malformed selectors.
    """
    spec = spec.strip()
    kind, sep, value = spec.partition(":")
    if sep and kind.strip().lower() in ("simple", "palette"):
        kind = kind.strip().lower()
        value = value.strip()
        try:
            code = int(value, 0)  # decimal or 0x hex
        except ValueError:
            entry = _lookup_color_name(value, kind)
            if entry is None:
                raise ValueError(
                    f"unknown {kind} color name {value!r} ({spec!r})"
                ) from None
            return entry
        table = SIMPLE_COLORS if kind == "simple" else PALETTE
        if code not in table:
            raise ValueError(f"unknown {kind} color 0x{code:02X} ({spec!r})")
        return kind, code
    entry = _lookup_color_name(spec)
    if entry is not None:
        return entry
    raise ValueError(
        f"unknown color {spec!r}; use a catalog name or 'kind:value'"
    )
