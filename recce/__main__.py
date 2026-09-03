"""Entry point for `python3 -m recce`.

The console script from `pyproject.toml` is a convenience. This is the
interface that matters on a locked-down machine, where the `recce/` directory
gets copied across and nothing is installed.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == '__main__':
    sys.exit(main())
