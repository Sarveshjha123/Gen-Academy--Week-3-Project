"""MCP SERVER 2: Supplier system (vendors, contract price lines, quotes, risk scores, contract documents).

Run it on its own:            python src/mcp_servers/supplier_server.py            (stdio)
Remote (for ChatGPT/Claude):  python src/mcp_servers/supplier_server.py --http   (http://127.0.0.1:8102/mcp)
"""
import sys

from _compat import Server, as_json, safe  # noqa: E402

from src import config
from src.core import supplier

mcp = Server("northfield-supplier")


@mcp.tool()
@safe
def get_vendors_for_part(part_id: str) -> str:
    """All vendors with a contract for this part: contract rate, MOQ, lead time, freight, contract expiry/status, risk score."""
    return as_json(supplier.vendors_for_part(part_id))


@mcp.tool()
@safe
def get_latest_quotes(part_id: str) -> str:
    """Latest quote from each vendor for a part, flagged if EXPIRED or ABOVE the contract rate (both not usable)."""
    return as_json(supplier.latest_quotes(part_id))


@mcp.tool()
@safe
def get_vendor_risk(vendor_id: str) -> str:
    """Vendor risk score 0-100 (higher = riskier) with its parts (delivery, quality, financial, geo/supply).
    NOTE: scores are SIMULATED in this prototype."""
    return as_json(supplier.vendor_risk(vendor_id))


@mcp.tool()
@safe
def get_contract_status(vendor_id: str) -> str:
    """Contract register entry for a vendor: contract ID, effective/expiry dates, auto-renew, renewal decision date."""
    return as_json(supplier.contract_status(vendor_id))


@mcp.tool()
@safe
def find_price_overcharges(plant_id: str) -> str:
    """Open purchase orders at a plant priced ABOVE the contract rate, with the dollar overcharge."""
    return as_json(supplier.price_overcharges(plant_id))


@mcp.tool()
@safe
def list_contracts_needing_attention(plant_id: str, within_days: int = 60) -> str:
    """Contracts of vendors this plant uses that are expired, expiring, or need a renewal decision within `within_days`."""
    return as_json(supplier.contracts_needing_attention(plant_id, within_days))


@mcp.resource("contract://{vendor_id}")
def contract_document(vendor_id: str) -> str:
    """Full text of a vendor's Master Supply Agreement."""
    match = sorted(config.CONTRACTS_DIR.glob(f"{vendor_id.upper()}_*.md"))
    return match[0].read_text() if match else f"No contract for {vendor_id}"


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.settings.port = 8102
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
