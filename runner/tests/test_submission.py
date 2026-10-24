"""``sandbox submission check`` and ``spot-check``, on hello_world's whole public set (two tasks).

A submission is a run directory; every way of tampering with one, or of submitting
something that is not a whole result, must be refused with a reason.
"""

import hashlib
import json
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest
from runner_testkit import HELLO_SET, by_policy, hello_result

from kit.examples.hello_world.scripted import golden_run, wrong_run_overshoots
from kit.world import worlds
from runner import hidden
from runner.cli import main
from runner.runs import MANIFEST, RESULT_FILE, RUNS_DIR, TRACE_FILE, RunConfig, execute
from runner.submission import SubmissionError, check, download, fetch, spot_check


@pytest.fixture
def submitted(tmp_path) -> Path:
    return hello_result(tmp_path / "submitted", runs=2)


def first_run(out: Path) -> Path:
    return sorted((out / RUNS_DIR).iterdir())[0]


def test_a_whole_reproducible_result_passes(submitted) -> None:
    checked = check(submitted)
    assert checked.ok, checked.problems
    assert checked.model == "test-model-a"
    assert checked.runs_verified == 4


def test_not_a_run_directory_or_a_broken_manifest_is_refused(tmp_path, submitted) -> None:
    assert "no manifest.json" in check(tmp_path / "empty").problems[0]
    (submitted / MANIFEST).write_text('{"format": "sandbox-run-v1"}')
    assert "not a sandbox-run-v1 directory" in check(submitted).problems[0]


def test_a_sample_a_fake_model_and_too_few_runs_are_refused(tmp_path) -> None:
    sampled = hello_result(tmp_path / "sampled", sample=1)
    assert any("sample of 1" in p for p in check(sampled).problems)
    fake = tmp_path / "fake"
    config = RunConfig(
        world="hello_world", tasks=HELLO_SET, model="fake:idle", model_name="x", provider="fake"
    )
    execute(config, fake)
    assert any("fake model" in p for p in check(fake).problems)
    assert check(fake, allow_fake=True).ok
    assert any("at least 3 needed" in p for p in check(fake, allow_fake=True, min_runs=3).problems)


def test_a_task_directory_or_another_set_is_not_a_result(tmp_path, hello_dir) -> None:
    out = tmp_path / "dir-run"
    config = RunConfig(
        world="hello_world", tasks=str(hello_dir), model="t/m", model_name="m", provider="t"
    )
    execute(config, out, factory=by_policy(golden_run))
    assert "a result must run the whole hello_world-v0.1" in check(out).problems[0]


def test_a_missing_or_errored_run_is_refused(submitted) -> None:
    shutil.rmtree(first_run(submitted))
    assert any("1 run(s) missing" in p for p in check(submitted).problems)


def test_an_errored_record_is_refused(submitted) -> None:
    target = first_run(submitted) / RESULT_FILE
    record = json.loads(target.read_text())
    record.update(status="error", error="provider kept failing", grade=None)
    target.write_text(json.dumps(record))
    assert any("1 run(s) errored" in p for p in check(submitted).problems)


def test_a_hidden_record_is_never_a_submission(submitted) -> None:
    target = first_run(submitted) / RESULT_FILE
    record = json.loads(target.read_text())
    record["visibility"] = "hidden"
    target.write_text(json.dumps(record))
    assert any("hidden tasks" in p for p in check(submitted).problems)


def test_a_canary_anywhere_in_the_files_is_refused(submitted) -> None:
    trace = first_run(submitted) / TRACE_FILE
    line = json.dumps({"note": f"quoting {hidden.canary(3, 'hidden-001')}"})
    trace.write_text(trace.read_text() + line + "\n")
    assert any("canary" in p for p in check(submitted).problems)


def test_a_tampered_grade_or_response_is_caught_by_the_replay(submitted, tmp_path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(submitted, copy)
    target = first_run(submitted) / RESULT_FILE
    record = json.loads(target.read_text())
    record["grade"]["passed"] = not record["grade"]["passed"]
    target.write_text(json.dumps(record))
    assert any("grade differs" in p for p in check(submitted).problems)

    trace = first_run(copy) / TRACE_FILE
    lines = trace.read_text().splitlines()
    for number, text in enumerate(lines):
        step = json.loads(text)
        if step.get("kind") == "tool_call" and isinstance(step.get("response"), dict):
            step["response"]["count"] = 99
            lines[number] = json.dumps(step)
            break
    trace.write_text("\n".join(lines) + "\n")
    assert any("world steps differ" in p for p in check(copy).problems)


def test_a_missing_trace_is_reported_not_raised(submitted) -> None:
    (first_run(submitted) / TRACE_FILE).unlink()
    assert any("could not replay" in p for p in check(submitted).problems)


def test_a_run_with_another_prompt_is_refused(submitted) -> None:
    manifest = json.loads((submitted / MANIFEST).read_text())
    manifest["config"]["prompt"] = "default/v0"
    (submitted / MANIFEST).write_text(json.dumps(manifest))
    assert any("ran with prompt 'default/v0'" in p for p in check(submitted).problems)


def test_the_spot_check_agrees_with_an_honest_submission(submitted, tmp_path) -> None:
    result = spot_check(submitted, tmp_path / "rerun", sample=2, factory=by_policy(golden_run))
    assert result.ok
    assert result.tasks == sorted(t.id for t in worlds.get("hello_world").task_source().tasks)
    assert (result.submitted, result.rerun, result.drop) == (100.0, 100.0, 0.0)


def test_the_spot_check_catches_a_submission_better_than_its_model(submitted, tmp_path) -> None:
    """Golden actions written by hand, claimed for a model that overshoots: the rerun shows it."""
    result = spot_check(
        submitted, tmp_path / "rerun", sample=2, factory=by_policy(wrong_run_overshoots)
    )
    assert result.rerun == 0.0
    assert result.drop == 100.0
    assert not result.ok
    lenient = spot_check(
        submitted,
        tmp_path / "rerun-2",
        sample=2,
        tolerance=100,
        factory=by_policy(wrong_run_overshoots),
    )
    assert lenient.ok


def test_the_cli_checks_submissions(submitted, tmp_path, capsys) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    assert main(["submission", "check", str(submitted), str(broken)]) == 1
    out = capsys.readouterr().out
    assert f"ok   {submitted}: test-model-a, 4 runs reproduced" in out
    assert "1/2 submission(s) pass" in out
    assert main(["submission", "check", str(submitted)]) == 0


# ------------------------------------------------------------------- fetch
def archive_of(run_dir: Path, path: Path) -> str:
    with tarfile.open(path, "w:gz") as bundle:
        bundle.add(run_dir, arcname=f"payments-v0.1-runs/{run_dir.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def submission_file(path: Path, sha: str, **changes) -> Path:
    data = {
        "format": "submission-v1",
        "task_set": HELLO_SET,
        "model": "test/model-a",
        "model_name": "test/model-a",
        "provider": "test",
        "archive_url": "https://example.org/runs.tar.gz",
        "archive_sha256": sha,
    }
    data.update(changes)
    path.write_text(json.dumps(data))
    return path


def local(archive: Path):
    """A download that copies a local archive, whatever the URL."""
    return lambda url, destination, max_bytes: shutil.copy(archive, destination)


def test_fetch_unpacks_the_run_directory_the_file_names(submitted, tmp_path) -> None:
    archive = tmp_path / "runs.tar.gz"
    sha = archive_of(submitted, archive)
    file = submission_file(tmp_path / "sub.json", sha)
    run_dir = fetch(file, tmp_path / "fetched", get=local(archive))
    assert run_dir.name == submitted.name
    assert check(run_dir).ok


def test_fetch_refuses_a_wrong_hash_a_wrong_model_and_plain_http(submitted, tmp_path) -> None:
    archive = tmp_path / "runs.tar.gz"
    sha = archive_of(submitted, archive)
    with pytest.raises(SubmissionError, match="sha256"):
        fetch(submission_file(tmp_path / "a.json", "0" * 64), tmp_path / "a", get=local(archive))
    claimed = submission_file(tmp_path / "b.json", sha, model="test/a-better-model")
    with pytest.raises(SubmissionError, match="the run directory records"):
        fetch(claimed, tmp_path / "b", get=local(archive))
    plain = submission_file(tmp_path / "c.json", sha, archive_url="http://example.org/x.tar.gz")
    with pytest.raises(SubmissionError, match="https"):
        fetch(plain, tmp_path / "c", get=local(archive))
    with pytest.raises(SubmissionError, match="archive_sha256"):
        fetch(submission_file(tmp_path / "d.json", "nope"), tmp_path / "d", get=local(archive))


def test_fetch_refuses_an_archive_that_escapes_its_directory(tmp_path) -> None:
    archive = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("outside")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname="../../escaped.txt")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(SubmissionError, match="cannot be unpacked safely"):
        fetch(submission_file(tmp_path / "e.json", sha), tmp_path / "out", get=local(archive))
    assert not (tmp_path / "escaped.txt").exists()


def test_fetch_wants_exactly_one_run_directory(submitted, tmp_path) -> None:
    second = tmp_path / "second"
    shutil.copytree(submitted, second)
    archive = tmp_path / "two.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(submitted, arcname="a")
        bundle.add(second, arcname="b")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(SubmissionError, match="holds 2"):
        fetch(submission_file(tmp_path / "f.json", sha), tmp_path / "out", get=local(archive))


def test_download_stops_at_the_size_cap(tmp_path) -> None:
    body = b"x" * 5000
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    with pytest.raises(SubmissionError, match="larger than 1000 bytes"):
        download("https://example.org/a.tar.gz", tmp_path / "a", 1000, client=client)
    fine = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    download("https://example.org/a.tar.gz", tmp_path / "b", 10_000, client=fine)
    assert (tmp_path / "b").read_bytes() == body


def test_the_cli_fetches_and_prints_the_run_directory(
    submitted, tmp_path, capsys, monkeypatch
) -> None:
    from runner import submission

    archive = tmp_path / "runs.tar.gz"
    sha = archive_of(submitted, archive)
    file = submission_file(tmp_path / "sub.json", sha)
    monkeypatch.setattr(submission, "download", local(archive))
    assert main(["submission", "fetch", str(file), "--out", str(tmp_path / "o")]) == 0
    assert capsys.readouterr().out.strip().endswith(submitted.name)
    bad = submission_file(tmp_path / "bad.json", "0" * 64)
    assert main(["submission", "fetch", str(bad), "--out", str(tmp_path / "p")]) == 1
    assert "sha256" in capsys.readouterr().err
