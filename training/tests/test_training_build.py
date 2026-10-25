"""The dataset build: determinism, de-duplication, balance, splits by family, the manifest."""

import json
from pathlib import Path

import pytest
from training_testkit import SMALL

from guard.decision import ActionCall, DecisionRequest
from training.data.build import BuildConfig, _balance, _dedupe, build, read_manifest, read_split
from training.data.example import SPLITS, ActionExample, Label, Source


def test_the_small_build_has_every_split_and_a_full_manifest(small_build: Path) -> None:
    manifest = read_manifest(small_build)
    assert manifest["name"] == SMALL.name
    assert manifest["sources"]["runs_not_as_declared"] == 0
    assert manifest["sources"]["golden_attempts_not_allowed"] == 0
    assert manifest["decontamination"] == {"done": False}
    for split in SPLITS:
        rows = read_split(small_build, split)
        assert rows, split
        assert all(row.split == split for row in rows)
        assert manifest["splits"][split]["rows"] == len(rows)
    assert set(manifest["files"]) == {f"{split}.jsonl" for split in SPLITS}


def test_no_family_is_in_two_splits(small_build: Path) -> None:
    seen: dict[str, str] = {}
    for split in SPLITS:
        for row in read_split(small_build, split):
            assert seen.setdefault(row.family, split) == split


def test_rows_carry_stress_and_template_sources(small_build: Path) -> None:
    rows = [row for split in SPLITS for row in read_split(small_build, split)]
    assert any(row.stressed for row in rows)
    assert any(row.source.template for row in rows)
    assert {row.source.run_kind for row in rows} == {"golden", "wrong"}
    assert all(row.label.decision == "allow" for row in rows if row.source.run_kind == "golden")


def test_the_build_is_the_same_whatever_the_number_of_workers(tmp_path: Path) -> None:
    config = BuildConfig(
        name="tiny", worlds=["payments"], templates=False, stress_seeds=[0], max_tasks=4
    )
    build(config, tmp_path / "one", workers=1)
    build(config, tmp_path / "two", workers=2)
    for split in SPLITS:
        one = (tmp_path / "one" / "tiny" / f"{split}.jsonl").read_bytes()
        two = (tmp_path / "two" / "tiny" / f"{split}.jsonl").read_bytes()
        assert one == two
    assert (
        read_manifest(tmp_path / "one" / "tiny")["files"]
        == read_manifest(tmp_path / "two" / "tiny")["files"]
    )


def _row(rid: str, decision: str, world: str = "payments", text: str = "pay") -> ActionExample:
    return ActionExample(
        id=rid,
        dataset="t",
        world=world,
        world_version="v0.1",
        family="f",
        category="rails",
        difficulty="easy",
        source=Source(
            task_id="t", task_hash="h", base_task="t", run="golden", run_kind="golden", step=0
        ),
        request=DecisionRequest(
            world=world,
            instruction=text,
            action=ActionCall(tool="pay"),
            sim_time="2026-01-01T09:00:00Z",
        ),
        label=Label(decision=decision),
    )


def test_exact_duplicates_go_and_conflicting_labels_go_entirely() -> None:
    rows = [_row("a", "allow"), _row("b", "allow"), _row("c", "block", text="other")]
    rows += [_row("d", "allow", text="torn"), _row("e", "block", text="torn")]
    kept, duplicates, conflicts = _dedupe(rows)
    assert [row.id for row in kept] == ["a", "c"]
    assert (duplicates, conflicts) == (1, 2)


def test_balance_caps_each_class_at_the_ratio_and_copes_with_a_missing_class() -> None:
    rows = [_row(f"a{i}", "allow") for i in range(10)] + [_row("b0", "block")]
    rows += [_row(f"s{i}", "allow", world="security") for i in range(3)]
    kept, caps = _balance(rows, seed=0, ratio=3.0)
    assert caps == {"payments": {"allow": 3, "block": 1}, "security": {"allow": 3}}
    assert len(kept) == 7


def test_a_config_is_found_by_name_or_refused() -> None:
    assert BuildConfig.load("ci").name == "actions-v0.1-ci"
    assert BuildConfig.load("full").max_modifiers == 2
    with pytest.raises(FileNotFoundError, match="packaged ones"):
        BuildConfig.load("nope")


def test_every_row_is_valid_json_with_sorted_keys(small_build: Path) -> None:
    line = (small_build / "train.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert json.dumps(json.loads(line), sort_keys=True) == line
