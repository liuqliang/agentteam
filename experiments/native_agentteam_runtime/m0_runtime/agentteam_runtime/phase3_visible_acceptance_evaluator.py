#!/usr/bin/python3
"""Trusted argv evaluator artifact used by the common experiment finalizer."""

from __future__ import annotations

import subprocess
import sys


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2 or arguments[0] != "--":
        return 64
    return subprocess.run(arguments[1:], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
