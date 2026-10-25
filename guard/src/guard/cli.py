"""``sandbox-guard``: check a policy, verify a decision log, run the MCP proxy.

- ``sandbox-guard policy list``: the packaged policy packs.
- ``sandbox-guard policy check <pack or file>``: validate a policy; print its id, version
  and hash; check that every world check it names exists.
- ``sandbox-guard verify <decisions.jsonl> [--head H] [--count N]``: verify a log's chain
  (and its head, which a removed last record shows against). Exit 1 on any problem.
- ``sandbox-guard proxy --policy P --backend B --world W --instruction TEXT --now T
  [--log FILE] [--fields module:callable] -- <server command> [args...]``: the guard in
  front of an MCP server started as a subprocess, on stdin/stdout. The decision records'
  time is ``--now`` (a fixed simulated time: the guard never reads the real clock). The
  policy fields come from ``--fields``, else from the world's own module, if it has one.
  With a world module, only its high-impact tools and the policy's tools are decided;
  without one, every other tool gets the policy's ``default``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from guard.actions import actions
from guard.engine import Guard, Outcome
from guard.policy import PolicyError, load_policy, packs
from guard.records import DecisionLog, read_log, verify_chain


def _print(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def _checks(world: str | None) -> dict[str, Any]:
    if world is None:
        return {}
    return {rule.id: rule for rule in actions.get(world).rules}


def cmd_policy_list(args: argparse.Namespace) -> int:
    """List the packs."""
    for name in packs():
        _print(name)
    return 0


def cmd_policy_check(args: argparse.Namespace) -> int:
    """Validate one policy."""
    try:
        policy = load_policy(args.source)
        Guard(policy, checks=_checks(policy.world))
    except (PolicyError, FileNotFoundError, KeyError) as error:
        _print(f"invalid: {error}")
        return 1
    _print(f"{policy.id} v{policy.version} ({policy.world or 'any world'})")
    _print(f"hash {policy.hash}")
    _print(f"tools: {', '.join(sorted(policy.actions))}; others: {policy.default}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify one decision log."""
    rows, problems = read_log(Path(args.log))
    problems += verify_chain(rows, head=args.head, count=args.count)
    for problem in problems:
        _print(problem)
    _print(f"{args.log}: {len(rows)} records, {'intact' if not problems else 'NOT intact'}")
    return 1 if problems else 0


def _callable(path: str) -> Callable[..., Any]:
    module_name, _, attribute = path.partition(":")
    if not module_name or not attribute:
        msg = f"expected module:callable, got {path!r}"
        raise ValueError(msg)
    found: Callable[..., Any] = getattr(importlib.import_module(module_name), attribute)
    return found


def cmd_proxy(args: argparse.Namespace) -> int:
    """Serve the proxy in front of a subprocess MCP server."""
    import anyio

    from guard.backends.config import build_backend, load_config
    from guard.mcp_proxy import serve_stdio
    from guard.wrap import request_from_call

    if not args.command:
        _print("give the upstream server's command after --")
        return 2
    policy = load_policy(args.policy)
    backend = build_backend(load_config(args.backend, args.set)) if args.backend else None
    log = DecisionLog(Path(args.log)) if args.log else None
    guard = Guard(policy, backend, checks=_checks(policy.world), log=log)
    now = dt.datetime.fromisoformat(args.now)
    fields: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = (
        _callable(args.fields) if args.fields else None
    )
    world = actions.get(args.world) if args.world in actions.names() else None
    guarded = set(policy.actions) | (set(world.high_impact) if world is not None else set())

    def check(tool: str, arguments: dict[str, Any]) -> Outcome | None:
        # A known world says which tools are high impact; without one, every tool the policy
        # does not name gets its default.
        if tool not in guarded and (world is not None or policy.default == "allow"):
            return None
        request = request_from_call(
            world=args.world, instruction=args.instruction, tool=tool, args=arguments, now=now
        )
        if fields is not None:
            given: Mapping[str, Any] | None = fields(arguments)
        elif world is not None:
            given = world.fields(request.model_dump(mode="json"))
        else:
            given = None
        return guard.decide(request, given)

    anyio.run(serve_stdio, args.command[0], list(args.command[1:]), check)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(prog="sandbox-guard", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command_name", required=True)

    policy = commands.add_parser("policy", help="policy packs and files")
    steps = policy.add_subparsers(dest="step", required=True)
    one = steps.add_parser("list", help="the packaged packs")
    one.set_defaults(handler=cmd_policy_list)
    one = steps.add_parser("check", help="validate a policy")
    one.add_argument("source", help="a pack name (payments/default) or a policy file")
    one.set_defaults(handler=cmd_policy_check)

    one = commands.add_parser("verify", help="verify a decision log's hash chain")
    one.add_argument("log")
    one.add_argument("--head", default=None, help="the last record's hash, kept elsewhere")
    one.add_argument("--count", type=int, default=None, help="how many records there were")
    one.set_defaults(handler=cmd_verify)

    one = commands.add_parser("proxy", help="the guard in front of a stdio MCP server")
    one.add_argument("--policy", required=True)
    one.add_argument("--backend", default=None, help="a backend config; default: hard rules only")
    one.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    one.add_argument("--world", required=True)
    one.add_argument("--instruction", required=True, help="what the user asked the agent")
    one.add_argument("--now", required=True, help="the simulated time, ISO 8601 with offset")
    one.add_argument("--log", default=None, help="append decision records to this file")
    one.add_argument(
        "--fields",
        default=None,
        help="module:callable, args -> policy fields (default: the world's)",
    )
    one.add_argument("command", nargs=argparse.REMAINDER, help="-- the server command")
    one.set_defaults(handler=cmd_proxy)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


__all__ = ["build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
