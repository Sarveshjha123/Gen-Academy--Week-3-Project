"""Pinecone (search over contracts and plant policies), Tavily (market news) and Mem0 (long-term memory).
Each has a clear fallback message if its key is missing, so the app still runs.
"""
import json
from functools import lru_cache

from src import config

# ================================================================ PINECONE (RAG)


@lru_cache(maxsize=1)
def _embeddings():
    from langchain_huggingface import HuggingFaceEmbeddings
    return HuggingFaceEmbeddings(model_name=config.EMBED_MODEL)


@lru_cache(maxsize=2)
def _store(namespace: str):
    from langchain_pinecone import PineconeVectorStore
    from pinecone import Pinecone
    index = Pinecone(api_key=config.PINECONE_API_KEY).Index(config.PINECONE_INDEX)
    return PineconeVectorStore(index=index, embedding=_embeddings(), namespace=namespace, text_key="text")


def search(namespace: str, query: str, flt: dict | None = None, k: int = config.TOP_K) -> list[dict]:
    if not config.PINECONE_API_KEY:
        return [{"error": "PINECONE_API_KEY missing in .env; run python -m src.ingest after adding it."}]
    hits = _store(namespace).similarity_search_with_score(query, k=k, filter=flt or None)
    return [{"chunk_id": d.metadata.get("chunk_id"), "doc_id": d.metadata.get("doc_id"),
             "section": d.metadata.get("section"), "score": round(float(s), 3), "text": d.page_content}
            for d, s in hits]


def search_contracts(query: str, vendor_id: str = "") -> str:
    """Search vendor Master Supply Agreements (Pinecone). Give vendor_id (like V10) to search one vendor's contract.
    Returns the most relevant clauses with their chunk_id, so you can quote them."""
    from src.core.data import vendor
    flt = None
    if vendor_id:
        try:
            flt = {"vendor_id": vendor(vendor_id)["vendor_id"]}
        except ValueError as e:
            return json.dumps({"error": str(e)})
    return json.dumps(search(config.NS_CONTRACTS, query, flt), indent=1)


def search_policy(query: str, plant_id: str = "") -> str:
    """Search procurement policies (Pinecone): the plant's approval policy + company-wide Contract Management and
    Purchase Order Raising policies. Give plant_id (like AUS) to include that plant's policy."""
    from src.core.data import plant
    flt = None
    if plant_id:
        try:
            flt = {"plant_id": {"$in": [plant(plant_id)["plant_id"], "GLOBAL"]}}
        except ValueError as e:
            return json.dumps({"error": str(e)})
    return json.dumps(search(config.NS_POLICIES, query, flt, k=5), indent=1)


# ================================================================ TAVILY (web search)

def market_news(query: str) -> str:
    """Recent web news (Tavily) about a market, commodity, region or supply-chain topic, e.g.
    'hydraulic pump supply shortage 2026' or 'steel price trend October 2026'. Returns titles, links and snippets."""
    if not config.TAVILY_API_KEY:
        return json.dumps({"error": "TAVILY_API_KEY missing in .env, so no web search was done."})
    from tavily import TavilyClient
    try:
        res = TavilyClient(api_key=config.TAVILY_API_KEY).search(query, topic="news", max_results=5, time_range="month")
    except Exception as e:  # network / quota problems should not crash the agent
        return json.dumps({"error": f"Tavily search failed: {e}"})
    return json.dumps([{"title": r.get("title"), "url": r.get("url"), "published": r.get("published_date"),
                        "snippet": (r.get("content") or "")[:500]} for r in res.get("results", [])], indent=1)


# ================================================================ MEM0 (long-term memory)

def _local_file(user_id):
    p = config.WORKSPACE / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{user_id}.json"


@lru_cache(maxsize=1)
def _mem0():
    if not config.MEM0_API_KEY:
        return None
    from mem0 import MemoryClient
    return MemoryClient(api_key=config.MEM0_API_KEY)


def remember(user_id: str, fact: str) -> dict:
    """Save one fact or preference (Mem0; local file if no Mem0 key)."""
    client = _mem0()
    if client:
        res = client.add([{"role": "user", "content": fact}], user_id=user_id, infer=False,
                         metadata={"app": "morning-procurement-desk"})
        return {"saved": True, "where": "mem0", "result": str(res)[:200]}
    f = _local_file(user_id)
    items = json.loads(f.read_text()) if f.exists() else []
    items.append(fact)
    f.write_text(json.dumps(items, indent=1))
    return {"saved": True, "where": "local file (add MEM0_API_KEY to use Mem0)"}


def recall(user_id: str, query: str = "") -> list[str]:
    """Memories for this user: the most relevant to `query`, or all of them."""
    client = _mem0()
    if client:
        try:
            res = client.search(query, filters={"user_id": user_id}, top_k=8) if query else \
                client.get_all(filters={"user_id": user_id}, page_size=50)
        except Exception as e:
            return [f"(memory unavailable: {e})"]
        rows = res.get("results", res) if isinstance(res, dict) else res
        return [r.get("memory", str(r)) for r in rows]
    f = _local_file(user_id)
    items = json.loads(f.read_text()) if f.exists() else []
    if not query:
        return items
    words = [w for w in query.lower().split() if len(w) > 3]
    return [i for i in items if any(w in i.lower() for w in words)] or items


def load_priorities(plant_id: str) -> dict:
    """Ranking priorities (cost / speed / risk weights) used by the scoring code. HARDCODED in the backend for now
    (src/core/workflows.py DEFAULT_PRIORITIES); a per-user priorities editor is listed as a future expansion."""
    from src.core.workflows import DEFAULT_PRIORITIES
    return dict(DEFAULT_PRIORITIES)
