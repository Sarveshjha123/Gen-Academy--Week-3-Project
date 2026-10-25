"""EVAL LAYER 3b: the TASK CARDS with the REAL sub-agents (Nebius models, Pinecone, Tavily). A scripted "Plant Head"
approves, requests changes or rejects, exactly like clicking the buttons.      python -m eval.eval_scenarios  (~10-15 min)
    python -m eval.eval_scenarios --ids AUS-04,DAL-05

Per scenario we score (code checks unless noted):
  recipient_ok      the email went to the right person (approval ladder / vendor)
  facts_ok          the exact amounts / PO numbers are in the email (e.g. the claim amount the contract-analyst found)
  subagents_ok      the expected sub-agents actually ran (from the run trace)
  quotes_verified   contract-analyst quotes found word-for-word in the contract (code check on its output)
  advisor_ok        after a REJECT, the policy-advisor recommended the step policy requires
  queue_ok          the task left the queue / the right follow-up task was created
  files_ok          draft versions in drafts/, the sent email in outbox/
  email_quality     LLM judge 1-5 (clear, correct, right tone for the recipient)
  latency_s         wall time incl. all sub-agents
Uses a temporary workspace, so your real queue is not touched. Results: eval/results/scenario_eval_report.json
"""
import argparse
import asyncio
import json
import shutil
import statistics
import tempfile
import time
from pathlib import Path

from src import config
from src.agent.engine import Steps, run_task
from src.agent.llm import judge_model, text_of
from src.agent.subagents import build_context
from src.core import tasks as T
from src.core.checks import read_json
from eval.common import pct, save

APPROVE = {"decision": "approve"}
RECOMMENDED = "pick the option the policy-advisor recommends"

# expected values come from the data (same numbers as the stand-in test eval/test_scenarios.py)
SCENARIOS = [
    dict(task="AUS-01", path="qty 40 -> transfer 8 -> vendor -> approve",
         answers=[{"choice": "set_qty", "value": 40}, {"choice": "transfer", "value": 8}, APPROVE, {"choice": "V05"}, APPROVE],
         to="rachel.stein", facts=["$60,400", "32", "P-110"],
         subagents=["contract-analyst", "market-researcher", "email-drafter + critic"],
         queue=lambda: any(t["type"] == "transfer_request" for t in T.load("DAL")), queue_note="transfer request in Marcus's queue"),
    dict(task=lambda: next((t["id"] for t in T.load("DAL") if t["type"] == "transfer_request" and t["status"] == "open"), None),
         label="DAL-transfer", path="Marcus declines Dana's transfer request",
         answers=[{"decision": "reject", "message": "We need our pumps for a rush order this week"}, {"choice": "decline"}, APPROVE],
         to="dana.whitfield", facts=["P-110", "8"], subagents=["policy-advisor", "email-drafter + critic"],
         queue=lambda: any(t["type"] == "pr_amend" for t in T.load("AUS")), queue_note="red 'amend purchase request' task for Dana"),
    dict(task=lambda: next((t["id"] for t in T.load("AUS") if t["type"] == "pr_amend" and t["status"] == "open"), None),
         label="AUS-amend", path="Dana amends her purchase request: 32 + 8 = 40",
         answers=[APPROVE], to="rachel.stein", facts=["32", "8", "40", "$75,440", "PR-"], subagents=["email-drafter + critic"],
         queue=lambda: any(t["type"] == "pr_amend" and t["status"] == "done" for t in T.load("AUS")), queue_note="amend task done"),
    dict(task="AUS-03", path="qty -> vendor -> request changes -> approve",
         answers=[{"choice": "set_qty", "value": 325}, {"choice": "V13"},
                  {"decision": "revise", "message": "Add that we can accept a split delivery if that helps the vendor"}, APPROVE],
         to="", facts=["325"], v2_must=["split"], subagents=["contract-analyst", "email-drafter + critic"],
         queue=lambda: T.get("AUS-03")["status"] == "done", queue_note="task done"),
    dict(task="AUS-04", path="approve", answers=[APPROVE], to="juniper", facts=["$1,764", "PO-24137"], subagents=["contract-analyst", "email-drafter + critic"],
         queue=lambda: any("credit note" in t["title"] for t in T.load("AUS")), queue_note="'check credit note' follow-up"),
    dict(task="DAL-05", path="reject -> waiver request", answers=[{"decision": "reject", "message": "Northstar is strategic, waive it"}, RECOMMENDED, APPROVE],
         to="rachel.stein", facts=["$1,065"], expect_option="waiver_request", subagents=["contract-analyst", "policy-advisor"],
         queue=lambda: T.get("DAL-05")["status"] in ("done", "closed"), queue_note="task finished"),
    dict(task="AUS-05", path="reject -> acceptance request", answers=[{"decision": "reject", "message": "Urgent volume, we accept the price this time"}, RECOMMENDED, APPROVE],
         to="rachel.stein", facts=["$2,600", "PO-24139"], expect_option="accept_request", subagents=["contract-analyst", "policy-advisor"],
         queue=lambda: T.get("AUS-05")["status"] in ("done", "closed"), queue_note="task finished"),
    dict(task="DAL-02", path="reject -> cancel and re-source", answers=[{"decision": "reject", "message": "No contract exceptions this quarter"}, RECOMMENDED, APPROVE],
         to="delta", facts=["PO-24145"], expect_option="cancel_resource", subagents=["contract-analyst", "policy-advisor"],
         queue=lambda: any(t["type"] == "restock" and t["params"].get("part_id") == "P-120" for t in T.load("DAL")), queue_note="re-source task for P-120"),
    dict(task="DAL-04", path="reject -> next step", answers=[{"decision": "reject", "message": "Their fill rate is not good enough to renew"}, RECOMMENDED, APPROVE],
         to="", facts=["2027-01-09"], subagents=["contract-analyst", "policy-advisor"],
         queue=lambda: T.get("DAL-04")["status"] in ("done", "closed", "snoozed"), queue_note="task finished"),
    dict(task="AUS-02", path="approve", answers=[APPROVE], to="ironwood", facts=["PO-24140"], subagents=["contract-analyst"],
         queue=lambda: any(t["type"] == "cancel_po" for t in T.load("AUS")), queue_note="'cancel if no ship date' follow-up"),
    dict(task="AUS-10", path="info -> done", answers=[{"choice": "done"}], to=None, facts=[], subagents=["market-researcher"],
         queue=lambda: T.get("AUS-10")["status"] == "done", queue_note="task done"),
]

QUALITY = """You review an email a Plant Head is about to send. Purpose: {purpose}
Facts gathered by the agents (the truth):
{facts}
EMAIL to {to}:
Subject: {subject}
{body}
Score 1-5: correct facts, clear ask, right tone for the recipient, concise (5 = ready to send as is).
JSON only: {{"score": n, "reason": "one sentence"}}"""


def human(answers, shown):
    it = iter(answers)

    def ask(payload):
        shown.append(payload)
        a = next(it, APPROVE)
        if a == RECOMMENDED:
            opts = payload.get("options", [])
            pick = next((o for o in opts if o.get("recommended")), opts[0] if opts else {"id": "snooze"})
            return {"choice": pick["id"]}
        return a
    return ask


def contains(text, group):
    return any(alt in (text or "") for alt in group.split("|"))


async def run_scenario(ctxs, sc):
    if callable(sc["task"]):                      # follow-up tasks created by an earlier scenario (ids known only at run time)
        found = sc["task"]()
        if not found:
            return {"task": sc["label"], "title": "(task not created)", "path": sc["path"], "error": "follow-up task was not created",
                    "latency_s": 0, "passed": False}
        sc = {**sc, "task": found}
    task = T.get(sc["task"])
    plant = task["plant_id"]
    if plant not in ctxs:
        ctxs[plant] = await build_context(plant)
    shown, t0 = [], time.time()
    steps = Steps(f"eval-{sc['task']}-{int(t0)}")
    error = None
    try:
        res = await run_task(task, ctxs[plant], human(sc["answers"], shown), steps)
    except Exception as e:  # report, keep going
        res, error = {}, f"{type(e).__name__}: {e}"
    latency = round(time.time() - t0, 1)
    approvals = [p for p in shown if p.get("type") == "approval"]
    choices = [p for p in shown if p.get("type") == "choice"]
    final = approvals[-1] if approvals else None
    agents = {t["agent"] for t in steps.trace}
    row = {"task": sc["task"], "title": task["title"], "path": sc["path"], "error": error, "latency_s": latency,
           "result": res.get("outcome"), "agents_used": sorted(agents)}
    if sc["to"] is not None:
        row["recipient_ok"] = bool(final) and (sc["to"] in final["email"]["to"].lower().replace(" ", "") if sc["to"] else True)
        row["facts_ok"] = bool(final) and all(contains(final["email"]["body"] + final["email"]["subject"], f) for f in sc["facts"])
    row["subagents_ok"] = all(a in agents for a in sc["subagents"])
    research = list((config.WORKSPACE / "research").glob("contract_*.json"))
    qcs = [json.loads(f.read_text()).get("quote_check", {}) for f in research]
    row["quotes_found"], row["quotes_checked"] = sum(q.get("found", 0) for q in qcs), sum(q.get("checked", 0) for q in qcs)
    for f in research:
        f.unlink()
    if sc.get("expect_option"):
        rec = next((o["id"] for c in choices for o in c.get("options", []) if o.get("recommended")), None)
        row["advisor_recommended"], row["advisor_ok"] = rec, rec == sc["expect_option"]
    if sc.get("v2_must"):
        row["revise_ok"] = len(approvals) >= 2 and approvals[1]["version"] == 2 and any(m in approvals[1]["email"]["body"] for m in sc["v2_must"])
    row["queue_ok"] = bool(sc["queue"]()) and sc["task"] not in [t["id"] for t in T.top5(plant)]
    row["queue_note"] = sc["queue_note"]
    if final:
        drafts = list((config.WORKSPACE / "drafts").glob(f"{sc['task']}_*_v*.md"))
        sent = list((config.WORKSPACE / "outbox").glob(f"{sc['task']}_*_email.txt"))
        row["files_ok"] = bool(drafts) and bool(sent)
        j = read_json(text_of(await judge_model().ainvoke(QUALITY.format(
            purpose=final["purpose"], facts=final["summary_md"][:5000], to=final["email"]["to"], subject=final["email"]["subject"],
            body=final["email"]["body"]))), {}) or {}
        row["email_quality"], row["quality_reason"] = j.get("score"), j.get("reason")
        row["email"] = final["email"]["body"][:2500]
        row["critic"] = final.get("critic")
    checks = [v for k, v in row.items() if k.endswith("_ok")]
    row["passed"] = not error and all(checks)
    return row


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    a = ap.parse_args()
    keep = set(a.ids.split(",")) if a.ids else None
    old = config.WORKSPACE
    config.WORKSPACE = Path(tempfile.mkdtemp())
    rows, ctxs = [], {}
    try:
        T.reset()
        for sc in SCENARIOS:
            if keep and (sc.get("label") or sc["task"]) not in keep:
                continue
            print(f"{sc.get('label') or sc['task']} [{sc['path']}] ...", flush=True)
            r = await run_scenario(ctxs, sc)
            rows.append(r)
            oks = " ".join(f"{k[:-3]}={'Y' if v else 'N'}" for k, v in r.items() if k.endswith("_ok"))
            print(f"   {'PASS' if r['passed'] else 'FAIL'} {oks} quality={r.get('email_quality')} {r['latency_s']}s "
                  f"{r.get('error') or ''}", flush=True)
    finally:
        shutil.rmtree(config.WORKSPACE, ignore_errors=True)
        config.WORKSPACE = old
    n = len(rows)

    def rate(key):
        xs = [r[key] for r in rows if key in r]
        return f"{pct(sum(xs) / len(xs))} of {len(xs)}" if xs else "n/a"
    q = [r["email_quality"] for r in rows if isinstance(r.get("email_quality"), (int, float))]
    summary = {"scenarios": n, "passed_all_checks": pct(sum(r["passed"] for r in rows) / max(1, n)),
               "right_recipient": rate("recipient_ok"), "right_facts_in_email": rate("facts_ok"), "subagents_ran": rate("subagents_ok"),
               "policy_advisor_right_next_step": rate("advisor_ok"), "request_changes_applied": rate("revise_ok"),
               "queue_moved_correctly": rate("queue_ok"), "files_saved": rate("files_ok"),
               "contract_quotes_verified": pct(sum(r["quotes_found"] for r in rows) / max(1, sum(r["quotes_checked"] for r in rows))),
               "email_quality_avg_1to5": round(statistics.mean(q), 2) if q else None,
               "median_latency_s": statistics.median(r["latency_s"] for r in rows) if rows else None,
               "models": {"sub-agents": config.HELPER_MODEL, "critic/judge": config.JUDGE_MODEL}}
    print("\n=== TASK SCENARIO EVAL (real sub-agents) ===")
    for k, v in summary.items():
        print(f"{k:34s} {v}")
    save("scenario_eval", rows, summary)


if __name__ == "__main__":
    asyncio.run(main())
