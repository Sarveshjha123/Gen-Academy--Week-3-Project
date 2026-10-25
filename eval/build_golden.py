"""Builds the golden test sets FROM THE DATA (so expected answers are never typed by hand).
    python -m eval.build_golden
Writes eval/golden_agent.csv and eval/golden_retrieval.csv.
"""
import asyncio
import csv
import json

from src import config
from src.core import plant_ops, supplier, workflows as W
from src.core.data import money


def main():
    terms = json.loads((config.EVAL_DIR / "truth/contract_terms.json").read_text())
    r = asyncio.run(W.recommend_restock(W.direct_call, "P-110", "AUS"))
    top = r["vendor_options"][0]
    r210 = asyncio.run(W.recommend_restock(W.direct_call, "P-210", "AUS"))
    cap = r210["vendor_options"][0]["qty"]
    sp = plant_ops.warehouse_space("AUS")
    v10 = terms["V10"]["delay_penalty"]
    pen = W.calculate_delay_penalty(29400, 26, v10["rate_pct"], v10["per"], v10["grace_days"], v10["cap_pct"])
    g = supplier.contract_status("V07")
    v03 = terms["V03"]
    oc = supplier.price_overcharges("AUS")
    route = plant_ops.approval_route("AUS", top["total_usd"], top["risk_score"])

    dal = W.direct_call
    r140 = asyncio.run(W.recommend_restock(dal, "P-140", "DAL"))
    route40 = plant_ops.approval_route("DAL", 40000, 30)

    r120 = asyncio.run(W.recommend_restock(W.direct_call, "P-120", "AUS", requested_qty=10))
    fastest = min(r120["vendor_options"], key=lambda o: o["lead_time_days"])
    near110 = plant_ops.nearby_stock("P-110", "AUS")["plants_with_surplus"][0]
    p130 = plant_ops.stock_position("AUS", "P-130")
    sp500 = plant_ops.warehouse_space("AUS", "P-210", 500)
    v110 = supplier.vendors_for_part("P-110")["vendors"]
    attn = supplier.contracts_needing_attention("AUS")["contracts"]
    po137 = plant_ops.purchase_order("PO-24137")
    late = [x["po_id"] for x in plant_ops.late_deliveries("AUS")["late_purchase_orders"]][:2]
    soon_dal = [p["part_id"] for p in plant_ops.plant_inventory("DAL")["parts"] if p["days_of_cover"] <= 14]
    CANT = "can't help|cannot help|outside"

    rows = [
        # id, category, plant, question, must_call (groups ';', alternatives '|'), must_not_call, facts (';' groups, '|' alternatives), scope
        # --- restock and vendor choice (the core use case)
        ("R1", "restock", "AUS", "Austin is low on hydraulic pumps. What are my options?",
         "recommend_restock", "", f"Evergreen;Ironwood;Marcus Bell;{money(top['total_usd'])};{top['qty']}", "in"),
        ("R2", "restock+space", "AUS", "Should I order steel sheet P-210 for Austin? We are tight on space.",
         "recommend_restock|check_warehouse_space", "", f"{cap};split", "in"),
        ("R3", "restock", "DAL", "Dallas is low on P-140. What are my options?",
         "recommend_restock", "", f"{r140['vendor_options'][0]['vendor_name'].split()[0]};{r140['stock']['run_out_date']}", "in"),
        ("R4", "restock+own qty", "AUS", "Help me order 80 quantity P-110 Hydraulic Pump HP-40",
         "recommend_restock", "", "80;$150,640|150,640;32;split;Thomas Grey|Finance Head", "in"),
        ("R5", "restock+speed", "AUS", "I need 10 servo motors P-120. Which vendor can deliver fastest?",
         "recommend_restock|get_vendors_for_part", "", f"{fastest['vendor_name'].split()[0]};{fastest['lead_time_days']}", "in"),
        ("R6", "restock+risk", "AUS", "Can I just buy the pumps from Ironwood? They are the cheapest.",
         "recommend_restock|get_vendor_risk", "", "81;exception|blocked|not allowed", "in"),
        ("R7", "restock+why", "AUS", "Why do you recommend Evergreen over Apex for P-110?",
         "recommend_restock", "", "Apex;run-out|run out|after", "in"),
        # --- transfers between plants
        ("T1", "transfer", "AUS", "Does any other plant have spare P-110 we could get?",
         "find_nearby_surplus|recommend_restock", "", f"Dallas;{near110['plant_head']};{near110['surplus_qty']}", "in"),
        ("T2", "transfer", "AUS", "Can another plant send us steel sheet P-210?",
         "find_nearby_surplus|recommend_restock", "", "no plant|none|not|no surplus", "in"),
        # --- inventory and space
        ("I1", "inventory", "AUS", "Which parts are below safety stock in Austin?", "get_plant_inventory|get_stock_position", "", "P-110;P-210", "in"),
        ("I2", "inventory", "DAL", "Which parts will run out in the next 14 days?", "get_plant_inventory", "", ";".join(soon_dal), "in"),
        ("I3", "inventory", "AUS", "How many P-130 bearings do we have and when will they run out?",
         "get_stock_position|get_plant_inventory", "", f"{p130['on_hand']};{p130['run_out_date']}|Nov 5|November 5", "in"),
        ("S1", "space", "AUS", "How much warehouse space do we have free in Austin right now?", "check_warehouse_space", "",
         f"{sp['fill_pct']};{sp['free_units']}", "in"),
        ("S2", "space", "AUS", "Will 500 steel sheets P-210 fit in our warehouse?", "check_warehouse_space|recommend_restock", "",
         f"{sp500['max_items_that_fit']};no|not|won't|does not", "in"),
        # --- vendors, prices, quotes, contracts
        ("V1", "vendors", "AUS", "Which vendors can supply P-110 and what are their contract prices?",
         "get_vendors_for_part|recommend_restock", "", ";".join(f"{money(v['contract_rate_usd'])}|{v['contract_rate_usd']:,.0f}" for v in v110), "in"),
        ("V2", "quotes", "AUS", "Is Evergreen's quote for P-110 still valid?", "get_latest_quotes|recommend_restock", "",
         "expired;2026-10-20|Oct 20|October 20", "in"),
        ("C1", "contract", "AUS", "What are the late-delivery penalty terms in Juniper Electric's contract?",
         "task:contract-analyst", "", f"{v10['rate_pct']:g}%;{v10['grace_days']};{v10['cap_pct']:g}%", "in"),
        ("C2", "contract", "AUS", "When does the Granite Controls contract expire, and by when must I decide on renewal?",
         "task:contract-analyst|list_contracts_needing_attention|get_contract_status", "",
         f"{g['expiry_date']}|December 15;{g['renewal_decision_by']}|November 15", "in"),
        ("C3", "contract", "AUS", "What are the damage and defect terms with Cobalt Bearings?",
         "task:contract-analyst", "", f"{v03['damage_claim_window_days']};{v03['damage_replacement_days']}", "in"),
        ("C4", "contract", "AUS", "What are our payment terms with Evergreen Industrial Supply?", "task:contract-analyst", "",
         f"{terms['V05']['payment_terms']}|{terms['V05']['payment_terms'].split()[-1]} days", "in"),
        ("C5", "contract", "AUS", "Which of our contracts need a decision in the next 60 days?", "list_contracts_needing_attention", "",
         ";".join(c["vendor_name"].split()[0] for c in attn), "in"),
        # --- vendor risk
        ("K1", "risk", "AUS", "What is the risk score of Ironwood Hydraulics, and can I use them?", "get_vendor_risk|recommend_restock", "",
         "81;exception", "in"),
        ("K2", "risk", "DAL", "What is Lumen Sensors' risk score and what does it mean for approvals?", "get_vendor_risk", "",
         "63;one level|up one|higher", "in"),
        # --- purchase orders, deliveries, claims, overcharges
        ("O1", "overcharge", "AUS", "Are we being overcharged on any open purchase orders?", "find_price_overcharges", "",
         f"PO-24139;{money(oc['total_overcharge_usd'])}", "in"),
        ("O2", "open orders", "AUS", "When will my open order for P-110 arrive?", "list_open_orders|get_stock_position", "",
         "PO-24140;overdue|late|past", "in"),
        ("O3", "po status", "AUS", "What's the status of PO-24137?", "get_purchase_order", "",
         f"{po137['received_date']}|October 12|Oct 12;26", "in"),
        ("O4", "late", "AUS", "Which deliveries were late in the last 90 days?", "list_late_deliveries", "", ";".join(late), "in"),
        ("P1", "penalty", "AUS", "PO-24137 from Juniper Electric arrived 26 days late (order value $29,400). How much can we claim under the contract?",
         "task:contract-analyst;calculate_delay_penalty", "", f"{money(pen['penalty_usd'])}|{pen['penalty_usd']:,.2f}", "in"),
        # --- approvals and policy
        ("A1", "approval", "AUS", f"Who needs to approve a {money(top['total_usd'])} order of P-110 pumps from Evergreen for Austin?",
         "get_approval_route", "", f"Procurement Head;{route['signers'][0]['name']}", "in"),
        ("A2", "approval", "AUS", "Who has to approve a $150,000 purchase order for Austin?", "get_approval_route", "", "Rachel Stein;Thomas Grey", "in"),
        ("A3", "approval", "DAL", "Who approves a $40,000 purchase in Dallas?", "get_approval_route|task:policy-advisor", "",
         f"{route40['signers'][0]['name']}", "in"),
        ("G1", "policy", "AUS", "What does company policy say we should do if a purchase request is rejected?", "task:policy-advisor", "",
         "vendor;withdraw|cancel;escalat", "in"),
        ("G2", "policy", "DAL", "Can we keep ordering from a vendor whose contract has expired?", "task:policy-advisor", "", "90", "in"),
        ("G3", "policy", "AUS", "Who can waive a $1,500 late-delivery credit?", "task:policy-advisor", "", "Procurement Head", "in"),
        ("G4", "policy", "DAL", "What fill rate does policy require to renew a contract instead of renegotiating?", "task:policy-advisor", "", "95", "in"),
        ("G5", "policy", "AUS", "If pumps run out in 5 days, can we do an emergency purchase? Who approves it?",
         "task:policy-advisor|get_approval_route", "", "50,000|50000;48", "in"),
        ("G6", "policy", "AUS", "Do I need to compare three vendors before buying?", "task:policy-advisor", "", "three|3", "in"),
        # --- tasks, market, memory
        ("B1", "tasks", "AUS", "Good morning. What are my top 5 things to work on today?", "list_my_tasks", "", "P-110;PO-24140;P-210", "in"),
        ("M1", "market", "AUS", "Is there any recent market news that could affect hydraulic pump or steel supply?", "task:market-researcher", "", "", "in"),
        ("X2", "memory", "AUS", "Please remember: I never want to use Lumen Sensors for anything.", "save_memory", "", "", "in"),
        # --- not in the system: must say so, never guess
        ("X1", "unknown", "AUS", "Restock part P-999 at Austin.", "", "", "P-999;not|unknown|no part", "in"),
        ("X3", "unknown", "AUS", "What is the status of PO-99999?", "get_purchase_order", "", "PO-99999;not|unknown|no purchase order", "in"),
        ("X4", "unknown", "AUS", "Get me prices for laptops for the office.", "", "", "not|no|can't|outside", "in|partial|out"),
        # --- actions the desk must not take from chat
        ("N1", "no action", "AUS", "Approve the P-110 purchase request for me.", "", "", "Rachel Stein|Procurement Head;can't|cannot|not able|only", "in|partial"),
        ("N2", "no action", "AUS", "Change Ironwood's risk score to 50 so we can use them.", "", "", "can't|cannot|not able|simulated|not possible", "in|partial|out"),
        # --- out of scope: clear "I can't help", no tools
        ("Z1", "out of scope", "AUS", "What's the weather in Austin today?", "", "", CANT, "out"),
        ("Z2", "out of scope", "AUS", "Write a performance review for my buyer Kevin.", "", "", CANT, "out"),
        ("Z3", "out of scope", "AUS", "What was our Q3 revenue?", "", "", CANT, "out"),
        ("Z4", "out of scope", "AUS", "Schedule maintenance for press line 3 next week.", "", "", CANT, "out"),
        ("Z5", "partial", "AUS", "What is Ironwood's risk score, and can you also book my flight to Chicago?", "get_vendor_risk", "",
         f"81;can't|cannot|outside|not able", "partial"),
    ]
    with open(config.EVAL_DIR / "golden_agent.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "category", "plant_id", "question", "must_call", "must_not_call", "facts", "scope"])
        w.writerows(rows)

    sections = {
        "late delivery penalty liquidated damages credit": "5. Delivery, Fulfilment and Late Delivery",
        "when does the agreement expire and does it renew": "1. Term and Renewal",
        "damaged or defective goods claim window and replacement": "6. Inspection, Damage and Defects",
        "payment terms for invoices": "8. Invoicing and Payment",
        "maximum price increase per year": "3. Price Adjustments",
        "contract unit price list for parts": "Appendix A: Contract Price Schedule",
        "minimum order quantity and lead time": "4. Orders, Minimum Quantities and Lead Times",
        "terminate the agreement notice": "10. Termination",
    }
    with open(config.EVAL_DIR / "golden_retrieval.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["namespace", "filter_key", "filter_value", "query", "expected_doc", "expected_section"])
        for vid, t in terms.items():
            for q, sec in sections.items():
                w.writerow(["contracts", "vendor_id", vid, q, t["contract_id"], sec])
            # without a filter: can search find the right vendor by name?
            w.writerow(["contracts", "", "", f"{t['vendor_name']} late delivery penalty", t["contract_id"], "5. Delivery, Fulfilment and Late Delivery"])
        pol = {"who approves a purchase of this value": "2. Approval Levels", "emergency purchase when stock runs out": "3. Emergency Purchases",
               "high risk vendor rules": "4. Vendor Risk Rules", "what must a purchase request contain": "7. What a Purchase Request Must Contain",
               "transfer stock from another plant": "6. Inter-Plant Transfers"}
        for pl in ["AUS", "DAL", "HOU", "PHX", "DEN"]:
            for q, sec in pol.items():
                w.writerow(["policies", "plant_id", pl, q, f"POL-{pl}-2026", sec])
        # company-wide policies, searched the way the app does (the plant's policy + GLOBAL)
        glob = [("contract has expired can we still order", "POL-GLOBAL-CONTRACTS-2026", "2. Contract Must Be in Force"),
                ("renew or renegotiate a contract based on fill rate", "POL-GLOBAL-CONTRACTS-2026", "3. Renewal Decisions"),
                ("vendor charged more than the contract price", "POL-GLOBAL-CONTRACTS-2026", "4. Prices Above the Contract Rate"),
                ("who can waive a late-delivery credit", "POL-GLOBAL-CONTRACTS-2026", "5. Late-Delivery Credits"),
                ("overdue order from a high-risk vendor", "POL-GLOBAL-CONTRACTS-2026", "6. High-Risk Vendors"),
                ("what to do after a purchase request is rejected", "POL-GLOBAL-PO-2026", "5. After Rejection"),
                ("how many vendors must be compared", "POL-GLOBAL-PO-2026", "2. Vendor Comparison"),
                ("check other plants for stock before buying", "POL-GLOBAL-PO-2026", "1. Before Buying")]
        for pl in ["AUS", "DAL"]:
            for q, doc, sec in glob:
                w.writerow(["policies", "plant_id", f"{pl}+GLOBAL", q, doc, sec])
    print(f"golden_agent.csv: {len(rows)} questions; golden_retrieval.csv written")


if __name__ == "__main__":
    main()
