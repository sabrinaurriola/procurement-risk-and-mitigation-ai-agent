"""Shim: the shared event cases live in src/db/ops_cases.py so the rehearsal harness can import them (src must not import tests)."""
from src.db.ops_cases import DB_EXPECT, DB_SKIP, INVALID, PYTHON_ONLY_INVALID, R, VALID, row  # noqa: F401
