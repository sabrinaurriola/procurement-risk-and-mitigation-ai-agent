"""Helpers to read table/column names out of sql/001_schema.sql (static schema mapping checks)."""
import re
from pathlib import Path

SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql"
_SKIP = {"CHECK", "CONSTRAINT", "UNIQUE", "FOREIGN", "PRIMARY", "CREATE"}


def schema_columns() -> dict[str, set[str]]:
    sql = SQL_FILE.read_text(encoding="utf-8")
    tables: dict[str, set[str]] = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S):
        cols = set()
        for line in m.group(2).splitlines():
            mm = re.match(r"\s{4}(\w+)\s+\S", line)
            if mm and mm.group(1).upper() not in _SKIP:
                cols.add(mm.group(1))
        tables[m.group(1)] = cols
    return tables
