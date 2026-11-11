"""ONE targeted GDELT diagnostic through the PRODUCTION code path (same query, window, client, retry policy).

It performs a single logical search (bounded: at most the production maximum of 3 HTTP attempts, only if the first fail
transiently), makes NO LLM call, and reports four things separately:
  1. integration  - did the call complete (HTTP status, attempts, per-attempt outcome and duration, errors)?
  2. response     - what did GDELT actually return (content type, size, JSON shape)?
  3. retrieved    - how many articles, with dates, languages and domains?
  4. retained     - how many pass the deterministic pre-filter (date window, language, ingredient match)?
Whether the retained articles are *relevant* is a model judgement and is deliberately not assessed here.

Usage: python -m src.tools.gdelt_diagnostic [--out runs/gdelt-diagnostic.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from src.agent.config import PROFILES, Limits
from src.agent.credibility import host_of, redact_contacts
from src.agent.evidence import prefilter
from src.tools.search import GdeltTool

RUNS = Path(__file__).resolve().parents[2] / "runs"


def classify(result, retained: int) -> str:
    """One-line verdict that keeps connectivity, retrieval and usable evidence apart."""
    if not result.ok:
        return f"INTEGRATION FAILURE ({result.error_code})"
    if not result.items:
        return "HTTP SUCCESS, NO ARTICLES RETURNED (integration worked; no evidence)"
    if retained == 0:
        return "HTTP SUCCESS, ARTICLES RETRIEVED, NONE PASSED THE PRE-FILTER (no usable evidence)"
    return f"HTTP SUCCESS, {len(result.items)} RETRIEVED, {retained} PASSED THE PRE-FILTER (relevance not assessed: no LLM call)"


def run(tool: GdeltTool | None = None, today: date | None = None) -> dict:
    lim = Limits()
    profile = PROFILES["sunflower_oil"]
    today = today or date.today()
    tool = tool or GdeltTool()
    res = tool.search(profile.gdelt_query, kind="risk", lookback_days=lim.lookback_days, max_results=lim.max_results_per_call)
    accepted, rejected = prefilter(res.items, profile, set(), today, lim.lookback_days)
    dates = sorted(i.published_date for i in res.items if i.published_date)
    report = {
        "run_date": str(today),
        "request": {"query": profile.gdelt_query, "lookback_days": lim.lookback_days, "max_results": lim.max_results_per_call, "params": res.params},
        "integration": {"ok": res.ok, "http_status": res.http_status, "attempts": res.attempts, "error_code": res.error_code, "attempt_log": res.attempt_log},
        "response": {"note": res.note, "last_response": res.params.get("last_response")},
        "retrieved": {"articles": len(res.items), "date_range": [str(dates[0]), str(dates[-1])] if dates else None,
                      "languages": dict(Counter(i.language or "unknown" for i in res.items)),
                      "domains": dict(Counter(host_of(i.url) for i in res.items))},
        "retained": {"passed_prefilter": len(accepted), "rejected": dict(Counter(r.reason for r in rejected)),
                     "relevance_judged": False},
        "articles": [{"title": redact_contacts(i.title)[:110], "domain": host_of(i.url), "seen": str(i.published_date), "language": i.language}
                     for i in res.items],
    }
    report["verdict"] = classify(res, len(accepted))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=RUNS / "gdelt-diagnostic.json")
    args = ap.parse_args(argv)
    rep = run()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rep, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(rep, indent=1, default=str, ensure_ascii=False))
    return 0 if rep["integration"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
