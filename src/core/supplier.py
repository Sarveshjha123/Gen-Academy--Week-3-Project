"""SUPPLIER system (think: Coupa / Ariba). Vendors, contract price lines, quotes, risk scores.
Plain code (WORKFLOWS), exposed to the agent through the Supplier MCP server.
"""
from src import config
from src.core.data import clean, part, plant, table, today, vendor


def vendor_risk(vendor_id: str) -> dict:
    """WORKFLOW 4: vendor risk score 0-100 (higher = riskier).
    LIMITATION: the score is SIMULATED (random, fixed seed). See README for the planned real model."""
    v = vendor(vendor_id)
    band_rule = (f"High >= {config.RISK_BLOCK} (blocked without exception), Medium >= 50, Low < 50; "
                 f">= {config.RISK_BUMP} moves approval up one level")
    return clean({**v, "band_rule": band_rule,
                  "limitation": "Simulated score. Planned: build it from on-time delivery, defect rate, damage claims, "
                                "financial health and news (workflow + RAG)."})


def contract_status(vendor_id: str) -> dict:
    """Contract register entry: dates only (detailed terms live in the contract document)."""
    v = vendor(vendor_id)
    c = table("contracts")
    row = c[c.vendor_id == v["vendor_id"]].iloc[0].to_dict()
    import datetime as dt
    exp = dt.date.fromisoformat(row["expiry_date"])
    row["days_to_expiry"] = (exp - today()).days
    row["status"] = "EXPIRED" if row["days_to_expiry"] < 0 else "active"
    if row["auto_renew"] in (True, "True"):
        decide_by = exp - dt.timedelta(days=int(row["non_renewal_notice_days"]))
        row["renewal_decision_by"] = decide_by.isoformat()
        row["days_to_renewal_decision"] = (decide_by - today()).days
    return clean(row)


def vendors_for_part(part_id: str) -> dict:
    """Every vendor with a contract price line for this part: rate, MOQ, lead time, freight, contract status, risk."""
    pt = part(part_id)
    lines = table("contract_price_lines")
    lines = lines[lines.part_id == pt["part_id"]]
    risk = table("vendor_risk_scores").set_index("vendor_id")
    out = []
    for r in lines.itertuples():
        cs = contract_status(r.vendor_id)
        out.append(dict(vendor_id=r.vendor_id, vendor_name=r.vendor_name, contract_id=r.contract_id,
                        contract_rate_usd=float(r.contract_rate_usd), moq=int(r.moq), lead_time_days=int(r.lead_time_days),
                        freight_per_order_usd=float(r.freight_per_order_usd), contract_expiry=cs["expiry_date"],
                        contract_status=cs["status"], risk_score=int(risk.loc[r.vendor_id, "risk_score"]),
                        risk_band=risk.loc[r.vendor_id, "risk_band"]))
    return clean({"part_id": pt["part_id"], "part_name": pt["part_name"], "vendors": out})


def latest_quotes(part_id: str) -> dict:
    """Latest quote from each vendor for this part, flagged expired / above contract rate."""
    pt = part(part_id)
    q = table("quotes")
    q = q[q.part_id == pt["part_id"]]
    lines = table("contract_price_lines").set_index(["part_id", "vendor_id"])
    t = today().isoformat()
    out = []
    for r in q.itertuples():
        rate = float(lines.loc[(pt["part_id"], r.vendor_id), "contract_rate_usd"])
        out.append(dict(quote_id=r.quote_id, vendor_id=r.vendor_id, vendor_name=r.vendor_name,
                        unit_price_usd=float(r.unit_price_usd), valid_until=r.valid_until,
                        expired=r.valid_until < t, contract_rate_usd=rate,
                        above_contract_rate=float(r.unit_price_usd) > rate))
    return clean({"part_id": pt["part_id"], "quotes": out,
                  "rule": "Policy 5: expired quotes may not be used. Contract section 2: quotes above the contract price are not valid."})


def price_overcharges(plant_id: str) -> dict:
    """IDEA 3: open purchase orders priced above the contract rate (money about to be overpaid)."""
    pl = plant(plant_id)
    po = table("purchase_orders")
    po = po[(po.plant_id == pl["plant_id"]) & (po.status == "open")]
    lines = table("contract_price_lines").set_index(["part_id", "vendor_id"])
    rows = []
    for r in po.itertuples():
        rate = float(lines.loc[(r.part_id, r.vendor_id), "contract_rate_usd"])
        if r.unit_price_usd > rate + 1e-9:
            rows.append(dict(po_id=r.po_id, vendor_id=r.vendor_id, vendor=r.vendor_name, part_id=r.part_id, qty=int(r.qty),
                             po_unit_price_usd=float(r.unit_price_usd), contract_rate_usd=rate,
                             overcharge_usd=round((r.unit_price_usd - rate) * r.qty, 2), promised_date=r.promised_date))
    rows.sort(key=lambda x: -x["overcharge_usd"])
    return clean({"plant_id": pl["plant_id"], "overcharged_open_orders": rows,
            "total_overcharge_usd": round(sum(r["overcharge_usd"] for r in rows), 2)})


def contracts_needing_attention(plant_id: str, within_days: int = 60) -> dict:
    """Contracts of vendors this plant buys from that expire, or need a renewal decision, soon (or already expired)."""
    pl = plant(plant_id)
    po = table("purchase_orders")
    used = set(po[po.plant_id == pl["plant_id"]].vendor_id)
    rows = []
    for vid in sorted(used):
        cs = contract_status(vid)
        soon = cs["days_to_expiry"] <= within_days or (cs.get("days_to_renewal_decision") is not None
                                                      and cs["days_to_renewal_decision"] <= within_days)
        if soon:
            rows.append(cs)
    rows.sort(key=lambda r: min(r["days_to_expiry"], r.get("days_to_renewal_decision", 9999)))
    return clean({"plant_id": pl["plant_id"], "contracts": rows})
