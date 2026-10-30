"""MWM command envelope for the Home Assistant infrared framework.

The framework hands emitter hardware any object implementing the
infrared_protocols Command contract (modulation, repeat_count,
get_raw_timings() -> signed-microsecond runs). infrared-protocols ships no
MWM codec -- this module provides our own, subclassing the library's Command
base when it is installed (HA installs manifest requirements automatically)
and falling back to a structural stand-in otherwise so the component stays
self-contained in test environments.
"""

from __future__ import annotations

from .protocol import CARRIER_HZ, raw_timings

try:  # pragma: no cover - exercised only when HA installed the requirement
    from infrared_protocols.commands import Command as _FrameworkCommand
except ImportError:  # minimal stand-in with the same construction contract

    class _FrameworkCommand:  # type: ignore[no-redef]
        def __init__(self, *, modulation: int, repeat_count: int = 0) -> None:
            self.modulation = modulation
            self.repeat_count = repeat_count

        def get_raw_timings(self) -> list[int]:
            raise NotImplementedError


class MwmCommand(_FrameworkCommand):
    """One MWM show message ready for an infrared emitter.

    Args:
        frame: The framed MWM bytes to transmit.
        repeat_count: Number of extra spaced re-transmissions.
        space_stretch_us: Extra microseconds added to every space (negative)
            segment in the merged timing list. Some Tuya-firmware S18 IR
            blasters compress spaces by a fixed offset; pre-stretching spaces
            compensates for that clock compression.
    """

    def __init__(
        self,
        frame: bytes,
        *,
        repeat_count: int = 0,
        space_stretch_us: int = 0,
    ) -> None:
        super().__init__(modulation=CARRIER_HZ, repeat_count=repeat_count)
        self.frame = bytes(frame)
        self._space_stretch_us = space_stretch_us

    def get_raw_timings(self) -> list[int]:
        timings = raw_timings(self.frame)
        if self._space_stretch_us == 0:
            return timings
        out = list(timings)
        for i in range(1, len(out), 2):  # spaces are odd indices in HA convention
            out[i] -= self._space_stretch_us
        return out

    def __repr__(self) -> str:  # debugging aid
        return f"MwmCommand({self.frame.hex().upper()})"
