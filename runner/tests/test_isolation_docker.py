"""``--isolation container`` with real containers.

Skipped unless ``SANDBOX_TEST_IMAGE`` names a built image (CI's ``container`` job builds
one; locally: ``docker build -t agent-sandbox:0.1.0 .`` then
``SANDBOX_TEST_IMAGE=agent-sandbox:0.1.0 uv run pytest -m docker``).

The probes run Python in the image with exactly the settings an episode gets
(:func:`runner.isolation.docker_run`), and try what a compromised episode would: reach
the network, write outside its scratch directory, gain privileges, use more memory, CPU
or processes than allowed. The network probe only ever aims at 192.0.2.1 (TEST-NET-1,
RFC 5737) and ``example.invalid``, so nothing real is contacted even if it got out.
The end-to-end tests run every task of both worlds in containers and compare the run
directories with in-process ones byte for byte, and verify each both ways.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from runner_testkit import call, scripted, turn, world_config

from runner.isolation import (
    LABEL,
    OUT_MOUNT,
    TASK_MOUNT,
    Isolation,
    Limits,
    Mount,
    docker_run,
    preflight,
)
from runner.runs import RUNS_DIR, execute, load_records
from runner.verify import verify, verify_isolated

IMAGE = os.environ.get("SANDBOX_TEST_IMAGE", "")

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not IMAGE, reason="set SANDBOX_TEST_IMAGE to a built agent-sandbox image"),
]


@pytest.fixture(scope="module", autouse=True)
def image_ready() -> None:
    preflight(Isolation(image=IMAGE))


@pytest.fixture(autouse=True)
def no_container_left_behind():
    yield
    left = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"label={LABEL}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert left.stdout.strip() == "", "a container was left behind"


def probe(
    tmp_path: Path, code: str, limits: Limits | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in the image with an episode's mounts and settings."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    task = tmp_path / "task.json"
    task.write_text("{}")
    argv = docker_run(
        Isolation(image=IMAGE, limits=limits or Limits()),
        name=f"agent-sandbox-probe-{os.getpid()}-{tmp_path.name}",
        mounts=[Mount(task, TASK_MOUNT), Mount(scratch, OUT_MOUNT, writable=True)],
        command=["-c", code],
        remove=True,
        entrypoint="python",
    )
    return subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)


def probed(tmp_path: Path, code: str) -> dict:
    done = probe(tmp_path, code)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


NETWORK = """
import json, os, socket
seen = {"interfaces": sorted(os.listdir("/sys/class/net"))}
try:
    socket.create_connection(("192.0.2.1", 80), timeout=3)
    seen["tcp"] = "connected"
except OSError as error:
    seen["tcp"] = error.errno
try:
    socket.getaddrinfo("example.invalid", 80)
    seen["dns"] = "resolved"
except OSError:
    seen["dns"] = "failed"
try:
    socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    seen["raw"] = "opened"
except OSError as error:
    seen["raw"] = error.errno
print(json.dumps(seen))
"""


def test_an_episode_cannot_reach_the_network(tmp_path) -> None:
    import errno

    seen = probed(tmp_path, NETWORK)
    assert seen["interfaces"] == ["lo"]
    assert seen["tcp"] == errno.ENETUNREACH
    assert seen["dns"] == "failed"
    assert seen["raw"] == errno.EPERM


WRITES = """
import errno, json
targets = ["/probe", "/app/probe", "/app/.venv/bin/probe", "/work/probe", "/tmp/probe",
           "/var/tmp/probe", "/dev/shm/probe", "/dev/probe", "/etc/hosts", "/sandbox/probe",
           "/sandbox/task.json", "/sandbox/out/probe"]
seen = {}
for target in targets:
    try:
        with open(target, "a") as handle:
            handle.write("x")
        seen[target] = "written"
    except OSError as error:
        seen[target] = errno.errorcode[error.errno]
print(json.dumps(seen))
"""


def test_an_episode_can_write_only_its_scratch_directory(tmp_path) -> None:
    seen = probed(tmp_path, WRITES)
    assert seen.pop("/sandbox/out/probe") == "written"
    assert (tmp_path / "scratch" / "probe").read_text() == "x"
    assert set(seen.values()) <= {"EROFS", "EACCES", "ENOENT"}, seen
    assert (tmp_path / "task.json").read_text() == "{}"


IDENTITY = """
import json, os, resource
status = dict(line.split(":", 1) for line in open("/proc/self/status").read().splitlines())
def cgroup(name):
    return open(f"/sys/fs/cgroup/{name}").read().strip()
keys = ("Uid", "CapEff", "CapPrm", "CapBnd", "NoNewPrivs", "Seccomp")
seen = {key: status[key].strip() for key in keys}
limits = ("pids.max", "memory.max", "memory.swap.max", "cpu.max")
seen.update({name: cgroup(name) for name in limits})
seen["rlimit_cpu"] = list(resource.getrlimit(resource.RLIMIT_CPU))
try:
    os.setuid(0)
    seen["setuid"] = "root"
except PermissionError:
    seen["setuid"] = "refused"
print(json.dumps(seen))
"""


def test_an_episode_is_unprivileged_and_limited(tmp_path) -> None:
    seen = probed(tmp_path, IDENTITY)
    assert seen["Uid"].split()[0] == str(os.getuid() or 65534)
    assert seen["Uid"].split()[0] != "0"
    assert seen["CapEff"] == seen["CapPrm"] == seen["CapBnd"] == "0000000000000000"
    assert seen["NoNewPrivs"] == "1"
    assert seen["Seccomp"] == "2"
    assert seen["pids.max"] == "128"
    assert seen["memory.max"] == str(512 * 2**20)
    assert seen["memory.swap.max"] == "0"
    assert seen["cpu.max"] == "100000 100000"
    assert seen["rlimit_cpu"] == [120, 120]
    assert seen["setuid"] == "refused"


def test_too_many_processes_are_refused(tmp_path) -> None:
    code = """
import threading, time
started = 0
try:
    for _ in range(400):
        threading.Thread(target=time.sleep, args=(5,), daemon=True).start()
        started += 1
except RuntimeError:
    pass
print(started)
"""
    done = probe(tmp_path, code)
    assert done.returncode == 0, done.stderr
    assert int(done.stdout) < 128


def test_burning_cpu_past_the_limit_kills_the_process(tmp_path) -> None:
    done = probe(tmp_path, "while True: pass", Limits(cpu_seconds=1))
    assert done.returncode in (128 + 24, 128 + 9)


def test_an_episode_killed_for_memory_is_an_error_run(hello_config, tmp_path) -> None:
    settings = Isolation(image=IMAGE, limits=Limits(memory="40m"))
    out = tmp_path / "out"
    execute(hello_config(), out, factory=scripted(turn(call("finish"))), isolation=settings)
    (record,) = load_records(out)
    assert record.status == "error"
    assert record.error == "episode container: killed: out of memory (limit 40m)"
    assert not (out / RUNS_DIR / record.run_id / "episode").exists()


# ----------------------------------------------------------------- end to end
def run_files(out: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(out)): path.read_bytes()
        for path in sorted((out / RUNS_DIR).rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("model", ["fake:golden", "fake:wrong"])
@pytest.mark.parametrize("world", ["payments", "security"])
def test_every_task_runs_the_same_in_a_container_and_verifies_both_ways(
    tmp_path, world, model
) -> None:
    settings = Isolation(image=IMAGE)
    config = world_config(world, model=model)
    in_process, contained = tmp_path / "in-process", tmp_path / "container"
    execute(config, in_process, concurrency=4)
    manifest = execute(config, contained, concurrency=4, isolation=settings)
    assert (manifest.status.completed, manifest.status.errored) == (50, 0)
    assert run_files(in_process) == run_files(contained)
    for directory in (in_process, contained):
        for report in (verify(directory), verify_isolated(directory, settings)):
            assert report.ok
            assert len(report.verdicts) == 50
