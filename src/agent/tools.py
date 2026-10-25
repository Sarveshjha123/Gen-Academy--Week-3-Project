"""The tools the agents can use. Three kinds:
  * MCP tools  - from the two company systems (Plant Ops, Supplier)                 -> WORKFLOWS (code)
  * workflow tools - our own code that combines both systems (restock, Top 5, PO)   -> WORKFLOWS (code)
  * knowledge tools - Pinecone search, Tavily news, Mem0 memory
The one WRITE action (sending a purchase request) lives in src/agent/drafter.py and always pauses for a human.
"""
import json

from langchain_core.tools import StructuredTool, tool

from src import config
from src.agent import knowledge
from src.core import workflows
from src.core.data import NotFound


def _j(obj):
    return json.dumps(obj, indent=1, default=str)


def build_tools(system_tools: dict, call, plant_id: str, user_id: str) -> dict:
    """Returns {name: tool}. `plant_id`/`user_id` = who is using the app (their priorities and memory)."""

    @tool
    async def recommend_restock(part_id: str, plant_id: str = plant_id, qty: int = 0, cost_weight: float = -1,
                                speed_weight: float = -1, risk_weight: float = -1) -> str:
        """RESTOCKING HELP (workflow). For one part at one plant: stock and run-out date, nearby plants with surplus to
        transfer, and the TOP 3 VENDOR OPTIONS (price, qty, total incl. freight, arrival date, simulated risk score,
        contract) ranked in code, plus blocked vendors, the recommendation and who must approve.
        qty: if the user asks for a specific quantity ("order 80"), PASS IT HERE; every option is then priced for that
        quantity and the calculated need is shown for comparison. Leave 0 to let the code calculate the need.
        Weights: pass cost/speed/risk (0-1) only if the user states different priorities."""
        pr = knowledge.load_priorities(plant_id)
        for k, v in (("cost", cost_weight), ("speed", speed_weight), ("risk", risk_weight)):
            if v >= 0:
                pr[k] = v
        try:
            return _j(await workflows.recommend_restock(call, part_id, plant_id, pr, requested_qty=int(qty or 0)))
        except NotFound as e:
            return _j({"error": str(e)})

    @tool
    async def morning_briefing(plant_id: str = plant_id) -> str:
        """TOP 5 ACTIVITIES for a plant this morning (workflow): restocks, late-delivery penalty claims, overdue orders,
        overcharges vs contract, contract renewals, warehouse space. Ranked in code using the user's priorities."""
        try:
            return _j(await workflows.morning_briefing(call, plant_id, knowledge.load_priorities(plant_id)))
        except NotFound as e:
            return _j({"error": str(e)})

    @tool
    async def prepare_purchase_order(plant_id: str, part_id: str, vendor_id: str, qty: int, reason: str = "") -> str:
        """Creates a DRAFT purchase request with all numbers calculated in code (unit price, freight, total, delivery
        date, vendors compared) and the approver required by the plant policy. Saves it to workspace/drafts/.
        `reason` = why this vendor was chosen (required by policy if it is not the lowest cost)."""
        try:
            pr = await workflows.prepare_purchase_order(call, plant_id, part_id, vendor_id, qty, reason)
        except NotFound as e:
            return _j({"error": str(e)})
        return _j({k: pr[k] for k in ("draft_id", "plant_id", "part_id", "part_name", "qty", "vendor_name", "unit_price_usd",
                                      "freight_usd", "total_usd", "expected_delivery", "need", "alternatives_compared",
                                      "approval", "saved_to")})

    @tool
    def calculate_delay_penalty(po_value_usd: float, days_late: int, rate_pct: float, per: str, grace_days: int,
                                cap_pct: float) -> str:
        """Late-delivery penalty in dollars, from the numbers in the contract's late-delivery clause.
        per: 'week', 'day', or 'week_or_part' (if the clause counts part of a week as a full week)."""
        return _j(workflows.calculate_delay_penalty(po_value_usd, days_late, rate_pct, per, grace_days, cap_pct))

    @tool
    def submit_for_approval(draft_id: str, email_to: str, email_subject: str, email_body: str) -> str:
        """WRITE ACTION: send the purchase request email + PO form to the approver for signature.
        The app PAUSES before this runs so the Plant Head can approve, edit or reject it."""
        draft = config.WORKSPACE / "drafts" / f"{draft_id}.json"
        if not draft.exists():
            return _j({"error": f"No draft {draft_id}. Call prepare_purchase_order first."})
        pr = json.loads(draft.read_text())
        missing = workflows.check_email(pr, email_body)   # HARNESS CHECK (code): real numbers, not invented ones
        if missing:
            return _j({"error": "Email not sent: it must state " + ", ".join(missing) + ". Fix the email and try again."})
        return _j(workflows.submit_for_approval_files(draft_id, email_to, email_subject, email_body))

    @tool
    def recall_memory(query: str = "") -> str:
        """Long-term memory (Mem0) about this Plant Head: priorities, preferred or blacklisted vendors, past decisions."""
        return _j({"user": user_id, "memories": knowledge.recall(user_id, query),
                   "scoring_priorities": knowledge.load_priorities(plant_id)})

    @tool
    def save_memory(fact: str) -> str:
        """Save a lasting preference or decision the Plant Head states (e.g. 'Never use Ironwood for pumps')."""
        return _j(knowledge.remember(user_id, fact))

    knowledge_tools = {
        "search_contracts": StructuredTool.from_function(knowledge.search_contracts),
        "search_policy": StructuredTool.from_function(knowledge.search_policy),
        "market_news": StructuredTool.from_function(knowledge.market_news),
    }
    own = {t.name: t for t in [recommend_restock, morning_briefing, prepare_purchase_order, calculate_delay_penalty,
                               submit_for_approval, recall_memory, save_memory]}
    return {**system_tools, **knowledge_tools, **own}
