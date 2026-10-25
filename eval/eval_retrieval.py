"""EVAL LAYER 2a: Pinecone search quality (no AI model needed, free).
    python -m eval.eval_retrieval
160 searches: 8 clause types x 15 contracts (filtered to the vendor), 15 searches by vendor NAME with no filter,
5 policy questions x 5 plants, and 8 company-policy questions x 2 plants (searched as the app does). Hit@1 / Hit@3 = was the right section the 1st / in the top 3 results?
"""
import csv
import statistics
import time

from src import config
from src.agent.knowledge import search
from eval.common import pct, save


def main():
    rows = []
    for g in csv.DictReader(open(config.EVAL_DIR / "golden_retrieval.csv")):
        val = g["filter_value"]
        flt = ({g["filter_key"]: {"$in": val.split("+")}} if "+" in val else {g["filter_key"]: val}) if g["filter_key"] else None
        t0 = time.time()
        hits = search(g["namespace"], g["query"], flt, k=3)
        ms = (time.time() - t0) * 1000
        if hits and "error" in hits[0]:
            raise SystemExit(hits[0]["error"])
        right = [h["doc_id"] == g["expected_doc"] and (h["section"] or "").startswith(g["expected_section"][:12]) for h in hits]
        rows.append({**g, "hit_at_1": bool(right and right[0]), "hit_at_3": any(right),
                     "right_doc_at_1": bool(hits) and hits[0]["doc_id"] == g["expected_doc"],
                     "top_section": hits[0]["section"] if hits else None, "ms": round(ms)})
    groups = {"contract clauses (vendor filter)": [r for r in rows if r["namespace"] == "contracts" and r["filter_key"]],
              "contract by vendor name (no filter)": [r for r in rows if r["namespace"] == "contracts" and not r["filter_key"]],
              "plant policies (plant filter)": [r for r in rows if r["namespace"] == "policies" and "+" not in r["filter_value"]],
              "company policies (plant + GLOBAL, as the app searches)": [r for r in rows if "+" in r["filter_value"]]}
    summary = {"searches": len(rows), "median_ms": statistics.median(r["ms"] for r in rows)}
    for name, rs in groups.items():
        summary[name] = {"n": len(rs), "hit@1": pct(sum(r["hit_at_1"] for r in rs) / len(rs)),
                         "hit@3": pct(sum(r["hit_at_3"] for r in rs) / len(rs)),
                         "right_document@1": pct(sum(r["right_doc_at_1"] for r in rs) / len(rs))}
    misses = {}
    for r in rows:
        if not r["hit_at_1"]:
            misses[r["query"]] = misses.get(r["query"], 0) + 1
    summary["most_missed_queries"] = dict(sorted(misses.items(), key=lambda x: -x[1])[:5])
    for k, v in summary.items():
        print(f"{k}: {v}")
    save("retrieval_eval", rows, summary)


if __name__ == "__main__":
    main()
