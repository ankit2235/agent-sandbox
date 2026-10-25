"""The payments tools: typed arguments, handlers, and the MCP server that serves them.

``schemas.json`` beside this file is the reviewed record of every tool's schema;
a test fails if the code and the file disagree.
"""

from pathlib import Path

from .handlers import SELLER_TOOLS, TOOL_NAMES, TOOLS, build_router, schema_document

SCHEMAS_FILE = Path(__file__).with_name("schemas.json")

__all__ = ["SCHEMAS_FILE", "SELLER_TOOLS", "TOOLS", "TOOL_NAMES", "build_router", "schema_document"]
