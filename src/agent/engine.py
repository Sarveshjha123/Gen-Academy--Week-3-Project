"""TASK ENGINE: runs one task's playbook, pauses for the human, and updates the queue.

  playbook.prepare()  -> code + SUB-AGENTS gather the facts (in parallel where possible) and build the plan
  email drafter + CRITIC -> version 1 saved in workspace/drafts/
  ASK HUMAN (approval card): approve | edit | request changes (new version) | reject
     approve -> "send" to workspace/outbox/, task done, follow-up tasks created (maybe in the other plant's queue)
     reject  -> POLICY-ADVISOR sub-agent reads the policies and recommends the next step
                ASK HUMAN (choice card): e.g. next vendor / escalate / waiver request / snooze / close
                -> the chosen next step may draft another email (same approve/reject loop)

`ask_human(payload)` is LangGraph's interrupt() in the app, and a scripted function in the evals.
`Steps` saves every AI/tool result of a run, so when LangGraph re-runs the node after a pause nothing is redone.
"""
import json
import time
from datetime import datetime

from src import config
from src.core import tasks as T

MAX_REVISIONS = 5


class Steps:
    """Per-run cache of step results + a trace of which sub-agents ran (shown in the app and used by the evals)."""

    def __init__(self, run_id: str):
        d = config.WORKSPACE / "runs"
        d.mkdir(parents=True, exist_ok=True)
        self.f = d / f"{run_id}.json"
        self.data = json.loads(self.f.read_text()) if self.f.exists() else {"steps": {}, "trace": []}

    def _save(self):
        self.f.write_text(json.dumps(self.data, indent=1, default=str))

    async def run(self, name, factory, agent=None):
        if name in self.data["steps"]:
            return self.data["steps"][name]
        t0 = time.time()
        value = await factory()
        self.data["steps"][name] = value
        self.data["trace"].append({"step": name, "agent": agent or "code", "seconds": round(time.time() - t0, 1)})
        self._save()
        return value

    def put(self, name, value):
        self.data["steps"][name] = value
        self._save()

    @property
    def trace(self):
        return self.data["trace"]


# ---------------------------------------------------------------- files
def save_draft(task_id, key, n, email_plan, version):
    d = config.WORKSPACE / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    critic = version.get("critic") or {}
    meta = f"Version {n} | written by: {version.get('written_by')}"
    if version.get("feedback"):
        meta += f" | your change request: {version['feedback']}"
    if critic.get("first_draft_issues"):
        meta += " | critic: " + "; ".join(critic["first_draft_issues"]) + (" (fixed)" if critic.get("fixed") else "")
    (d / f"{task_id}_{key}_v{n}.md").write_text(f"From: {email_plan['from']}\nTo: {email_plan['to']}\nSubject: {version['email_subject']}\n"
                                               f"{meta}\n\n{version['email_body']}\n")


def send(task_id, key, email_plan, final: dict):
    out = config.WORKSPACE / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{task_id}_{key}_email.txt"
    attach = f"\n---\nAttached: {email_plan['form_file']}\n" if email_plan.get("form_file") else ""
    f.write_text(f"From: {email_plan['from']}\nTo: {final['to']}\nSubject: {final['subject']}\nSent: {datetime.now():%Y-%m-%d %H:%M}\n\n"
                 f"{final['body']}\n{attach}")
    if email_plan.get("form_file") and (config.WORKSPACE / "drafts" / email_plan["form_file"]).exists():
        (out / email_plan["form_file"]).write_text((config.WORKSPACE / "drafts" / email_plan["form_file"]).read_text())
    return f"workspace/outbox/{f.name}"


# ---------------------------------------------------------------- one email: draft -> critic -> approval loop
async def email_round(task, plan, email_plan, helpers, ask_human, steps, key):
    """Returns ('approve', final_email) or ('reject', reason)."""
    from src.agent.drafter import draft_generic, revise_generic
    v1 = await steps.run(f"{key}_v1", lambda: draft_generic(helpers, email_plan), agent="email-drafter + critic")
    versions = steps.data["steps"].setdefault(f"{key}_versions", [v1])
    if len(versions) == 1:
        save_draft(task["id"], key, 1, email_plan, v1)
    i = 0
    while True:
        v = versions[i]
        answer = ask_human({
            "type": "approval", "task_id": task["id"], "title": task["title"], "key": key,
            "summary_md": plan.get("summary_md", ""), "form_file": email_plan.get("form_file"),
            "recipient_role": email_plan.get("recipient_role", ""), "amount": email_plan.get("amount", ""),
            "email": {"from": email_plan["from"], "to": email_plan["to"], "subject": v["email_subject"], "body": v["email_body"]},
            "version": i + 1, "critic": v.get("critic"), "feedback": v.get("feedback"),
            "trace": steps.trace, "purpose": email_plan["purpose"]})
        decision = (answer or {}).get("decision", "approve")
        if decision == "revise" and i + 1 < MAX_REVISIONS:
            if i + 1 >= len(versions):
                new = await revise_generic(helpers, email_plan, v, answer.get("message", ""))
                new["feedback"] = answer.get("message", "")
                versions.append(new)
                steps.put(f"{key}_versions", versions)
                steps.data["trace"].append({"step": f"{key}_v{i + 2}", "agent": "email-drafter (your change request)", "seconds": 0})
                save_draft(task["id"], key, i + 2, email_plan, new)
            i += 1
            continue
        if decision in ("approve", "edit"):
            e = answer.get("email") or {}
            final = {"to": e.get("to") or email_plan["to"], "subject": e.get("subject") or v["email_subject"],
                     "body": e.get("body") or v["email_body"], "edited_by_you": decision == "edit", "version": i + 1}
            return "approve", final
        return "reject", (answer or {}).get("message", "") or "No reason given."


# ---------------------------------------------------------------- the whole task
async def run_task(task: dict, ctx: dict, ask_human, steps: Steps) -> dict:
    from src.agent import playbooks as P
    pb = P.PLAYBOOKS[task["type"]]
    if task["type"] == "restock":
        return await run_restock(task, ctx, ask_human, steps, pb)
    plan = await steps.run("plan", lambda: pb["prepare"](task, ctx, steps))
    if plan.get("error"):
        T.update(task["id"], "snoozed", f"Could not prepare: {plan['error']}")
        return {"status": "snoozed", "outcome": plan["error"], "trace": steps.trace}

    if not plan.get("email"):                       # information task: show what the helpers found, then done/snooze
        answer = ask_human({"type": "choice", "task_id": task["id"], "title": task["title"], "summary_md": plan["summary_md"],
                            "question": "Anything else needed?", "trace": steps.trace,
                            "options": [{"id": "done", "label": "Mark done", "recommended": True},
                                        {"id": "snooze", "label": "Snooze to tomorrow"}]})
        return finish_simple(task, ctx, (answer or {}).get("choice", "done"), plan.get("done_outcome", "Reviewed."), steps)

    verdict, payload = await email_round(task, plan, plan["email"], ctx["helpers"], ask_human, steps, "main")
    if verdict == "approve":
        sent = send(task["id"], "main", plan["email"], payload)
        outcome = await pb["on_approve"](task, plan, ctx, payload)
        T.update(task["id"], "done", outcome["outcome"], sent=sent, followups=outcome.get("followups", []), pr=outcome.get("pr_record"))
        log(ctx, task, f"approved and sent ({plan['email']['purpose']})")
        return {"status": "done", "sent": sent, **outcome, "trace": steps.trace}

    return await handle_reject(task, plan, ctx, ask_human, steps, pb, payload)


async def handle_reject(task, plan, ctx, ask_human, steps, pb, reason):
    """Rejected: ask the policy-advisor what policy says to do next, then let the human choose."""
    options = pb["reject_options"](task, plan, ctx)
    advice = await steps.run("policy_advice", lambda: ctx["helpers"].policy_advisor(task, plan, reason, options),
                             agent="policy-advisor")
    rec = advice.get("recommended_option")
    for o in options:
        o["recommended"] = o["id"] == rec
    answer = ask_human({"type": "choice", "task_id": task["id"], "title": task["title"],
                        "summary_md": f"**You rejected the draft.** Reason: {reason}\n\n**Policy advisor:** {advice.get('advice', '')}"
                                      + (f"\n\nSources: {', '.join(advice.get('policy_refs', []))}" if advice.get("policy_refs") else ""),
                        "question": "What should happen next?", "options": options, "trace": steps.trace})
    choice = (answer or {}).get("choice", "snooze")
    opt = next((o for o in options if o["id"] == choice), {"id": "snooze"})
    plan["choice_value"] = (answer or {}).get("value")          # e.g. the new quantity typed on the choice card
    if opt.get("next_email"):                      # the chosen next step sends something else (same approve/reject loop)
        plan2 = await steps.run(f"plan_{choice}", lambda: pb["next_email"](task, plan, ctx, choice, reason), agent="code")
        if plan2.get("error"):
            T.update(task["id"], "snoozed", f"Next step failed: {plan2['error']}")
            return {"status": "snoozed", "outcome": plan2["error"], "trace": steps.trace}
        verdict2, payload2 = await email_round(task, plan2, plan2["email"], ctx["helpers"], ask_human, steps, choice)
        if verdict2 == "approve":
            sent = send(task["id"], choice, plan2["email"], payload2)
            outcome = await pb["on_next_approved"](task, plan, ctx, choice, plan2)
            T.update(task["id"], "done", outcome["outcome"], sent=sent, followups=outcome.get("followups", []),
                     pr=outcome.get("pr_record"))
            log(ctx, task, f"first draft rejected ({reason}); next step '{opt['label']}' sent")
            return {"status": "done", "sent": sent, **outcome, "trace": steps.trace}
        T.update(task["id"], "snoozed", f"Both drafts rejected ({reason}; {payload2}). Back on tomorrow's list.")
        log(ctx, task, f"rejected twice: {reason}; {payload2}")
        return {"status": "snoozed", "outcome": "Rejected twice; snoozed to tomorrow.", "trace": steps.trace}
    return finish_simple(task, ctx, choice, f"Closed after rejection: {reason}", steps, reason)


# ---------------------------------------------------------------- the guided RESTOCK task
async def run_restock(task, ctx, ask_human, steps, pb):
    """1. sub-agents + code gather the facts   2. ASK: how many do you need?
    3. another plant has surplus? ASK: request a transfer (how many)? -> transfer email to that Plant Head -> approval
    4. the rest from a vendor: ASK which vendor (ranked options, contract minimum order enforced)
    5. purchase request email to the approver(s) -> approval (reject -> policy-advisor -> next step).
    Every side effect (send, new tasks) runs inside steps.run, so LangGraph's replay after each pause never repeats it."""
    from src.agent import playbooks as P
    from src.core import workflows as W
    from src.core.data import money
    plan = await steps.run("plan", lambda: pb["prepare"](task, ctx, steps))
    if plan.get("error"):
        T.update(task["id"], "snoozed", f"Could not prepare: {plan['error']}")
        return {"status": "snoozed", "outcome": plan["error"], "trace": steps.trace}
    rec, call, plant_id = plan["rec"], ctx["call"], ctx["plant_id"]
    part, stock = rec["part"], rec["stock"]
    need = rec["vendor_options"][0]["calculated_need_qty"]

    # ---- 2. quantity
    a = ask_human({"type": "choice", "task_id": task["id"], "title": task["title"], "trace": steps.trace,
                   "summary_md": plan["summary_md"] + f"\n\n**Calculated need:** {need} (covers the vendor lead time plus 6 weeks, "
                                                      "and refills safety stock). Change it if you need a different amount.",
                   "question": f"How many {part['part_id']} {part['part_name']} do you need in total?",
                   "options": [{"id": "set_qty", "label": "Continue with this quantity", "input": "qty", "default": need, "recommended": True},
                               {"id": "snooze", "label": "Snooze to tomorrow"}]})
    if (a or {}).get("choice") == "snooze":
        return finish_simple(task, ctx, "snooze", "", steps)
    qty = max(1, int((a or {}).get("value") or need))
    notes = [f"You asked for {qty} in total."]

    # ---- 3. inter-plant transfer first (PO Raising policy section 1)
    nearby = await steps.run("nearby", lambda: call("find_nearby_surplus", {"part_id": part["part_id"], "plant_id": plant_id}),
                             agent="code via MCP")
    transferred, followups, sent = 0, [], []
    src = (nearby.get("plants_with_surplus") or [None])[0]
    if src:
        default_t = min(src["surplus_qty"], qty)
        a = ask_human({"type": "choice", "task_id": task["id"], "title": task["title"], "trace": steps.trace,
                       "summary_md": f"**{src['plant_name']} has {src['surplus_qty']} spare {part['part_id']}** (above their safety stock and "
                                     f"6 weeks of their own use). By truck: {src['transfer_days']} days, about {money(src['truck_cost_usd'])}, "
                                     f"arrives {src['arrives']}. Policy checks other plants before buying, and a transfer needs both Plant "
                                     f"Heads to agree (plant policy section 6).",
                       "question": f"Ask {src['plant_head']} ({src['plant_name']} Plant Head) for a transfer?",
                       "options": [{"id": "transfer", "label": f"Yes: draft a transfer request to {src['plant_head']}", "input": "qty",
                                    "default": default_t, "recommended": True},
                                   {"id": "no_transfer", "label": f"No: order all {qty} from a vendor"}]})
        if (a or {}).get("choice") == "transfer":
            tq = max(1, min(int((a or {}).get("value") or default_t), src["surplus_qty"], qty))
            ep_t = await steps.run("transfer_plan", lambda: _value(P.transfer_ask_email(ctx, src, part, tq, stock)))
            t_plan = {"summary_md": f"**Transfer request:** {tq} x {part['part_id']} from {src['plant_name']} ({src['plant_head']})."}
            verdict, final = await email_round(task, t_plan, ep_t, ctx["helpers"], ask_human, steps, "transfer")
            if verdict == "approve":
                done = await steps.run("transfer_sent", lambda: _value({"sent": send(task["id"], "transfer", ep_t, final),
                                                                       "followups": P.create_transfer_request(task, ctx, part, tq, src)}))
                sent.append(done["sent"])
                followups += done["followups"]
                transferred = tq
                notes.append(f"Transfer of {tq} requested from {src['plant_head']} ({src['plant_name']}).")
            else:
                notes.append(f"Transfer request not sent ({final}); ordering everything from a vendor.")
        else:
            notes.append("You chose not to ask for a transfer.")
    remaining = qty - transferred
    if remaining <= 0:
        out = " ".join(notes) + " Fully covered by the transfer; no purchase needed."
        T.update(task["id"], "done", out, sent=sent, followups=followups)
        log(ctx, task, out)
        return {"status": "done", "outcome": out, "sent": ", ".join(sent), "followups": followups, "trace": steps.trace}

    # ---- 4. choose the vendor for the rest (priced for that quantity; contract minimum order enforced)
    rec2 = await steps.run(f"options_{remaining}", lambda: W.recommend_restock(call, part["part_id"], plant_id, requested_qty=remaining),
                           agent="restock workflow (code via MCP)")
    if not rec2["vendor_options"]:
        T.update(task["id"], "snoozed", "No eligible vendor: escalate to Procurement.")
        return {"status": "snoozed", "outcome": "No eligible vendor.", "trace": steps.trace}
    opts = [{"id": o["vendor_id"], "recommended": o["rank"] == 1,
             "label": f"{o['vendor_name']}: {o['qty']} for {money(o['total_usd'])}, arrives {o['arrival_date']}"
                      + ("" if o["arrives_before_run_out"] else " (after run-out)") + f", risk {o['risk_score']}"
                      + (f" (contract minimum {o['moq']})" if o["qty"] > remaining else "")} for o in rec2["vendor_options"]]
    opts.append({"id": "snooze", "label": "Snooze to tomorrow"})
    a = ask_human({"type": "choice", "task_id": task["id"], "title": task["title"], "trace": steps.trace,
                   "summary_md": " ".join(notes) + f"\n\n{P.vendor_options_md(rec2, remaining)}",
                   "question": f"Which vendor should supply the remaining {remaining}?", "options": opts})
    vid = (a or {}).get("choice")
    if vid == "snooze" or vid not in {o["vendor_id"] for o in rec2["vendor_options"]}:
        return finish_simple(task, ctx, "snooze", "", steps)
    chosen = next(o for o in rec2["vendor_options"] if o["vendor_id"] == vid)
    why = "ranked #1 on cost, delivery speed and risk" if chosen["rank"] == 1 else f"chosen by the Plant Head (ranked #{chosen['rank']})"
    pr = await steps.run(f"pr_{vid}_{remaining}", lambda: W.prepare_purchase_order(call, plant_id, part["part_id"], vid, remaining, why),
                         agent="code: PO form + approver ladder")
    pr.pop("form_markdown", None)
    if pr.get("moq_note"):
        notes.append("Minimum order: " + pr["moq_note"])
    extra = (f"Transfer of {transferred} from another plant requested; this order covers the remaining {remaining}. " if transferred else "")
    ep = P.restock_email(ctx, pr, extra + (pr.get("moq_note") or ""))
    plan_pr = {**plan, "pr": pr, "rec": {**rec2, "transfer_option": None}, "email": ep,
               "summary_md": " ".join(notes) + f"\n\n**Purchase request** {pr['draft_id']}: {pr['qty']} x {pr['part_id']} from "
                                               f"{pr['vendor_name']}, {money(pr['total_usd'])}. Approver: {pr['approval']['approver_role']}."}

    # ---- 5. purchase request -> approval
    verdict, payload = await email_round(task, plan_pr, ep, ctx["helpers"], ask_human, steps, "main")
    if verdict == "approve":
        sent.append(send(task["id"], "main", ep, payload))
        outcome = await pb["on_approve"](task, plan_pr, ctx, payload)
        out = " ".join(notes[1:] + [outcome["outcome"]])
        T.update(task["id"], "done", out, sent=sent, followups=followups + outcome.get("followups", []), pr=outcome.get("pr_record"))
        log(ctx, task, out)
        return {"status": "done", "sent": ", ".join(sent), "outcome": out, "followups": followups + outcome.get("followups", []),
                "trace": steps.trace}
    return await handle_reject(task, plan_pr, ctx, ask_human, steps, pb, payload)


async def _value(v):
    return v


def finish_simple(task, ctx, choice, outcome, steps, reason=""):
    if choice == "snooze":
        T.update(task["id"], "snoozed", "Snoozed to tomorrow." + (f" Reason: {reason}" if reason else ""))
        return {"status": "snoozed", "outcome": "Snoozed to tomorrow.", "trace": steps.trace}
    status = "done" if choice == "done" else "closed"
    T.update(task["id"], status, outcome)
    if reason:
        log(ctx, task, f"closed without sending: {reason}")
    return {"status": status, "outcome": outcome, "trace": steps.trace}


def log(ctx, task, what):
    """Decisions go to long-term memory (Mem0), so the agent remembers them in later conversations."""
    try:
        from src.agent import knowledge
        knowledge.remember(ctx["user_id"], f"{config.TODAY}: task '{task['title']}': {what}.")
    except Exception:
        pass
