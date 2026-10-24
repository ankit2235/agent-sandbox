"""``sandbox tools``: the reference matches what the agent is offered; the docs are current."""

from pathlib import Path

import pytest

from kit.world import worlds
from runner.cli import main
from runner.reference import markdown, tool_list

DOCS = Path(__file__).parents[2] / "docs" / "site" / "tools"


@pytest.mark.parametrize("world", ["payments", "security"])
def test_the_reference_lists_every_tool_the_world_declares(world: str) -> None:
    spec = worlds.get(world)
    tools = tool_list(spec)
    assert [tool["name"] for tool in tools] == spec.tool_names
    page = markdown(spec)
    for name in spec.tool_names:
        assert f"## `{name}`" in page


@pytest.mark.parametrize("world", ["payments", "security"])
def test_the_docs_pages_are_the_generated_reference(world: str) -> None:
    committed = (DOCS / f"{world}.md").read_text(encoding="utf-8")
    assert committed == markdown(worlds.get(world)), (
        f"docs/site/tools/{world}.md is stale: "
        f"uv run sandbox tools --world {world} > docs/site/tools/{world}.md"
    )


def test_the_payments_page_matches_the_reviewed_schema_file() -> None:
    import json

    spec = worlds.get("payments")
    assert spec.tasks_dir is not None
    reviewed = json.loads((spec.tasks_dir.parent / "tools" / "schemas.json").read_text())
    by_name = {tool["name"]: tool for tool in tool_list(spec)}
    for tool in reviewed:
        assert by_name[tool["name"]]["input_schema"] == tool["input_schema"]


def test_the_cli_prints_the_reference(capsys) -> None:
    assert main(["tools", "--world", "hello_world"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Tool reference: hello_world")
    assert "## `increment`" in out
