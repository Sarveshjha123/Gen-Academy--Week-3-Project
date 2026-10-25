"""EMAIL-DRAFTER and CRITIC sub-agents (reflection pattern: draft -> review -> revise).

An "email plan" (built by a playbook in code) says who it is from/to, the purpose, the facts, and the items that MUST
appear word-for-word (amounts, PO numbers, names). The drafter writes; a CODE CHECK verifies the must-include items
(one retry, else a safe template written by code); the CRITIC (a separate model call) reviews against a checklist and the
drafter fixes what it finds. The human can then ask for more changes from the approval card.
"""
import json

from src.agent import prompts
from src.core.checks import read_json


def check(ep: dict, body: str) -> list:
    return [m for m in ep["must_include"] if m and m not in (body or "")]


def template(ep: dict) -> dict:
    """Safe fallback written by code: always correct, a little plain."""
    hello = " and ".join(ep["to_names"])
    lines = "\n".join(f"- {line}" for line in ep.get("template_lines", []))
    body = f"Hi {hello},\n\n{ep['purpose_sentence']}\n\n{lines}\n\nThanks,\n{ep['from_name']}"
    missing = check(ep, body)
    if missing:
        body += "\n\nKey details: " + "; ".join(missing)
    return {"email_subject": ep["subject_hint"], "email_body": body}


def _brief(ep):
    return {"sender": ep["from"], "recipients": ep["to"], "greet": ep["to_names"], "purpose": ep["purpose"],
            "facts": ep["facts"], "must_include": ep["must_include"], "subject_hint": ep["subject_hint"]}


async def write(helpers, ep, feedback=None, previous=None) -> dict:
    user = "BRIEF:\n" + json.dumps(_brief(ep), default=str)
    if previous:
        user += ("\n\nCURRENT EMAIL:\n" + json.dumps({k: previous[k] for k in ("email_subject", "email_body")})
                 + f"\n\nREQUESTED CHANGES: {feedback}\nRewrite the email with these changes. JSON only.")
    note = ""
    for attempt in range(2):
        out = read_json(await helpers.llm(prompts.EMAIL_WRITER, user + note), {}) or {}
        missing = check(ep, out.get("email_body", ""))
        if out.get("email_body") and not missing:
            return {"email_subject": out.get("email_subject") or ep["subject_hint"], "email_body": out["email_body"],
                    "written_by": "ai", "attempts": attempt + 1}
        note = f"\n\nYour last draft was missing these exact items: {missing or 'a body'}. Include them word-for-word."
    if previous:
        return {**{k: previous[k] for k in ("email_subject", "email_body")},
                "written_by": "previous version kept (the rewrite failed the code check)", "attempts": 2}
    return {**template(ep), "written_by": "template (AI draft failed the code check twice)", "attempts": 2}


async def critique(helpers, ep, email) -> dict:
    msg = prompts.EMAIL_CRITIC.format(checklist=ep["checklist"], facts=json.dumps(_brief(ep), default=str),
                                      email=json.dumps({k: email.get(k) for k in ("email_subject", "email_body")}))
    out = read_json(await helpers.judge(msg), {}) or {}
    return {"ok": bool(out.get("ok", True)), "issues": [str(i) for i in (out.get("issues") or [])][:5]}


async def draft_generic(helpers, ep) -> dict:
    v = await write(helpers, ep)
    review = await critique(helpers, ep, v)
    if not review["ok"] and review["issues"] and v["written_by"] == "ai":
        fixed = await write(helpers, ep, feedback="; ".join(review["issues"]), previous=v)
        if fixed["written_by"] == "ai":
            return {**fixed, "critic": {"first_draft_issues": review["issues"], "fixed": True}}
    return {**v, "critic": {"first_draft_issues": review["issues"], "fixed": False, "ok": review["ok"]}}


async def revise_generic(helpers, ep, previous, feedback) -> dict:
    return await write(helpers, ep, feedback=feedback, previous=previous)
