"""PLAYBOOKS: one per task type. Each says
  prepare(task)          which SUB-AGENTS + code gather the facts, and the email to draft (or none for info tasks)
  on_approve             what happens after the email is approved and sent (task done, follow-up tasks)
  reject_options         what the Plant Head may do instead (the policy-advisor recommends one)
  next_email / on_next_approved   the email for a chosen next step, and what happens when that is approved

Sub-agents used: contract-analyst (Pinecone contracts), policy-advisor (Pinecone policies), market-researcher (Tavily),
email-drafter + critic. Numbers, dates, approvers and rules are ALWAYS computed in code.
"""
import asyncio
import math
import re
from datetime import date, timedelta

from src import config
from src.core import tasks as T
from src.core import workflows as W
from src.core.checks import contract_clause, parse_delay_clause, verify_contract_quotes
from src.core.data import money, part as part_info, table, vendor as vendor_info

BUSINESS_2_DAYS = (config.TODAY + timedelta(days=2)).isoformat()


# ================================================================ helpers
def person(role, plant_id=None):
    p = table("people")
    rows = p[(p.role == role) & ((p.plant_id == plant_id) if plant_id else True)]
    r = rows.iloc[0]
    return {"name": r["name"], "email": r["email"], "role": role}


def fmt(p):
    return f"{p['name']} <{p['email']}>"


def vendor_contact(v_name):
    slug = re.sub(r"[^a-z0-9]+", "", v_name.lower().replace("&", "and"))
    return {"name": f"{v_name} team", "email": f"orders@{slug}.example", "role": "vendor"}


def email_plan(ctx, to, purpose, purpose_sentence, facts, must_include, checklist, subject, lines, form_file=None, amount="",
               recipient_role=""):
    to = to if isinstance(to, list) else [to]
    first = [p["name"].split()[0] if p["role"] != "vendor" else p["name"] for p in to]
    must = list(must_include) + [n for n, p in zip(first, to) if p["role"] != "vendor"]
    return {"from": f"{ctx['user_name']} <{ctx['user_email']}>", "from_name": ctx["user_name"], "to": ", ".join(fmt(p) for p in to),
            "to_names": first, "recipient_role": recipient_role or " + ".join(p["role"] for p in to), "purpose": purpose,
            "purpose_sentence": purpose_sentence, "facts": facts, "must_include": must, "checklist": checklist,
            "subject_hint": subject, "template_lines": lines, "form_file": form_file, "amount": amount}


def note_task(plant_id, priority, title, why, source):
    return T.add(plant_id, priority, "note", title, why, {"text": why}, source)


def has_open(plant_id, type_, key, value):
    return any(t["status"] == "open" and t["type"] == type_ and t["params"].get(key) == value for t in T.load(plant_id))


async def analyst_terms(ctx, steps, vendor_id, question, name="contract_terms"):
    v = vendor_info(vendor_id)
    return await steps.run(name, lambda: ctx["helpers"].contract_analyst(f"Vendor {v['vendor_id']} ({v['vendor_name']}). {question}"),
                           agent="contract-analyst")


def terms_md(res):
    """Short readable summary of what the contract-analyst found (+ the code's quote check)."""
    if not res or not res.get("data"):
        return "_Contract analyst: no answer._"
    d, qc = res["data"], res.get("quote_check", {})
    ok = f"{qc.get('found', 0)}/{qc.get('checked', 0)} quotes verified word-for-word"
    return f"**Contract analyst** ({d.get('contract_id', '')}): {d.get('answer', '')}  \n_{ok} in the contract._"


def market_md(text):
    return f"**Market researcher (web):**\n{text}" if text else ""


def stop_reason(o):
    return (f"ranked #1 by the restock workflow: arrives {o['arrival_date']} "
            f"({'before' if o['arrives_before_run_out'] else 'after'} the run-out date), risk score {o['risk_score']}, "
            f"{money(o['unit_landed_usd'])} per unit incl. freight")


# ================================================================ 1. RESTOCK  (red)  -> purchase request to the approver ladder
def restock_email(ctx, pr, extra_reason=""):
    a = pr["approval"]
    n = pr["need"]
    facts = {"purchase_request": pr["draft_id"], "part": f"{pr['part_id']} {pr['part_name']}", "qty": pr["qty"], "vendor": pr["vendor_name"],
             "unit_price": money(pr["unit_price_usd"]), "freight": money(pr["freight_usd"]), "total": money(pr["total_usd"]),
             "expected_delivery": pr["expected_delivery"], "stock": f"{n['on_hand']} on hand, safety stock {n['safety_stock']}, "
             f"{n['weekly_usage']}/week, runs out {n['run_out_date']}", "why_this_vendor": pr["reason"],
             "vendors_compared": [f"{x['vendor']} {money(x['total_usd'])}, {x['lead_time_days']} days, risk {x['risk_score']}" for x in pr["alternatives_compared"]],
             "approval_level": f"{a['approver_role']} ({a['approvals_needed']} approval(s))", "policy_reasons": a["reasons"],
             "minimum_order": pr.get("moq_note", ""), "note": extra_reason}
    return email_plan(
        ctx, a["signers"], "Ask the approver(s) to approve this purchase request so the PO can be issued.",
        f"Please approve purchase request {pr['draft_id']} for {pr['qty']} x {pr['part_id']} {pr['part_name']} from {pr['vendor_name']}.",
        facts, [pr["draft_id"], money(pr["total_usd"]), str(pr["qty"]), pr["vendor_name"].split()[0], n["run_out_date"]],
        "Plant policy section 7: part and quantity; why (stock, usage, run-out date); vendor chosen and why; vendors compared; "
        f"total incl. freight; expected delivery; approval level. Greets every approver; sent and signed by {ctx['user_name']} "
        "(the Plant Head) by name; under 220 words.",
        f"Approval needed: {pr['draft_id']} {pr['part_id']} x{pr['qty']} ({money(pr['total_usd'])})",
        [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items() if k not in ("policy_reasons",) and v],
        form_file=f"{pr['draft_id']}.md", amount=money(pr["total_usd"]), recipient_role=a["approver_role"])


async def restock_prepare(task, ctx, steps):
    call, part_id, plant_id = ctx["call"], task["params"]["part_id"], ctx["plant_id"]
    rec = await steps.run("restock_options", lambda: W.recommend_restock(call, part_id, plant_id), agent="restock workflow (code via MCP)")
    pt = part_info(part_id)
    if not rec["vendor_options"]:
        return {"error": "no eligible vendor"}
    top = rec["vendor_options"][0]
    contract, market = await asyncio.gather(
        analyst_terms(ctx, steps, top["vendor_id"], "Does the contract give us a late-delivery credit (rate, grace days, cap)? "
                                                    "When does it expire? What are the payment terms?"),
        steps.run("market", lambda: ctx["helpers"].market_researcher([f"{pt['part_name']} supply 2026", f"{pt['category']} industrial prices"]),
                  agent="market-researcher"))
    rows = "\n".join(f"| {o['rank']} | {o['vendor_name']} | {o['qty']} | {money(o['unit_price_usd'])} | {money(o['total_usd'])} | "
                     f"{o['arrival_date']}{'' if o['arrives_before_run_out'] else ' (after run-out)'} | {o['risk_score']} | "
                     f"{'; '.join(o['notes']) or o['price_source']} |" for o in rec["vendor_options"])
    blocked = "\n".join(f"- **{b['vendor_name']}** blocked: {b['blocked_reason']}" for b in rec["blocked_vendors"])
    tr = rec["transfer_option"]
    s = rec["stock"]
    summary = (f"**Need:** {s['on_hand']} on hand vs safety stock {s['safety_stock']}, {s['weekly_usage']}/week, runs out **{s['run_out_date']}**.\n\n"
               f"| # | Vendor | Qty | Unit | Total | Arrives | Risk | Note |\n|---|---|---|---|---|---|---|---|\n{rows}\n\n{blocked}\n\n"
               + (f"**Transfer:** {tr['transfer_qty']} from {tr['plant_name']} (surplus {tr['surplus_qty']}), arrives {tr['arrives']}. "
                  f"{tr['ask']} Approving this task puts the request in their queue.\n\n" if tr else "")
               + f"**Recommendation:** {rec['recommendation']}\n\n"
               + "**Policy checks:**\n" + "\n".join(f"- {c}" for c in rec.get("policy_checks", [])) + "\n\n"
               + f"{terms_md(contract)}\n\n{market_md(market)}")
    return {"summary_md": summary, "rec": rec, "contract": contract}


def transfer_ask_email(ctx, src, part, qty, stock):
    """Email to the OTHER plant's head asking for an inter-plant transfer (plant policy section 6: both Plant Heads agree)."""
    head = person("Plant Head", src["from_plant"])
    facts = {"part": f"{part['part_id']} {part['part_name']}", "qty_requested": qty, "your_surplus": src["surplus_qty"],
             "our_stock": f"{stock['on_hand']} on hand, safety stock {stock['safety_stock']}, runs out {stock['run_out_date']}",
             "truck": f"{src['transfer_days']} days by truck ({src['road_miles']} miles, about {money(src['truck_cost_usd'])})",
             "arrives_if_shipped_today": src["arrives"], "policy": "plant policy section 6: a transfer needs both Plant Heads' agreement"}
    return email_plan(ctx, head, "Ask the other Plant Head to agree to an inter-plant transfer.",
                      f"Could {src['plant_name']} transfer {qty} x {part['part_id']} to {ctx['plant_name']}?", facts,
                      [part["part_id"], str(qty), stock["run_out_date"]],
                      "Part and quantity; why (our stock and run-out date); their surplus; truck time and arrival; asks them to "
                      "confirm; friendly; under 150 words.", f"Transfer request: {qty} x {part['part_id']} to {ctx['plant_name']}",
                      [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()], recipient_role=f"Plant Head, {src['plant_name']}")


def create_transfer_request(task, ctx, part, qty, src):
    """Puts a RED transfer request at the top of the other Plant Head's queue, plus a 'waiting' note in mine."""
    follow = []
    if src["from_plant"] in T.USERS and src["from_plant"] != ctx["plant_id"]:
        follow.append(T.add(src["from_plant"], "red", "transfer_request",
                            f"Transfer request from {ctx['plant_name']}: {qty} x {part['part_id']}",
                            f"{ctx['user_name']} asks for {qty} x {part['part_id']} {part['part_name']} (your surplus: {src['surplus_qty']}). "
                            f"Policy: both Plant Heads must agree.",
                            {"part_id": part["part_id"], "qty": qty, "to_plant": ctx["plant_id"], "requested_by": ctx["user_name"],
                             "arrives": src["arrives"], "request_task": task["id"]}, task["id"], first=True))
    follow.append(note_task(ctx["plant_id"], "low", f"Waiting for {src['plant_name']} to confirm transfer of {qty} x {part['part_id']}",
                            f"Requested from {src['plant_head']} ({src['plant_name']}) on {config.TODAY}.", task["id"]))
    return follow


def vendor_options_md(rec, qty):
    rows = "\n".join(f"| {o['rank']} | {o['vendor_name']} | {o['qty']} | {money(o['unit_price_usd'])} (contract {money(o['contract_rate_usd'])}) | "
                     f"{money(o['total_usd'])} | {o['arrival_date']}{'' if o['arrives_before_run_out'] else ' (after run-out)'} | "
                     f"{o['risk_score']} | {'; '.join(o['notes']) or o['price_source']} |" for o in rec["vendor_options"])
    blocked = "\n".join(f"- **{b['vendor_name']}** blocked: {b['blocked_reason']}" for b in rec["blocked_vendors"])
    return (f"**Vendor options for {qty} units** (ranked on cost, delivery speed and risk):\n\n"
            f"| # | Vendor | Qty | Unit (contract) | Total | Arrives | Risk | Note |\n|---|---|---|---|---|---|---|---|\n{rows}\n\n{blocked}")


async def restock_on_approve(task, plan, ctx, final, pr=None):
    pr = pr or plan["pr"]
    rec = plan["rec"]
    plant_id = ctx["plant_id"]
    follow = [note_task(plant_id, "low", f"Track delivery: {pr['draft_id']} ({pr['qty']} x {pr['part_id']} from {pr['vendor_name']})",
                        f"Approval request sent {config.TODAY}. Expected delivery {pr['expected_delivery']}.", task["id"])]
    out = f"Purchase request {pr['draft_id']} ({money(pr['total_usd'])}) sent to {pr['approval']['approver_role']}."
    tr = rec.get("transfer_option")
    if tr and tr["from_plant"] in T.USERS and tr["from_plant"] != plant_id:
        tid = T.add(tr["from_plant"], "red", "transfer_request",
                    f"Transfer request from {ctx['plant_name']}: {tr['transfer_qty']} x {pr['part_id']}",
                    f"{ctx['user_name']} asks for {tr['transfer_qty']} x {pr['part_id']} {pr['part_name']} (your surplus: {tr['surplus_qty']}) "
                    f"to bridge until the vendor delivers. Policy: both Plant Heads must agree.",
                    {"part_id": pr["part_id"], "qty": tr["transfer_qty"], "to_plant": plant_id, "requested_by": ctx["user_name"],
                     "arrives": tr["arrives"], "request_task": task["id"]}, task["id"], first=True)
        follow += [tid, note_task(plant_id, "low", f"Waiting for {tr['plant_name']} to confirm transfer of {tr['transfer_qty']} x {pr['part_id']}",
                                  f"Requested from {T.USERS[tr['from_plant']]} ({tr['plant_name']}).", task["id"])]
        out += f" Transfer request for {tr['transfer_qty']} units sent to {T.USERS[tr['from_plant']]}'s queue."
    return {"outcome": out, "followups": follow, "pr_record": pr_record(pr)}


def pr_record(pr):
    """What a later amendment needs to know about a purchase request that was sent."""
    return {k: pr.get(k) for k in ("draft_id", "part_id", "part_name", "vendor_id", "vendor_name", "qty", "total_usd", "expected_delivery")} | {
        "approver_role": pr["approval"]["approver_role"], "signers": [x["name"] for x in pr["approval"]["signers"]]}


def _other_vendor(plan):
    """The best-ranked eligible vendor other than the one in the rejected request."""
    chosen = (plan.get("pr") or {}).get("vendor_id")
    return next((o for o in plan["rec"]["vendor_options"] if o["vendor_id"] != chosen), None)


def restock_reject_options(task, plan, ctx):
    opts = []
    o = _other_vendor(plan)
    if o:
        opts.append({"id": "alt_vendor", "label": f"Draft it for the next vendor instead: {o['vendor_name']} ({money(o['total_usd'])}, arrives {o['arrival_date']})",
                     "next_email": True})
    opts += [{"id": "change_qty", "label": f"Change the quantity and redraft (now {plan['pr']['qty']})", "next_email": True,
              "input": "qty", "default": plan["pr"]["qty"]},
             {"id": "escalate", "label": "Escalate to the Procurement Head today", "next_email": True},
             {"id": "snooze", "label": "Snooze to tomorrow"}, {"id": "close", "label": "Withdraw the request (close)"}]
    return opts


async def restock_next_email(task, plan, ctx, choice, reason):
    rec, plant_id = plan["rec"], ctx["plant_id"]
    if choice == "alt_vendor":
        o = _other_vendor(plan)
        pr2 = await W.prepare_purchase_order(ctx["call"], plant_id, rec["part"]["part_id"], o["vendor_id"], plan["pr"].get("qty_asked") or o["qty"],
                                             f"next-ranked vendor after the first proposal was rejected ({reason})")
        pr2.pop("form_markdown", None)
        return {"email": restock_email(ctx, pr2, f"Replaces the earlier proposal, rejected because: {reason}"), "pr": pr2}
    if choice == "change_qty":
        qty = int(plan.get("choice_value") or plan["pr"]["qty"])
        pr2 = await W.prepare_purchase_order(ctx["call"], plant_id, rec["part"]["part_id"], plan["pr"]["vendor_id"], qty,
                                             f"quantity set to {qty} by the Plant Head")
        pr2.pop("form_markdown", None)
        note = f"Quantity changed by the Plant Head from {plan['pr']['qty']} to {pr2['qty']}."
        if not pr2.get("fits_warehouse", True):
            note += " Not all of it fits the warehouse now: please arrange a split delivery (PO policy section 6)."
        return {"email": restock_email(ctx, pr2, note), "pr": pr2}
    head = person("Procurement Head")
    s = rec["stock"]
    p = rec["part"]
    facts = {"part": f"{p['part_id']} {p['part_name']}", "runs_out": s["run_out_date"], "rejected_proposal": plan["pr"]["draft_id"],
             "rejected_because": reason, "options": [f"{o['vendor_name']}: {o['qty']} for {money(o['total_usd'])}, arrives {o['arrival_date']}"
                                                     for o in rec["vendor_options"]], "transfer": rec.get("recommendation")}
    return {"email": email_plan(ctx, head, "Escalate: the part will run out and the proposed order was rejected; ask for a decision today.",
                                f"I need your decision today on how to cover {p['part_id']} {p['part_name']}, which runs out {s['run_out_date']}.",
                                facts, [p["part_id"], s["run_out_date"]], "States the run-out date, what was rejected and why, the options, "
                                "and a clear ask for a decision today. Polite, under 200 words.",
                                f"Decision needed today: {p['part_id']} runs out {s['run_out_date']}",
                                [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()], recipient_role="Procurement Head")}


async def restock_on_next(task, plan, ctx, choice, plan2):
    if choice in ("alt_vendor", "change_qty"):
        return await restock_on_approve(task, plan, ctx, None, pr=plan2["pr"])
    return {"outcome": "Escalated to the Procurement Head.",
            "followups": [note_task(ctx["plant_id"], "red", f"Waiting for Procurement Head decision on {plan['rec']['part']['part_id']}",
                                    "Escalated today; chase by end of day.", task["id"])]}


# ================================================================ 2. CLAIM late-delivery credit (contract-analyst finds the clause)
def _same(a, b):
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a or "").lower() == str(b or "").lower()


def numbers_in_quote(dp):
    q = (dp.get("quote") or "").lower()
    probs = []
    for k in ("rate_pct", "cap_pct"):
        v = dp.get(k)
        try:
            ok = v is not None and f"{float(v):g}%" in q
        except (TypeError, ValueError):
            ok = False
        if not ok:
            probs.append(f"{k}={v} not in the quote")
    try:
        g = int(float(dp.get("grace_days")))
        if not re.search(rf"\({g}\)|\b{g}\b", q):
            probs.append(f"grace_days={g} not in the quote")
    except (TypeError, ValueError):
        probs.append("grace_days missing")
    if str(dp.get("per", "")).split("_")[0] not in q:
        probs.append(f"per={dp.get('per')} not in the quote")
    return probs


async def claim_prepare(task, ctx, steps):
    po = await steps.run("po", lambda: ctx["call"]("get_purchase_order", {"po_id": task["params"]["po_id"]}), agent="code via MCP")
    got = po.get("received_date") or config.TODAY.isoformat()
    days_late = (date.fromisoformat(got) - date.fromisoformat(po["promised_date"])).days
    res = await analyst_terms(ctx, steps, po["vendor_id"],
                              "Find the LATE-DELIVERY penalty / liquidated damages / late delivery credit clause. Return terms.delay_penalty "
                              "with rate_pct, per (week, day or week_or_part), grace_days, cap_pct and the exact quote of the whole clause; "
                              "if there is none return delay_penalty.none = true with the quote.")
    dp = dict(((res.get("data") or {}).get("terms") or {}).get("delay_penalty") or {})
    # VERIFY the contract-analyst against the clause text itself (code reads section 5.2 straight from the contract file)
    clause = contract_clause(po["vendor_id"], "5.2")
    code_terms = parse_delay_clause(clause)
    keys = ("rate_pct", "per", "grace_days", "cap_pct")
    agree = bool(code_terms) and not code_terms.get("none") and all(_same(dp.get(k), code_terms[k]) for k in keys)
    head = f"**PO {po['po_id']}**: {int(po['qty'])} x {po['part_id']} from {po['vendor_name']}, value {money(po['po_value_usd'])}, promised {po['promised_date']}, delivered {got}: **{days_late} days late**.\n\n"
    if code_terms.get("none") or (dp.get("none") and not code_terms):
        return {"summary_md": head + f"{terms_md(res)}\n\n> {clause}\n\nThe contract has **no late-delivery credit**, so there is nothing to claim. "
                                     "Worth raising at the next renewal.", "email": None, "done_outcome": "No credit in the contract; nothing to claim."}
    if agree:
        check_note = "✅ The contract-analyst's rate, grace days and cap match the text of clause 5.2 (checked by code)."
    elif code_terms:
        found = {k: dp.get(k) for k in keys}
        check_note = (f"⚠️ The contract-analyst read {found}, which does not match clause 5.2. "
                      f"Using the terms code read from the clause itself: {code_terms}.")
    else:
        return {"summary_md": head + f"{terms_md(res)}\n\n> {clause or '(clause 5.2 not found)'}\n\n⚠️ The terms could not be verified "
                                     "automatically, so no claim was calculated. Please check section 5.2 of the contract.", "email": None,
                "done_outcome": "Terms need a manual check."}
    dp = {**code_terms, "quote": clause}
    calc = W.calculate_delay_penalty(float(po["po_value_usd"]), days_late, float(dp["rate_pct"]), str(dp["per"]),
                                     int(float(dp["grace_days"])), float(dp["cap_pct"]))
    amount = money(calc["penalty_usd"])
    v = vendor_info(po["vendor_id"])
    facts = {"po": po["po_id"], "part": f"{int(po['qty'])} x {po['part_id']}", "po_value": money(po["po_value_usd"]),
             "promised": po["promised_date"], "delivered": got, "days_late": days_late, "clause": f"{res['data'].get('contract_id')} section 5.2",
             "clause_quote": dp.get("quote"), "calculation": f"{days_late} days late - {calc['grace_days']} grace = {calc['days_beyond_grace']} days -> "
             f"{calc['periods']} {dp['per']}(s) x {float(dp['rate_pct']):g}% = {calc['penalty_pct']:g}% (cap {float(dp['cap_pct']):g}%)",
             "credit": amount, "ask": "credit note against our next invoice within 14 days"}
    summary = (head + f"{terms_md(res)}\n\n{check_note}\n\n> {clause}\n\n**Calculation (code):** {facts['calculation']} of {facts['po_value']} = **{amount}**")
    ep = email_plan(ctx, vendor_contact(v["vendor_name"]), "Claim the late-delivery credit owed under the contract.",
                    f"Under our contract, PO {po['po_id']} qualifies for a late-delivery credit of {amount}.", facts,
                    [po["po_id"], amount, f"{days_late} days"],
                    "PO number; promised vs delivered dates and days late; the clause quoted with its section; the calculation; the exact "
                    "credit; a clear request (credit note within 14 days); firm but polite; under 200 words.",
                    f"Late-delivery credit claim: {po['po_id']} ({amount})", [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()],
                    amount=amount, recipient_role=f"vendor: {v['vendor_name']}")
    return {"summary_md": summary, "email": ep, "po": po, "amount_usd": calc["penalty_usd"], "amount": amount, "vendor": v["vendor_name"]}


async def claim_on_approve(task, plan, ctx, final):
    due = (config.TODAY + timedelta(days=14)).isoformat()
    return {"outcome": f"Claim for {plan['amount']} sent to {plan['vendor']}.",
            "followups": [note_task(ctx["plant_id"], "low", f"Check {plan['vendor']} credit note of {plan['amount']} ({plan['po']['po_id']})",
                                    f"Claim sent {config.TODAY}; credit note due by {due}.", task["id"])]}


def claim_reject_options(task, plan, ctx):
    if plan["amount_usd"] >= 1000:
        first = {"id": "waiver_request", "label": f"Ask the Procurement Head to approve waiving {plan['amount']} (policy: $1,000+)", "next_email": True}
    else:
        first = {"id": "waive", "label": f"Waive the {plan['amount']} credit (under $1,000: your call; the reason is recorded)"}
    return [first, {"id": "snooze", "label": "Snooze to tomorrow"}]


async def claim_next_email(task, plan, ctx, choice, reason):
    head = person("Procurement Head")
    facts = {"po": plan["po"]["po_id"], "vendor": plan["vendor"], "credit_owed": plan["amount"], "why_waive": reason,
             "policy": "Contract Management Policy section 5: waiving $1,000 or more needs Procurement Head approval"}
    return {"email": email_plan(ctx, head, "Ask the Procurement Head to approve waiving a late-delivery credit.",
                                f"I'd like your approval to waive the {plan['amount']} late-delivery credit {plan['vendor']} owes us on {plan['po']['po_id']}.",
                                facts, [plan["amount"], plan["po"]["po_id"]], "States the credit, the PO, the reason for waiving and the policy; "
                                "clear yes/no ask; under 150 words.", f"Approval to waive {plan['amount']} credit ({plan['po']['po_id']})",
                                [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()], amount=plan["amount"],
                                recipient_role="Procurement Head")}


async def claim_on_next(task, plan, ctx, choice, plan2):
    return {"outcome": f"Waiver request ({plan['amount']}) sent to the Procurement Head.",
            "followups": [note_task(ctx["plant_id"], "low", f"Await Procurement Head decision: waive {plan['amount']} ({plan['po']['po_id']})",
                                    "If declined, send the claim to the vendor.", task["id"])]}


# ================================================================ 3. OVERCHARGE (price above contract)
async def overcharge_prepare(task, ctx, steps):
    call = ctx["call"]
    po = await steps.run("po", lambda: call("get_purchase_order", {"po_id": task["params"]["po_id"]}), agent="code via MCP")
    vend = await steps.run("vendors", lambda: call("get_vendors_for_part", {"part_id": po["part_id"]}), agent="code via MCP")
    rate = next(v["contract_rate_usd"] for v in vend["vendors"] if v["vendor_id"] == po["vendor_id"])
    over = round((float(po["unit_price_usd"]) - rate) * float(po["qty"]), 2)
    res = await analyst_terms(ctx, steps, po["vendor_id"], f"What does the contract say about quotes or prices ABOVE the contract price, "
                                                           f"and what is the contract price for {po['part_id']}? Quote the sentence exactly.")
    v = vendor_info(po["vendor_id"])
    facts = {"po": po["po_id"], "part": f"{int(po['qty'])} x {po['part_id']}", "po_unit_price": money(po["unit_price_usd"]),
             "contract_rate": money(rate), "overcharge_total": money(over), "contract": f"{v['vendor_name']} Master Supply Agreement section 2",
             "ask": f"correct the PO and invoice to the contract rate of {money(rate)}"}
    summary = (f"**PO {po['po_id']}**: {int(po['qty']):,} x {po['part_id']} at {money(po['unit_price_usd'])} vs contract **{money(rate)}** "
               f"= **{money(over)} too much** (calculated in code).\n\n{terms_md(res)}")
    ep = email_plan(ctx, vendor_contact(v["vendor_name"]), "Ask the vendor to correct the PO price to the contract rate.",
                    f"PO {po['po_id']} is priced above our contract rate; please correct it to {money(rate)}.", facts,
                    [po["po_id"], money(rate), money(over)], "PO number; PO price vs contract rate; total difference; cites contract "
                    "section 2; clear request to correct PO and invoice; polite; under 160 words.",
                    f"Price correction needed: {po['po_id']} ({money(over)})", [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()],
                    amount=money(over), recipient_role=f"vendor: {v['vendor_name']}")
    return {"summary_md": summary, "email": ep, "po": po, "over": money(over), "rate": money(rate), "vendor": v["vendor_name"]}


async def overcharge_on_approve(task, plan, ctx, final):
    return {"outcome": f"Price correction ({plan['over']}) requested from {plan['vendor']}.",
            "followups": [note_task(ctx["plant_id"], "low", f"Check corrected price on {plan['po']['po_id']} ({plan['vendor']})",
                                    f"Asked for {plan['rate']} per unit on {config.TODAY}.", task["id"])]}


def overcharge_reject_options(task, plan, ctx):
    return [{"id": "accept_request", "label": "Keep the higher price: ask the Procurement Head for written acceptance (required by policy)",
             "next_email": True}, {"id": "snooze", "label": "Snooze to tomorrow"}]


async def overcharge_next_email(task, plan, ctx, choice, reason):
    head = person("Procurement Head")
    facts = {"po": plan["po"]["po_id"], "vendor": plan["vendor"], "po_unit_price": money(plan["po"]["unit_price_usd"]),
             "contract_rate": plan["rate"], "extra_cost": plan["over"],
             "business_reason": reason, "policy": "Contract Management Policy section 4: a higher price needs the Procurement Head's written acceptance"}
    return {"email": email_plan(ctx, head, "Ask the Procurement Head to accept a price above the contract rate, in writing.",
                                f"I'd like your written acceptance to pay {plan['over']} above contract on {plan['po']['po_id']}.", facts,
                                [plan["over"], plan["po"]["po_id"], money(plan["po"]["unit_price_usd"]), plan["rate"]],
                                "States the PO, the PO price vs the contract rate, the extra cost, the business reason and the policy; clear ask; "
                                "under 150 words.", f"Acceptance needed: {plan['po']['po_id']} above contract ({plan['over']})",
                                [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()], amount=plan["over"],
                                recipient_role="Procurement Head")}


async def overcharge_on_next(task, plan, ctx, choice, plan2):
    return {"outcome": "Asked the Procurement Head to accept the higher price.",
            "followups": [note_task(ctx["plant_id"], "low", f"Await written acceptance for {plan['po']['po_id']} price",
                                    "Do not pay the higher invoice until accepted.", task["id"])]}


# ================================================================ 4. RENEWAL decision (contract-analyst + market-researcher + policy rule)
def vendor_performance(vendor_id):
    po = table("purchase_orders")
    po = po[(po.vendor_id == vendor_id) & po.received_date.notna()]
    n = len(po)
    on_time = int((po.received_date <= po.promised_date).sum())
    return {"deliveries": n, "on_time": on_time, "on_time_pct": round(100 * on_time / n) if n else None}


async def renewal_prepare(task, ctx, steps):
    vid = task["params"]["vendor_id"]
    cs = await steps.run("contract_status", lambda: ctx["call"]("get_contract_status", {"vendor_id": vid}), agent="code via MCP")
    risk = await steps.run("risk", lambda: ctx["call"]("get_vendor_risk", {"vendor_id": vid}), agent="code via MCP")
    perf = vendor_performance(vid)
    cats = sorted({part_info(p)["category"] for p in table("contract_price_lines").query("vendor_id == @vid").part_id})
    res, market = await asyncio.gather(
        analyst_terms(ctx, steps, vid, "Summarise expiry, auto-renewal and notice days, late-delivery penalty (or none), price escalation cap and "
                                       "termination notice."),
        steps.run("market", lambda: ctx["helpers"].market_researcher([f"{c} prices 2026" for c in cats[:2]]), agent="market-researcher"))
    dp = ((res.get("data") or {}).get("terms") or {}).get("delay_penalty") or {}
    pct = perf["on_time_pct"]
    reasons = []
    if pct is not None and pct < 85:
        rec = "exit and run an RFQ"
        reasons.append(f"on-time delivery {pct}% is below 85%")
    elif (pct is not None and pct < 95) or risk["risk_score"] >= 60 or dp.get("none"):
        rec = "renegotiate"
        if pct is not None and pct < 95:
            reasons.append(f"on-time delivery {pct}% is below 95%")
        if risk["risk_score"] >= 60:
            reasons.append(f"risk score {risk['risk_score']} is 60 or more")
        if dp.get("none"):
            reasons.append("the contract has no late-delivery credit")
    else:
        rec = "renew"
        reasons.append(f"on-time delivery {pct}% and risk score {risk['risk_score']} meet the renewal bar")
    head = person("Procurement Head")
    facts = {"vendor": cs["vendor_name"], "contract": cs["contract_id"], "expires": cs["expiry_date"],
             "decision_needed_by": cs.get("renewal_decision_by"), "on_time_delivery": f"{perf['on_time']}/{perf['deliveries']} ({pct}%)",
             "risk_score": f"{risk['risk_score']} (simulated)", "key_terms": (res.get("data") or {}).get("answer", ""),
             "recommendation": rec, "because": reasons, "policy": "Contract Management Policy section 3"}
    summary = (f"**{cs['vendor_name']}** contract {cs['contract_id']} expires **{cs['expiry_date']}**; decision needed by "
               f"**{cs.get('renewal_decision_by')}**.\n\n**Performance (code):** {facts['on_time_delivery']} on time · risk {risk['risk_score']}\n\n"
               f"**Recommendation (policy section 3 rule, code): {rec.upper()}** because {', '.join(reasons)}.\n\n{terms_md(res)}\n\n{market_md(market)}")
    ep = email_plan(ctx, head, "Ask the Procurement Head to approve the renewal recommendation before the notice date.",
                    f"I recommend we {rec} the {cs['vendor_name']} contract; please approve before {cs.get('renewal_decision_by')}.", facts,
                    [cs["vendor_name"].split()[0], rec, str(cs.get("renewal_decision_by"))],
                    "Vendor and contract; expiry and decision date; performance and risk; key terms; the recommendation and why (policy "
                    "section 3); clear ask for approval; under 220 words.", f"Renewal recommendation: {cs['vendor_name']} ({rec})",
                    [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()], recipient_role="Procurement Head")
    return {"summary_md": summary, "email": ep, "cs": cs, "rec": rec, "cats": cats}


async def renewal_on_approve(task, plan, ctx, final):
    cs = plan["cs"]
    tid = T.add(ctx["plant_id"], "orange", "vendor_notice", f"Send {plan['rec']} notice to {cs['vendor_name']}",
                f"Approval requested {config.TODAY}; notice must reach the vendor before {cs.get('renewal_decision_by')}.",
                {"vendor_id": cs["vendor_id"], "kind": plan["rec"], "deadline": cs.get("renewal_decision_by")}, task["id"])
    return {"outcome": f"Recommendation '{plan['rec']}' sent to the Procurement Head.", "followups": [tid]}


def renewal_reject_options(task, plan, ctx):
    return [{"id": "non_renewal", "label": "Send the vendor a non-renewal notice (keeps our options open)", "next_email": True},
            {"id": "rfq", "label": "Ask two other vendors for quotes (RFQ)", "next_email": True},
            {"id": "snooze", "label": "Snooze to tomorrow"}]


async def renewal_next_email(task, plan, ctx, choice, reason):
    cs = plan["cs"]
    if choice == "non_renewal":
        facts = {"contract": cs["contract_id"], "expires": cs["expiry_date"], "notice": "we will not renew automatically; open to new terms"}
        return {"email": email_plan(ctx, vendor_contact(cs["vendor_name"]), "Give formal notice of non-renewal before the notice date, keeping the door open to new terms.",
                                    f"This is our formal notice that we will not renew {cs['contract_id']} automatically when it expires on {cs['expiry_date']}.",
                                    facts, [cs["contract_id"], cs["expiry_date"]], "Formal notice; contract ID; expiry date; open to "
                                    "discussing new terms; polite; under 120 words.", f"Notice of non-renewal: {cs['contract_id']}",
                                    [f"{k.capitalize()}: {v}" for k, v in facts.items()], recipient_role=f"vendor: {cs['vendor_name']}")}
    lines = table("contract_price_lines")
    alts = [v for v in table("vendor_risk_scores").itertuples() if v.vendor_id != cs["vendor_id"] and v.risk_score < 75
            and any(part_info(p)["category"] in plan["cats"] for p in lines[lines.vendor_id == v.vendor_id].part_id)][:2]
    to = [vendor_contact(a.vendor_name) for a in alts] or [person("Procurement Head")]
    parts = [part_info(p) for p in sorted(set(lines[lines.vendor_id == cs["vendor_id"]].part_id))]
    facts = {"categories": plan["cats"], "parts": [f"{x['part_id']} {x['part_name']}" for x in parts],
             "supply_starts": cs["expiry_date"], "ask": "unit price, minimum order quantity and lead time for each part, by "
             + (config.TODAY + timedelta(days=10)).isoformat()}
    return {"email": email_plan(ctx, to, "Request for quotation from alternative vendors.",
                                f"We are inviting quotes for {', '.join(plan['cats'])} supply from {cs['expiry_date']}.", facts,
                                [cs["expiry_date"]] + [x["part_id"] for x in parts],
                                "Clear scope (each part by ID and name), start date, what to quote (price, minimum order, lead time), "
                                "reply deadline; professional; under 150 words.",
                                f"Request for quotation: {', '.join(plan['cats'])}", [f"{k.capitalize()}: {v}" for k, v in facts.items()],
                                recipient_role="vendors (RFQ)")}


async def renewal_on_next(task, plan, ctx, choice, plan2):
    msg = "Non-renewal notice sent." if choice == "non_renewal" else "RFQ sent to alternative vendors."
    return {"outcome": msg, "followups": [note_task(ctx["plant_id"], "low", f"Compare options before {plan['cs']['expiry_date']}: {plan['cs']['vendor_name']}",
                                                    msg, task["id"])]}


# ================================================================ 5. OVERDUE PO from a HIGH-RISK vendor
async def overdue_prepare(task, ctx, steps):
    call = ctx["call"]
    po = await steps.run("po", lambda: call("get_purchase_order", {"po_id": task["params"]["po_id"]}), agent="code via MCP")
    risk = await steps.run("risk", lambda: call("get_vendor_risk", {"vendor_id": po["vendor_id"]}), agent="code via MCP")
    stock = await steps.run("stock", lambda: call("get_stock_position", {"plant_id": ctx["plant_id"], "part_id": po["part_id"]}), agent="code via MCP")
    pt = part_info(po["part_id"])
    res, market = await asyncio.gather(
        analyst_terms(ctx, steps, po["vendor_id"], "What are the late-delivery credit terms and the termination terms?"),
        steps.run("market", lambda: ctx["helpers"].market_researcher([f"{pt['part_name']} supply delays", f"{pt['category']} suppliers Oklahoma Texas"]),
                  agent="market-researcher"))
    days = (config.TODAY - date.fromisoformat(po["promised_date"])).days
    v = vendor_info(po["vendor_id"])
    facts = {"po": po["po_id"], "part": f"{int(po['qty'])} x {po['part_id']} {pt['part_name']}", "promised": po["promised_date"],
             "days_overdue": days, "ask": f"confirmed ship date by {BUSINESS_2_DAYS}", "consequence": "otherwise we will cancel and re-source "
             "(Contract Management Policy section 6)", "late_credits": "late-delivery credits under the contract are accruing"}
    summary = (f"**{po['po_id']}** from **{v['vendor_name']}** (risk **{risk['risk_score']}**, {risk['risk_band']}): {int(po['qty'])} x {po['part_id']} "
               f"due {po['promised_date']}, **{days} days overdue**. Our stock runs out **{stock['run_out_date']}**.\n\n"
               f"**Policy (section 6):** ask for a confirmed ship date within 2 business days, else cancel and re-source.\n\n"
               f"{terms_md(res)}\n\n{market_md(market)}")
    ep = email_plan(ctx, vendor_contact(v["vendor_name"]), "Demand a confirmed ship date within 2 business days for an overdue order.",
                    f"PO {po['po_id']} is {days} days overdue; we need a confirmed ship date by {BUSINESS_2_DAYS}.", facts,
                    [po["po_id"], BUSINESS_2_DAYS], "PO and items; promised date and days overdue; firm deadline for a confirmed ship date; "
                    "states we will cancel and re-source otherwise; mentions late credits; firm, polite; under 160 words.",
                    f"URGENT: confirm ship date for {po['po_id']} by {BUSINESS_2_DAYS}", [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()],
                    recipient_role=f"vendor: {v['vendor_name']}")
    return {"summary_md": summary, "email": ep, "po": po, "vendor": v["vendor_name"]}


async def overdue_on_approve(task, plan, ctx, final):
    po = plan["po"]
    tid = T.add(ctx["plant_id"], "orange", "cancel_po", f"No ship date by {BUSINESS_2_DAYS}? Cancel {po['po_id']} ({plan['vendor']})",
                f"Expedite request sent {config.TODAY}. If {plan['vendor']} has not confirmed a ship date by {BUSINESS_2_DAYS}, cancel and re-source.",
                {"po_id": po["po_id"]}, task["id"])
    return {"outcome": f"Expedite request sent to {plan['vendor']} (deadline {BUSINESS_2_DAYS}).", "followups": [tid]}


def cancel_options(task, plan, ctx):
    return [{"id": "cancel_resource", "label": f"Cancel {plan['po']['po_id']} now and re-source", "next_email": True},
            {"id": "snooze", "label": "Snooze to tomorrow"}, {"id": "close", "label": "Close (keep waiting)"}]


def cancel_email(ctx, po, vendor_name, reason):
    facts = {"po": po["po_id"], "part": f"{int(po['qty'])} x {po['part_id']}", "promised": po["promised_date"], "reason": reason}
    return email_plan(ctx, vendor_contact(vendor_name), "Cancel the purchase order formally.",
                      f"We are cancelling PO {po['po_id']} with immediate effect.", facts, [po["po_id"]],
                      "Formal cancellation; PO and items; reason; asks for written confirmation; polite; under 120 words.",
                      f"Cancellation of {po['po_id']}", [f"{k.capitalize()}: {v}" for k, v in facts.items()], recipient_role=f"vendor: {vendor_name}")


async def cancel_next_email(task, plan, ctx, choice, reason):
    why = ("the vendor's contract has expired and no exception was granted (Contract Management Policy section 2)"
           if task["type"] == "expired_contract" else
           "overdue order from a high-risk vendor without a confirmed ship date (Contract Management Policy section 6)")
    return {"email": cancel_email(ctx, plan["po"], plan["vendor"], f"{why}; note: {reason}")}


async def cancel_on_next(task, plan, ctx, choice, plan2):
    po = plan["po"]
    follow = []
    if not has_open(ctx["plant_id"], "restock", "part_id", po["part_id"]):
        follow.append(T.add(ctx["plant_id"], "red", "restock", f"Re-source {po['part_id']} (PO {po['po_id']} cancelled)",
                            f"{int(po['qty'])} units from {plan['vendor']} cancelled {config.TODAY}.", {"part_id": po["part_id"]}, task["id"]))
    return {"outcome": f"{po['po_id']} cancelled.", "followups": follow}


async def cancel_po_prepare(task, ctx, steps):
    po = await steps.run("po", lambda: ctx["call"]("get_purchase_order", {"po_id": task["params"]["po_id"]}), agent="code via MCP")
    v = vendor_info(po["vendor_id"])
    reason = f"no confirmed ship date by {BUSINESS_2_DAYS}"
    return {"summary_md": f"**{po['po_id']}** from {v['vendor_name']} is still not confirmed. Policy section 6: cancel and re-source.",
            "email": cancel_email(ctx, po, v["vendor_name"], reason), "po": po, "vendor": v["vendor_name"]}


# ================================================================ 6. EXPIRED CONTRACT with an open PO
async def expired_prepare(task, ctx, steps):
    call = ctx["call"]
    po = await steps.run("po", lambda: call("get_purchase_order", {"po_id": task["params"]["po_id"]}), agent="code via MCP")
    cs = await steps.run("contract_status", lambda: call("get_contract_status", {"vendor_id": po["vendor_id"]}), agent="code via MCP")
    res = await analyst_terms(ctx, steps, po["vendor_id"], "When did the contract expire and does it renew automatically? Quote the term clause.")
    alt = await steps.run("alternatives", lambda: W.recommend_restock(call, po["part_id"], ctx["plant_id"]), agent="restock workflow (code via MCP)")
    head = person("Procurement Head")
    alt_txt = "; ".join(f"{o['vendor_name']} arrives {o['arrival_date']} ({money(o['total_usd'])} for {o['qty']})" for o in alt["vendor_options"][:2])
    facts = {"po": po["po_id"], "vendor": po["vendor_name"], "part": f"{int(po['qty'])} x {po['part_id']}", "due": po["promised_date"],
             "contract": f"{cs['contract_id']} expired {cs['expiry_date']}, no auto-renewal",
             "request": "written exception for 90 days so this open order can complete",
             "alternative_if_declined": alt_txt, "policy": "Contract Management Policy section 2"}
    summary = (f"**{po['po_id']}**: {int(po['qty'])} x {po['part_id']} from **{po['vendor_name']}**, due {po['promised_date']}. "
               f"Contract **{cs['contract_id']} expired {cs['expiry_date']}**.\n\n**Policy section 2:** no deliveries under an expired contract: "
               f"request a 90-day exception from the Procurement Head, or cancel and re-source.\n\n**Alternatives (code):** {alt_txt}\n\n{terms_md(res)}")
    ep = email_plan(ctx, head, "Request a 90-day written exception so an open order with an expired-contract vendor can complete.",
                    f"Delta's contract has expired but {po['po_id']} is still open; I'd like a 90-day exception so it can be delivered.",
                    facts, [po["po_id"], "90", po["vendor_name"].split()[0]], "PO, vendor, items, due date; contract expired; the exception "
                    "asked for (90 days); the alternative if declined; policy section 2; under 180 words.",
                    f"Exception request: {po['po_id']} (contract expired)", [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()],
                    recipient_role="Procurement Head")
    return {"summary_md": summary, "email": ep, "po": po, "vendor": po["vendor_name"]}


async def expired_on_approve(task, plan, ctx, final):
    return {"outcome": "Exception requested from the Procurement Head.",
            "followups": [note_task(ctx["plant_id"], "low", f"Await exception decision for {plan['po']['po_id']}",
                                    "If declined: cancel the PO and re-source.", task["id"])]}


# ================================================================ 7-8. simple vendor emails: quote refresh, ship-date confirmation, renewal notice
async def quote_prepare(task, ctx, steps):
    vid, pid = task["params"]["vendor_id"], task["params"]["part_id"]
    q = await steps.run("quotes", lambda: ctx["call"]("get_latest_quotes", {"part_id": pid}), agent="code via MCP")
    stock = await steps.run("stock", lambda: ctx["call"]("get_stock_position", {"plant_id": ctx["plant_id"], "part_id": pid}), agent="code via MCP")
    mine = next(x for x in q["quotes"] if x["vendor_id"] == vid)
    qty = math.ceil(stock["weekly_usage"] * 6)
    v = vendor_info(vid)
    facts = {"part": pid, "last_quote": mine["quote_id"], "last_price": money(mine["unit_price_usd"]), "valid_until": mine["valid_until"],
             "expected_quantity": f"about {qty} units over the next 6 weeks", "reply_by": BUSINESS_2_DAYS}
    return {"summary_md": f"Quote **{mine['quote_id']}** from {v['vendor_name']} for {pid}: {money(mine['unit_price_usd'])}, valid until "
                          f"**{mine['valid_until']}** ({'EXPIRED' if mine['expired'] else 'expiring soon'}). Policy section 7: refresh it before ordering.",
            "email": email_plan(ctx, vendor_contact(v["vendor_name"]), "Request an updated quote.", f"Please send an updated quote for {pid}.",
                                facts, [pid, mine["quote_id"]], "Part; previous quote ID and price; expected quantity; reply date; under 120 words.",
                                f"Updated quote request: {pid}", [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()],
                                recipient_role=f"vendor: {v['vendor_name']}"), "vendor": v["vendor_name"]}


async def ship_prepare(task, ctx, steps):
    po = await steps.run("po", lambda: ctx["call"]("get_purchase_order", {"po_id": task["params"]["po_id"]}), agent="code via MCP")
    space = await steps.run("space", lambda: ctx["call"]("check_warehouse_space", {"plant_id": ctx["plant_id"], "part_id": po["part_id"],
                                                                                   "qty": int(po["qty"])}), agent="code via MCP")
    v = vendor_info(po["vendor_id"])
    facts = {"po": po["po_id"], "items": f"{int(po['qty'])} x {po['part_id']}", "promised": po["promised_date"], "ask": "confirmed ship date and tracking"}
    return {"summary_md": f"**{po['po_id']}** due **{po['promised_date']}**, no ship confirmation yet. Warehouse: {space['fill_pct']}% full; "
                          f"this delivery {'fits' if space.get('fits', True) else 'does NOT fit'}.",
            "email": email_plan(ctx, vendor_contact(v["vendor_name"]), "Ask the vendor to confirm the ship date.",
                                f"Please confirm the ship date for PO {po['po_id']}.", facts, [po["po_id"], po["promised_date"]],
                                "PO; items; promised date; asks for ship date and tracking; under 100 words.", f"Ship date for {po['po_id']}?",
                                [f"{k.capitalize()}: {v}" for k, v in facts.items()], recipient_role=f"vendor: {v['vendor_name']}"),
            "vendor": v["vendor_name"]}


async def notice_prepare(task, ctx, steps):
    p = task["params"]
    cs = await steps.run("contract_status", lambda: ctx["call"]("get_contract_status", {"vendor_id": p["vendor_id"]}), agent="code via MCP")
    kind = p["kind"]
    purpose = {"renew": "Confirm we intend to renew on current terms.", "renegotiate": "Notify the vendor we want to renegotiate terms before renewal.",
               "exit and run an RFQ": "Give notice of non-renewal; we will run an RFQ."}[kind]
    facts = {"contract": cs["contract_id"], "expires": cs["expiry_date"], "intent": kind, "deadline": p.get("deadline")}
    return {"summary_md": f"Approved recommendation: **{kind}** for {cs['vendor_name']} ({cs['contract_id']}, expires {cs['expiry_date']}).",
            "email": email_plan(ctx, vendor_contact(cs["vendor_name"]), purpose, f"Regarding {cs['contract_id']}: {purpose}", facts,
                                [cs["contract_id"]], "Contract ID; expiry; our intent; next step (meeting/proposal); under 140 words.",
                                f"{cs['contract_id']}: {kind}", [f"{k.capitalize()}: {v}" for k, v in facts.items()],
                                recipient_role=f"vendor: {cs['vendor_name']}"), "vendor": cs["vendor_name"]}


async def simple_on_approve(task, plan, ctx, final):
    return {"outcome": f"Email sent to {plan.get('vendor', 'the recipient')}.", "followups": []}


def simple_options(task, plan, ctx):
    return [{"id": "snooze", "label": "Snooze to tomorrow"}, {"id": "close", "label": "Close without sending"}]


# ================================================================ 9. TRANSFER REQUEST from the other plant (created by a restock approval)
async def transfer_prepare(task, ctx, steps):
    p = task["params"]
    stock = await steps.run("stock", lambda: ctx["call"]("get_stock_position", {"plant_id": ctx["plant_id"], "part_id": p["part_id"]}), agent="code via MCP")
    spare = int(stock["on_hand"] - stock["safety_stock"] - math.ceil(stock["weekly_usage"] * config.COVER_WEEKS_AFTER_ARRIVAL))
    req = person("Plant Head", p["to_plant"])
    can = spare >= p["qty"]
    facts = {"part": p["part_id"], "qty": p["qty"], "ship": config.TODAY.isoformat(), "arrives": p["arrives"],
             "our_stock_after": stock["on_hand"] - p["qty"], "our_safety_stock": stock["safety_stock"]}
    return {"summary_md": f"**{p['requested_by']}** asks for **{p['qty']} x {p['part_id']}**. Your stock: {stock['on_hand']} on hand, safety "
                          f"{stock['safety_stock']}, {stock['weekly_usage']}/week, so you can spare **{spare}** while keeping 6 weeks of cover. "
                          f"{'✅ You can cover it.' if can else '⚠️ Not enough surplus.'}",
            "email": email_plan(ctx, req, "Confirm the inter-plant transfer.", f"Happy to help: we will transfer {p['qty']} x {p['part_id']} today.",
                                facts, [p["part_id"], str(p["qty"]), p["arrives"]], "Confirms part, quantity, ship and arrival dates; "
                                "friendly; under 100 words.", f"Transfer confirmed: {p['qty']} x {p['part_id']}",
                                [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()], recipient_role="Plant Head"),
            "req_plant": p["to_plant"], "p": p}


async def transfer_on_approve(task, plan, ctx, final):
    p = plan["p"]
    tid = note_task(plan["req_plant"], "low", f"Transfer confirmed: {p['qty']} x {p['part_id']} from {ctx['plant_name']}, arriving {p['arrives']}",
                    f"{ctx['user_name']} approved the transfer on {config.TODAY}.", task["id"])
    return {"outcome": f"Transfer of {p['qty']} x {p['part_id']} confirmed to {p['requested_by']}.", "followups": [tid]}


def transfer_options(task, plan, ctx):
    return [{"id": "decline", "label": "Decline and tell the requester why", "next_email": True}, {"id": "snooze", "label": "Snooze to tomorrow"}]


async def transfer_next_email(task, plan, ctx, choice, reason):
    p = plan["p"]
    req = person("Plant Head", p["to_plant"])
    facts = {"part": p["part_id"], "qty": p["qty"], "reason": reason}
    return {"email": email_plan(ctx, req, "Decline the transfer request politely with the reason.", f"Sorry, we can't transfer {p['qty']} x {p['part_id']} right now.",
                                facts, [p["part_id"]], "Part and quantity; clear reason; friendly; under 100 words.",
                                f"Transfer request: {p['qty']} x {p['part_id']}", [f"{k.capitalize()}: {v}" for k, v in facts.items()],
                                recipient_role="Plant Head")}


async def transfer_on_next(task, plan, ctx, choice, plan2):
    """Transfer DECLINED: the requesting plant still needs those units.
    If its purchase request was already sent -> a red task to send the approver an AMENDED request (old qty + declined qty).
    If not (or the transfer was meant to cover everything) -> a red task to cover the units another way."""
    p = plan["p"]
    req_task = next((t for t in T.load(plan["req_plant"]) if t["id"] == p.get("request_task")), None)
    old = (req_task or {}).get("pr")
    if old:
        tid = T.add(plan["req_plant"], "red", "pr_amend",
                    f"Amend {old['draft_id']}: {ctx['plant_name']} declined {p['qty']} x {p['part_id']}",
                    f"{ctx['user_name']} declined the transfer on {config.TODAY}. Ask the approver to raise {old['draft_id']} from "
                    f"{old['qty']} to {old['qty'] + p['qty']} units.",
                    {"part_id": p["part_id"], "declined_qty": p["qty"], "declined_by": ctx["user_name"], "from_plant_name": ctx["plant_name"],
                     "old_pr": old, "request_task": p.get("request_task")}, task["id"], first=True)
        out = f"Transfer declined; requester notified and asked to amend {old['draft_id']}."
    else:
        tid = T.add(plan["req_plant"], "red", "restock", f"Transfer declined by {ctx['plant_name']}: cover {p['qty']} x {p['part_id']} another way",
                    f"{ctx['user_name']} declined on {config.TODAY}.", {"part_id": p["part_id"]}, task["id"], first=True)
        out = "Transfer declined; requester notified."
    return {"outcome": out, "followups": [tid]}


# ================================================================ 9b. AMEND a purchase request after a declined transfer
async def amend_prepare(task, ctx, steps):
    p, call = task["params"], ctx["call"]
    old, declined = p["old_pr"], int(p["declined_qty"])
    new_qty = int(old["qty"]) + declined
    pr = await steps.run("amended_pr", lambda: W.prepare_purchase_order(
        call, ctx["plant_id"], p["part_id"], old["vendor_id"], new_qty,
        f"replaces {old['draft_id']}: {p['from_plant_name']} declined the transfer of {declined}"), agent="code: PO form + approver ladder")
    pr.pop("form_markdown", None)
    a = pr["approval"]
    level_changed = a["approver_role"] != old["approver_role"]
    facts = {"replaces": f"{old['draft_id']} ({old['qty']} x {old['part_id']}, {money(old['total_usd'])}, sent to {old['approver_role']})",
             "why": f"{p['from_plant_name']} ({p['declined_by']}) declined our transfer request for {declined} units",
             "new_request": pr["draft_id"], "new_quantity": f"{old['qty']} + {declined} = {pr['qty']}",
             "vendor": pr["vendor_name"], "new_total": money(pr["total_usd"]), "expected_delivery": pr["expected_delivery"],
             "stock": f"{pr['need']['on_hand']} on hand, safety stock {pr['need']['safety_stock']}, runs out {pr['need']['run_out_date']}",
             "approval_level": f"{a['approver_role']}" + (f" (changed from {old['approver_role']} because the total is now "
                                                          f"{money(pr['total_usd'])})" if level_changed else " (unchanged)"),
             "ask": f"please cancel {old['draft_id']} and approve {pr['draft_id']} instead"}
    ep = email_plan(ctx, a["signers"], "Ask the approver(s) to replace the earlier purchase request with an amended one for the higher quantity.",
                    f"Please replace {old['draft_id']} with {pr['draft_id']}: the quantity goes from {old['qty']} to {pr['qty']}.", facts,
                    [old["draft_id"], pr["draft_id"], str(old["qty"]), str(declined), str(pr["qty"]), money(pr["total_usd"])],
                    "Says which request it replaces and why (the declined transfer); old quantity + declined quantity = new quantity; "
                    "new total incl. freight; vendor and delivery date; approval level; clear ask to cancel the old one and approve the new "
                    f"one; sent and signed by {ctx['user_name']}; under 180 words.",
                    f"Amended purchase request: {pr['draft_id']} replaces {old['draft_id']} ({pr['qty']} x {p['part_id']})",
                    [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()], form_file=f"{pr['draft_id']}.md",
                    amount=money(pr["total_usd"]), recipient_role=a["approver_role"])
    summary = (f"**{p['from_plant_name']} declined the transfer of {declined} x {p['part_id']}**, so the purchase request must cover them.\n\n"
               f"| | Request | Quantity | Total | Approver |\n|---|---|---|---|---|\n"
               f"| Sent earlier | {old['draft_id']} | {old['qty']} | {money(old['total_usd'])} | {old['approver_role']} |\n"
               f"| Amended | {pr['draft_id']} | {old['qty']} + {declined} = **{pr['qty']}** | **{money(pr['total_usd'])}** | {a['approver_role']} |\n\n"
               + (f"⚠️ The approval level changes to **{a['approver_role']}** because the total is now {money(pr['total_usd'])}.\n\n" if level_changed else "")
               + (f"Minimum order: {pr['moq_note']}\n\n" if pr.get("moq_note") else "")
               + ("⚠️ Not all of it fits the warehouse now: a split delivery is needed (PO policy section 6).\n\n" if not pr.get("fits_warehouse", True) else ""))
    return {"summary_md": summary, "email": ep, "pr": pr, "old": old}


async def amend_on_approve(task, plan, ctx, final):
    pr, old = plan["pr"], plan["old"]
    follow = [note_task(ctx["plant_id"], "low", f"Track delivery: {pr['draft_id']} ({pr['qty']} x {pr['part_id']} from {pr['vendor_name']})",
                        f"Amended request sent {config.TODAY}; replaces {old['draft_id']}. Expected delivery {pr['expected_delivery']}.", task["id"])]
    return {"outcome": f"Amended request {pr['draft_id']} ({pr['qty']} units, {money(pr['total_usd'])}) sent to {pr['approval']['approver_role']}; "
                       f"replaces {old['draft_id']}.", "followups": follow, "pr_record": pr_record(pr)}


def amend_options(task, plan, ctx):
    return [{"id": "snooze", "label": "Snooze to tomorrow"},
            {"id": "close", "label": f"Keep {plan['old']['draft_id']} as it is (close; cover the {task['params']['declined_qty']} another way)"}]


# ================================================================ 10. INFORMATION tasks (no email): sub-agents report, you mark done
async def market_prepare(task, ctx, steps):
    text = await steps.run("market", lambda: ctx["helpers"].market_researcher(task["params"]["topics"]), agent="market-researcher")
    return {"summary_md": market_md(text) or "No relevant news found.", "email": None, "done_outcome": "Market news reviewed."}


async def vendor_review_prepare(task, ctx, steps):
    vid = task["params"]["vendor_id"]
    risk = await steps.run("risk", lambda: ctx["call"]("get_vendor_risk", {"vendor_id": vid}), agent="code via MCP")
    cs = await steps.run("contract_status", lambda: ctx["call"]("get_contract_status", {"vendor_id": vid}), agent="code via MCP")
    perf = vendor_performance(vid)
    res = await analyst_terms(ctx, steps, vid, "Summarise late-delivery penalty (or none), damage terms, auto-renewal and expiry.")
    return {"summary_md": f"**{risk['vendor_name']}**: risk **{risk['risk_score']}** ({risk['risk_band']}, simulated) · on time "
                          f"{perf['on_time']}/{perf['deliveries']} · contract expires {cs['expiry_date']} (auto-renew: {cs['auto_renew']}).\n\n{terms_md(res)}",
            "email": None, "done_outcome": "Vendor reviewed."}


async def space_prepare(task, ctx, steps):
    sp = await steps.run("space", lambda: ctx["call"]("check_warehouse_space", {"plant_id": ctx["plant_id"]}), agent="code via MCP")
    return {"summary_md": f"Warehouse **{sp['fill_pct']}%** full: {sp['free_units']:,} storage units free, {sp['reserved_for_open_orders_units']:,} "
                          f"reserved for open orders.", "email": None, "done_outcome": "Space checked."}


async def note_prepare(task, ctx, steps):
    return {"summary_md": task["params"].get("text", task["why"]), "email": None, "done_outcome": "Done."}


def _none(*a, **k):
    return []


PLAYBOOKS = {
    "restock": dict(prepare=restock_prepare, on_approve=restock_on_approve, reject_options=restock_reject_options,
                    next_email=restock_next_email, on_next_approved=restock_on_next),
    "claim": dict(prepare=claim_prepare, on_approve=claim_on_approve, reject_options=claim_reject_options,
                  next_email=claim_next_email, on_next_approved=claim_on_next),
    "overcharge": dict(prepare=overcharge_prepare, on_approve=overcharge_on_approve, reject_options=overcharge_reject_options,
                       next_email=overcharge_next_email, on_next_approved=overcharge_on_next),
    "renewal": dict(prepare=renewal_prepare, on_approve=renewal_on_approve, reject_options=renewal_reject_options,
                    next_email=renewal_next_email, on_next_approved=renewal_on_next),
    "overdue_high_risk": dict(prepare=overdue_prepare, on_approve=overdue_on_approve, reject_options=cancel_options,
                              next_email=cancel_next_email, on_next_approved=cancel_on_next),
    "cancel_po": dict(prepare=cancel_po_prepare, on_approve=lambda t, p, c, f: cancel_on_next(t, p, c, None, None),
                      reject_options=simple_options, next_email=None, on_next_approved=None),
    "expired_contract": dict(prepare=expired_prepare, on_approve=expired_on_approve, reject_options=cancel_options,
                             next_email=cancel_next_email, on_next_approved=cancel_on_next),
    "quote_refresh": dict(prepare=quote_prepare, on_approve=simple_on_approve, reject_options=simple_options, next_email=None, on_next_approved=None),
    "ship_confirm": dict(prepare=ship_prepare, on_approve=simple_on_approve, reject_options=simple_options, next_email=None, on_next_approved=None),
    "vendor_notice": dict(prepare=notice_prepare, on_approve=simple_on_approve, reject_options=simple_options, next_email=None, on_next_approved=None),
    "transfer_request": dict(prepare=transfer_prepare, on_approve=transfer_on_approve, reject_options=transfer_options,
                             next_email=transfer_next_email, on_next_approved=transfer_on_next),
    "pr_amend": dict(prepare=amend_prepare, on_approve=amend_on_approve, reject_options=amend_options, next_email=None, on_next_approved=None),
    "market_watch": dict(prepare=market_prepare), "vendor_review": dict(prepare=vendor_review_prepare),
    "space_review": dict(prepare=space_prepare), "note": dict(prepare=note_prepare),
}
