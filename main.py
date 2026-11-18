#!/usr/bin/env python3
"""Compatibility entry point for the Micro-only fork."""

import sys

from micro_hunter import main


if __name__ == "__main__":
    print(
        "This fork is restricted to VM.Standard.E2.1.Micro Always Free instances.",
        file=sys.stderr,
    )
    raise SystemExit(main())
