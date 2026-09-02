#!/usr/bin/env python3
"""Compatibility entry point for the local snapshot-based workflow.

The previous MusicKit API implementation is intentionally removed from the
execution path. Use workflow.py for Step 1, Step 2, and Agent preparation.
"""

from __future__ import annotations

import sys

from workflow import main


if __name__ == "__main__":
    raise SystemExit(main(["run", "--use-default-count-file", *sys.argv[1:]]))
