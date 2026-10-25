"""PLANT OPS system (think: the ERP). Stock, warehouse space, transfers between plants, purchase orders.
Every function is plain code (a WORKFLOW). These are exposed to the agent through the Plant Ops MCP server.
"""
import datetime as dt
import math

from src import config
from src.core.data import NotFound, clean, days_from_today, part, plant, table, today


def _open_pos(plant_id=None, part_id=None):
    po = table("purchase_orders")
    po = po[po.status == "open"]
    if plant_id:
        po = po[po.plant_id == plant_id]
    if part_id:
        po = po[po.part_id == part_id]
    return po


def stock_position(plant_id: str, part_id: str) -> dict:
    """Stock, usage, days of cover and run-out date for one part at one plant."""
    pl, pt = plant(plant_id), part(part_id)
    inv = table("inventory")
    row = inv[(inv.plant_id == pl["plant_id"]) & (inv.part_id == pt["part_id"])].iloc[0]
    daily = row.weekly_usage / 7
    open_po = _open_pos(pl["plant_id"], pt["part_id"])
    t = today().isoformat()
    on_time = open_po[open_po.promised_date >= t]
    overdue = open_po[open_po.promised_date < t]
    days_cover = row.on_hand / daily if daily else 999
    return clean({
        "plant_id": pl["plant_id"], "plant_name": pl["plant_name"], "part_id": pt["part_id"], "part_name": pt["part_name"],
        "critical_part": pt["critical"] == "yes", "on_hand": int(row.on_hand), "safety_stock": int(row.safety_stock),
        "weekly_usage": float(row.weekly_usage), "days_of_cover": round(days_cover, 1),
        "run_out_date": days_from_today(days_cover),
        "below_safety_stock": bool(row.on_hand < row.safety_stock),
        "on_order_on_time": int(on_time.qty.sum()),
        "on_order_overdue": [dict(po_id=r.po_id, vendor=r.vendor_name, qty=int(r.qty), promised_date=r.promised_date,
                                  days_overdue=(today() - dt.date.fromisoformat(r.promised_date)).days)
                             for r in overdue.itertuples()],
        "storage_units_per_item": float(pt["storage_units_per_item"]),
    })


def plant_inventory(plant_id: str, part_id: str = "") -> dict:
    """WORKFLOW 3a: current inventory at a plant. One part if part_id is given, otherwise every part sorted by urgency."""
    pl = plant(plant_id)
    if part_id:
        return stock_position(pl["plant_id"], part_id)
    rows = [stock_position(pl["plant_id"], p) for p in table("parts").part_id]
    rows.sort(key=lambda r: r["days_of_cover"])
    return clean({"plant_id": pl["plant_id"], "plant_name": pl["plant_name"], "as_of": today().isoformat(),
            "parts_below_safety_stock": [r["part_id"] for r in rows if r["below_safety_stock"]],
            "parts": [{k: r[k] for k in ("part_id", "part_name", "on_hand", "safety_stock", "weekly_usage",
                                          "days_of_cover", "below_safety_stock", "on_order_on_time")} for r in rows]})


def warehouse_space(plant_id: str, part_id: str = "", qty: int = 0) -> dict:
    """WORKFLOW 3b: free warehouse space. With a part, also says how many units of it fit (and whether `qty` fits)."""
    pl = plant(plant_id)
    wh = table("warehouses").set_index("plant_id").loc[pl["plant_id"]]
    parts = table("parts").set_index("part_id")
    inv = table("inventory")
    inv = inv[inv.plant_id == pl["plant_id"]]
    stock_units = float((inv.on_hand * inv.part_id.map(parts.storage_units_per_item)).sum())
    inbound = _open_pos(pl["plant_id"])
    inbound_units = float((inbound.qty * inbound.part_id.map(parts.storage_units_per_item)).sum())
    used = float(wh.other_goods_units) + stock_units + inbound_units
    free = max(0.0, float(wh.capacity_units) - used)
    out = {"plant_id": pl["plant_id"], "capacity_units": int(wh.capacity_units), "used_units": round(used),
           "reserved_for_open_orders_units": round(inbound_units), "free_units": round(free),
           "fill_pct": round(100 * used / wh.capacity_units, 1)}
    if part_id:
        pt = part(part_id)
        su = float(pt["storage_units_per_item"])
        out.update(part_id=pt["part_id"], storage_units_per_item=su, max_items_that_fit=int(free // su))
        if qty:
            out.update(requested_qty=int(qty), fits=bool(qty * su <= free))
    return clean(out)


def nearby_stock(part_id: str, plant_id: str) -> dict:
    """WORKFLOW 1a: other plants that hold SURPLUS of this part (stock above safety stock + 6 weeks of their own use)."""
    pl, pt = plant(plant_id), part(part_id)
    lanes = table("transfer_lanes")
    lanes = lanes[(lanes.to_plant == pl["plant_id"]) & (lanes.road_miles <= config.NEARBY_MILES)]
    inv = table("inventory")
    found = []
    for ln in lanes.itertuples():
        r = inv[(inv.plant_id == ln.from_plant) & (inv.part_id == pt["part_id"])].iloc[0]
        surplus = int(r.on_hand - r.safety_stock - math.ceil(r.weekly_usage * config.COVER_WEEKS_AFTER_ARRIVAL))
        if surplus > 0:
            src = plant(ln.from_plant)
            head = _plant_head(ln.from_plant)
            found.append(dict(from_plant=ln.from_plant, plant_name=src["plant_name"],
                              plant_head=head["name"], plant_head_email=head["email"],
                              ask=f"Check with {head['name']} ({src['plant_name']} Plant Head, {head['email']}) whether they can transfer up to {surplus}.",
                              surplus_qty=surplus, road_miles=int(ln.road_miles), transfer_days=int(ln.transfer_days),
                              truck_cost_usd=int(ln.truck_cost_usd), arrives=days_from_today(ln.transfer_days)))
    found.sort(key=lambda x: x["transfer_days"])
    return clean({"part_id": pt["part_id"], "to_plant": pl["plant_id"], "plants_with_surplus": found,
            "rule": f"surplus = on hand - safety stock - {config.COVER_WEEKS_AFTER_ARRIVAL} weeks of that plant's own use; within {config.NEARBY_MILES} road miles"})


def _plant_head(plant_id):
    p = table("people")
    r = p[(p.role == "Plant Head") & (p.plant_id == plant_id)]
    return {"name": r.iloc[0]["name"], "email": r.iloc[0]["email"]} if not r.empty else {"name": plant(plant_id)["plant_head"], "email": ""}


def open_orders(plant_id: str, part_id: str = "") -> dict:
    """Open purchase orders for a plant (optionally one part): vendor, qty, value, promised date, on time or overdue."""
    pl = plant(plant_id)
    pid = part(part_id)["part_id"] if part_id else None
    po = _open_pos(pl["plant_id"], pid)
    t = today()
    rows = []
    for r in po.itertuples():
        promised = dt.date.fromisoformat(r.promised_date)
        rows.append(dict(po_id=r.po_id, vendor_id=r.vendor_id, vendor=r.vendor_name, part_id=r.part_id, qty=int(r.qty),
                         unit_price_usd=float(r.unit_price_usd), po_value_usd=float(r.po_value_usd), order_date=r.order_date,
                         promised_date=r.promised_date, status="overdue" if promised < t else "on time",
                         days_overdue=max(0, (t - promised).days)))
    rows.sort(key=lambda x: x["promised_date"])
    return clean({"plant_id": pl["plant_id"], "part_id": pid, "as_of": t.isoformat(), "open_orders": rows,
                  "total_open_value_usd": round(sum(r["po_value_usd"] for r in rows), 2)})


def find_parts(query: str = "") -> dict:
    """Look up parts by name, category or ID (e.g. 'pump', 'steel', 'P-110'). Empty query = the whole catalogue."""
    p = table("parts")
    q = str(query).strip().lower()
    words = [w.rstrip("s") for w in q.replace("-", " ").split() if len(w) > 2] if q else []
    scored = [(sum(w in f"{r.part_id} {r.part_name} {r.category}".lower() for w in words) + 10 * (r.part_id.lower() == q), r)
              for r in p.itertuples()]
    best = max((sc for sc, _ in scored), default=0)
    rows = [r for sc, r in scored if (not q) or (best > 0 and sc == best)]
    return clean({"query": query, "matches": [dict(part_id=r.part_id, part_name=r.part_name, category=r.category,
                                                   critical=r.critical == "yes") for r in rows],
                  "note": "" if rows else "No part matches; this catalogue has 20 parts. Ask for the part ID or name."})


def late_deliveries(plant_id: str, days_back: int = 90) -> dict:
    """Purchase orders that arrived late (or are still open past their promised date). Used for penalty claims."""
    pl = plant(plant_id)
    po = table("purchase_orders")
    po = po[po.plant_id == pl["plant_id"]]
    t = today()
    rows = []
    for r in po.itertuples():
        promised = dt.date.fromisoformat(r.promised_date)
        if isinstance(r.received_date, str):
            received = dt.date.fromisoformat(r.received_date)
            late = (received - promised).days
            if late > 0 and (t - received).days <= days_back:
                rows.append(dict(po_id=r.po_id, vendor_id=r.vendor_id, vendor=r.vendor_name, part_id=r.part_id, qty=int(r.qty),
                                 po_value_usd=float(r.po_value_usd), promised_date=r.promised_date,
                                 received_date=r.received_date, days_late=late, status="received late"))
        elif promised < t:
            rows.append(dict(po_id=r.po_id, vendor_id=r.vendor_id, vendor=r.vendor_name, part_id=r.part_id, qty=int(r.qty),
                             po_value_usd=float(r.po_value_usd), promised_date=r.promised_date, received_date=None,
                             days_late=(t - promised).days, status="open and overdue"))
    rows.sort(key=lambda x: -x["po_value_usd"] * x["days_late"])
    return clean({"plant_id": pl["plant_id"], "late_purchase_orders": rows})


def purchase_order(po_id: str) -> dict:
    po = table("purchase_orders")
    row = po[po.po_id.str.upper() == str(po_id).strip().upper()]
    if row.empty:
        raise NotFound(f"Unknown purchase order '{po_id}'.")
    return clean(row.iloc[0].to_dict())


def approval_route(plant_id: str, total_usd: float, vendor_risk_score: int, emergency: bool = False,
                   lowest_cost: bool = True) -> dict:
    """WORKFLOW 5a: who must approve, from the plant's procurement policy (sections 2-5). Hard rules in code.
    The Plant Head RAISES the request; approvers by total incl. freight:
      <= $25k Junior Procurement Lead | <= $100k Procurement Head | above: Procurement Head + Finance Head (2 approvals)."""
    pl = plant(plant_id)
    m = table("approval_matrix").set_index("plant_id").loc[pl["plant_id"]]
    people = table("people")
    head = people[(people.role == "Plant Head") & (people.plant_id == pl["plant_id"])].iloc[0]
    requested_by = dict(name=head["name"], email=head["email"], role="Plant Head")
    if vendor_risk_score >= config.RISK_BLOCK:
        return clean({"plant_id": pl["plant_id"], "allowed": False, "approver_role": "Procurement Head (exception)",
                      "requested_by": requested_by,
                      "reasons": [f"Vendor risk score {vendor_risk_score} is {config.RISK_BLOCK} or more: policy section 4 "
                                  "does not allow this vendor without a written exception from the Procurement Head."],
                      "policy_doc": f"POL-{pl['plant_id']}-2026"})
    levels = [["Junior Procurement Lead"], ["Procurement Head"], ["Procurement Head", "Finance Head"]]
    limits = [m.junior_procurement_lead_limit_usd, m.procurement_head_limit_usd, float("inf")]
    level = next(i for i, lim in enumerate(limits) if total_usd <= lim)
    band = ["up to $25,000", "$25,000 to $100,000", "above $100,000"][level] if (limits[0], limits[1]) == (25000, 100000) else ""
    reasons = [f"Total ${total_usd:,.0f} is in the {band or 'policy'} band: {' + '.join(levels[level])} (section 2)."]
    if emergency and level == 1 and total_usd <= m.emergency_junior_lead_limit_usd:
        level = 0
        reasons.append(f"Emergency rule (section 3): run-out within 7 days, so the Junior Procurement Lead may approve up to "
                       f"${m.emergency_junior_lead_limit_usd:,.0f}; the Procurement Head must ratify within 48 hours.")
    if vendor_risk_score >= config.RISK_BUMP:
        level = min(level + 1, 2)
        reasons.append(f"Vendor risk {vendor_risk_score} is {config.RISK_BUMP} or more: approval moves up one level (section 4).")
    signers = []
    for role in levels[level]:
        p_ = people[people.role == role].iloc[0]
        signers.append(dict(name=p_["name"], email=p_["email"], role=role))
    if not lowest_cost:
        reasons.append("Selected vendor is not the lowest total cost: the request must say why (section 5).")
    return clean({"plant_id": pl["plant_id"], "allowed": True, "approver_role": " + ".join(levels[level]),
                  "approvals_needed": len(signers), "signers": signers, "requested_by": requested_by,
                  "justification_required": not lowest_cost, "reasons": reasons, "policy_doc": f"POL-{pl['plant_id']}-2026"})
