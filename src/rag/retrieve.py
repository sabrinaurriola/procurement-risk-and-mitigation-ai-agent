"""Read-only retrieval of policy passages: pgvector similarity first, PostgreSQL full-text search as the fallback.

Retrieval is per THEME (coverage, supplier status, alternative suppliers, benchmark use): one blended query dilutes
the ranking and can miss the most relevant policy, so each theme gets its own top-k and the results are merged.
All theme queries are embedded in ONE embedding request.

Passages are fictional internal context for recommendations. They are NOT evidence of real regulatory compliance and
NOT proof that a supplier currently complies. If retrieval fails, the result says so (status 'unavailable'); callers must
surface the limitation instead of inventing policy guidance."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from src.rag.chunker import Chunk
from src.rag.embed import Embedder, EmbeddingError, to_vector_literal

INGREDIENT_FILTER = "(ingredient_codes @> ARRAY[%(ing)s]::text[] OR cardinality(ingredient_codes) = 0)"
COUNT_SQL = ("SELECT count(*), count(embedding) FROM procurement.policy_chunks WHERE " + INGREDIENT_FILTER)
VECTOR_SQL = ("SELECT doc_id, chunk_index, doc_title, doc_type, content, "
              "1 - (embedding OPERATOR(extensions.<=>) %(q)s::extensions.vector) AS score "
              "FROM procurement.policy_chunks WHERE embedding IS NOT NULL AND " + INGREDIENT_FILTER +
              " ORDER BY embedding OPERATOR(extensions.<=>) %(q)s::extensions.vector LIMIT %(k)s")
FULLTEXT_SQL = ("SELECT doc_id, chunk_index, doc_title, doc_type, content, ts_rank(to_tsvector('english', content), query) AS score "
                "FROM procurement.policy_chunks, to_tsquery('english', %(tsq)s) AS query "
                "WHERE to_tsvector('english', content) @@ query AND " + INGREDIENT_FILTER +
                " ORDER BY score DESC LIMIT %(k)s")


@dataclass(frozen=True)
class PolicyPassage:
    policy_ref: str            # doc_id#chunk_index, the citation identifier
    doc_id: str
    chunk_index: int
    doc_title: str
    doc_type: str
    content: str
    score: float | None


@dataclass
class PolicyResult:
    status: str                # 'ok' (vector search) | 'fallback' (full-text) | 'unavailable'
    method: str                # 'pgvector' | 'fulltext' | 'pgvector+fulltext' | 'in_memory_vector' | 'in_memory_keyword' | 'none'
    passages: list[PolicyPassage] = field(default_factory=list)
    note: str | None = None
    embedding_calls: int = 0


class PolicyRetriever(Protocol):
    def retrieve(self, query: str | list[str], ingredient: str, k: int) -> PolicyResult: ...


def build_queries(ingredient: str, categories: set[str]) -> list[str]:
    """Deterministic theme queries from the risk categories that exist (no LLM)."""
    base = ingredient.replace("_", " ")
    qs = []
    if "contract_coverage" in categories:
        qs.append(f"{base} contract coverage objective uncovered volume increases coverage review escalation")
    if "supplier_status" in categories:
        qs.append(f"{base} conditional supplier approval audit conditions renewal sign-off Procurement Director")
    qs.append(f"{base} alternative suppliers are leads until qualified must not receive orders qualification steps")
    qs.append(f"{base} crude and refined oil required product public benchmark use premium assumption")
    return qs


def build_query(ingredient: str, categories: set[str]) -> str:
    return " ".join(build_queries(ingredient, categories))


def _as_list(query: str | list[str]) -> list[str]:
    return [query] if isinstance(query, str) else list(query)


def to_tsquery(text: str) -> str:
    """OR-joined tokens: 'a b c' -> 'a | b | c' (plainto/websearch would AND them all)."""
    toks = sorted({t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 3})
    return " | ".join(toks) if toks else "policy"


def _row_to_passage(r) -> PolicyPassage:
    doc_id, idx, title, dtype, content, score = r
    return PolicyPassage(f"{doc_id}#{idx}", doc_id, idx, title, dtype, content, float(score) if score is not None else None)


def _merge(results: list[PolicyResult], notes: list[str], calls: int) -> PolicyResult:
    best: dict[str, PolicyPassage] = {}
    for res in results:
        for p in res.passages:
            if p.policy_ref not in best or (p.score or 0) > (best[p.policy_ref].score or 0):
                best[p.policy_ref] = p
    ranked = sorted(best.values(), key=lambda p: -(p.score or 0))
    seen_text: set[str] = set()
    passages = []
    for p in ranked:                       # identical text under different document ids (e.g. the shared 'Purpose' paragraph)
        key = " ".join(p.content.split()).casefold()
        if key not in seen_text:
            seen_text.add(key)
            passages.append(p)
    all_notes = list(dict.fromkeys([n for n in notes if n] + [r.note for r in results if r.note]))
    if not passages:
        return PolicyResult("unavailable", results[0].method if results else "none", [], "; ".join(all_notes + ["no passage matched the queries"]), calls)
    methods = {r.method for r in results if r.passages}
    status = "fallback" if any(r.status == "fallback" for r in results if r.passages) else "ok"
    method = methods.pop() if len(methods) == 1 else "pgvector+fulltext"
    return PolicyResult(status, method, passages, "; ".join(all_notes) or None, calls)


class PostgresPolicyRetriever:
    """Uses a READ ONLY session. `embedder` may be None (then full-text search is used)."""

    def __init__(self, embedder: Embedder | None = None, connect: Callable | None = None):
        self._embedder = embedder
        self._connect = connect

    def retrieve(self, query: str | list[str], ingredient: str, k: int = 3) -> PolicyResult:
        queries = _as_list(query)
        if self._connect is None:
            from src.db.preflight import connect as default_connect
            self._connect = default_connect
        notes: list[str] = []
        calls = 0
        try:
            with self._connect() as conn:
                total, embedded = conn.execute(COUNT_SQL, {"ing": ingredient}).fetchone()
                if total == 0:
                    return PolicyResult("unavailable", "none", [], "no policy chunks are loaded for this ingredient", 0)
                vectors = None
                if self._embedder is not None and embedded > 0:
                    try:
                        vectors = self._embedder.embed(queries)        # ONE request for all theme queries
                        calls = 1
                    except EmbeddingError as exc:
                        notes.append(f"query embedding failed ({exc.code})")
                elif self._embedder is None:
                    notes.append("no embedder available")
                else:
                    notes.append("no embeddings stored yet")
                results: list[PolicyResult] = []
                for i, q in enumerate(queries):
                    res = None
                    if vectors is not None:
                        rows = conn.execute(VECTOR_SQL, {"q": to_vector_literal(vectors[i]), "ing": ingredient, "k": k}).fetchall()
                        if rows:
                            res = PolicyResult("ok", "pgvector", [_row_to_passage(r) for r in rows])
                    if res is None:
                        rows = conn.execute(FULLTEXT_SQL, {"tsq": to_tsquery(q), "ing": ingredient, "k": k}).fetchall()
                        res = PolicyResult("fallback", "fulltext", [_row_to_passage(r) for r in rows])
                    results.append(res)
                return _merge(results, notes, calls)
        except Exception as exc:  # noqa: BLE001 - sanitised: class name only, never connection details
            return PolicyResult("unavailable", "none", [], f"policy retrieval failed ({type(exc).__name__})", calls)


class InMemoryPolicyRetriever:
    """Offline retriever over parsed chunks (mock runs and tests). With an embedder it ranks by cosine similarity,
    otherwise (or on `fail_embedding`) by keyword overlap, mirroring the database fallback."""

    def __init__(self, chunks: list[Chunk], embedder: Embedder | None = None, fail_with: str | None = None,
                 fail_embedding: bool = False):
        self._chunks, self._embedder, self._fail, self._fail_embedding = chunks, embedder, fail_with, fail_embedding
        self._vectors = embedder.embed([c.content for c in chunks]) if embedder and chunks else []

    def _eligible(self, ingredient: str) -> list[tuple[int, Chunk]]:
        return [(i, c) for i, c in enumerate(self._chunks) if not c.ingredient_codes or ingredient in c.ingredient_codes]

    def retrieve(self, query: str | list[str], ingredient: str, k: int = 3) -> PolicyResult:
        queries = _as_list(query)
        if self._fail:
            return PolicyResult("unavailable", "none", [], f"policy retrieval failed ({self._fail})", 0)
        cands = self._eligible(ingredient)
        if not cands:
            return PolicyResult("unavailable", "none", [], "no policy chunks are loaded for this ingredient", 0)
        notes: list[str] = []
        calls = 0
        qvecs = None
        if self._embedder is not None and not self._fail_embedding:
            qvecs = self._embedder.embed(queries)                       # one batched request
            calls = 1
        elif self._embedder is not None:
            notes.append("query embedding failed (simulated)")
        else:
            notes.append("no embedder available")
        results = []
        for qi, q in enumerate(queries):
            if qvecs is not None:
                scored = sorted(((sum(a * b for a, b in zip(qvecs[qi], self._vectors[i])), c) for i, c in cands), key=lambda x: -x[0])
                hits = [(s, c) for s, c in scored if s > 0][:k]
                if hits:
                    results.append(PolicyResult("ok", "in_memory_vector", [self._passage(c, s) for s, c in hits]))
                    continue
            qt = set(to_tsquery(q).replace(" | ", " ").split())
            scored = sorted(((len(qt & set(re.findall(r"[a-z0-9]+", c.content.lower()))) / math.sqrt(len(c.content) + 1), c) for _, c in cands),
                            key=lambda x: -x[0])
            hits = [(s, c) for s, c in scored if s > 0][:k]
            results.append(PolicyResult("fallback", "in_memory_keyword", [self._passage(c, s) for s, c in hits]))
        return _merge(results, notes, calls)

    @staticmethod
    def _passage(c: Chunk, score: float) -> PolicyPassage:
        return PolicyPassage(c.policy_ref, c.doc_id, c.chunk_index, c.doc_title, c.doc_type, c.content, round(float(score), 4))
