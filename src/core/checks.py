"""Code checks (guardrails) used by the harness and by the evals. No AI here."""
import json
import re

from src import config


def read_json(text: str, default=None):
    """LLMs sometimes wrap JSON in words or ``` fences. Take the outermost {...}."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else default
    except json.JSONDecodeError:
        return default


def verify_contract_quotes(analysis_text: str) -> dict:
    """Code check: every 'quote' the contract analyst gives must appear word-for-word in that vendor's contract."""
    data = read_json(analysis_text, {}) or {}
    vid = str(data.get("vendor_id", "")).upper()
    files = sorted(config.CONTRACTS_DIR.glob(f"{vid}_*.md")) if vid else []
    if not files:
        return {"checked": 0, "found": 0, "missing": [], "note": "no vendor_id in answer"}
    contract = re.sub(r"\s+", " ", files[0].read_text()).lower()
    quotes = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "quote" and isinstance(v, str) and v.strip():
                    quotes.append(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    norm = [re.sub(r"\s+", " ", q).strip().strip('"').lower() for q in quotes]
    missing = [q for q, n in zip(quotes, norm) if n not in contract]
    return {"checked": len(quotes), "found": len(quotes) - len(missing), "missing": missing}


# ---------------------------------------------------------------- faithfulness check for chat answers (no AI)
_HYPHENS = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_SPACES = dict.fromkeys(map(ord, "\u00a0\u202f\u2009\u2007\u2008\u200a"), " ")
_QUOTES = {ord("\u2019"): "'", ord("\u2018"): "'", ord("\u201c"): '"', ord("\u201d"): '"', ord("\u200b"): None}


def normalize_text(text: str) -> str:
    """The model often writes P\u2011110 (non-breaking hyphen), 1\u202f880 (narrow space) and can\u2019t (curly quote).
    Turn them into plain characters so IDs, dates, numbers and phrases can be matched."""
    t = str(text or "").translate(_HYPHENS).translate(_SPACES).translate(_QUOTES)
    t = re.sub(r"(?<=\d) +%", "%", t)          # "2 %" -> "2%"
    t = re.sub(r"\$ +(?=\d)", "$", t)          # "$ 60" -> "$60"
    t = re.sub(r"(?<=\d) +k\b", "k", t)        # "25 k" -> "25k"
    return t


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
ID_RE = re.compile(r"\b(?:PO-\d{4,6}|P-\d{3}|V\d{2}|MSA-[A-Z0-9-]+|PR-[A-Z0-9-]+|Q-\d{3,5}|POL-[A-Z]+-\d{4})\b")
ISO_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
MDATE_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
NUM_RE = re.compile(r"(?<![\w.-])\$?\d+(?:\.\d+)?k?%?(?![\w-])", re.I)
# policy constants the supervisor prompt itself states (allowed without a tool call)
POLICY_NUMBERS = {25000, 100000, 50000, 75, 60, 48, 90, 95, 85, 1000, 30, 600, 2026}


def _join_thousands(text: str) -> str:
    """'60 400', '60,400', '1 880.0' (narrow spaces) -> '60400', '1880.0'."""
    return re.sub(r"(?<=\d)[,    ](?=\d{3}(?!\d))", "", text)


def _numbers(text: str) -> list:
    out = []
    for m in NUM_RE.finditer(_join_thousands(text)):
        tok = m.group(0)
        val = tok.lstrip("$").rstrip("%")
        mult = 1000 if val.lower().endswith("k") else 1
        try:
            out.append((tok, float(val.rstrip("kK")) * mult))
        except ValueError:
            pass
    return out


def _matches(v: float, pool: set) -> bool:
    for e in pool:
        if abs(e - v) <= max(0.01, 0.005 * abs(e)):   # rounding ($60.4k, 95.7 vs 95.68)
            return True
    return False


def verify_answer(answer: str, evidence: list, question: str = "") -> dict:
    """Every ID, date and number (>= 10, or with $/%/decimals) in the answer must appear in a tool result or the question.
    Returns {"checked": n, "unsupported": [...]} . Section numbers, years and small counts are ignored."""
    answer = normalize_text(answer)
    ev_text = _join_thousands(normalize_text(" ".join(evidence) + " " + question))
    ev_up = ev_text.upper()
    nums = {v for _, v in _numbers(ev_text)}
    pool = (nums | {round(v * 100, 4) for v in nums if 0 < v <= 1} | POLICY_NUMBERS      # 0.4 weight -> 40%
            | {round(100 - v, 4) for v in nums if 0 < v < 100 and v != int(v)})          # 95.7% full -> 4.3% free
    ev_dates = {f"{int(m)}-{int(d)}" for _, m, d in ISO_RE.findall(ev_text)}
    for mo, d in MDATE_RE.findall(ev_text):
        ev_dates.add(f"{MONTHS[mo[:3].lower()]}-{int(d)}")
    checked, bad = 0, []
    text = answer or ""
    for i in set(ID_RE.findall(text)):
        checked += 1
        if i.upper() not in ev_up:
            bad.append(i)
    dates_found = []
    for y, m, d in ISO_RE.findall(text):
        dates_found.append((f"{y}-{m}-{d}", f"{int(m)}-{int(d)}"))
    for mo, d in MDATE_RE.findall(text):
        dates_found.append((f"{mo} {d}", f"{MONTHS[mo[:3].lower()]}-{int(d)}"))
    for label, key in dates_found:
        checked += 1
        if key not in ev_dates:
            bad.append(label)
    # numbers: drop IDs, dates and section references first
    rest = ID_RE.sub(" ", text)
    rest = ISO_RE.sub(" ", rest)
    rest = MDATE_RE.sub(" ", rest)
    rest = re.sub(r"(?:section|sec\.?|§|step|option|#|week|weeks|days?|v)\s*\d+(?:\.\d+)?", " ", rest, flags=re.I)
    for tok, v in _numbers(rest):
        small_count = v < 10 and "$" not in tok and "%" not in tok and "." not in tok
        if small_count:
            continue
        checked += 1
        if not _matches(v, pool):
            bad.append(tok)
    return {"checked": checked, "unsupported": sorted(set(bad))}


# ---------------------------------------------------------------- late-delivery clause straight from the contract file (no AI)
def contract_clause(vendor_id: str, number: str = "5.2") -> str:
    """The exact text of one numbered clause (e.g. 5.2) from the vendor's contract file."""
    files = sorted(config.CONTRACTS_DIR.glob(f"{str(vendor_id).upper()}_*.md"))
    if not files:
        return ""
    for line in files[0].read_text().splitlines():
        if line.startswith(number + " "):
            return line.strip()
    return ""


def parse_delay_clause(text: str) -> dict:
    """Reads rate / per / grace / cap from a late-delivery clause. Returns {} if the wording is not recognised."""
    t = normalize_text(text or "")
    if not t:
        return {}
    if re.search(r"no liquidated damages|no .*credits", t, re.I):
        return {"none": True}
    rate = re.search(r"(\d+(?:\.\d+)?)% of the", t)
    grace = re.search(r"\((\d+)\) days", t)
    cap = re.search(r"(?:limited to|capped at|not exceed|exceed) (\d+(?:\.\d+)?)%", t)
    if "part thereof" in t or "week_or_part" in t:
        per = "week_or_part"
    elif re.search(r"per day|each day", t):
        per = "day"
    elif re.search(r"per week|each full week|per full week", t):
        per = "week"
    else:
        per = None
    if not (rate and grace and cap and per):
        return {}
    return {"rate_pct": float(rate.group(1)), "per": per, "grace_days": int(grace.group(1)), "cap_pct": float(cap.group(1))}
