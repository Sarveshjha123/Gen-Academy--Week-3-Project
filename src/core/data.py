"""Loads the fictional 'company systems' (CSV files). Plain pandas, no AI.
In a real company these would be the ERP (plants, stock, purchase orders) and the supplier system (vendors, contracts, quotes).
"""
import math
from datetime import date, timedelta
from functools import lru_cache

import pandas as pd

from src import config


TEXT_COLUMNS = {"plant_id", "plant_name", "state", "plant_head", "part_id", "part_name", "category", "critical", "vendor_id",
                "vendor_name", "risk_band", "method", "scored_on", "contract_id", "effective_date", "expiry_date", "auto_renew",
                "quote_id", "quoted_on", "valid_until", "po_id", "order_date", "promised_date", "received_date", "status", "note",
                "role", "name", "email", "from_plant", "to_plant"}


@lru_cache(maxsize=None)
def table(name: str) -> pd.DataFrame:
    """Read a CSV. Text columns stay text (empty = None); every other column is forced to a number,
    so different pandas versions can't read numbers as text (that caused a crash on some Macs)."""
    df = pd.read_csv(config.DATA_DIR / f"{name}.csv", dtype=str, keep_default_na=False)
    for col in df.columns:
        if col in TEXT_COLUMNS:
            df[col] = df[col].astype(object).where(df[col] != "", None)
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def reload():
    table.cache_clear()


def today() -> date:
    return config.TODAY


def days_from_today(n: float) -> str:
    return (config.TODAY + timedelta(days=math.floor(n))).isoformat()


class NotFound(ValueError):
    """Raised for unknown plants, parts or vendors, so the agent refuses instead of guessing."""


def plant(plant_id: str) -> dict:
    p = table("plants")
    key = str(plant_id).strip()
    row = p[(p.plant_id.str.upper() == key.upper()) | (p.plant_name.str.lower() == key.lower())]
    if row.empty:
        raise NotFound(f"Unknown plant '{plant_id}'. Known plants: " + ", ".join(f"{r.plant_id} ({r.plant_name})" for r in p.itertuples()))
    return row.iloc[0].to_dict()


def part(part_id: str) -> dict:
    p = table("parts")
    key = str(part_id).strip()
    row = p[(p.part_id.str.upper() == key.upper()) | (p.part_name.str.lower() == key.lower())]
    if row.empty:   # allow a partial name like "hydraulic pump"
        row = p[p.part_name.str.lower().str.contains(key.lower(), regex=False)]
    if row.empty or len(row) > 1:
        raise NotFound(f"Unknown or ambiguous part '{part_id}'. Use a part ID like P-110. Known parts: "
                       + ", ".join(f"{r.part_id} {r.part_name}" for r in p.itertuples()))
    return row.iloc[0].to_dict()


def vendor(vendor_id: str) -> dict:
    v = table("vendor_risk_scores")
    key = str(vendor_id).strip()
    row = v[(v.vendor_id.str.upper() == key.upper()) | (v.vendor_name.str.lower() == key.lower())]
    if row.empty:
        row = v[v.vendor_name.str.lower().str.contains(key.lower(), regex=False)]
    if row.empty or len(row) > 1:
        raise NotFound(f"Unknown vendor '{vendor_id}'. Known vendors: " + ", ".join(f"{r.vendor_id} {r.vendor_name}" for r in v.itertuples()))
    return row.iloc[0].to_dict()


def money(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"


def clean(obj):
    """Make numpy/pandas values JSON-friendly."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    if hasattr(obj, "item"):
        obj = obj.item()
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj
