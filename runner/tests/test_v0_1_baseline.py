"""Single-agent runs are unchanged by multi-agent support (Phase 9, item 1).

``fixtures/v0_1_baseline`` was made with the code before that item (Phase 9 item 6, d5e74f6):

- ``fingerprints.json``: for every golden and wrong run of every public payments and security
  task, the SHA-256 of its trace (every step as JSON), of its grade, and of each service's
  final snapshot;
- four run directories (``fake:golden`` and ``fake:wrong`` on six seeded tasks of each world,
  ``spans.jsonl`` left out: it holds wall-clock timings).

Today's code must reproduce every fingerprint, verify every directory with the same grades,
and write the same directories again byte for byte.
"""

import hashlib
import json
from pathlib import Path

import pytest

from kit.scripted import load_runs, play
from kit.world import worlds
from runner.runs import MANIFEST, RUNS_DIR, execute, load_records, read_manifest
from runner.verify import verify

BASELINE = Path(__file__).parent / "fixtures" / "v0_1_baseline"
FINGERPRINTS = json.loads((BASELINE / "fingerprints.json").read_text(encoding="utf-8"))
DIRECTORIES = ["payments-golden", "payments-wrong", "security-golden", "security-wrong"]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def runs() -> list[tuple[str, str, str]]:
    found = []
    for world in ("payments", "security"):
        spec = worlds.get(world)
        for task in spec.task_source().split(spec.version):
            for name in load_runs(task.id, spec.runs_dir).runs:
                found.append((world, task.id, name))
    return found


def test_the_baseline_covers_every_scripted_run_of_both_worlds() -> None:
    assert sorted(f"{w}/{t}/{n}" for w, t, n in runs()) == sorted(FINGERPRINTS)
    assert len(FINGERPRINTS) == 270


@pytest.mark.parametrize(("world", "task_id", "name"), runs(), ids="/".join)
def test_every_scripted_run_reproduces_its_baseline_bytes(
    world: str, task_id: str, name: str
) -> None:
    spec = worlds.get(world)
    task = spec.task_source().get(task_id)
    result = play(spec, task, load_runs(task_id, spec.runs_dir).runs[name])
    assert result.grade is not None
    trace = "".join(step.model_dump_json() + "\n" for step in result.trace.steps)
    expected = FINGERPRINTS[f"{world}/{task_id}/{name}"]
    assert digest(trace.encode()) == expected["trace"]
    assert digest(result.grade.model_dump_json().encode()) == expected["grade"]
    states = {key: digest(blob) for key, blob in sorted(result.final_state.items())}
    assert states == expected["state"]


def files(out: Path) -> dict[str, bytes]:
    paths = [out / MANIFEST, *sorted((out / RUNS_DIR).rglob("*"))]
    return {str(path.relative_to(out)): path.read_bytes() for path in paths if path.is_file()}


@pytest.mark.parametrize("name", DIRECTORIES)
def test_a_baseline_run_directory_verifies_and_is_written_again_byte_for_byte(
    name: str, tmp_path: Path
) -> None:
    old = BASELINE / name
    report = verify(old)
    assert report.ok and len(report.verdicts) == 6
    manifest = read_manifest(old)
    assert manifest is not None
    again = tmp_path / name
    execute(manifest.config, again)
    assert files(again) == files(old)
    assert [r.grade for r in load_records(again)] == [r.grade for r in load_records(old)]
