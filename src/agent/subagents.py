"""THE SUB-AGENTS (LangChain create_agent, Nebius models). Used by the task playbooks AND by the chat supervisor.

  contract-analyst   reads vendor contracts (Pinecone) -> terms + exact quotes (code then verifies the quotes)
  policy-advisor     reads company + plant policies (Pinecone) -> what should happen next, with citations
  market-researcher  web news (Tavily) -> market signals with links
  email-drafter      writes emails from a brief (src/agent/drafter.py)
  critic             reviews each draft against a checklist (a different model call; JUDGE_MODEL if set)
"""
import json
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda

from src import config
from src.agent import prompts
from src.agent.llm import helper_model, judge_model, text_of
from src.core.checks import read_json, verify_contract_quotes


def _last_ai(result):
    return next((text_of(m) for m in reversed(result["messages"]) if isinstance(m, AIMessage) and text_of(m).strip()), "")


def _save_research(name, payload):
    d = config.WORKSPACE / "research"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}_{datetime.now():%H%M%S}.json").write_text(json.dumps(payload, indent=1, default=str))


class Helpers:
    def __init__(self, tools: dict):
        from langchain.agents import create_agent
        self.tools = tools
        self._analyst = create_agent(helper_model(), [tools["search_contracts"], tools["get_contract_status"]],
                                     system_prompt=prompts.CONTRACT_ANALYST, name="contract-analyst")
        self._advisor = create_agent(helper_model(), [tools["search_policy"]], system_prompt=prompts.POLICY_ADVISOR, name="policy-advisor")
        self._market = create_agent(helper_model(), [tools["market_news"]], system_prompt=prompts.MARKET_RESEARCHER,
                                    name="market-researcher")

    # ---- plain model calls (drafter / critic)
    async def llm(self, system, user) -> str:
        return text_of(await helper_model().ainvoke([SystemMessage(content=system), HumanMessage(content=user)]))

    async def judge(self, msg) -> str:
        return text_of(await judge_model().ainvoke(msg))

    # ---- sub-agents
    async def contract_analyst(self, question) -> dict:
        res = await self._analyst.ainvoke({"messages": [HumanMessage(content=question)]}, {"recursion_limit": 25})
        text = _last_ai(res)
        data = read_json(text, {}) or {}
        out = {"data": data, "quote_check": verify_contract_quotes(text), "question": question}
        _save_research(f"contract_{data.get('vendor_id', 'x')}", out)
        return out

    async def policy_advisor(self, task, plan, reason, options) -> dict:
        q = json.dumps({"plant_id": task["plant_id"], "task": task["title"], "proposed": plan.get("email", {}).get("purpose"),
                        "rejected_because": reason, "allowed_options": {o["id"]: o["label"] for o in options}})
        res = await self._advisor.ainvoke({"messages": [HumanMessage(content=q)]}, {"recursion_limit": 20})
        out = read_json(_last_ai(res), {}) or {}
        if out.get("recommended_option") not in {o["id"] for o in options}:
            out["recommended_option"] = options[0]["id"]
        _save_research(f"policy_{task['id']}", out)
        return out

    async def market_researcher(self, topics) -> str:
        res = await self._market.ainvoke({"messages": [HumanMessage(content="Topics: " + "; ".join(topics))]}, {"recursion_limit": 15})
        return _last_ai(res)

    # the contract-analyst also serves the chat supervisor as a Deep Agents sub-agent (with the same code quote check)
    def contract_analyst_runnable(self):
        async def run(state, config=None):
            result = await self._analyst.ainvoke({"messages": state["messages"]}, config)
            answer = _last_ai(result)
            qc = verify_contract_quotes(answer)
            note = f"\n\n[quote check: {qc['found']}/{qc['checked']} quotes found word-for-word in the contract]"
            return {"messages": result["messages"] + [AIMessage(content=answer + note)]}
        return RunnableLambda(lambda s: None, afunc=run, name="contract-analyst")


async def build_context(plant_id: str):
    """Everything a task run needs: MCP tools, the workflow `call`, the sub-agents, and who the user is."""
    from src.agent.systems import load_system_tools, make_call
    from src.agent.tools import build_tools
    from src.core.data import plant, table
    pl = plant(plant_id)
    system_tools = await load_system_tools()
    call = make_call(system_tools)
    user_id = f"plant-head-{pl['plant_id'].lower()}"
    tools = build_tools(system_tools, call, pl["plant_id"], user_id)
    people = table("people")
    email = people[(people.role == "Plant Head") & (people.plant_id == pl["plant_id"])].iloc[0]["email"]
    return {"call": call, "helpers": Helpers(tools), "tools": tools, "plant_id": pl["plant_id"], "plant_name": pl["plant_name"],
            "user_name": pl["plant_head"], "user_email": email, "user_id": user_id}
