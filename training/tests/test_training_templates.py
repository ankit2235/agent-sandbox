"""Task templates: their format, their variants, and that nothing else mistakes them for tasks."""

import json
from pathlib import Path

import pytest

from guard.actions import actions
from kit.tasks import load_tasks
from kit.world import worlds
from runner.cli import main as sandbox
from runner.hidden import generate
from runner.validate import task_files
from training.data import templates
from training.data.labels import walk

WORLDS = ("payments", "security")


def test_each_world_ships_ten_templates_named_after_public_tasks() -> None:
    for name in WORLDS:
        spec = worlds.get(name)
        found = templates.load_all(spec)
        assert len(found) == 10
        public = {task.id for task in spec.task_source().split(spec.version)}
        assert {template.base for template in found} <= public


@pytest.mark.parametrize("world", WORLDS)
def test_the_first_variant_of_every_template_behaves(world: str) -> None:
    spec = worlds.get(world)
    acts = actions.get(world)
    for template in templates.load_all(spec):
        [first, *_] = templates.variants(spec, template, seed=0)
        assert first.task.id == f"{template.base}.tpl-001"
        assert first.runs.task == first.task.id
        for name, run in first.runs.runs.items():
            assert walk(spec, first.task, run, acts).behaved, (first.task.id, name)


def test_variants_are_seeded_and_never_the_base_itself() -> None:
    spec = worlds.get("payments")
    [template] = [t for t in templates.load_all(spec) if t.base == "commerce-two-shops"]
    once = templates.variants(spec, template, seed=0)
    again = templates.variants(spec, template, seed=0)
    other = templates.variants(spec, template, seed=1)
    assert [v.task for v in once] == [v.task for v in again]
    assert [v.task.seed for v in once] != [v.task.seed for v in other]
    base = spec.task_source().get(template.base)
    for variant in once:
        assert variant.task.grader == base.grader
        assert variant.task.model_dump(exclude={"id", "seed"}) != base.model_dump(
            exclude={"id", "seed"}
        )


def test_replacements_happen_at_once_so_a_swap_works() -> None:
    swapped = templates.substitute({"a": "Asha pays Ravi"}, {"Asha": "Ravi", "Ravi": "Asha"})
    assert swapped == {"a": "Ravi pays Asha"}
    assert templates.substitute({"q": 'say "hi"'}, {'"hi"': '"bye"'}) == {"q": 'say "bye"'}


def _write(folder: Path, base: str, slots: dict) -> Path:
    path = folder / f"{base}.json"
    doc = {"format": "task-template-v1", "base": base, "summary": "s", "slots": slots}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_template_whose_text_is_not_in_the_base_task_is_refused(tmp_path: Path) -> None:
    spec = worlds.get("payments")
    path = _write(
        tmp_path, "commerce-two-shops", {"x": [{}, {"No such text anywhere": "replacement"}]}
    )
    with pytest.raises(templates.TemplateError, match="not in the base task"):
        templates.variants(spec, templates.load(path), seed=0)


def test_a_template_that_would_change_the_grader_is_refused(tmp_path: Path) -> None:
    spec = worlds.get("payments")
    path = _write(tmp_path, "commerce-two-shops", {"x": [{}, {"two_shops": "two_stores"}]})
    with pytest.raises(templates.TemplateError, match="grader"):
        templates.variants(spec, templates.load(path), seed=0)


@pytest.mark.parametrize(
    ("base", "slots", "message"),
    [
        ("elsewhere", {"x": [{}, {"a": "b"}]}, "named after its base"),
        ("commerce-two-shops", {"x": [{}]}, "at least two"),
        ("commerce-two-shops", {"x": [{}, {"a": "b"}], "y": [{}, {"a": "c"}]}, "replaced by"),
        ("commerce-two-shops", {"x": [{}, {"": "b"}]}, "empty"),
    ],
)
def test_malformed_template_files_are_refused(
    tmp_path: Path, base: str, slots: dict, message: str
) -> None:
    path = _write(tmp_path, base, slots)
    if message == "named after its base":
        path = path.rename(tmp_path / "commerce-two-shops.json")
    with pytest.raises(templates.TemplateError, match=message):
        templates.load(path)


def test_a_template_for_a_task_that_is_not_public_is_refused(tmp_path: Path) -> None:
    spec = worlds.get("payments")
    path = _write(tmp_path, "no-such-task", {"x": [{}, {"a": "b"}]})
    with pytest.raises(templates.TemplateError, match="not a public"):
        templates.variants(spec, templates.load(path), seed=0)


# ------------------------------------------------- nothing else reads them
@pytest.mark.parametrize("world", WORLDS)
def test_the_task_loaders_ignore_templates(world: str) -> None:
    spec = worlds.get(world)
    assert spec.tasks_dir is not None
    assert (spec.tasks_dir / "templates").is_dir()
    loaded = load_tasks(spec.tasks_dir)
    assert len(loaded) == 50
    assert not any(".tpl-" in task.id for task in loaded)
    assert len(spec.task_source().split(spec.version)) == 50
    assert not any(path.parent.name == "templates" for path in task_files(spec.tasks_dir))


def test_sandbox_tasks_validate_ignores_templates(capsys: pytest.CaptureFixture[str]) -> None:
    assert sandbox(["tasks", "validate"]) == 0
    out = capsys.readouterr().out
    assert "templates" not in out
    assert "FAIL" not in out


def test_the_hidden_set_generator_ignores_templates(tmp_path: Path) -> None:
    spec = worlds.get("payments")
    made = generate(spec, count=2, seed=12345, root=tmp_path)
    public = {task.id for task in spec.task_source().split(spec.version)}
    record = json.loads((tmp_path / "payments" / spec.version / "set.json").read_text())
    assert {entry["base"] for entry in record["tasks"].values()} <= public
    assert not any(".tpl-" in task.id for task in made.tasks)
