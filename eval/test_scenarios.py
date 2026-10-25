"""EVAL LAYER 1b: every task scenario, approve AND reject paths, with STAND-IN sub-agents (no AI, free, seconds).
    python -m eval.test_scenarios
Checks the orchestration: the right email goes to the right person with the right numbers, files land in drafts/ and
outbox/, the queue moves (task leaves the top 5, the next one comes up), follow-up tasks are created (including in the
other plant's queue), and rejections lead to the policy-based next step. The real AI version is eval/eval_scenarios.py.
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from src import config
from src.core import tasks as T
from src.core import workflows as W

RESULTS = []
TERMS = json.loads((config.EVAL_DIR / "truth/contract_terms.json").read_text())


def check(name, ok, detail=""):
    RESULTS.append({"test": name, "passed": bool(ok), "detail": str(detail)[:300]})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   -> {detail}"))


class FakeHelpers:
    """Stand-ins that behave like good sub-agents: the contract-analyst returns the TRUE clause from the contract file."""
    calls = []

    async def llm(self, system, user):
        brief = json.loads(user.split("BRIEF:\n", 1)[1].split("\n\nCURRENT EMAIL", 1)[0].split("\n\nYour last", 1)[0])
        body = f"Hi {' and '.join(brief['greet'])},\n\n{brief['purpose']}\n\n" + "\n".join(f"- {m}" for m in brief["must_include"])
        if "REQUESTED CHANGES" in user:
            body += "\n\nChange applied: " + user.split("REQUESTED CHANGES: ", 1)[1].split("\n", 1)[0]
        self.calls.append("email-drafter")
        return json.dumps({"email_subject": brief["subject_hint"], "email_body": body + "\n\nThanks"})

    async def judge(self, msg):
        self.calls.append("critic")
        return json.dumps({"ok": True, "issues": []})

    async def contract_analyst(self, question):
        self.calls.append("contract-analyst")
        vid = question.split()[1]
        t = TERMS[vid]
        txt = next(config.CONTRACTS_DIR.glob(f"{vid}_*.md")).read_text()
        clause = next(l for l in txt.splitlines() if l.startswith("5.2"))[len("5.2 Late delivery. "):]
        quote = clause.split(" Liquidated damages are")[0].split(" Buyer's remedy")[0]
        dp = ({**t["delay_penalty"], "quote": quote} if t["delay_penalty"] else {"none": True, "quote": quote})
        from src.core.checks import verify_contract_quotes
        data = {"vendor_id": vid, "contract_id": t["contract_id"], "answer": f"Expires {t['expiry_date']}.",
                "terms": {"delay_penalty": dp, "expiry_date": {"value": t["expiry_date"]}}}
        return {"data": data, "quote_check": verify_contract_quotes(json.dumps(data))}

    async def policy_advisor(self, task, plan, reason, options):
        self.calls.append("policy-advisor")
        return {"advice": "Per POL-GLOBAL-PO-2026 section 5 ...", "recommended_option": options[0]["id"], "policy_refs": ["POL-GLOBAL-PO-2026 section 5"]}

    async def market_researcher(self, topics):
        self.calls.append("market-researcher")
        return "- No relevant news found."


def scripted(*answers):
    """A fake human: returns the scripted answers in order and records what was shown."""
    shown, it = [], iter(answers)

    def ask(payload):
        shown.append(payload)
        return next(it)
    return ask, shown


def ctx_for(plant):
    pl = {"AUS": ("Austin", "Dana Whitfield", "dana.whitfield@northfield.example"),
          "DAL": ("Dallas", "Marcus Bell", "marcus.bell@northfield.example")}[plant]
    return {"call": W.direct_call, "helpers": FakeHelpers(), "plant_id": plant, "plant_name": pl[0], "user_name": pl[1],
            "user_email": pl[2], "user_id": f"test-{plant}"}


async def run(task_id, *answers):
    from src.agent.engine import Steps, run_task
    task = T.get(task_id)
    ask, shown = scripted(*answers)
    FakeHelpers.calls = []
    res = await run_task(task, ctx_for(task["plant_id"]), ask, Steps(f"test-{task_id}-{len(RESULTS)}"))
    return res, shown, list(FakeHelpers.calls)


APPROVE = {"decision": "approve"}


async def main():
    old = config.WORKSPACE
    config.WORKSPACE = Path(tempfile.mkdtemp())
    try:
        T.reset()
        top = [t["id"] for t in T.top5("AUS")]
        check("Queue: Austin top 5 = 3 red then 2 orange", top == ["AUS-01", "AUS-02", "AUS-03", "AUS-04", "AUS-05"], top)
        check("Queue: 10 tasks each for Dana and Marcus", len(T.load("AUS")) == 10 and len(T.load("DAL")) == 10)
        pr_ = {t["priority"] for t in T.load("DAL")}
        check("Queue: red, orange and low tasks present", pr_ == {"red", "orange", "low"}, pr_)

        # ---- R1 guided restock: quantity -> transfer from Dallas -> vendor choice -> purchase request
        res, shown, calls = await run("AUS-01", {"choice": "set_qty", "value": 40}, {"choice": "transfer", "value": 8}, APPROVE,
                                      {"choice": "V05"}, APPROVE)
        check("Restock asks the quantity first (number box, calculated need 32 as default)", shown[0]["type"] == "choice"
              and shown[0]["options"][0].get("input") == "qty" and shown[0]["options"][0]["default"] == 32, shown[0].get("options"))
        check("Restock then asks about a transfer from Dallas, naming Marcus Bell", shown[1]["type"] == "choice"
              and "Marcus Bell" in shown[1]["question"], shown[1].get("question"))
        check("Transfer email: to Marcus Bell for 8 x P-110", shown[2]["type"] == "approval" and "marcus.bell" in shown[2]["email"]["to"]
              and "8" in shown[2]["email"]["body"], shown[2].get("email", {}).get("to"))
        check("Vendor choice lists the ranked options for the remaining 32 (Evergreen recommended)", shown[3]["type"] == "choice"
              and "32" in shown[3]["question"] and next(o for o in shown[3]["options"] if o.get("recommended"))["id"] == "V05",
              shown[3].get("options"))
        e = shown[4]["email"]
        check("Purchase request: 32 from Evergreen, $60,400, to Rachel Stein (Procurement Head)", "rachel.stein" in e["to"]
              and "$60,400" in e["body"] and "32" in e["body"], e["to"])
        check("Restock: from Dana Whitfield", e["from"].startswith("Dana Whitfield"))
        check("Restock: sub-agents used: contract-analyst + market-researcher + drafter + critic",
              {"contract-analyst", "market-researcher", "email-drafter", "critic"} <= set(calls), calls)
        check("Restock: drafts saved for both emails, both in the outbox",
              (config.WORKSPACE / "drafts" / "AUS-01_transfer_v1.md").exists() and (config.WORKSPACE / "drafts" / "AUS-01_main_v1.md").exists()
              and (config.WORKSPACE / "outbox" / "AUS-01_transfer_email.txt").exists() and (config.WORKSPACE / "outbox" / "AUS-01_main_email.txt").exists())
        check("Restock P-110: task done and gone from top 5", T.get("AUS-01")["status"] == "done" and "AUS-01" not in [t["id"] for t in T.top5("AUS")])
        nxt = [t["id"] for t in T.top5("AUS")]
        check("Queue moves: AUS-06 (orange) now in the top 5", "AUS-06" in nxt, nxt)
        tr = [t for t in T.load("DAL") if t["type"] == "transfer_request"]
        check("Approved transfer email creates ONE red transfer request in Marcus's queue (8 x P-110)",
              len(tr) == 1 and tr[0]["priority"] == "red" and tr[0]["params"]["qty"] == 8, tr)
        check("Transfer request is #1 in Marcus's top 5", T.top5("DAL")[0]["type"] == "transfer_request")

        # ---- transfer request APPROVED by Marcus -> confirmation note in Dana's queue
        res, shown, _ = await run(tr[0]["id"], APPROVE)
        check("Transfer approve: email to Dana, confirms 8 x P-110", "dana.whitfield" in shown[0]["email"]["to"] and "P-110" in shown[0]["email"]["body"])
        check("Transfer approve: Dana gets 'Transfer confirmed' task", any("Transfer confirmed" in t["title"] for t in T.load("AUS")))

        # ---- request changes -> version 2, then approve
        res, shown, calls = await run("AUS-03", {"choice": "set_qty", "value": 325}, {"choice": "V13"},
                                      {"decision": "revise", "message": "Mention the split delivery"}, APPROVE)
        check("No plant has spare P-210: no transfer question, straight to vendor choice", shown[1]["type"] == "choice"
              and "vendor" in shown[1]["question"].lower(), shown[1].get("question"))
        check("Request changes: next card shows version 2 with the change", shown[3]["version"] == 2
              and "split delivery" in shown[3]["email"]["body"], [x.get("version") for x in shown])
        check("Request changes: v1 and v2 both saved in drafts/", (config.WORKSPACE / "drafts" / "AUS-03_main_v2.md").exists())
        check("Restock P-210: 325 ordered from Meridian", "325" in shown[2]["email"]["body"], shown[2]["email"]["body"][:200])

        # ---- claim APPROVED: exact amount from the contract clause, to the vendor
        res, shown, calls = await run("AUS-04", APPROVE)
        check("Claim PO-24137 approve: $1,764 to Juniper Electric, no rates asked from the user",
              "$1,764" in shown[0]["email"]["body"] and "juniper" in shown[0]["email"]["to"].lower(), shown[0]["email"]["to"])
        check("Claim approve: follow-up 'check credit note' created", any("credit note" in t["title"] for t in T.load("AUS")))

        # ---- claim REJECTED (>= $1,000) -> policy: waiver needs the Procurement Head -> email to Rachel
        T.reset("DAL")
        res, shown, calls = await run("DAL-05", {"decision": "reject", "message": "Northstar is strategic"},
                                      {"choice": "waiver_request"}, APPROVE)
        check("Claim reject ($1,065 >= $1,000): policy-advisor ran, options offered", "policy-advisor" in calls and shown[1]["type"] == "choice")
        check("Claim reject: waiver request goes to the Procurement Head", "rachel.stein" in shown[2]["email"]["to"] and "$1,065" in shown[2]["email"]["body"])
        check("Claim reject: task done, waiver follow-up created", T.get("DAL-05")["status"] == "done"
              and any("Await Procurement Head decision" in t["title"] for t in T.load("DAL")))

        # ---- small claim rejected (< $1,000) -> can waive directly, task closed
        res, shown, _ = await run("AUS-08", {"decision": "reject", "message": "goodwill"}, {"choice": "waive"})
        check("Small claim ($556) reject: 'waive' offered and task closed", any(o["id"] == "waive" for o in shown[1]["options"])
              and T.get("AUS-08")["status"] == "closed", shown[1]["options"])

        # ---- overcharge REJECTED -> written acceptance request to the Procurement Head
        res, shown, _ = await run("AUS-05", {"decision": "reject", "message": "urgent volume, accept for now"}, {"choice": "accept_request"}, APPROVE)
        check("Overcharge reject: acceptance request to Procurement Head with $2,600", "rachel.stein" in shown[2]["email"]["to"]
              and "$2,600" in shown[2]["email"]["body"])

        # ---- renewal APPROVED -> a 'send notice to vendor' task appears
        res, shown, calls = await run("AUS-06", APPROVE)
        check("Renewal: recommendation email to Procurement Head", "rachel.stein" in shown[0]["email"]["to"])
        check("Renewal approve: new orange task to send the notice to Granite", any(t["type"] == "vendor_notice" for t in T.load("AUS")))

        # ---- renewal REJECTED -> non-renewal notice to the vendor
        res, shown, _ = await run("DAL-04", {"decision": "reject", "message": "not convinced"}, {"choice": "non_renewal"}, APPROVE)
        check("Renewal reject: non-renewal notice to Harbor Steel", "harborsteel" in shown[2]["email"]["to"].replace(" ", "").lower(), shown[2]["email"]["to"])

        # ---- high-risk overdue APPROVED -> 'cancel if no ship date' task; then REJECT path cancels and re-sources
        res, shown, _ = await run("AUS-02", APPROVE)
        check("Overdue Ironwood: expedite email to vendor with deadline", "PO-24140" in shown[0]["email"]["body"] and "ironwood" in shown[0]["email"]["to"].lower())
        c = [t for t in T.load("AUS") if t["type"] == "cancel_po"]
        check("Overdue approve: follow-up 'cancel if no ship date' created", bool(c))

        # ---- expired contract REJECTED -> cancel Delta PO -> re-source task (red) for P-120
        res, shown, _ = await run("DAL-02", {"decision": "reject", "message": "no exceptions this quarter"}, {"choice": "cancel_resource"}, APPROVE)
        check("Expired contract reject: cancellation email to Delta", "delta" in shown[2]["email"]["to"].lower() and "PO-24145" in shown[2]["email"]["body"])
        check("Expired contract: re-source task for P-120 created (red)", any(t["type"] == "restock" and t["params"].get("part_id") == "P-120"
                                                                              and t["priority"] == "red" for t in T.load("DAL")))

        # ---- restock REJECTED -> option 2 vendor
        res, shown, _ = await run("DAL-01", {"choice": "set_qty"}, {"choice": "V10"}, {"decision": "reject", "message": "use another vendor"},
                                  {"choice": "alt_vendor"}, APPROVE)
        check("Restock reject: policy-advisor card, then a draft for a DIFFERENT vendor",
              shown[3]["type"] == "choice" and "Juniper" not in shown[4]["email"]["to"] and shown[2]["email"]["body"] != shown[4]["email"]["body"],
              [x.get("email", {}).get("to") for x in shown])

        # ---- restock: no transfer, reject, change the quantity to 80 -> needs 2 approvers
        T.reset("AUS")
        res, shown, _ = await run("AUS-01", {"choice": "set_qty", "value": 32}, {"choice": "no_transfer"}, {"choice": "V05"},
                                  {"decision": "reject", "message": "I want 80 units"}, {"choice": "change_qty", "value": 80}, APPROVE)
        e2 = shown[5]["email"] if len(shown) > 5 else {"to": "", "body": ""}
        check("Declining the transfer: whole 32 goes to the vendor choice", "32" in shown[2]["question"], shown[2].get("question"))
        check("Change quantity: offered with a number box after a reject", any(o["id"] == "change_qty" and o.get("input") == "qty"
                                                                             for o in shown[4]["options"]))
        check("Change quantity to 80: new draft for 80 at $150,640 to Rachel Stein AND Thomas Grey",
              "80" in e2["body"] and "$150,640" in e2["body"] and "rachel.stein" in e2["to"] and "thomas.grey" in e2["to"], e2["to"])

        # ---- Marcus DECLINES the transfer -> Dana gets a red task to amend her purchase request (32 + 8 = 40)
        T.reset("AUS")
        T.reset("DAL")
        await run("AUS-01", {"choice": "set_qty", "value": 40}, {"choice": "transfer", "value": 8}, APPROVE, {"choice": "V05"}, APPROVE)
        old_pr = T.get("AUS-01").get("pr") or {}
        check("Dana's restock task remembers the purchase request it sent (32 units)", old_pr.get("qty") == 32, old_pr)
        tr = next(t for t in T.load("DAL") if t["type"] == "transfer_request")
        res, shown, _ = await run(tr["id"], {"decision": "reject", "message": "we need our pumps for a rush order"}, {"choice": "decline"}, APPROVE)
        check("Marcus declines: email to Dana with the reason", "dana.whitfield" in shown[2]["email"]["to"], shown[2]["email"]["to"])
        am = [t for t in T.load("AUS") if t["type"] == "pr_amend"]
        check("Decline creates a RED 'amend purchase request' task at the top of Dana's list",
              len(am) == 1 and am[0]["priority"] == "red" and T.top5("AUS")[0]["id"] == am[0]["id"], [t["title"] for t in T.top5("AUS")])
        res, shown, _ = await run(am[0]["id"], APPROVE)
        e = shown[0]["email"]
        check("Amended request to Rachel: replaces the old one, 32 + 8 = 40, new total $75,440",
              "rachel.stein" in e["to"] and old_pr["draft_id"] in e["body"] and "$75,440" in e["body"]
              and all(x in e["body"] for x in ("32", "8", "40")), e["body"][:400])
        check("Amend task done, new request remembered", T.get(am[0]["id"])["status"] == "done" and T.get(am[0]["id"])["pr"]["qty"] == 40)

        # ---- decline pushes the amended total over $100,000 -> Finance Head added
        T.reset("AUS")
        T.reset("DAL")
        await run("AUS-01", {"choice": "set_qty", "value": 58}, {"choice": "transfer", "value": 8}, APPROVE, {"choice": "V05"}, APPROVE)
        tr = next(t for t in T.load("DAL") if t["type"] == "transfer_request")
        await run(tr["id"], {"decision": "reject", "message": "no"}, {"choice": "decline"}, APPROVE)
        am = next(t for t in T.load("AUS") if t["type"] == "pr_amend")
        res, shown, _ = await run(am["id"], APPROVE)
        check("Amended 50 + 8 = 58 ($109,280): approval moves up to Rachel Stein AND Thomas Grey, card warns",
              "thomas.grey" in shown[0]["email"]["to"] and "approval level changes" in shown[0]["summary_md"], shown[0]["email"]["to"])

        # ---- contract minimum order: 9 needed, 8 transferred, 1 left -> Evergreen's minimum is 2
        T.reset("AUS")
        T.reset("DAL")
        res, shown, _ = await run("AUS-01", {"choice": "set_qty", "value": 9}, {"choice": "transfer", "value": 8}, APPROVE,
                                  {"choice": "V05"}, APPROVE)
        check("Contract minimum: 1 left, Evergreen minimum is 2 -> option and request say so and order 2",
              "contract minimum 2" in next(o for o in shown[3]["options"] if o["id"] == "V05")["label"]
              and "minimum order is 2" in shown[4]["summary_md"], shown[4].get("summary_md", "")[:300])

        # ---- transfer covers everything -> no purchase request
        T.reset("AUS")
        T.reset("DAL")
        res, shown, _ = await run("AUS-01", {"choice": "set_qty", "value": 8}, {"choice": "transfer", "value": 8}, APPROVE)
        check("Need 8, transfer 8: done without a purchase request", T.get("AUS-01")["status"] == "done" and len(shown) == 3
              and "no purchase needed" in res["outcome"], res.get("outcome"))

        # ---- contract-analyst gets the numbers WRONG -> code catches it and uses clause 5.2 itself (claim still correct)
        T.reset("AUS")
        orig = FakeHelpers.contract_analyst

        async def wrong(self, question):
            out = await orig(self, question)
            out["data"]["terms"]["delay_penalty"] = {**out["data"]["terms"]["delay_penalty"], "rate_pct": 5}
            return out
        FakeHelpers.contract_analyst = wrong
        try:
            res, shown, _ = await run("AUS-04", APPROVE)
        finally:
            FakeHelpers.contract_analyst = orig
        check("Analyst misreads the rate (5% instead of 2%): code catches it, claim is still $1,764",
              "$1,764" in shown[0]["email"]["body"] and "does not match clause 5.2" in shown[0]["summary_md"], shown[0]["summary_md"][:300])

        # ---- info task + snooze
        res, shown, calls = await run("AUS-10", {"choice": "done"})
        check("Market watch: market-researcher used, no email, task done", "market-researcher" in calls and shown[0]["type"] == "choice"
              and T.get("AUS-10")["status"] == "done")
        res, shown, _ = await run("DAL-06", {"decision": "reject", "message": "later"}, {"choice": "snooze"})
        check("Snooze: task leaves today's list", T.get("DAL-06")["status"] == "snoozed")
    finally:
        shutil.rmtree(config.WORKSPACE, ignore_errors=True)
        config.WORKSPACE = old
    passed = sum(r["passed"] for r in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} scenario checks passed")
    out = config.EVAL_DIR / "results"
    out.mkdir(exist_ok=True)
    (out / "scenario_tests.json").write_text(json.dumps({"passed": passed, "total": len(RESULTS), "tests": RESULTS}, indent=1))
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
