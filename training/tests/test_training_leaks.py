"""The leakage checks: CI's on a built dataset, and the local one against a hidden set.

The hidden sets here are made in ``tmp_path`` (a public task copied and marked hidden, or
``runner.hidden.generate``); the real private repository is never read by a test.
"""

import json
from pathlib import Path

import pytest
import yaml

import training.data.hidden as hidden_module
from kit.scripted import load_runs
from kit.world import worlds
from runner.hidden import canary, generate, hash_file, published
from training.cli import main as train_cli
from training.data.build import BuildConfig, build, read_manifest
from training.data.hidden import HiddenRowsError, hidden_rows
from training.data.leaks import check_dataset, check_hidden

BASE = "commerce-ambiguous-size-ask"


def _rewrite(folder: Path, name: str, edit) -> None:
    path = folder / f"{name}.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows = edit(rows)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def _rehash(folder: Path) -> None:
    import hashlib

    manifest = read_manifest(folder)
    manifest["files"] = {
        name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in manifest["files"]
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _all(folder: Path) -> list[dict]:
    return [
        json.loads(line)
        for split in ("train", "validation", "test")
        for line in (folder / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_a_clean_build_passes(small_build: Path) -> None:
    assert check_dataset(small_build) == []


def test_an_edited_file_no_longer_matches_its_hash(dataset: Path) -> None:
    with (dataset / "test.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert any("sha256" in problem for problem in check_dataset(dataset))


def test_a_canary_anywhere_is_found(dataset: Path) -> None:
    def plant(rows: list) -> list:
        rows[0]["request"]["instruction"] += " " + canary(7, "x")
        return rows

    _rewrite(dataset, "train", plant)
    _rehash(dataset)
    assert any("canary" in problem for problem in check_dataset(dataset))


def test_a_row_from_a_hidden_task_is_found_by_id_and_by_hash(dataset: Path) -> None:
    hashes = published(worlds.get("payments"))["tasks"]

    def taint(rows: list) -> list:
        rows[0]["source"]["task_id"] = "hidden-001"
        rows[1]["source"]["task_hash"] = hashes[0]
        return rows

    _rewrite(dataset, "train", taint)
    _rehash(dataset)
    problems = check_dataset(dataset)
    assert any("hidden-set id" in problem for problem in problems)
    assert any("published hidden set" in problem for problem in problems)


def test_a_family_in_two_splits_and_a_copied_row_are_found(dataset: Path) -> None:
    moved: dict = {}

    def take(rows: list) -> list:
        moved.update(rows[0])
        return rows

    _rewrite(dataset, "train", take)

    def add(rows: list) -> list:
        copy = dict(moved)
        copy["id"] = "copied0000000000000000"
        copy["split"] = "test"
        return [*rows, copy]

    _rewrite(dataset, "test", add)
    _rehash(dataset)
    problems = check_dataset(dataset)
    assert any(f"family {moved['family']} is in" in problem for problem in problems)
    assert any("near-duplicates" in problem for problem in problems)


# ---------------------------------------------------------------- hidden set
@pytest.fixture
def fake_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A one-task payments hidden set in tmp_path: a public task's copy, marked hidden.

    Its rows are the public task's rows exactly, so a build of that task must lose them to
    decontamination. Loaded as if it were the private repository.
    """
    spec = worlds.get("payments")
    assert spec.runs_dir is not None
    base = spec.task_source().get(BASE)
    task = base.model_copy(
        update={"id": "hidden-001", "visibility": "hidden", "tags": [canary(1, "hidden-001")]}
    )
    root = tmp_path / "private"
    folder = root / "payments" / spec.version
    (folder / task.category).mkdir(parents=True)
    (folder / task.category / "hidden-001.yaml").write_text(
        yaml.safe_dump(task.model_dump(mode="json")), encoding="utf-8"
    )
    (folder / "set.json").write_text(
        json.dumps({"tasks": {"hidden-001": {"base": BASE}}}), encoding="utf-8"
    )
    runs = load_runs(BASE, spec.runs_dir).model_copy(update={"task": "hidden-001"})
    (root / "payments" / f"runs-{spec.version}").mkdir(parents=True)
    (root / "payments" / f"runs-{spec.version}" / "hidden-001.yaml").write_text(
        yaml.safe_dump(runs.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )
    monkeypatch.setattr(hidden_module, "load_hidden", lambda _spec: [task])
    monkeypatch.setattr(hidden_module, "private_root", lambda: root)
    return root


def test_hidden_rows_are_built_in_memory_with_their_family(fake_hidden: Path) -> None:
    found = hidden_rows("payments")
    assert found.tasks == 1
    assert found.rows
    assert {row.family for row in found.rows} == {BASE}
    assert all(row.dataset.endswith("-hidden") for row in found.rows)


def test_the_hidden_check_counts_rows_of_a_hidden_task_and_names_none(
    dataset: Path, fake_hidden: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = read_manifest(dataset)
    manifest["config"]["worlds"] = ["payments"]
    (dataset / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert any(row["source"]["task_id"] == BASE for row in _all(dataset))
    assert check_hidden(dataset).near_duplicates >= 1
    assert train_cli(["dataset", "hidden-check", str(dataset)]) == 1
    out = capsys.readouterr().out
    assert "hidden-0" not in out
    assert BASE not in out
    assert "near-duplicates" in out


def test_decontamination_drops_rows_near_the_hidden_set(tmp_path: Path, fake_hidden: Path) -> None:
    config = BuildConfig(
        name="decon", worlds=["payments"], templates=False, max_modifiers=0, max_tasks=4
    )
    manifest = build(config, tmp_path / "out", decontaminate=True)
    counts = manifest["decontamination"]["worlds"]["payments"]
    assert counts["hidden_tasks"] == 1
    assert counts["rows_dropped"] >= 1
    assert not any(row["source"]["task_id"] == BASE for row in _all(tmp_path / "out" / "decon"))
    assert check_hidden(tmp_path / "out" / "decon").near_duplicates == 0
    assert "hidden-0" not in (tmp_path / "out" / "decon" / "manifest.json").read_text()


def test_without_hidden_tasks_dir_the_error_names_no_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HIDDEN_TASKS_DIR", raising=False)
    with pytest.raises(HiddenRowsError, match="sandbox hidden check") as caught:
        hidden_rows("payments")
    assert "hidden-0" not in str(caught.value)


def test_a_hidden_set_that_fails_its_checks_names_no_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = worlds.get("payments")
    root = tmp_path / "private"
    generate(spec, count=2, seed=99, root=root)
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(root))
    assert hash_file(spec).exists()
    with pytest.raises(HiddenRowsError) as caught:
        hidden_rows("payments")
    assert "hidden-0" not in str(caught.value)
