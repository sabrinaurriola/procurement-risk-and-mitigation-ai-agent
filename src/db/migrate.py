"""Migration runner. DRY-RUN BY DEFAULT; `--apply` is required to write and needs explicit user approval.

Rules:
- Applied versions are read from procurement.schema_migrations; recorded versions are skipped.
- If the procurement schema contains tables but schema_migrations has no record, abort (unrecorded objects).
- pgvector must be installed in, or installable into, the schema the SQL uses ("extensions").
- Each migration file is transactional (BEGIN ... COMMIT) and records its own version.

Usage: python -m src.db.migrate [--apply]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from src.db.preflight import EXPECTED_VECTOR_SCHEMA, connect, inspect

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def migration_files(sql_dir: Path = SQL_DIR) -> list[tuple[str, Path]]:
    files = []
    for p in sorted(sql_dir.glob("[0-9][0-9][0-9]_*.sql")):
        files.append((re.match(r"(\d{3})_", p.name).group(1), p))
    return files


def plan(info: dict, applied: set[str], files: list[tuple[str, Path]]) -> tuple[list[tuple[str, Path]], list[str]]:
    """Return (pending migrations, blocking problems). Pure function over inspected state."""
    problems: list[str] = []
    if "procurement" in info["schemas"] and not applied and info["procurement_tables"]:
        problems.append("procurement schema has tables but no schema_migrations record: refusing to continue")
    if info["vector_installed"]:
        _version, schema = info["vector_installed"][0]
        if schema != EXPECTED_VECTOR_SCHEMA:
            problems.append(f"pgvector is installed in schema {schema!r}, SQL expects {EXPECTED_VECTOR_SCHEMA!r}")
    elif not info["vector_available"]:
        problems.append("pgvector is not available on this server")
    if EXPECTED_VECTOR_SCHEMA not in info["schemas"]:
        problems.append(f"schema {EXPECTED_VECTOR_SCHEMA!r} does not exist")
    pending = [(v, p) for v, p in files if v not in applied]
    return pending, problems


def applied_versions(conn) -> set[str]:
    exists = conn.execute("SELECT to_regclass('procurement.schema_migrations')").fetchone()[0]
    if not exists:
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM procurement.schema_migrations")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute pending migrations (requires approval)")
    args = ap.parse_args()
    files = migration_files()
    try:
        with connect() as conn:  # read-only inspection
            info = inspect(conn)
            applied = applied_versions(conn)
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__} sqlstate={getattr(exc, 'sqlstate', None)}")
        return 1
    pending, problems = plan(info, applied, files)
    print("recorded versions:", sorted(applied) or "none")
    print("pending:", [v for v, _ in pending] or "none")
    for p in problems:
        print("BLOCKER:", p)
    if problems:
        return 2
    if not args.apply:
        print("DRY RUN: no changes made. Re-run with --apply after explicit approval.")
        return 0
    import psycopg
    from dotenv import load_dotenv
    import os
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15, sslmode="require", autocommit=True) as conn:
        for version, path in pending:
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"applied {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
