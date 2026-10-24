"""The world registers with the kit, and its reviewed schema files match the code."""

import json

from kit.failures import failure_types
from kit.world import worlds
from runner.cli import main
from worlds.security import FAILURE_TYPES, TOOLS_VERSION, VERSION, world_spec
from worlds.security.tools import SCHEMAS_FILE, TOOL_NAMES, build_router, schema_document

PROMPT_TOOLS = [
    "list_alerts", "get_alert", "search_logs", "timeline", "get_host", "get_user",
    "isolate_host", "disable_account", "reset_credentials", "block_indicator", "restore",
    "reimage_host", "update_ticket", "escalate", "submit_report", "ask_user",
]  # fmt: skip


def test_the_registry_finds_the_world_through_its_entry_point() -> None:
    assert "security" in worlds.names()
    spec = worlds.get("security")
    assert spec.version == VERSION == "v0.1"
    assert spec.categories == ["triage", "investigation", "containment", "reporting", "multi"]
    assert TOOLS_VERSION == "tools-security-v0.1"


def test_failure_types_are_namespaced_and_registered() -> None:
    ids = {definition.id for definition in FAILURE_TYPES}
    assert ids == {
        "security.missed_compromise",
        "security.false_positive_escalated",
        "security.false_containment",
        "security.over_containment",
        "security.evidence_destroyed",
        "security.late_escalation",
        "security.wrong_root_cause",
        "security.incomplete_report",
    }
    world_spec()
    assert set(failure_types.ids("security")) == ids
    assert failure_types.high_impact_ids("security") == {
        "security.missed_compromise",
        "security.false_containment",
        "security.evidence_destroyed",
        "security.late_escalation",
    }


def test_the_tools_are_the_prompts_sixteen_plus_collect_evidence() -> None:
    assert sorted(TOOL_NAMES) == sorted([*PROMPT_TOOLS, "collect_evidence"])
    assert build_router().names == TOOL_NAMES
    assert world_spec().tool_names == TOOL_NAMES


def test_the_reviewed_tool_schemas_match_the_code() -> None:
    assert SCHEMAS_FILE.read_text(encoding="utf-8") == schema_document(), (
        "tool schemas changed: review, then regenerate with\n"
        'uv run python -c "from worlds.security.tools import SCHEMAS_FILE, schema_document; '
        "SCHEMAS_FILE.write_text(schema_document(), encoding='utf-8')\""
    )
    names = [tool["name"] for tool in json.loads(SCHEMAS_FILE.read_text(encoding="utf-8"))]
    assert names == TOOL_NAMES


def test_sandbox_worlds_lists_security(capsys) -> None:
    assert main(["worlds"]) == 0
    assert "security" in capsys.readouterr().out
