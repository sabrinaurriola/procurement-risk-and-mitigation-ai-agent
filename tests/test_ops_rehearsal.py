"""Offline tests of the rehearsal harness (src/db/ops_rehearsal.py) against a scripted FAKE PostgreSQL. Nothing here connects to any database:
psycopg.connect is replaced by a function that fails the test, and every connection the harness opens comes from the fake.

What this proves: the harness's own logic (transaction ownership, savepoints, identity handling, abort/stop rules, independent verification,
output hygiene). What it does NOT prove: that real PostgreSQL behaves like the fake. That is the point of the approved rehearsal later."""
from __future__ import annotations

import inspect
import io
import re
import types
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

import src.db.ops_rehearsal as rh
from src.db.ops_cases import DB_EXPECT, DB_SKIP, INVALID, VALID
from src.db.sql_guard import Stmt, load_reviewed_migration

DRAFT = Path(rh.DRAFT)
MIGRATION = load_reviewed_migration(DRAFT.read_text(encoding="utf-8"))
IDLE, INTRANS, INERROR = 0, 2, 3
CASES = [(n, c, "valid") for n, c in VALID.items()] + [(n, c, "invalid") for n, c in INVALID.items() if n not in DB_SKIP]


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    import psycopg

    def boom(*a, **k):
        raise AssertionError("a test tried to open a real database connection")
    monkeypatch.setattr(psycopg, "connect", boom)
    monkeypatch.setattr(rh, "default_connect", boom)
    monkeypatch.delenv("RUN_DB_TESTS", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


# ───────────────────────────── the fake server ─────────────────────────────
def priv_name(v):
    return v if isinstance(v, str) else "SELECT"


def fn_args(sql, params):
    """Arguments of `SELECT fn(a, b, c)`: %s placeholders take the next parameter, quoted literals are taken from the text."""
    inner = re.search(r"\((.*)\)", sql).group(1)
    it = iter(params or ())
    return [next(it) if a.strip() == "%s" else a.strip().strip("'") for a in inner.split(",")]


class DbError(Exception):
    def __init__(self, sqlstate, constraint=None, message="driver text that must never be printed: password=hunter2 host=db.example"):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = types.SimpleNamespace(constraint_name=constraint)


class Cur:
    def __init__(self, rows=(), rowcount=-1):
        self.rows, self.rowcount = list(rows), rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Server:
    def __init__(self, **flags):
        self.persisted: set[str] = set(flags.pop("persisted", ()))
        self.flags = flags
        self.tables = [f"table_{i}" for i in range(flags.pop("n_tables", 10))] if "n_tables" in flags else [f"table_{i}" for i in range(10)]
        # every relation kind in public: table, partitioned table, view, materialized view, foreign table, sequence, plus an awkward identifier
        self.public_rels = [("Mixed_Case", "r"), ("notes", "r"), ("part", "p"), ("v_view", "v"), ("mv", "m"), ("ft", "f"), ("seq_a", "S"), ('we"ird', "r")]
        self.public_tables = [n for n, k in self.public_rels if k in ("r", "p", "v", "m")]
        self.conns: list[Conn] = []
        self.interrupted = False
        self.log: list[tuple[int, str, str, tuple]] = []        # (connection index, active user, sql, params)

    def connect(self):
        c = Conn(self, len(self.conns))
        self.conns.append(c)
        return c


class _Info:
    def __init__(self, conn):
        self._c = conn

    @property
    def transaction_status(self):
        return self._c.ts                                  # live view of the fake transaction state


class Conn:
    def __init__(self, server: Server, idx: int):
        self.s, self.idx = server, idx
        self.ts = IDLE
        self.user = "postgres"
        self.txn_objs: set[str] = set()
        self.keys: set[str] = set()
        self.stack: list[tuple[str, str, set[str]]] = []
        self.closed = False
        self.info = _Info(self)

    # ── driver surface ──
    def close(self):
        self.closed = True

    def execute(self, sql, params=None):
        assert not self.closed, "statement on a closed connection"
        self.s.log.append((self.idx, self.user, sql, tuple(params) if params is not None else ()))
        up = " ".join(sql.upper().split())
        f = self.s.flags
        if f.get("interrupt_at") and self.idx == 1 and up.startswith(f["interrupt_at"]) and not self.s.interrupted:
            self.s.interrupted = True                          # one Ctrl-C (or SystemExit) arrives while this statement runs
            self.ts = {"intrans": INTRANS, "inerror": INERROR, "unknown": 4, "idle": IDLE}[f.get("interrupt_state", "intrans")]
            raise f.get("interrupt_exc", KeyboardInterrupt)()
        if self.ts == INERROR and not up.startswith(("ROLLBACK",)):
            raise DbError("25P02")
        try:
            cur = self._do(sql, up, params)
        except DbError:
            if self.ts == INTRANS:
                self.ts = INERROR
            raise
        commit_after = self.s.flags.get("commit_after")
        if commit_after and up.startswith(commit_after) and self.ts == INTRANS:
            self.s.persisted |= self.txn_objs                    # a commit the harness did not ask for
            self.ts = IDLE
        return cur

    # ── behaviour ──
    def visible(self):
        return self.s.persisted | self.txn_objs

    def _do(self, sql, up, params):
        f = self.s.flags
        if up == "BEGIN":
            self.ts = INTRANS
            return Cur()
        if up == "COMMIT":
            self.s.persisted |= self.txn_objs
            self.txn_objs.clear()
            self.ts = IDLE
            return Cur()
        if up.startswith("ROLLBACK TO SAVEPOINT"):
            if f.get("rollback_to_fails"):
                raise DbError("XX000")
            name = sql.split()[-1]
            i = next(k for k, e in enumerate(self.stack) if e[0] == name)
            _, self.user, self.keys = self.stack[i][0], self.stack[i][1], set(self.stack[i][2])
            del self.stack[i + 1:]
            self.ts = INTRANS
            return Cur()
        if up == "ROLLBACK":
            if self.ts == 4 or f.get("rollback_fails"):
                raise DbError("08006")
            if f.get("survive_rollback"):
                self.s.persisted |= self.txn_objs
            self.txn_objs.clear()
            self.ts = IDLE
            return Cur()
        if up.startswith("SAVEPOINT"):
            self.stack.append((sql.split()[-1], self.user, set(self.keys)))
            return Cur()
        if up.startswith("RELEASE SAVEPOINT"):
            name = sql.split()[-1]
            self.stack = [e for e in self.stack if e[0] != name]
            return Cur()
        if up.startswith(("SET LOCAL", "SET DEFAULT_TRANSACTION")):
            return Cur()
        if up.startswith("SET ROLE"):
            if not f.get("ignore_set_role"):
                self.user = sql.split('"')[1]
            return Cur()
        if up == "RESET ROLE":
            self.user = "postgres"
            return Cur()
        if up == "SELECT CURRENT_USER, SESSION_USER":
            return Cur([(self.user, "postgres")])
        if self.user in (rh.ROLE, rh.PROBE):
            return self._restricted(sql, up, params)
        return self._owner(sql, up, params)

    def _owner(self, sql, up, params):
        f = self.s.flags
        if up.startswith("INSERT INTO OPS.OPERATIONAL_EVENTS"):
            return self._insert(sql, up, params, owner=True)
        if up.startswith("INSERT INTO PROCUREMENT.SCHEMA_MIGRATIONS") or "VALUES ('002')" in up:
            self.txn_objs.add("mig002")
        elif up.startswith("CREATE SCHEMA OPS"):
            self.txn_objs.add("schema")
        elif up.startswith("CREATE ROLE OPS_WRITER"):
            self.txn_objs.add("role")
        elif up.startswith("CREATE ROLE") and rh.PROBE.upper() in up:
            self.txn_objs.add("probe")
        elif up.startswith("CREATE TABLE OPS.OPERATIONAL_EVENTS"):
            self.txn_objs.add("table")
        elif up == "SELECT CURRENT_SETTING('SERVER_VERSION_NUM')":
            return Cur([(str(f.get("version", 170011)),)])
        elif "SELECT COUNT(*) FROM OPS.OPERATIONAL_EVENTS" in up:
            return Cur([(len(self.keys),)])
        elif up.startswith("CREATE TABLE PUBLIC.REHEARSAL_PUBLIC_"):
            if f.get("public_create_fails"):
                raise DbError("42501")
        elif up == "SELECT CURRENT_SETTING('STANDARD_CONFORMING_STRINGS')":
            return Cur([(f.get("scs", "on"),)])
        elif up.startswith("SELECT TO_REGCLASS(%S) IS NOT NULL"):
            return Cur([(not f.get("public_scratch_missing"),)])
        elif "FROM PG_CLASS C JOIN PG_NAMESPACE N" in up and "N.NSPNAME = 'PUBLIC'" in up:
            return Cur([r for r in self.s.public_rels if r[0] != params[0]])
        elif "FROM PG_CLASS C JOIN PG_NAMESPACE N" in up and "N.NSPNAME = 'PROCUREMENT'" in up:
            return Cur([(t, "r") for t in self.s.tables] + f.get("procurement_extra", []))
        elif "FROM PG_CLASS C JOIN PG_NAMESPACE N" in up and "C.RELKIND = 'S'" in up:
            return Cur(f.get("global_seq_offenders", []))
        elif "FROM PG_CLASS C JOIN PG_NAMESPACE N" in up:
            return Cur(f.get("global_offenders", []))
        elif up.startswith("SELECT N.NSPNAME FROM PG_NAMESPACE N"):
            return Cur([(n,) for n in f.get("create_schemas", [])])
        elif up.startswith("SELECT HAS_SEQUENCE_PRIVILEGE"):
            role, _, priv = fn_args(sql, params)
            return Cur([(role == rh.ROLE and priv in f.get("seq_priv", ()),)])
        elif up.startswith("SELECT HAS_ANY_COLUMN_PRIVILEGE"):
            return Cur([(bool(f.get("column_priv")) and params[1].startswith("public.") and params[1].endswith(f.get("column_priv")),)])
        elif up.startswith("SELECT TABLENAME FROM PG_TABLES"):
            return Cur([(t,) for t in self.s.tables])
        elif up.startswith("SELECT COUNT(*) FROM PROCUREMENT.SCHEMA_MIGRATIONS WHERE VERSION"):
            return Cur([(1 if "mig002" in self.visible() else 0,)])
        elif up.startswith("SELECT COUNT(*) FROM PROCUREMENT.SCHEMA_MIGRATIONS"):
            return Cur([(1 + ("mig002" in self.visible()),)])
        elif up.startswith("SELECT COUNT(*) FROM PROCUREMENT."):
            return Cur([(5,)])
        elif "TO_REGNAMESPACE" in up:
            return Cur([("schema" not in self.visible(),)])
        elif "TO_REGCLASS('OPS.OPERATIONAL_EVENTS') IS NULL" in up:
            return Cur([("table" not in self.visible(),)])
        elif "FROM PG_ROLES WHERE ROLNAME = ANY" in up:
            names = params[0]
            if rh.ROLE in names:
                return Cur([(n,) for n, o in ((rh.ROLE, "role"), (rh.PROBE, "probe")) if o in self.visible()])
            return Cur([(n,) for n in names])
        elif "FROM PG_ROLES WHERE ROLNAME = %S" in up:
            if "ROLBYPASSRLS, ROLSUPER" in up:
                return Cur([(bool(f.get("probe_bypass")), False)])
            return Cur([f.get("writer_flags", (False, False, False, False, False, False, True))])
        elif "FROM PG_AUTH_MEMBERS" in up:
            return Cur([(0,)])
        elif up.startswith("SELECT HAS_TABLE_PRIVILEGE"):
            role, tbl, priv = fn_args(sql, params)
            if role == rh.ROLE and tbl.startswith("public.") and f.get("public_priv") and priv == "SELECT":
                return Cur([(True,)])
            if role == rh.ROLE and tbl.startswith("procurement.") and f.get("procurement_priv") and priv == priv_name(f["procurement_priv"]):
                return Cur([(True,)])
            if role == rh.ROLE and not tbl.startswith("ops."):
                return Cur([(False,)])
            if role == rh.ROLE:
                return Cur([(priv == "INSERT" or (priv == "SELECT" and bool(f.get("writer_has_select"))),)])
            return Cur([(role == rh.PROBE and priv in ("SELECT", "INSERT"),)])
        elif up.startswith("SELECT HAS_SCHEMA_PRIVILEGE"):
            role, schema, priv = fn_args(sql, params)
            return Cur([(role in (rh.ROLE, rh.PROBE) and schema == "ops" and priv == "USAGE",)])
        elif "RELROWSECURITY" in up:
            return Cur([(True, False)])
        elif "FROM PG_POLICY" in up:
            return Cur([("ops_writer_insert", "a", [rh.ROLE])])
        return Cur()

    def _insert(self, sql, up, params, owner):
        f = self.s.flags
        if not owner and "RETURNING" in up:
            raise DbError("42501")
        if not owner and "ON CONFLICT" in up and f.get("on_conflict_denied"):
            raise DbError("42501")
        cols = [c.strip() for c in re.search(r"\(([^)]*)\) VALUES", sql).group(1).split(",")]
        if f.get("non_db_exception"):
            raise ValueError("host=db.example password=hunter2")
        row = {c: (p.obj if isinstance(p, Jsonb) else p) for c, p in zip(cols, params)}
        name, kind = next(((n, k) for n, c, k in CASES if c == row), (None, None))
        if kind == "invalid" and name not in f.get("accept_invalid", ()):
            sqlstate, cons = DB_EXPECT[name]
            raise DbError(sqlstate, f.get("wrong_constraint", {}).get(name) or (sorted(cons)[0] if cons else None))
        key = row["event_key"]
        if key in self.keys:
            if "ON CONFLICT DO NOTHING" in up:
                return Cur(rowcount=0)
            raise DbError("23505", "operational_events_event_key_uq")
        self.keys.add(key)
        return Cur(rowcount=1)

    def _restricted(self, sql, up, params):
        f = self.s.flags
        if up.startswith("INSERT INTO OPS.OPERATIONAL_EVENTS"):
            if self.user == rh.PROBE:
                raise DbError("42501")
            return self._insert(sql, up, params, owner=False)
        if self.user == rh.PROBE and up == "SELECT COUNT(*) FROM OPS.OPERATIONAL_EVENTS":
            return Cur([(1 if f.get("probe_sees_rows") else 0,)])
        if f.get("allow_create_public") and up.startswith("CREATE TABLE PUBLIC."):
            return Cur()
        if "PUBLIC.REHEARSAL_PUBLIC_" in up and f.get("public_denial_sqlstate"):
            raise DbError(f["public_denial_sqlstate"])
        raise DbError("42501")


def run(server: Server, migration=MIGRATION) -> rh.Report:
    return rh.execute_rehearsal(server.connect, migration)


def stmts(server: Server, idx: int):
    return [(u, s, p) for i, u, s, p in server.log if i == idx]


def first_word(sql: str) -> str:
    return re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.S).strip().split(None, 1)[0].upper()


# ───────────────────────────── happy path ─────────────────────────────
def test_clean_run_passes_rolls_back_and_verifies_on_a_different_connection():
    s = Server()
    rep = run(s)
    failed = [c for c in rep.checks if not c.ok]
    assert not failed and not rep.refused and not rep.possible_commit and rep.exit_code == rh.EXIT_OK
    assert len(rep.checks) > 100 and all(c.ok for c in rep.verification) and len(rep.verification) == 7
    assert s.persisted == set()                                                                   # nothing survived
    assert len(s.conns) == 3 and all(c.closed for c in s.conns) and [c.ts for c in s.conns] == [IDLE, IDLE, IDLE]


def test_the_run_owns_one_transaction_begin_first_rollback_last_and_never_commits():
    s = Server()
    run(s)
    b = stmts(s, 1)
    assert first_word(b[0][1]) == "BEGIN" and b[-1][1] == "ROLLBACK"
    assert [x for x in b if x[1] in ("BEGIN", "ROLLBACK")] == [b[0], b[-1]]                      # exactly one of each
    assert not any(re.search(r"\bCOMMIT\b|\bPREPARE\b|\bEND\b;?$", sql, re.I) for _, sql, _ in b if first_word(sql) not in ("DO",))
    assert not any("COMMIT" in sql.upper() for _, sql, _ in b)
    assert {first_word(sql) for _, sql, _ in b} <= {"BEGIN", "SET", "SELECT", "SAVEPOINT", "ROLLBACK", "RELEASE", "GRANT", "CREATE", "ALTER", "REVOKE", "DO", "INSERT",
                                                    "UPDATE", "DELETE", "TRUNCATE", "DROP", "RESET"}


def test_the_draft_wrappers_are_not_executed_and_the_body_is_run_statement_by_statement():
    s = Server()
    run(s)
    b = [sql for _, sql, _ in stmts(s, 1)]
    for st in MIGRATION.body:
        assert st.raw in b
    assert b.count("BEGIN") == 1 and "COMMIT" not in b


def test_independent_connections_do_the_pre_check_and_the_verification_read_only():
    s = Server()
    run(s)
    for idx in (0, 2):
        words = {first_word(sql) for _, sql, _ in stmts(s, idx)}
        assert words <= {"SET", "SELECT"} and "BEGIN" not in words
        assert stmts(s, idx)[0][1].startswith("SET default_transaction_read_only")
    assert stmts(s, 0) and stmts(s, 1) and stmts(s, 2)
    last_a = max(i for i, (c, *_r) in enumerate(s.log) if c == 0)
    first_b = min(i for i, (c, *_r) in enumerate(s.log) if c == 1)
    last_b = max(i for i, (c, *_r) in enumerate(s.log) if c == 1)
    first_c = min(i for i, (c, *_r) in enumerate(s.log) if c == 2)
    assert last_a < first_b and last_b < first_c                                                   # A, then B, then C: never interleaved


def test_every_expected_failure_runs_inside_a_savepoint_and_is_rolled_back():
    s = Server()
    run(s)
    b = stmts(s, 1)
    keep = "rehearsal_public_create"                      # the one savepoint that guards creating the public scratch table: released, kept on success
    sp = [sql.split()[-1] for _, sql, _ in b if sql.startswith("SAVEPOINT") and not sql.endswith(keep)]
    rb = [sql.split()[-1] for _, sql, _ in b if sql.startswith("ROLLBACK TO SAVEPOINT")]
    rel = [sql.split()[-1] for _, sql, _ in b if sql.startswith("RELEASE SAVEPOINT") and not sql.endswith(keep)]
    assert [sql for _, sql, _ in b if sql.endswith(keep)] == [f"SAVEPOINT {keep}", f"RELEASE SAVEPOINT {keep}"]
    assert sp and sorted(sp) == sorted(rb) == sorted(rel) and len(set(sp)) == len(sp)
    for i, (_, sql, _) in enumerate(b):                                                            # every tested INSERT is directly after a SAVEPOINT or another tested statement
        if sql.startswith("INSERT INTO ops.operational_events"):
            prior = [x[1] for x in b[:i] if x[1].startswith(("SAVEPOINT", "ROLLBACK TO", "RELEASE"))]
            assert prior and prior[-1].startswith("SAVEPOINT")


def test_set_role_is_always_followed_by_an_identity_check_and_restored():
    s = Server()
    run(s)
    b = stmts(s, 1)
    for i, (_, sql, _) in enumerate(b):
        if sql.startswith(("SET ROLE", "RESET ROLE")):
            assert b[i + 1][1] == "SELECT current_user, session_user"
    resets = [i for i, (_, sql, _) in enumerate(b) if sql == "RESET ROLE"]
    sets = [i for i, (_, sql, _) in enumerate(b) if sql.startswith("SET ROLE")]
    assert len(resets) == len(sets) == 4                                                           # writer, forbidden, public (scratch) and probe phases
    assert b[-1][0] == "postgres"


def test_all_cases_all_tables_and_all_expected_checks_are_exercised():
    s = Server()
    rep = run(s)
    names = {c.name for c in rep.checks}
    for n in VALID:
        assert f"valid row inserts: {n}" in names
    for n in INVALID:
        if n not in DB_SKIP:
            assert f"invalid row rejected: {n}" in names
    assert f"invalid row rejected: unknown_field" not in names
    for i in range(10):
        assert f"ops_writer holds no privilege on procurement.table_{i} (table)" in names
    assert not any("denied SELECT on procurement" in n or "denied INSERT into procurement" in n for n in names)
    for needle in ("plain duplicate event_key fails 23505", "ON CONFLICT DO NOTHING", "INSERT ... RETURNING is denied", "ops_writer denied on events: SELECT",
                   "ops_writer denied on events: UPDATE", "ops_writer denied on events: DELETE", "ops_writer denied on events: TRUNCATE",
                   "ops_writer denied on existing scratch table: DROP TABLE", "ops_writer denied: CREATE TABLE in procurement", "row level security enabled",
                   "exactly one policy", "probe role has USAGE", "probe role has no BYPASSRLS", "probe with SELECT privilege sees 0 rows",
                   "probe with INSERT privilege is denied", "events table is empty before rollback", "ops_writer table privileges = INSERT only"):
        assert any(needle in n for n in names), needle
    writer_inserts = [p for _, u, sql, p in s.log if u == rh.ROLE and sql.startswith("INSERT INTO ops.operational_events")]
    assert len(writer_inserts) >= len(CASES)


def test_ddl_denial_checks_target_a_uniquely_named_scratch_table_that_exists_first():
    s1, s2 = Server(), Server()
    run(s1)
    run(s2)
    b = [sql for _, sql, _ in stmts(s1, 1)]
    creates = [i for i, sql in enumerate(b) if re.match(r"CREATE TABLE ops\.rehearsal_scratch_[0-9a-f]{8} ", sql)]
    drops = [i for i, sql in enumerate(b) if re.match(r"DROP TABLE ops\.rehearsal_scratch_[0-9a-f]{8}$", sql)]
    assert len(creates) == 1 and len(drops) == 1 and creates[0] < drops[0]
    name1 = re.search(r"rehearsal_scratch_[0-9a-f]{8}", b[creates[0]]).group(0)
    name2 = re.search(r"rehearsal_scratch_[0-9a-f]{8}", next(sql for _, sql, _ in stmts(s2, 1) if "rehearsal_scratch_" in sql)).group(0)
    assert name1 != name2


# ───────────────────────────── P1 regression: public-schema audit ─────────────────────────────
def test_public_audit_uses_a_real_scratch_table_and_checks_every_existing_public_table():
    s = Server()
    rep = run(s)
    names = [c.name for c in rep.checks]
    assert rep.exit_code == rh.EXIT_OK
    for needle in ("public scratch table exists", "ops_writer CREATE on schema public: no", "no privilege on the new public scratch table",
                   "no privilege on any existing public table"):
        assert any(needle in n for n in names), needle
    for op in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER TABLE", "DROP TABLE"):
        assert f"ops_writer denied on real public scratch table: {op}" in names
    assert not any("existing public." in n and "denied" in n for n in names)                      # no live denial test on any existing public relation
    b = [(u, sql) for u, sql, _ in stmts(s, 1)]
    create = next(i for i, (_, sql) in enumerate(b) if re.match(r"CREATE TABLE public\.rehearsal_public_[0-9a-f]{8} ", sql))
    first_denial = next(i for i, (u, sql) in enumerate(b) if u == rh.ROLE and "rehearsal_public_" in sql)
    exists = next(i for i, (_, sql) in enumerate(b) if sql.startswith("SELECT to_regclass(%s) IS NOT NULL"))
    privs = [i for i, (_, sql) in enumerate(b) if sql.startswith("SELECT has_table_privilege") and i > create]
    assert create < exists < first_denial and privs
    assert all(u == "postgres" for u, sql in b[create:create + 1])                                  # created by the owner, never by ops_writer


def test_the_public_audit_grants_nothing():
    s = Server()
    run(s)
    grants = [sql for _, sql, _ in stmts(s, 1) if first_word(sql) in ("GRANT", "REVOKE")]
    assert grants and not any(re.search(r"\bpublic\.|SCHEMA public|TO PUBLIC", g, re.I) for g in grants)         # (REVOKE ... FROM PUBLIC is the reviewed revoke)
    assert all(re.search(r'ops|"ops_writer"|"ops_rls_probe"|PUBLIC;?$', g) for g in grants)


def test_if_the_public_scratch_table_cannot_be_created_the_run_fails_closed_and_says_so():
    s = Server(public_create_fails=True)
    rep = run(s)
    bad = [c for c in rep.checks if not c.ok]
    assert [c.name for c in bad] == ["public scratch table could be created by the migration identity", "rehearsal aborted"]
    assert "NOT performed" in bad[0].info and "limitation" in bad[0].info and bad[0].info.startswith("42501")
    assert not any("real public scratch table" in c.name for c in rep.checks)                      # nothing was silently skipped-as-passed
    assert rep.exit_code == rh.EXIT_CHECK_FAILED and [sql for _, sql, _ in stmts(s, 1)][-1] == "ROLLBACK" and s.persisted == set()
    assert not any("'procurement'" in sql and "pg_class" in sql for _, sql, _ in stmts(s, 1)[-5:])    # stopped before the later phases


@pytest.mark.parametrize("flags,needle", [({"public_priv": True}, "no privilege on any existing public table"),
                                          ({"public_scratch_missing": True}, "public scratch table exists"),
                                          ({"public_denial_sqlstate": "42P01"}, "denied on real public scratch table: SELECT")])
def test_public_audit_failures_are_detected(flags, needle):
    rep = run(Server(**flags))
    assert rep.exit_code == rh.EXIT_CHECK_FAILED and any((not c.ok) and needle in c.name for c in rep.checks)
    assert all(c.ok for c in rep.verification)


# ───────────────────────────── procurement: catalog-only, like public ─────────────────────────────
def test_no_sql_is_executed_against_any_existing_procurement_relation():
    s = Server()
    rep = run(s)
    body = {st.raw for st in MIGRATION.body}
    for u, sql, p in stmts(s, 1):
        if sql in body:
            continue                                                                   # the reviewed migration's own version record is the only exception
        sql = re.sub(r"procurement\.rehearsal_new_\w+", "<harness-created name>", sql)           # CREATE TABLE of a name that does not exist: denial test only
        assert not re.search(r"(FROM|INTO|UPDATE|TABLE|TRUNCATE|JOIN)\s+procurement\.", sql, re.I), sql
        if u == rh.ROLE:
            assert "procurement" not in sql.lower(), sql
    assert not any(u == rh.ROLE and "procurement" in sql.lower() for u, sql, _ in stmts(s, 1) if "CREATE TABLE" not in sql)
    names = [c.name for c in rep.checks]
    assert sum(n.startswith("ops_writer holds no privilege on procurement.") for n in names) == 10


def test_procurement_relations_of_every_kind_are_audited_from_the_catalog():
    s = Server(procurement_extra=[("v_x", "v"), ("seq_x", "S"), ("mv_x", "m"), ("ft_x", "f"), ("part_x", "p")])
    rep = run(s)
    names = [c.name for c in rep.checks]
    for n, k in (("v_x", "view"), ("seq_x", "sequence"), ("mv_x", "materialized view"), ("ft_x", "foreign table"), ("part_x", "partitioned table")):
        assert f"ops_writer holds no privilege on procurement.{n} ({k})" in names
    calls = [(sql, p) for _, sql, p in stmts(s, 1) if p and len(p) == 3 and p[1] == "procurement.\"seq_x\""]
    assert [p[2] for _, p in calls] == ["USAGE", "SELECT", "UPDATE"] and all(sql.startswith("SELECT has_sequence_privilege") for sql, _ in calls)
    assert rep.exit_code == rh.EXIT_OK


@pytest.mark.parametrize("flags", [{"procurement_priv": "SELECT"}, {"procurement_priv": "INSERT"}, {"procurement_priv": "TRIGGER"}])
def test_a_privilege_on_a_procurement_table_fails_the_rehearsal_and_names_the_table(flags):
    rep = run(Server(**flags))
    bad = [c for c in rep.checks if not c.ok]
    assert rep.exit_code == rh.EXIT_CHECK_FAILED and len(bad) == 10 and all(c.info == flags["procurement_priv"] for c in bad)
    assert all(c.name.startswith("ops_writer holds no privilege on procurement.table_") for c in bad)
    assert all(c.ok for c in rep.verification)


def test_a_procurement_view_with_a_privilege_is_reported_by_kind():
    rep = run(Server(procurement_extra=[("v_x", "v")], procurement_priv="SELECT"))
    assert any((not c.ok) and c.name == "ops_writer holds no privilege on procurement.v_x (view)" for c in rep.checks)


# ───────────────────────────── P1 regression: full relation-kind and sequence coverage ─────────────────────────────
def test_the_public_inventory_covers_every_relation_kind_from_the_catalog_not_pg_tables():
    s = Server()
    rep = run(s)
    b = stmts(s, 1)
    inv = next(sql for _, sql, _ in b if "FROM pg_class c JOIN pg_namespace n" in sql and "nspname = 'public'" in sql)
    assert "relkind IN ('r','p','v','m','f','S')" in inv and "pg_tables" not in inv
    assert not any("pg_tables" in sql and "'public'" in sql for _, sql, _ in b)
    info = next(c.info for c in rep.checks if c.name.startswith("ops_writer holds no privilege on any existing public table, partitioned"))
    for kind in ("table=3", "partitioned table=1", "view=1", "materialized view=1", "foreign table=1", "sequence=1"):
        assert kind in info, kind
    assert "8 relations" in info


def test_every_relation_gets_the_right_privilege_probes_with_safe_quoting_and_no_state_changing_calls():
    s = Server()
    run(s)
    b = [(u, sql, p) for u, sql, p in stmts(s, 1)]
    for name, kind in s.public_rels:
        q = f'public."{name.replace(chr(34), chr(34) * 2)}"'
        t_calls = [p[2] for _, sql, p in b if sql.startswith("SELECT has_table_privilege") and len(p) == 3 and p[1] == q]
        s_calls = [p[2] for _, sql, p in b if sql.startswith("SELECT has_sequence_privilege") and p[1] == q]
        col = [1 for _, sql, p in b if sql.startswith("SELECT has_any_column_privilege") and p[1] == q]
        if kind == "S":
            assert s_calls == ["USAGE", "SELECT", "UPDATE"] and not t_calls and not col                  # sequences: USAGE, SELECT and UPDATE, explicitly
        else:
            assert t_calls == list(rh.TABLE_PRIVS) and len(col) == 1 and not s_calls
    sql_all = " ".join(sql.lower() for _, sql, _ in b)
    assert not re.search(r"\b(nextval|currval|setval|lastval)\b", sql_all)
    # No live SQL against any existing public relation: their names appear only as PARAMETERS of the catalog privilege functions, never in statement text.
    for name, _kind in s.public_rels:
        for u, sql, p in b:
            esc = name.replace('"', '""')
            assert f'public."{esc}"' not in sql and f"public.{name}" not in sql, (name, sql)
    # The only statements that name public.<something> in their text are the harness's own scratch table.
    for u, sql, _ in b:
        for m in re.findall(r"public\.(\w+)", sql):
            assert m.startswith("rehearsal_public_") or m.startswith("rehearsal_new_"), sql


def test_live_statements_run_as_ops_writer_never_touch_existing_public_relations_in_the_source_either():
    src = inspect.getsource(rh.Rehearsal.public_checks)
    assert "LIMIT 1" not in src and not re.search(r"SELECT 1 FROM public", src) and "for name, kind in rels:" in src
    assert not re.search(r"expect_denied\([^)]*rels", src)
    live_lines = [ln for ln in src.splitlines() if "expect_denied" in ln or "self.attempt" in ln]
    assert all("scratch" in ln or "label" in ln for ln in live_lines)                                   # only the scratch table / its op list


@pytest.mark.parametrize("flags,needle", [({"seq_priv": ("USAGE",)}, "USAGE, SELECT or UPDATE on any existing public sequence"),
                                          ({"seq_priv": ("SELECT",)}, "USAGE, SELECT or UPDATE on any existing public sequence"),
                                          ({"seq_priv": ("UPDATE",)}, "USAGE, SELECT or UPDATE on any existing public sequence"),
                                          ({"column_priv": '"notes"'}, "on any existing public table, partitioned table"),
                                          ({"global_offenders": [("auth", "users", "r")]}, "outside ops.operational_events"),
                                          ({"global_offenders": [("extensions", "pg_stat_statements", "v")]}, "outside ops.operational_events"),
                                          ({"global_seq_offenders": [("storage", "s1", "S")]}, "outside ops.operational_events"),
                                          ({"create_schemas": ["scratchy"]}, "CREATE on no schema")])
def test_unauthorized_privileges_on_any_relation_kind_fail_the_rehearsal(flags, needle):
    rep = run(Server(**flags))
    bad = [c for c in rep.checks if not c.ok]
    assert rep.exit_code == rh.EXIT_CHECK_FAILED and len(bad) == 1 and needle in bad[0].name
    assert all(c.ok for c in rep.verification) and bad[0].info not in ("", "none")


def test_offenders_are_named_with_their_kind_so_a_reviewer_can_judge_them():
    rep = run(Server(seq_priv=("USAGE", "UPDATE"), global_offenders=[("extensions", "pg_stat_statements", "v")]))
    infos = " | ".join(c.info for c in rep.checks if not c.ok)
    assert "seq_a[sequence]: USAGE,UPDATE" in infos and "extensions.pg_stat_statements[view]" in infos


def test_a_denial_for_the_wrong_reason_is_not_accepted_as_a_denial():
    rep = run(Server(public_denial_sqlstate="42P01"))
    bad = [c for c in rep.checks if not c.ok]
    assert bad and all(c.info == "got 42P01" for c in bad) and len(bad) == 7                         # every operation on the 'missing' object fails the check


def test_standard_conforming_strings_off_refuses_to_start():
    s = Server(scs="off")
    rep = run(s)
    assert rep.exit_code == rh.EXIT_REFUSED and any("standard_conforming_strings" in r for r in rep.refused) and len(s.conns) == 1


def test_rls_probe_is_granted_and_verified_before_any_denial_is_tested():
    s = Server()
    run(s)
    b = stmts(s, 1)
    sqls = [sql for _, sql, _ in b]
    grant_usage = next(i for i, x in enumerate(sqls) if x.startswith("GRANT USAGE ON SCHEMA ops TO"))
    grant_table = next(i for i, x in enumerate(sqls) if x.startswith("GRANT SELECT, INSERT ON ops.operational_events TO"))
    verify = next(i for i, (_, sql, p) in enumerate(b) if sql.startswith("SELECT has_table_privilege") and p[0] == rh.PROBE)
    flags = next(i for i, x in enumerate(sqls) if "rolbypassrls, rolsuper" in x)
    first_probe = next(i for i, (u, *_r) in enumerate(b) if u == rh.PROBE)
    assert grant_usage < grant_table < verify < first_probe and flags < first_probe
    assert any("NOBYPASSRLS" in x and "NOSUPERUSER" in x for x in sqls if x.startswith('CREATE ROLE "ops_rls_probe"'))


# ───────────────────────────── failure handling ─────────────────────────────
def test_an_unexpected_commit_stops_the_run_and_never_claims_a_rollback():
    s = Server(commit_after="CREATE INDEX")
    rep = run(s)
    assert rep.possible_commit and rep.exit_code == rh.EXIT_COMMITTED
    text = " ".join(rep.notes)
    assert "POSSIBLE PERSISTENT CHANGES" in text and "cannot undo a commit" in text and "NOT a clean bill of health" in text
    b = [sql for _, sql, _ in stmts(s, 1)]
    assert b[-1].strip().startswith("CREATE INDEX") and "ROLLBACK" not in b                             # stopped at once; did not "roll back" a committed transaction
    assert not any(x.startswith("SET ROLE") for x in b)
    assert rep.verification and any(not c.ok for c in rep.verification)                          # the read-only observation shows what persisted
    buf = io.StringIO()
    rh.print_report(rep, buf)
    assert "POSSIBLE PERSISTENT CHANGES" in buf.getvalue() and "ROLLED BACK AND VERIFIED CLEAN" not in buf.getvalue()


def test_a_commit_hidden_in_the_body_is_caught_even_if_the_guard_were_bypassed():
    sneaky = rh.Migration(body=(Stmt(raw="CREATE SCHEMA ops", code="CREATE SCHEMA ops", skeleton="CREATE SCHEMA ops"),
                                Stmt(raw="COMMIT", code="COMMIT", skeleton="COMMIT"),
                                Stmt(raw="CREATE ROLE ops_writer LOGIN", code="x", skeleton="CREATE ROLE ops_writer LOGIN")), digest="0" * 64)
    s = Server()
    rep = run(s, sneaky)
    assert rep.possible_commit and rep.exit_code == rh.EXIT_COMMITTED and "schema" in s.persisted
    assert "ROLLBACK" not in [sql for _, sql, _ in stmts(s, 1)]


def test_leftovers_after_rollback_make_the_result_not_clean():
    s = Server(survive_rollback=True)
    rep = run(s)
    assert rep.exit_code == rh.EXIT_NOT_CLEAN and not rep.possible_commit
    bad = {c.name for c in rep.verification if not c.ok}
    assert {"schema ops absent", "table ops.operational_events absent", "role ops_writer absent", "probe role absent", "migration 002 not recorded"} <= bad
    buf = io.StringIO()
    rh.print_report(rep, buf)
    assert "ROLLBACK VERIFICATION FAILED" in buf.getvalue()


@pytest.mark.parametrize("flags", [{"persisted": {"schema"}}, {"persisted": {"role"}}, {"persisted": {"probe"}}, {"persisted": {"mig002"}},
                                   {"persisted": {"table"}}, {"version": 150004}, {"n_tables": 9}, {"n_tables": 11}])
def test_the_harness_refuses_to_start_when_the_database_is_not_in_the_expected_state(flags):
    s = Server(**flags)
    rep = run(s)
    assert rep.refused and rep.exit_code == rh.EXIT_REFUSED and rep.checks == []
    assert len(s.conns) == 1 and not any(first_word(sql) in ("BEGIN", "CREATE") for _, _, sql, _ in s.log)      # never reached B, never began


def test_an_invalid_row_that_the_database_accepts_fails_the_rehearsal_but_is_still_rolled_back_and_clean():
    s = Server(accept_invalid={"bad_mode"})
    rep = run(s)
    assert rep.exit_code == rh.EXIT_CHECK_FAILED
    assert [c.name for c in rep.checks if not c.ok] == ["invalid row rejected: bad_mode"]
    assert next(c for c in rep.checks if c.name.endswith("bad_mode")).info == "ACCEPTED"
    assert s.persisted == set() and all(c.ok for c in rep.verification)


def test_a_row_rejected_for_the_wrong_reason_fails():
    s = Server(wrong_constraint={"bad_mode": "ck_severity"})
    rep = run(s)
    assert [c.name for c in rep.checks if not c.ok] == ["invalid row rejected: bad_mode"] and rep.exit_code == rh.EXIT_CHECK_FAILED


def test_a_disproved_on_conflict_assumption_is_reported_not_hidden():
    s = Server(on_conflict_denied=True)
    rep = run(s)
    bad = [c for c in rep.checks if not c.ok]
    assert len(bad) == 1 and "ON CONFLICT DO NOTHING" in bad[0].name and "23505" in bad[0].name and bad[0].info == "42501"


@pytest.mark.parametrize("flags,needle", [({"writer_has_select": True}, "table privileges = INSERT only"), ({"probe_bypass": True}, "probe role has no BYPASSRLS"),
                                          ({"probe_sees_rows": True}, "probe with SELECT privilege sees 0 rows"),
                                          ({"allow_create_public": True}, "denied: CREATE TABLE in public"),
                                          ({"writer_flags": (False, False, False, False, True, False, True)}, "role flags")])
def test_security_regressions_are_detected(flags, needle):
    rep = run(Server(**flags))
    assert rep.exit_code == rh.EXIT_CHECK_FAILED and any((not c.ok) and needle in c.name for c in rep.checks)


def test_identity_mismatch_aborts_and_rolls_back():
    s = Server(ignore_set_role=True)
    rep = run(s)
    assert any(c.name == "rehearsal aborted" and "identity mismatch" in c.info for c in rep.checks)
    assert rep.exit_code == rh.EXIT_CHECK_FAILED and [sql for _, sql, _ in stmts(s, 1)][-1] == "ROLLBACK" and s.persisted == set()


def test_a_failing_rollback_to_savepoint_aborts_the_run_and_the_transaction_is_rolled_back():
    s = Server(rollback_to_fails=True)
    rep = run(s)
    assert any(c.name == "rehearsal aborted" for c in rep.checks) and rep.exit_code == rh.EXIT_CHECK_FAILED
    assert [sql for _, sql, _ in stmts(s, 1)][-1] == "ROLLBACK" and s.persisted == set()


def test_a_non_database_exception_aborts_without_printing_its_text():
    s = Server(non_db_exception=True)
    rep = run(s)
    buf = io.StringIO()
    rh.print_report(rep, buf)
    out = buf.getvalue()
    assert "rehearsal aborted" in out and "hunter2" not in out and "db.example" not in out and "ValueError" in out
    assert [sql for _, sql, _ in stmts(s, 1)][-1] == "ROLLBACK"


def test_driver_messages_never_reach_the_output_even_when_a_check_fails():
    s = Server(accept_invalid=set(), on_conflict_denied=True)
    buf = io.StringIO()
    rh.print_report(run(s), buf)
    assert "hunter2" not in buf.getvalue() and "db.example" not in buf.getvalue()


def test_a_migration_statement_that_fails_is_recorded_and_the_transaction_rolled_back(monkeypatch):
    s = Server()
    orig = Conn._owner

    def failing(self, sql, up, params):
        if up.startswith("CREATE INDEX"):
            raise DbError("42P07")
        return orig(self, sql, up, params)
    monkeypatch.setattr(Conn, "_owner", failing)
    rep = run(s)
    assert any("migration statement" in c.name and not c.ok and c.info == "DbError sqlstate=42P07" for c in rep.checks)
    assert rep.exit_code == rh.EXIT_CHECK_FAILED and s.persisted == set() and [sql for _, sql, _ in stmts(s, 1)][-1] == "ROLLBACK"


def test_unavailable_verification_is_not_reported_as_clean():
    s = Server()
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("host=db.example password=hunter2")
        return s.connect()
    rep = rh.execute_rehearsal(connect, MIGRATION)
    assert rep.exit_code == rh.EXIT_NOT_CLEAN
    buf = io.StringIO()
    rh.print_report(rep, buf)
    assert "hunter2" not in buf.getvalue() and "OSError" in buf.getvalue()


# ───────────────────────────── interruption and cleanup ─────────────────────────────
@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("state", ["intrans", "inerror"])
def test_an_interrupt_mid_run_still_rolls_back_explicitly_and_verifies_independently(exc, state):
    s = Server(interrupt_at="CREATE INDEX", interrupt_state=state, interrupt_exc=exc)
    rep = run(s)                                                                                      # the interrupt does not escape: it is reported
    assert rep.interrupted == exc.__name__ and rep.exit_code == rh.EXIT_CHECK_FAILED and rep.rollback_confirmed and not rep.possible_commit
    b = [sql for _, sql, _ in stmts(s, 1)]
    assert b[-1] == "ROLLBACK" and s.persisted == set() and len(s.conns) == 3 and all(c.closed for c in s.conns)
    assert rep.verification and all(c.ok for c in rep.verification) and stmts(s, 2)                  # new connection verified
    assert any(c.name == "rehearsal interrupted" and not c.ok and c.info == exc.__name__ for c in rep.checks)
    buf = io.StringIO()
    rh.print_report(rep, buf)
    assert "INTERRUPTED" in buf.getvalue() and "ALL PASSED" not in buf.getvalue()


def test_an_interrupt_with_an_unusable_connection_does_not_claim_a_rollback():
    s = Server(interrupt_at="CREATE INDEX", interrupt_state="unknown")
    rep = run(s)
    assert not rep.rollback_confirmed and rep.interrupted == "KeyboardInterrupt" and rep.exit_code == rh.EXIT_CHECK_FAILED
    assert "ROLLBACK" not in [sql for _, sql, _ in stmts(s, 1)]                                       # nothing could be issued; nothing is claimed
    text = " ".join(rep.notes)
    assert "ROLLBACK NOT CONFIRMED" in text and "nothing is claimed until the independent verification" in text
    buf = io.StringIO()
    rh.print_report(rep, buf)
    assert "ROLLED BACK AND VERIFIED CLEAN" not in buf.getvalue()


def test_a_rollback_that_fails_after_an_interrupt_is_reported_as_unconfirmed():
    s = Server(interrupt_at="CREATE INDEX", rollback_fails=True)
    rep = run(s)
    assert not rep.rollback_confirmed and rep.exit_code == rh.EXIT_CHECK_FAILED and "ROLLBACK NOT CONFIRMED" in " ".join(rep.notes)


def test_an_interrupt_after_the_transaction_already_ended_is_still_reported_as_possible_persistent_changes():
    s = Server(interrupt_at="CREATE INDEX", interrupt_state="idle", commit_after="CREATE SCHEMA")
    rep = run(s)
    assert rep.possible_commit and rep.exit_code == rh.EXIT_COMMITTED                                 # the exit-code distinction is preserved
    assert "ROLLBACK" not in [sql for _, sql, _ in stmts(s, 1)] and "cannot undo a commit" in " ".join(rep.notes)


def test_a_leftover_after_an_interrupt_is_still_detected_by_the_independent_verification():
    s = Server(interrupt_at="CREATE INDEX", survive_rollback=True)
    rep = run(s)
    assert rep.exit_code == rh.EXIT_NOT_CLEAN and rep.interrupted == "KeyboardInterrupt"


def test_a_second_interrupt_during_verification_is_never_reported_as_clean():
    s = Server(interrupt_at="CREATE INDEX")
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt()
        return s.connect()
    rep = rh.execute_rehearsal(connect, MIGRATION)
    assert rep.exit_code == rh.EXIT_NOT_CLEAN and rep.interrupted == "KeyboardInterrupt"
    assert [sql for _, sql, _ in stmts(s, 1)][-1] == "ROLLBACK"
    buf = io.StringIO()
    rh.print_report(rep, buf)
    assert "VERIFICATION FAILED" in buf.getvalue() and "ROLLED BACK AND VERIFIED CLEAN" not in buf.getvalue()


def test_an_interrupt_during_the_pre_check_starts_nothing_and_exits_cleanly_from_the_cli():
    s = Server()

    def connect():
        c = s.connect()
        orig = c.execute

        def boom(sql, params=None):
            if "server_version_num" in sql:
                raise KeyboardInterrupt()
            return orig(sql, params)
        c.execute = boom
        return c
    out = io.StringIO()
    assert rh.main(["--execute"], connect=connect, out=out) == rh.EXIT_REFUSED
    assert "INTERRUPTED before any transaction" in out.getvalue()
    assert len(s.conns) == 1 and s.conns[0].closed and not any(first_word(sql) in ("BEGIN", "CREATE") for _, _, sql, _ in s.log)


def test_an_interrupt_during_begin_itself_still_gets_an_explicit_rollback():
    s = Server(interrupt_at="BEGIN")                                                                  # fake leaves the transaction open, as a real server may
    rep = run(s)
    assert rep.interrupted == "KeyboardInterrupt" and rep.rollback_confirmed and not rep.possible_commit
    assert [sql for _, sql, _ in stmts(s, 1)][-1] == "ROLLBACK" and all(c.ok for c in rep.verification)


def test_an_interrupt_during_begin_when_the_transaction_never_started_is_not_a_phantom_commit():
    s = Server(interrupt_at="BEGIN", interrupt_state="idle")
    rep = run(s)
    assert rep.interrupted == "KeyboardInterrupt" and rep.rollback_confirmed and not rep.possible_commit and rep.exit_code == rh.EXIT_CHECK_FAILED
    assert "ROLLBACK" not in [sql for _, sql, _ in stmts(s, 1)]


# ───────────────────────────── CLI and source hygiene ─────────────────────────────
def test_dry_run_never_connects_and_prints_the_plan():
    out = io.StringIO()

    def connect():
        raise AssertionError("dry run must not connect")
    assert rh.main([], connect=connect, out=out) == 0
    text = out.getvalue()
    assert "15 statements" in text and "DRY RUN" in text and "BEGIN" not in text.split("migration body")[1].split("cases:")[0].replace("BEGIN/COMMIT", "")


def test_execute_uses_only_the_injected_connection_factory_and_reports():
    s = Server()
    out = io.StringIO()
    assert rh.main(["--execute"], connect=s.connect, out=out) == 0
    assert "ALL PASSED, ROLLED BACK AND VERIFIED CLEAN" in out.getvalue() and len(s.conns) == 3


def test_a_file_with_a_mid_file_commit_is_refused_before_any_connection(tmp_path):
    bad = tmp_path / "bad.sql"
    bad.write_text(DRAFT.read_text(encoding="utf-8").replace("CREATE INDEX", "COMMIT;\nCREATE INDEX", 1), encoding="utf-8")
    out = io.StringIO()

    def connect():
        raise AssertionError("must not connect")
    assert rh.main(["--sql", str(bad), "--execute"], connect=connect, out=out) == rh.EXIT_REFUSED
    assert "GUARD REFUSED" in out.getvalue()


def test_connection_failure_output_has_no_url_or_driver_text(monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(rh, "default_connect", lambda: (_ for _ in ()).throw(OSError("postgresql://u:hunter2@db.example/postgres")))
    out = io.StringIO()
    assert rh.main(["--execute"], out=out) == rh.EXIT_REFUSED
    assert "hunter2" not in out.getvalue() and "postgresql://" not in out.getvalue() and "OSError" in out.getvalue()


def test_the_harness_source_never_issues_commit_or_prepares_a_transaction():
    src = inspect.getsource(rh)
    assert not re.search(r"""execute\(\s*f?["'](COMMIT|END|PREPARE)""", src)
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith(("#", '"""'))]
    assert not any(re.search(r"""["']\s*(COMMIT|PREPARE TRANSACTION|COMMIT PREPARED)\b""", ln) for ln in code_lines[30:] if "never" not in ln)


def test_documentation_describes_the_procedure_and_keeps_pooler_and_api_exposure_as_post_apply_gates():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "day4-architecture.md").read_text(encoding="utf-8")
    sec = doc[doc.index("### 8.4"):doc.index("### 8.5")]
    priv = doc[doc.index("### 8.3"):doc.index("### 8.4")]
    assert "does not independently guarantee that arbitrary nested text" in priv and "**Python only**" in priv and "PostgreSQL enforces a narrower set" in priv
    for needle in ("pg_class", "partitioned table, view, materialized view, foreign table and sequence", "USAGE, SELECT, UPDATE", "nextval/currval/setval",
                   "all non-system schemas", "KeyboardInterrupt", "ROLLBACK NOT CONFIRMED", "second interrupt", "REVIEWED_SEQUENCE", "public-schema audit", "fails closed", "standard_conforming_strings", "quote-aware",
                   "python -m src.db.ops_rehearsal", "sql_guard.py", "never issues `COMMIT`", "POSSIBLE PERSISTENT CHANGES", "uniquely named scratch table",
                   "NOBYPASSRLS", "independent verification", "Exit codes", "never run against any database", "WITH SET TRUE"):
        assert needle in sec or needle.lower() in sec.lower(), needle
    assert re.search(r"pooler login[^|\n]*\(\*\*P1\*\*\)|\(\*\*P1\*\*\)", sec) and "Supabase API exposure of schema `ops` (**P10**)" in sec
    assert re.search(r"\| P1 \| connect as `ops_writer` through the pooler", sec) and re.search(r"\| P10 \| Supabase API settings", sec)
    assert str(len(VALID)) in sec and str(len(INVALID) - len(DB_SKIP)) in sec and "10 `procurement` tables" in sec


def test_rehearsal_module_does_not_import_the_migration_runner_or_apply_anything():
    src = inspect.getsource(rh)
    assert "src.db.migrate" not in src and "autocommit=True" in src
    assert rh.DRAFT.parent.name == "drafts"
