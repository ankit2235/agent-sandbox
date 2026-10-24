"""Submitted results: ``sandbox submission check`` and ``sandbox submission spot-check``.

A submission is one ``sandbox run`` directory (``sandbox-run-v1``) of one model over a
world's whole public task set. Run directories are never committed: a pull request adds a
small submission file, ``submissions/<set>/<model>.json`` (:class:`Submission`), naming a
``.tar.gz`` of the run directory and its sha256, and :func:`fetch` downloads, checks and
unpacks it. :func:`check` needs no model and no key:

- the manifest and every record parse, and the format is ``sandbox-run-v1``;
- the run is of the full public set of the world's current version (not a sample, a
  stressed or hidden set, or a task directory), every task with every run, all completed;
- no record is of a hidden task and no file holds a hidden-set canary;
- ``sandbox verify`` reproduces every run, and the leaderboard files built from it are
  valid against ``results-v0.1``.

A replay cannot tell a model's actions from actions someone wrote by hand. :func:`spot_check`
closes part of that gap: it reruns a seeded sample of the tasks with the recorded model and
settings and compares the pass rates. Real models need keys, so a maintainer runs it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import tarfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
import jsonschema
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kit.world import worlds

from .agent import system_prompt
from .export import RunDir, build_world, load_run_dir
from .hidden import HiddenSetError, scan
from .models import FAKE_PREFIX, ModelFactory
from .runs import (
    RUN_FORMAT,
    RunConfig,
    execute,
    load_records,
    read_manifest,
    resolve_task_set,
    run_id_for,
    task_set_version,
)
from .verify import verify

CHECK_DATE = dt.date(2000, 1, 1)
"""The ``updated`` date of the leaderboard files built only to be validated, never written."""
SPOT_SAMPLE = 10
SPOT_TOLERANCE = 30.0
"""Points the rerun may fall below the submitted pass rate on the same tasks. Invented."""
MAX_LISTED = 5
SUBMISSION_FORMAT = "submission-v1"
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
"""The largest archive :func:`fetch` downloads: 500 MiB."""


class SubmissionError(ValueError):
    """A submission file, or the archive it names, is not acceptable."""


class Submission(BaseModel):
    """``submissions/<set>/<model>.json``: who ran what, and where the run directory is."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["submission-v1"]
    task_set: str
    model: str
    model_name: str
    provider: str
    open_weight: bool = False
    archive_url: str
    """An https URL of a ``.tar.gz`` holding exactly one run directory."""
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contact: str = ""

    @field_validator("archive_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            msg = "archive_url must be an https:// URL"
            raise ValueError(msg)
        return value


Download = Callable[[str, Path, int], None]


def download(
    url: str, destination: Path, max_bytes: int, *, client: httpx.Client | None = None
) -> None:
    """Stream ``url`` into ``destination``; refuse anything larger than ``max_bytes``."""
    received = 0
    session = client or httpx.Client(follow_redirects=True, timeout=60.0)
    with (
        session,
        session.stream("GET", url) as response,
        destination.open("wb") as sink,
    ):
        response.raise_for_status()
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received > max_bytes:
                msg = f"the archive is larger than {max_bytes} bytes"
                raise SubmissionError(msg)
            sink.write(chunk)


def load_submission(path: Path) -> Submission:
    """Read and validate a submission file."""
    try:
        return Submission.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        msg = f"{path} is not a {SUBMISSION_FORMAT} file: {problems}"
        raise SubmissionError(msg) from None


def fetch(
    path: Path,
    out: Path,
    *,
    get: Download | None = None,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> Path:
    """Download, hash-check and safely unpack a submission's archive; return its run directory."""
    submission = load_submission(path)
    out.mkdir(parents=True, exist_ok=True)
    archive = out / "archive.tar.gz"
    (get or download)(submission.archive_url, archive, max_bytes)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != submission.archive_sha256:
        msg = f"the archive's sha256 is {digest}, not the {submission.archive_sha256} submitted"
        raise SubmissionError(msg)
    unpacked = out / "unpacked"
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(unpacked, filter="data")
    except (tarfile.TarError, OSError) as error:
        msg = f"the archive cannot be unpacked safely: {error}"
        raise SubmissionError(msg) from None
    manifests = sorted(unpacked.rglob("manifest.json"))
    if len(manifests) != 1:
        msg = f"the archive must hold exactly one run directory; it holds {len(manifests)}"
        raise SubmissionError(msg)
    run_dir = manifests[0].parent
    try:
        manifest = read_manifest(run_dir)
    except ValidationError:
        msg = "the archive's manifest.json is not sandbox-run-v1"
        raise SubmissionError(msg) from None
    assert manifest is not None
    config = manifest.config
    claimed = (submission.task_set, submission.model, submission.model_name, submission.provider)
    recorded = (manifest.version, config.model, config.model_name, config.provider)
    if claimed != recorded:
        msg = f"the submission says {claimed}, the run directory records {recorded}"
        raise SubmissionError(msg)
    return run_dir


@dataclass(slots=True)
class Checked:
    """What :func:`check` found about one run directory."""

    path: Path
    problems: list[str] = field(default_factory=list)
    run_dir: RunDir | None = None
    runs_verified: int = 0

    @property
    def ok(self) -> bool:
        """True when nothing is wrong."""
        return not self.problems

    @property
    def model(self) -> str:
        """The model's slug, or ``?`` if the manifest did not load."""
        return self.run_dir.manifest.config.model_slug if self.run_dir else "?"


def _listed(problems: list[str]) -> list[str]:
    extra = len(problems) - MAX_LISTED
    return problems[:MAX_LISTED] + ([f"... and {extra} more"] if extra > 0 else [])


def check(path: Path, *, allow_fake: bool = False, min_runs: int = 1) -> Checked:
    """Every check a submitted run directory must pass. Never raises for a bad submission."""
    result = Checked(path)
    try:
        run_dir = load_run_dir(path)
    except FileNotFoundError as error:
        result.problems.append(str(error))
        return result
    except ValidationError as error:
        result.problems.append(f"not a {RUN_FORMAT} directory: {error.error_count()} bad field(s)")
        return result
    result.run_dir = run_dir
    manifest, config = run_dir.manifest, run_dir.manifest.config
    if manifest.format != RUN_FORMAT:
        result.problems.append(f"format is {manifest.format!r}, not {RUN_FORMAT!r}")
    if config.world not in worlds:
        result.problems.append(f"unknown world {config.world!r}")
        return result
    spec = worlds.get(config.world)
    expected = task_set_version(spec)
    if manifest.version != expected or config.tasks != expected:
        result.problems.append(
            f"runs {config.tasks!r} ({manifest.version}); a result must run the whole {expected}"
        )
        return result
    if config.sample is not None:
        result.problems.append(f"ran a sample of {config.sample} tasks, not the whole set")
    if config.model.startswith(FAKE_PREFIX) and not allow_fake:
        result.problems.append(f"{config.model} is a fake model, not a result")
    prompt_id, _ = system_prompt(spec.name)
    if config.prompt != prompt_id:
        result.problems.append(f"ran with prompt {config.prompt!r}; {expected} uses {prompt_id!r}")
    if config.runs < min_runs:
        result.problems.append(f"{config.runs} run(s) per task; at least {min_runs} needed")
    tasks = resolve_task_set(spec, expected)
    if sorted(manifest.tasks) != sorted(task.id for task in tasks):
        result.problems.append(f"the manifest does not list exactly the {len(tasks)} tasks")
    by_id = {record.run_id: record for record in run_dir.records}
    wanted = {run_id_for(config, task, n) for task in tasks for n in range(1, config.runs + 1)}
    missing = sorted(wanted - set(by_id))
    if missing:
        result.problems.append(f"{len(missing)} run(s) missing, e.g. {missing[0]}")
    extra = sorted(set(by_id) - wanted)
    if extra:
        result.problems.append(f"{len(extra)} run(s) not of this set, e.g. {extra[0]}")
    errored = sorted(r.run_id for r in run_dir.records if r.status != "completed")
    if errored:
        result.problems.append(f"{len(errored)} run(s) errored, e.g. {errored[0]}")
    if any(record.visibility != "public" for record in run_dir.records):
        result.problems.append("holds runs of hidden tasks; those are never submitted")
    sightings = scan([path], {})
    if sightings:
        result.problems.append(f"{len(sightings)} hidden-set canary sighting(s) in its files")
    if result.problems:
        return result
    try:
        report = verify(path)
    except (FileNotFoundError, ValueError) as error:
        result.problems.append(f"verify could not replay the runs: {error}")
        return result
    result.runs_verified = sum(verdict.ok for verdict in report.verdicts)
    for verdict in report.verdicts:
        result.problems += [f"verify {verdict.run_id}: {problem}" for problem in verdict.problems]
    try:
        build_world(spec, [run_dir], CHECK_DATE)
    except (ValueError, jsonschema.ValidationError, HiddenSetError) as error:
        result.problems.append(f"the leaderboard files would be invalid: {error}")
    result.problems = _listed(result.problems)
    return result


@dataclass(slots=True)
class SpotCheck:
    """A rerun of a sample of a submission's tasks, against what was submitted."""

    tasks: list[str]
    submitted: float
    """Pass rate in % of the submitted runs, on the sampled tasks only."""
    rerun: float
    tolerance: float
    rerun_dir: Path
    errored: int = 0

    @property
    def drop(self) -> float:
        """Points the rerun scored below the submission (negative: above)."""
        return round(self.submitted - self.rerun, 1)

    @property
    def ok(self) -> bool:
        """Within tolerance, and every rerun completed."""
        return self.errored == 0 and self.drop <= self.tolerance


def spot_check(
    path: Path,
    out: Path,
    *,
    sample: int = SPOT_SAMPLE,
    seed: int = 0,
    tolerance: float = SPOT_TOLERANCE,
    factory: ModelFactory | None = None,
    concurrency: int = 1,
) -> SpotCheck:
    """Rerun ``sample`` seeded tasks once each with the submission's model and settings."""
    run_dir = load_run_dir(path)
    config: RunConfig = run_dir.manifest.config.model_copy(
        update={"sample": sample, "sample_seed": seed, "runs": 1, "prompt": ""}
    )
    execute(config, out, concurrency=concurrency, factory=factory)
    reruns = load_records(out)
    picked = sorted({record.task_id for record in reruns})
    submitted: dict[str, list[bool]] = defaultdict(list)
    for record in run_dir.records:
        if record.task_id in picked:
            submitted[record.task_id].append(record.passed)
    completed = [record for record in reruns if record.status == "completed"]

    def rate(values: list[float]) -> float:
        return round(100 * sum(values) / len(values), 1) if values else 0.0

    return SpotCheck(
        tasks=picked,
        submitted=rate([sum(runs) / len(runs) for runs in submitted.values() if runs]),
        rerun=rate([float(record.passed) for record in completed]),
        tolerance=tolerance,
        rerun_dir=out,
        errored=len(reruns) - len(completed),
    )


__all__ = [
    "CHECK_DATE",
    "MAX_ARCHIVE_BYTES",
    "SPOT_SAMPLE",
    "SPOT_TOLERANCE",
    "SUBMISSION_FORMAT",
    "Checked",
    "SpotCheck",
    "Submission",
    "SubmissionError",
    "check",
    "download",
    "fetch",
    "load_submission",
    "spot_check",
]
