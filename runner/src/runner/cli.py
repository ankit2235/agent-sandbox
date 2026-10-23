"""The ``sandbox`` command.

Phase 0 ships only what the kit can already do: list installed worlds,
validate task files, and run a scripted episode. ``tasks validate`` also checks
each file against its world's ``schema.json`` when the world ships one. The model-driven ``sandbox
run`` arrives with the runner in Phase 4.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from kit import __version__, worlds
from kit.scripted import run_script
from kit.tasks import TaskSource, load_task, load_tasks
from kit.types import TaskSpec, ToolCall
from kit.world import WorldSpec, load_grader

DEFAULT_POLICY = "kit.examples.hello_world.scripted:golden_run"
TASK_FILE_SCHEMA = "schema.json"
"""A world may ship a JSON Schema for whole task files beside its tasks."""


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
        _print()
    return 0


def _resolve_tasks(spec: WorldSpec, directory: str | None) -> TaskSource:
    if directory:
        return TaskSource(load_tasks(directory))
    return spec.task_source()


def _task_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("**/*.y*ml"))


def _world_schema(spec: WorldSpec) -> dict[str, Any] | None:
    """A world's whole-file task schema, if it ships one as ``<tasks_dir>/schema.json``."""
    if spec.tasks_dir is None:
        return None
    path = spec.tasks_dir / TASK_FILE_SCHEMA
    if not path.exists():
        return None
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def _validate_file(path: Path, root: Path) -> TaskSpec:
    """Every check one task file must pass. Raises on the first problem."""
    import jsonschema

    task = load_task(path)
    spec = worlds.get(task.world)
    spec.validate_task(task)
    load_grader(task.grader)
    schema = _world_schema(spec)
    if schema is not None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        # Validate the file as JSON sees it: YAML turns 2026-10-18 into a date object.
        jsonschema.validate(json.loads(json.dumps(raw, default=str)), schema)
    if path.stem != task.id:
        msg = f"file name {path.name} does not match the task id {task.id!r}"
        raise ValueError(msg)
    if path.parent != root and path.parent.name != task.category:
        msg = f"a {task.category!r} task must live in a {task.category}/ directory"
        raise ValueError(msg)
    return task


def cmd_tasks_validate(args: argparse.Namespace) -> int:
    """Validate task files: the kit's checks, the world's file schema, names and ids."""
    roots = [Path(path) for path in args.paths or []]
    if not roots:
        for name in worlds.names():
            tasks_dir = worlds.get(name).tasks_dir
            if tasks_dir is not None:
                roots.append(tasks_dir)
    files = [(path, root) for root in roots for path in _task_files(root)]
    if not files:
        _print("No task files found.")
        return 1
    failures = 0
    pending = 0
    seen: dict[str, Path] = {}
    for path, root in files:
        try:
            task = _validate_file(path, root)
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


def build_parser() -> argparse.ArgumentParser:
    """The ``sandbox`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="sandbox",
        description="Agent sandbox: run and inspect simulated worlds.",
    )
    parser.add_argument("--version", action="version", version=f"agent-sandbox {__version__}")
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``sandbox`` script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
