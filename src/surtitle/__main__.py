"""Entry point for ``python -m surtitle``.

The launcher scripts use this form rather than the console script, because it
works from a bundled runtime without relying on a launcher executable that a
zip extraction may not have marked executable on Unix.
"""

from __future__ import annotations

import sys

from surtitle.cli import main

if __name__ == "__main__":
    main(sys.argv[1:])
