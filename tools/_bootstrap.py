#!/usr/bin/env python3
"""Shared bootstrap for the MWM rig research tools in this directory.

Every tool in ``tools/`` needs to import the ``mwm`` protocol library that
lives at ``python/mwm/``.  Importing that package directly is awkward from
an arbitrary working directory, so this module locates it on disk once and
puts it on ``sys.path``.

To use, at the top of any tool (right after stdlib imports)::

    from _bootstrap import mwm   # or: import _bootstrap

    build_frame      = mwm.build_frame
    irsend_payload   = mwm.irsend_payload
    describe_frame   = mwm.describe_frame
    ...

The imported library is exposed as ``mwm`` (a module object) here so tools
only ever depend on this one bootstrap, not on repeating the path math.

Why this indirection: the tools live at the repository root under
``tools/``, while the library is one directory down under ``python/mwm``.
Rather than each tool duplicating a fragile relative-path computation (and
breaking if moved), we centralise it in exactly one place and document the
intent.

NOTE: this is a *research* harness for human-driven rig testing.  It only
talks to MQTT and the on-disk library; it does not import Home Assistant.
Run the library's unit tests from ``python/tests/`` instead
(``make test-python``).
"""

from __future__ import annotations

import sys
from pathlib import Path

# This file is at <repo>/tools/_bootstrap.py, so its parent is the repo
# root and the library sits one directory down under python/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MWM_SRC = _REPO_ROOT / "python" / "mwm"

if str(_MWM_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_MWM_SRC.parent))

# Import mwm as a top-level package.  The library is self-contained pure
# stdlib and has no Home Assistant dependency.
import importlib  # noqa: E402

mwm = importlib.import_module("mwm")
