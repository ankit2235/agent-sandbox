"""The shared test vectors, run by the Python SDK (the TypeScript SDK runs the same files).

The vectors are written by ``scripts/guard_vectors.py``, whose decision cases state their
expected answers by hand; a test here fails if the committed files drift from it.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import jsonschema
import pytest
import referencing

from guard.canonical import CanonicalError, canonical, digest
from guard.decision import BackendResult
from guard.policy import PolicyError, parse_policy
from guard.records import verify_chain

REPO = Path(__file__).resolve().parents[2]
VECTORS = REPO / "guard" / "test-vectors"
SCHEMAS = REPO / "guard" / "src" / "guard" / "schemas"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "guard_vectors", REPO / "scripts" / "guard_vectors.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _generator()


def load(name: str) -> dict:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


def test_the_committed_vectors_match_the_generator() -> None:
    for name, text in GENERATOR.build().items():
        assert (VECTORS / name).read_text(encoding="utf-8") == text, f"{name} is stale"


def test_canonical_json_and_hashes() -> None:
    data = load("canonical.json")
    for case in data["cases"]:
        assert canonical(case["value"]) == case["canonical"], case["name"]
        assert digest(case["value"]) == case["sha256"], case["name"]
    for case in data["invalid"]:
        with pytest.raises(CanonicalError):
            canonical(case["value"])


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), "\ud800", {1: "a"}, {"a": object()}, 10**400]
)
def test_values_without_a_canonical_form(value: object) -> None:
    with pytest.raises(CanonicalError):
        canonical(value)


def test_policies_hash_alike_and_invalid_ones_are_refused() -> None:
    data = load("policies.json")
    for case in data["valid"]:
        assert parse_policy(case["policy"]).hash == case["hash"], case["name"]
    for case in data["invalid"]:
        with pytest.raises(PolicyError):
            parse_policy(case["policy"])


@pytest.mark.parametrize("case", load("decisions.json")["cases"], ids=lambda case: case["name"])
def test_every_decision_vector(case: dict) -> None:
    inputs = {
        "request": case["request"],
        "fields": case["fields"],
        "delegation": case["delegation"],
        "pack_limits": case["pack_limits"],
        "history": [[h["tool"], h["amount"], h["sim_time"]] for h in case["history"]],
        "fires": case["fires"],
        "backend": case["backend"],
    }
    decision, layer, codes, confidence, record = GENERATOR.run_case(case["policy"], inputs)
    want = case["expected"]
    assert (decision, layer, codes, confidence) == (
        want["decision"],
        want["layer"],
        want["reason_codes"],
        want["confidence"],
    )
    assert record.inputs == want["inputs_hash"]
    assert record.policy.hash == want["policy_hash"]
    assert record.policy.delegation == want["delegation_hash"]


def test_every_chain_vector() -> None:
    for case in load("chains.json")["cases"]:
        problems = verify_chain(case["records"], head=case["head"], count=case["count"])
        assert (not problems) == case["ok"], (case["name"], problems)


def test_the_backend_result_schema_and_the_python_model_agree() -> None:
    schemas = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMAS.glob("*.schema.json")
    }
    registry = referencing.Registry().with_resources(
        (schema["$id"], referencing.Resource.from_contents(schema)) for schema in schemas.values()
    )
    validator = jsonschema.Draft202012Validator(
        schemas["backend-result.schema.json"], registry=registry
    )
    payloads = []
    for case in load("decisions.json")["cases"]:
        backend = case["backend"] or {}
        payloads += [backend[key] for key in ("result", "late", "invalid") if key in backend]
    assert len(payloads) > 15
    for payload in payloads:
        try:
            BackendResult.model_validate(payload)
            model_ok = True
        except ValueError:
            model_ok = False
        assert validator.is_valid(payload) == model_ok, payload
