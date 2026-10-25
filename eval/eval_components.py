"""EVAL LAYER 2: the two AI helpers on their own, against known answers.
    python -m eval.eval_components            # both
    python -m eval.eval_components --only contracts|drafts
A) contract-analyst on all 15 contracts: are the extracted terms right? (truth = eval/truth/contract_terms.json)
   + are its quotes word-for-word in the contract (code check)?
B) email-drafter + critic on 5 purchase requests (same path as the task cards): right approver, AI email carries the
   real numbers (code check), the critic's catches, covers what policy section 7 requires (LLM judge 1-5).
"""
import argparse
import asyncio
import json
import statistics
import time

from src import config
from src.agent.llm import judge_model, text_of
from src.agent.subagents import Helpers
from src.agent.systems import load_system_tools, make_call
from src.agent.tools import build_tools
from src.core import workflows as W
from src.core.checks import read_json
from eval.common import pct, save


def num(x):
    try:
        return float(str(x).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def compare(terms: dict, truth: dict) -> dict:
    """Field-by-field comparison with the truth file. Returns {field: True/False}."""
    t = terms or {}
    g = lambda k, sub="value": (t.get(k) or {}).get(sub) if isinstance(t.get(k), dict) else None  # noqa: E731
    res = {"expiry_date": g("expiry_date") == truth["expiry_date"],
           "auto_renew": (g("auto_renew") in (True, "true", "True")) == truth["auto_renew"]}
    if truth["auto_renew"]:
        res["notice_days"] = num(g("auto_renew", "notice_days")) == truth["non_renewal_notice_days"]
    dp, tp = t.get("delay_penalty") or {}, truth["delay_penalty"]
    if tp is None:
        res["delay_none"] = bool(dp.get("none")) or not dp.get("rate_pct")
    else:
        res["delay_rate"] = num(dp.get("rate_pct")) == tp["rate_pct"]
        res["delay_per"] = str(dp.get("per", "")).lower() == tp["per"]
        res["delay_per_lenient"] = str(dp.get("per", "")).lower().startswith(tp["per"].split("_")[0])
        res["delay_grace"] = num(dp.get("grace_days")) == tp["grace_days"]
        res["delay_cap"] = num(dp.get("cap_pct")) == tp["cap_pct"]
    dm = t.get("damage") or {}
    res["damage_window"] = num(dm.get("claim_window_days")) == truth["damage_claim_window_days"]
    res["damage_replacement"] = num(dm.get("replacement_days")) == truth["damage_replacement_days"]
    res["return_freight"] = str(dm.get("return_freight_paid_by", "")).lower() == truth["return_freight_paid_by"]
    res["fill_rate"] = num(g("fill_rate_sla_pct")) == truth["fill_rate_sla_pct"]
    res["escalation_cap"] = num(g("price_escalation_cap_pct")) == truth["price_escalation_cap_pct"]
    res["payment_terms"] = str(g("payment_terms") or "").replace(" ", "").lower() == truth["payment_terms"].replace(" ", "").lower()
    return res


async def eval_contracts(helpers):
    truth = json.loads((config.EVAL_DIR / "truth/contract_terms.json").read_text())
    rows = []
    for vid, tr in truth.items():
        q = (f"Vendor {vid} ({tr['vendor_name']}). Summarise ALL key terms of the contract: expiry date, auto-renewal and notice "
             f"days, late-delivery penalty, damage and defect terms, fill rate, price escalation cap, payment terms.")
        t0 = time.time()
        res = await helpers.contract_analyst(q)
        data, qc = res["data"], res["quote_check"]
        answer = json.dumps(data)
        fields = compare(data.get("terms", {}), tr)
        strict = {k: v for k, v in fields.items() if k != "delay_per_lenient"}
        rows.append({"vendor_id": vid, "json_ok": bool(data), "field_accuracy": sum(strict.values()) / len(strict),
                     "fields": fields, "quotes_checked": qc["checked"], "quotes_found": qc["found"],
                     "latency_s": round(time.time() - t0, 1), "answer": answer[:3000]})
        print(f"  {vid}: fields {sum(strict.values())}/{len(strict)}  quotes {qc['found']}/{qc['checked']}  "
              f"wrong: {[k for k, v in strict.items() if not v]}", flush=True)
    per_field = {}
    for r in rows:
        for k, v in r["fields"].items():
            per_field.setdefault(k, []).append(v)
    summary = {"contracts": len(rows), "json_parsed": pct(sum(r["json_ok"] for r in rows) / len(rows)),
               "field_accuracy": pct(statistics.mean(r["field_accuracy"] for r in rows)),
               "quotes_word_for_word": pct(sum(r["quotes_found"] for r in rows) / max(1, sum(r["quotes_checked"] for r in rows))),
               "per_field": {k: pct(sum(v) / len(v)) for k, v in per_field.items()},
               "median_latency_s": statistics.median(r["latency_s"] for r in rows), "model": config.HELPER_MODEL}
    return rows, summary


DRAFT_JUDGE = """Policy section 7 says a purchase request must state: (1) part and quantity, (2) why it is needed (stock, usage, run-out date),
(3) vendor selected and why, (4) other vendors compared, (5) total cost incl. freight, (6) expected delivery date, (7) approval level.
EMAIL:
{email}
DRAFT FACTS: {facts}
Score 1-5 how completely and correctly the email covers the 7 items using the draft facts (5 = all correct). JSON only: {{"score": n, "missing": ["..."]}}"""


async def eval_drafts(helpers, call):
    """Same path as the task cards: code builds the purchase request, the drafter writes, the critic reviews, code checks."""
    from src.agent.drafter import check, draft_generic
    from src.agent.playbooks import restock_email
    ctx = {"user_name": "Dana Whitfield", "user_email": "dana.whitfield@northfield.example"}
    cases = [("AUS", "P-110"), ("AUS", "P-210"), ("HOU", "P-120"), ("DEN", "P-240"), ("PHX", "P-210")]
    rows = []
    for plant, part in cases:
        rec = await W.recommend_restock(call, part, plant)
        o = rec["vendor_options"][0]
        t0 = time.time()
        pr = await W.prepare_purchase_order(call, plant, part, o["vendor_id"], o["qty"], "ranked #1 by the restock workflow")
        expected = await call("get_approval_route", {"plant_id": plant, "total_usd": o["total_usd"], "vendor_risk_score": o["risk_score"],
                                                     "emergency": rec["stock"]["days_of_cover"] <= 7, "lowest_cost": o["lowest_cost"]})
        ep = restock_email(ctx, pr)
        email = await draft_generic(helpers, ep)
        body = email.get("email_body", "")
        crit = email.get("critic") or {}
        row = {"case": f"{plant} {part}", "written_by_ai": email["written_by"] == "ai", "ai_attempts": email.get("attempts"),
               "numbers_ok": check(ep, body) == [], "missing": check(ep, body),
               "critic_issues": crit.get("first_draft_issues"), "critic_fixed": crit.get("fixed"),
               "approver_matches_policy": pr["approval"]["approver_role"] == expected.get("approver_role"),
               "qty_as_asked": pr["qty"] == o["qty"]}
        j = read_json(text_of(judge_model().invoke(DRAFT_JUDGE.format(email=body, facts=json.dumps(
            {k: pr[k] for k in ("part_id", "qty", "vendor_name", "total_usd", "expected_delivery", "need", "approval")}, default=str)))), {}) or {}
        row.update(policy7_score=j.get("score", 1), policy7_missing=j.get("missing"), email=body[:2000],
                   latency_s=round(time.time() - t0, 1))
        rows.append(row)
        print(f"  {row['case']}: ai_email_passed_check={row['written_by_ai']} (attempts {row['ai_attempts']}) "
              f"approver_ok={row['approver_matches_policy']} policy7={row['policy7_score']}", flush=True)
    n = len(rows)
    summary = {"cases": n, "ai_email_passed_number_check": pct(sum(r["written_by_ai"] for r in rows) / n),
               "first_try_pass": pct(sum(r["written_by_ai"] and r["ai_attempts"] == 1 for r in rows) / n),
               "final_email_numbers_correct": pct(sum(r["numbers_ok"] for r in rows) / n),
               "approver_matches_policy": pct(sum(r["approver_matches_policy"] for r in rows) / n),
               "critic_flagged_first_draft": pct(sum(bool(r["critic_issues"]) for r in rows) / n),
               "policy7_avg_score_1to5": round(statistics.mean(r["policy7_score"] for r in rows), 2), "model": config.HELPER_MODEL}
    return rows, summary


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    st = await load_system_tools()
    call = make_call(st)
    helpers = Helpers(build_tools(st, call, "AUS", "eval-user"))
    if a.only in ("", "contracts"):
        print("A) contract-analyst on 15 contracts")
        rows, s = await eval_contracts(helpers)
        print(json.dumps(s, indent=1))
        save("contract_analyst_eval", rows, s)
    if a.only in ("", "drafts"):
        print("B) email-drafter + critic on 5 purchase requests")
        rows, s = await eval_drafts(helpers, call)
        print(json.dumps(s, indent=1))
        save("email_drafter_eval", rows, s)


if __name__ == "__main__":
    asyncio.run(main())
