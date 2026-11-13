"""Rehearsal R of migration 002 (docs/day4-architecture.md section 8.4). NOT RUN YET: it needs the user's explicit approval.

`python -m src.db.ops_rehearsal`            dry run: parse + guard the draft, print the plan. Connects to nothing.
`python -m src.db.ops_rehearsal --execute`  connects with DATABASE_URL, runs the draft body and every security check inside ONE transaction
                                            that this harness owns, ROLLS IT BACK, then verifies on a NEW connection that nothing remains.

Safety design
* The draft's own BEGIN/COMMIT are removed by src/db/sql_guard.py (lexer-aware, fail-closed); the body is run inside the harness's BEGIN.
* The harness never issues COMMIT. After EVERY statement it checks the server's transaction status; "idle" in the middle of the run means the
  transaction ended (a commit we did not ask for). It then stops at once and reports POSSIBLE PERSISTENT CHANGES: a later ROLLBACK cannot undo a
  commit, so nothing is "rolled back" and manual review is required.
* Expected failures run inside SAVEPOINTs and are undone with ROLLBACK TO SAVEPOINT; the identity (current_user) is asserted after every change.
* Before: independent connection A (read-only) refuses to start if ops, ops_writer, the probe role or migration 002 already exist.
  After: independent connection C (read-only) verifies that none of them exist and that procurement row counts are unchanged.
* Output is limited to check names, SQLSTATE and constraint names: never URLs, hosts, passwords or driver messages.
Not proven here (post-apply gates): pooler login `ops_writer.<ref>` and role-level settings through the pooler (P1), Supabase API exposure of
schema ops (P10), and the real password."""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from psycopg.types.json import Jsonb

from src.api.events import INSERT_COLUMNS
from src.db.ops_cases import DB_EXPECT, DB_SKIP, INVALID, VALID
from src.db.sql_guard import GuardError, Migration, load_reviewed_migration

DRAFT = Path(__file__).resolve().parents[2] / "sql" / "drafts" / "002_ops_events.sql"
TABLE = "ops.operational_events"
ROLE, PROBE = "ops_writer", "ops_rls_probe"
API_ROLES = ("anon", "authenticated", "service_role")
EXPECTED_PROCUREMENT_TABLES = 10
VERSION = "002"
TS_IDLE, TS_INTRANS, TS_INERROR, TS_UNKNOWN = 0, 2, 3, 4            # psycopg.pq.TransactionStatus values
EXIT_OK, EXIT_CHECK_FAILED, EXIT_REFUSED, EXIT_NOT_CLEAN, EXIT_COMMITTED = 0, 1, 2, 3, 4
TABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
SEQUENCE_PRIVS = ("USAGE", "SELECT", "UPDATE")
KIND_NAMES = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "f": "foreign table", "S": "sequence"}
# Catalog queries (read-only). Relation kinds: r table, p partitioned table, v view, m materialized view, f foreign table, S sequence.
Q_PUBLIC_RELATIONS = ("SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                      "WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f','S') AND c.relname <> %s ORDER BY c.relkind, c.relname")
Q_PROCUREMENT_RELATIONS = ("SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                           "WHERE n.nspname = 'procurement' AND c.relkind IN ('r','p','v','m','f','S') ORDER BY c.relkind, c.relname")
_SYSTEM_SCHEMA ="n.nspname <> 'information_schema' AND left(n.nspname, 3) <> 'pg_'"
Q_OUTSIDE_RELATIONS = ("SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                       f"WHERE c.relkind IN ('r','p','v','m','f') AND {_SYSTEM_SCHEMA} AND NOT (n.nspname = 'ops' AND c.relname = 'operational_events') "
                       "AND (has_any_column_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,REFERENCES') OR EXISTS (SELECT 1 FROM unnest(ARRAY['SELECT','INSERT',"
                       "'UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) AS p WHERE has_table_privilege(%s, c.oid, p))) ORDER BY 1, 2")
Q_OUTSIDE_SEQUENCES = ("SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                       f"WHERE c.relkind = 'S' AND {_SYSTEM_SCHEMA} AND EXISTS (SELECT 1 FROM unnest(ARRAY['USAGE','SELECT','UPDATE']) AS p "
                       "WHERE has_sequence_privilege(%s, c.oid, p)) ORDER BY 1, 2")
Q_CREATE_SCHEMAS = ("SELECT n.nspname FROM pg_namespace n WHERE left(n.nspname, 3) <> 'pg_' AND n.nspname <> 'information_schema' "
                    "AND has_schema_privilege(%s, n.oid, 'CREATE') ORDER BY 1")


class RehearsalAbort(Exception):
    """The harness detected an unsafe or unexpected condition and stopped."""


class UnexpectedCommit(RehearsalAbort):
    """The transaction ended before the harness ended it."""


@dataclass
class Check:
    name: str
    ok: bool
    info: str = ""


@dataclass
class Outcome:
    ok: bool
    sqlstate: str | None = None
    constraint: str | None = None
    rowcount: int = -1


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    verification: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    possible_commit: bool = False
    interrupted: str | None = None          # class name of a BaseException (KeyboardInterrupt, SystemExit, ...) that ended the run early
    rollback_confirmed: bool = True         # the harness itself saw the transaction return to idle after its ROLLBACK

    @property
    def exit_code(self) -> int:
        if self.possible_commit:
            return EXIT_COMMITTED
        if self.refused:
            return EXIT_REFUSED
        if not self.verification or not all(c.ok for c in self.verification):
            return EXIT_NOT_CLEAN
        if self.interrupted or not self.rollback_confirmed:
            return EXIT_CHECK_FAILED
        return EXIT_OK if all(c.ok for c in self.checks) else EXIT_CHECK_FAILED


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def describe(exc: BaseException) -> str:
    """Safe description of an exception: class and SQLSTATE (and our own abort text), never the driver message."""
    if isinstance(exc, RehearsalAbort):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__} sqlstate={getattr(exc, 'sqlstate', None)}"


def insert_statement(row: dict[str, Any], suffix: str = "") -> tuple[str, list[Any]]:
    unknown = set(row) - set(INSERT_COLUMNS)
    if unknown:
        raise RehearsalAbort(f"case has non-column fields {sorted(unknown)}")
    cols = [c for c in INSERT_COLUMNS if c in row]
    sql = f"INSERT INTO {TABLE} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}){suffix}"
    return sql, [Jsonb(row[c]) if c == "detail" else row[c] for c in cols]


def _scalar(conn, sql: str, params=None):
    return conn.execute(sql, params).fetchone()[0]


# ───────── pre-check and post-verification (independent, read-only connections) ─────────
def procurement_tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'procurement' ORDER BY tablename").fetchall()]


def snapshot(conn) -> dict[str, Any]:
    tables = procurement_tables(conn)
    return {"migrations": _scalar(conn, "SELECT count(*) FROM procurement.schema_migrations"),
            "tables": {t: _scalar(conn, f"SELECT count(*) FROM procurement.{qi(t)}") for t in tables}}


def leftovers(conn) -> list[Check]:
    """One Check per object that must not exist. ok == absent."""
    roles = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", ([ROLE, PROBE],)).fetchall()}
    return [
        Check("schema ops absent", _scalar(conn, "SELECT to_regnamespace('ops') IS NULL")),
        Check("table ops.operational_events absent", _scalar(conn, "SELECT to_regclass('ops.operational_events') IS NULL")),
        Check("role ops_writer absent", ROLE not in roles),
        Check("probe role absent", PROBE not in roles),
        Check("migration 002 not recorded", _scalar(conn, "SELECT count(*) FROM procurement.schema_migrations WHERE version = %s", (VERSION,)) == 0),
    ]


def precheck(conn) -> tuple[list[str], dict[str, Any] | None]:
    problems: list[str] = []
    if int(conn.info.transaction_status) != TS_IDLE:
        problems.append("pre-check connection is inside a transaction")
    conn.execute("SET default_transaction_read_only = on")
    if int(_scalar(conn, "SELECT current_setting('server_version_num')")) < 160000:
        problems.append("PostgreSQL 16 or newer is required (role membership and SET semantics)")
    if _scalar(conn, "SELECT current_setting('standard_conforming_strings')") != "on":
        problems.append("standard_conforming_strings is not on (the guard assumes backslashes in literals are plain characters)")
    problems += [f"already exists: {c.name.replace(' absent', '').replace(' not recorded', ' recorded')}" for c in leftovers(conn) if not c.ok]
    tables = procurement_tables(conn)
    if len(tables) != EXPECTED_PROCUREMENT_TABLES:
        problems.append(f"expected {EXPECTED_PROCUREMENT_TABLES} procurement tables, found {len(tables)}")
    return problems, (None if problems else snapshot(conn))


def verify_clean(conn, baseline: dict[str, Any]) -> list[Check]:
    conn.execute("SET default_transaction_read_only = on")
    out = leftovers(conn)
    now = snapshot(conn)
    out.append(Check("schema_migrations row count unchanged", now["migrations"] == baseline["migrations"]))
    out.append(Check("procurement tables and row counts unchanged", now["tables"] == baseline["tables"]))
    return out


# ───────── the rehearsal itself (one connection, one transaction) ─────────
class Rehearsal:
    def __init__(self, conn, migration: Migration) -> None:
        self.conn, self.m = conn, migration
        self.checks: list[Check] = []
        self.owner: str | None = None
        self.expected_user: str | None = None
        self.began = False
        self.in_txn_confirmed = False
        self.rolled_back = False
        self._n = 0
        self.tag = uuid.uuid4().hex[:8]

    # ── plumbing ──
    def record(self, name: str, ok: bool, info: str = "") -> None:
        self.checks.append(Check(name, bool(ok), info))

    def status(self) -> int:
        return int(self.conn.info.transaction_status)

    def assert_txn(self, what: str = "") -> None:
        s = self.status()
        if s == TS_IDLE:
            raise UnexpectedCommit(f"transaction ended unexpectedly after {what or 'a statement'}")
        if s != TS_INTRANS:
            raise RehearsalAbort(f"unexpected transaction status {s} after {what or 'a statement'}")

    def exec(self, sql: str, params=None):
        cur = self.conn.execute(sql, params)
        self.assert_txn(sql.strip().split(None, 1)[0].upper() if sql.strip() else "")
        return cur

    def scalar(self, sql: str, params=None):
        return self.exec(sql, params).fetchone()[0]

    def try_exec(self, sql: str, params=None) -> Outcome:
        try:
            cur = self.conn.execute(sql, params)
        except Exception as exc:
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate is None:
                raise RehearsalAbort(f"non-database exception {type(exc).__name__}") from None
            s = self.status()
            if s == TS_IDLE:
                raise UnexpectedCommit("transaction ended unexpectedly while a statement failed") from None
            if s != TS_INERROR:
                raise RehearsalAbort(f"unexpected transaction status {s} after a failed statement") from None
            return Outcome(False, sqlstate, getattr(getattr(exc, "diag", None), "constraint_name", None))
        self.assert_txn("a tested statement")
        return Outcome(True, rowcount=cur.rowcount)

    @contextmanager
    def savepoint(self):
        """Everything done inside is undone on exit (ROLLBACK TO + RELEASE). An exception skips the cleanup and aborts the whole run."""
        name = f"rehearsal_sp_{self._n}"
        self._n += 1
        self.exec(f"SAVEPOINT {name}")
        yield
        self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        self.assert_txn("ROLLBACK TO SAVEPOINT")
        self.conn.execute(f"RELEASE SAVEPOINT {name}")
        self.assert_txn("RELEASE SAVEPOINT")

    def assert_identity(self) -> None:
        cu, su = self.conn.execute("SELECT current_user, session_user").fetchone()
        self.assert_txn("identity check")
        if cu != self.expected_user or su != self.owner:
            raise RehearsalAbort("identity mismatch: the active role is not the one the harness set")

    @contextmanager
    def as_role(self, role: str):
        if role not in (ROLE, PROBE):
            raise RehearsalAbort("as_role accepts only the two rehearsal roles")
        with self.savepoint():
            self.exec(f"SET ROLE {qi(role)}")
            self.expected_user = role
            self.assert_identity()
            yield
        self.expected_user = self.owner
        self.exec("RESET ROLE")
        self.assert_identity()

    def attempt(self, sql: str, params=None) -> Outcome:
        """One statement in its own savepoint; always undone; identity re-asserted afterwards."""
        with self.savepoint():
            out = self.try_exec(sql, params)
        self.assert_identity()
        return out

    def expect_denied(self, name: str, sql: str, params=None) -> None:
        out = self.attempt(sql, params)
        self.record(name, (not out.ok) and out.sqlstate == "42501", "42501" if out.sqlstate == "42501" else f"got {'success' if out.ok else out.sqlstate}")

    # ── phases ──
    def run(self) -> None:
        if self.status() != TS_IDLE:
            raise RehearsalAbort("connection is already inside a transaction")
        self.began = True                       # BEFORE the statement: an interrupt during BEGIN must still trigger the explicit rollback
        self.conn.execute("BEGIN")
        self.assert_txn("BEGIN")
        self.in_txn_confirmed = True            # from here on an idle status means the transaction ended without us
        self.exec("SET LOCAL lock_timeout = '5s'")
        self.exec("SET LOCAL statement_timeout = '60s'")
        cu, su = self.exec("SELECT current_user, session_user").fetchone()
        if cu != su:
            raise RehearsalAbort("session already runs under a switched role")
        self.owner = self.expected_user = cu
        self.record("server is PostgreSQL 16 or newer", int(self.scalar("SELECT current_setting('server_version_num')")) >= 160000)
        self.record("standard_conforming_strings is on", self.scalar("SELECT current_setting('standard_conforming_strings')") == "on")

        for i, st in enumerate(self.m.body, 1):                                   # the guarded migration body, one statement at a time
            try:
                self.exec(st.raw)
            except RehearsalAbort:
                raise
            except Exception as exc:
                self.record(f"migration statement {i} ({st.keyword})", False, describe(exc))
                raise RehearsalAbort(f"migration statement {i} failed") from None
        self.record("migration body ran", True, f"{len(self.m.body)} statements")

        self.setup()
        self.catalog_checks()
        self.writer_checks()
        self.forbidden_checks()
        self.public_checks()
        self.procurement_checks()
        self.rls_checks()
        self.record("events table is empty before rollback", self.scalar(f"SELECT count(*) FROM {TABLE}") == 0)

    def setup(self) -> None:
        o = qi(self.owner)
        self.exec(f"GRANT {qi(ROLE)} TO {o} WITH SET TRUE")                      # temporary, inside the transaction: lets the owner SET ROLE
        self.exec(f"CREATE ROLE {qi(PROBE)} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT")
        self.exec(f"GRANT USAGE ON SCHEMA ops TO {qi(PROBE)}")
        self.exec(f"GRANT SELECT, INSERT ON {TABLE} TO {qi(PROBE)}")
        self.exec(f"GRANT {qi(PROBE)} TO {o} WITH SET TRUE")
        self.scratch = f"rehearsal_scratch_{self.tag}"
        self.exec(f"CREATE TABLE ops.{self.scratch} (id integer)")               # owner-owned, nothing granted: target for the DDL denial checks

    def catalog_checks(self) -> None:
        flags = self.exec("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit, rolcanlogin FROM pg_roles WHERE rolname = %s",
                          (ROLE,)).fetchone()
        self.record("ops_writer role flags (only LOGIN)", tuple(flags) == (False, False, False, False, False, False, True) if flags else False)
        self.record("ops_writer belongs to no role", self.scalar(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = %s", (ROLE,)) == 0)
        privs = {p: self.scalar("SELECT has_table_privilege(%s, %s, %s)", (ROLE, TABLE, p))
                 for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")}
        self.record("ops_writer table privileges = INSERT only", [p for p, v in privs.items() if v] == ["INSERT"], ",".join(p for p, v in privs.items() if v))
        self.record("ops_writer schema ops: USAGE yes, CREATE no",
                    self.scalar("SELECT has_schema_privilege(%s, 'ops', 'USAGE')", (ROLE,)) is True
                    and self.scalar("SELECT has_schema_privilege(%s, 'ops', 'CREATE')", (ROLE,)) is False)
        self.record("ops_writer has no privilege on schema procurement",
                    self.scalar("SELECT has_schema_privilege(%s, 'procurement', 'USAGE')", (ROLE,)) is False
                    and self.scalar("SELECT has_schema_privilege(%s, 'procurement', 'CREATE')", (ROLE,)) is False)
        present = [r[0] for r in self.exec("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(API_ROLES),)).fetchall()]
        for r in present:
            ok = (self.scalar("SELECT has_schema_privilege(%s, 'ops', 'USAGE')", (r,)) is False
                  and self.scalar("SELECT has_table_privilege(%s, %s, 'SELECT')", (r, TABLE)) is False
                  and self.scalar("SELECT has_table_privilege(%s, %s, 'INSERT')", (r, TABLE)) is False)
            self.record(f"Supabase API role {r} has no access to ops", ok)
        rls = self.exec("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)", (TABLE,)).fetchone()
        self.record("row level security enabled (not forced)", tuple(rls) == (True, False) if rls else False)
        pol = self.exec("SELECT polname, polcmd, (SELECT array_agg(rolname ORDER BY rolname) FROM pg_roles WHERE oid = ANY(polroles)) "
                        "FROM pg_policy WHERE polrelid = to_regclass(%s)", (TABLE,)).fetchall()
        self.record("exactly one policy: INSERT for ops_writer", len(pol) == 1 and tuple(pol[0][:2]) == ("ops_writer_insert", "a") and list(pol[0][2] or []) == [ROLE])

    def writer_checks(self) -> None:
        with self.as_role(ROLE):
            for name, row in VALID.items():
                out = self.attempt(*insert_statement(row))
                self.record(f"valid row inserts: {name}", out.ok and out.rowcount == 1, "inserted" if out.ok else f"{out.sqlstate}/{out.constraint}")
            for name, row in INVALID.items():
                if name in DB_SKIP:
                    continue
                sqlstate, allowed = DB_EXPECT[name]
                out = self.attempt(*insert_statement(row))
                ok = (not out.ok) and out.sqlstate == sqlstate and (not allowed or out.constraint in allowed)
                self.record(f"invalid row rejected: {name}", ok, f"{out.sqlstate}/{out.constraint}" if not out.ok else "ACCEPTED")
            good = VALID["received"]
            with self.savepoint():
                first, second = self.try_exec(*insert_statement(good)), None
                if first.ok:
                    second = self.try_exec(*insert_statement(good))
            self.assert_identity()
            self.record("plain duplicate event_key fails 23505 on the unique constraint",
                        first.ok and second is not None and not second.ok and second.sqlstate == "23505"
                        and second.constraint == "operational_events_event_key_uq", f"{second.sqlstate}/{second.constraint}" if second else "first insert failed")
            with self.savepoint():
                a = self.try_exec(*insert_statement(good, " ON CONFLICT DO NOTHING"))
                b = self.try_exec(*insert_statement(good, " ON CONFLICT DO NOTHING")) if a.ok else None
            self.assert_identity()
            self.record("ON CONFLICT DO NOTHING: rowcount 1 then 0, no error (else fall back to catching 23505)",
                        a.ok and a.rowcount == 1 and b is not None and b.ok and b.rowcount == 0,
                        "ok" if (b and b.ok) else f"{a.sqlstate or (b.sqlstate if b else None)}")
            self.expect_denied("INSERT ... RETURNING is denied", *insert_statement(good, " RETURNING event_id"))

    def forbidden_checks(self) -> None:
        t, s, x = TABLE, f"ops.{self.scratch}", self.tag
        on_events = [("SELECT", f"SELECT 1 FROM {t} LIMIT 1"), ("SELECT count", f"SELECT count(*) FROM {t}"),
                     ("UPDATE", f"UPDATE {t} SET severity = 'info'"), ("DELETE", f"DELETE FROM {t}"), ("TRUNCATE", f"TRUNCATE {t}"),
                     ("ALTER TABLE ADD COLUMN", f"ALTER TABLE {t} ADD COLUMN extra integer"),
                     ("ALTER TABLE DISABLE RLS", f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY"), ("DROP TABLE", f"DROP TABLE {t}"),
                     ("CREATE POLICY", f"CREATE POLICY p_{x} ON {t} FOR SELECT TO {ROLE} USING (true)"),
                     ("ALTER TABLE OWNER TO itself", f"ALTER TABLE {t} OWNER TO {ROLE}")]       # (a plain GRANT would only WARN: ops_writer holds some privilege)
        on_scratch = [("SELECT", f"SELECT 1 FROM {s}"), ("INSERT", f"INSERT INTO {s} VALUES (1)"), ("ALTER TABLE", f"ALTER TABLE {s} ADD COLUMN extra integer"),
                      ("DROP TABLE", f"DROP TABLE {s}"), ("TRUNCATE", f"TRUNCATE {s}")]
        creates = [("CREATE TABLE in ops", f"CREATE TABLE ops.rehearsal_new_{x} (id integer)"),
                   ("CREATE TABLE in public", f"CREATE TABLE public.rehearsal_new_{x} (id integer)"),
                   ("CREATE TABLE in procurement", f"CREATE TABLE procurement.rehearsal_new_{x} (id integer)"),
                   ("CREATE SCHEMA", f"CREATE SCHEMA rehearsal_schema_{x}"), ("CREATE ROLE", f"CREATE ROLE rehearsal_role_{x}")]
        with self.as_role(ROLE):
            for label, sql in on_events:
                self.expect_denied(f"ops_writer denied on events: {label}", sql)
            for label, sql in on_scratch:
                self.expect_denied(f"ops_writer denied on existing scratch table: {label}", sql)
            for label, sql in creates:
                self.expect_denied(f"ops_writer denied: {label}", sql)

    def held_privileges(self, qualified: str, kind: str) -> list[str]:
        """Effective privileges of ops_writer (PUBLIC and role membership included) on one relation. Catalog functions only: nothing is executed on
        the object, and no sequence function (nextval/currval/setval) is ever called."""
        if kind == "S":
            return [p for p in SEQUENCE_PRIVS if self.scalar("SELECT has_sequence_privilege(%s, %s, %s)", (ROLE, qualified, p))]
        held = [p for p in TABLE_PRIVS if self.scalar("SELECT has_table_privilege(%s, %s, %s)", (ROLE, qualified, p))]
        if self.scalar("SELECT has_any_column_privilege(%s, %s, 'SELECT,INSERT,UPDATE,REFERENCES')", (ROLE, qualified)):
            held.append("column-level")
        return held

    def public_checks(self) -> None:
        """Effective privileges of ops_writer on schema public and its tables, plus denial tests against a REAL scratch table created here.
        No privilege is granted to make anything pass. If the scratch table cannot be created the run fails closed (the limitation is reported)."""
        privs = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
        scratch = f"public.rehearsal_public_{self.tag}"
        self.exec("SAVEPOINT rehearsal_public_create")
        made = self.try_exec(f"CREATE TABLE {scratch} (id integer)")
        if not made.ok:
            self.conn.execute("ROLLBACK TO SAVEPOINT rehearsal_public_create")
            self.assert_txn("ROLLBACK TO SAVEPOINT")
            self.conn.execute("RELEASE SAVEPOINT rehearsal_public_create")
            self.assert_txn("RELEASE SAVEPOINT")
            self.record("public scratch table could be created by the migration identity", False,
                        f"{made.sqlstate}: public-schema denial tests were NOT performed (limitation)")
            raise RehearsalAbort("cannot create a scratch table in public; failing closed instead of skipping the public-schema checks")
        self.exec("RELEASE SAVEPOINT rehearsal_public_create")
        self.record("public scratch table exists (denials below cannot be 'object missing')", self.scalar("SELECT to_regclass(%s) IS NOT NULL", (scratch,)))
        usage = self.scalar("SELECT has_schema_privilege(%s, 'public', 'USAGE')", (ROLE,))
        self.record("ops_writer CREATE on schema public: no", self.scalar("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (ROLE,)) is False,
                    f"USAGE={'yes' if usage else 'no'} (informational: PUBLIC normally has it)")
        mine = [p for p in privs if self.scalar("SELECT has_table_privilege(%s, %s, %s)", (ROLE, scratch, p))]
        self.record("ops_writer holds no privilege on the new public scratch table (default ACLs included)", not mine, ",".join(mine) or "none")
        rels = [(r[0], r[1]) for r in self.exec(Q_PUBLIC_RELATIONS, (scratch.split(".", 1)[1],)).fetchall()]
        counts: dict[str, int] = {}
        for _, k in rels:
            counts[k] = counts.get(k, 0) + 1
        inventory = ", ".join(f"{KIND_NAMES.get(k, k)}={n}" for k, n in sorted(counts.items())) or "none"
        offenders: list[str] = []
        for name, kind in rels:
            held = self.held_privileges(f"public.{qi(name)}", kind)
            if held:
                offenders.append(f"{name}[{KIND_NAMES.get(kind, kind)}]: {','.join(held)}")
        seq_off = [o for o in offenders if "[sequence]" in o]
        self.record("ops_writer holds no privilege on any existing public table, partitioned table, view, materialized view or foreign table (incl. column-level)",
                    not [o for o in offenders if "[sequence]" not in o], f"{len(rels)} relations ({inventory})" + "".join(f"; {o}" for o in offenders if o not in seq_off))
        self.record("ops_writer holds no USAGE, SELECT or UPDATE on any existing public sequence", not seq_off,
                    f"{counts.get('S', 0)} sequences" + "".join(f"; {o}" for o in seq_off))
        outside = ([f"{a}.{b}[{KIND_NAMES.get(c, c)}]" for a, b, c in self.exec(Q_OUTSIDE_RELATIONS, (ROLE, ROLE)).fetchall()]
                   + [f"{a}.{b}[sequence]" for a, b, c in self.exec(Q_OUTSIDE_SEQUENCES, (ROLE,)).fetchall()])
        self.record("ops_writer holds no privilege on any relation outside ops.operational_events (all non-system schemas, PUBLIC and membership included)",
                    not outside, "none" if not outside else f"{len(outside)} found: " + ", ".join(outside[:20]))
        creates = [r[0] for r in self.exec(Q_CREATE_SCHEMAS, (ROLE,)).fetchall()]
        self.record("ops_writer has CREATE on no schema", not creates, "none" if not creates else ", ".join(creates))
        ops = [("SELECT", f"SELECT 1 FROM {scratch}"), ("INSERT", f"INSERT INTO {scratch} VALUES (1)"), ("UPDATE", f"UPDATE {scratch} SET id = 2"),
               ("DELETE", f"DELETE FROM {scratch}"), ("TRUNCATE", f"TRUNCATE {scratch}"),
               ("ALTER TABLE", f"ALTER TABLE {scratch} ADD COLUMN extra integer"), ("DROP TABLE", f"DROP TABLE {scratch}")]
        with self.as_role(ROLE):
            for label, sql in ops:
                self.expect_denied(f"ops_writer denied on real public scratch table: {label}", sql)
        # Existing public relations are NEVER queried: a view, materialized view, foreign table or table with triggers/policies/functions could run
        # code with side effects. Their effective privileges are proven from the catalogs above; live denial tests run only on harness-created objects.

    def procurement_checks(self) -> None:
        tables = procurement_tables(self.conn)
        self.assert_txn("catalog read")
        if len(tables) != EXPECTED_PROCUREMENT_TABLES:
            raise RehearsalAbort(f"expected {EXPECTED_PROCUREMENT_TABLES} procurement tables, found {len(tables)}")
        # Catalog-only, like public: no statement is executed against any existing procurement relation (a view, trigger or function could run code).
        rels = [(r[0], r[1]) for r in self.exec(Q_PROCUREMENT_RELATIONS).fetchall()]
        if not set(tables) <= {n for n, _ in rels}:
            raise RehearsalAbort("procurement table list and relation list disagree")
        for name, kind in rels:
            held = self.held_privileges(f"procurement.{qi(name)}", kind)
            self.record(f"ops_writer holds no privilege on procurement.{name} ({KIND_NAMES.get(kind, kind)})", not held, ",".join(held) or "none")

    def rls_checks(self) -> None:
        """Prove row level security does the denying: the probe role is GRANTED schema USAGE and table SELECT/INSERT (verified first),
        has no BYPASSRLS, and has no policy, so it must see no rows and be unable to insert."""
        self.record("probe role has USAGE on ops and SELECT, INSERT on the table",
                    self.scalar("SELECT has_schema_privilege(%s, 'ops', 'USAGE')", (PROBE,)) is True
                    and self.scalar("SELECT has_table_privilege(%s, %s, 'SELECT')", (PROBE, TABLE)) is True
                    and self.scalar("SELECT has_table_privilege(%s, %s, 'INSERT')", (PROBE, TABLE)) is True)
        flags = self.exec("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = %s", (PROBE,)).fetchone()
        self.record("probe role has no BYPASSRLS and is not superuser", tuple(flags) == (False, False) if flags else False)
        with self.savepoint():
            seeded = self.try_exec(*insert_statement(VALID["received"]))             # as the owner: owners bypass row level security
            owner_sees = self.scalar(f"SELECT count(*) FROM {TABLE}") if seeded.ok else None
            with self.as_role(PROBE):
                probe_sees = self.scalar(f"SELECT count(*) FROM {TABLE}")
                probe_insert = self.attempt(*insert_statement(VALID["replayed"]))
        self.assert_identity()
        self.record("owner can see the seeded row", seeded.ok and owner_sees == 1)
        self.record("probe with SELECT privilege sees 0 rows (row level security, no policy)", probe_sees == 0, str(probe_sees))
        self.record("probe with INSERT privilege is denied by row level security", (not probe_insert.ok) and probe_insert.sqlstate == "42501", str(probe_insert.sqlstate))

    # ── end ──
    def finish(self) -> tuple[bool, bool]:
        """Roll back whatever is open and close the connection. Returns (ended_early, rollback_confirmed).
        ended_early: the transaction had already ended on its own (a commit we did not ask for) - nothing is rolled back in that case.
        rollback_confirmed: the harness issued ROLLBACK and then SAW the transaction return to idle. If the connection is unusable (interrupt, lost
        connection) this is False: the server discards an open transaction when the session ends, but only the independent verification can show it.
        Also runs after KeyboardInterrupt/SystemExit, so it must not raise."""
        ended_early, confirmed = False, not self.began
        try:
            if self.began and not self.rolled_back:
                s = self.status()
                if s == TS_IDLE:
                    ended_early = self.in_txn_confirmed                             # idle before BEGIN ever took effect is not a commit
                    self.rolled_back = not self.in_txn_confirmed
                elif s != TS_UNKNOWN:                                               # in transaction, in error, or a statement still marked active
                    self.conn.execute("ROLLBACK")
                    self.rolled_back = self.status() == TS_IDLE
            confirmed = self.rolled_back or not self.began
        except BaseException:
            confirmed = False                                                       # includes a second Ctrl-C: reported, never hidden
        try:
            self.conn.close()
        except BaseException:
            pass
        return ended_early, confirmed and not ended_early


def execute_rehearsal(connect: Callable[[], Any], migration: Migration) -> Report:
    rep = Report()
    conn_a = connect()
    try:
        problems, baseline = precheck(conn_a)
    finally:
        conn_a.close()
    if problems or baseline is None:
        rep.refused = problems or ["pre-check failed"]
        return rep
    h = Rehearsal(connect(), migration)
    try:
        h.run()
    except UnexpectedCommit as exc:
        rep.possible_commit = True
        rep.notes.append(f"STOPPED: {exc}")
    except Exception as exc:
        h.record("rehearsal aborted", False, describe(exc))
    except BaseException as exc:                                       # KeyboardInterrupt, SystemExit: still roll back and verify, then report
        rep.interrupted = type(exc).__name__
        h.record("rehearsal interrupted", False, type(exc).__name__)
        rep.notes.append(f"INTERRUPTED ({type(exc).__name__}): rolling back explicitly and verifying independently.")
    ended_early, confirmed = h.finish()
    rep.checks = h.checks
    rep.rollback_confirmed = confirmed
    if not confirmed and not ended_early:
        rep.notes.append("ROLLBACK NOT CONFIRMED by the harness (connection unusable or the rollback itself failed). The server discards an open "
                         "transaction when the session ends, but nothing is claimed until the independent verification below says so.")
    if ended_early and not rep.possible_commit:
        rep.possible_commit = True
        rep.notes.append("STOPPED: the transaction was already closed when the harness tried to roll it back")
    if rep.possible_commit:
        rep.notes.append("POSSIBLE PERSISTENT CHANGES. A later ROLLBACK cannot undo a commit, so none is claimed. Manual review of schema ops, "
                         "roles ops_writer / ops_rls_probe and migration 002 is required. The read-only observation below is NOT a clean bill of health.")
    try:
        conn_c = connect()
        try:
            rep.verification = verify_clean(conn_c, baseline)
        finally:
            conn_c.close()
    except BaseException as exc:                                       # includes a second interrupt: verification incomplete => never "clean"
        rep.verification = [Check("independent verification could run", False, describe(exc) if isinstance(exc, Exception) else type(exc).__name__)]
    return rep


# ───────── CLI ─────────
def default_connect():
    import psycopg
    from dotenv import load_dotenv
    load_dotenv()
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15, sslmode="require", autocommit=True)


def print_report(rep: Report, out=sys.stdout) -> None:
    def line(c: Check) -> str:
        return f"  {'PASS' if c.ok else 'FAIL'}  {c.name}" + (f"  [{c.info}]" if c.info else "")
    for r in rep.refused:
        print(f"REFUSED: {r}", file=out)
    for n in rep.notes:
        print(n, file=out)
    if rep.checks:
        print(f"rehearsal checks ({sum(c.ok for c in rep.checks)}/{len(rep.checks)} passed):", file=out)
        for c in rep.checks:
            print(line(c), file=out)
    if rep.verification:
        print("independent verification (new connection):", file=out)
        for c in rep.verification:
            print(line(c), file=out)
    verdict = {EXIT_OK: "ALL PASSED, ROLLED BACK AND VERIFIED CLEAN", EXIT_CHECK_FAILED: "SOME CHECKS FAILED, THE RUN WAS ABORTED OR THE ROLLBACK WAS NOT CONFIRMED BY THE HARNESS (independent verification found no leftovers)",
               EXIT_REFUSED: "REFUSED TO START (nothing was run)", EXIT_NOT_CLEAN: "ROLLBACK VERIFICATION FAILED: manual review required",
               EXIT_COMMITTED: "POSSIBLE PERSISTENT CHANGES: manual review required"}[rep.exit_code]
    print(f"RESULT: {verdict}", file=out)


def plan_text(m: Migration) -> str:
    rows = [f"  {i:>2}. {s.keyword:<7} {s.skeleton[:70]}" for i, s in enumerate(m.body, 1)]
    return "\n".join([f"migration body ({len(m.body)} statements; outer BEGIN/COMMIT removed by the guard), sha256 {m.digest[:16]}...", *rows,
                      f"cases: {len(VALID)} valid rows, {len(INVALID) - len(DB_SKIP)} invalid rows, run as {ROLE}; probe role {PROBE}; unique scratch table"])


def main(argv: list[str] | None = None, *, connect: Callable[[], Any] | None = None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description="Rolled-back rehearsal of migration 002 (dry run by default).")
    ap.add_argument("--sql", type=Path, default=DRAFT)
    ap.add_argument("--execute", action="store_true", help="connect and run the rehearsal (requires explicit approval)")
    args = ap.parse_args(argv)
    try:
        migration = load_reviewed_migration(args.sql.read_text(encoding="utf-8"))
    except (GuardError, OSError) as exc:
        print(f"GUARD REFUSED: {exc}" if isinstance(exc, GuardError) else f"cannot read file: {type(exc).__name__}", file=out)
        return EXIT_REFUSED
    print(plan_text(migration), file=out)
    if not args.execute:
        print("DRY RUN: nothing was connected or executed. --execute needs the user's explicit approval.", file=out)
        return EXIT_OK
    try:
        rep = execute_rehearsal(connect or default_connect, migration)
    except KeyboardInterrupt:                                          # only reachable before the transaction exists (pre-check): nothing to roll back
        print("INTERRUPTED before any transaction was started; nothing was run.", file=out)
        return EXIT_REFUSED
    except Exception as exc:
        print(f"FAIL: {describe(exc)}", file=out)
        return EXIT_REFUSED
    print_report(rep, out)
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
