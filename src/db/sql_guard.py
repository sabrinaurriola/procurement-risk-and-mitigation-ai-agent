"""Fail-closed SQL guard for the migration rehearsal (docs/day4-architecture.md section 8.4). Pure functions, no database access.

Why it exists: the draft migration is wrapped in BEGIN ... COMMIT. The rehearsal must run the body inside ITS OWN transaction and roll it back,
so the wrappers have to be removed, and nothing else in the file may be able to end, nest or escape that transaction.

Design:
* A small lexer (comments, '...' with '', "..." identifiers, $tag$...$tag$) finds statement boundaries. Text is never rewritten: each statement
  keeps its exact original text (`raw`) and the wrappers are removed by dropping whole statements, never by string replacement.
* Constructs the lexer does not model are REJECTED, not guessed: prefixed literals (E'..', U&'..', B'..', X'..', N'..'), U&".." identifiers,
  backslashes outside literals (psql meta-commands), unterminated literals/comments, a final statement without ';'.
* After the two wrappers are removed, every remaining statement must match the reviewed migration exactly in shape and TARGET (schema ops,
  role ops_writer, table ops.operational_events, procurement.schema_migrations version 002, and the one reviewed DO block). A first-keyword
  allow-list alone is deliberately not enough.
Any deviation raises GuardError. Editing the migration therefore means editing the reviewed patterns below in the same change."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

__all__ = ["GuardError", "Stmt", "Migration", "scan", "strip_wrappers", "check_body", "load_reviewed_migration", "REVIEWED_DO_BODY"]


class GuardError(Exception):
    pass


@dataclass(frozen=True)
class Stmt:
    raw: str            # exact original text of the statement, without its terminating ';' (may include leading comments/whitespace)
    code: str           # comments removed, whitespace collapsed, literals kept
    skeleton: str       # like `code`, every literal / dollar-quoted body replaced by a placeholder (for keyword checks)

    @property
    def keyword(self) -> str:
        return self.skeleton.split(" ", 1)[0].upper() if self.skeleton else ""


@dataclass(frozen=True)
class Migration:
    body: tuple[Stmt, ...]      # wrappers removed; guarded
    digest: str                 # sha256 of the whole file as read, for the reviewer


_NL = "\x01"        # stands for a newline inside a dollar-quoted body while the rest of the statement is whitespace-collapsed
_IDENT = re.compile(r"[A-Za-z0-9_$\u0080-￿]")
_DOLLAR = re.compile(r"\$([A-Za-z_\u0080-￿][A-Za-z0-9_\u0080-￿]*)?\$")


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def scan(sql: str) -> list[Stmt]:
    """Split into statements on top-level ';'. Raises GuardError on anything the lexer does not model."""
    i, n, start = 0, len(sql), 0
    code: list[str] = []
    skel: list[str] = []
    out: list[Stmt] = []
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            code.append(" "); skel.append(" ")
            continue
        if c == "/" and sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1; j += 2
                elif sql.startswith("*/", j):
                    depth -= 1; j += 2
                else:
                    j += 1
            if depth:
                raise GuardError("unterminated block comment")
            i = j
            code.append(" "); skel.append(" ")
            continue
        if c == "'":
            prev = sql[i - 1] if i else ""
            if prev and (_IDENT.match(prev) or prev == "&"):
                raise GuardError("prefixed string literal (E'', U&'', B'', X'', N'') is not supported and is rejected")
            j = i + 1
            while True:
                k = sql.find("'", j)
                if k == -1:
                    raise GuardError("unterminated string literal")
                if sql.startswith("''", k):
                    j = k + 2
                    continue
                break
            if "\\" in sql[i:k + 1]:
                raise GuardError("backslash inside a string literal: its meaning depends on standard_conforming_strings, so it is rejected")
            code.append(sql[i:k + 1]); skel.append("'?'")
            i = k + 1
            continue
        if c == '"':
            if i and sql[i - 1] == "&":
                raise GuardError('U&"..." identifier is not supported and is rejected')
            j = i + 1
            while True:
                k = sql.find('"', j)
                if k == -1:
                    raise GuardError("unterminated quoted identifier")
                if sql.startswith('""', k):
                    j = k + 2
                    continue
                break
            if "\\" in sql[i:k + 1]:
                raise GuardError("backslash inside a quoted identifier is rejected")
            code.append(sql[i:k + 1]); skel.append('"?"')
            i = k + 1
            continue
        if c == "$" and not (i and _IDENT.match(sql[i - 1])):
            m = _DOLLAR.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                if end == -1:
                    raise GuardError("unterminated dollar-quoted string")
                if "\\" in sql[i:end + len(tag)]:
                    raise GuardError("backslash inside a dollar-quoted body is rejected")
                code.append(sql[i:end + len(tag)].replace("\n", _NL)); skel.append("$?$")      # keep line structure so -- comments inside stay line-bound
                i = end + len(tag)
                continue
        if c == "\\":
            raise GuardError("backslash outside a literal (possible psql meta-command) is rejected")
        if c == ";":
            c_s, s_s = _collapse("".join(code)), _collapse("".join(skel))
            if c_s:
                out.append(Stmt(raw=sql[start:i], code=c_s, skeleton=s_s))
            code, skel, start = [], [], i + 1
            i += 1
            continue
        code.append(c); skel.append(c)
        i += 1
    if _collapse("".join(code)):
        raise GuardError("final statement is not terminated by ';'")
    return out


def strip_wrappers(stmts: list[Stmt]) -> list[Stmt]:
    """Drop ONLY the outer BEGIN (first) and COMMIT (last). Anything else in those positions is an error."""
    if len(stmts) < 3:
        raise GuardError("migration is too short to have BEGIN ... COMMIT wrappers")
    if stmts[0].skeleton.upper() != "BEGIN":
        raise GuardError("first statement must be exactly BEGIN")
    if stmts[-1].skeleton.upper() != "COMMIT":
        raise GuardError("last statement must be exactly COMMIT")
    return list(stmts[1:-1])


_TXN_WORDS = {"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "PREPARE"}
_DO_FORBIDDEN = re.compile(r"\b(COMMIT|ROLLBACK|SAVEPOINT|RELEASE|ABORT)\b", re.I)

# The reviewed DO block (comments stripped, whitespace collapsed). Changing the DO block in the draft requires changing this constant.
REVIEWED_DO_BODY = (
    "DECLARE r text; BEGIN FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP "
    "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN "
    "EXECUTE format('REVOKE ALL ON SCHEMA ops FROM %I', r); "
    "EXECUTE format('REVOKE ALL ON ops.operational_events FROM %I', r); "
    "END IF; END LOOP; END")

_ALLOWED = [re.compile(p, re.I | re.S) for p in (
    r"CREATE SCHEMA ops",
    r"CREATE ROLE ops_writer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT",
    r"ALTER ROLE ops_writer SET (search_path = ops, pg_catalog|statement_timeout = '5s'|idle_in_transaction_session_timeout = '10s')",
    r"CREATE TABLE ops\.operational_events \(.*\)",
    r"CREATE INDEX ix_operational_events_request ON ops\.operational_events \(request_id, occurred_at\)",
    r"ALTER TABLE ops\.operational_events ENABLE ROW LEVEL SECURITY",
    r"CREATE POLICY ops_writer_insert ON ops\.operational_events FOR INSERT TO ops_writer WITH CHECK \(true\)",
    r"REVOKE ALL ON SCHEMA ops FROM PUBLIC",
    r"REVOKE ALL ON ops\.operational_events FROM PUBLIC",
    r"GRANT USAGE ON SCHEMA ops TO ops_writer",
    r"GRANT INSERT ON ops\.operational_events TO ops_writer",
    r"INSERT INTO procurement\.schema_migrations \(version\) VALUES \('002'\)",
)]
DO_PIN = "DO"          # the DO block is pinned by its normalised body (REVIEWED_DO_BODY), not by a digest of text that contains comments
# The COMPLETE reviewed statement sequence, in order: (label, sha256 of the statement's comment-free, whitespace-collapsed code, or DO_PIN).
# This is the authority. A changed, added, removed or reordered statement - including any change inside CREATE TABLE (a new column, default,
# expression, sequence reference or constraint) - fails closed until a human reviews it and updates this table in the same change.
# Regenerate with: `python -I` over scan()/strip_wrappers(): hashlib.sha256(stmt.code.encode()).hexdigest().
REVIEWED_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("CREATE SCHEMA ops", "66c02d64e9b8014ba436482596f37780386283deb91db8ca945f307d11dd8509"),
    ("CREATE ROLE ops_writer", "9b0fec1973b72d3c3ba8a5020f005d40fc5be96ea93f58294f5d2e3e4eced3f8"),
    ("ALTER ROLE ops_writer SET search_path", "cbc2686fbd1aef7d80926535554cc2992d9a69d60f245db378a1a1323f2a42c2"),
    ("ALTER ROLE ops_writer SET statement_timeout", "11e93e77aa8c7d2195b7ea3b9f01c725865a7e4e8bd56ae13573665afaf37b79"),
    ("ALTER ROLE ops_writer SET idle_in_transaction_session_timeout", "545c60d9316bfd337c6a0a27250b7b2c8c0f965d0a13ac126c1f46871f5e3088"),
    ("CREATE TABLE ops.operational_events", "7b3bc196205bfcd84402b8ea350b89559ff8f542072808bcadd14f77141c37fc"),
    ("CREATE INDEX ix_operational_events_request", "51630f0776e35f42531f16d256230cd239a2902661d267f2347a0638bcab66c7"),
    ("ENABLE ROW LEVEL SECURITY", "875efa346c6b4788643ee40e7d4ee3c8778e4295bfef7a99bc249e19bf1a4f0d"),
    ("CREATE POLICY ops_writer_insert", "c41e9725e113e36e6a369d423bd96eb671a054e41a465048c48aef6ab6e7eab5"),
    ("REVOKE ALL ON SCHEMA ops FROM PUBLIC", "5bb94ebf00a124586716e3ca2fab590596961fc5c54001cb9882abfa0213731f"),
    ("REVOKE ALL ON ops.operational_events FROM PUBLIC", "7d072f70c91817db305287214d41cfd60c2a9f8d6fceeaa2e937068dad58e617"),
    ("DO block revoking from the Supabase API roles", DO_PIN),
    ("GRANT USAGE ON SCHEMA ops", "a8b20d87881ab0de9ed3cc3d09b949cd807253198eb4e68abbb4ab31386eeb17"),
    ("GRANT INSERT ON ops.operational_events", "7343ed95a5edcf8dca8c6c5601f43922e35439cf8e10ac7cb35a4e217a9ee0fc"),
    ("INSERT INTO procurement.schema_migrations 002", "61062201d13f6592533e3a03d5982e465730571bbe0da0a6aea43d4756134453"),
)
_ALLOWED_DEFAULTS = {"gen_random_uuid()", "now()", "'info'", "'{}'::jsonb"}
_TABLE_FORBIDDEN = re.compile(r"\b(REFERENCES|INHERITS|PARTITION|NEXTVAL|SERIAL|BIGSERIAL|SMALLSERIAL|SEQUENCE|GENERATED|IDENTITY|COLLATE|UNLOGGED|TEMP|TEMPORARY)\b"
                              r"|\bAS SELECT\b|\(\s*LIKE\b", re.I)


def _strip_comments(text: str) -> str:
    """Remove -- and /* */ comments OUTSIDE '...' and "..." (a '--' inside a literal is data, not a comment)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
        elif text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1; j += 2
                elif text.startswith("*/", j):
                    depth -= 1; j += 2
                else:
                    j += 1
            if depth:
                raise GuardError("unterminated block comment inside a DO body")
            out.append(" ")
            i = j
        elif text[i] in "'\"":
            q = text[i]
            j = i + 1
            while True:
                k = text.find(q, j)
                if k == -1:
                    raise GuardError("unterminated literal inside a DO body")
                if text.startswith(q * 2, k):
                    j = k + 2
                    continue
                break
            out.append(text[i:k + 1])
            i = k + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _normalise_do_body(code: str) -> str | None:
    m = re.fullmatch(r"DO \$\$(.*)\$\$", code, re.S | re.I)
    if not m:
        return None
    return _collapse(_strip_comments(m.group(1).replace(_NL, "\n")))


def check_body(body: list[Stmt]) -> None:
    """Every statement must be transaction-safe AND one of the reviewed statements (shape and targets)."""
    if not body:
        raise GuardError("empty migration body")
    if len(body) != len(REVIEWED_SEQUENCE):
        raise GuardError(f"migration has {len(body)} statements, the reviewed sequence has {len(REVIEWED_SEQUENCE)}; review and update REVIEWED_SEQUENCE")
    for idx, s in enumerate(body, 1):
        where = f"statement {idx}"
        label, pin = REVIEWED_SEQUENCE[idx - 1]
        if s.keyword in _TXN_WORDS:
            raise GuardError(f"{where}: transaction control ({s.keyword}) is not allowed inside the body")
        if re.search(r"\bAND CHAIN\b|\bPREPARED\b|\bTRANSACTION\b", s.skeleton, re.I):
            raise GuardError(f"{where}: transaction-control wording is not allowed")
        if re.search(r"\bCONCURRENTLY\b", s.skeleton, re.I):
            raise GuardError(f"{where}: CONCURRENTLY cannot run inside a transaction")
        if (pin == DO_PIN) != (s.keyword == "DO"):
            raise GuardError(f"{where}: expected '{label}' at this position of the reviewed sequence")
        if s.keyword == "DO":
            do_body = _normalise_do_body(s.code)
            if do_body is None:
                raise GuardError(f"{where}: DO block must use plain $$ quoting")
            if _DO_FORBIDDEN.search(do_body):
                raise GuardError(f"{where}: DO block contains transaction-control words")
            if do_body != REVIEWED_DO_BODY:
                raise GuardError(f"{where}: DO block differs from the reviewed block")
            continue
        if not any(p.fullmatch(s.code) for p in _ALLOWED):
            raise GuardError(f"{where}: {s.keyword} statement is not in the reviewed migration set")
        if hashlib.sha256(s.code.encode("utf-8")).hexdigest() != pin:
            raise GuardError(f"{where}: differs from the reviewed '{label}'; review the change and update REVIEWED_SEQUENCE in the same change")
        if s.skeleton.upper().startswith("CREATE TABLE"):
            if _TABLE_FORBIDDEN.search(s.skeleton):
                raise GuardError(f"{where}: table definition uses a forbidden clause")
            defaults = re.findall(r"\bDEFAULT\s+((?:'[^']*'(?:::\w+)?|[\w.]+\(\)))", s.code)
            if len(defaults) != len(re.findall(r"\bDEFAULT\b", s.skeleton)) or not set(defaults) <= _ALLOWED_DEFAULTS:
                raise GuardError(f"{where}: table definition has a default outside the reviewed set")
            if re.findall(r"\b\w+\.\w+", s.skeleton) != ["ops.operational_events"]:
                raise GuardError(f"{where}: table definition references another qualified object")


def load_reviewed_migration(sql: str) -> Migration:
    stmts = scan(sql)
    body = strip_wrappers(stmts)
    check_body(body)
    return Migration(body=tuple(body), digest=hashlib.sha256(sql.encode("utf-8")).hexdigest())
