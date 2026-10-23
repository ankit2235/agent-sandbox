"""The security tools: typed arguments, handlers, and the MCP server that serves them.

``schemas.json`` beside this file is the reviewed record of every tool's schema
(``tools-security-v0.1``, not yet frozen); a test fails if the code and the file
disagree.
"""

from pathlib import Path

from .handlers import TOOL_NAMES, TOOLS, build_router, schema_document

SCHEMAS_FILE = Path(__file__).with_name("schemas.json")

__all__ = ["SCHEMAS_FILE", "TOOLS", "TOOL_NAMES", "build_router", "schema_document"]
