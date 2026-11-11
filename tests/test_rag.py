"""Policy RAG: chunking, idempotent loading plan, embedders, retrieval and fallback (offline; no database writes, no paid calls)."""
import hashlib
import math
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.rag import load_policies as lp
from src.rag.chunker import MAX_CHARS, DocumentError, load_documents, parse_document
from src.rag.embed import DIMENSIONS, EmbeddingError, HashingEmbedder, OpenAIEmbedder, to_vector_literal
from src.rag.retrieve import (FULLTEXT_SQL, VECTOR_SQL, InMemoryPolicyRetriever, PostgresPolicyRetriever, build_queries,
                              build_query, to_tsquery)
from tests.schema_utils import schema_columns

DOCS = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "documents"
INGREDIENTS = {"sunflower_oil", "wheat"}


@pytest.fixture(scope="module")
def chunks():
    cs, errors = load_documents(DOCS, INGREDIENTS)
    assert errors == []
    return cs


def doc(tmp_path, body="## Rule\nThis is a FICTIONAL rule about contract coverage.", **meta):
    base = {"doc_id": "POL-009", "title": "Test policy", "doc_type": "policy", "ingredients": "sunflower_oil", "synthetic": "true"}
    base.update(meta)
    front = "\n".join(f"{k}: {v}" for k, v in base.items() if v is not None)
    p = tmp_path / f"{base['doc_id']}-test.md"
    p.write_text(f"---\n{front}\n---\n# Title\n\n{body}\n", encoding="utf-8")
    return p


# ───────────── chunker ─────────────
def test_real_documents_parse_into_stable_hashed_chunks(chunks):
    assert len({c.doc_id for c in chunks}) == 6 and len(chunks) == 25
    assert {c.doc_type for c in chunks} == {"policy", "supplier_assessment"}
    assert all(c.is_synthetic and len(c.content_hash) == 64 and c.content_hash == hashlib.sha256(c.content.encode()).hexdigest() for c in chunks)
    refs = [c.policy_ref for c in chunks]
    assert len(set(refs)) == len(refs) and "POL-001#0" in refs and "ASM-003#2" in refs
    for d in {c.doc_id for c in chunks}:
        assert [c.chunk_index for c in chunks if c.doc_id == d] == list(range(sum(1 for c in chunks if c.doc_id == d)))
    assert all(c.content.startswith("## ") and len(c.content) <= MAX_CHARS for c in chunks)


def test_every_chunk_is_fictional_and_ingredient_scoped(chunks):
    assert all("fictional" in c.content.lower() for c in chunks if c.chunk_index == 0)      # every document declares itself fictional up front
    assert all(set(c.ingredient_codes) <= INGREDIENTS for c in chunks)


def test_chunking_is_deterministic(chunks):
    again, _ = load_documents(DOCS, INGREDIENTS)
    assert [(c.policy_ref, c.content_hash) for c in again] == [(c.policy_ref, c.content_hash) for c in chunks]


def test_long_sections_are_split_on_paragraph_boundaries(tmp_path):
    body = "## Long\n" + "\n\n".join(f"Paragraph {i} FICTIONAL " + "word " * 60 for i in range(8))
    cs = parse_document(doc(tmp_path, body))
    assert len(cs) > 1 and all(len(c.content) <= MAX_CHARS and c.content.startswith("## Long") for c in cs)
    assert [c.chunk_index for c in cs] == list(range(len(cs)))


@pytest.mark.parametrize("meta,msg", [
    ({"doc_id": "BAD"}, "must look like POL-001"), ({"doc_type": "memo"}, "doc_type must be one of"),
    ({"synthetic": "false"}, "synthetic: true"), ({"title": None}, "needs 'title'"), ({"ingredients": "sugar"}, "unknown ingredient"),
])
def test_invalid_front_matter_is_rejected_with_a_clear_message(tmp_path, meta, msg):
    with pytest.raises(DocumentError, match=msg):
        parse_document(doc(tmp_path, **meta), INGREDIENTS)


def test_document_must_declare_itself_fictional_and_have_sections(tmp_path):
    with pytest.raises(DocumentError, match="FICTIONAL"):
        parse_document(doc(tmp_path, "## Rule\nA plain rule."))
    with pytest.raises(DocumentError, match="no '## ' sections"):
        parse_document(doc(tmp_path, "Only text FICTIONAL, no headings"))


def test_duplicate_document_ids_and_missing_directory_are_reported(tmp_path):
    a = doc(tmp_path)
    (tmp_path / "POL-009-copy.md").write_text(a.read_text(encoding="utf-8"), encoding="utf-8")
    _, errors = load_documents(tmp_path, INGREDIENTS)
    assert any("already used" in e for e in errors)
    _, errors = load_documents(tmp_path / "nowhere", INGREDIENTS)
    assert errors


# ───────────── loader plan: idempotency, changes, orphans ─────────────
def existing_from(chunks, embedded=True):
    return {(c.doc_id, c.chunk_index): {"doc_title": c.doc_title, "doc_type": c.doc_type, "content_hash": c.content_hash,
                                        "ingredient_codes": list(c.ingredient_codes), "has_embedding": embedded} for c in chunks}


def test_empty_database_plans_all_inserts_and_embeddings(chunks):
    p = lp.plan_chunks(chunks, {})
    assert len(p.new) == 25 and not (p.changed or p.unchanged or p.orphans) and len(p.needs_embedding) == 25


def test_rerun_after_load_and_embed_is_a_no_op(chunks):
    p = lp.plan_chunks(chunks, existing_from(chunks))
    assert len(p.unchanged) == 25 and not (p.new or p.changed or p.orphans or p.needs_embedding)


def test_chunks_loaded_without_embeddings_only_need_the_embedding_step(chunks):
    p = lp.plan_chunks(chunks, existing_from(chunks, embedded=False))
    assert len(p.unchanged) == 25 and not p.new and len(p.needs_embedding) == 25


def test_changed_content_is_an_update_that_requires_re_embedding(chunks):
    ex = existing_from(chunks)
    ex[("POL-002", 1)]["content_hash"] = "0" * 64
    p = lp.plan_chunks(chunks, ex)
    assert p.changed == ["POL-002#1"] and p.needs_embedding == ["POL-002#1"] and len(p.unchanged) == 24


def test_database_only_chunks_are_reported_never_deleted(chunks):
    ex = existing_from(chunks)
    ex[("POL-001", 99)] = {"doc_title": "x", "doc_type": "policy", "content_hash": "1" * 64, "ingredient_codes": [], "has_embedding": True}
    assert lp.plan_chunks(chunks, ex).orphans == ["POL-001#99"]
    assert "DELETE" not in (lp.UPSERT_SQL + lp.EMBED_UPDATE_SQL).upper()


def test_upsert_clears_the_embedding_when_content_changes_and_is_a_no_op_when_unchanged():
    assert "ON CONFLICT (doc_id, chunk_index) DO UPDATE" in lp.UPSERT_SQL
    assert "THEN NULL ELSE t.embedding END" in lp.UPSERT_SQL and "IS DISTINCT FROM" in lp.UPSERT_SQL
    assert "is_synthetic" in lp.UPSERT_SQL and "true" in lp.UPSERT_SQL.split("VALUES")[1]
    assert "content_hash = %(content_hash)s" in lp.EMBED_UPDATE_SQL                # vector only stored for the hashed content


def test_loader_columns_exist_in_the_approved_schema():
    cols = schema_columns()["policy_chunks"]
    for sql in (lp.SELECT_EXISTING_SQL, lp.UPSERT_SQL, lp.SELECT_PENDING_SQL, lp.EMBED_UPDATE_SQL, VECTOR_SQL, FULLTEXT_SQL):
        for token in ("doc_id", "chunk_index", "doc_title", "doc_type", "content", "content_hash", "ingredient_codes", "embedding", "embedding_model"):
            if token in sql:
                assert token in cols, token
    assert "vector(1536)" in (Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql").read_text(encoding="utf-8")


def test_dry_run_cli_makes_no_changes_and_reports_cost(capsys):
    assert lp.main(["--no-db"]) == 0
    out = capsys.readouterr().out
    assert "documents: 6 | chunks: 25" in out and "DRY RUN" in out


def test_cli_rejects_invalid_documents(tmp_path, capsys):
    doc(tmp_path, synthetic="false")
    assert lp.main(["--no-db", "--docs-dir", str(tmp_path)]) == 2
    assert "REJECTED" in capsys.readouterr().out
    assert lp.main(["--no-db", "--apply"]) == 2


def test_embed_is_refused_without_explicit_paid_authorisation(monkeypatch, capsys):
    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr("src.db.preflight.connect", fake_connect)
    monkeypatch.setattr(lp, "fetch_existing", lambda conn: {})
    monkeypatch.delenv("ALLOW_PAID_CALLS", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    assert lp.main(["--embed"]) == 3
    assert "REFUSED" in capsys.readouterr().out


def test_cost_estimate_is_small_and_uses_the_documented_price(chunks):
    tok = lp.estimate_tokens(chunks, [c.policy_ref for c in chunks])
    assert 1000 < tok < 3000 and tok / 1e6 * lp.PRICE_PER_M_TOKENS < 0.001


# ───────────── embedders ─────────────
class FakeEmbeddings:
    def __init__(self, vectors=None, tokens=120, error=None):
        self.calls, self._v, self._t, self._e = [], vectors, tokens, error

    def create(self, **kw):
        self.calls.append(kw)
        if self._e:
            raise self._e
        n = len(kw["input"])
        vecs = self._v if self._v is not None else [[0.1] * DIMENSIONS for _ in range(n)]
        return SimpleNamespace(data=[SimpleNamespace(embedding=v) for v in vecs], usage=SimpleNamespace(prompt_tokens=self._t, total_tokens=self._t))


def test_openai_embedder_is_refused_without_paid_authorisation_and_needs_a_key(monkeypatch):
    with pytest.raises(EmbeddingError) as e:
        OpenAIEmbedder(allow_paid=False, client=object())
    assert e.value.code == "PAID_CALLS_NOT_AUTHORISED"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EmbeddingError) as e:
        OpenAIEmbedder(allow_paid=True)
    assert e.value.code == "MISSING_CREDENTIALS"


def test_openai_embedder_uses_the_approved_model_dimensions_and_logs_usage_without_text(tmp_path):
    fake = FakeEmbeddings()
    emb = OpenAIEmbedder(allow_paid=True, client=SimpleNamespace(embeddings=fake), usage_log=tmp_path / "u.jsonl", request_id="r")
    out = emb.embed(["secret policy text", "another"])
    assert len(out) == 2 and all(len(v) == 1536 for v in out)
    assert fake.calls[0]["model"] == "text-embedding-3-small" and fake.calls[0]["dimensions"] == 1536 and len(fake.calls) == 1   # one batch
    assert emb.usage() == {"calls": 1, "input_tokens": 120}
    log = (tmp_path / "u.jsonl").read_text(encoding="utf-8")
    assert "secret policy text" not in log and '"purpose": "embedding"' in log


def test_openai_embedder_rejects_wrong_dimensions_budget_overrun_and_sanitises_errors(tmp_path):
    bad = OpenAIEmbedder(allow_paid=True, client=SimpleNamespace(embeddings=FakeEmbeddings(vectors=[[0.1] * 3])), usage_log=None)
    with pytest.raises(EmbeddingError) as e:
        bad.embed(["x"])
    assert e.value.code == "UNEXPECTED_EMBEDDING_SHAPE"
    capped = OpenAIEmbedder(allow_paid=True, client=SimpleNamespace(embeddings=FakeEmbeddings(tokens=60_000)), usage_log=None, max_total_tokens=1000)
    capped.embed(["x"])
    with pytest.raises(EmbeddingError) as e:
        capped.embed(["y"])
    assert e.value.code == "TOKEN_BUDGET_EXCEEDED"
    boom = OpenAIEmbedder(allow_paid=True, client=SimpleNamespace(embeddings=FakeEmbeddings(error=RuntimeError("401 key sk-SECRET"))), usage_log=None)
    with pytest.raises(EmbeddingError) as e:
        boom.embed(["x"])
    assert e.value.code == "EMBEDDING_CALL_FAILED:RuntimeError" and "SECRET" not in str(e.value)


def test_hashing_embedder_is_deterministic_normalised_and_semantically_ordered():
    h = HashingEmbedder()
    a, b, c = h.embed(["conditional supplier audit renewal", "supplier audit conditions are pending", "crude oil refining capacity"])
    assert len(a) == 1536 and math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9) and h.embed(["conditional supplier audit renewal"])[0] == a
    dot = lambda u, v: sum(x * y for x, y in zip(u, v))  # noqa: E731
    assert dot(a, b) > dot(a, c)
    assert to_vector_literal([0.5, 0.25]) == "[0.5000000,0.2500000]"


# ───────────── in-memory retrieval (mock runs) ─────────────
@pytest.fixture(scope="module")
def memory(chunks):
    return InMemoryPolicyRetriever(chunks, HashingEmbedder())


@pytest.mark.parametrize("query,expected", [
    ("conditional supplier approval audit renewal sign-off", "ASM-003"),
    ("uncovered volume escalation coverage review", "POL-002"),
    ("alternative supplier qualification leads must not receive orders", "POL-003"),
    ("crude and refined oil specification", "POL-003"),
])
def test_vector_retrieval_ranks_the_relevant_document_first_or_near_first(memory, query, expected):
    res = memory.retrieve(query, "sunflower_oil", 3)
    assert res.status == "ok" and res.method == "in_memory_vector" and res.embedding_calls == 1
    assert expected in [p.doc_id for p in res.passages[:3]] and all(p.policy_ref == f"{p.doc_id}#{p.chunk_index}" for p in res.passages)
    assert res.passages == sorted(res.passages, key=lambda p: -p.score)


def test_passages_keep_ids_titles_and_provenance_from_the_documents(memory, chunks):
    res = memory.retrieve("conditional supplier", "sunflower_oil", 4)
    by = {c.policy_ref: c for c in chunks}
    for p in res.passages:
        c = by[p.policy_ref]
        assert (p.doc_id, p.chunk_index, p.doc_title, p.doc_type, p.content) == (c.doc_id, c.chunk_index, c.doc_title, c.doc_type, c.content)


def test_ingredient_filter_excludes_other_ingredient_documents(chunks):
    res = InMemoryPolicyRetriever(chunks, HashingEmbedder()).retrieve("benchmark specification refined", "wheat", 10)
    assert res.passages and all(p.doc_id in ("POL-001", "POL-002") for p in res.passages)


def test_embedding_failure_falls_back_to_keyword_search_and_says_so(chunks):
    res = InMemoryPolicyRetriever(chunks, HashingEmbedder(), fail_embedding=True).retrieve("conditional supplier audit", "sunflower_oil", 3)
    assert res.status == "fallback" and res.method == "in_memory_keyword" and res.passages and "embedding failed" in res.note
    res2 = InMemoryPolicyRetriever(chunks).retrieve("conditional supplier audit", "sunflower_oil", 3)
    assert res2.status == "fallback" and "no embedder" in res2.note


def test_failure_empty_corpus_and_no_match_are_unavailable_not_invented(chunks):
    assert InMemoryPolicyRetriever(chunks, fail_with="TIMEOUT").retrieve("q", "sunflower_oil", 3).status == "unavailable"
    empty = InMemoryPolicyRetriever([], HashingEmbedder()).retrieve("q", "sunflower_oil", 3)
    assert empty.status == "unavailable" and not empty.passages and "no policy chunks" in empty.note
    nomatch = InMemoryPolicyRetriever(chunks).retrieve("zzzz qqqq", "sunflower_oil", 3)
    assert nomatch.status == "unavailable" and not nomatch.passages


def test_theme_queries_are_deterministic_and_reflect_the_risk_categories():
    qs = build_queries("sunflower_oil", {"contract_coverage", "supplier_status"})
    assert len(qs) == 4 and "uncovered volume increases" in qs[0] and "conditional supplier" in qs[1]
    assert "alternative suppliers are leads" in qs[2] and "public benchmark use" in qs[3]
    assert build_queries("sunflower_oil", {"contract_coverage", "supplier_status"}) == qs
    bare = build_queries("sunflower_oil", set())
    assert len(bare) == 2 and not any("conditional" in q or "uncovered" in q for q in bare)
    assert to_tsquery("Conditional supplier AUDIT, audit!") == "audit | conditional | supplier" and to_tsquery("a b") == "policy"


@pytest.mark.parametrize("theme,heading", [
    (0, "## Escalation of growing uncovered volume"), (1, "## Conditions to close"), (1, "## Conditional suppliers"),
    (2, "## Alternative suppliers are leads until qualified"), (3, "## Use of public benchmarks"), (3, "## Crude and refined oil")])
def test_each_theme_query_surfaces_its_intended_policy_passage(memory, theme, heading):
    q = build_queries("sunflower_oil", {"contract_coverage", "supplier_status"})[theme]
    res = memory.retrieve(q, "sunflower_oil", 3)
    assert any(p.content.startswith(heading) for p in res.passages), [p.content.splitlines()[0] for p in res.passages]


def test_one_blended_query_would_have_missed_the_coverage_policy_which_is_why_retrieval_is_per_theme(memory):
    blended = build_query("sunflower_oil", {"contract_coverage", "supplier_status"})
    top6 = memory.retrieve(blended, "sunflower_oil", 6).passages
    assert not any(p.content.startswith("## Escalation of growing uncovered volume") for p in top6)      # live finding of 11 Nov
    merged = memory.retrieve(build_queries("sunflower_oil", {"contract_coverage", "supplier_status"}), "sunflower_oil", 3)
    assert any(p.content.startswith("## Escalation of growing uncovered volume") for p in merged.passages)


def test_themed_retrieval_embeds_all_queries_in_one_request_and_merges_without_duplicates(chunks):
    emb = HashingEmbedder()
    r = InMemoryPolicyRetriever(chunks, emb)
    before = emb.usage()["calls"]
    res = r.retrieve(build_queries("sunflower_oil", {"contract_coverage", "supplier_status"}), "sunflower_oil", 3)
    assert emb.usage()["calls"] - before == 1 and res.embedding_calls == 1
    refs = [p.policy_ref for p in res.passages]
    assert len(refs) == len(set(refs)) and 4 <= len(refs) <= 12 and res.passages == sorted(res.passages, key=lambda p: -p.score)


# ───────────── PostgreSQL retriever against a fake read-only connection ─────────────
class FakeConn:
    def __init__(self, total=25, embedded=25, vector_rows=None, text_rows=None, error=None):
        self.total, self.embedded, self.vector_rows, self.text_rows, self.error, self.sql = total, embedded, vector_rows or [], text_rows or [], error, []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.error:
            raise self.error
        head = sql.strip()[:20]
        rows = ([(self.total, self.embedded)] if head.startswith("SELECT count") else self.vector_rows if "OPERATOR(extensions" in sql
                else self.text_rows)
        return SimpleNamespace(fetchone=lambda: rows[0], fetchall=lambda: rows)


def pg(conn, embedder=None):
    @contextmanager
    def connect():
        yield conn
    return PostgresPolicyRetriever(embedder, connect)


ROW = ("POL-002", 3, "Contract Coverage and Price-Risk Policy", "policy", "## Escalation\nUncovered volume increases ...", 0.83)


def test_postgres_vector_search_is_used_when_embeddings_exist():
    conn = FakeConn(vector_rows=[ROW])
    res = pg(conn, HashingEmbedder()).retrieve("coverage review", "sunflower_oil", 4)
    assert res.status == "ok" and res.method == "pgvector" and res.embedding_calls == 1 and res.passages[0].policy_ref == "POL-002#3"
    sql, params = conn.sql[-1]
    assert "OPERATOR(extensions.<=>)" in sql and "LIMIT" in sql and params["k"] == 4 and params["ing"] == "sunflower_oil"
    assert params["q"].startswith("[") and params["q"].count(",") == 1535                      # a 1,536-dimension literal


def test_full_text_fallback_when_no_embeddings_are_stored_or_no_embedder_exists():
    conn = FakeConn(embedded=0, text_rows=[ROW])
    res = pg(conn, HashingEmbedder()).retrieve("coverage review", "sunflower_oil", 4)
    assert res.status == "fallback" and res.method == "fulltext" and "no embeddings stored" in res.note and res.embedding_calls == 0
    res = pg(FakeConn(text_rows=[ROW]), None).retrieve("coverage review", "sunflower_oil", 4)
    assert res.status == "fallback" and "no embedder" in res.note
    assert "to_tsvector('english', content)" in FULLTEXT_SQL


def test_query_embedding_failure_falls_back_to_full_text():
    class Failing:
        model = "x"

        def embed(self, texts):
            raise EmbeddingError("EMBEDDING_CALL_FAILED:APITimeoutError")

    res = pg(FakeConn(text_rows=[ROW]), Failing()).retrieve("q", "sunflower_oil", 3)
    assert res.status == "fallback" and "query embedding failed (EMBEDDING_CALL_FAILED:APITimeoutError)" in res.note


def test_empty_table_no_match_and_database_errors_are_unavailable_with_a_sanitised_note():
    empty = pg(FakeConn(total=0, embedded=0)).retrieve("q", "sunflower_oil", 3)
    assert empty.status == "unavailable" and "no policy chunks are loaded" in empty.note and not empty.passages
    nomatch = pg(FakeConn(embedded=0, text_rows=[])).retrieve("q", "sunflower_oil", 3)
    assert nomatch.status == "unavailable" and "no passage matched" in nomatch.note
    boom = pg(FakeConn(error=RuntimeError("connect failed postgresql://user:SECRETPW@db.example/postgres"))).retrieve("q", "sunflower_oil", 3)
    assert boom.status == "unavailable" and "RuntimeError" in boom.note and "SECRETPW" not in boom.note


def test_retrieval_sql_is_read_only_and_parameterised():
    for sql in (VECTOR_SQL, FULLTEXT_SQL):
        assert sql.lstrip().upper().startswith("SELECT") and "%(ing)s" in sql and "%(k)s" in sql
        assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE"))


def test_postgres_retriever_embeds_all_theme_queries_in_one_request_and_runs_one_search_per_query():
    conn = FakeConn(vector_rows=[ROW])
    emb = HashingEmbedder()
    res = pg(conn, emb).retrieve(build_queries("sunflower_oil", {"contract_coverage", "supplier_status"}), "sunflower_oil", 3)
    assert res.status == "ok" and res.embedding_calls == 1 and emb.usage()["calls"] == 1
    assert sum(1 for sql, _ in conn.sql if "OPERATOR(extensions" in sql) == 4
    assert [p.policy_ref for p in res.passages] == ["POL-002#3"]                  # identical rows from every theme are merged


# ───────────── loader safeguards (step 2/3 of the controlled ingestion) ─────────────
class FakeWriteConn:
    """Minimal connection double for embed_pending: records the SQL parameters; no database."""

    def __init__(self, pending):
        self.pending, self.updates, self.transaction_used = pending, [], False

    def execute(self, sql, params=None):
        assert sql == lp.SELECT_PENDING_SQL
        return SimpleNamespace(fetchall=lambda: self.pending)

    @contextmanager
    def transaction(self):
        self.transaction_used = True
        yield

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def executemany(self, sql, rows):
                assert sql == lp.EMBED_UPDATE_SQL
                conn.updates += rows

        return Cur()


def test_embed_pending_sends_every_missing_chunk_in_one_batch_and_guards_each_update_with_its_hash():
    emb = HashingEmbedder()
    pending = [("POL-001", 0, "text zero", "h0"), ("POL-001", 1, "text one", "h1")]
    conn = FakeWriteConn(pending)
    assert lp.embed_pending(conn, emb) == 2 and emb.usage()["calls"] == 1 and conn.transaction_used
    assert [(u["doc_id"], u["chunk_index"], u["content_hash"], u["model"]) for u in conn.updates] == [
        ("POL-001", 0, "h0", "test-hashing-1536"), ("POL-001", 1, "h1", "test-hashing-1536")]
    assert all(u["vec"].startswith("[") for u in conn.updates)


def test_embed_pending_makes_no_call_when_nothing_is_missing():
    emb = HashingEmbedder()
    assert lp.embed_pending(FakeWriteConn([]), emb) == 0 and emb.usage()["calls"] == 0


def test_apply_refuses_to_overwrite_changed_chunks_without_explicit_permission(monkeypatch, capsys, chunks):
    @contextmanager
    def fake_connect():
        yield object()

    existing = existing_from(chunks)
    existing[("POL-002", 1)]["content_hash"] = "0" * 64                 # stored content differs from the file
    monkeypatch.setattr("src.db.preflight.connect", fake_connect)
    monkeypatch.setattr(lp, "fetch_existing", lambda conn: existing)
    monkeypatch.setattr(lp, "apply_chunks", lambda *a, **k: pytest.fail("must not write"))
    assert lp.main(["--apply"]) == 2
    out = capsys.readouterr().out
    assert "REFUSED" in out and "POL-002#1" in out and "--allow-updates" in out


def test_verify_sql_reports_counts_dimensions_models_and_synthetic_flag():
    assert "extensions.vector_dims(embedding)" in lp.VERIFY_SQL and "bool_and(is_synthetic)" in lp.VERIFY_SQL and "DELETE" not in lp.VERIFY_SQL.upper()


# ───────────── query-embedding cache ─────────────
def test_cached_embedder_pays_only_for_missing_texts_and_in_one_batch(tmp_path):
    from src.rag.embed import CachedEmbedder
    inner = HashingEmbedder()
    c = CachedEmbedder(inner, tmp_path / "cache.json")
    a = c.embed(["first query", "second query"])
    assert inner.usage()["calls"] == 1 and c.misses == 2
    again = CachedEmbedder(inner, tmp_path / "cache.json").embed(["second query", "first query"])           # reloaded from disk
    assert inner.usage()["calls"] == 1 and again == [a[1], a[0]]
    c3 = CachedEmbedder(inner, tmp_path / "cache.json")
    c3.embed(["first query", "third query"])
    assert inner.usage()["calls"] == 2 and c3.misses == 1


def test_cache_only_embedder_fails_closed_so_retrieval_falls_back(tmp_path):
    from src.rag.embed import CachedEmbedder
    with pytest.raises(EmbeddingError) as e:
        CachedEmbedder(None, tmp_path / "none.json").embed(["unknown query"])
    assert e.value.code == "QUERY_EMBEDDING_NOT_CACHED"


# ───────────── duplicate passages (found in the real retrieval test: ASM-001#0 and ASM-003#0 share a paragraph) ─────────────
def test_identical_passage_text_under_different_documents_is_returned_once(chunks):
    r = InMemoryPolicyRetriever(chunks, HashingEmbedder()).retrieve("recipe chocolate cake butter eggs purpose status fictional assessment", "sunflower_oil", 10)
    texts = [" ".join(p.content.split()) for p in r.passages]
    assert len(texts) == len(set(texts))
    dup = [c for c in chunks if c.chunk_index == 0 and c.doc_type == "supplier_assessment"]
    assert len(dup) == 2 and dup[0].content == dup[1].content                      # the data really does contain the twin chunks
    assert sum(1 for p in InMemoryPolicyRetriever(dup, HashingEmbedder()).retrieve("fictional assessment purpose", "sunflower_oil", 5).passages) == 1


# ───────────── retrieval-verification helpers ─────────────
def test_relevance_evaluation_helpers():
    from src.rag.retrieve import PolicyPassage
    from src.rag.verify_retrieval import Case, evaluate, rank_of_expected
    P = lambda ref, head, s: PolicyPassage(ref, ref.split("#")[0], 0, "t", "policy", head + "\nbody", s)  # noqa: E731
    cs = [Case("good", "q", ["## Rule A"]), Case("late", "q", ["## Rule A"]), Case("miss", "q", ["## Rule A"]), Case("neg", "q", [])]
    res = {"good": [P("X#1", "## Rule A", 0.7)], "late": [P("X#2", "## Other", 0.5), P("X#3", "## Rule A", 0.4)],
           "miss": [P("X#4", "## Other", 0.3)], "neg": [P("X#5", "## Other", 0.2)]}
    ev = evaluate(res, cs)
    assert ev["ranks"] == {"good": 1, "late": 2, "miss": None} and ev["hit_at_3"] == 2 and ev["positives"] == 3
    assert ev["separated"] is True and ev["min_top1_positive"] == 0.3 and ev["max_top1_negative"] == 0.2
    overlapping = evaluate({**res, "neg": [P("X#5", "## Other", 0.45)]}, cs)
    assert overlapping["separated"] is False and overlapping["max_top1_negative"] == 0.45
    assert rank_of_expected([], ["## Rule A"]) is None
    assert True and rank_of_expected([], ["## Rule A"]) is None
