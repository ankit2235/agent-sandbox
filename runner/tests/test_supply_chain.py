"""The sandbox's own supply chain: dependency scanning, signed releases, the image.

Nothing here reaches the network: the scan script is fed canned audit reports, and the
workflows and the Dockerfile are read as files.
"""

import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
OWNER_GUARD = "github.repository == 'prarabdha-soni/agent-sandbox'"
ATTEST = "actions/attest@v4.2.2"


def load_script():
    spec = importlib.util.spec_from_file_location(
        "dependency_scan", REPO / "scripts/dependency_scan.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


scan = load_script()


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------- dependency scan
def uv_report(*items: tuple[str, str, str]) -> dict:
    return {
        "vulnerabilities": [
            {
                "dependency": {"name": name, "version": version},
                "id": advisory,
                "fix_versions": ["9"],
            }
            for name, version, advisory in items
        ]
    }


ACCEPTED = {("pypi", "pillow", "12.3.0", "GHSA-a"), ("npm", "ajv", "8.17.1", "GHSA-n")}


def test_an_advisory_not_accepted_fails_the_scan() -> None:
    findings = scan.python_findings(
        uv_report(("pillow", "12.3.0", "GHSA-a"), ("brotli", "1.2.0", "GHSA-b"))
    )
    assert scan.problems(findings, ACCEPTED, "pypi") == ["brotli 1.2.0: GHSA-b (fixed in 9)"]


def test_an_accepted_advisory_passes_and_a_stale_entry_fails() -> None:
    findings = scan.python_findings(uv_report(("Pillow", "12.3.0", "GHSA-a")))
    assert scan.problems(findings, ACCEPTED, "pypi") == []
    assert scan.problems([], ACCEPTED, "pypi") == [
        "stale ignore: pillow 12.3.0 GHSA-a is no longer reported at that version"
    ]


def test_an_accepted_advisory_at_another_version_needs_review() -> None:
    findings = scan.python_findings(uv_report(("pillow", "12.4.0", "GHSA-a")))
    assert scan.problems(findings, ACCEPTED, "pypi") == [
        "pillow 12.4.0: GHSA-a (fixed in 9)",
        "stale ignore: pillow 12.3.0 GHSA-a is no longer reported at that version",
    ]


def test_npm_findings_take_the_advisory_from_its_url_and_the_version_from_the_lock() -> None:
    report = {
        "vulnerabilities": {
            "ajv": {
                "via": [{"url": "https://github.com/advisories/GHSA-n"}],
                "fixAvailable": {"version": "8.20.0"},
            },
            "wrapper": {"via": ["ajv"], "fixAvailable": True},
        }
    }
    lock = {"packages": {"node_modules/ajv": {"version": "8.17.1"}}}
    (finding,) = scan.npm_findings(report, lock)
    assert (finding.package, finding.version, finding.advisory, finding.fixed_in) == (
        "ajv",
        "8.17.1",
        "GHSA-n",
        "8.20.0",
    )
    assert scan.problems([finding], ACCEPTED, "npm") == []


def test_an_ignore_entry_needs_a_reason(tmp_path) -> None:
    bad = tmp_path / "ignores.toml"
    bad.write_text('[[ignore]]\necosystem = "pypi"\npackage = "x"\nversion = "1"\nids = ["G"]\n')
    with pytest.raises(ValueError, match="no reason"):
        scan.load_ignores(bad)


def test_every_accepted_advisory_names_a_locked_version() -> None:
    """An entry for a version the lock no longer holds is stale before any scan runs."""
    entries = tomllib.loads(scan.IGNORES.read_text(encoding="utf-8"))["ignore"]
    assert entries and len(scan.load_ignores()) == sum(len(entry["ids"]) for entry in entries)
    locked = {
        (package["name"], package["version"])
        for package in tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))["package"]
        if "version" in package
    }
    npm = json.loads((REPO / "guard/typescript/package-lock.json").read_text(encoding="utf-8"))
    for entry in entries:
        if entry["ecosystem"] == "pypi":
            assert (entry["package"], entry["version"]) in locked, entry["package"]
        else:
            node = npm["packages"][f"node_modules/{entry['package']}"]
            assert node["version"] == entry["version"], entry["package"]


def test_the_scan_runs_in_ci_weekly_and_on_dependency_changes() -> None:
    flow = workflow("dependency-scan.yml")
    triggers = flow[True]  # YAML 1.1 reads the key `on` as True
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    assert "uv.lock" in triggers["pull_request"]["paths"]
    commands = [step.get("run", "") for job in flow["jobs"].values() for step in job["steps"]]
    assert "python3 scripts/dependency_scan.py python" in commands
    assert "python3 scripts/dependency_scan.py npm guard/typescript" in commands
    assert flow["permissions"] == {"contents": "read"}


# ---------------------------------------------------------- signed releases
def test_every_release_job_runs_only_on_the_owners_repository() -> None:
    jobs = workflow("release.yml")["jobs"]
    assert set(jobs) == {"build", "pypi", "image"}
    for name, job in jobs.items():
        assert job["if"] == OWNER_GUARD, name


def test_the_archives_and_the_image_are_signed_with_sigstore() -> None:
    jobs = workflow("release.yml")["jobs"]
    for name in ("build", "image"):
        permissions = jobs[name]["permissions"]
        for scope in ("id-token", "attestations", "artifact-metadata"):
            assert permissions[scope] == "write", (name, scope)
    build = [step for step in jobs["build"]["steps"] if step.get("uses") == ATTEST]
    assert [step["with"] for step in build] == [{"subject-path": "dist/*"}]
    steps = jobs["build"]["steps"]
    built = next(n for n, s in enumerate(steps) if s.get("run", "").startswith("uv build"))
    signed = next(n for n, s in enumerate(steps) if s.get("uses") == ATTEST)
    uploaded = next(n for n, s in enumerate(steps) if "upload-artifact" in s.get("uses", ""))
    assert built < signed < uploaded
    (image,) = [step for step in jobs["image"]["steps"] if step.get("uses") == ATTEST]
    assert image["with"] == {
        "subject-name": "ghcr.io/prarabdha-soni/agent-sandbox",
        "subject-digest": "${{ steps.push.outputs.digest }}",
        "push-to-registry": True,
    }
    (publish,) = [s for s in jobs["pypi"]["steps"] if "gh-action-pypi-publish" in s.get("uses", "")]
    assert publish["with"]["attestations"] is True
    assert jobs["pypi"]["permissions"]["id-token"] == "write"


# ------------------------------------------------------------------- image
def test_the_image_pins_its_bases_by_digest_and_runs_as_non_root() -> None:
    lines = (REPO / "Dockerfile").read_text(encoding="utf-8").splitlines()
    bases = [line.split()[1] for line in lines if line.startswith("FROM ")]
    assert len(bases) == 2
    for base in bases:
        assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", base), base
    (user,) = [line for line in lines if line.startswith("USER ")]
    assert user.split()[1].split(":")[0] not in ("0", "root")


def test_the_image_build_context_holds_every_workspace_member() -> None:
    members = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["uv"]["workspace"][
        "members"
    ]
    ignore = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for member in members:
        assert f"!{member.split('/')[0]}/" in ignore, member


# -------------------------------------------------------------- submissions
def test_submitted_traces_are_replayed_in_a_container_built_after_the_files_check() -> None:
    steps = workflow("results-submission.yml")["jobs"]["check"]["steps"]
    names = [step.get("name", step.get("uses", step.get("run", ""))) for step in steps]
    files_only = names.index("only submission files change")
    build = next(n for n, s in enumerate(steps) if "docker build" in s.get("run", ""))
    check = next(n for n, s in enumerate(steps) if "submission check" in s.get("run", ""))
    assert files_only < build < check
    assert "--isolation container" in steps[check]["run"]


# ----------------------------------------------------------------- contact
def test_security_md_names_no_invented_contact() -> None:
    text = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    assert "https://github.com/prarabdha-soni/agent-sandbox/security/advisories/new" in text
    assert "example.invalid" not in text
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), (
        "no email address until the owner names one"
    )
    assert "pending" in text.lower()
