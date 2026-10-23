"""OpenTelemetry spans: written locally for every run; OTLP and Langfuse only by env var."""

import base64
import importlib.util
import json

import pytest
from runner_testkit import call, scripted, turn

from runner.runs import execute
from runner.tracing import SPANS_FILE, langfuse_target, tracer_provider


def test_each_run_is_a_span_with_model_and_tool_children(tmp_path, hello_config) -> None:
    out = tmp_path / "out"
    execute(
        hello_config(),
        out,
        factory=scripted(turn(call("increment", {"by": 3}), call("nope")), turn(text="done")),
        env={},
    )
    spans = [json.loads(line) for line in (out / SPANS_FILE).read_text().splitlines()]
    by_name: dict[str, list[dict]] = {}
    for span in spans:
        by_name.setdefault(span["name"], []).append(span)
    [run] = by_name["sandbox.run"]
    assert run["attributes"]["sandbox.task_id"] == "hello-count-to-three"
    assert run["attributes"]["sandbox.status"] == "completed"
    assert len(by_name["model.complete"]) == 2
    assert [s["attributes"]["sandbox.tool"] for s in by_name["tool.call"]] == ["increment", "nope"]
    assert by_name["tool.call"][1]["attributes"]["sandbox.error_code"] == "unknown_tool"
    run_span = run["context"]["span_id"]
    assert all(s["parent_id"] == run_span for s in by_name["model.complete"] + by_name["tool.call"])


def test_langfuse_is_reached_over_otlp_with_basic_auth() -> None:
    assert langfuse_target({}) is None
    assert langfuse_target({"LANGFUSE_PUBLIC_KEY": "pk"}) is None
    endpoint, headers = langfuse_target(
        {
            "LANGFUSE_PUBLIC_KEY": "pk",
            "LANGFUSE_SECRET_KEY": "sk",
            "LANGFUSE_HOST": "https://lf.example/",
        }
    )
    assert endpoint == "https://lf.example/api/public/otel/v1/traces"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pk:sk").decode()
    assert headers["x-langfuse-ingestion-version"] == "4"


@pytest.mark.skipif(
    importlib.util.find_spec("opentelemetry.exporter") is not None,
    reason="the otlp extra is installed",
)
def test_exporting_without_the_otlp_extra_says_what_to_install(tmp_path) -> None:
    with pytest.raises(ImportError, match="--extra otlp"):
        tracer_provider(tmp_path, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid"})
    with pytest.raises(ImportError, match="--extra otlp"):
        tracer_provider(tmp_path, {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})
