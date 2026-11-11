"""Opt-in (RUN_DB_TESTS=1), READ ONLY: the populated policy_chunks table. No writes, no paid calls (cached query embeddings only)."""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to read from Supabase")


def _conn():
    from dotenv import load_dotenv
    from src.db.preflight import connect
    load_dotenv()
    return connect()


def test_stored_chunks_match_the_files_and_are_embedded():
    from src.rag.chunker import load_documents
    from src.rag.load_policies import DOCS_DIR
    chunks, errors = load_documents(DOCS_DIR)
    assert not errors
    expected = {c.policy_ref: c for c in chunks}
    with _conn() as conn:
        rows = conn.execute("SELECT doc_id, chunk_index, content, content_hash, is_synthetic, embedding_model, "
                            "extensions.vector_dims(embedding) FROM procurement.policy_chunks").fetchall()
    if not rows:
        pytest.skip("policy_chunks not loaded")
    assert len(rows) == len(expected) == 25
    for doc_id, idx, content, h, synth, model, dims in rows:
        c = expected[f"{doc_id}#{idx}"]
        assert content == c.content and h == c.content_hash and synth is True
        assert model == "text-embedding-3-small" and dims == 1536


def test_cached_query_retrieval_returns_the_intended_policy_with_real_stored_text():
    from src.rag.embed import CachedEmbedder, CACHE_PATH
    from src.rag.retrieve import PostgresPolicyRetriever, build_queries
    if not Path(CACHE_PATH).exists():
        pytest.skip("no cached query embeddings")
    res = PostgresPolicyRetriever(CachedEmbedder(None)).retrieve(build_queries("sunflower_oil", {"contract_coverage", "supplier_status"}), "sunflower_oil", 3)
    if res.status != "ok":
        pytest.skip(f"cache does not cover these queries ({res.status})")
    refs = {p.policy_ref for p in res.passages}
    assert res.method == "pgvector" and {"POL-002#3", "POL-001#2"} <= refs
    texts = [" ".join(p.content.split()) for p in res.passages]
    assert len(texts) == len(set(texts))
