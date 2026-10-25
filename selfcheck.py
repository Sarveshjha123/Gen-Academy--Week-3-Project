"""RUN THIS FIRST:   python selfcheck.py
Checks every connection one by one and tells you exactly what to fix. Nothing is changed.
"""
import asyncio
import json
import os
import sys
import time

OK, BAD, SKIP = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m", "\033[93mSKIP\033[0m"
results = []


def show(name, status, msg=""):
    results.append(status)
    print(f"{status}  {name}" + (f"  -> {msg}" if msg else ""))


def main():
    print(f"Python {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        show("Python 3.11+", BAD, "Deep Agents needs Python 3.11 or newer. Create the venv with python3.11 or python3.12.")
    from src import config
    for key in ["NEBIUS_API_KEY", "PINECONE_API_KEY", "TAVILY_API_KEY", "MEM0_API_KEY", "LANGSMITH_API_KEY"]:
        val = config._key(key)
        show(f".env has {key}", OK if val else (BAD if key in ("NEBIUS_API_KEY", "PINECONE_API_KEY") else SKIP),
             "" if val else "missing (optional)" if key not in ("NEBIUS_API_KEY", "PINECONE_API_KEY") else "missing (required)")

    # 1. data + code rules
    from src.core import workflows as W
    r = asyncio.run(W.recommend_restock(W.direct_call, "P-110", "AUS"))
    show("Data + restock workflow", OK if r["vendor_options"] else BAD, r["recommendation"][:90])
    from src.core import tasks as T
    seed = json.loads((config.DATA_DIR / "tasks_seed.json").read_text())
    show("Task queues (10 tasks each for Dana / Marcus)", OK if sum(t["plant_id"] == "AUS" for t in seed) == 10
         and sum(t["plant_id"] == "DAL" for t in seed) == 10 else BAD, f"Dana's #1 today: {T.top5('AUS')[0]['title']}")

    # 2. MCP servers through LangChain's MCP adapter
    try:
        from src.agent.systems import load_system_tools
        t0 = time.time()
        tools = asyncio.run(load_system_tools())
        show(f"MCP servers ({len(tools)} tools, {time.time() - t0:.1f}s)", OK if len(tools) == 15 else BAD, ", ".join(sorted(tools))[:120])
        out = asyncio.run(tools["get_vendor_risk"].ainvoke({"vendor_id": "V09"}))
        show("MCP tool call get_vendor_risk(V09)", OK if "81" in str(out) else BAD, str(out)[:90])
    except Exception as e:
        show("MCP servers", BAD, f"{type(e).__name__}: {e}. Try USE_MCP=false in .env to continue without MCP.")

    # 3. Nebius model + tool calling
    try:
        from langchain_core.tools import tool

        from src.agent.llm import supervisor_model, text_of

        @tool
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b
        t0 = time.time()
        msg = supervisor_model().bind_tools([add]).invoke("Use the add tool to add 2 and 3.")
        show(f"Nebius {config.CHAT_MODEL} tool calling ({time.time() - t0:.1f}s)", OK if msg.tool_calls else BAD,
             str(msg.tool_calls)[:90] if msg.tool_calls else "model answered without calling the tool: " + text_of(msg)[:80])
    except Exception as e:
        show("Nebius model", BAD, f"{e}"[:200])

    # 4. Pinecone
    try:
        from pinecone import Pinecone
        stats = Pinecone(api_key=config.PINECONE_API_KEY).Index(config.PINECONE_INDEX).describe_index_stats()
        ns = {k: v["vector_count"] if isinstance(v, dict) else v.vector_count for k, v in (stats.get("namespaces") or {}).items()}
        good = ns.get("contracts", 0) > 0 and ns.get("policies", 0) > 0
        show(f"Pinecone index '{config.PINECONE_INDEX}'", OK if good else BAD, json.dumps(ns) if good else f"{ns}: run  python -m src.ingest")
        from src.agent.knowledge import search
        hit = search("policies", "what to do after a purchase request is rejected", {"plant_id": "GLOBAL"}, k=1)
        g_ok = bool(hit) and str(hit[0].get("doc_id", "")).startswith("POL-GLOBAL")
        show("Pinecone has the company-wide policies (contract + PO raising)", OK if g_ok else BAD,
             hit[0].get("doc_id") if g_ok else "not found: run  python -m src.ingest  again (new policies were added)")
    except Exception as e:
        show("Pinecone", BAD, f"{e}"[:160] + "  (run python -m src.ingest)")

    # 5. Tavily
    from src.agent import knowledge
    if config.TAVILY_API_KEY:
        out = knowledge.market_news("hydraulic pump supply chain")
        show("Tavily web search", BAD if '"error"' in out[:20] else OK, out[:90].replace("\n", " "))
    # 6. Mem0
    if config.MEM0_API_KEY:
        try:
            knowledge.remember("selfcheck-user", "Selfcheck: prefers faster delivery for critical parts.")
            time.sleep(2)
            got = knowledge.recall("selfcheck-user", "delivery")
            show("Mem0 memory (save + search)", OK if got else BAD, str(got)[:90] or "saved but not found yet (Mem0 may take a few seconds)")
        except Exception as e:
            show("Mem0", BAD, str(e)[:160])
    # 7. LangSmith
    if config._key("LANGSMITH_API_KEY"):
        show("LangSmith tracing", OK if os.getenv("LANGSMITH_TRACING", "").lower() == "true" else BAD,
             f"project {os.getenv('LANGSMITH_PROJECT', 'default')}" if os.getenv("LANGSMITH_TRACING") else "set LANGSMITH_TRACING=true")
    # 8. Deep Agents import
    try:
        import deepagents
        show(f"Deep Agents {getattr(deepagents, '__version__', '')}", OK)
    except Exception as e:
        show("Deep Agents", BAD, str(e)[:160])
    print(f"\n{results.count(OK)} passed, {results.count(BAD)} failed, {results.count(SKIP)} skipped")


if __name__ == "__main__":
    main()
