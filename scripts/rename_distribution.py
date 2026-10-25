#!/usr/bin/env python3
"""Rename the published distributions: ``scripts/rename_distribution.py <new-name>``.

The umbrella package is published as ``runner.DISTRIBUTION`` and every other package as
``<DISTRIBUTION>-<part>`` (``-kit``, ``-runner``, ...). This replaces the current name with the
new one in every tracked text file that uses it (pyproject files, code, docs, workflows, the
Docker and OpenEnv files), except the history in ``docs/DECISIONS.md`` and ``CHANGELOG.md``.
Run ``uv lock`` afterwards. Import names and the ``sandbox`` command do not change.

Standard library only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSTANT = ROOT / "runner/src/runner/__init__.py"
KEEP = {"docs/DECISIONS.md", "CHANGELOG.md", "uv.lock"}
NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def current() -> str:
    """The name in ``runner.DISTRIBUTION``."""
    match = re.search(r'^DISTRIBUTION = "([^"]+)"', CONSTANT.read_text(encoding="utf-8"), re.M)
    if match is None:
        msg = f"no DISTRIBUTION constant in {CONSTANT}"
        raise SystemExit(msg)
    return match.group(1)


def tracked() -> list[Path]:
    """Every file git tracks or would add (untracked but not ignored)."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode()
    return [Path(name) for name in out.split("\0") if name]


def main(argv: list[str]) -> int:
    """Replace the current name with ``argv[0]``; print each file changed."""
    if len(argv) != 1 or not NAME.match(argv[0]):
        sys.stderr.write("usage: rename_distribution.py <new-name> (lowercase, dashes)\n")
        return 2
    old, new = current(), argv[0]
    if old == new:
        sys.stdout.write(f"already named {new}\n")
        return 0
    pattern = re.compile(rf"(?<![\w-]){re.escape(old)}(?![\w])")
    changed = 0
    for relative in tracked():
        if relative.as_posix() in KEEP:
            continue
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        updated = pattern.sub(new, text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed += 1
            sys.stdout.write(f"renamed in {relative}\n")
    sys.stdout.write(f"{old} -> {new} in {changed} file(s). Now run: uv lock\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
