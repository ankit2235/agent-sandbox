"""The security task file: where tasks live and the JSON Schema every file must match.

A task file is a kit :class:`~kit.types.TaskSpec` in YAML, at
``tasks/<category>/<id>.yaml`` inside this package. ``tasks/schema.json`` is
generated from the code below and reviewed like the tool schemas: a test fails
if the file and the code disagree.

What the schema adds to ``TaskSpec``: ``initial_state`` is a
:class:`~.state.SecurityState`, tools and categories are this world's, the
grader lives in ``worlds.security.graders``, every task offers ``ask_user``,
and ``expected_outcome`` and ``reviewer_signoff`` must be written out
(``reviewer_signoff: null`` until a named SOC reviewer signs).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kit.types import ID_RE, TaskSpec

from .state import SecurityState
from .tools import TOOL_NAMES

CATEGORIES = ["triage", "investigation", "containment", "reporting", "multi"]

TASKS_DIR = Path(__file__).parent / "tasks"
TASK_SCHEMA_FILE = TASKS_DIR / "schema.json"

GRADER_PATTERN = r"^worlds\.security\.graders\.[a-z_]+:[a-z_][a-z0-9_]*$"
REQUIRED = (
    "id",
    "version",
    "world",
    "category",
    "difficulty",
    "instruction",
    "initial_state",
    "allowed_tools",
    "max_steps",
    "grader",
    "seed",
    "expected_outcome",
    "reviewer_signoff",
)


def task_file_schema() -> dict[str, Any]:
    """JSON Schema for one security task file."""
    schema = TaskSpec.model_json_schema()
    state = SecurityState.model_json_schema()
    definitions: dict[str, Any] = schema.setdefault("$defs", {})
    for name, definition in state.pop("$defs", {}).items():
        if name in definitions:
            msg = f"schema definition {name!r} is defined twice"
            raise ValueError(msg)
        definitions[name] = definition
    definitions["SecurityState"] = state
    properties = schema["properties"]
    for name in ("id", "version"):
        properties[name]["pattern"] = ID_RE.pattern
    properties["world"] = {"const": "security"}
    properties["category"] = {"enum": CATEGORIES}
    properties["grader"] = {"type": "string", "pattern": GRADER_PATTERN}
    properties["initial_state"] = {"$ref": "#/$defs/SecurityState"}
    properties["instruction"]["minLength"] = 1
    properties["expected_outcome"] = {"type": "string", "minLength": 1}
    properties["allowed_tools"] = {
        "type": "array",
        "items": {"enum": TOOL_NAMES},
        "uniqueItems": True,
        "contains": {"const": "ask_user"},
    }
    schema["required"] = list(REQUIRED)
    schema["title"] = "Security task file"
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def schema_document() -> str:
    """The schema as canonical JSON, the form ``tasks/schema.json`` is kept in."""
    return json.dumps(task_file_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


__all__ = ["CATEGORIES", "TASKS_DIR", "TASK_SCHEMA_FILE", "schema_document", "task_file_schema"]
