"""THE CHAT SUPERVISOR (Deep Agents) for questions: "what are my options for P-110?", "what does Juniper's contract say?",
"what does policy say about waiving a claim?". It plans, calls code tools (via MCP) and delegates to sub-agents with `task`.
Sending anything happens through the task cards (fixed playbooks with a human approval step), not from chat.

          Plant Head (chat)
              |
   Procurement Lead (supervisor, gpt-oss-120b, to-do list, recursion limit)
      |              |                 |                 |
 code tools     contract-analyst   policy-advisor    market-researcher
 (2 MCP servers) (Pinecone)        (Pinecone)        (Tavily)
"""
import json
import time
import uuid

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from src import config
from src.agent import prompts
from src.agent.llm import supervisor_model, text_of
from src.core import tasks as T

MAIN_TOOLS = ["recommend_restock", "find_parts", "find_nearby_surplus", "get_stock_position", "get_plant_inventory",
              "check_warehouse_space", "get_vendor_risk", "get_vendors_for_part", "get_latest_quotes", "get_contract_status",
              "list_open_orders", "list_late_deliveries", "get_purchase_order", "find_price_overcharges",
              "list_contracts_needing_attention", "get_approval_route", "calculate_delay_penalty", "recall_memory", "save_memory"]

CAN_HELP = ("restocking options and vendor comparison, stock and run-out dates, warehouse space, transfers from nearby plants, "
            "vendors, contract prices, quotes and contract terms, vendor risk, approvals and procurement policy, purchase orders "
            "and late deliveries, late-delivery credits, overcharges, today's tasks and supply-market news")


def build_supervisor(ctx):
    from deepagents import CompiledSubAgent, create_deep_agent
    from deepagents.backends import FilesystemBackend

    helpers, tools = ctx["helpers"], ctx["tools"]

    @tool
    def list_my_tasks() -> str:
        """The Plant Head's task queue today: top 5 open tasks (red first), plus what is already done."""
        return json.dumps({"top_5": [{k: t[k] for k in ("id", "priority", "title", "why")} for t in T.top5(ctx["plant_id"])],
                           "done_or_closed": [{k: t.get(k) for k in ("id", "title", "status", "outcome")} for t in T.finished(ctx["plant_id"])][:10]})

    subagents = [
        CompiledSubAgent(name="contract-analyst", runnable=helpers.contract_analyst_runnable(),
                         description="Reads a vendor's Master Supply Agreement (Pinecone) and returns terms with exact quotes: expiry, renewal, "
                                     "late-delivery credits, damage terms, fill rate, price escalation, payment terms, contract rates."),
        CompiledSubAgent(name="policy-advisor", runnable=helpers._advisor,
                         description="Answers what company or plant policy says (approval levels, rejections, waivers, expired contracts, "
                                     "renewals, prices above contract), with policy and section. Give it the plant_id."),
        CompiledSubAgent(name="market-researcher", runnable=helpers._market,
                         description="Searches recent web news (Tavily) on a commodity, part category or region; returns signals with links."),
    ]
    return create_deep_agent(
        model=supervisor_model(),
        tools=[tools[n] for n in MAIN_TOOLS] + [list_my_tasks],
        system_prompt=prompts.SUPERVISOR.format(person=ctx["user_name"], plant_name=ctx["plant_name"], plant_id=ctx["plant_id"],
                                                today=config.TODAY.isoformat()),
        subagents=subagents,
        backend=FilesystemBackend(root_dir=str(config.WORKSPACE), virtual_mode=True),
        checkpointer=InMemorySaver(),
        name="procurement-lead",
    )


class Recorder(BaseCallbackHandler):
    """Records every tool call (incl. which sub-agent was asked) and token use: the trace for the UI and evals."""

    def __init__(self):
        self.tools, self.tokens, self.llm_calls, self.outputs = [], {"input": 0, "output": 0}, 0, []

    def on_tool_start(self, serialized, input_str, **kw):
        name = (serialized or {}).get("name") or kw.get("name") or "?"
        inputs = kw.get("inputs") or {}
        if name == "task":
            name = f"task:{inputs.get('subagent_type', '?')}"
        self.tools.append(name)

    def on_tool_end(self, output, **kw):
        self.outputs.append(str(getattr(output, "content", output))[:30000])

    def on_llm_end(self, response, **kw):
        self.llm_calls += 1
        for gens in response.generations:
            for g in gens:
                u = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                self.tokens["input"] += u.get("input_tokens", 0)
                self.tokens["output"] += u.get("output_tokens", 0)


async def scope_check(question: str) -> dict:
    """Front-desk filter (a small model call): in / partial / out of scope. Fails open (treated as in scope)."""
    from src.agent.llm import helper_model
    from src.core.checks import read_json
    try:
        out = read_json(text_of(await helper_model().ainvoke(prompts.SCOPE.format(question=question[:2000]))), {}) or {}
        return {"scope": out.get("scope", "in") if out.get("scope") in ("in", "partial", "out") else "in", "reason": out.get("reason", "")}
    except Exception as e:  # never block the user because the filter failed
        return {"scope": "in", "reason": f"filter error: {type(e).__name__}"}


async def _invoke(agent, cfg, text, rec):
    result = await agent.ainvoke({"messages": [{"role": "user", "content": text}]}, cfg)
    answer = next((text_of(m) for m in reversed(result.get("messages", [])) if isinstance(m, AIMessage) and text_of(m).strip()
                   and not getattr(m, "tool_calls", None)), "")
    return result, answer


async def ask(agent, thread_id: str, text: str, run_id=None) -> dict:
    """One chat turn with three guardrails:
    1. SCOPE: out-of-scope questions get a clear "I can't help with that" (no tools, no cost).
    2. FAITHFULNESS: code checks every number, date and ID in the answer against the tool results.
    3. REPAIR: if something is unsupported, the agent is asked once to correct it; anything still unsupported is flagged."""
    from src.core.checks import normalize_text, verify_answer
    rec = Recorder()
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": config.MAX_AGENT_STEPS, "callbacks": [rec],
           "metadata": {"app": "morning-procurement-desk"}, "tags": ["northfield", "chat"]}
    if run_id:
        cfg["run_id"] = run_id
    t0, error, result, answer = time.time(), None, {}, ""
    scope = await scope_check(text)
    base = {"tools_called": rec.tools, "tool_outputs": rec.outputs, "tokens": rec.tokens, "interrupt": None, "scope": scope}
    if scope["scope"] == "out":
        answer = (f"I can't help with that: it's outside this procurement desk ({scope['reason'] or 'not a procurement question'}). "
                  f"I can help with {CAN_HELP}.")
        return {**base, "answer": answer, "llm_calls": 1, "latency_s": round(time.time() - t0, 1), "todos": [], "error": None,
                "verification": {"checked": 0, "unsupported": [], "repaired": False}}
    if scope["scope"] == "partial":
        text = text + "\n\n(Note from the scope filter: part of this question may be outside procurement. Answer the procurement part and say plainly which part you can't help with.)"
    try:
        result, answer = await _invoke(agent, cfg, text, rec)
    except Exception as e:  # recursion limit / provider errors: report, don't crash
        error = f"{type(e).__name__}: {e}"
    check = verify_answer(answer, rec.outputs, text) if answer and not error else {"checked": 0, "unsupported": []}
    repaired = False
    if check["unsupported"] and not error:
        fix = ("CODE CHECK: these values in your last answer are not in any tool result: " + ", ".join(check["unsupported"])
               + ". Re-check with the tools. Correct them, remove them, or show the calculation from tool numbers. "
                 "Reply with the full corrected answer only.")
        try:
            result2, answer2 = await _invoke(agent, cfg, fix, rec)
            if answer2.strip():
                check2 = verify_answer(answer2, rec.outputs, text)
                if len(check2["unsupported"]) <= len(check["unsupported"]):
                    result, answer, check, repaired = result2, answer2, check2, True
        except Exception as e:
            error = f"repair failed: {type(e).__name__}: {e}"
            error = None if answer else error
    answer = normalize_text(answer)        # plain hyphens/spaces/quotes: easier to read, copy and check
    return {**base, "answer": answer if answer or not error else f"Sorry, the run stopped: {error}",
            "llm_calls": rec.llm_calls + 1, "latency_s": round(time.time() - t0, 1), "todos": result.get("todos", []),
            "error": error, "verification": {**check, "repaired": repaired}}


def new_thread() -> str:
    return uuid.uuid4().hex[:12]
