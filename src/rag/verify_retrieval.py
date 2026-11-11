"""Evaluate REAL pgvector retrieval against the populated policy table: relevance, not just execution.

Embeds every test query in ONE request (cached on disk, so re-runs cost nothing), then for each query:
  * runs the production vector search (read-only session) and shows the top passages with similarity;
  * checks that a passage that actually answers the question is in the top 3;
and also tests: negative controls (does similarity separate relevant from irrelevant?), the full-text fallback, a failing
embedder, missing-policy handling and the production theme queries.

Usage: ALLOW_PAID_CALLS=1 python -m src.rag.verify_retrieval      (paid only for queries not yet cached)
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from src.rag.embed import CachedEmbedder, EmbeddingError, OpenAIEmbedder
from src.rag.retrieve import PostgresPolicyRetriever, build_queries

RUNS = Path(__file__).resolve().parents[2] / "runs"
ING = "sunflower_oil"
CATS = {"contract_coverage", "supplier_status"}

COVERAGE = ["## Escalation of growing uncovered volume"]
SUPPLIER = ["## Conditional suppliers", "## Conditions to close"]
ALT = ["## Alternative suppliers are leads until qualified", "## Qualification steps"]
SPEC = ["## Use of public benchmarks", "## Crude and refined oil", "## Required product", "## Data quality"]
BENCH_AGE = ["## Use of public benchmarks", "## Data quality"]


@dataclass(frozen=True)
class Case:
    label: str
    query: str
    expected: list[str]          # headings of passages that actually answer the question (empty for negative controls)


def cases() -> list[Case]:
    theme = build_queries(ING, CATS)
    return [
        Case("T1 coverage / rising uncovered demand (production theme query)", theme[0], COVERAGE),
        Case("T2 conditional supplier approval and renewal (production theme query)", theme[1], SUPPLIER),
        Case("T3 alternative supplier qualification (production theme query)", theme[2], ALT),
        Case("T4 specification and benchmark limitations (production theme query)", theme[3], SPEC),
        Case("Q1 'What does our policy say when uncovered volume keeps rising from month to month?'",
             "What does our policy say when uncovered volume keeps rising from month to month?", COVERAGE),
        Case("Q2 'What approval is needed to renew a contract with a conditionally approved supplier?'",
             "What approval is needed to renew a contract with a conditionally approved supplier?", SUPPLIER),
        Case("Q3 'Can we send a purchase order to a supplier we found through web research?'",
             "Can we send a purchase order to a supplier we found through web research?", ALT),
        Case("Q4 'Can the crude sunflower oil benchmark be used as our supplier price, and how old can it be?'",
             "Can the crude sunflower oil benchmark be used as our supplier price, and how old can it be?", BENCH_AGE),
        Case("N1 negative control: football", "football match results and league table", []),
        Case("N2 negative control: cake recipe", "recipe for chocolate cake with butter and eggs", []),
        Case("N3 negative control: wheat milling spec (not covered by any policy)", "wheat flour milling protein content quality specification", []),
    ]


def heading(content: str) -> str:
    return content.splitlines()[0].strip()


def rank_of_expected(passages, expected: list[str]) -> int | None:
    """1-based rank of the first passage whose heading is an expected answer, or None."""
    for i, p in enumerate(passages, 1):
        if any(heading(p.content).startswith(e) for e in expected):
            return i
    return None


def evaluate(results: dict[str, list], cs: list[Case]) -> dict:
    pos = [c for c in cs if c.expected]
    ranks = {c.label: rank_of_expected(results[c.label], c.expected) for c in pos}
    top1_pos = [results[c.label][0].score for c in pos if results[c.label]]
    top1_neg = [results[c.label][0].score for c in cs if not c.expected and results[c.label]]
    return {"hit_at_3": sum(1 for r in ranks.values() if r is not None and r <= 3), "positives": len(pos), "ranks": ranks,
            "min_top1_positive": min(top1_pos) if top1_pos else None, "max_top1_negative": max(top1_neg) if top1_neg else None,
            "separated": bool(top1_pos and top1_neg and min(top1_pos) > max(top1_neg))}


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv()
    paid = os.getenv("ALLOW_PAID_CALLS") == "1"
    try:
        inner = OpenAIEmbedder(allow_paid=True, request_id="policy-retrieval-test") if paid else None
    except EmbeddingError as exc:
        print(f"EMBEDDER NOT AVAILABLE: {exc.code}")
        return 1
    emb = CachedEmbedder(inner)
    cs = cases()
    try:
        emb.embed([c.query for c in cs])                      # ONE request for every uncached query
    except EmbeddingError as exc:
        print(f"EMBEDDING FAILED: {exc.code}")
        return 1
    vec = PostgresPolicyRetriever(emb)
    out, lines = {}, ["# Real pgvector retrieval verification", ""]
    for c in cs:
        res = vec.retrieve(c.query, ING, 3)
        out[c.label] = res.passages
        lines += [f"## {c.label}", f"status={res.status} method={res.method} embedding_calls={res.embedding_calls}"]
        for i, p in enumerate(res.passages, 1):
            ok = "✔" if any(heading(p.content).startswith(e) for e in c.expected) else " "
            lines.append(f"{i}. {ok} score={p.score:.3f} [{p.policy_ref}] {heading(p.content)} — {' '.join(p.content.split()[3:30])}…")
        lines.append("")
    ev = evaluate(out, cs)
    lines += ["## Evaluation", f"- relevant passage in the top 3: **{ev['hit_at_3']} of {ev['positives']}** queries; ranks {ev['ranks']}",
              f"- lowest top-1 similarity of a relevant query: {ev['min_top1_positive']:.3f}; highest top-1 of a negative control: {ev['max_top1_negative']:.3f}; "
              f"separated: **{ev['separated']}**", ""]

    # fallbacks and missing-policy handling
    fts = PostgresPolicyRetriever(None)
    lines.append("## Full-text fallback (no embedder)")
    fb_hits = 0
    for c in [x for x in cs if x.expected]:
        r = fts.retrieve(c.query, ING, 3)
        rk = rank_of_expected(r.passages, c.expected)
        fb_hits += rk is not None
        lines.append(f"- {c.label[:2]}: status={r.status} method={r.method}; expected passage rank={rk}; top={[p.policy_ref for p in r.passages]}; note={r.note}")
    class Failing:
        model = "x"
        def embed(self, texts):
            raise EmbeddingError("EMBEDDING_CALL_FAILED:APITimeoutError")
    r = PostgresPolicyRetriever(Failing()).retrieve(cs[0].query, ING, 3)
    lines += [f"- embedder failure: status={r.status} method={r.method} passages={len(r.passages)} note={r.note}"]
    miss = PostgresPolicyRetriever(emb).retrieve("anything", "sugar", 3)
    nomatch = fts.retrieve("zzzz qqqq", ING, 3)
    lines += ["", "## Missing-policy handling",
              f"- ingredient with no policy (sugar): status={miss.status}, passages={len(miss.passages)}, note={miss.note}",
              f"- full-text query with no match: status={nomatch.status}, passages={len(nomatch.passages)}, note={nomatch.note}"]
    text = "\n".join(lines)
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / "rag-retrieval-verification.md").write_text(text, encoding="utf-8")
    (RUNS / "rag-retrieval-verification.json").write_text(json.dumps({"evaluation": ev, "fulltext_hits_top3_of_4": fb_hits}, default=str, indent=1), encoding="utf-8")
    print(text)
    print(f"\nembedding usage this run: {emb.usage()} | cache hits {emb.hits} misses {emb.misses}")
    return 0 if ev["hit_at_3"] == ev["positives"] else 2


if __name__ == "__main__":
    sys.exit(main())
