"""Read-only preflight checks against Supabase PostgreSQL. Never writes; never prints credentials or hosts.

Usage: python -m src.db.preflight
"""
from __future__ import annotations

import os
import sys

import psycopg
from dotenv import load_dotenv

EXPECTED_VECTOR_SCHEMA = "extensions"


def connect() -> psycopg.Connection:
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("FAIL: DATABASE_URL not set")
    conn = psycopg.connect(url, connect_timeout=15, sslmode="require")
    conn.read_only = True  # every statement runs in a READ ONLY transaction
    return conn


def inspect(conn: psycopg.Connection) -> dict:
    q = lambda sql, *a: conn.execute(sql, a or None).fetchall()  # noqa: E731
    info: dict = {}
    info["server_version"] = q("SHOW server_version")[0][0]
    info["vector_installed"] = q(
        "SELECT e.extversion, n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        "WHERE e.extname = 'vector'")
    info["vector_available"] = q("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
    info["schemas"] = [r[0] for r in q(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' ORDER BY 1")]
    info["procurement_tables"] = [r[0] for r in q(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'procurement' ORDER BY 1")]
    info["can_create_schema"] = q("SELECT has_database_privilege(current_database(), 'CREATE')")[0][0]
    info["can_use_extensions_schema"] = (
        q("SELECT has_schema_privilege('extensions', 'USAGE')")[0][0] if "extensions" in info["schemas"] else None)
    return info


def main() -> int:
    try:
        with connect() as conn:
            info = inspect(conn)
    except Exception as exc:  # sanitised: class and SQLSTATE only
        print(f"FAIL: {type(exc).__name__} sqlstate={getattr(exc, 'sqlstate', None)}")
        return 1
    print("server_version:", info["server_version"])
    print("pgvector installed (version, schema):", info["vector_installed"] or "NOT INSTALLED")
    print("pgvector available to install:", info["vector_available"] or "NO")
    print("existing schemas:", info["schemas"])
    print("procurement schema exists:", "procurement" in info["schemas"], "| tables:", info["procurement_tables"])
    print("CREATE privilege on database:", info["can_create_schema"])
    print("USAGE on 'extensions' schema:", info["can_use_extensions_schema"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
