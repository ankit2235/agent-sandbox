"""``--isolation container``, without Docker.

The worker (``sandbox episode serve``) is started here as a plain local process over the
same stdio transport the container uses, so the protocol, the trace merge, the output
checks and every failure path are tested on any machine. The ``docker`` command lines
are checked as data, and Docker itself is replaced by small scripts where a test needs
its answers. ``test_isolation_docker.py`` runs the real containers.
"""

import itertools
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from runner_testkit import call, scripted, turn, world_config

from kit import __version__
from kit.clock import EPOCH
from kit.trace import Trace
from kit.types import GradeResult
from kit.world import worlds
from runner import isolation, runs, submission
from runner.cli import main
from runner.isolation import (
    EPISODE_FILE,
    GUARD_REFUSAL,
    HIDDEN_MOUNT,
    OUT_MOUNT,
    RUN_MOUNT,
    SCRATCH_DIR,
    ContainerEpisode,
    EpisodeFailed,
    EpisodeOutput,
    Isolation,
    IsolationError,
    Limits,
    Mount,
    PendingMessage,
    docker_run,
    merged_trace,
    remove_tree,
    run_contained,
)
from runner.runs import RESULT_FILE, RUNS_DIR, RunConfig, execute, load_records, task_hash
from runner.verify import verify, verify_isolated


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(itertools.pairwise(argv))


class LocalWorker(ContainerEpisode):
    """The real worker and protocol, as a local process instead of a container."""

    def argv(self) -> list[str]:
        command = ["episode", "serve", "--task", str(self.task_file), "--out", str(self.scratch)]
        return [sys.executable, "-m", "runner.cli", *command]

    def exit_status(self) -> tuple[int, bool]:
        return 0, False


@pytest.fixture
def no_docker(monkeypatch):
    """No container is ever started or removed."""
    monkeypatch.setattr(isolation, "remove_container", lambda name: None)
    monkeypatch.setattr(runs, "preflight", lambda settings: None)


@pytest.fixture
def local(no_docker, monkeypatch):
    monkeypatch.setattr(runs, "ContainerEpisode", LocalWorker)


def run_files(out: Path) -> dict[str, bytes]:
    """Every file a run directory holds per run, by relative path."""
    return {
        str(path.relative_to(out)): path.read_bytes()
        for path in sorted((out / RUNS_DIR).rglob("*"))
        if path.is_file()
    }


def fake_docker(tmp_path: Path, body: str) -> Path:
    """A ``docker`` stand-in: logs its arguments to $DOCKER_TEST_LOG, then runs ``body``."""
    script = tmp_path / "docker"
    script.write_text(f'#!/bin/sh\necho "$@" >> "$DOCKER_TEST_LOG"\n{body}\n')
    script.chmod(0o755)
    return script


# ------------------------------------------------------------- command lines
def test_every_container_is_locked_down() -> None:
    argv = docker_run(
        Isolation(image="img:1"),
        name="ep",
        mounts=[Mount(Path("/host/ro"), "/in"), Mount(Path("/host/rw"), "/out", writable=True)],
        command=["episode", "serve"],
        user=(1000, 1000),
    )
    assert argv[:2] == ["docker", "run"]
    assert argv[-3:] == ["img:1", "episode", "serve"]
    expected = {
        ("--network", "none"),
        ("--ipc", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges=true"),
        ("--pull", "never"),
        ("--user", "1000:1000"),
        ("--cpus", "1"),
        ("--memory", "512m"),
        ("--memory-swap", "512m"),
        ("--pids-limit", "128"),
        ("--ulimit", "cpu=120"),
        ("--log-driver", "none"),
        ("--label", "agent-sandbox.episode=1"),
        ("--mount", "type=bind,src=/host/ro,dst=/in,readonly"),
        ("--mount", "type=bind,src=/host/rw,dst=/out"),
    }
    assert expected <= pairs(argv)
    assert "--read-only" in argv
    for loose in ("--tmpfs", "--privileged", "--cap-add", "-v", "--volume", "--network=host"):
        assert loose not in argv


def test_an_episode_container_can_write_only_its_scratch_directory(tmp_path, no_docker) -> None:
    task = worlds.get("payments").task_source().split("v0.1")[0]
    episode = ContainerEpisode(task, tmp_path / "run", Isolation(), task_hash=task_hash(task))
    episode.prepare()
    try:
        argv = episode.argv()
        mounts = [value for flag, value in pairs(argv) if flag == "--mount"]
        writable = [m for m in mounts if not m.endswith(",readonly")]
        assert writable == [f"type=bind,src={episode.scratch.resolve()},dst={OUT_MOUNT}"]
        assert len(mounts) == 2
        assert ("--interactive" in argv) and ("--rm" not in argv)
        assert stat.S_IMODE(episode.task_file.stat().st_mode) == 0o600
        assert json.loads(episode.task_file.read_text())["id"] == task.id
    finally:
        episode.cleanup()
    assert not episode.scratch.exists()
    assert not episode.task_file.parent.exists()


def test_a_mount_path_with_a_comma_is_refused() -> None:
    with pytest.raises(IsolationError, match="comma"):
        Mount(Path("/tmp/a,b"), "/in").flag()


def test_the_container_user_is_the_runner_unless_the_runner_is_root(monkeypatch) -> None:
    monkeypatch.setattr(isolation.os, "getuid", lambda: 1234)
    monkeypatch.setattr(isolation.os, "getgid", lambda: 99)
    assert isolation.container_user() == (1234, 99)
    monkeypatch.setattr(isolation.os, "getgid", lambda: 0)
    assert isolation.container_user() == (1234, 65534)
    monkeypatch.setattr(isolation.os, "getuid", lambda: 0)
    assert isolation.container_user() == (65534, 65534)


def test_a_root_runner_hands_only_the_scratch_directory_to_nobody(
    tmp_path, no_docker, monkeypatch
) -> None:
    chowned: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(isolation.os, "getuid", lambda: 0)
    monkeypatch.setattr(isolation.os, "getgid", lambda: 0)
    monkeypatch.setattr(isolation.os, "chown", lambda p, u, g: chowned.append((Path(p), u, g)))
    task = worlds.get("payments").task_source().split("v0.1")[0]
    episode = ContainerEpisode(task, tmp_path / "run", Isolation(), task_hash=task_hash(task))
    episode.prepare()
    try:
        assert chowned == [
            (episode.scratch, 65534, 65534),
            (episode.task_file, 65534, 65534),
        ]
        assert ("--user", "65534:65534") in pairs(episode.argv())
    finally:
        episode.cleanup()


# --------------------------------------------------- the same run, either way
def test_a_scripted_conversation_comes_out_byte_identical(hello_config, tmp_path, local) -> None:
    script = scripted(
        turn(call("get_count"), text="Let me look first."),
        turn(call("increment", "{not json"), call("increment"), text="Up by one."),
        turn(call("increment"), call("increment"), tokens=7),
        turn(text="Nearly there, no tool this time? No: one more."),
    )
    config = hello_config()
    execute(config, tmp_path / "in-process", factory=script)
    execute(config, tmp_path / "local", factory=script, isolation=Isolation())
    assert run_files(tmp_path / "in-process") == run_files(tmp_path / "local")
    (record,) = load_records(tmp_path / "local")
    assert (record.status, record.malformed_calls, record.steps) == ("completed", 1, 4)
    assert not list((tmp_path / "local").rglob(SCRATCH_DIR))


@pytest.mark.parametrize(
    ("world", "model"),
    [("payments", "fake:golden"), ("payments", "fake:wrong"), ("security", "fake:golden")],
)
def test_world_runs_come_out_byte_identical_and_verify(tmp_path, local, world, model) -> None:
    config = world_config(world, model=model, sample=4)
    execute(config, tmp_path / "in-process")
    execute(config, tmp_path / "local", isolation=Isolation(), concurrency=2)
    assert run_files(tmp_path / "in-process") == run_files(tmp_path / "local")
    assert verify(tmp_path / "local").ok


def test_a_capped_run_stops_the_same_way(hello_config, tmp_path, local) -> None:
    script = scripted(*[turn(call("get_count")) for _ in range(10)])
    config = hello_config(max_steps=3)
    execute(config, tmp_path / "a", factory=script)
    execute(config, tmp_path / "b", factory=script, isolation=Isolation())
    assert run_files(tmp_path / "a") == run_files(tmp_path / "b")
    assert load_records(tmp_path / "b")[0].stop_reason == "max_turns"


# ------------------------------------------------------------- failure paths
class Hangs(LocalWorker):
    def argv(self) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]


class Crashes(LocalWorker):
    def argv(self) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]


@pytest.mark.parametrize("worker", [Hangs, Crashes])
def test_a_hung_or_crashed_episode_is_an_error_run(
    hello_config, tmp_path, no_docker, monkeypatch, worker
) -> None:
    monkeypatch.setattr(runs, "ContainerEpisode", worker)
    settings = Isolation(limits=Limits(call_timeout=1.0))
    manifest = execute(
        hello_config(), tmp_path / "out", factory=scripted(turn(call("finish"))), isolation=settings
    )
    assert manifest.status.errored == 1
    (record,) = load_records(tmp_path / "out")
    assert record.status == "error" and record.grade is None
    assert record.error is not None and record.error.startswith("episode container: ")
    run = tmp_path / "out" / RUNS_DIR / record.run_id
    assert sorted(path.name for path in run.iterdir()) == [RESULT_FILE]


def test_an_error_run_is_retried_on_resume(hello_config, tmp_path, no_docker, monkeypatch) -> None:
    monkeypatch.setattr(runs, "ContainerEpisode", Crashes)
    out = tmp_path / "out"
    script = scripted(turn(call("increment"), call("increment"), call("increment")))
    execute(hello_config(), out, factory=script, isolation=Isolation())
    monkeypatch.setattr(runs, "ContainerEpisode", LocalWorker)
    manifest = execute(hello_config(), out, factory=script, isolation=Isolation())
    assert (manifest.status.completed, manifest.status.errored) == (1, 0)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ((0, True), "out of memory"),
        ((137, False), "SIGKILL"),
        ((152, False), "CPU-time limit"),
        ((3, False), "exited with status 3"),
    ],
)
def test_a_killed_or_failed_container_is_named(tmp_path, no_docker, status, message) -> None:
    class Exits(LocalWorker):
        def exit_status(self) -> tuple[int, bool]:
            return status

    task = worlds.get("payments").task_source().split("v0.1")[0]
    episode = Exits(task, tmp_path / "run", Isolation(), task_hash=task_hash(task))
    episode.prepare()
    try:
        with pytest.raises(EpisodeFailed, match=message):
            episode.finish()
    finally:
        episode.cleanup()


@pytest.fixture
def prepared(tmp_path, no_docker):
    task = worlds.get("payments").task_source().split("v0.1")[0]
    episode = ContainerEpisode(task, tmp_path / "run", Isolation(), task_hash=task_hash(task))
    episode.prepare()
    yield episode
    episode.cleanup()


def output_for(episode: ContainerEpisode, **changes: object) -> EpisodeOutput:
    payload: dict[str, object] = {
        "format": "sandbox-episode-v1",
        "version": __version__,
        "task_hash": episode.task_hash,
        "reset_steps": 0,
        "first_sim_time": EPOCH,
        "trace": Trace(run_id="r", task_id=episode.task.id, world=episode.task.world),
        "grade": GradeResult(passed=True, score=1.0),
    }
    payload.update(changes)
    return EpisodeOutput.model_validate(payload)


def test_a_good_episode_file_is_read(prepared) -> None:
    good = output_for(prepared)
    (prepared.scratch / EPISODE_FILE).write_text(good.model_dump_json())
    assert prepared.read_output() == good


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"version": "0.0.1"}, "rebuild"),
        ({"task_hash": "0" * 64}, "different task"),
        ({"format": "other"}, "not 'sandbox-episode-v1'"),
    ],
)
def test_an_episode_file_for_another_task_or_version_is_refused(prepared, changes, message):
    (prepared.scratch / EPISODE_FILE).write_text(output_for(prepared, **changes).model_dump_json())
    with pytest.raises(EpisodeFailed, match=message):
        prepared.read_output()


def test_a_missing_garbled_or_oversized_episode_file_is_refused(prepared, monkeypatch) -> None:
    target = prepared.scratch / EPISODE_FILE
    with pytest.raises(EpisodeFailed, match="wrote no"):
        prepared.read_output()
    target.write_text('{"grade": "passed"}')
    with pytest.raises(EpisodeFailed, match="not a valid"):
        prepared.read_output()
    target.write_text(output_for(prepared).model_dump_json())
    monkeypatch.setattr(isolation, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(EpisodeFailed, match="larger than"):
        prepared.read_output()


def test_an_episode_file_that_is_a_link_or_a_fifo_is_never_opened(prepared, tmp_path) -> None:
    secret = tmp_path / "host-secret.txt"
    secret.write_text("not for the runner to read")
    target = prepared.scratch / EPISODE_FILE
    target.symlink_to(secret)
    with pytest.raises(EpisodeFailed, match="cannot be opened safely"):
        prepared.read_output()
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(EpisodeFailed, match="not a regular file"):
        prepared.read_output()


def test_merging_refuses_a_trace_with_other_calls_than_were_sent(prepared) -> None:
    output = output_for(prepared)
    with pytest.raises(EpisodeFailed, match="0 tool calls, but the runner sent 2"):
        merged_trace(output, [], calls=2)
    with pytest.raises(EpisodeFailed):
        merged_trace(output, [PendingMessage(after=1, text="hi", usage=None)], calls=0)


def test_messages_said_before_any_call_follow_the_steps_written_at_reset() -> None:
    trace = Trace(run_id="r", task_id="t", world="w")
    trace.add_event(EPOCH, "loaded")
    trace.add_tool_call(EPOCH, "look", {})
    output = EpisodeOutput(
        format="sandbox-episode-v1",
        version=__version__,
        task_hash="h",
        reset_steps=1,
        first_sim_time=EPOCH,
        trace=trace,
        grade=GradeResult(passed=True, score=1.0),
    )
    messages = [PendingMessage(0, "first", None), PendingMessage(1, "after", None)]
    merged = merged_trace(output, messages, calls=1)
    kinds = [
        (step.index, step.kind, step.agent_message or step.event or step.tool)
        for step in merged.steps
    ]
    assert kinds == [
        (0, "event", "loaded"),
        (1, "agent_message", "first"),
        (2, "tool_call", "look"),
        (3, "agent_message", "after"),
    ]


def test_removing_a_scratch_directory_never_follows_a_link(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("stays")
    scratch = tmp_path / "scratch"
    locked = scratch / "locked"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x")
    (scratch / "link-to-dir").symlink_to(outside)
    (scratch / "link-to-file").symlink_to(keep)
    outside.chmod(0o750)
    locked.chmod(0)
    remove_tree(scratch)
    assert not scratch.exists()
    assert keep.read_text() == "stays"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o750  # the link's target was not chmod-ed


# ----------------------------------------------------------- docker missing
def test_docker_missing_down_or_without_the_image_is_reported(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_TEST_LOG", str(tmp_path / "log"))
    monkeypatch.setattr(isolation, "DOCKER", str(tmp_path / "no-such-docker"))
    with pytest.raises(IsolationError, match="not installed"):
        isolation.preflight(Isolation())
    down = fake_docker(tmp_path, 'echo "Cannot connect to the Docker daemon" >&2; exit 1')
    monkeypatch.setattr(isolation, "DOCKER", str(down))
    with pytest.raises(IsolationError, match="Cannot connect to the Docker daemon"):
        isolation.preflight(Isolation())
    no_image = fake_docker(tmp_path, 'case "$1" in version) echo 29.8.1;; *) exit 1;; esac')
    monkeypatch.setattr(isolation, "DOCKER", str(no_image))
    with pytest.raises(IsolationError, match=r"never pulled.*docker build -t img:9 \."):
        isolation.preflight(Isolation(image="img:9"))


def test_the_cli_reports_docker_problems_and_the_guard_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(isolation, "DOCKER", str(tmp_path / "no-such-docker"))
    base = ["run", "--world", "payments", "--tasks", "payments-v0.1", "--model", "fake:golden"]
    assert main([*base, "--out", str(tmp_path / "a"), "--isolation", "container"]) == 2
    assert "docker is not installed" in capsys.readouterr().out
    guarded = ["--guard", "payments/default", "--backend", "rules_only"]
    assert main([*base, "--out", str(tmp_path / "b"), *guarded, "--isolation", "container"]) == 2
    assert GUARD_REFUSAL in capsys.readouterr().out
    assert not (tmp_path / "b").exists()


def test_execute_refuses_a_guard_in_a_container(tmp_path, no_docker) -> None:
    from guard.backends.rules_only import RulesOnlyBackend
    from guard.policy import load_policy
    from runner.guarding import GuardSetup

    setup = GuardSetup(load_policy("payments/default"), RulesOnlyBackend(), policy_source="x")
    config = world_config("payments", guard=setup.config)
    with pytest.raises(IsolationError, match="--guard cannot be combined"):
        execute(config, tmp_path / "out", guard=setup, isolation=Isolation())


# ------------------------------------------------------------------- verify
def test_verify_json_round_trips(hello_config, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    execute(hello_config(), out, factory=scripted(turn(call("increment"))))
    assert main(["verify", "--run", str(out), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["skipped"] == []
    assert [v["problems"] for v in printed["verdicts"]] == [[]]


def recorded_report(tmp_path: Path, monkeypatch, report: str, code: int = 0) -> Path:
    log = tmp_path / "docker.log"
    monkeypatch.setenv("DOCKER_TEST_LOG", str(log))
    (tmp_path / "report.json").write_text(report)
    script = fake_docker(tmp_path, f'cat "{tmp_path / "report.json"}"; exit {code}')
    monkeypatch.setattr(isolation, "DOCKER", str(script))
    return log


def test_verify_in_a_container_mounts_the_run_and_its_tasks_read_only(
    hello_config, tmp_path, monkeypatch
) -> None:
    out = tmp_path / "out"
    config = hello_config()
    execute(config, out, factory=scripted(turn(call("increment"))))
    report = verify(out)
    from runner.verify import report_json

    log = recorded_report(tmp_path, monkeypatch, report_json(report))
    assert verify_isolated(out, Isolation()).verdicts == report.verdicts
    argv = log.read_text().split()
    assert f"type=bind,src={out.resolve()},dst={RUN_MOUNT},readonly" in argv
    tasks = Path(config.tasks).resolve()
    assert f"type=bind,src={tasks},dst={tasks},readonly" in argv
    assert ("--rm" in argv) and ("--network" in argv) and ("--interactive" not in argv)
    assert argv[-4:] == ["verify", "--run", RUN_MOUNT, "--json"]


def test_verify_of_a_hidden_run_mounts_the_hidden_set_read_only(tmp_path, monkeypatch) -> None:
    out = tmp_path / "out"
    out.mkdir()
    config = world_config("payments", tasks="payments-v0.1-hidden")
    manifest = runs.Manifest(config=config, version="payments-v0.1", tasks=[])
    (out / "manifest.json").write_text(manifest.model_dump_json())
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.delenv("HIDDEN_TASKS_DIR", raising=False)
    with pytest.raises(FileNotFoundError, match="HIDDEN_TASKS_DIR"):
        verify_isolated(out, Isolation())
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(private))
    log = recorded_report(tmp_path, monkeypatch, '{"verdicts": [], "skipped": []}', code=1)
    assert verify_isolated(out, Isolation()).verdicts == []
    argv = log.read_text().split()
    assert f"type=bind,src={private.resolve()},dst={HIDDEN_MOUNT},readonly" in argv
    assert f"HIDDEN_TASKS_DIR={HIDDEN_MOUNT}" in argv


@pytest.mark.parametrize(("report", "code"), [("not json", 0), ('{"verdicts": []}', 0), ("", 2)])
def test_a_container_that_prints_no_report_fails_verify(
    hello_config, tmp_path, monkeypatch, report, code
) -> None:
    out = tmp_path / "out"
    execute(hello_config(), out, factory=scripted(turn(call("increment"))))
    recorded_report(tmp_path, monkeypatch, report, code)
    with pytest.raises(EpisodeFailed):
        verify_isolated(out, Isolation())


def test_a_one_shot_container_gets_no_writable_mount(tmp_path) -> None:
    with pytest.raises(ValueError, match="read-only mounts only"):
        run_contained(Isolation(), ["verify"], [Mount(tmp_path, "/x", writable=True)])


def test_submission_check_replays_in_a_container_when_asked(tmp_path, monkeypatch) -> None:
    seen: list[Path] = []

    def contained(path: Path, settings: Isolation):
        seen.append(path)
        raise EpisodeFailed("killed: out of memory (limit 512m)")

    monkeypatch.setattr(submission, "verify_isolated", contained)
    out = tmp_path / "golden"
    execute(world_config("payments"), out, concurrency=4)
    checked = submission.check(out, allow_fake=True, isolation=Isolation())
    assert seen == [out]
    assert any("out of memory" in problem for problem in checked.problems)


def test_the_run_config_does_not_record_isolation() -> None:
    """A directory may be resumed or verified either way: isolation changes no result."""
    assert "isolation" not in RunConfig.model_fields
