-- 001_schema.sql — DRAFT FOR REVIEW. NOT APPLIED. Do not run without explicit approval.
-- Creates: schema "procurement", pgvector extension (in the Supabase "extensions" schema), 10 tables:
--   schema_migrations, data_sources, ingest_runs, commodity_prices, ingredients,
--   suppliers, supplier_ingredients, contracts, demand_forecasts, policy_chunks.
-- Preflight (read-only, 10 Nov 2026): pgvector 0.8.2 available but NOT installed; schema "extensions" exists
-- and is usable; schema "procurement" does not exist. Tables use plain CREATE TABLE (no IF NOT EXISTS) on purpose:
-- the runner (src/db/migrate.py) skips recorded versions and aborts on unrecorded objects.
-- Synthetic rows (suppliers, contracts, forecasts, policy chunks) must carry is_synthetic = true.
-- The schema is kept out of the public schema so Supabase's auto-generated REST API does not expose it.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
CREATE SCHEMA IF NOT EXISTS procurement;
SET LOCAL search_path = procurement, extensions, public;

CREATE TABLE schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ───────────── External market data (EC workbooks) ─────────────
CREATE TABLE data_sources (
    source_id    SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,                 -- e.g. EC_OILSEEDS, EC_CEREALS, FR_CEREALS
    publisher    TEXT NOT NULL,
    file_name    TEXT NOT NULL,
    sha256       CHAR(64) NOT NULL,
    url          TEXT,
    retrieved_at DATE,
    licence_note TEXT,
    UNIQUE (file_name, sha256)
);

CREATE TABLE ingest_runs (
    run_id           SERIAL PRIMARY KEY,
    source_id        INT NOT NULL REFERENCES data_sources(source_id),
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_read        INT NOT NULL,
    inserted         INT NOT NULL,
    skipped_existing INT NOT NULL,
    flagged          INT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('applied', 'failed'))
);

CREATE TABLE commodity_prices (
    id             BIGSERIAL PRIMARY KEY,
    source_id      INT NOT NULL REFERENCES data_sources(source_id),
    run_id         INT NOT NULL REFERENCES ingest_runs(run_id),
    -- series_key = source|product|product_type|member_state|market|market_stage|quotation_basis|currency|unit
    series_key     TEXT NOT NULL,
    product        TEXT NOT NULL,
    product_type   TEXT,
    member_state   CHAR(2) NOT NULL,
    market         TEXT,
    market_stage   TEXT NOT NULL,
    quotation_basis TEXT,                        -- e.g. place of quotation or FOB/Rendu basis
    currency       CHAR(3) NOT NULL,
    unit           TEXT NOT NULL,                -- normalised, e.g. 'tonne'
    unit_raw       TEXT,
    price          NUMERIC(12,4),                -- NULL only when quality_flags explains why
    price_raw      TEXT,
    period_start   DATE NOT NULL,
    period_end     DATE NOT NULL,
    marketing_year TEXT,
    week_in_my     INT,
    source_file    TEXT NOT NULL,
    source_sheet   TEXT NOT NULL,
    source_row     INT NOT NULL,
    occurrence_no  INT NOT NULL DEFAULT 1,       -- distinguishes repeated records of the same series+period
    row_hash       CHAR(64) NOT NULL,
    quality_flags  TEXT[] NOT NULL DEFAULT '{}',
    raw_values     JSONB NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (series_key, period_start, occurrence_no),
    CHECK (price IS NULL OR price > 0),
    CHECK (period_end >= period_start),
    CHECK (price IS NOT NULL OR cardinality(quality_flags) > 0),
    CHECK (series_key LIKE '%|' || currency || '|' || unit)
);
CREATE INDEX commodity_prices_series_period_idx ON commodity_prices (series_key, period_start DESC);

-- ───────────── Synthetic company data (Bakery Foods Inc., fictional) ─────────────
CREATE TABLE ingredients (
    ingredient_code     TEXT PRIMARY KEY,        -- sunflower_oil, wheat
    display_name        TEXT NOT NULL,
    unit                TEXT NOT NULL DEFAULT 'tonne',
    benchmark_series_key TEXT,                   -- logical link to commodity_prices.series_key
    benchmark_note      TEXT
);

CREATE TABLE suppliers (
    supplier_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    country         CHAR(2) NOT NULL,
    approval_status TEXT NOT NULL CHECK (approval_status IN ('approved', 'conditional', 'unverified', 'blocked')),
    notes           TEXT,
    is_synthetic    BOOLEAN NOT NULL DEFAULT true CHECK (is_synthetic)
);

CREATE TABLE supplier_ingredients (
    supplier_id     TEXT NOT NULL REFERENCES suppliers(supplier_id),
    ingredient_code TEXT NOT NULL REFERENCES ingredients(ingredient_code),
    PRIMARY KEY (supplier_id, ingredient_code)
);

CREATE TABLE contracts (
    contract_id                TEXT PRIMARY KEY,
    supplier_id                TEXT NOT NULL,
    ingredient_code            TEXT NOT NULL,
    contract_type              TEXT NOT NULL CHECK (contract_type IN ('fixed', 'index_linked')),
    currency                   CHAR(3) NOT NULL,
    committed_monthly_volume_t NUMERIC(12,3) NOT NULL CHECK (committed_monthly_volume_t > 0),
    start_date                 DATE NOT NULL,
    end_date                   DATE NOT NULL,
    unit_price                 NUMERIC(12,4),    -- fixed
    index_series_key           TEXT,             -- index_linked
    base_price                 NUMERIC(12,4),
    index_base_value           NUMERIC(12,4),
    pass_through               NUMERIC(5,4),
    fixed_premium              NUMERIC(12,4),
    notes                      TEXT,
    is_synthetic               BOOLEAN NOT NULL DEFAULT true CHECK (is_synthetic),
    FOREIGN KEY (supplier_id, ingredient_code) REFERENCES supplier_ingredients (supplier_id, ingredient_code),
    CHECK (end_date > start_date),
    -- Every mandatory field is guarded by an explicit IS NOT NULL, so a NULL can never make the expression
    -- UNKNOWN (which a CHECK would accept). Exactly one branch must be TRUE.
    CONSTRAINT contracts_type_fields_ck CHECK (
        (
            contract_type = 'fixed'
            AND unit_price IS NOT NULL AND unit_price > 0
            AND index_series_key IS NULL AND base_price IS NULL AND index_base_value IS NULL
            AND pass_through IS NULL AND fixed_premium IS NULL
        )
        OR
        (
            contract_type = 'index_linked'
            AND unit_price IS NULL
            AND index_series_key IS NOT NULL AND length(btrim(index_series_key)) > 0
            AND base_price IS NOT NULL AND base_price > 0
            AND index_base_value IS NOT NULL AND index_base_value > 0
            AND pass_through IS NOT NULL AND pass_through >= 0 AND pass_through <= 1
            AND fixed_premium IS NOT NULL
        )
    )
);

CREATE TABLE demand_forecasts (
    forecast_id       SERIAL PRIMARY KEY,
    ingredient_code   TEXT NOT NULL REFERENCES ingredients(ingredient_code),
    period_month      DATE NOT NULL CHECK (EXTRACT(DAY FROM period_month) = 1),
    forecast_volume_t NUMERIC(12,3) NOT NULL CHECK (forecast_volume_t > 0),
    forecast_version  TEXT NOT NULL,
    generated_on      DATE NOT NULL,
    is_synthetic      BOOLEAN NOT NULL DEFAULT true CHECK (is_synthetic),
    UNIQUE (ingredient_code, period_month, forecast_version)
);

-- ───────────── Synthetic documents for RAG ─────────────
CREATE TABLE policy_chunks (
    chunk_id         SERIAL PRIMARY KEY,
    doc_id           TEXT NOT NULL,
    doc_title        TEXT NOT NULL,
    doc_type         TEXT NOT NULL CHECK (doc_type IN ('policy', 'supplier_assessment')),
    chunk_index      INT NOT NULL,
    content          TEXT NOT NULL,
    content_hash     CHAR(64) NOT NULL,
    ingredient_codes TEXT[] NOT NULL DEFAULT '{}',
    embedding        extensions.vector(1536),   -- NULL until embeddings are generated (paid call, separately approved)
    embedding_model  TEXT,
    is_synthetic     BOOLEAN NOT NULL DEFAULT true CHECK (is_synthetic),
    UNIQUE (doc_id, chunk_index)
);

INSERT INTO schema_migrations (version) VALUES ('001');

COMMIT;
