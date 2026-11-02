"""mcp-output-guard - keep oversized MCP tool output out of the agent context.

The public library entry point is :func:`limit_result`, which mirrors what the
``mcp-output-guard run`` proxy applies to every ``tools/call`` response.
"""

from __future__ import annotations

from .limits import DEFAULT_IMAGE_MAX_CHARS, ToolLimits, limit_result
from .spill import SpillStore, default_store

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_IMAGE_MAX_CHARS",
    "SpillStore",
    "ToolLimits",
    "__version__",
    "default_store",
    "limit_result",
]
