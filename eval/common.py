"""Shared helpers for the AI evals: the LLM judge, LangSmith feedback, saving results."""
import csv
import json
import time

from src import config
from src.core.checks import read_json

JUDGE_PROMPT = """You are grading a procurement assistant. Be strict and fair.
QUESTION: {question}
EVIDENCE (what the tools returned; the only source of truth):
{evidence}
STANDING FACTS the assistant is given in its instructions (also valid evidence):
Approval ladder: up to $25,000 Junior Procurement Lead Kevin O'Neill; $25,000-$100,000 Procurement Head Rachel Stein; above $100,000
Procurement Head + Finance Head Thomas Grey; vendor risk >= 60 moves approval up one level; risk >= 75 blocked without a Procurement
Head exception. Emergency: part runs out within 7 days and would stop production -> Junior Procurement Lead may approve up to
$50,000, ratified by the Procurement Head within 48 hours. Vendor risk scores are simulated.
EXPECTED KEY FACTS (if any): {facts}
ANSWER:
{answer}

Grade:
- Listing every item a tool returned (e.g. all late orders) is correct, not "extra".
- correct: 1 if the answer correctly and completely addresses the question (saying 'I can't help' is correct for questions outside procurement; refusing is correct when the item does not exist, or when asked to take an action the assistant cannot take, such as approving, paying or changing data), else 0.
- grounded: 1 if every number, date, name and claim in the answer is supported by the EVIDENCE, else 0.
- helpful: 1-5 for a busy Plant Head (clear, actionable, right level of detail).
Reply with JSON only: {{"correct": 0 or 1, "grounded": 0 or 1, "helpful": 1-5, "reason": "one sentence"}}"""


def judge(question, answer, evidence, facts=""):
    from src.agent.llm import judge_model, text_of
    msg = JUDGE_PROMPT.format(question=question, answer=answer[:6000], evidence="\n---\n".join(evidence)[:60000] or "(none)", facts=facts or "(none)")
    for _ in range(2):
        try:
            out = read_json(text_of(judge_model().invoke(msg)), None)
            if out:
                return out
        except Exception as e:  # provider hiccup: try once more
            last = str(e)
            time.sleep(2)
    return {"correct": 0, "grounded": 0, "helpful": 1, "reason": "judge failed"}


def langsmith_feedback(run_id, scores: dict):
    """If LangSmith is configured, attach each score to the run's trace (visible in the LangSmith UI)."""
    if not (config._key("LANGSMITH_API_KEY") or config._key("LANGCHAIN_API_KEY")) or not run_id:
        return
    try:
        from langsmith import Client
        c = Client()
        for k, v in scores.items():
            if isinstance(v, (int, float, bool)):
                c.create_feedback(run_id, key=k, score=float(v))
    except Exception as e:
        print(f"   (LangSmith feedback skipped: {e})")


def save(name, rows, summary):
    out = config.EVAL_DIR / "results"
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    (out / f"{name}_report.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, default=str))
    if rows:
        keys = sorted({k for r in rows for k in r})
        with open(out / f"{name}_results.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows([{k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in r.items()} for r in rows])
    (out / f"{name}_{stamp}.json").write_text(json.dumps({"summary": summary}, indent=1, default=str))
    print(f"\nSaved eval/results/{name}_report.json and {name}_results.csv")


def pct(x):
    return f"{100 * x:.0f}%"
