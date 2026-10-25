"""Creates ALL the fictional data for Northfield Industrial (run once:  python scripts/generate_data.py).

Everything is invented. The random parts use a fixed seed, so every run gives the same data.
Some situations are planted on purpose so the agent has real judgement calls to make
(cheapest vendor is high-risk, a quote has expired, a nearby plant has spare stock, the warehouse is nearly full...).
"""
import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA, CONTRACTS, POLICIES, TRUTH = ROOT / "data", ROOT / "docs/contracts", ROOT / "docs/policies", ROOT / "eval/truth"
TODAY = date(2026, 10, 26)          # a Monday morning
rng = random.Random(42)


def write_csv(name, rows):
    with open(DATA / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def d(days):
    return (TODAY + timedelta(days=days)).isoformat()


# ------------------------------------------------------------------ PLANTS
plants = [
    dict(plant_id="AUS", plant_name="Austin", state="TX", lat=30.27, lon=-97.74, plant_head="Dana Whitfield", warehouse_capacity_units=9000),
    dict(plant_id="DAL", plant_name="Dallas", state="TX", lat=32.78, lon=-96.80, plant_head="Marcus Bell", warehouse_capacity_units=11000),
    dict(plant_id="HOU", plant_name="Houston", state="TX", lat=29.76, lon=-95.37, plant_head="Priya Raman", warehouse_capacity_units=10000),
    dict(plant_id="PHX", plant_name="Phoenix", state="AZ", lat=33.45, lon=-112.07, plant_head="Luis Ortega", warehouse_capacity_units=8000),
    dict(plant_id="DEN", plant_name="Denver", state="CO", lat=39.74, lon=-104.99, plant_head="Hannah Cole", warehouse_capacity_units=7500),
]
write_csv("plants.csv", plants)


def road_miles(a, b):
    r = 3959
    la1, lo1, la2, lo2 = map(math.radians, [a["lat"], a["lon"], b["lat"], b["lon"]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(h)) * 1.2)   # x1.2 = roads are not straight lines


lanes = []
for a in plants:
    for b in plants:
        if a["plant_id"] != b["plant_id"]:
            miles = road_miles(a, b)
            lanes.append(dict(from_plant=a["plant_id"], to_plant=b["plant_id"], road_miles=miles,
                              transfer_days=math.ceil(miles / 450) + 1, truck_cost_usd=round(400 + miles * 1.6)))
write_csv("transfer_lanes.csv", lanes)

# ------------------------------------------------------------------ PARTS (storage_units = warehouse space one item uses)
parts = [
    ("P-110", "Hydraulic Pump HP-40", "Hydraulics", 1850, 6.0, "yes"),
    ("P-120", "Servo Motor 2kW", "Motion", 1240, 3.0, "yes"),
    ("P-130", "Ball Bearing 6205", "Bearings", 6.8, 0.05, "no"),
    ("P-140", "Gearbox GX-20", "Motion", 2900, 8.0, "yes"),
    ("P-150", "Proximity Sensor PX-12", "Sensors", 48, 0.1, "no"),
    ("P-160", "PLC Controller C-500", "Controls", 3400, 1.5, "yes"),
    ("P-170", "Conveyor Belt 10m", "Conveying", 620, 4.0, "no"),
    ("P-180", "Pneumatic Valve PV-3", "Pneumatics", 135, 0.3, "no"),
    ("P-190", "Hydraulic Hose 2m", "Hydraulics", 42, 0.4, "no"),
    ("P-200", "Industrial Gasket Kit", "Seals", 18, 0.1, "no"),
    ("P-210", "Steel Sheet 2mm", "Metals", 95, 1.2, "no"),
    ("P-220", "Aluminium Extrusion 6m", "Metals", 64, 1.5, "no"),
    ("P-230", "Welding Wire Spool", "Consumables", 88, 0.6, "no"),
    ("P-240", "Safety Relay SR-2", "Controls", 210, 0.1, "yes"),
    ("P-250", "VFD Drive 5kW", "Motion", 980, 2.0, "yes"),
    ("P-260", "Coolant 20L", "Consumables", 74, 0.8, "no"),
    ("P-270", "Linear Guide Rail", "Motion", 310, 1.0, "no"),
    ("P-280", "Fastener Kit M8", "Fasteners", 22, 0.2, "no"),
    ("P-290", "Power Supply 24V", "Controls", 160, 0.3, "no"),
    ("P-300", "Filter Cartridge FC-9", "Hydraulics", 36, 0.3, "no"),
]
write_csv("parts.csv", [dict(part_id=p, part_name=n, category=c, typical_price_usd=pr, storage_units_per_item=su, critical=cr)
                        for p, n, c, pr, su, cr in parts])
PART = {p[0]: p for p in parts}

# ------------------------------------------------------------------ VENDORS
# (id, name, city/region, country, categories they supply)
vendors = [
    ("V01", "Apex Fluid Systems", "San Antonio, TX", "USA", ["Hydraulics"]),
    ("V02", "Brightline Motion", "Dayton, OH", "USA", ["Motion"]),
    ("V03", "Cobalt Bearings Co.", "Rockford, IL", "USA", ["Bearings", "Fasteners"]),
    ("V04", "Delta Drive Components", "Monterrey, NL", "Mexico", ["Motion", "Controls"]),
    ("V05", "Evergreen Industrial Supply", "Fresno, CA", "USA", ["Hydraulics", "Consumables", "Seals", "Pneumatics"]),
    ("V06", "Forge & Fastener Inc.", "Allentown, PA", "USA", ["Fasteners", "Metals"]),
    ("V07", "Granite Controls", "St. Paul, MN", "USA", ["Controls", "Sensors"]),
    ("V08", "Harbor Steel Works", "Beaumont, TX", "USA", ["Metals"]),
    ("V09", "Ironwood Hydraulics", "Tulsa, OK", "USA", ["Hydraulics"]),
    ("V10", "Juniper Electric", "Charlotte, NC", "USA", ["Motion", "Controls"]),
    ("V11", "Keystone Pneumatics", "Erie, PA", "USA", ["Pneumatics", "Seals", "Conveying"]),
    ("V12", "Lumen Sensors", "Hsinchu", "Taiwan", ["Sensors", "Controls"]),
    ("V13", "Meridian Metals", "Birmingham, AL", "USA", ["Metals", "Consumables"]),
    ("V14", "Northstar Automation", "Hamilton, ON", "Canada", ["Hydraulics", "Motion", "Controls", "Conveying", "Bearings"]),
    ("V15", "Orion Seals & Gaskets", "Grand Rapids, MI", "USA", ["Seals", "Hydraulics", "Bearings", "Consumables", "Fasteners", "Sensors", "Pneumatics", "Conveying"]),
]
VN = {v[0]: v[1] for v in vendors}

# ------------------------------------------------------------------ SIMULATED VENDOR RISK (random on purpose; see README limitation)
risk_override = {"V09": 81, "V04": 77, "V12": 63, "V14": 28, "V01": 34, "V05": 41}
risk_rows = []
for vid, *_ in vendors:
    score = risk_override.get(vid, rng.randint(18, 58))
    parts_ = {k: max(0, min(100, score + rng.randint(-15, 15))) for k in ["delivery", "quality", "financial", "geo_supply"]}
    band = "High" if score >= 75 else "Medium" if score >= 50 else "Low"
    risk_rows.append(dict(vendor_id=vid, vendor_name=VN[vid], risk_score=score, risk_band=band,
                          delivery_risk=parts_["delivery"], quality_risk=parts_["quality"],
                          financial_risk=parts_["financial"], geo_supply_risk=parts_["geo_supply"],
                          method="SIMULATED (random, seed 42)", scored_on=d(-3)))
write_csv("vendor_risk_scores.csv", risk_rows)

# ------------------------------------------------------------------ CONTRACT TERMS (the "truth" behind each contract document)
penalty_styles = {  # rate, per, grace_days, cap_pct  (None = no delay penalty clause)
    "V01": (1.5, "week", 5, 10), "V02": (2.0, "week", 3, 12), "V03": (1.0, "week", 7, 8), "V04": (0.5, "day", 2, 10),
    "V05": (1.5, "week", 5, 10), "V06": (1.0, "week", 5, 6), "V07": (2.0, "week", 5, 15), "V08": (0.25, "day", 3, 8),
    "V09": (1.0, "week", 10, 5), "V10": (2.0, "week", 5, 10), "V11": (1.5, "week", 5, 9), "V12": None,
    "V13": (1.0, "week", 5, 7), "V14": (2.5, "week", 3, 12), "V15": (1.0, "week", 5, 8),
}
expiry = {"V04": d(-26), "V07": d(50), "V12": d(140), "V03": d(95), "V08": d(75)}   # V04 already expired, V07 expires soon
terms = {}
for i, (vid, name, city, country, cats) in enumerate(vendors):
    start = TODAY - timedelta(days=rng.randint(300, 900))
    exp = expiry.get(vid, d(rng.randint(200, 700)))
    pen = penalty_styles[vid]
    terms[vid] = dict(
        vendor_id=vid, vendor_name=name, contract_id=f"MSA-{vid}-{start.year}",
        effective_date=start.isoformat(), expiry_date=exp,
        auto_renew=vid not in ("V04", "V12"), non_renewal_notice_days=rng.choice([30, 45, 60, 90]),
        payment_terms=rng.choice(["Net 30", "Net 45", "Net 60", "2/10 Net 30"]),
        price_escalation_cap_pct=rng.choice([3, 4, 5, 6]),
        fill_rate_sla_pct=rng.choice([95, 97, 98, 99]),
        delay_penalty=None if pen is None else dict(rate_pct=pen[0], per=pen[1], grace_days=pen[2], cap_pct=pen[3]),
        damage_claim_window_days=rng.choice([5, 7, 10, 14, 30]),
        damage_remedy=rng.choice(["replace at vendor cost", "full credit or replacement at buyer's choice", "credit of invoice value"]),
        damage_replacement_days=rng.choice([5, 7, 10, 15]),
        return_freight_paid_by=rng.choice(["vendor", "vendor", "buyer"]),
        warranty_months=rng.choice([6, 12, 18, 24]),
        liability_cap=rng.choice(["fees paid in the prior 12 months", "2x annual contract value", "USD 1,000,000"]),
        termination_notice_days=rng.choice([30, 60, 90]),
        governing_law=rng.choice(["Texas", "Delaware", "New York"]),
    )
if TODAY.isoformat() > terms["V07"]["expiry_date"]:
    raise SystemExit("bad")
terms["V07"]["non_renewal_notice_days"] = 30      # decision needed within ~20 days
terms["V07"]["auto_renew"] = True
terms["V08"]["non_renewal_notice_days"] = 60     # Dallas: Harbor Steel renewal decision due in 15 days
terms["V08"]["auto_renew"] = True

# ------------------------------------------------------------------ WHICH VENDOR SELLS WHICH PART (contract price lines)
supply = {}
for pid, pname, cat, price, su, crit in parts:
    sellers = [v[0] for v in vendors if cat in v[4]]
    rng.shuffle(sellers)
    supply[pid] = sorted(sellers[: max(3, min(5, len(sellers)))])
supply["P-110"] = ["V01", "V05", "V09", "V14"]        # planted scenario (see below)
supply["P-130"] = ["V03", "V14", "V15"]
supply["P-250"] = ["V02", "V04", "V10", "V14"]
supply["P-160"] = ["V04", "V07", "V10", "V12", "V14"]

special_lines = {   # (rate, moq, lead_days, freight)  planted values
    ("P-110", "V01"): (1820, 4, 21, 180), ("P-110", "V05"): (1880, 2, 10, 240), ("P-110", "V09"): (1640, 6, 18, 150),
    ("P-110", "V14"): (1950, 4, 14, 320), ("P-130", "V03"): (6.80, 500, 12, 90), ("P-130", "V14"): (7.10, 1000, 20, 260),
    ("P-130", "V15"): (6.95, 250, 9, 120),
}
lines = []
for pid, sellers in supply.items():
    base = PART[pid][3]
    for vid in sellers:
        rate, moq, lead, freight = special_lines.get((pid, vid), (
            round(base * rng.uniform(0.9, 1.12), 2 if base < 100 else 0),
            rng.choice([1, 2, 5, 10]) if base > 500 else rng.choice([50, 100, 200, 500]) if base < 50 else rng.choice([10, 20, 25]),
            rng.choice([7, 10, 14, 21, 28, 35]) + (14 if VN and vid in ("V12", "V04") else 0),
            rng.choice([90, 120, 150, 200, 260]) + (300 if vid == "V12" else 0)))
        lines.append(dict(contract_id=terms[vid]["contract_id"], vendor_id=vid, vendor_name=VN[vid], part_id=pid,
                          part_name=PART[pid][1], contract_rate_usd=rate, moq=moq, lead_time_days=lead,
                          freight_per_order_usd=freight))
write_csv("contract_price_lines.csv", lines)
LINE = {(l["part_id"], l["vendor_id"]): l for l in lines}
for vid in terms:
    terms[vid]["price_lines"] = {l["part_id"]: l["contract_rate_usd"] for l in lines if l["vendor_id"] == vid}

# ------------------------------------------------------------------ LATEST QUOTES
quotes, qn = [], 5000
for l in lines:
    qn += 1
    k = (l["part_id"], l["vendor_id"])
    rate = float(l["contract_rate_usd"])
    price = round(rate * rng.uniform(0.95, 1.06), 2)
    valid = d(rng.choice([-12, -4, 9, 14, 21, 30, 45]))
    planted = {("P-110", "V01"): (1790, d(18)), ("P-110", "V05"): (1860, d(-6)), ("P-110", "V09"): (1610, d(25)),
               ("P-110", "V14"): (1935, d(30)), ("P-130", "V03"): (6.75, d(20)), ("P-130", "V15"): (7.40, d(15)),
               ("P-130", "V14"): (7.05, d(-2)), ("P-300", "V01"): (39.06, d(5))}
    if k in planted:
        price, valid = planted[k]
    quotes.append(dict(quote_id=f"Q-{qn}", vendor_id=l["vendor_id"], vendor_name=l["vendor_name"], part_id=l["part_id"],
                       unit_price_usd=price, quoted_on=(date.fromisoformat(valid) - timedelta(days=30)).isoformat(),
                       valid_until=valid, lead_time_days_quoted=l["lead_time_days"]))
write_csv("quotes.csv", quotes)

# ------------------------------------------------------------------ INVENTORY (per plant per part)
inv = []
for p in plants:
    for pid, pname, cat, price, su, crit in parts:
        weekly = max(1, round((40 / max(price, 1) ** 0.5) * rng.uniform(0.6, 1.6) * (30 if price < 50 else 1)))
        safety = math.ceil(weekly * rng.choice([1.5, 2, 2.5]))
        on_hand = math.ceil(weekly * rng.uniform(3, 9))
        inv.append(dict(plant_id=p["plant_id"], part_id=pid, on_hand=on_hand, safety_stock=safety,
                        weekly_usage=weekly, on_order=0))
INV = {(r["plant_id"], r["part_id"]): r for r in inv}
planted_inv = {   # (on_hand, safety, weekly, on_order)
    ("AUS", "P-110"): (6, 8, 4, 0), ("DAL", "P-110"): (38, 6, 2, 0), ("HOU", "P-110"): (9, 6, 3, 0),
    ("PHX", "P-110"): (14, 4, 2, 0), ("DEN", "P-110"): (5, 4, 2, 4),
    ("AUS", "P-130"): (900, 1200, 600, 0), ("DAL", "P-130"): (2600, 900, 450, 0),
    ("AUS", "P-250"): (11, 6, 3, 0), ("AUS", "P-160"): (7, 3, 1, 0),
    ("HOU", "P-120"): (3, 4, 2, 0), ("PHX", "P-210"): (40, 60, 35, 0), ("DEN", "P-240"): (6, 10, 5, 0),
    ("AUS", "P-210"): (50, 80, 60, 0),   # big, bulky order needed -> warehouse space limits it
    ("DAL", "P-140"): (1, 3, 1, 0),      # Dallas: gearbox about to run out; cheapest vendor's contract expired
}
for k, (oh, ss, wk, oo) in planted_inv.items():
    INV[k].update(on_hand=oh, safety_stock=ss, weekly_usage=wk, on_order=oo)
write_csv("inventory.csv", inv)

# warehouse: make Austin ~93% full, others 60-80%
wh = []
for p in plants:
    used = sum(INV[(p["plant_id"], pid)]["on_hand"] * PART[pid][4] for pid in PART)
    target = 0.93 if p["plant_id"] == "AUS" else rng.uniform(0.6, 0.8)
    other = round(p["warehouse_capacity_units"] * target - used)       # space taken by finished goods, WIP etc.
    wh.append(dict(plant_id=p["plant_id"], capacity_units=p["warehouse_capacity_units"],
                   other_goods_units=max(0, other), reserved_inbound_units=0))
write_csv("warehouses.csv", wh)

# ------------------------------------------------------------------ PURCHASE ORDERS (history + open)
pos, n = [], 24100
def add_po(plant, part, vendor, qty, unit, ordered, promised, received, note=""):
    global n
    n += 1
    pos.append(dict(po_id=f"PO-{n}", plant_id=plant, vendor_id=vendor, vendor_name=VN[vendor], part_id=part, qty=qty,
                    unit_price_usd=unit, po_value_usd=round(qty * unit, 2), order_date=ordered, promised_date=promised,
                    received_date=received, status="received" if received else "open", note=note))

for _ in range(36):
    pl = rng.choice(plants)["plant_id"]
    pid = rng.choice(list(supply))
    vid = rng.choice(supply[pid])
    li = LINE[(pid, vid)]
    qty = int(li["moq"]) * rng.randint(1, 4)
    od = -rng.randint(20, 120)
    prom = od + int(li["lead_time_days"])
    late = rng.choice([0, 0, 0, 0, 1, 2, -1])
    rec = prom + late if prom + late < 0 else None
    add_po(pl, pid, vid, qty, float(li["contract_rate_usd"]), d(od), d(prom), d(rec) if rec is not None else None)

# planted, for the morning brief + penalty claims + overcharges
add_po("AUS", "P-250", "V10", 30, 980.0, d(-70), d(-40), d(-14), "received 26 days late")          # penalty claim
add_po("AUS", "P-120", "V02", 12, float(LINE[("P-120", "V02")]["contract_rate_usd"]) if ("P-120", "V02") in LINE else 1240.0,
       d(-45), d(-20), d(-9), "received 11 days late")
add_po("AUS", "P-130", "V03", 4000, 7.45, d(-6), d(6), None, "unit price above contract rate")      # overcharge
add_po("AUS", "P-110", "V09", 6, 1640.0, d(-30), d(-8), None, "open and overdue, high-risk vendor")  # late + risky
add_po("AUS", "P-160", "V07", 2, 3400.0, d(-10), d(12), None, "")
add_po("DAL", "P-210", "V08", 300, 108.50, d(-15), d(5), None, "unit price above contract rate")
add_po("HOU", "P-120", "V02", 8, 1290.0, d(-28), d(-3), None, "open and overdue")
add_po("PHX", "P-210", "V13", 200, 99.0, d(-40), d(-18), d(-2), "received 16 days late")
add_po("DAL", "P-120", "V04", 4, 1312.0, d(-25), d(10), None, "open; supplier contract expired")      # Dallas red
add_po("DAL", "P-130", "V14", 3000, 7.10, d(-50), d(-30), d(-12), "received 18 days late")          # Dallas claim
write_csv("purchase_orders.csv", pos)

# ------------------------------------------------------------------ APPROVAL HIERARCHY (same for every plant)
# The Plant Head RAISES the request (sender). Approvers by PO total incl. freight:
#   <= 25,000 Junior Procurement Lead | <= 100,000 Procurement Head | above: Procurement Head + Finance Head
JUNIOR_LIMIT, HEAD_LIMIT, EMERGENCY_JUNIOR_LIMIT = 25000, 100000, 50000
matrix = [(p["plant_id"], JUNIOR_LIMIT, HEAD_LIMIT, EMERGENCY_JUNIOR_LIMIT) for p in plants]
write_csv("approval_matrix.csv", [dict(plant_id=a, junior_procurement_lead_limit_usd=b, procurement_head_limit_usd=c,
                                       emergency_junior_lead_limit_usd=e) for a, b, c, e in matrix])
people = [dict(role="Plant Head", name=p["plant_head"], email=p["plant_head"].lower().replace(" ", ".") + "@northfield.example", plant_id=p["plant_id"]) for p in plants]
people += [dict(role="Junior Procurement Lead", name="Kevin O'Neill", email="kevin.oneill@northfield.example", plant_id="ALL"),
           dict(role="Procurement Head", name="Rachel Stein", email="rachel.stein@northfield.example", plant_id="ALL"),
           dict(role="Finance Head", name="Thomas Grey", email="thomas.grey@northfield.example", plant_id="ALL")]
write_csv("people.csv", people)

# ------------------------------------------------------------------ CONTRACT DOCUMENTS (long, so search over them is worth it)
WORDS = {1: "one", 2: "two", 3: "three", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 12: "twelve",
         14: "fourteen", 15: "fifteen", 18: "eighteen", 24: "twenty-four", 30: "thirty", 45: "forty-five", 60: "sixty", 90: "ninety"}


def w(nm):
    return f"{WORDS.get(nm, str(nm))} ({nm})"


def pct(x):
    return f"{x:g}%"


def long_date(iso):
    return date.fromisoformat(iso).strftime("%B %d, %Y").replace(" 0", " ")


style_variants = [
    "Supplier shall pay Buyer liquidated damages equal to {rate} of the value of the delayed line items for each full {per} of delay beyond a grace period of {grace} days after the Promised Delivery Date. Total liquidated damages for any Purchase Order shall not exceed {cap} of that Purchase Order's value.",
    "If any delivery arrives after the Promised Delivery Date plus {grace} days, Supplier will credit Buyer {rate} of the late shipment's invoice value per {per} (or part thereof counted as full) of delay, capped at {cap} of the affected Purchase Order.",
    "Late Delivery Credit: for deliveries late by more than {grace} days, a credit of {rate} of the delayed goods' value accrues per {per} of lateness. The aggregate credit is limited to {cap} of the Purchase Order value.",
]
for vid, t in terms.items():
    name, city, country = VN[vid], next(v[2] for v in vendors if v[0] == vid), next(v[3] for v in vendors if v[0] == vid)
    pen = t["delay_penalty"]
    if pen:
        style = rng.randrange(len(style_variants))
        if style == 1 and pen["per"] == "week":     # "or part thereof counted as full" => partial weeks count
            t["delay_penalty"]["per"] = "week_or_part"
        clause = style_variants[style].format(rate=pct(pen["rate_pct"]), per=pen["per"], grace=w(pen["grace_days"]),
                                                   cap=pct(pen["cap_pct"]))
        clause += " Liquidated damages are Buyer's sole monetary remedy for late delivery, without prejudice to termination rights for repeated failure."
    else:
        clause = ("Supplier will use commercially reasonable efforts to meet Promised Delivery Dates. No liquidated damages, "
                  "credits or penalties apply to late delivery under this Agreement. Buyer's remedy for persistent lateness "
                  "is limited to termination under Section 10.")
    renew = (f"This Agreement renews automatically for successive one-year terms unless either party gives written notice of "
             f"non-renewal at least {w(t['non_renewal_notice_days'])} days before the end of the then-current term."
             if t["auto_renew"] else
             "This Agreement does not renew automatically. Any extension must be agreed in a signed written amendment.")
    table = "\n".join(f"| {pid} | {PART[pid][1]} | USD {LINE[(pid, vid)]['contract_rate_usd']:,} | {LINE[(pid, vid)]['moq']} | "
                      f"{LINE[(pid, vid)]['lead_time_days']} days |" for pid in t["price_lines"])
    doc = f"""---
doc_id: {t['contract_id']}
doc_type: vendor_contract
vendor_id: {vid}
vendor_name: {name}
title: Master Supply Agreement - {name}
---
# Master Supply Agreement: {name}

> Fictional document created for a course project. Northfield Industrial and {name} do not exist.

This Master Supply Agreement (the "Agreement") is made between Northfield Industrial, Inc. ("Buyer") and {name}, with offices in {city}, {country} ("Supplier"). Agreement number {t['contract_id']}.

## 1. Term and Renewal
This Agreement takes effect on {long_date(t['effective_date'])} and remains in force until {long_date(t['expiry_date'])} (the "Expiry Date"), unless terminated earlier under Section 10. {renew}

## 2. Products and Contract Prices
Supplier will sell the products listed in Appendix A at the contract unit prices shown there. Contract prices include standard packaging. Prices exclude sales tax and the per-order freight charge stated in Appendix A. Any quotation issued by Supplier at a price above the Appendix A price is not valid for products covered by this Agreement unless Buyer's Procurement Head accepts it in writing.

## 3. Price Adjustments
Supplier may request a price adjustment no more than once in any twelve-month period, with sixty (60) days' written notice. Any increase shall not exceed {pct(t['price_escalation_cap_pct'])} in a twelve-month period and must be supported by documented changes in raw-material or labor costs.

## 4. Orders, Minimum Quantities and Lead Times
Buyer places Purchase Orders ("POs") from time to time. Each PO must meet the minimum order quantity ("MOQ") in Appendix A. Supplier confirms each PO within two (2) business days and commits to a Promised Delivery Date no later than the standard lead time shown in Appendix A.

## 5. Delivery, Fulfilment and Late Delivery
5.1 Fill rate. Supplier shall fulfil at least {pct(t['fill_rate_sla_pct'])} of ordered units complete and on time, measured each calendar quarter.
5.2 Late delivery. {clause}
5.3 Partial shipments require Buyer's prior consent.

## 6. Inspection, Damage and Defects
Buyer will inspect goods on receipt. Buyer must notify Supplier of visible damage, shortage or defects within {w(t['damage_claim_window_days'])} days of receipt. For goods damaged in transit or found defective, Supplier's remedy is {t['damage_remedy']}; replacements ship within {w(t['damage_replacement_days'])} days of the claim. Return freight for damaged or defective goods is paid by the {t['return_freight_paid_by']}. Claims notified after the claim window are accepted only at Supplier's discretion.

## 7. Warranty
Supplier warrants that products are free from defects in materials and workmanship for {w(t['warranty_months'])} months from delivery and conform to the agreed specifications.

## 8. Invoicing and Payment
Supplier invoices on shipment. Payment terms are {t['payment_terms']} from the date a correct invoice is received. Invoices that do not match the PO price are held until corrected.

## 9. Insurance and Liability
Supplier maintains general liability insurance of at least USD 2,000,000 per occurrence. Except for injury, fraud or confidentiality breaches, each party's total liability under this Agreement is limited to {t['liability_cap']}.

## 10. Termination
Either party may terminate this Agreement for convenience with {w(t['termination_notice_days'])} days' written notice. Buyer may terminate immediately if Supplier misses the fill-rate commitment in Section 5.1 for two consecutive quarters.

## 11. Force Majeure
Neither party is liable for delays caused by events beyond its reasonable control, provided it notifies the other party within five (5) days and uses reasonable efforts to resume performance. Force majeure does not excuse payment obligations.

## 12. General
This Agreement is governed by the laws of the State of {t['governing_law']}. It is the entire agreement between the parties for its subject matter and may be changed only in a signed writing.

## Appendix A: Contract Price Schedule

| Part | Description | Contract unit price | MOQ | Standard lead time |
|---|---|---|---|---|
{table}

Freight per order: as listed in Buyer's contract register for each part.
"""
    slug = name.lower().replace("&", "and").replace(".", "").replace(" ", "-")
    (CONTRACTS / f"{vid}_{slug}.md").write_text(doc)

(TRUTH / "contract_terms.json").write_text(json.dumps(terms, indent=2))

# contract register = what a contract-management system stores (dates only; the detailed terms live in the documents)
write_csv("contracts.csv", [dict(contract_id=t["contract_id"], vendor_id=v, vendor_name=t["vendor_name"],
                                 effective_date=t["effective_date"], expiry_date=t["expiry_date"],
                                 auto_renew=t["auto_renew"], non_renewal_notice_days=t["non_renewal_notice_days"])
                            for v, t in terms.items()])

# ------------------------------------------------------------------ PLANT PROCUREMENT POLICIES (one per plant)
for (pl, jr, hd, em), p in zip(matrix, plants):
    doc = f"""---
doc_id: POL-{pl}-2026
doc_type: plant_policy
plant_id: {pl}
title: {p['plant_name']} Plant Procurement and Approval Policy
---
# {p['plant_name']} Plant Procurement and Approval Policy (2026)

> Fictional document created for a course project.

## 1. Purpose
This policy sets who may approve purchases for the {p['plant_name']} plant ({pl}) and what every purchase request must contain.

## 2. Approval Levels
The Plant Head ({p['plant_head']}) raises every purchase request for the plant but does not approve it. Approval is based on the total purchase order value including freight:
- Up to USD {jr:,}: the Junior Procurement Lead (Kevin O'Neill) approves.
- Above USD {jr:,} and up to USD {hd:,}: the Procurement Head (Rachel Stein) approves and signs.
- Above USD {hd:,}: two approvals are required, from the Procurement Head (Rachel Stein) and the Finance Head (Thomas Grey).

## 3. Emergency Purchases
If a part will run out within seven (7) days and the shortage would stop production, the Junior Procurement Lead may approve up to USD {em:,}. The purchase must be ratified by the Procurement Head within 48 hours.

## 4. Vendor Risk Rules
- A vendor with a risk score of 60 or more moves the approval up one level.
- A vendor with a risk score of 75 or more may not be used without a written exception from the Procurement Head.
- Vendor risk scores are refreshed monthly by the Supplier Quality team.

## 5. Vendor Selection
- Use a vendor with a valid master supply agreement that is in force through the expected delivery date.
- Compare at least three eligible vendors where three exist.
- If the selected vendor is not the lowest total cost, the request must state why (for example faster delivery or lower risk).
- Quotes past their validity date may not be used; use the contract price or request a new quote.

## 6. Inter-Plant Transfers
Before buying, check whether another Northfield plant holds surplus stock. A transfer needs agreement from both Plant Heads and is not a purchase, so the approval levels in Section 2 do not apply.

## 7. What a Purchase Request Must Contain
Every request to an approver must state: the part and quantity; why it is needed (current stock, usage and run-out date); the vendor selected and why; the other vendors compared; total cost including freight; expected delivery date; and the approval level that applies.

## 8. Warehouse Space
Orders must fit within available warehouse space at the receiving plant. Where space is short, split deliveries or use a smaller order quantity.
"""
    (POLICIES / f"POL-{pl}-2026.md").write_text(doc)

# ------------------------------------------------------------------ COMPANY-WIDE POLICIES (used by the policy-advisor sub-agent)
(POLICIES / "POL-GLOBAL-CONTRACTS-2026.md").write_text("""---
doc_id: POL-GLOBAL-CONTRACTS-2026
doc_type: company_policy
plant_id: GLOBAL
title: Contract Management Policy
---
# Contract Management Policy (2026)

> Fictional document created for a course project.

## 1. Purpose
This policy applies to every Northfield plant. It covers contracts in force, renewals, pricing, late-delivery credits, high-risk vendors and quotes.

## 2. Contract Must Be in Force
No new purchase order may be placed and no delivery accepted under an expired contract. If a vendor's contract has expired while purchase orders are still open, the Plant Head must either (a) request a written exception from the Procurement Head allowing those open orders to complete (maximum 90 days), or (b) cancel the open orders and re-source from an eligible vendor with a valid contract.

## 3. Renewal Decisions
A renewal decision must be made before the non-renewal notice date in the contract. Recommend:
- Renew, when on-time delivery is 95% or better and the vendor risk score is below 60.
- Renegotiate, when on-time delivery is between 85% and 95%, the risk score is 60 or more, or the contract has no late-delivery credit.
- Exit and run a request for quotation (RFQ) with at least two other vendors, when on-time delivery is below 85%.
Every renewal, renegotiation or exit recommendation is sent to the Procurement Head for approval. If the Procurement Head does not approve the recommendation, the Plant Head may send a non-renewal notice to keep options open, or request quotes from alternative vendors.

## 4. Prices Above the Contract Rate
Prices above the contract rate are not payable. The plant must ask the vendor to correct the purchase order or invoice to the contract rate. Accepting a higher price requires the Procurement Head's written acceptance with a business reason.

## 5. Late-Delivery Credits
Late-delivery credits owed under a contract must be claimed within 30 days of delivery. Waiving a credit of USD 1,000 or more requires the Procurement Head's approval. The Plant Head may waive a credit under USD 1,000, recording the reason (for example, to protect a strategic relationship).

## 6. High-Risk Vendors
For an overdue order from a vendor with a risk score of 75 or more, the plant sends an expedite request asking for a confirmed ship date within two business days. If no confirmed date is received, the Plant Head may cancel the order and re-source from an eligible vendor, informing the Procurement Head.

## 7. Quotes
Expired quotes may not be used. For planned orders, request an updated quote at least five days before the current quote expires.
""")
(POLICIES / "POL-GLOBAL-PO-2026.md").write_text("""---
doc_id: POL-GLOBAL-PO-2026
doc_type: company_policy
plant_id: GLOBAL
title: Purchase Order Raising Policy
---
# Purchase Order Raising Policy (2026)

> Fictional document created for a course project.

## 1. Before Buying
Check stock at other Northfield plants first. A transfer from a plant with surplus is preferred when it arrives sooner than a vendor delivery.

## 2. Vendor Comparison
Compare at least three eligible vendors where three exist. Eligible means: a contract in force through delivery, a risk score below 75 and a valid (not expired) price.

## 3. Raising the Request
The Plant Head raises the purchase request with the content required by the plant policy (section 7) and sends it to the approver set by the approval levels.

## 4. After Approval
Issue the purchase order to the vendor within one business day and track delivery until receipt.

## 5. After Rejection
A rejected request may not be resubmitted unchanged. The Plant Head may: (a) revise the request using the approver's feedback and resubmit it, (b) choose the next-ranked eligible vendor and resubmit, or (c) withdraw the request and record the reason. If the part will run out before any alternative can arrive, escalate to the Procurement Head the same day.

## 6. Split Deliveries
If warehouse space is short, order the quantity that fits and schedule the remainder as a second delivery.
""")

# ------------------------------------------------------------------ TASK QUEUES for the two app users (10 each)
tasks = [
    # Austin, Dana Whitfield
    dict(id="AUS-01", plant_id="AUS", priority="red", type="restock", title="Restock P-110 Hydraulic Pump HP-40",
         why="6 on hand vs safety stock 8; runs out Nov 5. Critical part.", params={"part_id": "P-110"}),
    dict(id="AUS-02", plant_id="AUS", priority="red", type="overdue_high_risk", title="Overdue PO-24140 from Ironwood Hydraulics (risk 81)",
         why="6 pumps were due Oct 18 (8 days late) from a high-risk vendor; we are short on this part.", params={"po_id": "PO-24140"}),
    dict(id="AUS-03", plant_id="AUS", priority="red", type="restock", title="Restock P-210 Steel Sheet 2mm",
         why="50 on hand vs safety stock 80; about 6 days of cover. Warehouse is nearly full.", params={"part_id": "P-210"}),
    dict(id="AUS-04", plant_id="AUS", priority="orange", type="claim", title="Claim late-delivery credit on PO-24137 (Juniper Electric)",
         why="30 VFD drives arrived 26 days late; the contract has a late-delivery credit.", params={"po_id": "PO-24137"}),
    dict(id="AUS-05", plant_id="AUS", priority="orange", type="overcharge", title="Overcharge on PO-24139 (Cobalt Bearings)",
         why="$7.45 per bearing vs contract $6.80 on 4,000 units.", params={"po_id": "PO-24139"}),
    dict(id="AUS-06", plant_id="AUS", priority="orange", type="renewal", title="Renewal decision: Granite Controls contract",
         why="Expires Dec 15; non-renewal notice due Nov 15.", params={"vendor_id": "V07"}),
    dict(id="AUS-07", plant_id="AUS", priority="low", type="quote_refresh", title="Request a fresh quote: Evergreen, P-110",
         why="Evergreen's P-110 quote expired Oct 20; they are our fastest pump vendor.", params={"vendor_id": "V05", "part_id": "P-110"}),
    dict(id="AUS-08", plant_id="AUS", priority="low", type="claim", title="Claim late-delivery credit on PO-24138 (Brightline Motion)",
         why="12 servo motors arrived 11 days late.", params={"po_id": "PO-24138"}),
    dict(id="AUS-09", plant_id="AUS", priority="low", type="overcharge", title="Overcharge on PO-24141 (Granite Controls)",
         why="$3,400 per PLC vs contract rate on 2 units.", params={"po_id": "PO-24141"}),
    dict(id="AUS-10", plant_id="AUS", priority="low", type="market_watch", title="Market watch: hydraulics and steel",
         why="Weekly check of supply news for our two red categories.", params={"topics": ["hydraulic pump supply", "steel sheet prices"]}),
    # Dallas, Marcus Bell
    dict(id="DAL-01", plant_id="DAL", priority="red", type="restock", title="Restock P-140 Gearbox GX-20",
         why="1 on hand vs safety stock 3; about 7 days of cover. Critical part.", params={"part_id": "P-140"}),
    dict(id="DAL-02", plant_id="DAL", priority="red", type="expired_contract", title="Open PO-24145 with Delta Drive: contract expired",
         why="Delta's contract expired Sep 30, but 4 servo motors are still on order (due Nov 5).", params={"po_id": "PO-24145", "vendor_id": "V04"}),
    dict(id="DAL-03", plant_id="DAL", priority="orange", type="overcharge", title="Overcharge on PO-24142 (Harbor Steel Works)",
         why="$108.50 per sheet vs contract rate on 300 sheets.", params={"po_id": "PO-24142"}),
    dict(id="DAL-04", plant_id="DAL", priority="orange", type="renewal", title="Renewal decision: Harbor Steel Works contract",
         why="Expires Jan 9; non-renewal notice due Nov 10.", params={"vendor_id": "V08"}),
    dict(id="DAL-05", plant_id="DAL", priority="orange", type="claim", title="Claim late-delivery credit on PO-24146 (Northstar Automation)",
         why="3,000 bearings arrived 18 days late.", params={"po_id": "PO-24146"}),
    dict(id="DAL-06", plant_id="DAL", priority="low", type="quote_refresh", title="Request a fresh quote: Apex Fluid Systems, P-300",
         why="Apex's P-300 quote expires Oct 31; we reorder filters monthly.", params={"vendor_id": "V01", "part_id": "P-300"}),
    dict(id="DAL-07", plant_id="DAL", priority="low", type="ship_confirm", title="Confirm ship date: PO-24142 (Harbor Steel)",
         why="300 steel sheets due Oct 31; no ship confirmation yet.", params={"po_id": "PO-24142"}),
    dict(id="DAL-08", plant_id="DAL", priority="low", type="market_watch", title="Market watch: steel and gearboxes",
         why="Weekly check of supply news.", params={"topics": ["steel sheet prices", "industrial gearbox supply"]}),
    dict(id="DAL-09", plant_id="DAL", priority="low", type="vendor_review", title="Vendor review: Lumen Sensors (risk 63)",
         why="Medium-risk vendor with no auto-renewal; check before the next sensor order.", params={"vendor_id": "V12"}),
    dict(id="DAL-10", plant_id="DAL", priority="low", type="space_review", title="Warehouse space check",
         why="Make sure the steel delivery on Oct 31 fits.", params={}),
]
for i, t in enumerate(tasks):
    t.update(status="open", order=i, created=TODAY.isoformat(), source="seed")
(DATA / "tasks_seed.json").write_text(json.dumps(tasks, indent=1))

print(f"plants {len(plants)}, parts {len(parts)}, vendors {len(vendors)}, price lines {len(lines)}, quotes {len(quotes)}, "
      f"POs {len(pos)}, contracts {len(terms)}, policies {len(matrix)}")
