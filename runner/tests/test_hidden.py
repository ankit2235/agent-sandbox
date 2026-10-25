"""The hidden set: generated into a private directory, hashed, checked, scanned for canaries.

Every hidden task here is generated at test time into ``tmp_path``; the public repo
never holds one (CONTRIBUTING rule 6). The published hash list is pointed at a file
under ``tmp_path`` too.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from runner_testkit import world_config

from kit.tasks import load_task
from kit.world import worlds
from runner import hidden
from runner.cli import main
from runner.runs import execute, load_records, resolve_task_set

REPO = Path(__file__).resolve().parents[2]
SECRET = 424242
"""A test seed. The real set's seed is chosen by the repo owner and kept private."""


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """A payments hidden set of four tasks, generated once for the module."""
    root = tmp_path_factory.mktemp("private") / "agent-sandbox-hidden"
    result = hidden.generate(worlds.get("payments"), 4, SECRET, root)
    return root, result


@pytest.fixture
def private(made, tmp_path, monkeypatch):
    """HIDDEN_TASKS_DIR at the generated set, its hashes published under ``tmp_path``."""
    root, _ = made
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(root))
    published = tmp_path / "hidden-v0.1.json"
    original = hidden.hash_file
    monkeypatch.setattr(
        hidden, "hash_file", lambda s: published if s.name == "payments" else original(s)
    )
    hidden.write_hashes(worlds.get("payments"), root)
    return root, published


def task_files(root: Path) -> list[Path]:
    return sorted((root / "payments" / "v0.1").rglob("*.yaml"))


# ------------------------------------------------------------------ canaries
def test_a_canary_is_seeded_well_formed_and_per_task() -> None:
    first = hidden.canary(SECRET, "hidden-001")
    assert first == hidden.canary(SECRET, "hidden-001")
    assert hidden.CANARY_RE.fullmatch(first)
    assert first != hidden.canary(SECRET, "hidden-002") != hidden.canary(SECRET + 1, "hidden-001")


# ---------------------------------------------------------------- generation
def test_generate_writes_hidden_tasks_runs_and_the_private_seed(made) -> None:
    root, result = made
    assert [t.id for t in result.tasks] == [f"hidden-00{n}" for n in range(1, 5)]
    for task in result.tasks:
        assert task.visibility == "hidden" and task.stress
        assert len(hidden.canaries_of(task)) == 1
        assert (root / "payments" / "runs-v0.1" / f"{task.id}.yaml").exists()
        assert (root / "payments" / "v0.1" / task.category / f"{task.id}.yaml").exists()
    meta = json.loads((root / "payments" / "v0.1" / "set.json").read_text())
    assert meta["seed"] == SECRET and meta["count"] == 4
    assert {m["base"] for m in meta["tasks"].values()} <= {
        t.id for t in worlds.get("payments").task_source().tasks
    }


def test_generation_is_deterministic(made, tmp_path) -> None:
    _, result = made
    again = hidden.generate(worlds.get("payments"), 4, SECRET, tmp_path / "again")
    assert again.tasks == result.tasks


def test_generate_refuses_an_existing_set_and_the_public_repo(made, tmp_path) -> None:
    root, _ = made
    spec = worlds.get("payments")
    with pytest.raises(hidden.HiddenSetError, match="already holds a hidden set"):
        hidden.generate(spec, 1, SECRET, root)
    with pytest.raises(hidden.HiddenSetError, match="inside the public repository"):
        hidden.generate(spec, 1, SECRET, REPO / "runs" / "never-here")
    assert not (REPO / "runs" / "never-here").exists()
    with pytest.raises(hidden.HiddenSetError, match="at least 1"):
        hidden.generate(spec, 0, SECRET, tmp_path)


def test_more_tasks_than_can_be_made_is_an_error(tmp_path, monkeypatch) -> None:
    real = hidden.applicable
    two = {"rails-pay-bill-exact", "commerce-two-shops"}
    monkeypatch.setattr(
        hidden,
        "applicable",
        lambda base, seed, runs_dir=None: real(base, seed, runs_dir) if base.id in two else {},
    )
    with pytest.raises(hidden.HiddenSetError, match="could be made"):
        hidden.generate(worlds.get("payments"), 20, SECRET, tmp_path / "too-many")
    assert not (tmp_path / "too-many").exists(), "nothing is written when the set falls short"


def test_a_security_set_is_made_the_same_way(tmp_path) -> None:
    result = hidden.generate(worlds.get("security"), 2, SECRET, tmp_path / "sec")
    assert len(result.tasks) == 2
    assert all(t.world == "security" and t.visibility == "hidden" for t in result.tasks)


# ------------------------------------------------------------ hashes and load
def test_the_published_list_holds_hashes_only(private) -> None:
    _, published = private
    data = json.loads(published.read_text())
    assert data["count"] == 4 and data["set"] == "payments-v0.1-hidden"
    text = published.read_text()
    assert "hidden-00" not in text and "sandbox-canary" not in text
    assert data["set_hash"] == hidden.set_hash(data["tasks"])


def test_the_hidden_set_loads_only_when_it_matches_its_hashes(private) -> None:
    root, _ = private
    spec = worlds.get("payments")
    assert len(resolve_task_set(spec, "payments-v0.1-hidden")) == 4
    path = task_files(root)[0]
    original = path.read_text()
    raw = yaml.safe_load(original)
    raw["max_steps"] += 1
    path.write_text(yaml.safe_dump(raw))
    try:
        with pytest.raises(hidden.HiddenSetError, match="does not match its published hashes"):
            resolve_task_set(spec, "payments-v0.1-hidden")
    finally:
        path.write_text(original)


def test_a_reformatted_file_keeps_its_hash(private) -> None:
    root, _ = private
    path = task_files(root)[0]
    original = path.read_text()
    path.write_text(yaml.safe_dump(yaml.safe_load(original), sort_keys=False, width=60))
    try:
        assert len(hidden.load_hidden(worlds.get("payments"))) == 4
    finally:
        path.write_text(original)


def test_a_tampered_hash_list_or_none_at_all_is_refused(private, monkeypatch) -> None:
    _, published = private
    spec = worlds.get("payments")
    data = json.loads(published.read_text())
    published.write_text(json.dumps({**data, "count": 5}))
    with pytest.raises(hidden.HiddenSetError, match="inconsistent"):
        hidden.load_hidden(spec)
    published.unlink()
    with pytest.raises(hidden.HiddenSetError, match="no published hash list"):
        hidden.load_hidden(spec)


def test_hidden_tasks_need_the_private_repo(monkeypatch, tmp_path) -> None:
    spec = worlds.get("payments")
    monkeypatch.delenv("HIDDEN_TASKS_DIR", raising=False)
    with pytest.raises(hidden.HiddenSetError, match="HIDDEN_TASKS_DIR is not set"):
        resolve_task_set(spec, "payments-v0.1-hidden")
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_task_set(spec, "payments-v0.1-hidden")
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(tmp_path))
    with pytest.raises(hidden.HiddenSetError, match="no hidden set"):
        resolve_task_set(spec, "payments-v0.1-hidden")


def test_hidden_tasks_never_load_as_a_plain_directory(made) -> None:
    root, _ = made
    with pytest.raises(ValueError, match=r"load only as payments-v0\.1-hidden"):
        resolve_task_set(worlds.get("payments"), str(root / "payments" / "v0.1"))


# ------------------------------------------------------------------ canaries
def bad_set(made, tmp_path, change) -> list:
    root, _ = made
    tasks = [load_task(p) for p in task_files(root)]
    return change(tasks)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda ts: [ts[0].model_copy(update={"tags": ["x"]}), *ts[1:]], "exactly one canary"),
        (
            lambda ts: [ts[0].model_copy(update={"tags": [*ts[0].tags, ts[1].tags[-1]]})],
            "exactly one canary",
        ),
        (
            lambda ts: [ts[0].model_copy(update={"tags": ["sandbox-canary:not-a-uuid"]})],
            "exactly one canary",
        ),
        (lambda ts: [ts[0], ts[1].model_copy(update={"tags": ts[0].tags})], "repeats the canary"),
        (lambda ts: [ts[0].model_copy(update={"visibility": "public"})], "not marked hidden"),
    ],
)
def test_a_bad_canary_is_refused(made, tmp_path, change, message) -> None:
    with pytest.raises(hidden.HiddenSetError, match=message):
        hidden.check_canaries(bad_set(made, tmp_path, change))


# -------------------------------------------------------------------- check
def test_check_plays_every_run(private) -> None:
    report = hidden.check_set(worlds.get("payments"))
    assert report.tasks == 4 and report.runs >= 8 and not report.problems


def test_check_reports_a_trap_that_does_not_bite(private) -> None:
    root, _ = private
    path = sorted((root / "payments" / "runs-v0.1").glob("*.yaml"))[0]
    original = path.read_text()
    raw = yaml.safe_load(original)
    wrong = next(name for name in raw["runs"] if name != "golden")
    raw["runs"][wrong]["fails_with"] = "payments.false_claim"
    path.write_text(yaml.safe_dump(raw))
    try:
        problems = hidden.check_set(worlds.get("payments")).problems
    finally:
        path.write_text(original)
    assert len(problems) == 1 and "not payments.false_claim" in problems[0]


def test_the_fake_models_replay_hidden_runs_through_the_runner(private, tmp_path) -> None:
    for model, passed in (("fake:golden", True), ("fake:wrong", False)):
        out = tmp_path / model.replace(":", "-")
        execute(world_config("payments", model=model, tasks="payments-v0.1-hidden"), out)
        records = load_records(out)
        assert len(records) == 4
        assert all(r.visibility == "hidden" and r.passed is passed for r in records)


# --------------------------------------------------------------- scan, leaks
def test_scan_finds_canaries_in_model_output(private, tmp_path) -> None:
    root, _ = private
    spec = worlds.get("payments")
    task = hidden.load_hidden(spec)[0]
    (leaked,) = hidden.canaries_of(task)
    output = tmp_path / "outputs" / "reply.txt"
    output.parent.mkdir()
    output.write_text(f"line one\nthe model wrote {leaked} here\n")
    rotated = hidden.canary(1, "retired-001")
    (tmp_path / "outputs" / "old.jsonl").write_text(json.dumps({"text": rotated}) + "\n")
    (tmp_path / "outputs" / "blob.bin").write_bytes(b"\xff\xfe\x00")
    known = hidden.known_canaries([spec], root)
    found = hidden.scan([tmp_path / "outputs"], known)
    assert [(f.path.name, f.line, f.hidden_set) for f in found] == [
        ("old.jsonl", 1, None),
        ("reply.txt", 2, "payments-v0.1-hidden"),
    ]


def test_a_run_directory_holds_no_canary_unless_the_model_wrote_one(private, tmp_path) -> None:
    out = tmp_path / "run"
    execute(world_config("payments", model="fake:golden", tasks="payments-v0.1-hidden"), out)
    assert hidden.scan([out], {}) == []


def test_leaks_finds_a_hidden_task_copied_into_public_paths(private, tmp_path) -> None:
    root, _ = private
    public = tmp_path / "public-copy"
    public.mkdir()
    raw = yaml.safe_load(task_files(root)[0].read_text())
    (public / "innocent-name.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    (public / "not-a-task.yaml").write_text("just: data\n")
    specs = [worlds.get("payments"), worlds.get("security")]
    assert len(hidden.public_leaks(specs, [public])) == 1
    assert hidden.public_leaks(specs, [REPO / "worlds"]) == []


# ---------------------------------------------------------------------- CLI
def test_the_cli_runs_the_whole_cycle(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "private"
    published = tmp_path / "hidden-v0.1.json"
    original = hidden.hash_file
    monkeypatch.setattr(
        hidden, "hash_file", lambda s: published if s.name == "security" else original(s)
    )
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(root))
    root.mkdir()
    gen = ["hidden", "generate", "--world", "security", "--count", "2", "--seed", "9"]
    assert main(gen) == 0
    assert main(["hidden", "hashes", "--world", "security"]) == 0
    assert main(["hidden", "check", "--world", "security"]) == 0
    text = capsys.readouterr().out
    assert "2 hidden tasks written" in text and "0 problem(s)" in text
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing here\n")
    assert main(["hidden", "scan", str(clean)]) == 0
    task = hidden.load_hidden(worlds.get("security"))[0]
    clean.write_text(hidden.canaries_of(task)[0])
    assert main(["hidden", "scan", str(clean)]) == 1
    assert "from security-v0.1-hidden" in capsys.readouterr().out
    assert main(["hidden", "leaks", str(REPO / "worlds")]) == 0


# ------------------------------------------------------ the public-repo check
SCRIPT = REPO / "scripts" / "check_no_hidden.py"


def check(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, paths)], capture_output=True, text=True, check=False
    )


def test_the_leak_check_catches_canaries_and_hidden_tasks(tmp_path) -> None:
    files = {
        "task.yaml": "id: x\nvisibility: hidden\n",
        "task.json": json.dumps({"id": "x", "visibility": "hidden"}),
        "notes.md": f"copied from a model reply: {hidden.canary(3, 'x').upper()}\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    result = check(*(tmp_path / name for name in files))
    assert result.returncode == 1
    assert result.stderr.count("must not live in the public repo") == 3


def test_the_leak_check_allows_code_that_builds_hidden_tasks(tmp_path) -> None:
    code = tmp_path / "builder.py"
    code.write_text('payload = {"visibility": "hidden"}\nPREFIX = "sandbox-canary:"\n')
    public = tmp_path / "task.yaml"
    public.write_text("id: x\nvisibility: public\n")
    assert check(code, public).returncode == 0


def test_no_tracked_file_holds_hidden_content() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    result = check(*(REPO / path for path in tracked))
    assert result.returncode == 0, result.stderr
