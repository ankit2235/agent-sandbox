"""The ``sandbox`` command.

- ``worlds``: list installed worlds;
- ``tasks validate``: check task files, including each world's ``schema.json``;
- ``example``: run one scripted episode;
- ``run``: run a model over a task set (:mod:`runner.runs`), resumable; with
  ``--guard <policy> --backend <name>``, the guard stands in front of the world's
  high-impact tools (:mod:`runner.guarding`);
- ``verify``: replay a run directory's recorded actions and confirm its grades;
- ``--isolation container`` (on ``run``, ``verify`` and ``submission check``): the world
  code runs in locked-down containers (:mod:`runner.isolation`);
- ``episode serve``: inside such a container, serve one episode over stdio;
- ``export``: write leaderboard files from one or more run directories;
- ``stress list | calibrate | report``: the stress modifiers and their calibration;
- ``hidden generate | hashes | check | scan | leaks``: the hidden set (:mod:`runner.hidden`);
- ``quickstart``: ten tasks with one model, and a score;
- ``release-check`` and ``freeze``: the release gate (:mod:`runner.release`);
- ``submission fetch | check | spot-check``: submitted results (:mod:`runner.submission`);
- ``tools``: a world's tool reference (:mod:`runner.reference`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kit import __version__, worlds
from kit.scripted import run_script
from kit.tasks import TaskSource, hidden_tasks_dir, load_tasks
from kit.types import ToolCall
from kit.world import WorldSpec, load_grader
from stress.core import GENERIC, registry

from . import DISTRIBUTION, hidden, reference, release, submission
from .calibrate import calibrate, write_report
from .calibrate import markdown as calibration_markdown
from .counterpart import SCRIPTED
from .export import export
from .guarding import GuardSetup
from .isolation import (
    GUARD_REFUSAL,
    IMAGE,
    EpisodeFailed,
    Isolation,
    IsolationError,
    preflight,
    serve,
)
from .models import FAKE_PREFIX, model_factory
from .runs import RunConfig, RunRecord, execute, load_records, slug, task_set_version
from .score import score, summary_lines
from .validate import task_files, validate_file
from .verify import report_json, verify, verify_isolated

DEFAULT_POLICY = "kit.examples.hello_world.scripted:golden_run"
QUICKSTART_WORLD = "payments"
QUICKSTART_TASKS = 10


def _print(line: str = "") -> None:
    sys.stdout.write(f"{line}\n")


def cmd_worlds(args: argparse.Namespace) -> int:
    """List installed worlds and what they declare."""
    names = worlds.names()
    if not names:
        _print("No worlds installed.")
        return 0
    if args.json:
        payload = [
            worlds.get(name).model_dump(mode="json", exclude={"task_schema"}) for name in names
        ]
        _print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for name in names:
        spec = worlds.get(name)
        _print(f"{spec.name} ({spec.version})")
        if spec.description:
            _print(f"  {spec.description}")
        _print(f"  categories    : {', '.join(spec.categories)}")
        _print(f"  tools         : {', '.join(spec.tool_names)}")
        _print(f"  failure types : {', '.join(f.id for f in spec.failure_types) or '-'}")
        _print(f"  tasks         : {spec.tasks_dir or '-'}")
        for extra, path in sorted(spec.task_sets.items()):
            _print(f"  extra set     : {spec.name}-{spec.version}-{extra} ({path})")
        for role in spec.roles:
            _print(f"  counterpart   : {role.name} ({', '.join(role.tools)})")
        _print()
    return 0


def _resolve_tasks(spec: WorldSpec, directory: str | None) -> TaskSource:
    if directory:
        return TaskSource(load_tasks(directory))
    return spec.task_source()


def cmd_tasks_validate(args: argparse.Namespace) -> int:
    """Validate task files: the kit's checks, the world's file schema, names and ids."""
    roots = [Path(path) for path in args.paths or []]
    if not roots:
        for name in worlds.names():
            spec = worlds.get(name)
            if spec.tasks_dir is not None:
                roots.append(spec.tasks_dir)
            roots += [spec.task_sets[extra] for extra in sorted(spec.task_sets)]
    files = [(path, root) for root in roots for path in task_files(root)]
    if not files:
        _print("No task files found.")
        return 1
    failures = 0
    pending = 0
    seen: dict[str, Path] = {}
    for path, root in files:
        try:
            task = validate_file(path, root)
            if task.run_key in seen:
                msg = f"duplicate task id; also in {seen[task.run_key]}"
                raise ValueError(msg)
        except Exception as error:
            failures += 1
            _print(f"FAIL {path}: {type(error).__name__}: {error}")
            continue
        seen[task.run_key] = path
        pending += task.reviewer_signoff is None
        _print(f"ok   {task.run_key}")
    _print()
    _print(f"{len(files) - failures}/{len(files)} task files valid")
    if pending:
        _print(f"{pending} task(s) awaiting reviewer sign-off (reviewer_signoff: null)")
    return 1 if failures else 0


def cmd_example(args: argparse.Namespace) -> int:
    """Run one scripted episode and print the trace summary and the grade."""
    spec = worlds.get(args.world)
    source = _resolve_tasks(spec, args.tasks)
    task = source.get(args.task) if args.task else source.tasks[0]
    policy: Any = load_grader(args.policy)  # same dotted-path loader, no world import
    calls: Sequence[ToolCall] = policy(task)
    run = run_script(spec, task, calls, seed=args.seed)

    _print(f"task   : {task.run_key} ({task.difficulty})")
    _print(f"ask    : {task.instruction.strip()}")
    _print(f"policy : {args.policy}")
    _print()
    for step in run.trace.steps:
        marker = "!" if step.error else " "
        detail = step.error.code if step.error else json.dumps(step.response, sort_keys=True)
        _print(
            f" {marker} {step.index:>2} {step.sim_time.time()} {step.tool or step.event} {detail}"
        )
    _print()
    grade = run.grade
    if grade is None:
        _print("not graded")
        return 1
    for check in grade.checks:
        _print(f" {'PASS' if check.passed else 'FAIL'} {check.name}: {check.detail}")
    _print()
    _print(
        f"score {grade.score:.2f}  passed={grade.passed}  failure_type={grade.failure_type or '-'}"
    )
    if args.trace:
        destination = run.trace.write_jsonl(Path(args.trace))
        _print(f"trace written to {destination}")
    return 0 if grade.passed else 2


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        msg = f"expected a date as YYYY-MM-DD, got {value!r}"
        raise argparse.ArgumentTypeError(msg) from None


def _report(record: RunRecord) -> None:
    if record.status == "error":
        _print(f"ERR  {record.run_id}: {record.error}")
        return
    grade = record.grade
    assert grade is not None
    mark = "pass" if grade.passed else "FAIL"
    detail = grade.failure_type or "-"
    _print(
        f"{mark} {record.run_id} score={grade.score:.2f} {detail} "
        f"steps={record.steps} stop={record.stop_reason} cost=${record.usage.cost_usd:.4f}"
    )


def _provider(model: str, given: str | None) -> str:
    return given or (model.split("/", 1)[0] if "/" in model else "unknown")


def _isolation(args: argparse.Namespace) -> Isolation | None:
    return Isolation(image=args.image) if args.isolation == "container" else None


def cmd_run(args: argparse.Namespace) -> int:
    """Run a model over a task set; resumable. Exit 1 if any run errored."""
    provider = _provider(args.model, args.provider)
    if (args.guard is None) != (args.backend is None):
        _print("--guard and --backend go together")
        return 2
    isolation = _isolation(args)
    if isolation is not None and args.guard is not None:
        _print(f"error: {GUARD_REFUSAL}")
        return 2
    if args.counterpart.startswith(FAKE_PREFIX):
        _print(f"error: --counterpart {args.counterpart}: use scripted or a LiteLLM model id")
        return 2
    guard = (
        GuardSetup.from_names(args.guard, args.backend, args.set)
        if args.guard is not None
        else None
    )
    if guard is not None and guard.policy.world not in (None, args.world):
        _print(f"policy {guard.policy.id} is for {guard.policy.world}, not {args.world}")
        return 2
    config = RunConfig(
        world=args.world,
        tasks=args.tasks,
        model=args.model,
        model_name=args.model_name or args.model,
        provider=provider,
        open_weight=args.open_weight,
        runs=args.runs,
        sample=args.sample,
        sample_seed=args.sample_seed,
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        max_cost=args.max_cost,
        temperature=args.temperature,
        retries=args.retries,
        guard=guard.config if guard is not None else None,
        counterpart=args.counterpart,
    )
    out = Path(args.out)
    try:
        manifest = execute(
            config,
            out,
            concurrency=args.concurrency,
            report=_report,
            guard=guard,
            isolation=isolation,
        )
    except IsolationError as error:
        _print(f"error: {error}")
        return 2
    status = manifest.status
    _print()
    _print(
        f"{out}: {status.completed}/{status.total} runs completed, "
        f"{status.errored} errored, {status.pending} pending"
    )
    records = load_records(out)
    if any(record.status == "completed" for record in records):
        for line in summary_lines(worlds.get(config.world), records):
            _print(line)
    if args.updated is not None and guard is not None:
        _print("guarded runs are not exported to the main leaderboard: see sandbox-train report")
    elif args.updated is not None:
        written = export([out], out / "export", args.updated)
        _print(f"leaderboard files: {len(written)} written under {out / 'export'}")
    return 1 if status.errored or status.pending else 0


def cmd_quickstart(args: argparse.Namespace) -> int:
    """Ten seeded public tasks, one run each, and the score. Resumable, like ``run``."""
    spec = worlds.get(args.world)
    try:
        model_factory(args.model)
    except ImportError as error:
        _print(f"cannot run {args.model}: {error}")
        return 2
    out = Path(args.out) if args.out else Path("runs") / "quickstart" / slug(args.model)
    config = RunConfig(
        world=spec.name,
        tasks=task_set_version(spec),
        model=args.model,
        model_name=args.model,
        provider=_provider(args.model, args.provider),
        sample=QUICKSTART_TASKS,
        sample_seed=0,
        max_cost=args.max_cost,
    )
    _print(f"{QUICKSTART_TASKS} tasks of {task_set_version(spec)} with {args.model}, into {out}")
    manifest = execute(config, out, concurrency=args.concurrency, report=_report)
    records = load_records(out)
    completed = [record for record in records if record.status == "completed"]
    _print()
    if completed:
        for line in summary_lines(spec, records):
            _print(line)
        overall = score(spec, completed).metrics["overall"]
        _print()
        _print(
            f"score: {overall['value']}% ± {overall['ci']} on {len(completed)} tasks "
            f"of {task_set_version(spec)} ({args.model})"
        )
    _print(f"check it: sandbox verify --run {out}")
    _print(
        f"the whole set: sandbox run --world {spec.name} --tasks {task_set_version(spec)} "
        f"--model {args.model} --runs 3 --out runs/<name>"
    )
    status = manifest.status
    return 1 if status.errored or status.pending else 0


def cmd_release_check(args: argparse.Namespace) -> int:
    """Every release condition, by name; exit 1 unless all pass."""
    report = release.release_check(
        args.task_set,
        repo=Path(args.repo),
        results=Path(args.results) if args.results else None,
        calibration=Path(args.calibration) if args.calibration else None,
        published=Path(args.published) if args.published else None,
        min_models=args.min_models,
        min_runs=args.min_runs,
    )
    for line in release.report_lines(report):
        _print(line)
    return 0 if report.ok else 1


def cmd_freeze(args: argparse.Namespace) -> int:
    """Write the task set's freeze file from the public tasks as they are now."""
    spec = release.world_of(args.task_set)
    path = release.write_freeze(spec)
    _print(f"{release.frozen_list(spec)['count']} tasks of {args.task_set} frozen in {path}")
    return 0


def cmd_submission_check(args: argparse.Namespace) -> int:
    """Check submitted run directories; exit 1 if any fails."""
    isolation = _isolation(args)
    if isolation is not None:
        try:
            preflight(isolation)
        except IsolationError as error:
            _print(f"error: {error}")
            return 2
    failed = 0
    for path in args.runs:
        checked = submission.check(
            Path(path), allow_fake=args.allow_fake, min_runs=args.min_runs, isolation=isolation
        )
        if checked.ok:
            _print(f"ok   {path}: {checked.model}, {checked.runs_verified} runs reproduced")
            continue
        failed += 1
        for problem in checked.problems:
            _print(f"FAIL {path}: {problem}")
    _print()
    _print(f"{len(args.runs) - failed}/{len(args.runs)} submission(s) pass")
    return 1 if failed else 0


def cmd_submission_fetch(args: argparse.Namespace) -> int:
    """Download and unpack a submission's archive; print its run directory alone, last."""
    try:
        run_dir = submission.fetch(Path(args.file), Path(args.out))
    except submission.SubmissionError as error:
        sys.stderr.write(f"FAIL {args.file}: {error}\n")
        return 1
    _print(str(run_dir))
    return 0


def cmd_submission_spot_check(args: argparse.Namespace) -> int:
    """Rerun a seeded sample of a submission's tasks and compare the pass rates."""
    run = Path(args.run)
    out = Path(args.out) if args.out else run.parent / f"{run.name}-spot-check-{args.seed}"
    result = submission.spot_check(
        run,
        out,
        sample=args.sample,
        seed=args.seed,
        tolerance=args.tolerance,
        concurrency=args.concurrency,
    )
    verdict = "ok" if result.ok else "FAIL"
    _print(
        f"{verdict} {run}: {len(result.tasks)} tasks (seed {args.seed}); submitted "
        f"{result.submitted}% on them, rerun {result.rerun}%, a drop of {result.drop} points "
        f"(tolerance {result.tolerance:g}); {result.errored} rerun(s) errored; reruns in {out}"
    )
    return 0 if result.ok else 1


def cmd_tools(args: argparse.Namespace) -> int:
    """Print a world's tool reference as Markdown."""
    sys.stdout.write(reference.markdown(worlds.get(args.world)))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Replay every completed run and confirm the same grades."""
    isolation = _isolation(args)
    if isolation is None:
        report = verify(Path(args.run))
    else:
        try:
            preflight(isolation)
            report = verify_isolated(Path(args.run), isolation)
        except (IsolationError, EpisodeFailed) as error:
            _print(f"error: {error}")
            return 2
    if args.json:
        _print(report_json(report))
        return 0 if report.ok and report.verdicts else 1
    for verdict in report.verdicts:
        if verdict.ok:
            _print(f"ok   {verdict.run_id}")
        else:
            for problem in verdict.problems:
                _print(f"DIFF {verdict.run_id}: {problem}")
    for run_id in report.skipped:
        _print(f"skip {run_id}: errored, never graded")
    reproduced = sum(verdict.ok for verdict in report.verdicts)
    _print()
    _print(f"{reproduced}/{len(report.verdicts)} runs reproduced their grades")
    return 0 if report.ok and report.verdicts else 1


def cmd_episode_serve(args: argparse.Namespace) -> int:
    """Inside an episode container: serve one task over stdio, then grade it."""
    return serve(Path(args.task), Path(args.out))


def cmd_export(args: argparse.Namespace) -> int:
    """Write worlds.json and each world's results, tasks and traces."""
    written = export([Path(path) for path in args.runs], Path(args.out), args.updated)
    _print(f"{len(written)} files written under {args.out}, all valid against results-v0.1")
    return 0


def cmd_stress_list(args: argparse.Namespace) -> int:
    """List each world's modifiers, which generic contract they implement, and aliases."""
    for name in [args.world] if args.world else registry.names():
        stress = registry.get(name)
        _print(f"{name}")
        for modifier in stress.modifiers.values():
            aliases = [a for a, target in stress.aliases.items() if target == modifier.name]
            kind = "generic" if modifier.generic else "world"
            also = f" (also: {', '.join(sorted(aliases))})" if aliases else ""
            _print(f"  {modifier.name:<26} {kind:<8} {modifier.summary}{also}")
        missing = sorted(set(GENERIC) - set(stress.modifiers) - set(stress.aliases))
        if missing:
            _print(f"  not implemented here: {', '.join(missing)}")
        _print()
    return 0


def cmd_stress_calibrate(args: argparse.Namespace) -> int:
    """Run base and stressed tasks for each model and write the calibration report."""
    settings = {
        key: getattr(args, key)
        for key in ("max_steps", "max_tokens", "max_cost", "temperature", "retries")
    }
    report = calibrate(
        args.world,
        args.model,
        Path(args.out),
        modifiers=args.modifier or (),
        per_modifier_count=args.per_modifier,
        seed=args.seed,
        runs=args.runs,
        concurrency=args.concurrency,
        settings=settings,
        report=_report,
    )
    _print()
    _print(calibration_markdown(report))
    _print(f"report written to {Path(args.out) / 'report.md'} and report.json")
    return 0


def cmd_stress_report(args: argparse.Namespace) -> int:
    """Rebuild the calibration report from its run directories."""
    report = write_report(Path(args.out))
    _print(calibration_markdown(report))
    return 0


def cmd_hidden_generate(args: argparse.Namespace) -> int:
    """Generate a hidden set, with its runs, into the private repository."""
    spec = worlds.get(args.world)
    root = Path(args.dir) if args.dir else hidden.private_root()
    made = hidden.generate(spec, args.count, args.seed, root)
    _print(
        f"{len(made.tasks)} hidden tasks written under {hidden.tasks_dir(spec, root)} "
        f"({made.skipped} candidates skipped); runs under {hidden.runs_dir(spec, root)}"
    )
    _print("Next: sandbox hidden hashes, then sandbox hidden check, then commit the hash list.")
    return 0


def cmd_hidden_hashes(args: argparse.Namespace) -> int:
    """Write the public hash list for the private set."""
    spec = worlds.get(args.world)
    path = hidden.write_hashes(spec, hidden.private_root())
    data = hidden.published(spec)
    _print(f"{data['count']} task hashes for {data['set']} written to {path}")
    _print(f"set hash {data['set_hash']}")
    return 0


def cmd_hidden_check(args: argparse.Namespace) -> int:
    """Check the private set: files, canaries, published hashes, and every run."""
    spec = worlds.get(args.world)
    report = hidden.check_set(spec)
    for problem in report.problems:
        _print(f"FAIL {problem}")
    _print(
        f"{hidden.set_name(spec)}: {report.tasks} tasks, {report.runs} runs played, "
        f"{len(report.problems)} problem(s); hashes match the published list"
    )
    return 1 if report.problems else 0


def cmd_hidden_scan(args: argparse.Namespace) -> int:
    """Search model output (files or run directories) for hidden-set canaries."""
    specs = [worlds.get(name) for name in worlds.names()]
    known = hidden.known_canaries(specs, hidden_tasks_dir())
    sightings = hidden.scan([Path(path) for path in args.paths], known)
    for found in sightings:
        where = found.hidden_set or "an unknown or retired set"
        _print(f"CANARY {found.path}:{found.line}: from {where}")
    _print(f"{len(sightings)} canary sighting(s)")
    return 1 if sightings else 0


def cmd_hidden_leaks(args: argparse.Namespace) -> int:
    """Fail if a task file under the paths is one of a published hidden set's tasks."""
    specs = [worlds.get(name) for name in worlds.names()]
    leaks = hidden.public_leaks(specs, [Path(path) for path in args.paths])
    for leak in leaks:
        _print(f"LEAK {leak}")
    _print(f"{len(leaks)} hidden task(s) found in the given paths")
    return 1 if leaks else 0


def _run_caps(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-steps", type=int, default=None, help="below the task's cap")
    parser.add_argument("--max-tokens", type=int, default=None, help="per run")
    parser.add_argument("--max-cost", type=float, default=None, help="USD per run")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--retries", type=int, default=5, help="per model call")


def _isolation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--isolation",
        choices=["none", "container"],
        default="none",
        help="container: run the world code in locked-down Docker containers",
    )
    parser.add_argument(
        "--image", default=IMAGE, help=f"the image for --isolation container (default {IMAGE})"
    )


def build_parser() -> argparse.ArgumentParser:
    """The ``sandbox`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="sandbox",
        description="Agent sandbox: run and inspect simulated worlds.",
    )
    parser.add_argument("--version", action="version", version=f"{DISTRIBUTION} {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    worlds_parser = subparsers.add_parser("worlds", help="list installed worlds")
    worlds_parser.add_argument("--json", action="store_true", help="machine-readable output")
    worlds_parser.set_defaults(func=cmd_worlds)

    tasks_parser = subparsers.add_parser("tasks", help="task file tools")
    tasks_sub = tasks_parser.add_subparsers(dest="tasks_command", required=True)
    validate_parser = tasks_sub.add_parser("validate", help="validate task files")
    validate_parser.add_argument("paths", nargs="*", help="directories to validate")
    validate_parser.set_defaults(func=cmd_tasks_validate)

    example_parser = subparsers.add_parser("example", help="run a scripted episode")
    example_parser.add_argument("--world", default="hello_world")
    example_parser.add_argument("--task", default=None, help="task id (default: the first)")
    example_parser.add_argument("--tasks", default=None, help="task directory override")
    example_parser.add_argument("--policy", default=DEFAULT_POLICY, help="module:callable")
    example_parser.add_argument("--seed", type=int, default=None)
    example_parser.add_argument("--trace", default=None, help="write the trace to this path")
    example_parser.set_defaults(func=cmd_example)

    run_parser = subparsers.add_parser("run", help="run a model over a task set")
    run_parser.add_argument("--world", required=True)
    run_parser.add_argument(
        "--model", required=True, help="a LiteLLM model id, or fake:golden|fake:wrong|fake:idle"
    )
    run_parser.add_argument(
        "--tasks",
        required=True,
        help="payments-v0.1, payments-v0.1-stressed, payments-v0.1-hidden, payments-v0.1-agents "
        "or a task directory",
    )
    run_parser.add_argument("--out", required=True, help="run directory; rerun to resume")
    run_parser.add_argument(
        "--counterpart",
        default=SCRIPTED,
        help="who plays the other agents of multi-agent tasks: scripted (the world's, the "
        "default) or a LiteLLM model id",
    )
    run_parser.add_argument("--runs", type=int, default=1, help="runs per task")
    run_parser.add_argument("--concurrency", type=int, default=1)
    run_parser.add_argument("--sample", type=int, default=None, help="run N tasks, seeded")
    run_parser.add_argument("--sample-seed", type=int, default=0)
    _run_caps(run_parser)
    run_parser.add_argument("--model-name", default=None, help="leaderboard name")
    run_parser.add_argument("--provider", default=None, help="default: the id's prefix")
    run_parser.add_argument("--open-weight", action="store_true")
    run_parser.add_argument(
        "--updated", type=_date, default=None, help="also export, dated YYYY-MM-DD"
    )
    run_parser.add_argument(
        "--guard", default=None, help="a policy pack (payments/default) or file: guard the run"
    )
    run_parser.add_argument("--backend", default=None, help="the guard's backend config")
    run_parser.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE", help="backend config field"
    )
    _isolation_options(run_parser)
    run_parser.set_defaults(func=cmd_run)

    verify_parser = subparsers.add_parser("verify", help="replay a run directory's actions")
    verify_parser.add_argument("--run", required=True, help="a sandbox run output directory")
    verify_parser.add_argument("--json", action="store_true", help="machine-readable output")
    _isolation_options(verify_parser)
    verify_parser.set_defaults(func=cmd_verify)

    episode_parser = subparsers.add_parser(
        "episode", help="inside an episode container (used by --isolation container)"
    )
    episode_sub = episode_parser.add_subparsers(dest="episode_command", required=True)
    serve_parser = episode_sub.add_parser("serve", help="serve one task over stdio, then grade")
    serve_parser.add_argument("--task", required=True, help="the task as JSON")
    serve_parser.add_argument("--out", required=True, help="where episode.json is written")
    serve_parser.set_defaults(func=cmd_episode_serve)

    export_parser = subparsers.add_parser("export", help="write leaderboard files")
    export_parser.add_argument("runs", nargs="+", help="run directories")
    export_parser.add_argument("--out", required=True, help="the leaderboard's data directory")
    export_parser.add_argument("--updated", type=_date, required=True, help="YYYY-MM-DD")
    export_parser.set_defaults(func=cmd_export)

    stress_parser = subparsers.add_parser("stress", help="stress modifiers and calibration")
    stress_sub = stress_parser.add_subparsers(dest="stress_command", required=True)
    list_parser = stress_sub.add_parser("list", help="list each world's modifiers")
    list_parser.add_argument("--world", default=None)
    list_parser.set_defaults(func=cmd_stress_list)
    calibrate_parser = stress_sub.add_parser("calibrate", help="score drop per modifier")
    calibrate_parser.add_argument("--world", required=True)
    calibrate_parser.add_argument(
        "--model", required=True, action="append", help="repeat for each model (2-3)"
    )
    calibrate_parser.add_argument("--out", required=True, help="calibration directory; resumable")
    calibrate_parser.add_argument("--modifier", action="append", help="default: all")
    calibrate_parser.add_argument("--per-modifier", type=int, default=10)
    calibrate_parser.add_argument("--seed", type=int, default=0)
    calibrate_parser.add_argument("--runs", type=int, default=1, help="runs per task")
    calibrate_parser.add_argument("--concurrency", type=int, default=1)
    _run_caps(calibrate_parser)
    calibrate_parser.set_defaults(func=cmd_stress_calibrate)
    report_parser = stress_sub.add_parser("report", help="rebuild a calibration report")
    report_parser.add_argument("out", help="a calibration directory")
    report_parser.set_defaults(func=cmd_stress_report)

    hidden_parser = subparsers.add_parser("hidden", help="the hidden task set")
    hidden_sub = hidden_parser.add_subparsers(dest="hidden_command", required=True)
    generate_parser = hidden_sub.add_parser("generate", help="write a hidden set, with runs")
    generate_parser.add_argument("--world", required=True)
    generate_parser.add_argument("--count", type=int, required=True)
    generate_parser.add_argument("--seed", type=int, required=True, help="the set's secret seed")
    generate_parser.add_argument("--dir", default=None, help="default: HIDDEN_TASKS_DIR")
    generate_parser.set_defaults(func=cmd_hidden_generate)
    hashes_parser = hidden_sub.add_parser("hashes", help="publish the set's task hashes")
    hashes_parser.add_argument("--world", required=True)
    hashes_parser.set_defaults(func=cmd_hidden_hashes)
    check_parser = hidden_sub.add_parser("check", help="check the private set and its runs")
    check_parser.add_argument("--world", required=True)
    check_parser.set_defaults(func=cmd_hidden_check)
    scan_parser = hidden_sub.add_parser("scan", help="search model output for canaries")
    scan_parser.add_argument("paths", nargs="+", help="files or run directories")
    scan_parser.set_defaults(func=cmd_hidden_scan)
    leaks_parser = hidden_sub.add_parser("leaks", help="no hidden task in these paths")
    leaks_parser.add_argument("paths", nargs="+", help="files or directories")
    leaks_parser.set_defaults(func=cmd_hidden_leaks)

    quick_parser = subparsers.add_parser("quickstart", help="ten tasks, one model, a score")
    quick_parser.add_argument("--model", required=True, help="a LiteLLM model id or fake:golden")
    quick_parser.add_argument("--world", default=QUICKSTART_WORLD)
    quick_parser.add_argument("--out", default=None, help="default: runs/quickstart/<model>")
    quick_parser.add_argument("--provider", default=None, help="default: the id's prefix")
    quick_parser.add_argument("--max-cost", type=float, default=None, help="USD per run")
    quick_parser.add_argument("--concurrency", type=int, default=1)
    quick_parser.set_defaults(func=cmd_quickstart)

    gate_parser = subparsers.add_parser("release-check", help="may this task set be released?")
    gate_parser.add_argument("task_set", help="e.g. payments-v0.1")
    gate_parser.add_argument(
        "--results", default=None, help="run directories of the models (outside git)"
    )
    gate_parser.add_argument(
        "--calibration", default=None, help="default: docs/calibration/<set>/report.json"
    )
    gate_parser.add_argument(
        "--published", default=None, help="leaderboard data; default: leaderboard/public/data"
    )
    gate_parser.add_argument("--repo", default=".", help="the repository checkout")
    gate_parser.add_argument("--min-models", type=int, default=release.MIN_MODELS)
    gate_parser.add_argument("--min-runs", type=int, default=release.MIN_RUNS)
    gate_parser.set_defaults(func=cmd_release_check)

    freeze_parser = subparsers.add_parser("freeze", help="freeze a task set's task content")
    freeze_parser.add_argument("task_set", help="e.g. payments-v0.1")
    freeze_parser.set_defaults(func=cmd_freeze)

    sub_parser = subparsers.add_parser("submission", help="submitted results")
    sub_sub = sub_parser.add_subparsers(dest="submission_command", required=True)
    sub_check = sub_sub.add_parser("check", help="validate and replay run directories")
    sub_check.add_argument("runs", nargs="+", help="submitted run directories")
    sub_check.add_argument("--min-runs", type=int, default=1, help="runs per task")
    sub_check.add_argument("--allow-fake", action="store_true", help="accept fake models")
    _isolation_options(sub_check)
    sub_check.set_defaults(func=cmd_submission_check)
    fetch_parser = sub_sub.add_parser("fetch", help="download a submission's run directory")
    fetch_parser.add_argument("file", help="submissions/<set>/<model>.json")
    fetch_parser.add_argument("--out", required=True, help="where to unpack it")
    fetch_parser.set_defaults(func=cmd_submission_fetch)
    spot_parser = sub_sub.add_parser("spot-check", help="rerun a sample with the same model")
    spot_parser.add_argument("run", help="a submitted run directory")
    spot_parser.add_argument("--sample", type=int, default=submission.SPOT_SAMPLE)
    spot_parser.add_argument("--seed", type=int, default=0, help="which tasks are sampled")
    spot_parser.add_argument("--tolerance", type=float, default=submission.SPOT_TOLERANCE)
    spot_parser.add_argument("--out", default=None, help="where the reruns go")
    spot_parser.add_argument("--concurrency", type=int, default=1)
    spot_parser.set_defaults(func=cmd_submission_spot_check)

    tools_parser = subparsers.add_parser("tools", help="a world's tool reference (Markdown)")
    tools_parser.add_argument("--world", required=True)
    tools_parser.set_defaults(func=cmd_tools)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``sandbox`` script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
