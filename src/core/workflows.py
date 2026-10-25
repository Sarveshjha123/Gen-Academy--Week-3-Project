"""WORKFLOWS that combine both systems. Plain code: the maths and the rules, no AI.

Each workflow gets a `call(tool_name, args)` function. In the app that function calls the two MCP servers
(Plant Ops + Supplier); in tests it calls the same functions directly. Same logic either way.
Independent look-ups run IN PARALLEL (asyncio.gather), the Session 2 latency tip.
"""
import asyncio
import json
import math
from datetime import date, timedelta

from src import config
from src.core import plant_ops, supplier
from src.core.data import NotFound, days_from_today, money, today

# ---------------------------------------------------------------- how tools are reached
DIRECT = {
    "get_stock_position": plant_ops.stock_position, "get_plant_inventory": plant_ops.plant_inventory,
    "check_warehouse_space": plant_ops.warehouse_space, "find_nearby_surplus": plant_ops.nearby_stock,
    "list_late_deliveries": plant_ops.late_deliveries, "get_purchase_order": plant_ops.purchase_order,
    "get_approval_route": plant_ops.approval_route, "list_open_orders": plant_ops.open_orders,
    "find_parts": plant_ops.find_parts,
    "get_vendors_for_part": supplier.vendors_for_part, "get_latest_quotes": supplier.latest_quotes,
    "get_vendor_risk": supplier.vendor_risk, "get_contract_status": supplier.contract_status,
    "find_price_overcharges": supplier.price_overcharges,
    "list_contracts_needing_attention": supplier.contracts_needing_attention,
}


async def direct_call(name, args):
    return DIRECT[name](**args)


DEFAULT_PRIORITIES = {"cost": 0.4, "speed": 0.3, "risk": 0.3, "critical_parts": [], "focus": ""}


def _norm(values):
    lo, hi = min(values), max(values)
    return [0.0 if hi == lo else (v - lo) / (hi - lo) for v in values]


# ================================================================ WORKFLOW 1: RESTOCK OPTIONS
async def recommend_restock(call, part_id: str, plant_id: str, priorities: dict | None = None, requested_qty: int = 0) -> dict:
    """Need -> nearby surplus -> vendors (rate, MOQ, lead time) -> risk + quotes -> space -> 3 scored options.
    requested_qty > 0 = the user asked for a specific quantity: every option is priced for THAT quantity (rounded up to the
    vendor's MOQ); the calculated need is still shown so the user can compare; space limits become a split delivery."""
    pr = {**DEFAULT_PRIORITIES, **(priorities or {})}
    w_total = pr["cost"] + pr["speed"] + pr["risk"] or 1
    w = {k: pr[k] / w_total for k in ("cost", "speed", "risk")}

    stock = await call("get_stock_position", {"plant_id": plant_id, "part_id": part_id})
    pid, plid = stock["part_id"], stock["plant_id"]
    space, nearby, vendors, quotes = await asyncio.gather(
        call("check_warehouse_space", {"plant_id": plid, "part_id": pid}),
        call("find_nearby_surplus", {"part_id": pid, "plant_id": plid}),
        call("get_vendors_for_part", {"part_id": pid}),
        call("get_latest_quotes", {"part_id": pid}))
    risks = await asyncio.gather(*[call("get_vendor_risk", {"vendor_id": v["vendor_id"]}) for v in vendors["vendors"]])
    risk_by = {r["vendor_id"]: r for r in risks}
    quote_by = {q["vendor_id"]: q for q in quotes["quotes"]}

    weekly, su = stock["weekly_usage"], stock["storage_units_per_item"]
    run_out = date.fromisoformat(stock["run_out_date"])
    max_fit = space["max_items_that_fit"]
    options, blocked = [], []
    for v in vendors["vendors"]:
        vid = v["vendor_id"]
        risk = risk_by[vid]["risk_score"]
        q = quote_by.get(vid)
        if q and not q["expired"] and not q["above_contract_rate"]:
            unit, source = q["unit_price_usd"], f"quote {q['quote_id']} (valid until {q['valid_until']})"
        elif q and q["expired"]:
            unit, source = v["contract_rate_usd"], f"contract rate (quote {q['quote_id']} EXPIRED {q['valid_until']}: request a new quote)"
        elif q and q["above_contract_rate"]:
            unit, source = v["contract_rate_usd"], f"contract rate (quote {q['quote_id']} is above contract rate, not valid)"
        else:
            unit, source = v["contract_rate_usd"], "contract rate (no quote on file)"
        lead = v["lead_time_days"]
        arrival = today() + timedelta(days=lead)
        need = weekly * (lead / 7 + config.COVER_WEEKS_AFTER_ARRIVAL) + stock["safety_stock"] - stock["on_hand"] - stock["on_order_on_time"]
        need_qty = max(v["moq"], math.ceil(max(need, 0) / v["moq"]) * v["moq"])
        notes = []
        if requested_qty and requested_qty > 0:
            qty = max(v["moq"], int(requested_qty))
            if qty != requested_qty:
                notes.append(f"Below the contract minimum: {v['vendor_name']}'s minimum order is {v['moq']}, so the order must be {qty} "
                             f"(you asked for {requested_qty}; contract section 4).")
            if qty > max_fit:
                first = (max_fit // v["moq"]) * v["moq"]
                notes.append(f"Warehouse fits {first} now: ask for a split delivery ({first} now, {qty - first} later; PO policy section 6).")
        else:
            qty = need_qty
        if not requested_qty and qty > max_fit:
            capped = (max_fit // v["moq"]) * v["moq"]
            notes.append(f"Warehouse space limits this order to {capped} (needed {qty}); split the rest into a second delivery.")
            qty = capped
        total = round(qty * unit + v["freight_per_order_usd"], 2) if qty else 0.0
        row = dict(vendor_id=vid, vendor_name=v["vendor_name"], risk_score=risk, risk_band=risk_by[vid]["risk_band"],
                   unit_price_usd=unit, price_source=source, qty=qty, moq=v["moq"], freight_usd=v["freight_per_order_usd"],
                   contract_rate_usd=v["contract_rate_usd"], calculated_need_qty=need_qty, total_usd=total, unit_landed_usd=round(total / qty, 2) if qty else None, lead_time_days=lead,
                   arrival_date=arrival.isoformat(), arrives_before_run_out=arrival <= run_out,
                   contract_id=v["contract_id"], contract_expiry=v["contract_expiry"], notes=notes)
        if v["contract_status"] == "EXPIRED" or v["contract_expiry"] < arrival.isoformat():
            row["blocked_reason"] = (f"Contract {v['contract_id']} is not in force through delivery (expiry {v['contract_expiry']}); "
                                    f"plant policy section 5 and Contract Management policy section 2.")
            blocked.append(row)
        elif risk >= config.RISK_BLOCK:
            row["blocked_reason"] = f"Risk score {risk} >= {config.RISK_BLOCK}: not allowed without a Procurement Head exception (plant policy section 4)."
            blocked.append(row)
        elif qty == 0:
            row["blocked_reason"] = "No space in the warehouse for even one minimum order quantity."
            blocked.append(row)
        else:
            options.append(row)

    if options:   # score: lower = better; weighted, normalised across the eligible vendors
        c, s, r = _norm([o["unit_landed_usd"] for o in options]), _norm([o["lead_time_days"] for o in options]), _norm([o["risk_score"] for o in options])
        for o, ci, si, ri in zip(options, c, s, r):
            # arriving after run-out: a critical part would stop production, so that outweighs any saving
            penalty = 1.0 if (not o["arrives_before_run_out"] and stock["critical_part"]) else 0.3 if not o["arrives_before_run_out"] else 0.0
            raw = w["cost"] * ci + w["speed"] * si + w["risk"] * ri + penalty
            o["fit_score"] = round(100 * (1 - raw / 2))
            o["score_parts"] = {"cost": round(ci, 2), "speed": round(si, 2), "risk": round(ri, 2), "late_penalty": penalty}
        options.sort(key=lambda o: -o["fit_score"])
    top = options[:3]
    cheapest = min((o["unit_landed_usd"] for o in options), default=None)
    for i, o in enumerate(top, 1):
        o["rank"] = i
        o["lowest_cost"] = o["unit_landed_usd"] == cheapest

    # transfer to bridge the gap until the chosen vendor delivers
    transfer = None
    if nearby["plants_with_surplus"] and top:
        lead = top[0]["lead_time_days"]
        gap = math.ceil(weekly * lead / 7 + stock["safety_stock"] - stock["on_hand"] - stock["on_order_on_time"])
        if gap > 0 or stock["below_safety_stock"]:
            src_ = nearby["plants_with_surplus"][0]
            qty_t = max(1, min(src_["surplus_qty"], gap if gap > 0 else stock["safety_stock"] - stock["on_hand"]))
            transfer = dict(src_, transfer_qty=qty_t,
                            ask=f"Check with {src_['plant_head']} ({src_['plant_name']} Plant Head, {src_['plant_head_email']}) "
                                f"whether they can transfer {qty_t} x {pid}.",
                            why=f"Covers use until the vendor delivers ({lead} days) and refills safety stock. A transfer is not a purchase: "
                                f"it needs both Plant Heads' agreement (plant policy section 6), no approval ladder.")

    headline = "No eligible vendor: escalate to Procurement." if not top else (
        (f"Transfer {transfer['transfer_qty']} from {transfer['plant_name']} now (arrives {transfer['arrives']}), and " if transfer else "")
        + f"order {top[0]['qty']} from {top[0]['vendor_name']} for {money(top[0]['total_usd'])} (arrives {top[0]['arrival_date']}).")
    quantity = {"basis": "calculated need: weekly use x (lead time + 6 weeks) + safety stock - on hand - on-time open orders"}
    approval = None
    if top:
        approval = await call("get_approval_route", {"plant_id": plid, "total_usd": top[0]["total_usd"],
                                                     "vendor_risk_score": top[0]["risk_score"],
                                                     "emergency": stock["days_of_cover"] <= 7, "lowest_cost": top[0]["lowest_cost"]})
        approval = {k: approval.get(k) for k in ("approver_role", "approvals_needed", "signers", "reasons")}
    if requested_qty and top:
        need0 = top[0]["calculated_need_qty"]
        extra = top[0]["qty"] - need0
        quantity = {"basis": f"YOUR quantity: {requested_qty} (as asked)", "requested_qty": requested_qty,
                    "calculated_need_qty": need0,
                    "comparison": (f"{extra} more than the calculated need of {need0} (about {round(extra / max(weekly, 0.1), 1)} extra weeks of use)"
                                   if extra > 0 else f"{-extra} less than the calculated need of {need0}" if extra < 0 else "matches the calculated need"),
                    "fits_warehouse_now": top[0]["qty"] <= max_fit}
    pol = f"POL-{plid}-2026"
    checks = [f"Other plants checked first (PO Raising policy section 1, {pol} section 6): "
              + (f"{transfer['plant_name']} has {transfer['surplus_qty']} surplus. {transfer['ask']}" if transfer
                 else "no plant within 600 road miles has surplus of this part.")]
    checks.append(f"Vendors compared (PO Raising policy section 2, {pol} section 5): {len(top)} eligible of {len(vendors['vendors'])} "
                  f"on contract for this part" + (f"; {len(blocked)} blocked." if blocked else "."))
    checks += [f"{b['vendor_name']} blocked: {b['blocked_reason']}" for b in blocked]
    for o in top:
        if "EXPIRED" in o["price_source"] or "above contract" in o["price_source"]:
            checks.append(f"{o['vendor_name']}: {o['price_source']} ({pol} section 5, Contract Management policy section 7).")
    if any("split delivery" in n or "Warehouse space limits" in n for o in top for n in o["notes"]):
        checks.append(f"Warehouse space is short ({pol} section 8, PO Raising policy section 6): split the delivery.")
    if top and approval:
        checks.append(f"Approval for option 1 ({money(top[0]['total_usd'])}): {approval['approver_role']} "
                      f"({', '.join(x['name'] for x in approval['signers'])}). Rules applied from {pol}: "
                      + " ".join(r.strip() for r in approval.get("reasons") or []))
    return {"part": {k: stock[k] for k in ("part_id", "part_name", "critical_part")},
            "plant_id": plid, "stock": {k: stock[k] for k in ("on_hand", "safety_stock", "weekly_usage", "days_of_cover",
                                                             "run_out_date", "below_safety_stock", "on_order_on_time", "on_order_overdue")},
            "warehouse": {k: space[k] for k in ("free_units", "fill_pct", "max_items_that_fit")},
            "priorities_used": w, "recommendation": headline, "quantity": quantity, "approval_for_top_option": approval,
            "transfer_option": transfer, "policy_checks": checks,
            "vendor_options": top, "blocked_vendors": blocked,
            "method": "Scores and quantities are calculated in code. If the user gives a quantity, every option is priced for it (rounded up to MOQ). Otherwise order qty = weekly use x (lead time + 6 weeks) + safety stock - on hand - on-time open orders, rounded up to MOQ, capped by warehouse space.",
            "limitations": ["Vendor risk scores are SIMULATED (random) for this prototype."]}


# ================================================================ IDEA 1: LATE DELIVERY PENALTY
def calculate_delay_penalty(po_value_usd: float, days_late: int, rate_pct: float, per: str, grace_days: int, cap_pct: float) -> dict:
    """Penalty from the contract's late-delivery clause. Partial periods do NOT count (only full weeks/days),
    except where the contract says 'part thereof counted as full' (pass per='week_or_part')."""
    late_beyond = max(0, days_late - grace_days)
    if per.startswith("day"):
        periods = late_beyond
    elif per == "week_or_part":
        periods = math.ceil(late_beyond / 7)
    else:
        periods = late_beyond // 7
    pct_ = min(periods * rate_pct, cap_pct)
    return {"days_late": days_late, "grace_days": grace_days, "days_beyond_grace": late_beyond, "periods": periods,
            "rate_pct": rate_pct, "per": per, "cap_pct": cap_pct, "penalty_pct": round(pct_, 2),
            "penalty_usd": round(po_value_usd * pct_ / 100, 2), "capped": periods * rate_pct > cap_pct}


# ================================================================ MORNING BRIEF: TOP 5
async def morning_briefing(call, plant_id: str, priorities: dict | None = None) -> dict:
    pr = {**DEFAULT_PRIORITIES, **(priorities or {})}
    inv, late, over, contracts, space = await asyncio.gather(
        call("get_plant_inventory", {"plant_id": plant_id}), call("list_late_deliveries", {"plant_id": plant_id}),
        call("find_price_overcharges", {"plant_id": plant_id}),
        call("list_contracts_needing_attention", {"plant_id": plant_id, "within_days": 60}),
        call("check_warehouse_space", {"plant_id": plant_id}))
    plid = inv["plant_id"]
    parts_tbl = {p["part_id"]: p for p in inv["parts"]}
    items = []

    # 1) running low (below safety, or less than 3 weeks of cover)
    for p in inv["parts"]:
        covered_by_open_orders = p["on_order_on_time"] and p["on_hand"] + p["on_order_on_time"] >= p["safety_stock"] + 2 * p["weekly_usage"]
        if (p["below_safety_stock"] or p["days_of_cover"] < 21) and not covered_by_open_orders:
            crit = p["part_id"] in pr["critical_parts"] or _is_critical(p["part_id"])
            urgency = max(0.0, min(1.0, 1 - p["days_of_cover"] / 30))
            if p["below_safety_stock"]:
                urgency = max(urgency, 0.9 if crit else 0.6)
            from src.core.data import part as _part
            value = round(_part(p["part_id"])["typical_price_usd"] * p["weekly_usage"] * 4)   # ~4 weeks of use at stake
            items.append(dict(type="restock", title=f"Restock {p['part_id']} {p['part_name']}",
                              why=f"{p['on_hand']} on hand vs safety stock {p['safety_stock']}; about {p['days_of_cover']} days of cover left.",
                              urgency=urgency, impact_usd=value, critical=crit, part_id=p["part_id"],
                              ask=f"{plid} is low on {p['part_id']}. Give me 3 vendor options and your recommendation."))
    # 2) late deliveries -> penalty claim (IDEA 1) / chase
    for l in late["late_purchase_orders"]:
        if l["status"] == "received late" and l["days_late"] >= 7:   # most contracts have a 2-10 day grace period
            items.append(dict(type="penalty_claim", title=f"Claim late-delivery credit on {l['po_id']} ({l['vendor']})",
                              why=f"{l['part_id']} x{l['qty']} arrived {l['days_late']} days late; PO value {money(l['po_value_usd'])}.",
                              urgency=0.55, impact_usd=round(l["po_value_usd"] * 0.05), part_id=l["part_id"],
                              ask=f"Claim the late-delivery credit for {l['po_id']} from {l['vendor']}."))
        elif l["status"] != "received late":
            items.append(dict(type="chase_overdue", title=f"Chase overdue {l['po_id']} from {l['vendor']}",
                              why=f"{l['part_id']} x{l['qty']} was due {l['promised_date']} ({l['days_late']} days overdue).",
                              urgency=min(0.85, 0.4 + l["days_late"] / 30), impact_usd=round(l["po_value_usd"] * 0.5), part_id=l["part_id"],
                              ask=f"{l['po_id']} from {l['vendor']} is overdue. What is the vendor's risk and what should I do?"))
    # 3) overcharges (IDEA 3)
    for o in over["overcharged_open_orders"]:
        items.append(dict(type="overcharge", title=f"Fix overcharge on {o['po_id']} ({o['vendor']})",
                          why=f"PO price ${o['po_unit_price_usd']} vs contract ${o['contract_rate_usd']} on {o['qty']:,} units = {money(o['overcharge_usd'])} too much.",
                          urgency=0.45 + min(0.3, o["overcharge_usd"] / 10000), impact_usd=o["overcharge_usd"],
                          part_id=o["part_id"], ask=f"{o['po_id']} is priced above the contract rate. Draft an email asking {o['vendor']} to correct it."))
    # 4) contracts
    for c in contracts["contracts"]:
        d_ = c.get("days_to_renewal_decision", c["days_to_expiry"])
        expired = c["status"] == "EXPIRED"
        items.append(dict(type="contract", title=("Contract EXPIRED: " if expired else "Renewal decision: ") + f"{c['vendor_name']}",
                          why=(f"{c['contract_id']} expired {c['expiry_date']}; its vendor cannot be used until renewed."
                               if expired else f"{c['contract_id']} expires {c['expiry_date']}; non-renewal notice due by {c.get('renewal_decision_by', c['expiry_date'])} ({d_} days)."),
                          urgency=0.6 if expired else max(0.2, min(0.9, 1 - d_ / 60)), impact_usd=None,
                          ask=f"Summarise the key terms of {c['vendor_name']}'s contract (expiry, renewal, damage, delay penalties, rates) and what I should decide."))
    # 5) warehouse
    if space["fill_pct"] >= 90:
        items.append(dict(type="space", title=f"Warehouse {space['fill_pct']}% full",
                          why=f"Only {space['free_units']:,} storage units free; large orders may need split deliveries.",
                          urgency=0.45, impact_usd=None, ask=f"How much warehouse space is free at {plid} and what does that mean for orders?"))

    imp = [i["impact_usd"] for i in items if i["impact_usd"]]
    hi = max(imp) if imp else 1
    for i in items:
        crit = i.get("critical") or (i.get("part_id") in pr["critical_parts"]) or \
               (i.get("part_id") and parts_tbl.get(i["part_id"], {}).get("part_id") and _is_critical(i.get("part_id")))
        focus_hit = pr.get("focus") and pr["focus"] in i["type"]
        i["priority_score"] = round(100 * (0.55 * i["urgency"] + 0.30 * (math.log10(1 + (i["impact_usd"] or 0)) / math.log10(1 + hi)) + 0.10 * bool(crit) + 0.05 * bool(focus_hit)))
        i["critical"] = bool(crit)
    items.sort(key=lambda i: -i["priority_score"])
    top, rest, per_type = [], [], {}
    for i in items:   # keep the Top 5 varied: at most 2 of the same kind
        if len(top) < 5 and per_type.get(i["type"], 0) < 2:
            top.append(i)
            per_type[i["type"]] = per_type.get(i["type"], 0) + 1
        else:
            rest.append(i)
    for n, i in enumerate(top + rest, 1):
        i["rank"] = n
    return {"plant_id": plid, "date": today().isoformat(), "top_5": top, "also_on_radar": rest[:8],
            "kpis": {"parts_below_safety": len(inv["parts_below_safety_stock"]),
                     "late_or_overdue_pos": len(late["late_purchase_orders"]),
                     "overcharge_usd": over["total_overcharge_usd"], "warehouse_fill_pct": space["fill_pct"],
                     "contracts_needing_attention": len(contracts["contracts"])},
            "method": "Ranked in code: 55% urgency, 30% money at stake (log scale), 10% critical part, 5% your focus; at most 2 items of one kind in the Top 5."}


def _is_critical(part_id):
    from src.core.data import part
    try:
        return part(part_id)["critical"] == "yes"
    except NotFound:
        return False


# ================================================================ WORKFLOW 5: PURCHASE ORDER DRAFT
def _next_draft_id():
    d = config.WORKSPACE / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    n = 1 + len(list(d.glob("PR-*.json")))
    return f"PR-{today():%Y%m%d}-{n:03d}"


async def prepare_purchase_order(call, plant_id: str, part_id: str, vendor_id: str, qty: int, reason: str = "",
                                 alternatives: list | None = None) -> dict:
    """Builds the purchase request with numbers from code, finds the approver from policy, saves drafts/PR-....json/.md"""
    stock = await call("get_stock_position", {"plant_id": plant_id, "part_id": part_id})
    vendors, quotes = await asyncio.gather(call("get_vendors_for_part", {"part_id": stock["part_id"]}),
                                           call("get_latest_quotes", {"part_id": stock["part_id"]}))
    v = next((x for x in vendors["vendors"] if x["vendor_id"].upper() == str(vendor_id).upper()
              or x["vendor_name"].lower() == str(vendor_id).lower()), None)
    if not v:
        raise NotFound(f"Vendor {vendor_id} has no contract price line for {stock['part_id']}.")
    risk = await call("get_vendor_risk", {"vendor_id": v["vendor_id"]})
    q = next((x for x in quotes["quotes"] if x["vendor_id"] == v["vendor_id"]), None)
    unit = q["unit_price_usd"] if q and not q["expired"] and not q["above_contract_rate"] else v["contract_rate_usd"]
    asked = int(qty)
    qty = max(v["moq"], asked)          # contract section 4: each PO must meet the minimum order quantity (MOQ)
    space = await call("check_warehouse_space", {"plant_id": stock["plant_id"], "part_id": stock["part_id"], "qty": qty})
    total = round(qty * unit + v["freight_per_order_usd"], 2)
    # lowest cost among eligible vendors (same logic as the restock workflow)
    opts = await recommend_restock(call, stock["part_id"], stock["plant_id"])
    eligible = {o["vendor_id"]: o for o in opts["vendor_options"]}
    lowest = (not eligible) or v["vendor_id"] in eligible and eligible[v["vendor_id"]].get("lowest_cost", False)
    emergency = stock["days_of_cover"] <= 7
    route = await call("get_approval_route", {"plant_id": stock["plant_id"], "total_usd": total,
                                              "vendor_risk_score": risk["risk_score"], "emergency": emergency,
                                              "lowest_cost": bool(lowest)})
    arrival = days_from_today(v["lead_time_days"])
    draft_id = _next_draft_id()
    pr = {"draft_id": draft_id, "status": "DRAFT - awaiting approval", "created": today().isoformat(),
          "plant_id": stock["plant_id"], "plant_name": stock["plant_name"],
          "part_id": stock["part_id"], "part_name": stock["part_name"], "qty": qty, "unit_price_usd": unit,
          "freight_usd": v["freight_per_order_usd"], "total_usd": total, "vendor_id": v["vendor_id"],
          "vendor_name": v["vendor_name"], "contract_id": v["contract_id"], "vendor_risk_score": risk["risk_score"],
          "lead_time_days": v["lead_time_days"], "expected_delivery": arrival,
          "need": {k: stock[k] for k in ("on_hand", "safety_stock", "weekly_usage", "days_of_cover", "run_out_date")},
          "fits_warehouse": space.get("fits", True), "reason": reason, "qty_asked": asked, "moq": v["moq"],
          "moq_note": (f"{v['vendor_name']}'s contract minimum order is {v['moq']}, so the order is {qty} (asked: {asked})."
                       if qty != asked else ""),
          "alternatives_compared": [dict(vendor=o["vendor_name"], total_usd=o["total_usd"], lead_time_days=o["lead_time_days"],
                                         risk_score=o["risk_score"]) for o in opts["vendor_options"]] if alternatives is None else alternatives,
          "approval": route}
    md = render_purchase_request(pr)
    folder = config.WORKSPACE / "drafts"
    (folder / f"{draft_id}.json").write_text(json.dumps(pr, indent=2))
    (folder / f"{draft_id}.md").write_text(md)
    return {**pr, "form_markdown": md, "saved_to": f"workspace/drafts/{draft_id}.md"}


def render_purchase_request(pr: dict) -> str:
    a = pr["approval"]
    signers = ", ".join(f"{s['name']} ({s['role']})" for s in a.get("signers", [])) or a.get("approver_role")
    alts = "\n".join(f"| {x['vendor']} | {money(x['total_usd'])} | {x['lead_time_days']} days | {x['risk_score']} |"
                     for x in pr["alternatives_compared"])
    n = pr["need"]
    return f"""# Purchase Request {pr['draft_id']}  (DRAFT)

**Northfield Industrial, {pr['plant_name']} plant ({pr['plant_id']})** · created {pr['created']}

| Field | Value |
|---|---|
| Part | {pr['part_id']} {pr['part_name']} |
| Quantity | {pr['qty']:,} |
| Vendor | {pr['vendor_name']} ({pr['vendor_id']}), contract {pr['contract_id']} |
| Unit price | {money(pr['unit_price_usd'])} |
| Freight | {money(pr['freight_usd'])} |
| **Total** | **{money(pr['total_usd'])}** |
| Expected delivery | {pr['expected_delivery']} ({pr['lead_time_days']} days) |
| Vendor risk score | {pr['vendor_risk_score']} (simulated) |
| Fits warehouse | {'Yes' if pr['fits_warehouse'] else 'No: split delivery'} |

**Why it's needed:** {n['on_hand']} on hand, safety stock {n['safety_stock']}, use {n['weekly_usage']}/week, about {n['days_of_cover']} days of cover (runs out {n['run_out_date']}).
{('**Reason / vendor choice:** ' + pr['reason']) if pr['reason'] else ''}

**Vendors compared**

| Vendor | Total | Lead time | Risk |
|---|---|---|---|
{alts}

**Requested by:** {a.get('requested_by', {}).get('name', '')} (Plant Head)
**Approval required ({a.get('approvals_needed', 1)}):** {a.get('approver_role')}: {signers}
{chr(10).join('- ' + r for r in a.get('reasons', []))}

""" + "\n".join(f"Signature ({s_['role']}, {s_['name']}): ______________________   Date: __________"
                for s_ in a.get("signers", [])) + "\n"


def check_email(pr: dict, email_body: str) -> list:
    """HARNESS CHECK (code): the approval email must carry the real numbers from the draft. Returns what is missing."""
    missing = []
    body = email_body or ""
    if not any(t in body for t in (money(pr["total_usd"]), f"{pr['total_usd']:,.2f}", f"{pr['total_usd']:,.0f}")):
        missing.append(f"the total {money(pr['total_usd'])}")
    if pr["vendor_name"].split()[0] not in body:
        missing.append(f"the vendor name {pr['vendor_name']}")
    if str(pr["qty"]) not in body.replace(",", ""):
        missing.append(f"the quantity {pr['qty']}")
    for signer in pr.get("approval", {}).get("signers") or []:
        if signer["name"].split()[0] not in body:
            missing.append(f"the approver's name ({signer['name']})")
    return missing


def submit_for_approval_files(draft_id: str, email_to: str, email_subject: str, email_body: str) -> dict:
    """Runs only AFTER the human approved (the agent pauses before this). 'Sends' = writes to workspace/outbox/."""
    src_ = config.WORKSPACE / "drafts" / f"{draft_id}.json"
    if not src_.exists():
        raise NotFound(f"No draft {draft_id} in workspace/drafts.")
    pr = json.loads(src_.read_text())
    pr["status"] = "SENT for signature"
    out = config.WORKSPACE / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    sender = pr.get("approval", {}).get("requested_by", {})
    frm = f"{sender.get('name', '')} <{sender.get('email', '')}>" if sender else ""
    email = f"From: {frm}\nTo: {email_to}\nSubject: {email_subject}\n\n{email_body}\n\n---\nAttached: {draft_id}.md\n"
    (out / f"{draft_id}_email.txt").write_text(email)
    (out / f"{draft_id}.md").write_text(render_purchase_request(pr).replace("(DRAFT)", "(SENT FOR SIGNATURE)"))
    src_.write_text(json.dumps(pr, indent=2))
    return {"sent": True, "draft_id": draft_id, "to": email_to, "files": [f"workspace/outbox/{draft_id}_email.txt", f"workspace/outbox/{draft_id}.md"]}
