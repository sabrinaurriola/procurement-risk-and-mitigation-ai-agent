"""Opt-in (RUN_DB_TESTS=1). Verifies every loader INSERT against the live schema using PREPARE inside a READ ONLY
transaction: the server parses and plans each statement (tables, columns, types, conflict targets) but nothing executes
and no data is written."""
import os
import re

import pytest
from dotenv import load_dotenv

from src.db.preflight import connect
from src.ingestion import ec_oilseeds as ec
from src.ingestion import load_synthetic as ls

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to run against Supabase")


def to_positional(sql: str) -> str:
    names: dict[str, int] = {}

    def repl(m):
        name = m.group(1)
        if name not in names:
            names[name] = len(names) + 1
        return f"${names[name]}"

    sql = re.sub(r"%\((\w+)\)s", repl, sql)
    count = iter(range(1, 100))
    return re.sub(r"%s", lambda _m: f"${next(count)}", sql)


STATEMENTS = {f"upsert_{s.table}": ls.upsert_sql(s) for s in ls.SPECS}
STATEMENTS |= {"prices": ec.insert_prices_sql(), "source": ec.SOURCE_SQL, "run": ec.RUN_SQL}
STATEMENTS |= {f"select_{s.table}": ls.select_sql(s) for s in ls.SPECS}


@pytest.mark.parametrize("name", sorted(STATEMENTS))
def test_statement_prepares_against_live_schema(name):
    load_dotenv()
    with connect() as conn:  # read-only session
        conn.execute(f"PREPARE p_{name[:20].replace('-', '_')} AS {to_positional(STATEMENTS[name])}")
        conn.rollback()
