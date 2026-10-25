"""Connects the agent to the two company systems THROUGH MCP (Session 2).

MultiServerMCPClient starts each MCP server as a small local process (stdio) and turns its tools into LangChain tools.
`make_call` lets our code workflows call those same MCP tools (so the workflows also go through MCP).
Set USE_MCP=false in .env to skip MCP and call the same Python functions directly (a fallback if MCP gives trouble).
"""
import asyncio
import json
import sys

from src import config
from src.core.data import NotFound
from src.core.workflows import DIRECT, direct_call

SERVERS = {
    "plant_ops": {"command": sys.executable, "args": [str(config.ROOT / "src/mcp_servers/plant_ops_server.py")], "transport": "stdio"},
    "supplier": {"command": sys.executable, "args": [str(config.ROOT / "src/mcp_servers/supplier_server.py")], "transport": "stdio"},
}


def _content_to_text(result) -> str:
    if isinstance(result, tuple):          # (content, artifact)
        result = result[0]
    if isinstance(result, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", str(b)) for b in result)
    return getattr(result, "content", result) if not isinstance(result, str) else result


async def load_system_tools():
    """Returns {tool_name: LangChain tool}. Through MCP, or local wrappers of the same functions."""
    if config.USE_MCP:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        client = MultiServerMCPClient(SERVERS)
        tools = await client.get_tools()
        return {t.name: t for t in tools}
    import functools

    from langchain_core.tools import StructuredTool

    def wrap(name, fn):
        @functools.wraps(fn)          # keeps the argument names/types so the tool schema is correct
        def run(*args, **kwargs):
            try:
                return json.dumps(fn(*args, **kwargs), default=str)
            except (ValueError, KeyError, IndexError) as e:
                return json.dumps({"error": str(e)})
        return StructuredTool.from_function(func=run, name=name, description=(fn.__doc__ or name).strip())
    return {name: wrap(name, fn) for name, fn in DIRECT.items()}


def make_call(system_tools: dict):
    """A `call(name, args)` for the workflows. Goes through the MCP tools; returns Python dicts."""
    if not config.USE_MCP:
        return direct_call

    async def call(name, args):
        raw = await system_tools[name].ainvoke(args)
        data = json.loads(_content_to_text(raw))
        if isinstance(data, dict) and "error" in data and len(data) == 1:
            raise NotFound(data["error"])
        return data
    return call


_LOOP = None


def _background_loop():
    """One long-lived event loop in a background thread. Reusing it avoids 'event loop is closed' errors
    from HTTP clients that remember the loop they were created on (important for Streamlit)."""
    global _LOOP
    if _LOOP is None:
        import threading
        _LOOP = asyncio.new_event_loop()
        threading.Thread(target=_LOOP.run_forever, daemon=True, name="agent-loop").start()
    return _LOOP


def run_sync(coro):
    """Run async code from normal (sync) code, e.g. in Streamlit."""
    return asyncio.run_coroutine_threadsafe(coro, _background_loop()).result()
