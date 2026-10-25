"""Decision records: an append-only, hash-chained log of every decision (``guard-record-v1``).

One record per decision, one JSON object per line. Each record holds the hash of the
request and fields it was decided on (never the request itself), the exact policy (id,
version, hash, and the delegation's hash), the backend and its model version, the
decision, its reason codes and confidence, which layer decided, the latency and the
simulated time. ``prev`` is the previous record's ``hash`` (64 zeros for the first);
``hash`` is the SHA-256 of the record's canonical JSON without ``hash``.

:func:`verify_chain` finds an edited, removed, inserted or reordered record. A removed
*last* record leaves a valid chain, so whoever keeps the log also keeps its head (the
last hash and the count: a sandbox run keeps them in ``result.json``) and passes them in.
These records become the trust layer's receipts later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from guard.canonical import digest
from guard.decision import DecisionLabel

FORMAT: Literal["guard-record-v1"] = "guard-record-v1"
GENESIS = "0" * 64
RECORD_SCHEMA: dict[str, Any] = json.loads(
    files("guard").joinpath("schemas/record.schema.json").read_text(encoding="utf-8")
)
HEX = re.compile(r"^[0-9a-f]{64}$")
_RECORD = jsonschema.Draft202012Validator(RECORD_SCHEMA)

Layer = Literal["hard", "backend", "fail_closed", "default", "user"]
"""Which layer decided: the hard rules, the backend, the backend failing closed, the policy's
default for a tool it does not name, or the user confirming an ask_user."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicyRef(_Strict):
    """The exact policy and delegation a decision was made under."""

    id: str
    version: int
    hash: str
    delegation: str | None = None
    """The hash of the delegated limits, when there were any."""


class ActionRef(_Strict):
    """The action decided on: its tool and the hash of its arguments."""

    tool: str
    args: str


class BackendRef(_Strict):
    """The backend asked, and the exact model that answered."""

    name: str
    model: str | None = None
    model_version: str | None = None


class Tokens(_Strict):
    """What the backend's call used, when it says."""

    input: int | None = None
    output: int | None = None


class DecisionRecord(_Strict):
    """One decision, as written to the log."""

    format: Literal["guard-record-v1"] = FORMAT
    seq: int = Field(ge=0)
    prev: str
    sim_time: str
    policy: PolicyRef
    inputs: str
    action: ActionRef
    backend: BackendRef | None = None
    layer: Layer
    decision: DecisionLabel
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    latency_ms: float = Field(ge=0)
    tokens: Tokens | None = None
    error: str | None = None
    hash: str


@dataclass(frozen=True, slots=True)
class Head:
    """Where a log ends: how many records, and the last one's hash."""

    count: int
    hash: str


def record_hash(body: dict[str, Any]) -> str:
    """The hash of a record: its canonical JSON without ``hash``."""
    return digest({key: value for key, value in body.items() if key != "hash"})


class DecisionLog:
    """An append-only log, kept in memory and, if given a path, appended to a JSONL file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: list[DecisionRecord] = []

    @property
    def head(self) -> Head | None:
        """The current head, or None while the log is empty."""
        if not self.records:
            return None
        return Head(count=len(self.records), hash=self.records[-1].hash)

    def append(self, body: dict[str, Any]) -> DecisionRecord:
        """Chain and write one record (``body`` without ``seq``, ``prev`` or ``hash``)."""
        chained = {
            "format": FORMAT,
            **body,
            "seq": len(self.records),
            "prev": self.records[-1].hash if self.records else GENESIS,
        }
        chained = DecisionRecord.model_validate({**chained, "hash": GENESIS}).model_dump(
            mode="json"
        )
        chained["hash"] = record_hash(chained)
        record = DecisionRecord.model_validate(chained)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(chained, ensure_ascii=False, sort_keys=True) + "\n")
        self.records.append(record)
        return record


def read_log(path: Path) -> tuple[list[Any], list[str]]:
    """Every line of a log file as parsed JSON, and a problem for each line that is not JSON."""
    rows: list[Any] = []
    problems: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            problems.append(f"line {number}: empty")
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            problems.append(f"line {number}: not JSON ({error.msg})")
    return rows, problems


def verify_chain(
    records: list[Any], *, head: str | None = None, count: int | None = None
) -> list[str]:
    """Every problem with a chain of records (parsed JSON); empty when it is intact."""
    problems: list[str] = []
    previous = GENESIS
    for index, raw in enumerate(records):
        error = jsonschema.exceptions.best_match(_RECORD.iter_errors(raw))
        if error is not None:
            problems.append(f"record {index}: not a {FORMAT} record ({error.message})")
            previous = raw.get("hash", GENESIS) if isinstance(raw, dict) else GENESIS
            continue
        if raw["seq"] != index:
            problems.append(f"record {index}: seq is {raw['seq']} (removed or reordered)")
        if raw["prev"] != previous:
            problems.append(f"record {index}: prev does not match the record before it")
        if record_hash(raw) != raw["hash"]:
            problems.append(f"record {index}: hash does not match its content (edited)")
        previous = raw["hash"]
    if count is not None and count != len(records):
        problems.append(f"the log has {len(records)} records; its head says {count}")
    if head is not None:
        last = records[-1].get("hash") if records and isinstance(records[-1], dict) else None
        if last != head:
            problems.append("the last record is not the head (records removed from the end)")
    return problems


__all__ = [
    "FORMAT",
    "GENESIS",
    "HEX",
    "RECORD_SCHEMA",
    "ActionRef",
    "BackendRef",
    "DecisionLog",
    "DecisionRecord",
    "Head",
    "Layer",
    "PolicyRef",
    "Tokens",
    "read_log",
    "record_hash",
    "verify_chain",
]
