"""EVAL LAYER 1: the code (workflows, rules, guardrails). No AI, free, runs in seconds.
    python -m eval.test_logic            # direct
    python -m eval.test_logic --mcp      # also check the MCP servers give the same answers (parity)
Every expected value below was worked out by hand from the data (see the planted scenario in scripts/generate_data.py).
"""
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from src import config
from src.core import plant_ops, supplier, workflows as W
from src.core.checks import verify_contract_quotes
from src.core.data import NotFound

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append({"test": name, "passed": bool(ok), "detail": str(detail)[:200]})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   -> {detail}"))


async def run(call):
    # ---------- 3. inventory + space
    s = plant_ops.stock_position("AUS", "P-110")
    check("P-110 Austin: 10.5 days of cover, runs out 2026-11-05", s["days_of_cover"] == 10.5 and s["run_out_date"] == "2026-11-05", s)
    check("P-110 Austin: below safety stock", s["below_safety_stock"])
    check("P-110 Austin: overdue PO-24140 not counted as on-time stock", s["on_order_on_time"] == 0 and s["on_order_overdue"][0]["po_id"] == "PO-24140", s["on_order_overdue"])
    sp = plant_ops.warehouse_space("AUS")
    check("Austin warehouse > 90% full", sp["fill_pct"] > 90, sp)
    check("Plant name works as ID ('Austin')", plant_ops.stock_position("Austin", "P-110")["plant_id"] == "AUS")

    # ---------- 1. restock
    r = await W.recommend_restock(call, "P-110", "AUS")
    names = [o["vendor_name"] for o in r["vendor_options"]]
    check("Restock P-110: exactly 3 vendor options", len(r["vendor_options"]) == 3, names)
    check("Restock P-110: Ironwood (risk 81) is BLOCKED even though cheapest",
          any(b["vendor_id"] == "V09" and "Risk score 81" in b["blocked_reason"] for b in r["blocked_vendors"]), r["blocked_vendors"])
    top = r["vendor_options"][0]
    check("Restock P-110: #1 is Evergreen (only vendor arriving before run-out)", top["vendor_id"] == "V05", names)
    check("Restock P-110: Evergreen qty 32, total $60,400", top["qty"] == 32 and top["total_usd"] == 60400, top)
    check("Restock P-110: Evergreen's expired quote is NOT used (contract rate instead)", "EXPIRED" in top["price_source"] and top["unit_price_usd"] == 1880, top["price_source"])
    check("Restock P-110: Apex uses its valid quote $1,790", next(o for o in r["vendor_options"] if o["vendor_id"] == "V01")["unit_price_usd"] == 1790)
    t = r["transfer_option"]
    check("Restock P-110: transfer 8 from Dallas, arriving in 2 days", t and t["from_plant"] == "DAL" and t["transfer_qty"] == 8 and t["transfer_days"] == 2, t)
    check("Restock P-110: 'lowest_cost' flag is on exactly one option", sum(o["lowest_cost"] for o in r["vendor_options"]) == 1)
    r2 = await W.recommend_restock(call, "P-210", "AUS")
    check("Restock P-210: order capped by warehouse space (split delivery)", all(o["qty"] <= r2["warehouse"]["max_items_that_fit"] for o in r2["vendor_options"])
          and any("Warehouse space limits" in n for o in r2["vendor_options"] for n in o["notes"]), r2["vendor_options"][0])
    r3 = await W.recommend_restock(call, "P-120", "HOU")
    check("Restock P-120 Houston: Delta Drive blocked (contract expired)", any(b["vendor_id"] == "V04" and "not in force" in b["blocked_reason"] for b in r3["blocked_vendors"]), r3["blocked_vendors"])
    fp = await call("find_parts", {"query": "hydraulic pumps"})
    check("Find part by name: 'hydraulic pumps' -> P-110 only", [m["part_id"] for m in fp["matches"]] == ["P-110"], fp)
    fp2 = await call("find_parts", {"query": "laptop"})
    check("Find part: 'laptop' -> no match (out of catalogue)", fp2["matches"] == [], fp2)
    ns = await call("find_nearby_surplus", {"part_id": "P-110", "plant_id": "AUS"})
    check("Nearby surplus names the plant head to contact (Marcus Bell, Dallas)", ns["plants_with_surplus"][0]["plant_head"] == "Marcus Bell"
          and "Check with Marcus Bell" in ns["plants_with_surplus"][0]["ask"], ns)
    check("Restock transfer option says who to check with", "Marcus Bell" in r["transfer_option"]["ask"])
    check("Restock policy checks cite transfer-first, 3 vendors, blocked vendor, approver",
          any("section 1" in c and "Marcus Bell" in c for c in r["policy_checks"]) and any("3 eligible" in c for c in r["policy_checks"])
          and any("Ironwood" in c and "blocked" in c for c in r["policy_checks"]) and any("Rachel Stein" in c for c in r["policy_checks"]),
          r["policy_checks"])
    check("Restock shows contract price next to the price used", all("contract_rate_usd" in o for o in r["vendor_options"]))
    oo = await call("list_open_orders", {"plant_id": "AUS", "part_id": "P-110"})
    check("Open orders P-110 Austin: PO-24140 overdue", any(o["po_id"] == "PO-24140" and o["status"] == "overdue" for o in oo["open_orders"]), oo)
    rq = await W.recommend_restock(call, "P-110", "AUS", requested_qty=80)
    check("Requested qty: every option priced for 80 (not the calculated 32)", all(o["qty"] == 80 for o in rq["vendor_options"]),
          [o["qty"] for o in rq["vendor_options"]])
    check("Requested qty: compares with the calculated need (32) and flags split delivery",
          rq["quantity"]["calculated_need_qty"] == 32 and not rq["quantity"]["fits_warehouse_now"]
          and any("split delivery" in n for n in rq["vendor_options"][0]["notes"]), rq["quantity"])
    check("Requested qty: $150,640 needs Procurement Head + Finance Head",
          [x["name"] for x in rq["approval_for_top_option"]["signers"]] == ["Rachel Stein", "Thomas Grey"], rq["approval_for_top_option"])
    r4 = await W.recommend_restock(call, "P-110", "AUS", {"cost": 1.0, "speed": 0.0, "risk": 0.0})
    check("Restock P-110 with cost-only priority still ranks the only on-time vendor first (late penalty)", r4["vendor_options"][0]["vendor_id"] == "V05")
    try:
        await W.recommend_restock(call, "P-999", "AUS")
        check("Unknown part is refused", False, "no error")
    except NotFound as e:
        check("Unknown part is refused (no guessing)", "Unknown" in str(e))
    try:
        await W.recommend_restock(call, "P-110", "Chicago")
        check("Unknown plant is refused", False, "no error")
    except NotFound:
        check("Unknown plant is refused (no guessing)", True)

    # ---------- 4. risk
    vr = supplier.vendor_risk("Ironwood Hydraulics")
    check("Vendor risk: Ironwood = 81, High, marked simulated", vr["risk_score"] == 81 and vr["risk_band"] == "High" and "SIMULATED" in vr["method"], vr)

    # ---------- 5. approvals (policy rules in code): Plant Head raises; ladder by total incl. freight
    ar = plant_ops.approval_route
    r_ = ar("AUS", 60400, 41)
    check("Approval: $60,400 -> Procurement Head Rachel Stein; raised by Plant Head Dana Whitfield",
          r_["approver_role"] == "Procurement Head" and r_["signers"][0]["name"] == "Rachel Stein" and r_["requested_by"]["name"] == "Dana Whitfield", r_)
    check("Approval: $12,000 -> Junior Procurement Lead Kevin O'Neill", ar("AUS", 12000, 30)["signers"][0]["name"] == "Kevin O'Neill")
    check("Approval: exactly $25,000 -> still Junior Procurement Lead", ar("AUS", 25000, 30)["approver_role"] == "Junior Procurement Lead")
    check("Approval: $12,000 with vendor risk 63 -> moved up to Procurement Head", ar("AUS", 12000, 63)["approver_role"] == "Procurement Head")
    check("Approval: $40,000 emergency -> Junior Procurement Lead (emergency limit $50k)", ar("AUS", 40000, 30, emergency=True)["approver_role"] == "Junior Procurement Lead")
    big = ar("DEN", 150000, 30)
    check("Approval: $150,000 -> 2 approvals: Procurement Head + Finance Head (Thomas Grey)",
          big["approvals_needed"] == 2 and [x["name"] for x in big["signers"]] == ["Rachel Stein", "Thomas Grey"], big)
    check("Approval: $80,000 with risk 63 -> moved up to Procurement Head + Finance Head", ar("AUS", 80000, 63)["approvals_needed"] == 2)
    check("Approval: vendor risk 81 -> not allowed", ar("AUS", 5000, 81)["allowed"] is False)
    check("Approval: not lowest cost -> justification required", ar("AUS", 9000, 30, lowest_cost=False)["justification_required"])

    # ---------- penalty claims (idea 1) + overcharges (idea 3)
    terms = json.loads((config.EVAL_DIR / "truth/contract_terms.json").read_text())
    p = terms["V10"]["delay_penalty"]
    pen = W.calculate_delay_penalty(29400, 26, p["rate_pct"], p["per"], p["grace_days"], p["cap_pct"])
    check("Penalty: PO-24137 Juniper, 26 days late, 2%/week after 5 days -> 3 weeks, 6%, $1,764", pen["penalty_usd"] == 1764 and pen["penalty_pct"] == 6, pen)
    check("Penalty: daily rate capped (0.5%/day x 28 days -> cap 10%)", W.calculate_delay_penalty(10000, 30, 0.5, "day", 2, 10)["penalty_pct"] == 10)
    check("Penalty: 'part of a week counts' rounds up", W.calculate_delay_penalty(10000, 12, 2, "week_or_part", 3, 12)["periods"] == 2)
    check("Penalty: within grace -> $0", W.calculate_delay_penalty(10000, 4, 2, "week", 5, 10)["penalty_usd"] == 0)
    oc = supplier.price_overcharges("AUS")
    check("Overcharges AUS: PO-24139 $2,600 + PO-24141 $280 = $2,880", oc["total_overcharge_usd"] == 2880 and oc["overcharged_open_orders"][0]["po_id"] == "PO-24139", oc)
    ca = supplier.contracts_needing_attention("AUS")
    g = next((c for c in ca["contracts"] if c["vendor_id"] == "V07"), None)
    check("Contracts AUS: Granite Controls renewal decision due 2026-11-15", g and g["renewal_decision_by"] == "2026-11-15", ca)

    # ---------- morning brief
    b = await W.morning_briefing(call, "AUS")
    types = [i["type"] for i in b["top_5"]]
    check("Morning brief AUS: #1 is restock P-110", b["top_5"][0]["type"] == "restock" and b["top_5"][0]["part_id"] == "P-110", b["top_5"][0]["title"])
    check("Morning brief AUS: includes a penalty claim and an overcharge", "penalty_claim" in types and "overcharge" in types, types)
    check("Morning brief AUS: at most 2 items of one kind", max(types.count(x) for x in set(types)) <= 2, types)
    check("Morning brief AUS: P-130 not flagged (4,000 on order arrives in time)", all(i.get("part_id") != "P-130" or i["type"] != "restock" for i in b["top_5"] + b["also_on_radar"]))

    # ---------- PO draft + email guardrail (in a temp workspace)
    old = config.WORKSPACE
    config.WORKSPACE = Path(tempfile.mkdtemp())
    try:
        pr = await W.prepare_purchase_order(call, "AUS", "P-110", "V05", 32, "Only vendor arriving before run-out")
        check("PO draft: total $60,400, approver Procurement Head, saved", pr["total_usd"] == 60400 and pr["approval"]["approver_role"] == "Procurement Head"
              and (config.WORKSPACE / "drafts" / f"{pr['draft_id']}.md").exists(), pr["approval"])
        pr2 = await W.prepare_purchase_order(call, "AUS", "P-110", "V05", 31, "")
        check("PO draft: 31 is above Evergreen's minimum (2), so 31 is kept", pr2["qty"] == 31 and not pr2["moq_note"])
        pr3 = await W.prepare_purchase_order(call, "AUS", "P-110", "V05", 1, "")
        check("PO draft: 1 is below the contract minimum (2) -> order 2, with a note", pr3["qty"] == 2 and "minimum order is 2" in pr3["moq_note"])
        bad = W.check_email(pr, "Hi Rachel, please approve the Evergreen order of 32 pumps, total $61,000.")
        check("Email guardrail: wrong total is caught", any("total" in m for m in bad), bad)
        good = W.check_email(pr, "Hi Rachel, please approve 32 pumps from Evergreen Industrial Supply, total $60,400.")
        check("Email guardrail: correct email passes", good == [], good)
        sent = W.submit_for_approval_files(pr["draft_id"], "Rachel Stein <rachel.stein@northfield.example>", "Approval", "Hi Rachel ...")
        mail = (config.WORKSPACE / "outbox" / f"{pr['draft_id']}_email.txt").read_text()
        check("Submit (after human approval) writes email + PO to outbox, FROM the Plant Head",
              sent["sent"] and mail.startswith("From: Dana Whitfield <dana.whitfield@northfield.example>"), mail[:80])
        form = (config.WORKSPACE / "outbox" / f"{pr['draft_id']}.md").read_text()
        check("PO form shows 'Requested by' and a signature line per approver", "Requested by:** Dana Whitfield" in form and "Signature (Procurement Head, Rachel Stein)" in form)
    finally:
        shutil.rmtree(config.WORKSPACE, ignore_errors=True)
        config.WORKSPACE = old

    # ---------- contract-analyst guardrail
    ok = verify_contract_quotes(json.dumps({"vendor_id": "V10", "terms": {"payment_terms": {"quote": "Payment terms are Net 30 from the date a correct invoice is received."}}}))
    bad = verify_contract_quotes(json.dumps({"vendor_id": "V10", "terms": {"x": {"quote": "Supplier pays a 50% penalty for any delay."}}}))
    check("Quote check: real contract sentence is found", ok["found"] == 1, ok)
    check("Quote check: invented sentence is caught", bad["found"] == 0 and bad["missing"], bad)


def test_delay_clauses():
    from src.core.checks import contract_clause, parse_delay_clause
    truth = json.loads((config.EVAL_DIR / "truth/contract_terms.json").read_text())
    ok = 0
    for vid, t in truth.items():
        tp = t["delay_penalty"]
        exp = {"none": True} if tp is None else {k: tp[k] for k in ("rate_pct", "per", "grace_days", "cap_pct")}
        ok += parse_delay_clause(contract_clause(vid)) == exp
    check("Code reads the late-delivery clause of all 15 contracts correctly (backstop for the contract-analyst)", ok == 15, f"{ok}/15")


def test_verify_answer():
    from src.core.checks import verify_answer
    ev = ['{"qty": 32, "total_usd": 60400.0, "arrival_date": "2026-11-05", "fill_pct": 95.7, "w": 0.4, "po": "PO-24140", "part": "P-110"}']
    ok = verify_answer("Order 32 for $60,400, arriving Nov 5 (2026-11-05). Space 95.7% full; cost weight 40%. See PO-24140, section 5.", ev)
    check("Faithfulness check: grounded answer passes (commas, dates, %, sections)", ok["unsupported"] == [], ok)
    bad = verify_answer("Order 35 for $61,000, arriving 2026-11-09. See PO-29999.", ev)
    check("Faithfulness check: invented qty, total, date and PO are caught",
          set(bad["unsupported"]) == {"35", "$61000", "2026-11-09", "PO-29999"}, bad)
    nb = verify_answer("Order 32 for $60 400 (narrow spaces) or $60.4k.", ev)
    check("Faithfulness check: '60 400' and '$60.4k' formats accepted", nb["unsupported"] == [], nb)
    uni = verify_answer("Order P\u2011110: $60\u202f400, arrives 2026\u201111\u201105, PO\u201124140, 4.3 % free.", ev)
    check("Faithfulness check: model's special hyphens/spaces (P\u2011110, 60\u202f400) are read correctly", uni["unsupported"] == [], uni)


async def mcp_call_factory():
    """A `call` that goes through the two real MCP servers (stdio), using the plain MCP client."""
    from contextlib import AsyncExitStack

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    stack = AsyncExitStack()
    sessions = {}
    for name in ("plant_ops", "supplier"):
        params = StdioServerParameters(command=sys.executable, args=[str(config.ROOT / f"src/mcp_servers/{name}_server.py")])
        r, w = await stack.enter_async_context(stdio_client(params))
        s = await stack.enter_async_context(ClientSession(r, w))
        await s.initialize()
        for t in (await s.list_tools()).tools:
            sessions[t.name] = s

    async def call(tool, args):
        res = await sessions[tool].call_tool(tool, args)
        data = json.loads(res.content[0].text)
        if isinstance(data, dict) and list(data) == ["error"]:
            raise NotFound(data["error"])
        return data
    return call, stack, sorted(sessions)


async def main():
    t0 = time.time()
    await run(W.direct_call)
    test_verify_answer()
    test_delay_clauses()
    if "--mcp" in sys.argv:
        print("\n--- same tests, but every look-up goes through the MCP servers ---")
        call, stack, names = await mcp_call_factory()
        check(f"MCP: {len(names)} tools discovered on 2 servers", len(names) == 15, names)
        try:
            await run(call)
        finally:
            await stack.aclose()
    passed = sum(r["passed"] for r in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed in {time.time() - t0:.1f}s")
    out = config.EVAL_DIR / "results"
    out.mkdir(exist_ok=True)
    (out / "logic_tests.json").write_text(json.dumps({"passed": passed, "total": len(RESULTS), "tests": RESULTS}, indent=1))
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
