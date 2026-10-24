#!/usr/bin/env python3
"""Fail if a hidden task, or a canary from one, is in the given files.

Hidden tasks live in a separate private repository (``HIDDEN_TASKS_DIR``). This
check runs as a pre-commit hook on staged files and, in CI, over every tracked
file (``.github/workflows/no-hidden-leak.yml``). It looks for:

- a canary: ``sandbox-canary:`` followed by a UUID, the tag every hidden task carries
  (naming the prefix alone, as code and docs do, is fine);
- a task file marked hidden (its ``visibility`` key set to hidden).

``sandbox hidden leaks`` complements it in CI: no task file in the repository may
match a published hidden task's hash, however it is formatted or named.

Standard library only, so the hook runs without the project installed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CANARY = re.compile(
    r"sandbox-canary:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
HIDDEN_IN_DATA = re.compile(r"""\bvisibility["']?\s*:\s*["']?hidden\b""", re.MULTILINE)
"""In a YAML or JSON file: the key, quoted or not."""
HIDDEN_ELSEWHERE = re.compile(r"""^\s*(- )?visibility:\s*["']?hidden\b""", re.MULTILINE)
"""Anywhere else (code builds hidden tasks in tests): only YAML written out as text."""
DATA_SUFFIXES = (".yaml", ".yml", ".json", ".jsonl")
SKIP_SUFFIXES = (".pyc",)
SELF = Path(__file__).name


def offences_in(path: Path) -> list[str]:
    """What in one file looks like hidden task content."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    found: list[str] = []
    hidden = HIDDEN_IN_DATA if path.suffix in DATA_SUFFIXES else HIDDEN_ELSEWHERE
    for pattern, what in ((CANARY, "a canary"), (hidden, "a task marked hidden")):
        match = pattern.search(text)
        if match is not None:
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path}:{line}: contains {what}")
    return found


def main(paths: list[str]) -> int:
    """Return 1 if any file looks like hidden task content."""
    offences: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file() or path.suffix in SKIP_SUFFIXES or path.name == SELF:
            continue
        offences.extend(offences_in(path))
    for offence in offences:
        sys.stderr.write(f"hidden content must not live in the public repo: {offence}\n")
    return 1 if offences else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
