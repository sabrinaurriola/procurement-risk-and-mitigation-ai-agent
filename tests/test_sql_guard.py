"""Offline tests of the fail-closed SQL guard (src/db/sql_guard.py). No database."""
from pathlib import Path

import pytest

from src.db.sql_guard import GuardError, REVIEWED_DO_BODY, check_body, load_reviewed_migration, scan, strip_wrappers

DRAFT = Path(__file__).resolve().parents[1] / "sql" / "drafts" / "002_ops_events.sql"
RAW = DRAFT.read_text(encoding="utf-8")


def wrap(*body: str) -> str:
    return "BEGIN;\n" + "\n".join(body) + "\nCOMMIT;\n"


GOOD = "CREATE SCHEMA ops;"


# ───────── lexer ─────────
def test_semicolons_and_keywords_in_comments_strings_and_dollar_quotes_do_not_split_or_trigger():
    sql = ("-- COMMIT; ROLLBACK;\n/* nested /* COMMIT; */ still comment; */\nSELECT 'COMMIT;', 'it''s; ROLLBACK;', \"commit;\", $$ COMMIT; $$, $t$ ROLLBACK; $t$;\n"
           "SELECT $1;")
    s = scan(sql)
    assert len(s) == 2 and s[0].skeleton == "SELECT '?', '?', \"?\", $?$, $?$" and s[1].skeleton == "SELECT $1"
    assert all(x.keyword == "SELECT" for x in s)


def test_statements_keep_their_exact_original_text():
    s = scan(RAW)
    assert all(x.raw in RAW for x in s)                                           # every statement is a verbatim slice of the file
    body = strip_wrappers(s)
    assert [x.raw for x in body] == [x.raw for x in s[1:-1]]                      # nothing rewritten, only the two wrappers dropped
    assert s[0].skeleton == "BEGIN" and s[-1].skeleton == "COMMIT"


@pytest.mark.parametrize("sql,frag", [
    ("SELECT E'a\\nb';", "prefixed string"), ("SELECT e'x';", "prefixed string"), ("SELECT U&'d\\0061t';", "prefixed string"),
    ("SELECT u&'x';", "prefixed string"), ("SELECT B'101';", "prefixed string"), ("SELECT X'1F';", "prefixed string"), ("SELECT N'x';", "prefixed string"),
    ('SELECT U&"d\\0061t";', "identifier"), ("SELECT 'abc;", "unterminated string"), ("SELECT /* x;", "unterminated block"),
    ("SELECT $$ x;", "unterminated dollar"), ('SELECT "x;', "unterminated quoted"), ("SELECT 1", "not terminated"),
    ("\\connect other", "backslash"), ("SELECT 1 \\gexec;", "backslash"),
])
def test_unsupported_or_malformed_syntax_is_rejected_not_guessed(sql, frag):
    with pytest.raises(GuardError, match=frag):
        scan(sql)


def test_harmless_quote_neighbours_are_accepted():
    assert len(scan("SELECT ('a'), ARRAY['x','y'], format('%I', r) FROM t WHERE c IN ('a') AND d ='b';")) == 1


# ───────── wrappers ─────────
def test_the_real_draft_loads_and_has_no_transaction_control_left():
    m = load_reviewed_migration(RAW)
    assert len(m.body) == 15                       # schema, role, 3 role settings, table, index, RLS, policy, 2 revokes, DO, 2 grants, version record
    assert not any(s.keyword in {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT"} for s in m.body)
    assert len(m.digest) == 64


def test_reviewed_do_body_constant_equals_the_draft():
    from src.db.sql_guard import _normalise_do_body
    do = [s for s in scan(RAW) if s.keyword == "DO"]
    assert len(do) == 1 and _normalise_do_body(do[0].code) == REVIEWED_DO_BODY


@pytest.mark.parametrize("sql", [
    "CREATE SCHEMA ops;\nCOMMIT;", "BEGIN;\nCREATE SCHEMA ops;", "BEGIN;\nCOMMIT;", "BEGIN TRANSACTION;\nCREATE SCHEMA ops;\nCOMMIT;",
    "BEGIN;\nCREATE SCHEMA ops;\nEND;", "BEGIN;\nCREATE SCHEMA ops;\nCOMMIT AND CHAIN;", "BEGIN;\nCREATE SCHEMA ops;\nCOMMIT;\nSELECT 1;",
    "BEGIN;\nBEGIN;\nCREATE SCHEMA ops;\nCOMMIT;", "SELECT 1;\nBEGIN;\nCREATE SCHEMA ops;\nCOMMIT;",
])
def test_only_exact_outer_wrappers_are_accepted(sql):
    with pytest.raises(GuardError):
        load_reviewed_migration(sql)


@pytest.mark.parametrize("stmt", [
    "COMMIT;", "ROLLBACK;", "SAVEPOINT x;", "RELEASE SAVEPOINT x;", "END;", "ABORT;", "BEGIN;", "START TRANSACTION;", "PREPARE TRANSACTION 'x';",
    "COMMIT PREPARED 'x';", "ROLLBACK PREPARED 'x';", "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;", "ROLLBACK AND CHAIN;",
    "commit ;", "-- hi\nCOMMIT;", "/* x */ rollback;", "ROLLBACK TO SAVEPOINT x;",
])
def test_transaction_control_in_the_body_is_rejected(stmt):
    with pytest.raises(GuardError):
        load_reviewed_migration(wrap(GOOD, stmt, "CREATE SCHEMA ops;"))


@pytest.mark.parametrize("stmt", [
    "SET ROLE postgres;", "RESET ROLE;", "SET SESSION AUTHORIZATION postgres;", "ALTER SYSTEM SET x = 1;", "CREATE DATABASE x;", "VACUUM;",
    "CREATE INDEX CONCURRENTLY ix ON ops.operational_events (request_id);", "CREATE SCHEMA other;", "CREATE ROLE other LOGIN;",
    "CREATE ROLE ops_writer LOGIN SUPERUSER;", "GRANT SELECT ON ops.operational_events TO ops_writer;", "GRANT INSERT ON ops.operational_events TO PUBLIC;",
    "GRANT INSERT ON procurement.suppliers TO ops_writer;", "GRANT ops_writer TO postgres;", "DROP SCHEMA procurement CASCADE;",
    "INSERT INTO procurement.suppliers (id) VALUES (1);", "INSERT INTO procurement.schema_migrations (version) VALUES ('003');",
    "UPDATE procurement.schema_migrations SET version = '002';", "ALTER TABLE procurement.suppliers DISABLE ROW LEVEL SECURITY;",
    "CREATE POLICY p ON ops.operational_events FOR SELECT TO ops_writer USING (true);", "ALTER ROLE ops_writer PASSWORD 'x';",
    "ALTER ROLE ops_writer SET search_path = procurement;", "COPY ops.operational_events FROM PROGRAM 'x';", "SELECT pg_sleep(1);",
    "TRUNCATE procurement.suppliers;", "CREATE TABLE ops.operational_events (id int REFERENCES procurement.suppliers (id));",
    "CREATE TABLE procurement.evil (id int);", "CREATE TABLE ops.operational_events (id int) INHERITS (procurement.suppliers);",
])
def test_statements_outside_the_reviewed_set_or_with_foreign_targets_are_rejected(stmt):
    with pytest.raises(GuardError):
        load_reviewed_migration(wrap(stmt))


@pytest.mark.parametrize("do", [
    "DO $$ BEGIN COMMIT; END $$;", "DO $$ BEGIN ROLLBACK; END $$;", "DO $x$ BEGIN PERFORM 1; END $x$;",
    "DO $$ BEGIN EXECUTE 'DROP SCHEMA procurement CASCADE'; END $$;", "DO $$ BEGIN PERFORM 1; END $$;",
])
def test_do_blocks_must_be_the_exact_reviewed_block(do):
    with pytest.raises(GuardError):
        load_reviewed_migration(wrap(do))


def mutate(old: str, new: str, count: int = 1) -> str:
    assert old in RAW, old
    return RAW.replace(old, new, count)


def test_the_reviewed_do_block_is_accepted_and_a_comment_inside_it_is_ignored():
    load_reviewed_migration(mutate("LOOP ", "LOOP -- extra comment; COMMIT\n"))
    load_reviewed_migration(mutate("FOREACH", "/* COMMIT; ROLLBACK; */ FOREACH"))


def test_look_alikes_in_comments_and_whitespace_changes_are_accepted_and_do_not_change_the_statement_count():
    sql = mutate("CREATE SCHEMA ops;", "-- COMMIT; ROLLBACK;\nCREATE   SCHEMA\n  ops; /* COMMIT; */")
    m = load_reviewed_migration(sql)
    assert len(m.body) == 15 and m.body[0].code == "CREATE SCHEMA ops"


# ───────── P1 regression: the complete reviewed sequence is pinned ─────────
def test_the_pinned_sequence_equals_the_real_draft_statement_by_statement():
    import hashlib
    from src.db.sql_guard import DO_PIN, REVIEWED_SEQUENCE
    body = strip_wrappers(scan(RAW))
    assert len(body) == len(REVIEWED_SEQUENCE) == 15
    for s, (label, pin) in zip(body, REVIEWED_SEQUENCE):
        assert (pin == DO_PIN) == (s.keyword == "DO") and (pin == DO_PIN or hashlib.sha256(s.code.encode()).hexdigest() == pin), label


@pytest.mark.parametrize("old,new", [
    # CREATE TABLE: any new column, default, expression, sequence, identity or constraint change must fail closed
    ("    detail            jsonb       NOT NULL DEFAULT '{}'::jsonb,", "    detail            jsonb       NOT NULL DEFAULT '{}'::jsonb,\n    extra integer,"),
    ("severity          text        NOT NULL DEFAULT 'info',", "severity          text        NOT NULL DEFAULT 'warning',"),
    ("severity          text        NOT NULL DEFAULT 'info',", "severity          text        NOT NULL DEFAULT nextval('ops.s'),"),
    ("recorded_at       timestamptz NOT NULL DEFAULT now(),", "recorded_at       timestamptz NOT NULL DEFAULT now() + interval '1 day',"),
    ("recorded_at       timestamptz NOT NULL DEFAULT now(),", "recorded_at       timestamptz NOT NULL DEFAULT (SELECT now()),"),
    ("event_key         text        NOT NULL,", "event_key         text        GENERATED ALWAYS AS IDENTITY,"),
    ("event_key         text        NOT NULL,", "event_key         text        NOT NULL DEFAULT 'x',"),
    ("event_id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),", "event_id          uuid        PRIMARY KEY DEFAULT public.fn(),"),
    ("event_id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),", "event_id          serial      PRIMARY KEY,"),
    ("CHECK (duration_ms >= 0)", "CHECK (duration_ms >= 0 OR true)"),
    ("CHECK (cost_usd >= 0)", "CHECK (cost_usd >= 1)"),
    ("'info', 'warning', 'error'", "'info', 'warning', 'error', 'debug'"),
    ("octet_length(detail::text) <= 4096", "octet_length(detail::text) <= 9096"),
    ("CONSTRAINT ck_duration CHECK (duration_ms >= 0),\n", ""),
    # other statements: same rule
    ("GRANT INSERT ON ops.operational_events TO ops_writer;", "GRANT INSERT ON ops.operational_events TO ops_writer;\nGRANT USAGE ON SCHEMA ops TO ops_writer;"),
    ("GRANT USAGE ON SCHEMA ops TO ops_writer;", ""),
    ("CREATE INDEX ix_operational_events_request ON ops.operational_events (request_id, occurred_at);",
     "CREATE INDEX ix_operational_events_request ON ops.operational_events (request_id);"),
    ("FOR INSERT TO ops_writer WITH CHECK (true)", "FOR INSERT TO ops_writer WITH CHECK (request_id <> 'x')"),
    ("ALTER ROLE ops_writer SET statement_timeout = '5s'", "ALTER ROLE ops_writer SET statement_timeout = '50s'"),
    ("create_does_not_exist", ""),
])
def test_any_change_to_the_reviewed_migration_fails_closed(old, new):
    if old == "create_does_not_exist":
        sql = RAW.replace("COMMIT;", "CREATE SCHEMA ops;\nCOMMIT;")                                      # an additional statement
    else:
        sql = mutate(old, new)
    with pytest.raises(GuardError):
        load_reviewed_migration(sql)


def test_reordering_two_reviewed_statements_fails_closed():
    a, b = "GRANT USAGE ON SCHEMA ops TO ops_writer;", "GRANT INSERT ON ops.operational_events TO ops_writer;"
    assert a in RAW and b in RAW
    swapped = RAW.replace(a, "@@A@@").replace(b, a).replace("@@A@@", b)
    with pytest.raises(GuardError, match="differs from the reviewed|expected"):
        load_reviewed_migration(swapped)


def test_a_changed_default_is_reported_by_the_default_audit_even_if_the_pin_is_updated(monkeypatch):
    import hashlib
    import src.db.sql_guard as g
    sql = mutate("DEFAULT 'info'", "DEFAULT 'evil'")
    body = strip_wrappers(scan(sql))
    pins = list(g.REVIEWED_SEQUENCE)
    pins[5] = (pins[5][0], hashlib.sha256(body[5].code.encode()).hexdigest())                          # a careless reviewer re-pins the new table
    monkeypatch.setattr(g, "REVIEWED_SEQUENCE", tuple(pins))
    with pytest.raises(GuardError, match="default outside the reviewed set"):
        load_reviewed_migration(sql)


def test_a_new_forbidden_table_clause_is_reported_even_if_the_pin_is_updated(monkeypatch):
    import hashlib
    import src.db.sql_guard as g
    sql = mutate("event_key         text        NOT NULL,", "event_key         text        NOT NULL GENERATED ALWAYS AS IDENTITY,")
    body = strip_wrappers(scan(sql))
    pins = list(g.REVIEWED_SEQUENCE)
    pins[5] = (pins[5][0], hashlib.sha256(body[5].code.encode()).hexdigest())
    monkeypatch.setattr(g, "REVIEWED_SEQUENCE", tuple(pins))
    with pytest.raises(GuardError, match="forbidden clause"):
        load_reviewed_migration(sql)


# ───────── P1 regression: backslashes and the quote-aware DO comparison ─────────
@pytest.mark.parametrize("sql", [r"SELECT 'a\b';", r"SELECT 'it\'s';", r'SELECT "a\b";', r"SELECT $$ a\b $$;", r"SELECT $t$ \n $t$;"])
def test_backslashes_inside_any_literal_are_rejected(sql):
    with pytest.raises(GuardError, match="backslash"):
        scan(sql)


def test_a_backslash_in_the_real_draft_is_rejected_wherever_it_hides():
    for old, new in (("'info'", r"'in\fo'"), ("'^[A-Z_]{1,64}$'", r"'^[A-Z\_]{1,64}$'"), ("FROM %I', r);", r"FROM %I\', r);")):
        with pytest.raises(GuardError, match="backslash"):
            load_reviewed_migration(mutate(old, new))
    load_reviewed_migration(mutate("CREATE SCHEMA ops;", "-- C:\\path\\in a comment is harmless\nCREATE SCHEMA ops;"))


def test_do_comment_stripping_is_quote_aware():
    from src.db.sql_guard import _normalise_do_body
    body = _normalise_do_body("DO $$ PERFORM 'a--b'; -- real comment\n PERFORM \"c--d\"; /* x */ PERFORM 1; $$")
    assert body == "PERFORM 'a--b'; PERFORM \"c--d\"; PERFORM 1;"
    with pytest.raises(GuardError, match="unterminated"):
        _normalise_do_body("DO $$ PERFORM 'open; $$")


def test_a_literal_containing_dashes_inside_the_do_block_is_not_treated_as_a_comment():
    # the naive strip(--[^\n]*) would turn this into text that can equal the reviewed body; the quote-aware strip keeps the literal, so it differs
    sql = mutate("EXECUTE format('REVOKE ALL ON SCHEMA ops FROM %I', r);", "EXECUTE format('REVOKE ALL ON SCHEMA ops FROM %I', r); PERFORM '-- x';")
    with pytest.raises(GuardError, match="DO block differs"):
        load_reviewed_migration(sql)


def test_check_body_requires_something_to_run():
    with pytest.raises(GuardError):
        check_body([])
