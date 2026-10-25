"""The published packages: one name, one version, pinned to each other, and nothing extra inside.

The build test builds every distribution with ``uv build`` into ``tmp_path`` and reads the
archives. It needs ``uv`` and a git checkout, as CI has; without them it is skipped.
"""

import email
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from kit import __version__
from kit.openenv_adapter import INSTALL_HINT
from runner import DISTRIBUTION

ROOT = Path(__file__).parents[2]
PARTS = {
    "kit": "kit",
    "stress": "stress",
    "runner": "runner",
    "guard": "guard",
    "training": "training",
    "worlds/payments": "world-payments",
    "worlds/security": "world-security",
}
ROOT_SDIST = {"pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md"}
GENERATED = {"PKG-INFO", ".gitignore"}
"""Written by the build into every sdist's top level."""


def project(folder: str) -> dict:
    return tomllib.loads((ROOT / folder / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def names() -> dict[str, str]:
    """Each distribution's name, from its pyproject."""
    return {"": project(".")["name"], **{f: project(f)["name"] for f in PARTS}}


def test_every_distribution_carries_the_one_name_and_version() -> None:
    assert project(".")["name"] == DISTRIBUTION
    for folder, part in PARTS.items():
        assert project(folder)["name"] == f"{DISTRIBUTION}-{part}"
    for folder in ["", *PARTS]:
        assert project(folder or ".")["version"] == __version__
    assert f"'{DISTRIBUTION}[openenv]'" in INSTALL_HINT


def test_internal_dependencies_are_pinned_to_the_same_version() -> None:
    ours = set(names().values())
    for folder in [".", *PARTS]:
        meta = project(folder)
        requirements = list(meta.get("dependencies", []))
        for extra in meta.get("optional-dependencies", {}).values():
            requirements += extra
        for requirement in requirements:
            bare = requirement.split("==")[0].split("[")[0]
            if bare in ours:
                assert requirement.endswith(f"=={__version__}"), (folder, requirement)


def test_the_rename_script_reads_the_constant() -> None:
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/rename_distribution.py"), DISTRIBUTION],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"already named {DISTRIBUTION}"


def git_files() -> set[str]:
    """Files git tracks or would add; anything ignored is left out."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return {name for name in out.split("\0") if name}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    if shutil.which("uv") is None or not (ROOT / ".git").exists():
        pytest.skip("needs uv and a git checkout")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--all-packages", "--quiet", "--out-dir", str(out)],
        cwd=ROOT,
        check=True,
    )
    return out


def dist_file(built: Path, name: str, suffix: str) -> Path:
    stem = name.replace("-", "_")
    [found] = list(built.glob(f"{stem}-{__version__}{suffix}"))
    return found


def test_every_sdist_holds_only_files_git_would_ship(built) -> None:
    allowed = git_files()
    for folder, name in names().items():
        with tarfile.open(dist_file(built, name, ".tar.gz")) as archive:
            members = [m.name.split("/", 1)[1] for m in archive.getmembers() if m.isfile()]
        members = [member for member in members if member not in GENERATED]
        for member in members:
            repo_path = f"{folder}/{member}" if folder else member
            assert repo_path in allowed, f"{name} sdist ships {repo_path}, which git does not"
            assert not any(part.startswith(".") for part in Path(member).parts[:-1]), member
        if not folder:
            assert set(members) <= ROOT_SDIST, sorted(set(members) - ROOT_SDIST)


def test_every_wheel_holds_only_its_package_and_metadata(built) -> None:
    packages = {"kit": "kit/", "stress": "stress/", "runner": "runner/"}
    packages |= {"guard": "guard/", "training": "training/"}
    packages |= {"worlds/payments": "worlds/payments/", "worlds/security": "worlds/security/"}
    for folder, name in names().items():
        with zipfile.ZipFile(dist_file(built, name, "-py3-none-any.whl")) as wheel:
            members = wheel.namelist()
            metadata = email.message_from_bytes(
                wheel.read(f"{name.replace('-', '_')}-{__version__}.dist-info/METADATA")
            )
        info = f"{name.replace('-', '_')}-{__version__}.dist-info/"
        prefix = packages.get(folder)
        for member in members:
            assert member.startswith(info) or (prefix and member.startswith(prefix)), member
        for requirement in metadata.get_all("Requires-Dist") or []:
            if requirement.startswith(DISTRIBUTION):
                assert f"=={__version__}" in requirement, requirement


def test_the_world_wheels_carry_their_tasks_runs_and_schemas(built) -> None:
    for world in ("payments", "security"):
        name = f"{DISTRIBUTION}-world-{world}"
        with zipfile.ZipFile(dist_file(built, name, "-py3-none-any.whl")) as wheel:
            members = set(wheel.namelist())
        base = f"worlds/{world}/"
        tasks = [m for m in members if m.startswith(base + "tasks/") and m.endswith(".yaml")]
        runs = [m for m in members if m.startswith(base + "graders/runs/")]
        extra = [m for m in members if m.startswith(base + "agent_tasks/") and m.endswith(".yaml")]
        assert len(tasks) == 50
        # payments also ships payments-v0.1-agents (12 tasks, their runs beside the others)
        assert len(extra) == (12 if world == "payments" else 0)
        assert len(runs) == 50 + len(extra)
        for needed in ("tasks/schema.json", "tasks/hidden-v0.1.json", "tools/schemas.json"):
            assert base + needed in members
