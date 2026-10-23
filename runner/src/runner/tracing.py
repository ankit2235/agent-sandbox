"""OpenTelemetry spans for runs: always to a local file, optionally to OTLP or Langfuse.

Each run is a ``sandbox.run`` span with a ``model.complete`` child per model
call and a ``tool.call`` child per tool call. Spans are observability only:
the JSONL trace, not the spans, is the record graders and ``sandbox verify``
use, and nothing in an episode reads them.

Where spans go, decided by environment variables only:

- always: ``<out>/spans.jsonl``, one span per line;
- ``OTEL_EXPORTER_OTLP_ENDPOINT`` or ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``: an
  OTLP/HTTP collector, configured by the standard ``OTEL_*`` variables;
- ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` (and ``LANGFUSE_HOST``,
  default ``https://cloud.langfuse.com``): Langfuse's OTLP endpoint.

The last two need the ``otlp`` extra.
"""

from __future__ import annotations

import base64
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from kit import __version__

SPANS_FILE = "spans.jsonl"
TRACER_NAME = "agent-sandbox-runner"
LANGFUSE_DEFAULT_HOST = "https://cloud.langfuse.com"
LANGFUSE_PATH = "/api/public/otel/v1/traces"
OTLP_VARIABLES = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")


class JsonlSpanExporter(SpanExporter):
    """Appends each finished span to a file as one JSON line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Write the spans; one line each."""
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            for span in spans:
                handle.write(span.to_json(indent=None) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Nothing to release: the file is opened per batch."""


def langfuse_target(env: Mapping[str, str]) -> tuple[str, dict[str, str]] | None:
    """Langfuse's OTLP endpoint and headers, if its keys are set."""
    public, secret = env.get("LANGFUSE_PUBLIC_KEY"), env.get("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        return None
    host = env.get("LANGFUSE_HOST", LANGFUSE_DEFAULT_HOST).rstrip("/")
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return f"{host}{LANGFUSE_PATH}", {
        "Authorization": f"Basic {token}",
        "x-langfuse-ingestion-version": "4",
    }


def _otlp_exporter(**kwargs: Any) -> SpanExporter:
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as error:
        msg = (
            "span export to OTLP or Langfuse needs the otlp extra: "
            "uv sync --all-packages --extra otlp"
        )
        raise ImportError(msg) from error
    exporter: SpanExporter = OTLPSpanExporter(**kwargs)
    return exporter


def tracer_provider(out_dir: Path, env: Mapping[str, str] | None = None) -> TracerProvider:
    """A provider for one ``sandbox run``: the local file, plus any exporter the env asks for.

    A provider of its own rather than the global one, so two runs in one process
    (as in the tests) never share or re-set global state.
    """
    environ = os.environ if env is None else env
    provider = TracerProvider(
        resource=Resource.create({"service.name": TRACER_NAME, "service.version": __version__})
    )
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(out_dir / SPANS_FILE)))
    if any(environ.get(name) for name in OTLP_VARIABLES):
        provider.add_span_processor(BatchSpanProcessor(_otlp_exporter()))
    langfuse = langfuse_target(environ)
    if langfuse is not None:
        endpoint, headers = langfuse
        provider.add_span_processor(
            BatchSpanProcessor(_otlp_exporter(endpoint=endpoint, headers=headers))
        )
    return provider


__all__ = ["SPANS_FILE", "JsonlSpanExporter", "langfuse_target", "tracer_provider"]
