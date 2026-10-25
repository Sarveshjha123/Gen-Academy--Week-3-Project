"""MCP SERVER 1: Plant Ops (the ERP side: stock, warehouse space, transfers, purchase orders, approval policy).

Run it on its own:            python src/mcp_servers/plant_ops_server.py            (stdio, started by the agent)
Remote (for ChatGPT/Claude):  python src/mcp_servers/plant_ops_server.py --http   (http://127.0.0.1:8101/mcp)
Inspect it by hand:           npx @modelcontextprotocol/inspector python src/mcp_servers/plant_ops_server.py
"""
import sys

from _compat import Server, as_json, safe  # noqa: E402  (also puts the project on sys.path)

from src import config
from src.core import plant_ops

mcp = Server("northfield-plant-ops")


@mcp.tool()
@safe
def get_stock_position(plant_id: str, part_id: str) -> str:
    """Stock for ONE part at ONE plant: on hand, safety stock, weekly use, days of cover, run-out date, open orders.
    plant_id: AUS, DAL, HOU, PHX or DEN (or the city name). part_id: like P-110."""
    return as_json(plant_ops.stock_position(plant_id, part_id))


@mcp.tool()
@safe
def get_plant_inventory(plant_id: str, part_id: str = "") -> str:
    """Current inventory at a plant. Leave part_id empty to list every part, most urgent first."""
    return as_json(plant_ops.plant_inventory(plant_id, part_id))


@mcp.tool()
@safe
def check_warehouse_space(plant_id: str, part_id: str = "", qty: int = 0) -> str:
    """Free warehouse space at a plant (storage units, % full). With part_id: how many items of that part fit;
    with qty: whether that quantity fits."""
    return as_json(plant_ops.warehouse_space(plant_id, part_id, qty))


@mcp.tool()
@safe
def find_nearby_surplus(part_id: str, plant_id: str) -> str:
    """Other Northfield plants (within 600 road miles) holding SURPLUS of this part that could be transferred to plant_id,
    with transfer days and truck cost."""
    return as_json(plant_ops.nearby_stock(part_id, plant_id))


@mcp.tool()
@safe
def list_open_orders(plant_id: str, part_id: str = "") -> str:
    """Open purchase orders for a plant (optionally one part): vendor, qty, value, promised date, on time or overdue."""
    return as_json(plant_ops.open_orders(plant_id, part_id))


@mcp.tool()
@safe
def find_parts(query: str = "") -> str:
    """Find part IDs by name or category (e.g. 'hydraulic pump', 'steel', 'sensor'). Use when the user names a part without its ID."""
    return as_json(plant_ops.find_parts(query))


@mcp.tool()
@safe
def list_late_deliveries(plant_id: str, days_back: int = 90) -> str:
    """Purchase orders for a plant that arrived late (last `days_back` days) or are open and overdue."""
    return as_json(plant_ops.late_deliveries(plant_id, days_back))


@mcp.tool()
@safe
def get_purchase_order(po_id: str) -> str:
    """One purchase order by ID (like PO-24137): vendor, part, qty, price, value, promised and received dates."""
    return as_json(plant_ops.purchase_order(po_id))


@mcp.tool()
@safe
def get_approval_route(plant_id: str, total_usd: float, vendor_risk_score: int, emergency: bool = False,
                       lowest_cost: bool = True) -> str:
    """Who must approve a purchase, from the plant's procurement policy: approval band by total value, emergency rule,
    vendor-risk rules, and whether a justification is required (not the lowest-cost vendor)."""
    return as_json(plant_ops.approval_route(plant_id, total_usd, vendor_risk_score, emergency, lowest_cost))


@mcp.resource("policy://{plant_id}")
def plant_policy(plant_id: str) -> str:
    """Full text of a plant's procurement and approval policy."""
    return (config.POLICIES_DIR / f"POL-{plant_id.upper()}-2026.md").read_text()


@mcp.prompt()
def purchase_request_email(draft_id: str) -> str:
    """Template for the email that asks an approver to sign a purchase request."""
    return (f"Write a short, professional email asking the approver to sign purchase request {draft_id}. "
            "Cover: what is needed and how many; why (stock, usage, run-out date); the vendor chosen and why; "
            "the vendors compared; total cost incl. freight; delivery date; the approval level that applies. "
            "Use only numbers from the purchase request.")


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.settings.port = 8101
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
