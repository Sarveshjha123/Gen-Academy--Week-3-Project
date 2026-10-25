"""The brains. Nebius speaks the OpenAI 'chat completions' language, so we use ChatOpenAI with Nebius's address.
Three roles can use different models (set in .env): supervisor, helpers (sub-agents), judge (evals).
"""
from functools import lru_cache

from langchain_openai import ChatOpenAI

from src import config


@lru_cache(maxsize=None)
def chat_model(model_name: str, max_tokens: int = 4000) -> ChatOpenAI:
    extra = {"extra_body": {"reasoning_effort": config.REASONING_EFFORT}} if config.REASONING_EFFORT else {}
    return ChatOpenAI(model=model_name, api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL,
                      temperature=0, seed=42, max_tokens=max_tokens, use_responses_api=False, **extra)


def supervisor_model():
    return chat_model(config.CHAT_MODEL, 6000)


def helper_model():
    return chat_model(config.HELPER_MODEL, 4000)


def judge_model():
    return chat_model(config.JUDGE_MODEL, 2000)


def text_of(message) -> str:
    """Plain text from an AI message (content can be a string or a list of blocks)."""
    c = getattr(message, "content", message)
    if isinstance(c, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return str(c or "")


from src.core.checks import read_json  # noqa: E402,F401  (re-exported)
