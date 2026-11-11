"""Database integration tests. Opt-in: RUN_DB_TESTS=1. Every statement runs inside a transaction that is ROLLED BACK;
no data is ever committed. Requires the 001 migration to be applied."""
import os
import uuid

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import errors as E

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to run against Supabase")

KEY = "EC_OILSEEDS|Crude sunflower oil|Standard|ES|Average|DEPPROC||EUR|tonne"


@pytest.fixture
def cur():
    load_dotenv()
    conn = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15, sslmode="require", autocommit=False)
    c = conn.cursor()
    c.execute("SET LOCAL search_path = procurement, extensions, public")
    c.execute("INSERT INTO ingredients VALUES ('t_oil','Sunflower oil','tonne',%s,NULL),('t_wheat','Wheat','tonne',NULL,NULL)", (KEY,))
    c.execute("INSERT INTO suppliers (supplier_id,name,country,approval_status) VALUES ('T-S1','Test (fictional)','ES','approved')")
    c.execute("INSERT INTO supplier_ingredients VALUES ('T-S1','t_oil')")
    yield c
    conn.rollback()  # nothing is ever committed
    conn.close()


def expect(cur, exc, sql, params=()):
    cur.execute("SAVEPOINT t")
    with pytest.raises(exc):
        cur.execute(sql, params)
    cur.execute("ROLLBACK TO SAVEPOINT t")


FIXED = "INSERT INTO contracts (contract_id,supplier_id,ingredient_code,contract_type,currency,committed_monthly_volume_t,start_date,end_date,unit_price,index_series_key,base_price,index_base_value,pass_through,fixed_premium) VALUES (%s,'T-S1','t_oil','fixed','EUR',10,'2026-01-01','2026-12-31',%s,%s,%s,%s,%s,%s)"
INDEX = "INSERT INTO contracts (contract_id,supplier_id,ingredient_code,contract_type,currency,committed_monthly_volume_t,start_date,end_date,unit_price,index_series_key,base_price,index_base_value,pass_through,fixed_premium) VALUES (%s,'T-S1','t_oil','index_linked','EUR',10,'2026-01-01','2026-12-31',%s,%s,%s,%s,%s,%s)"


def test_valid_fixed_and_index_contracts_accepted(cur):
    cur.execute("SELECT count(*) FROM contracts")
    before = cur.fetchone()[0]
    cur.execute(FIXED, ("C1", 1520, None, None, None, None, None))
    cur.execute(INDEX, ("C2", None, KEY, 1600, 1400, 0.8, 0))
    cur.execute("SELECT count(*) FROM contracts")
    assert cur.fetchone()[0] == before + 2


@pytest.mark.parametrize("price", [None, 0, -1])
def test_invalid_fixed_price_rejected(cur, price):
    expect(cur, E.CheckViolation, FIXED, ("C1", price, None, None, None, None, None))


def test_fixed_with_index_fields_rejected(cur):
    expect(cur, E.CheckViolation, FIXED, ("C1", 1520, KEY, None, None, None, None))


@pytest.mark.parametrize("i", range(5))
def test_incomplete_index_contract_rejected(cur, i):
    vals = [KEY, 1600, 1400, 0.8, 0]
    vals[i] = None
    expect(cur, E.CheckViolation, INDEX, ("C1", None, *vals))


@pytest.mark.parametrize("pt", [-0.1, 1.2])
def test_index_pass_through_out_of_range_rejected(cur, pt):
    expect(cur, E.CheckViolation, INDEX, ("C1", None, KEY, 1600, 1400, pt, 0))


def test_index_with_unit_price_rejected(cur):
    expect(cur, E.CheckViolation, INDEX, ("C1", 1500, KEY, 1600, 1400, 0.8, 0))


def test_supplier_must_supply_ingredient(cur):
    expect(cur, E.ForeignKeyViolation,
           FIXED.replace("'t_oil'", "'t_wheat'"), ("C1", 265, None, None, None, None, None))


def test_unknown_supplier_rejected(cur):
    expect(cur, E.ForeignKeyViolation, FIXED.replace("'T-S1'", "'NOPE'"), ("C1", 1520, None, None, None, None, None))


def test_synthetic_flag_cannot_be_false(cur):
    expect(cur, E.CheckViolation, "INSERT INTO suppliers (supplier_id,name,country,approval_status,is_synthetic) VALUES ('T-S2','x','ES','approved',false)")
    expect(cur, E.CheckViolation, FIXED.replace("VALUES (%s", "VALUES (%s").replace("fixed_premium)", "fixed_premium,is_synthetic)").replace("%s)", "%s,false)"),
           ("C9", 1520, None, None, None, None, None))
    expect(cur, E.CheckViolation, "INSERT INTO demand_forecasts (ingredient_code,period_month,forecast_volume_t,forecast_version,generated_on,is_synthetic) VALUES ('t_wheat','2026-12-01',1,'v','2026-11-10',false)")
    expect(cur, E.CheckViolation, "INSERT INTO policy_chunks (doc_id,doc_title,doc_type,chunk_index,content,content_hash,is_synthetic) VALUES ('d','t','policy',0,'c',repeat('a',64),false)")


def test_forecast_rules(cur):
    ins = "INSERT INTO demand_forecasts (ingredient_code,period_month,forecast_volume_t,forecast_version,generated_on) VALUES ('t_wheat',%s,%s,'v','2026-11-10')"
    expect(cur, E.CheckViolation, ins, ("2026-12-15", 10))
    expect(cur, E.CheckViolation, ins, ("2026-12-01", 0))
    cur.execute(ins, ("2026-12-01", 10))
    expect(cur, E.UniqueViolation, ins, ("2026-12-01", 11))


def test_commodity_price_rules(cur):
    cur.execute("INSERT INTO data_sources (name,publisher,file_name,sha256) VALUES ('TEST_ONLY','EC','constraint_test.xlsx',repeat('a',64)) RETURNING source_id")
    sid = cur.fetchone()[0]
    cur.execute("INSERT INTO ingest_runs (source_id,rows_read,inserted,skipped_existing,flagged,status) VALUES (%s,0,0,0,0,'applied') RETURNING run_id", (sid,))
    rid = cur.fetchone()[0]
    ins = ("INSERT INTO commodity_prices (source_id,run_id,series_key,product,member_state,market_stage,currency,unit,price,period_start,period_end,source_file,source_sheet,source_row,occurrence_no,row_hash,quality_flags,raw_values) "
           "VALUES (%s,%s,%s,'p','ES','DEPPROC','EUR','tonne',%s,'2030-01-07','2030-01-13','f','s',2,%s,repeat('b',64),%s,'{}')")
    expect(cur, E.CheckViolation, ins, (sid, rid, KEY, None, 1, []))               # NULL price needs a flag
    expect(cur, E.CheckViolation, ins, (sid, rid, KEY, -5, 1, []))                 # non-positive price
    expect(cur, E.CheckViolation, ins, (sid, rid, KEY.replace("|EUR|", "|USD|"), 1450, 1, []))  # key/currency mismatch
    cur.execute(ins, (sid, rid, KEY, 1450, 1, []))
    cur.execute(ins, (sid, rid, KEY, None, 2, ["NON_NUMERIC_PRICE"]))              # flagged missing price accepted; duplicate period kept
    expect(cur, E.UniqueViolation, ins, (sid, rid, KEY, 1450, 1, []))              # same occurrence rejected


TABLES = ("commodity_prices", "contracts", "suppliers", "supplier_ingredients", "demand_forecasts", "ingredients", "policy_chunks",
          "data_sources", "ingest_runs")


def _counts():
    with psycopg.connect(os.environ["DATABASE_URL"], sslmode="require") as conn:
        return {t: conn.execute(f"SELECT count(*) FROM procurement.{t}").fetchone()[0] for t in TABLES}


def test_rolled_back_test_inserts_leave_row_counts_unchanged():
    load_dotenv()
    before = _counts()
    conn = psycopg.connect(os.environ["DATABASE_URL"], sslmode="require", autocommit=False)
    try:
        c = conn.cursor()
        c.execute("SET LOCAL search_path = procurement, extensions, public")
        c.execute("INSERT INTO ingredients VALUES ('t_oil','Test oil','tonne',NULL,NULL)")
        c.execute("INSERT INTO suppliers (supplier_id,name,country,approval_status) VALUES ('T-S1','Test (fictional)','ES','approved')")
        c.execute("SELECT count(*) FROM suppliers")
        assert c.fetchone()[0] == before["suppliers"] + 1          # visible inside the transaction
    finally:
        conn.rollback()
        conn.close()
    assert _counts() == before                                    # nothing persisted
