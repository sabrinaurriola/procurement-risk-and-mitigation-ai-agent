"""S2 preparation (offline, no database): the DRAFT migration sql/drafts/002_ops_events.sql, the Python event model and the documentation must agree.
Nothing here connects to a database or applies anything."""
import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.api.events import (BENCHMARK_STATUSES, DETAIL_KEYS, DETAIL_MAX_BYTES, DETAIL_MAX_BYTES_DB, EVENT_TYPES, FINANCIAL_STATUSES, INSERT_COLUMNS,
                            INT_MAX, NOTICE_KINDS, NOTION_ID_RE, REASON_CODE_RE, SENSITIVE_MARKERS, SHA256_RE, SOURCES, STATUS_CLASSES, OperationalEvent,
                            events_from_envelope)
from tests.ops_event_cases import DB_EXPECT, DB_SKIP, INVALID, PYTHON_ONLY_INVALID, VALID
from tests.test_api_events import envelope

ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = ROOT / "sql" / "drafts" / "002_ops_events.sql"
RAW = SQL_FILE.read_text(encoding="utf-8")
SQL = re.sub(r"--[^\n]*", "", RAW)                                   # code without comments
SCHEMA = OperationalEvent.model_json_schema()["properties"]
DOC = (ROOT / "docs" / "day4-architecture.md").read_text(encoding="utf-8")
COLTYPE = r"(?:uuid|text|timestamptz|integer|jsonb|numeric\(\d+,\d+\))(?=[\s,])"


def quoted_list(constraint: str) -> set[str]:
    m = re.search(rf"CONSTRAINT {constraint} CHECK \(\s*\w+ IN \(([^)]*)\)", SQL, re.S)
    assert m, constraint
    return set(re.findall(r"'([^']*)'", m.group(1)))


def table_block() -> str:
    return re.search(r"CREATE TABLE ops\.operational_events \((.*?)\n\);", SQL, re.S).group(1)


# ───────── SQL ⇔ Python ─────────
def test_enumerations_in_sql_equal_the_python_literals():
    assert quoted_list("ck_event_type") == set(EVENT_TYPES) and len(EVENT_TYPES) == 11
    assert quoted_list("ck_component") == {"api", "n8n"}
    assert quoted_list("ck_severity") == {"info", "warning", "error"}
    assert quoted_list("ck_mode") == {"live", "mock"}
    assert quoted_list("ck_assessment_status") == {"completed", "partial", "failed"}
    assert quoted_list("ck_archive_status") == {"incomplete", "stored", "failed"}
    assert quoted_list("ck_delivery_status") == {"sent", "failed"}
    for name, enum in (("component", {"api", "n8n"}), ("severity", {"info", "warning", "error"}), ("mode", {"live", "mock"}),
                       ("assessment_status", {"completed", "partial", "failed"}), ("archive_status", {"incomplete", "stored", "failed"}),
                       ("delivery_status", {"sent", "failed"})):
        prop = SCHEMA[name]
        found = set(prop.get("enum", [])) or {v for part in prop.get("anyOf", []) for v in part.get("enum", [])}
        assert found == enum, name


def test_removed_states_do_not_exist_anywhere():
    for banned in ("assessment.rejected", "'skipped'", "'pending'"):
        assert banned not in SQL, banned
    assert "assessment.rejected" not in EVENT_TYPES


def test_columns_in_sql_equal_the_python_insert_columns():
    body = table_block().split("CONSTRAINT")[0]
    cols = re.findall(rf"^\s{{4}}(\w+)\s+{COLTYPE}", body, re.M)
    assert set(cols) == set(INSERT_COLUMNS) | {"event_id", "recorded_at"} and len(cols) == len(set(cols))
    assert [c for c in cols if c in INSERT_COLUMNS] == list(INSERT_COLUMNS)                  # same order
    assert set(OperationalEvent.model_fields) == set(INSERT_COLUMNS)
    types = dict(re.findall(rf"^\s{{4}}(\w+)\s+({COLTYPE})", body, re.M))
    assert types["cost_usd"] == "numeric(10,6)" and types["duration_ms"] == types["input_tokens"] == types["output_tokens"] == "integer"
    assert INT_MAX == 2**31 - 1


def test_detail_rules_agree():
    block = re.search(r"CONSTRAINT ck_detail CHECK \((.*?)'\{\}'::jsonb\),", SQL, re.S).group(1)
    keys = set(re.findall(r"'([a-z0-9_]+)'", re.search(r"ARRAY\[(.*?)\]::text\[\]", block, re.S).group(1)))
    assert keys == set(DETAIL_KEYS) and len(DETAIL_KEYS) == 22
    assert int(re.search(r"octet_length\(detail::text\) <= (\d+)", block).group(1)) == DETAIL_MAX_BYTES_DB == 4096
    assert DETAIL_MAX_BYTES < DETAIL_MAX_BYTES_DB and "jsonb_typeof(detail) = 'object'" in block


def _values_block() -> str:
    return re.search(r"CONSTRAINT ck_detail_values CHECK \((.*?)\)\),\s*(?=CONSTRAINT)", SQL, re.S).group(1)


def _sql_in_list(key: str) -> set[str]:
    m = re.search(rf"detail->>'{key}' IN \(([^)]*)\)", _values_block())
    assert m, key
    return set(re.findall(r"'([^']*)'", m.group(1)))


def _sql_regex(key: str) -> str:
    return re.search(rf"detail->>'{key}' ~ '([^']+)'", _values_block()).group(1)


def test_detail_value_rules_agree_between_sql_and_python():
    assert _sql_in_list("source") == set(SOURCES) and _sql_in_list("status_class") == set(STATUS_CLASSES)
    assert _sql_in_list("benchmark_status") == set(BENCHMARK_STATUSES) and _sql_in_list("financial_status") == set(FINANCIAL_STATUSES)
    assert _sql_in_list("notice_kind") == set(NOTICE_KINDS)
    assert _sql_regex("status") == REASON_CODE_RE and _sql_regex("notion_page_id") == NOTION_ID_RE and _sql_regex("report_sha256") == SHA256_RE
    covered = set(re.findall(r"\(detail->'(\w+)'\) IS NULL", _values_block()))
    assert covered == {"source", "status_class", "status", "benchmark_status", "financial_status", "notice_kind", "notion_page_id", "report_sha256"}
    assert covered <= set(DETAIL_KEYS)


def test_sensitive_marker_list_is_identical_in_sql_and_python():
    sql_marker = re.search(r"detail::text !~\* '([^']+)'", SQL).group(1)
    assert sql_marker == SENSITIVE_MARKERS
    for hit in ("https://x", "a@b", "Bearer x", "API_KEY=1", "api-key", "my password", "passwd", "secret", "token", "Authorization", "sk-abc", "postgresql://", "postgres:"):
        assert re.search(SENSITIVE_MARKERS, hit, re.I), hit
    for fine in ("risks", "tavily", "report_sha256", "http_wait_ms", "HTTP_429", "0f4e1c2a-6b7d-4e8f-9a0b-1c2d3e4f5a6b"):
        assert not re.search(SENSITIVE_MARKERS, fine, re.I), fine


def test_database_expectations_cover_every_database_testable_invalid_row():
    assert set(DB_EXPECT) == set(INVALID) - set(DB_SKIP)
    constraints = set(re.findall(r"CONSTRAINT (\w+)", SQL))
    for name, (sqlstate, names) in DB_EXPECT.items():
        assert sqlstate in {"23514", "22003"} and (names or sqlstate == "22003"), name
        assert names <= constraints, (name, names - constraints)
    assert not (set(DB_EXPECT) & set(VALID)) and DB_SKIP <= set(INVALID)


def test_patterns_agree():
    rid = re.search(r"CONSTRAINT ck_request_id CHECK \(request_id ~ '([^']+)'", SQL).group(1)
    key = re.search(r"CONSTRAINT ck_event_key CHECK \(event_key ~ '([^']+)'", SQL).group(1)
    code = re.search(r"CONSTRAINT ck_error_code CHECK \(error_code ~ '([^']+)'", SQL).group(1)
    assert rid == SCHEMA["request_id"]["pattern"] and key == SCHEMA["event_key"]["pattern"]
    assert code in {p.get("pattern") for p in SCHEMA["error_code"]["anyOf"]}
    assert "starts_with(event_key, request_id || '|')" in SQL


def test_the_coupling_checks_use_null_safe_forms():
    assert "IS NOT DISTINCT FROM 'stored'" in SQL and "IS NOT DISTINCT FROM 'sent'" in SQL and "IS NOT DISTINCT FROM 'failed'" in SQL
    assert "COALESCE(archive_status IN ('incomplete', 'failed'), false)" in SQL
    assert "(event_type = 'assessment.finished') = (assessment_status IS NOT NULL)" in SQL
    assert "(component = 'n8n') = (event_type ~ '^(archive|delivery)[.]')" in SQL


# ───────── safety of the SQL itself ─────────
def test_the_draft_is_isolated_from_the_migration_runner():
    from src.db.migrate import SQL_DIR, migration_files
    assert [v for v, _ in migration_files()] == ["001"] and SQL_FILE.parent == SQL_DIR / "drafts"
    assert not list(SQL_DIR.glob("002_*.sql"))
    assert RAW.lstrip().startswith("-- 002_ops_events.sql — DRAFT FOR REVIEW. NOT APPLIED.")


def test_the_draft_has_no_password_and_no_dangerous_privileges():
    no_markers = SQL.replace(SENSITIVE_MARKERS, "")                                            # the marker regex itself names "postgres" and "passw..."
    assert not re.search(r"PASSWORD", no_markers, re.I) and "postgres" not in no_markers.lower().replace("procurement.schema_migrations", "")
    for attr in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS", "NOINHERIT"):
        assert attr in SQL
    grants = re.findall(r"GRANT\s+(.*?)\s+ON\s+(.*?)\s+TO\s+(\w+)", SQL, re.S)
    assert grants == [("USAGE", "SCHEMA ops", "ops_writer"), ("INSERT", "ops.operational_events", "ops_writer")]
    assert not re.search(r"GRANT\s+(ALL|SELECT|UPDATE|DELETE|TRUNCATE)", SQL, re.I) and "WITH GRANT OPTION" not in SQL
    assert "IF NOT EXISTS" not in SQL                                                         # fail loudly instead of adopting unknown state
    assert SQL.strip().startswith("BEGIN;") and SQL.strip().endswith("COMMIT;")
    assert re.findall(r"procurement\.\w+", SQL) == ["procurement.schema_migrations"]            # the only procurement object touched
    assert "ENABLE ROW LEVEL SECURITY" in SQL and "FOR INSERT TO ops_writer WITH CHECK (true)" in SQL
    assert not re.search(r"FORCE ROW LEVEL SECURITY|CREATE POLICY[^;]*FOR (SELECT|UPDATE|DELETE|ALL)", SQL)
    assert "REVOKE ALL ON SCHEMA ops FROM PUBLIC" in SQL and "REVOKE ALL ON ops.operational_events FROM PUBLIC" in SQL
    assert "INSERT INTO procurement.schema_migrations (version) VALUES ('002')" in SQL


# ───────── shared valid/invalid cases (the same rows run against PostgreSQL at the G-OPS gate) ─────────
@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_rows_are_accepted_and_map_to_columns(name):
    e = OperationalEvent(**VALID[name])
    assert list(e.to_row()) == list(INSERT_COLUMNS) and e.to_row()["event_key"] == VALID[name]["event_key"]


def test_valid_rows_cover_every_event_type():
    assert {v["event_type"] for v in VALID.values()} == set(EVENT_TYPES)


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_rows_are_rejected(name):
    with pytest.raises(ValidationError):
        OperationalEvent(**INVALID[name])


@pytest.mark.parametrize("name", sorted(PYTHON_ONLY_INVALID))
def test_rows_only_python_rejects(name):
    with pytest.raises(ValidationError):
        OperationalEvent(**PYTHON_ONLY_INVALID[name])


def test_derived_events_from_a_demo_002_shaped_envelope_are_valid_rows():
    rows = [e.to_row() for e in events_from_envelope(envelope(), model="gpt-5.4-mini")]
    assert {r["event_type"] for r in rows} == {"research.source", "validation.removed", "assessment.finished"}
    assert [r for r in rows if r["event_type"] == "assessment.finished"][0]["cost_usd"] == Decimal("0.049048")
    assert all(r["event_key"].startswith("e-1|") and r["occurred_at"].tzinfo for r in rows)


# ───────── documentation agrees ─────────
def test_the_documentation_matches_the_design():
    section = DOC[DOC.index("## 8."):DOC.index("## 9.")]
    for t in EVENT_TYPES:
        assert t in section, t
    for k in DETAIL_KEYS:
        assert k in section, k
    for c in INSERT_COLUMNS:
        assert c in section, c
    assert "sql/drafts/002_ops_events.sql" in section and "G-OPS" in section
    mentions = [ln for ln in DOC.splitlines() if "assessment.rejected" in ln]
    assert all(("emoved" in ln or "emoval" in ln) for ln in mentions), mentions                                     # only ever mentioned as REMOVED
    assert "assessment.rejected" not in (ROOT / "docs" / "api-specification.md").read_text(encoding="utf-8")
