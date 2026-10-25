"""All settings in one place. Secrets come from your private .env file (never commit it)."""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _key(name):
    """A key from .env; the 'paste-your-...' placeholders count as missing."""
    v = os.getenv(name, "").strip()
    return None if not v or v.startswith("paste-") else v


if not _key("LANGSMITH_API_KEY"):          # no real LangSmith key -> switch tracing off quietly
    os.environ["LANGSMITH_TRACING"] = "false"

DATA_DIR = ROOT / "data"
CONTRACTS_DIR = ROOT / "docs" / "contracts"
POLICIES_DIR = ROOT / "docs" / "policies"
WORKSPACE = ROOT / "workspace"          # the agents' shared folder (drafts/, outbox/, research/)
EVAL_DIR = ROOT / "eval"

TODAY = date.fromisoformat(os.getenv("TODAY", "2026-10-26"))   # fixed "today" so answers match the data

# ---- Models (Nebius, OpenAI-compatible). Helpers can use a smaller, faster model.
LLM_API_KEY = _key("NEBIUS_API_KEY")
LLM_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.studio.nebius.com/v1/")
CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-120b")          # supervisor
HELPER_MODEL = os.getenv("HELPER_MODEL") or CHAT_MODEL                # sub-agents (try openai/gpt-oss-20b)
JUDGE_MODEL = os.getenv("JUDGE_MODEL") or CHAT_MODEL                  # evals: ideally a different model
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low")

# ---- Services
PINECONE_API_KEY = _key("PINECONE_API_KEY")
PINECONE_INDEX = os.getenv("PINECONE_INDEX", "northfield-procurement")
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384
NS_CONTRACTS, NS_POLICIES = "contracts", "policies"
CHUNK_SIZE, CHUNK_OVERLAP, TOP_K = 900, 120, 5

TAVILY_API_KEY = _key("TAVILY_API_KEY")
MEM0_API_KEY = _key("MEM0_API_KEY")
USE_MCP = os.getenv("USE_MCP", "true").lower() == "true"     # false = call the same functions directly (fallback)

# ---- Business rules (hard rules in code)
RISK_BLOCK = 75          # policy 4: >= 75 cannot be used without an exception
RISK_BUMP = 60           # policy 4: >= 60 moves approval up one level
COVER_WEEKS_AFTER_ARRIVAL = 6
NEARBY_MILES = 600
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "60"))    # recursion limit = stop runaway loops
DEFAULT_PLANT = "AUS"
