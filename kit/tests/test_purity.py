"""Determinism is enforced here, not by code review.

No module in the kit, the runner, the stress library or any world may read a
wall clock or draw unseeded randomness. A file that genuinely needs the
underlying primitive marks the line ``# purity-allow`` and explains why.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

SOURCE_ROOTS = [
    REPO / "kit" / "src",
    REPO / "runner" / "src",
    REPO / "stress" / "src",
    REPO / "guard" / "src",
    REPO / "training" / "src",
    *sorted((REPO / "worlds").glob("*/src")),
]

BANNED_SUFFIXES = (
    "time.time",
    "time.monotonic",
    "time.perf_counter",
    "time.sleep",
    "datetime.now",
    "datetime.utcnow",
    "datetime.today",
    "date.today",
    "uuid.uuid1",
    "uuid.uuid4",
    "os.urandom",
    "random.random",
    "random.randint",
    "random.choice",
    "random.choices",
    "random.shuffle",
    "random.sample",
    "random.seed",
    "random.randbytes",
    "secrets.token_hex",
    "secrets.token_bytes",
    "secrets.choice",
)

ALLOW_MARKER = "# purity-allow"
ALLOW_FILE_MARKER = "# purity-allow-file"

# Only calls rooted in one of these names are banned, so `step.sim_time.time()`
# and `clock.now()` are not mistaken for `time.time()` and `datetime.now()`.
BANNED_ROOTS = {"time", "datetime", "date", "uuid", "os", "random", "secrets", "dt"}


def _root(node: ast.expr) -> str | None:
    """The leftmost name of a pure dotted path, or None if it is an expression."""
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def python_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        files.extend(sorted(root.rglob("*.py")))
    return files


def test_there_is_source_to_scan() -> None:
    assert python_files(), f"no python files under {SOURCE_ROOTS}"


@pytest.mark.parametrize("path", python_files(), ids=lambda path: str(path.relative_to(REPO)))
def test_no_wall_clock_and_no_unseeded_randomness(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    offences: list[str] = []
    if ALLOW_FILE_MARKER in source:
        pytest.skip(f"{path.name} is explicitly exempt: {ALLOW_FILE_MARKER}")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        dotted = ast.unparse(node)
        if not dotted.endswith(BANNED_SUFFIXES) or _root(node) not in BANNED_ROOTS:
            continue
        line = lines[node.lineno - 1]
        if ALLOW_MARKER in line or (node.lineno >= 2 and ALLOW_MARKER in lines[node.lineno - 2]):
            continue
        offences.append(f"{path.relative_to(REPO)}:{node.lineno} {dotted}")
    assert not offences, "use SimClock and SeededRNG instead of these calls:\n  " + "\n  ".join(
        offences
    )


def test_the_check_catches_a_violation(tmp_path: Path) -> None:
    sample = tmp_path / "bad.py"
    sample.write_text("import time\n\n\ndef f():\n    return time.time()\n")
    tree = ast.parse(sample.read_text())
    found = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and ast.unparse(node).endswith(BANNED_SUFFIXES)
        and _root(node) in BANNED_ROOTS
    ]
    assert found == ["time.time"]


def test_the_check_ignores_lookalikes(tmp_path: Path) -> None:
    sample = tmp_path / "fine.py"
    sample.write_text("def f(step, clock):\n    return step.sim_time.time(), clock.now()\n")
    tree = ast.parse(sample.read_text())
    flagged = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and ast.unparse(node).endswith(BANNED_SUFFIXES)
        and _root(node) in BANNED_ROOTS
    ]
    assert flagged == []
