"""Compatibility wrapper for the packaged Codex Sync server.

Running ``python sync_server.py`` still starts the server from a source
checkout, while ``import sync_server`` returns the packaged implementation so
existing tests and scripts mutate the real module state.
"""
from __future__ import annotations

import sys

from codex_sync import sync_server as _impl


if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
