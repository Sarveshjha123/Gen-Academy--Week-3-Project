"""Runs a task playbook inside a small LangGraph graph so it can PAUSE for the human (interrupt) and RESUME later
(checkpointer + thread_id), exactly like the Session 1 demo. The playbook decides the path; the AI only does the
judgement work (sub-agents), so the flow is reliable.
"""
import uuid
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.agent.engine import Steps, run_task
from src.core import tasks as T


class RunState(TypedDict, total=False):
    task_id: str
    run_id: str
    result: dict


def build_runner(ctx):
    async def run_node(state: RunState):
        task = T.get(state["task_id"])
        result = await run_task(task, ctx, interrupt, Steps(state["run_id"]))
        return {"result": result}

    g = StateGraph(RunState)
    g.add_node("run_task", run_node)
    g.add_edge(START, "run_task")
    g.add_edge("run_task", END)
    return g.compile(checkpointer=InMemorySaver())


def _cfg(thread):
    return {"configurable": {"thread_id": thread}, "tags": ["northfield", "task"], "metadata": {"app": "morning-procurement-desk"}}


def _parse(out):
    intr = out.get("__interrupt__") if isinstance(out, dict) else None
    if intr:
        return {"pending": intr[0].value if hasattr(intr[0], "value") else intr[0]}
    return {"result": out.get("result", {})}


async def start(runner, task_id: str):
    thread = uuid.uuid4().hex[:10]
    out = await runner.ainvoke({"task_id": task_id, "run_id": f"{task_id}-{thread}"}, _cfg(thread))
    return thread, _parse(out)


async def resume(runner, thread: str, answer: dict):
    out = await runner.ainvoke(Command(resume=answer), _cfg(thread))
    return _parse(out)
