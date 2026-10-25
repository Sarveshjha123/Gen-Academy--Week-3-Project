"""MCP SDK v1 calls the server class FastMCP; v2 renamed it MCPServer. Support both."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:          # so "from src..." works when a client starts this file directly
    sys.path.insert(0, str(ROOT))

try:
    from mcp.server.fastmcp import FastMCP as Server
except ImportError:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as Server


def as_json(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


def safe(fn):
    """Return a clear error message (as JSON) instead of crashing, so the agent can tell the user or retry."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, KeyError, IndexError) as e:
            return json.dumps({"error": str(e)})
    return wrapper
