"""EVAL LAYER 3a: the chat supervisor (Deep Agent), end to end, on 50 golden questions a Plant Head would really ask
(restock, transfers, inventory, vendors, contracts, risk, POs, policy, unknown items, actions it must refuse, out of scope).
    python -m eval.eval_agent                 # all
    python -m eval.eval_agent --ids C1,G1     # some
    python -m eval.eval_agent --repeat 2      # run each twice (consistency)
Scores per question:
  tools_ok      right tools / sub-agents used (trajectory, e.g. task:policy-advisor)   - code check
  no_bad_tool   did not call a forbidden tool                                            - code check
  facts         share of expected facts present in the answer                            - code check
  scope_ok      out-of-scope questions get "I can't help" and no tools; in-scope ones are answered - code check
  faithful      every number, date and ID in the answer is found in the tool results     - code check (after one repair)
  correct, grounded, helpful                                                             - LLM judge with the tool outputs as evidence
plus latency, LLM calls and tokens (cost). Scores are also attached to each LangSmith trace if LangSmith is on.
The task cards (draft -> approve / reject -> next step) are tested in eval_scenarios.py.
"""
import argparse
import asyncio
import csv
import statistics
import uuid

from src import config
from src.agent.subagents import build_context
from src.agent.supervisor import ask, build_supervisor, new_thread
from eval.common import judge, langsmith_feedback, pct, save


def any_of(group: str, items, text=False):
    alts = [a.strip() for a in group.split("|") if a.strip()]
    if text:
        from src.core.checks import _join_thousands, normalize_text
        low = normalize_text(items).lower()
        flat = _join_thousands(low).replace(",", "")
        return any(a.lower() in low or a.lower().replace(",", "") in flat for a in alts)
    return any(any(t == a or t.startswith(a + ":") for t in items) for a in alts)


async def run_one(agents, row):
    plant = row["plant_id"]
    if plant not in agents:
        agents[plant] = build_supervisor(await build_context(plant))
    run_id = str(uuid.uuid4())
    res = await ask(agents[plant], new_thread(), row["question"], run_id=run_id)
    tools, answer = list(res["tools_called"]), res["answer"]
    must = [g for g in row["must_call"].split(";") if g.strip()]
    must_not = [g for g in row["must_not_call"].split(";") if g.strip()]
    facts = [g for g in row["facts"].split(";") if g.strip()]
    expected_scope = (row.get("scope") or "in").split("|")
    got_scope = res["scope"]["scope"]
    ver = res.get("verification") or {"checked": 0, "unsupported": []}
    scored = {
        "tools_ok": all(any_of(g, tools) for g in must),
        "no_bad_tool": not any(any_of(g, tools) for g in must_not),
        "facts": (sum(any_of(g, answer, text=True) for g in facts) / len(facts)) if facts else 1.0,
        "scope_ok": (got_scope in expected_scope) and (got_scope != "out" or not tools),
        "faithful": not ver["unsupported"],
    }
    j = judge(row["question"], answer, res["tool_outputs"], row["facts"])
    scored.update(correct=int(j.get("correct", 0)), grounded=int(j.get("grounded", 0)), helpful=int(j.get("helpful", 1)))
    scored["passed"] = all([scored["tools_ok"], scored["no_bad_tool"], scored["facts"] == 1.0, scored["correct"], scored["scope_ok"],
                            scored["faithful"]])
    langsmith_feedback(run_id, scored)
    return {"id": row["id"], "category": row["category"], "question": row["question"], **scored, "judge_reason": j.get("reason"),
            "latency_s": res["latency_s"], "llm_calls": res["llm_calls"], "input_tokens": res["tokens"]["input"],
            "output_tokens": res["tokens"]["output"], "tools_called": tools, "error": res["error"], "answer": answer[:4000],
            "scope_got": got_scope, "values_checked": ver["checked"], "unsupported_values": ver["unsupported"],
            "repaired": ver.get("repaired", False)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()
    rows = list(csv.DictReader(open(config.EVAL_DIR / "golden_agent.csv")))
    if a.ids:
        keep = set(a.ids.split(","))
        rows = [r for r in rows if r["id"] in keep]
    agents, out = {}, []
    for rep in range(a.repeat):
        for r in rows:
            print(f"[{rep + 1}/{a.repeat}] {r['id']} {r['question'][:70]}...", flush=True)
            res = await run_one(agents, r)
            res["repeat"] = rep + 1
            out.append(res)
            flag = "PASS" if res["passed"] else "FAIL"
            print(f"   {flag} tools={res['tools_ok']} facts={res['facts']:.2f} scope={res['scope_got']} faithful={res['faithful']} "
                  f"correct={res['correct']} "
                  f"grounded={res['grounded']} {res['latency_s']}s  tools: {', '.join(res['tools_called'][:8])}")
    n = len(out)
    lat = [r["latency_s"] for r in out]
    summary = {
        "questions": n, "passed_all_checks": pct(sum(r["passed"] for r in out) / n),
        "right_tools": pct(sum(r["tools_ok"] for r in out) / n), "no_forbidden_tool": pct(sum(r["no_bad_tool"] for r in out) / n),
        "failure_rate": pct(1 - sum(r["passed"] for r in out) / n),
        "facts_found": pct(sum(r["facts"] for r in out) / n),
        "scope_handled_right": pct(sum(r["scope_ok"] for r in out) / n),
        "faithfulness_answers_fully_grounded": pct(sum(r["faithful"] for r in out) / n),
        "faithfulness_values_grounded": pct(1 - sum(len(r["unsupported_values"]) for r in out) / max(1, sum(r["values_checked"] for r in out))),
        "answers_fixed_by_repair_step": sum(r["repaired"] for r in out),
        "judge_correct": pct(sum(r["correct"] for r in out) / n), "judge_grounded": pct(sum(r["grounded"] for r in out) / n),
        "judge_helpful_avg": round(statistics.mean(r["helpful"] for r in out), 2),
        "median_latency_s": round(statistics.median(lat), 1), "p90_latency_s": round(sorted(lat)[int(0.9 * (n - 1))], 1),
        "avg_llm_calls": round(statistics.mean(r["llm_calls"] for r in out), 1),
        "avg_tokens": round(statistics.mean(r["input_tokens"] + r["output_tokens"] for r in out)),
        "models": {"supervisor": config.CHAT_MODEL, "helpers": config.HELPER_MODEL, "judge": config.JUDGE_MODEL},
        "use_mcp": config.USE_MCP,
    }
    if a.repeat > 1:
        by = {}
        for r in out:
            by.setdefault(r["id"], []).append(r["passed"])
        summary["consistent_across_repeats"] = pct(sum(len(set(v)) == 1 for v in by.values()) / len(by))
    cats = {}
    for r in out:
        cats.setdefault(r["category"].split("+")[0].split()[0], []).append(r["passed"])
    summary["pass_rate_by_category"] = {k: f"{sum(v)}/{len(v)}" for k, v in sorted(cats.items())}
    subs = [r for r in out if any(t.startswith("task:") for t in r["tools_called"])]
    summary["questions_that_used_a_subagent"] = f"{len(subs)}/{n}"
    print("\n=== AGENT EVAL SUMMARY ===")
    for k, v in summary.items():
        print(f"{k:28s} {v}")
    save("agent_eval", out, summary)


if __name__ == "__main__":
    asyncio.run(main())
