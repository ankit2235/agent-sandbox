#!/usr/bin/env python3
"""Fail if a hidden task, or a canary from one, is about to be committed.

Hidden tasks live in a separate private repository. Phase 5 adds the canary
format; this check exists from Phase 0 so the rule is never accidentally broken
in between.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKERS = ("sandbox-canary:", "visibility: hidden", 'visibility: "hidden"')
SKIP_SUFFIXES = (".pyc",)
SELF = Path(__file__).name


def main(paths: list[str]) -> int:
    """Return 1 if any staged file looks like hidden task content."""
    offences: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file() or path.suffix in SKIP_SUFFIXES or path.name == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for marker in MARKERS:
            if marker in text:
                offences.append(f"{path}: contains {marker!r}")
    for offence in offences:
        sys.stderr.write(f"hidden content must not live in the public repo: {offence}\n")
    return 1 if offences else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
