#!/usr/bin/env python3
"""Fail on a known vulnerability in a dependency, unless it is accepted with a reason.

    python3 scripts/dependency_scan.py python                # uv audit over uv.lock (OSV)
    python3 scripts/dependency_scan.py npm guard/typescript  # npm audit over package-lock.json

CI runs both (``.github/workflows/dependency-scan.yml``) on dependency changes, on main and
every week, since new advisories appear without any change here. Accepted advisories are in
``.github/dependency-scan-ignores.toml``, by ecosystem, package, version and advisory id,
each with its reason. The scan fails on:

- an advisory that is not accepted;
- an accepted one that is no longer reported, or whose package is now at another version
  (the entry is stale: remove it or review it).

Both audits send the locked package names and versions to their advisory service; nothing
else. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IGNORES = ROOT / ".github" / "dependency-scan-ignores.toml"
UV_AUDIT = ["uv", "audit", "--frozen", "--output-format", "json"]
NPM_AUDIT = ["npm", "audit", "--json", "--package-lock-only"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One advisory against one locked package version."""

    ecosystem: str
    package: str
    version: str
    advisory: str
    fixed_in: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        """What an ignore entry names: ecosystem, package, version, advisory."""
        return (self.ecosystem, self.package.lower(), self.version, self.advisory)


def load_ignores(path: Path = IGNORES) -> set[tuple[str, str, str, str]]:
    """The accepted advisories. Every entry needs its package, version, ids and a reason."""
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("ignore", [])
    accepted: set[tuple[str, str, str, str]] = set()
    for number, entry in enumerate(entries, start=1):
        required = ("ecosystem", "package", "version", "ids", "reason")
        missing = [key for key in required if not entry.get(key)]
        if missing:
            msg = f"{path.name}: entry {number} has no {', '.join(missing)}"
            raise ValueError(msg)
        for advisory in entry["ids"]:
            accepted.add((entry["ecosystem"], entry["package"].lower(), entry["version"], advisory))
    return accepted


def python_findings(report: dict[str, Any]) -> list[Finding]:
    """Findings from ``uv audit --output-format json``."""
    return [
        Finding(
            "pypi",
            item["dependency"]["name"],
            item["dependency"]["version"],
            item["id"],
            ", ".join(item.get("fix_versions") or []),
        )
        for item in report["vulnerabilities"]
    ]


def npm_findings(report: dict[str, Any], lock: dict[str, Any]) -> list[Finding]:
    """Findings from ``npm audit --json``; versions come from ``package-lock.json``."""
    found: list[Finding] = []
    for name, entry in report.get("vulnerabilities", {}).items():
        version = lock.get("packages", {}).get(f"node_modules/{name}", {}).get("version", "?")
        fix = entry.get("fixAvailable")
        fixed_in = fix.get("version", "") if isinstance(fix, dict) else ""
        for via in entry.get("via", []):
            if isinstance(via, dict):  # a string names another package, reported on its own
                found.append(Finding("npm", name, version, via["url"].rsplit("/", 1)[-1], fixed_in))
    return found


def problems(
    findings: Iterable[Finding], accepted: set[tuple[str, str, str, str]], ecosystem: str
) -> list[str]:
    """Unaccepted findings, then stale entries of this ecosystem."""
    found = list(findings)
    seen = {finding.key for finding in found}
    listed = [
        f"{f.package} {f.version}: {f.advisory} (fixed in {f.fixed_in or 'no release yet'})"
        for f in found
        if f.key not in accepted
    ]
    stale = [
        f"stale ignore: {package} {version} {advisory} is no longer reported at that version"
        for kind, package, version, advisory in sorted(accepted)
        if kind == ecosystem and (kind, package, version, advisory) not in seen
    ]
    return listed + stale


def _audit(command: list[str], cwd: Path) -> dict[str, Any]:
    """Run an audit; it exits non-zero when it finds something, so only its JSON counts."""
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    try:
        report: dict[str, Any] = json.loads(done.stdout)
    except json.JSONDecodeError:
        msg = f"`{' '.join(command)}` printed no report (exit {done.returncode}): {done.stderr}"
        raise SystemExit(msg.strip()) from None
    return report


def main(argv: list[str] | None = None) -> int:
    """Run one ecosystem's audit; exit 1 on any problem."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ecosystem", choices=["python", "npm"])
    parser.add_argument("project", nargs="?", default=".", help="for npm: the package directory")
    args = parser.parse_args(argv)
    accepted = load_ignores()
    if args.ecosystem == "python":
        findings = python_findings(_audit(UV_AUDIT, ROOT))
        kind = "pypi"
    else:
        project = ROOT / args.project
        lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
        findings = npm_findings(_audit(NPM_AUDIT, project), lock)
        kind = "npm"
    found = problems(findings, accepted, kind)
    for line in found:
        sys.stdout.write(f"FAIL {line}\n")
    ok = len(findings) - sum(not line.startswith("stale") for line in found)
    sys.stdout.write(
        f"{args.ecosystem}: {len(findings)} known advisories, {ok} accepted with a reason "
        f"({IGNORES.relative_to(ROOT)}), {len(found)} problem(s)\n"
    )
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
