"""Episodes in locked-down containers: ``sandbox run --isolation container``.

The agent never runs code of its own, but its tool arguments drive world code: the tool
handlers, the services and the grader. So does a recorded trace when ``sandbox verify``
replays it, and submitted traces come from outside. Those paths run here in a container
with no network, a read-only root filesystem, no capabilities, no new privileges, a
non-root user, and CPU, memory, process and CPU-time limits. The model calls (which need
the network and the provider keys), the agent loop, the caps and the run directory stay
in the runner, outside; no key ever enters a container.

The boundary is MCP over stdio, the protocol the agent already speaks in process:

1. the runner writes the task as JSON to a private temporary file, mounted read-only;
2. ``docker run -i ... sandbox episode serve`` builds the episode through the world
   registry, resets it and serves the kit's :class:`kit.mcp_bridge.McpBridge` on stdin
   and stdout (:func:`serve`);
3. when the runner disconnects, the worker grades the episode and writes
   ``episode.json`` (the world's trace and the grade) into ``runs/<run_id>/episode/``,
   the container's only writable path;
4. the runner waits for the container, reads that file defensively, puts the model's
   messages back into the trace where they were said (:func:`merged_trace`), and writes
   ``trace.jsonl`` and ``result.json`` exactly as an in-process run does; then it removes
   the scratch directory and the container.

A container that is killed (memory, CPU time), times out, exits non-zero or writes a bad
file makes the run an ``error`` run, as a provider failure does: it is not graded, and a
resume runs it again. The timeouts are wall-clock, but they never decide a score.

:func:`run_contained` runs one command in the same container settings with only
read-only mounts; ``sandbox verify --isolation container`` uses it
(:func:`runner.verify.verify_isolated`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, ValidationError

from kit import __version__
from kit.clock import EPOCH, SimClock
from kit.episode import BaseEpisode
from kit.mcp_bridge import McpBridge
from kit.rng import SeededRNG
from kit.trace import Trace, TraceStep
from kit.types import GradeResult, TaskSpec, Usage
from kit.world import load_grader, worlds

DOCKER = "docker"
IMAGE = f"agent-sandbox:{__version__}"
"""The default image: built locally with ``docker build -t agent-sandbox:<version> .``."""
LABEL = "agent-sandbox.episode"
"""Every container started here carries this label (``docker ps -a --filter label=...``)."""
SCRATCH_DIR = "episode"
"""``runs/<run_id>/episode/``: the episode container's only writable path."""
EPISODE_FILE = "episode.json"
EPISODE_FORMAT = "sandbox-episode-v1"
TASK_MOUNT = "/sandbox/task.json"
OUT_MOUNT = "/sandbox/out"
RUN_MOUNT = "/sandbox/run"
HIDDEN_MOUNT = "/sandbox/hidden"
WORKDIR = "/work"
FALLBACK_USER = (65534, 65534)
"""The container user when the runner itself is root: ``nobody:nogroup``."""
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
"""The most the runner reads back from a container: ``episode.json`` or verify's report."""
STDERR_TAIL = 2000
DOCKER_TIMEOUT = 60.0
"""Seconds for one Docker housekeeping command (inspect, wait for removal, ...)."""
GUARD_REFUSAL = (
    "--guard cannot be combined with --isolation container yet: the guard reads the live "
    "episode state, which stays inside the container (DECISIONS)"
)
CONTAINER_ERRORS: tuple[type[BaseException], ...] = (
    McpError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
)
"""What the MCP client raises when the episode's process dies, hangs or closes the pipe."""


class IsolationError(RuntimeError):
    """Containers cannot be used at all: Docker missing or down, the image missing."""


class EpisodeFailed(RuntimeError):
    """One container failed (killed, timed out, bad output); its run is an error run."""


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource and time limits for every container (measured defaults: DECISIONS)."""

    cpus: str = "1"
    memory: str = "512m"
    """Also the memory-plus-swap limit, so a container never swaps."""
    pids: int = 128
    cpu_seconds: int = 120
    """``RLIMIT_CPU``: CPU time, not wall time; the kernel ends the process past it."""
    call_timeout: float = 60.0
    """Seconds the episode may take to answer one MCP request (initialize, a tool call)."""
    exit_timeout: float = 60.0
    """Seconds the episode may take to grade and exit once the runner disconnects."""
    run_timeout: float = 1800.0
    """Seconds one :func:`run_contained` command (a whole ``verify``) may take."""


@dataclass(frozen=True, slots=True)
class Isolation:
    """How ``--isolation container`` runs: the image and the limits."""

    image: str = IMAGE
    limits: Limits = field(default_factory=Limits)


@dataclass(frozen=True, slots=True)
class Mount:
    """A host path bound into a container; read-only unless ``writable``."""

    source: Path
    target: str
    writable: bool = False

    def flag(self) -> list[str]:
        """The ``--mount`` argument. Docker parses it as CSV, so some characters are refused."""
        for part in (str(self.source), self.target):
            if any(char in part for char in ',"\n'):
                msg = f"cannot mount {part!r} into a container: it contains a comma or a quote"
                raise IsolationError(msg)
        spec = f"type=bind,src={self.source},dst={self.target}"
        return ["--mount", spec if self.writable else f"{spec},readonly"]


# ------------------------------------------------------------------- docker
def _docker(*args: str, timeout: float = DOCKER_TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [DOCKER, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        msg = "docker is not installed: there is no `docker` command on PATH"
        raise IsolationError(msg) from None


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no message"


def preflight(isolation: Isolation) -> None:
    """Fail before any run if Docker is missing or down, or the image is not built."""
    try:
        server = _docker("version", "--format", "{{.Server.Version}}")
    except subprocess.TimeoutExpired:
        msg = "the Docker daemon did not answer `docker version`"
        raise IsolationError(msg) from None
    if server.returncode != 0:
        msg = f"the Docker daemon is not reachable: {_last_line(server.stderr)}"
        raise IsolationError(msg)
    image = _docker("image", "inspect", "--format", "{{.Id}}", isolation.image)
    if image.returncode != 0:
        msg = (
            f"image {isolation.image} is not present locally (it is never pulled); build it "
            f"with `docker build -t {isolation.image} .`"
        )
        raise IsolationError(msg)


def container_user() -> tuple[int, int]:
    """The runner's own uid and gid, so the scratch files are its own; never root.

    A runner started as root runs containers as :data:`FALLBACK_USER` and hands that
    user the scratch directory only.
    """
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        return FALLBACK_USER
    return uid, gid if gid != 0 else FALLBACK_USER[1]


def remove_container(name: str) -> None:
    """Remove a container by name, running or not; a missing one is fine."""
    _docker("rm", "--force", name)


def docker_run(
    isolation: Isolation,
    *,
    name: str,
    mounts: Sequence[Mount],
    command: Sequence[str],
    user: tuple[int, int] | None = None,
    env: Mapping[str, str] | None = None,
    interactive: bool = False,
    remove: bool = False,
    entrypoint: str | None = None,
) -> list[str]:
    """The ``docker run`` command line every container here is started with.

    No network, a read-only root filesystem with no tmpfs (``--ipc none`` also removes
    Docker's writable ``/dev/shm``), every capability dropped,
    no new privileges, a non-root user, CPU, memory (no swap), process and CPU-time
    limits, no logging driver (the MCP traffic carries task content), and never a pull.
    """
    limits = isolation.limits
    uid, gid = user if user is not None else container_user()
    argv = [DOCKER, "run"]
    if interactive:
        argv.append("--interactive")
    if remove:
        argv.append("--rm")
    argv += [
        "--name",
        name,
        "--label",
        f"{LABEL}=1",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--ipc",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        f"{uid}:{gid}",
        "--cpus",
        limits.cpus,
        "--memory",
        limits.memory,
        "--memory-swap",
        limits.memory,
        "--pids-limit",
        str(limits.pids),
        "--ulimit",
        f"cpu={limits.cpu_seconds}",
        "--log-driver",
        "none",
        "--sig-proxy=false",
        "--workdir",
        WORKDIR,
    ]
    for key, value in (env or {}).items():
        argv += ["--env", f"{key}={value}"]
    for mount in mounts:
        argv += mount.flag()
    if entrypoint is not None:
        argv += ["--entrypoint", entrypoint]
    return [*argv, isolation.image, *command]


def container_path(value: str) -> str:
    """Where a host path the run names (relative to the working directory) sits inside."""
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(WORKDIR, value))


def _docker_env() -> dict[str, str]:
    """The Docker client's own settings (``DOCKER_HOST``, ...); nothing else reaches it."""
    return {key: value for key, value in os.environ.items() if key.startswith("DOCKER_")}


def _read_regular(path: Path, what: str) -> bytes:
    """Read a file a container wrote: a regular file, not a link or a FIFO, size-capped."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        msg = f"the episode wrote no {what}"
        raise EpisodeFailed(msg) from None
    except OSError as error:
        msg = f"{what} cannot be opened safely ({error.strerror}); refused"
        raise EpisodeFailed(msg) from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            msg = f"{what} is not a regular file; refused"
            raise EpisodeFailed(msg)
        data = handle.read(MAX_OUTPUT_BYTES + 1)
    if len(data) > MAX_OUTPUT_BYTES:
        msg = f"{what} is larger than {MAX_OUTPUT_BYTES} bytes; refused"
        raise EpisodeFailed(msg)
    return data


def remove_tree(path: Path) -> None:
    """Remove a directory a container wrote into, even where it took its own permissions.

    Never follows a symbolic link: links are removed, not their targets.
    """
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if not path.exists():
        return
    for root, dirs, _ in os.walk(path):
        for name in dirs:
            child = os.path.join(root, name)
            if not os.path.islink(child):
                os.chmod(child, 0o700)
    shutil.rmtree(path)


# ------------------------------------------------------------------- worker
class EpisodeOutput(BaseModel):
    """``episode.json``: what the worker hands back when the runner disconnects."""

    model_config = ConfigDict(extra="forbid")

    format: str
    version: str
    task_hash: str
    reset_steps: int
    """Trace steps the world wrote while loading the task, before the first call."""
    first_sim_time: dt.datetime
    trace: Trace
    """The world's trace: tool calls and events, no agent messages."""
    grade: GradeResult


def serve(task_file: Path, out: Path) -> int:
    """``sandbox episode serve``: inside the container, serve one episode, then grade it."""
    from .runs import task_hash  # runs imports this module

    task = TaskSpec.model_validate_json(task_file.read_text(encoding="utf-8"))
    spec = worlds.get(task.world)
    episode = spec.make_episode(task, SimClock(EPOCH), SeededRNG(task.seed))
    if not isinstance(episode, BaseEpisode):
        msg = f"world {spec.name!r} must build its episodes on kit.episode.BaseEpisode"
        raise TypeError(msg)
    first = episode.reset(task)
    reset_steps = len(episode.trace.steps)
    try:
        bridge = McpBridge(episode, episode.router, task, name=f"sandbox-{episode.world}")
        anyio.run(bridge.serve_stdio)
        grade = load_grader(task.grader)(episode.state, episode.trace.grader_view(), task)
        output = EpisodeOutput(
            format=EPISODE_FORMAT,
            version=__version__,
            task_hash=task_hash(task),
            reset_steps=reset_steps,
            first_sim_time=first.sim_time,
            trace=episode.trace,
            grade=grade,
        )
        temporary = out / f"{EPISODE_FILE}.tmp"
        temporary.write_text(output.model_dump_json(), encoding="utf-8")
        os.replace(temporary, out / EPISODE_FILE)
    finally:
        episode.close()
    return 0


# --------------------------------------------------------------------- host
@dataclass(frozen=True, slots=True)
class PendingMessage:
    """Something the model said, and how many tool calls the world had applied by then."""

    after: int
    text: str
    usage: Usage | None


def merged_trace(output: EpisodeOutput, messages: Sequence[PendingMessage], calls: int) -> Trace:
    """The world's trace with the model's messages put back where an in-process run has them.

    In process, a message is appended right after the tool call before it (or after the
    steps the world wrote at reset), at that moment's simulated time; events a later call
    drains come after it. ``calls`` is how many tool calls the runner sent: a trace that
    applied a different number is refused.
    """
    pending = deque(messages)
    world = output.trace.steps
    steps: list[TraceStep] = list(world[: output.reset_steps])
    applied = 0

    def said(sim_time: dt.datetime) -> None:
        while pending and pending[0].after == applied:
            message = pending.popleft()
            steps.append(
                TraceStep(
                    index=0,
                    sim_time=sim_time,
                    kind="agent_message",
                    agent_message=message.text,
                    usage=message.usage,
                )
            )

    said(output.first_sim_time)
    for step in world[output.reset_steps :]:
        steps.append(step)
        if step.kind == "tool_call":
            applied += 1
            said(step.sim_time)
    if applied != calls or pending:
        msg = f"the episode's trace has {applied} tool calls, but the runner sent {calls}"
        raise EpisodeFailed(msg)
    indexed = [step.model_copy(update={"index": number}) for number, step in enumerate(steps)]
    return output.trace.model_copy(update={"steps": indexed})


class ContainerEpisode:
    """One run's episode in its own container, as the agent loop sees it.

    It is a :class:`runner.agent.EpisodeLink`: the MCP session goes to the container, the
    model's messages are kept here until :meth:`trace` merges them in.
    """

    def __init__(
        self, task: TaskSpec, directory: Path, isolation: Isolation, *, task_hash: str
    ) -> None:
        self.task = task
        self.isolation = isolation
        self.task_hash = task_hash
        self.scratch = directory / SCRATCH_DIR
        digest = hashlib.sha256(str(self.scratch.resolve()).encode()).hexdigest()[:16]
        self.name = f"agent-sandbox-ep-{digest}"
        self.user = container_user()
        self.messages: list[PendingMessage] = []
        self.calls = 0
        self._private: Path | None = None
        self._stderr: TextIO | None = None

    # ------------------------------------------------------- EpisodeLink
    @property
    def step_count(self) -> int:
        """Tool calls sent to the world so far."""
        return self.calls

    def tool_called(self) -> None:
        """Count one tool call the world answered."""
        self.calls += 1

    def add_agent_message(self, text: str, usage: Usage | None) -> None:
        """Keep what the model said, with its place among the tool calls."""
        self.messages.append(PendingMessage(after=self.calls, text=text, usage=usage))

    @asynccontextmanager
    async def session(self, guard: Any = None) -> AsyncIterator[ClientSession]:
        """An MCP client session to the episode, in a container started for it."""
        if guard is not None:
            raise IsolationError(GUARD_REFUSAL)
        argv = self.argv()
        params = StdioServerParameters(command=argv[0], args=argv[1:], env=_docker_env())
        timeout = dt.timedelta(seconds=self.isolation.limits.call_timeout)
        assert self._stderr is not None, "prepare() first"
        async with (
            stdio_client(params, errlog=self._stderr) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timeout) as client,
        ):
            await client.initialize()
            yield client

    # ---------------------------------------------------------- lifecycle
    @property
    def task_file(self) -> Path:
        """The private copy of the task the container reads."""
        assert self._private is not None, "prepare() first"
        return self._private / "task.json"

    def argv(self) -> list[str]:
        """The command that starts the episode: ``docker run ... sandbox episode serve``."""
        mounts = [
            Mount(self.task_file, TASK_MOUNT),
            Mount(self.scratch.resolve(), OUT_MOUNT, writable=True),
        ]
        command = ["episode", "serve", "--task", TASK_MOUNT, "--out", OUT_MOUNT]
        return docker_run(
            self.isolation,
            name=self.name,
            mounts=mounts,
            command=command,
            user=self.user,
            interactive=True,
        )

    def prepare(self) -> None:
        """A fresh scratch directory and task file; a stale container of this run removed."""
        remove_container(self.name)
        remove_tree(self.scratch)
        self.scratch.mkdir(parents=True)
        self._private = Path(tempfile.mkdtemp(prefix="agent-sandbox-task-"))
        self.task_file.write_text(self.task.model_dump_json(), encoding="utf-8")
        self.task_file.chmod(0o600)
        if os.getuid() == 0:
            # Root on the host: the container user gets the scratch directory and can read
            # the task; nothing else is handed over.
            uid, gid = self.user
            os.chown(self.scratch, uid, gid)
            os.chown(self.task_file, uid, gid)
        self._stderr = (self._private / "stderr.log").open("w+", encoding="utf-8")

    def stderr_tail(self) -> str:
        """The end of what the container (or the Docker client) wrote to stderr."""
        if self._stderr is None:
            return ""
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()[-STDERR_TAIL:].strip()

    def exit_status(self) -> tuple[int, bool]:
        """Wait for the container to exit; its exit code and whether memory killed it."""
        limits = self.isolation.limits
        try:
            waited = _docker("wait", self.name, timeout=limits.exit_timeout)
        except subprocess.TimeoutExpired:
            remove_container(self.name)
            msg = f"the episode did not exit within {limits.exit_timeout:g}s of the run ending"
            raise EpisodeFailed(msg) from None
        if waited.returncode != 0:
            msg = f"the episode container never ran: {self.stderr_tail() or waited.stderr}"
            raise EpisodeFailed(msg.strip())
        state = _docker(
            "inspect", "--format", "{{.State.ExitCode}} {{.State.OOMKilled}}", self.name
        )
        code, oom = state.stdout.split()
        return int(code), oom == "true"

    def finish(self) -> None:
        """Wait for the episode to grade and exit; raise if it was killed or failed."""
        code, oom = self.exit_status()
        limits = self.isolation.limits
        if oom:
            msg = f"killed: out of memory (limit {limits.memory})"
            raise EpisodeFailed(msg)
        if code == 128 + 9:
            msg = "killed (SIGKILL), e.g. by a memory or CPU-time limit"
            raise EpisodeFailed(msg)
        if code == 128 + 24:
            msg = f"killed: CPU-time limit ({limits.cpu_seconds}s)"
            raise EpisodeFailed(msg)
        if code != 0:
            msg = f"exited with status {code}: {self.stderr_tail() or 'no message'}"
            raise EpisodeFailed(msg)

    def read_output(self) -> EpisodeOutput:
        """``episode.json``, checked: this task, this version."""
        data = _read_regular(self.scratch / EPISODE_FILE, EPISODE_FILE)
        try:
            output = EpisodeOutput.model_validate_json(data)
        except ValidationError as error:
            msg = f"{EPISODE_FILE} is not a valid {EPISODE_FORMAT} file ({error.error_count()} bad)"
            raise EpisodeFailed(msg) from None
        if output.format != EPISODE_FORMAT:
            msg = f"{EPISODE_FILE} is {output.format!r}, not {EPISODE_FORMAT!r}"
            raise EpisodeFailed(msg)
        if output.version != __version__:
            msg = f"the image runs version {output.version}, the runner {__version__}; rebuild it"
            raise EpisodeFailed(msg)
        if output.task_hash != self.task_hash or output.trace.task_id != self.task.id:
            msg = "the episode ran a different task than the runner sent"
            raise EpisodeFailed(msg)
        return output

    def trace(self, output: EpisodeOutput) -> Trace:
        """The full trace: the world's steps and the model's messages, as in process."""
        return merged_trace(output, self.messages, self.calls)

    def cleanup(self) -> None:
        """Remove the container, the scratch directory and the private task file."""
        remove_container(self.name)
        remove_tree(self.scratch)
        if self._stderr is not None:
            self._stderr.close()
        if self._private is not None:
            shutil.rmtree(self._private)


# ------------------------------------------------------- one-shot commands
@dataclass(frozen=True, slots=True)
class Contained:
    """What one :func:`run_contained` command printed, and how it exited."""

    returncode: int
    stdout: bytes
    stderr: str


def run_contained(
    isolation: Isolation,
    command: Sequence[str],
    mounts: Sequence[Mount],
    env: Mapping[str, str] | None = None,
) -> Contained:
    """Run one ``sandbox`` command in a locked-down container with read-only mounts only."""
    if any(mount.writable for mount in mounts):
        msg = "a one-shot container gets read-only mounts only"
        raise ValueError(msg)
    name = f"agent-sandbox-cmd-{os.getpid()}-{threading.get_ident()}"
    argv = docker_run(isolation, name=name, mounts=mounts, command=command, env=env, remove=True)
    timeout = isolation.limits.run_timeout
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            completed = subprocess.run(
                argv, stdout=out, stderr=err, timeout=timeout, check=False, env=_client_env()
            )
        except FileNotFoundError:
            msg = "docker is not installed: there is no `docker` command on PATH"
            raise IsolationError(msg) from None
        except subprocess.TimeoutExpired:
            remove_container(name)
            msg = f"the container did not finish within {timeout:g}s; removed"
            raise EpisodeFailed(msg) from None
        if out.tell() > MAX_OUTPUT_BYTES:
            msg = f"the container printed more than {MAX_OUTPUT_BYTES} bytes; refused"
            raise EpisodeFailed(msg)
        out.seek(0)
        err.seek(0)
        stderr = err.read()[-STDERR_TAIL:].decode("utf-8", errors="replace").strip()
        return Contained(completed.returncode, out.read(), stderr)


def _client_env() -> dict[str, str]:
    """The Docker client's environment: the basics and its own settings, no provider keys."""
    keep = ("HOME", "PATH", "USER", "LOGNAME", "XDG_RUNTIME_DIR")
    basics = {key: os.environ[key] for key in keep if key in os.environ}
    return {**basics, **_docker_env()}


__all__ = [
    "CONTAINER_ERRORS",
    "EPISODE_FILE",
    "GUARD_REFUSAL",
    "IMAGE",
    "LABEL",
    "SCRATCH_DIR",
    "Contained",
    "ContainerEpisode",
    "EpisodeFailed",
    "EpisodeOutput",
    "Isolation",
    "IsolationError",
    "Limits",
    "Mount",
    "PendingMessage",
    "container_path",
    "container_user",
    "docker_run",
    "merged_trace",
    "preflight",
    "remove_container",
    "remove_tree",
    "run_contained",
    "serve",
]
