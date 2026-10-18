"""Traces.

A trace is the full record of an episode. Graders get a narrower view of it:
:meth:`Trace.grader_view` returns steps that structurally cannot carry the
agent's reasoning, so "graders never read the agent's thinking" is a type
rather than a convention.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kit.types import Strict, ToolError, Usage

StepKind = Literal["agent_message", "tool_call", "tool_result", "event"]
GraderStepKind = Literal["tool_call", "tool_result", "event"]


class DiffEntry(Strict):
    """One row a tool call created, changed or deleted, as the service reports it."""

    op: Literal["create", "update", "delete"]
    path: str
    before: Any = None
    after: Any = None


class GraderTraceStep(Strict):
    """A trace step with no field for the agent's reasoning."""

    index: int
    sim_time: dt.datetime
    kind: GraderStepKind
    tool: str | None = None
    args: dict[str, Any] | None = None
    response: Any = None
    error: ToolError | None = None
    event: str | None = None
    state_diff: list[DiffEntry] = Field(default_factory=list)


class TraceStep(Strict):
    """One step of an episode: a tool call, a scheduled event or an agent message."""

    index: int
    sim_time: dt.datetime
    kind: StepKind
    agent_message: str | None = None
    tool: str | None = None
    args: dict[str, Any] | None = None
    response: Any = None
    error: ToolError | None = None
    event: str | None = None
    state_diff: list[DiffEntry] = Field(default_factory=list)
    usage: Usage | None = None

    def grader_step(self) -> GraderTraceStep | None:
        """Return this step as a grader sees it, or ``None`` for an agent message."""
        if self.kind == "agent_message":
            return None
        return GraderTraceStep(
            index=self.index,
            sim_time=self.sim_time,
            kind=self.kind,
            tool=self.tool,
            args=self.args,
            response=self.response,
            error=self.error,
            event=self.event,
            state_diff=list(self.state_diff),
        )


class _StepQuery(BaseModel):
    """Shared read helpers over a list of steps."""

    model_config = ConfigDict(extra="forbid")

    def _steps(self) -> list[Any]:
        raise NotImplementedError

    def tool_calls(self, tool: str | None = None) -> list[Any]:
        """Every tool call, optionally only those to one tool."""
        return [
            step
            for step in self._steps()
            if step.kind == "tool_call" and (tool is None or step.tool == tool)
        ]

    def events(self, name: str | None = None) -> list[Any]:
        """Scheduled events that fired."""
        return [
            step
            for step in self._steps()
            if step.kind == "event" and (name is None or step.event == name)
        ]

    def count(self, tool: str) -> int:
        """How many times one tool was called."""
        return len(self.tool_calls(tool))

    def tools_used(self) -> set[str]:
        """The set of tool names the agent called."""
        return {step.tool for step in self.tool_calls() if step.tool is not None}


class GraderTrace(_StepQuery):
    """What a grader may read: the agent's actions and their effects, no reasoning."""

    run_id: str
    task_id: str
    world: str
    steps: list[GraderTraceStep] = Field(default_factory=list)

    def _steps(self) -> list[Any]:
        return list(self.steps)


class Trace(_StepQuery):
    """The full, ordered record of one episode."""

    run_id: str
    task_id: str
    world: str
    steps: list[TraceStep] = Field(default_factory=list)

    def _steps(self) -> list[Any]:
        return list(self.steps)

    @property
    def next_index(self) -> int:
        """The index the next appended step will get."""
        return len(self.steps)

    def add_agent_message(
        self, sim_time: dt.datetime, text: str, usage: Usage | None = None
    ) -> TraceStep:
        """Record what the model said. Never shown to graders."""
        return self._append(
            TraceStep(
                index=self.next_index,
                sim_time=sim_time,
                kind="agent_message",
                agent_message=text,
                usage=usage,
            )
        )

    def add_tool_call(
        self,
        sim_time: dt.datetime,
        tool: str,
        args: dict[str, Any],
        response: Any = None,
        error: ToolError | None = None,
        state_diff: list[DiffEntry] | None = None,
    ) -> TraceStep:
        """Record a tool call, its result and the rows it changed."""
        return self._append(
            TraceStep(
                index=self.next_index,
                sim_time=sim_time,
                kind="tool_call",
                tool=tool,
                args=args,
                response=response,
                error=error,
                state_diff=state_diff or [],
            )
        )

    def add_event(
        self,
        sim_time: dt.datetime,
        event: str,
        payload: dict[str, Any] | None = None,
        state_diff: list[DiffEntry] | None = None,
    ) -> TraceStep:
        """Record something the world did on its own, e.g. a limit change firing."""
        return self._append(
            TraceStep(
                index=self.next_index,
                sim_time=sim_time,
                kind="event",
                event=event,
                args=payload or {},
                state_diff=state_diff or [],
            )
        )

    def _append(self, step: TraceStep) -> TraceStep:
        self.steps.append(step)
        return step

    def grader_view(self) -> GraderTrace:
        """The same episode with every agent message removed."""
        steps = [step.grader_step() for step in self.steps]
        return GraderTrace(
            run_id=self.run_id,
            task_id=self.task_id,
            world=self.world,
            steps=[step for step in steps if step is not None],
        )

    def write_jsonl(self, path: str | Path) -> Path:
        """Write one JSON object per line: a header, then the steps."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        header = {"run_id": self.run_id, "task_id": self.task_id, "world": self.world}
        with target.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "header", **header}, sort_keys=True) + "\n")
            for step in self.steps:
                handle.write(step.model_dump_json() + "\n")
        return target

    @classmethod
    def read_jsonl(cls, path: str | Path) -> Trace:
        """Read a trace back, for ``sandbox verify`` and the trace viewer."""
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        if not lines:
            msg = f"{path} is empty"
            raise ValueError(msg)
        header = json.loads(lines[0])
        return cls(
            run_id=header["run_id"],
            task_id=header["task_id"],
            world=header["world"],
            steps=[TraceStep.model_validate_json(line) for line in lines[1:] if line.strip()],
        )
