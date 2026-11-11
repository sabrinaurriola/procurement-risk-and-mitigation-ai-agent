"""Fictional policy documents -> `procurement.policy_chunks` (and, separately, embeddings).

THREE STAGES, each explicit and each needing approval before it writes:
  (default)  DRY RUN   parse + chunk + compare with the database (read-only); prints what would change and what an
                       embedding run would cost. No writes, no paid calls.
  --apply    WRITE     upsert chunks (content, hash, provenance) with embedding = NULL. Idempotent: an unchanged chunk is
                       untouched; a changed chunk has its embedding cleared so it is re-embedded. Orphans are reported,
                       never deleted. No paid calls.
  --embed    PAID      embed chunks that have no embedding (text-embedding-3-small, 1,536 dims) and store the vectors.
                       Refused unless ALLOW_PAID_CALLS=1.

Usage: python -m src.rag.load_policies [--no-db] [--apply] [--embed]
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.agent.config import SUPPORTED_INGREDIENTS
from src.rag.chunker import Chunk, load_documents
from src.rag.embed import EMBEDDING_MODEL, Embedder, EmbeddingError, OpenAIEmbedder, to_vector_literal

DOCS_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic" / "documents"
PRICE_PER_M_TOKENS = 0.02    # USD, text-embedding-3-small list price (OpenAI model page, checked 11 Nov 2026; verify)

SELECT_EXISTING_SQL = ("SELECT doc_id, chunk_index, doc_title, doc_type, content_hash, ingredient_codes, "
                       "embedding IS NOT NULL AS has_embedding, embedding_model FROM procurement.policy_chunks")
UPSERT_SQL = (
    "INSERT INTO procurement.policy_chunks AS t (doc_id, doc_title, doc_type, chunk_index, content, content_hash, ingredient_codes, is_synthetic) "
    "VALUES (%(doc_id)s, %(doc_title)s, %(doc_type)s, %(chunk_index)s, %(content)s, %(content_hash)s, %(ingredient_codes)s, true) "
    "ON CONFLICT (doc_id, chunk_index) DO UPDATE SET doc_title = EXCLUDED.doc_title, doc_type = EXCLUDED.doc_type, "
    "content = EXCLUDED.content, content_hash = EXCLUDED.content_hash, ingredient_codes = EXCLUDED.ingredient_codes, "
    "embedding = CASE WHEN t.content_hash IS DISTINCT FROM EXCLUDED.content_hash THEN NULL ELSE t.embedding END, "
    "embedding_model = CASE WHEN t.content_hash IS DISTINCT FROM EXCLUDED.content_hash THEN NULL ELSE t.embedding_model END "
    "WHERE (t.doc_title, t.doc_type, t.content, t.content_hash, t.ingredient_codes) IS DISTINCT FROM "
    "(EXCLUDED.doc_title, EXCLUDED.doc_type, EXCLUDED.content, EXCLUDED.content_hash, EXCLUDED.ingredient_codes)")
SELECT_PENDING_SQL = ("SELECT doc_id, chunk_index, content, content_hash FROM procurement.policy_chunks "
                      "WHERE embedding IS NULL ORDER BY doc_id, chunk_index")
VERIFY_SQL = ("SELECT count(*), count(embedding), array_agg(DISTINCT extensions.vector_dims(embedding)) FILTER (WHERE embedding IS NOT NULL), "
              "array_agg(DISTINCT embedding_model) FILTER (WHERE embedding IS NOT NULL), bool_and(is_synthetic) FROM procurement.policy_chunks")
EMBED_UPDATE_SQL = ("UPDATE procurement.policy_chunks SET embedding = %(vec)s::extensions.vector, embedding_model = %(model)s "
                    "WHERE doc_id = %(doc_id)s AND chunk_index = %(chunk_index)s AND content_hash = %(content_hash)s")


@dataclass
class ChunkPlan:
    total: int = 0
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)       # in the database but not in the files (never deleted)
    needs_embedding: list[str] = field(default_factory=list)


def plan_chunks(chunks: list[Chunk], existing: dict[tuple[str, int], dict]) -> ChunkPlan:
    """Pure comparison of files vs database rows. `existing` maps (doc_id, chunk_index) -> row dict."""
    p = ChunkPlan(total=len(chunks))
    seen = set()
    for c in chunks:
        key = (c.doc_id, c.chunk_index)
        seen.add(key)
        row = existing.get(key)
        if row is None:
            p.new.append(c.policy_ref)
            p.needs_embedding.append(c.policy_ref)
        elif (row["doc_title"], row["doc_type"], row["content_hash"], tuple(row["ingredient_codes"])) != (
                c.doc_title, c.doc_type, c.content_hash, c.ingredient_codes):
            p.changed.append(c.policy_ref)
            p.needs_embedding.append(c.policy_ref)
        else:
            p.unchanged.append(c.policy_ref)
            if not row["has_embedding"]:
                p.needs_embedding.append(c.policy_ref)
    p.orphans = [f"{d}#{i}" for (d, i) in sorted(existing) if (d, i) not in seen]
    return p


def fetch_existing(conn) -> dict[tuple[str, int], dict]:
    cur = conn.execute(SELECT_EXISTING_SQL)
    names = [d.name for d in cur.description]
    return {(r[0], r[1]): dict(zip(names, r)) for r in cur.fetchall()}


def apply_chunks(conn, chunks: list[Chunk]) -> int:
    """Upsert all chunks in one transaction; returns the number of rows actually inserted or changed."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, [{"doc_id": c.doc_id, "doc_title": c.doc_title, "doc_type": c.doc_type, "chunk_index": c.chunk_index,
                                          "content": c.content, "content_hash": c.content_hash,
                                          "ingredient_codes": list(c.ingredient_codes)} for c in chunks])
            return cur.rowcount


def embed_pending(conn, embedder: Embedder) -> int:
    """Embed every chunk without a vector in ONE batch and store the vectors (guarded by the content hash)."""
    rows = conn.execute(SELECT_PENDING_SQL).fetchall()
    if not rows:
        return 0
    vectors = embedder.embed([r[2] for r in rows])
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(EMBED_UPDATE_SQL, [{"vec": to_vector_literal(v), "model": embedder.model, "doc_id": r[0], "chunk_index": r[1],
                                                "content_hash": r[3]} for r, v in zip(rows, vectors)])
    return len(rows)


def verify_embeddings(conn) -> dict:
    total, embedded, dims, models, synthetic = conn.execute(VERIFY_SQL).fetchone()
    return {"chunks": total, "with_embedding": embedded, "dimensions": sorted(dims or []), "models": sorted(models or []), "all_synthetic": synthetic}


def estimate_tokens(chunks: list[Chunk], refs: list[str]) -> int:
    by = {c.policy_ref: c for c in chunks}
    return sum(round(len(by[r].content) / 4) for r in refs)


def render(chunks: list[Chunk], errors: list[str], plan: ChunkPlan | None) -> str:
    lines = ["Policy documents dry-run summary", f"documents: {len({c.doc_id for c in chunks})} | chunks: {len(chunks)} | document errors: {len(errors)}"]
    lines += [f"  ERROR {e}" for e in errors]
    per: dict[str, list[Chunk]] = {}
    for c in chunks:
        per.setdefault(c.doc_id, []).append(c)
    for d, cs in per.items():
        lines.append(f"  {d} '{cs[0].doc_title[:58]}' ({cs[0].doc_type}): {len(cs)} chunks, {sum(len(c.content) for c in cs)} chars, "
                     f"ingredients={list(cs[0].ingredient_codes) or 'all'}")
    if plan is not None:
        lines += [f"database effect if --apply: insert {len(plan.new)}, update {len(plan.changed)}, unchanged {len(plan.unchanged)}, "
                  f"orphans (reported, never deleted) {len(plan.orphans)}",
                  f"embeddings needed: {len(plan.needs_embedding)} chunks"]
        if plan.needs_embedding:
            tok = estimate_tokens(chunks, plan.needs_embedding)
            lines.append(f"estimated embedding cost: ~{tok} tokens in one batch = ~${tok / 1e6 * PRICE_PER_M_TOKENS:.6f} "
                         f"at ${PRICE_PER_M_TOKENS}/M tokens ({EMBEDDING_MODEL}, 1,536 dims; price to be verified)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write chunks (no embeddings) to the database (approval required)")
    ap.add_argument("--embed", action="store_true", help="embed chunks without vectors (PAID; needs ALLOW_PAID_CALLS=1)")
    ap.add_argument("--allow-updates", action="store_true", help="permit --apply to change chunks whose stored content differs (otherwise refused)")
    ap.add_argument("--no-db", action="store_true", help="parse only; no database access")
    ap.add_argument("--docs-dir", type=Path, default=DOCS_DIR)
    args = ap.parse_args(argv)

    chunks, errors = load_documents(args.docs_dir, set(SUPPORTED_INGREDIENTS) | {"wheat"})
    plan = None
    if not args.no_db:
        from src.db.preflight import connect
        try:
            with connect() as conn:                       # READ ONLY session
                plan = plan_chunks(chunks, fetch_existing(conn))
        except Exception as exc:
            print(f"FAIL: database read error {type(exc).__name__} sqlstate={getattr(exc, 'sqlstate', None)}")
            return 1
    print(render(chunks, errors, plan))
    if errors:
        print("REJECTED: fix the document errors above before loading.")
        return 2
    if not args.apply and not args.embed:
        print("DRY RUN: no changes made. --apply writes chunks (no embeddings); --embed makes the paid embedding call. Both need approval.")
        return 0
    if args.no_db:
        print("--apply/--embed cannot be combined with --no-db")
        return 2
    if args.apply and plan.changed and not args.allow_updates:
        print(f"REFUSED: {len(plan.changed)} existing chunk(s) have different content and would be overwritten: {plan.changed}. "
              "Review the change, then re-run with --allow-updates. Nothing was written.")
        return 2
    import psycopg
    from dotenv import load_dotenv
    load_dotenv()
    if args.embed and os.getenv("ALLOW_PAID_CALLS") != "1":
        print("REFUSED: --embed makes a paid embedding call. Set ALLOW_PAID_CALLS=1 only after approval.")
        return 3
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15, sslmode="require") as conn:
        if args.apply:
            affected = apply_chunks(conn, chunks)
            after = plan_chunks(chunks, fetch_existing(conn))
            print(f"APPLIED: planned insert {len(plan.new)} / update {len(plan.changed)} / skip {len(plan.unchanged)}; rows actually written {affected}")
            print(f"VERIFIED AFTER WRITE: unchanged {len(after.unchanged)}, new {len(after.new)}, changed {len(after.changed)}, orphans {len(after.orphans)}")
        if args.embed:
            try:
                n = embed_pending(conn, OpenAIEmbedder(allow_paid=True, request_id="policy-embed"))
            except EmbeddingError as exc:
                print(f"EMBEDDING FAILED: {exc.code}")
                return 1
            print(f"EMBEDDED: {n} chunks (one batch)")
            print(f"VERIFIED: {verify_embeddings(conn)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
