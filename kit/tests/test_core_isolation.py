"""The core must stay domain-neutral.

``kit/``, ``runner/``, ``stress/`` (and later ``guard/``, ``trust/``, the
leaderboard) may not import a world. A world may not import another world.
CI runs this as its own job, so a rule break fails fast and visibly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE_ROOTS = [REPO / "kit" / "src", REPO / "runner" / "src", REPO / "stress" / "src"]
WORLD_ROOTS = sorted((REPO / "worlds").glob("*/src"))


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def core_files() -> list[Path]:
    files: list[Path] = []
    for root in CORE_ROOTS:
        files.extend(sorted(root.rglob("*.py")))
    return files


@pytest.mark.parametrize("path", core_files(), ids=lambda path: str(path.relative_to(REPO)))
def test_core_never_imports_a_world(path: Path) -> None:
    offences = sorted(
        module
        for module in imported_modules(path)
        if module == "worlds" or module.startswith("worlds.")
    )
    assert not offences, (
        f"{path.relative_to(REPO)} imports {offences}; worlds register themselves through the "
        "agent_sandbox.worlds entry point"
    )


def test_a_world_never_imports_another_world() -> None:
    offences: list[str] = []
    for root in WORLD_ROOTS:
        world_name = root.parent.name
        for path in sorted(root.rglob("*.py")):
            for module in imported_modules(path):
                if module.startswith("worlds.") and not module.startswith(f"worlds.{world_name}"):
                    offences.append(f"{path.relative_to(REPO)} -> {module}")
    assert not offences, "worlds must not depend on each other:\n  " + "\n  ".join(offences)


def test_the_kit_exposes_no_world_names() -> None:
    import kit

    exported = " ".join(kit.__all__)
    for world in ("payments", "security", "drug_safety"):
        assert world not in exported
