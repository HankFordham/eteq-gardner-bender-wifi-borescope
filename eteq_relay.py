#!/usr/bin/env python3
"""Compatibility wrapper for the original single-file script.

The tool now lives in the ``eteq`` package. This keeps older command lines
working. Prefer ``eteq`` (after ``pip install -e .``) or ``python -m eteq``.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from eteq.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
