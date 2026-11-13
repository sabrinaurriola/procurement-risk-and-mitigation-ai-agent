-- 002_ops_events.sql — DRAFT FOR REVIEW. NOT APPLIED. Do not run without explicit approval of this SQL, its permissions and the G-OPS plan
-- (docs/day4-architecture.md section 8).
--
-- This file lives in sql/drafts/ on purpose: the migration runner (src/db/migrate.py) only reads sql/NNN_*.sql, so it cannot apply this file.
-- After approval it is moved to sql/002_ops_events.sql and applied with `python -m src.db.migrate` (dry-run first, then --apply).
--
-- Creates: schema "ops", table ops.operational_events, role ops_writer (INSERT on that one table, nothing else), migration record 002.
-- Touches no table in schema "procurement" except the final schema_migrations record.
-- Plain CREATE (no IF NOT EXISTS) on purpose, as in 001: if the schema, table or role already exists, the transaction aborts instead of
-- adopting unknown state.
-- NO password is set here and none may ever be committed. The user sets it out-of-band after the apply (docs/day4-architecture.md section 8).
-- Every CHECK below has a mirror in src/api/events.py (tests/test_ops_events_sql.py fails if they drift apart).
-- NULL trap: a CHECK passes when it evaluates to NULL. The coupling checks therefore use IS NOT DISTINCT FROM / COALESCE so that a missing
-- status can never slip through.

BEGIN;

CREATE SCHEMA ops;

CREATE ROLE ops_writer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
ALTER ROLE ops_writer SET search_path = ops, pg_catalog;
ALTER ROLE ops_writer SET statement_timeout = '5s';
ALTER ROLE ops_writer SET idle_in_transaction_session_timeout = '10s';

CREATE TABLE ops.operational_events (
    event_id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_key         text        NOT NULL,
    occurred_at       timestamptz NOT NULL DEFAULT now(),     -- when the event happened (supplied by the writer)
    recorded_at       timestamptz NOT NULL DEFAULT now(),     -- when the row was stored (an outbox flush may be late)
    request_id        text        NOT NULL,
    component         text        NOT NULL,
    event_type        text        NOT NULL,
    severity          text        NOT NULL DEFAULT 'info',
    mode              text,
    assessment_status text,
    archive_status    text,
    delivery_status   text,
    error_code        text,
    duration_ms       integer,
    input_tokens      integer,
    output_tokens     integer,
    cost_usd          numeric(10,6),                          -- an ESTIMATE; NULL when unknown (mock run, unpriced model, failed run)
    detail            jsonb       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT operational_events_event_key_uq UNIQUE (event_key),
    CONSTRAINT ck_request_id CHECK (request_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    CONSTRAINT ck_event_key CHECK (event_key ~ '^[A-Za-z0-9_.:|-]{1,200}$' AND starts_with(event_key, request_id || '|')),
    CONSTRAINT ck_component CHECK (component IN ('api', 'n8n')),
    CONSTRAINT ck_event_type CHECK (event_type IN (
        'assessment.received', 'assessment.replayed', 'assessment.timeout', 'assessment.interrupted', 'assessment.finished',
        'research.source', 'validation.removed', 'archive.stored', 'archive.failed', 'delivery.sent', 'delivery.failed')),
    CONSTRAINT ck_severity CHECK (severity IN ('info', 'warning', 'error')),
    CONSTRAINT ck_mode CHECK (mode IN ('live', 'mock')),
    CONSTRAINT ck_assessment_status CHECK (assessment_status IN ('completed', 'partial', 'failed')),
    CONSTRAINT ck_archive_status CHECK (archive_status IN ('incomplete', 'stored', 'failed')),
    CONSTRAINT ck_delivery_status CHECK (delivery_status IN ('sent', 'failed')),
    CONSTRAINT ck_error_code CHECK (error_code ~ '^[A-Z_]{1,64}$'),
    CONSTRAINT ck_duration CHECK (duration_ms >= 0),
    CONSTRAINT ck_tokens CHECK (input_tokens >= 0 AND output_tokens >= 0),
    CONSTRAINT ck_cost CHECK (cost_usd >= 0),
    CONSTRAINT ck_detail CHECK (
        jsonb_typeof(detail) = 'object'
        AND octet_length(detail::text) <= 4096
        AND (detail - ARRAY['source', 'status_class', 'status', 'count', 'kinds', 'llm_calls', 'tool_calls', 'research_rounds', 'warnings',
                            'missing_data', 'benchmark_status', 'financial_status', 'cost_estimated', 'http_wait_ms',
                            'notion_page_id', 'report_sha256', 'blocks_total', 'blocks_appended', 'duplicate_pages', 'retry_count',
                            'notice_kind', 'archive_failed']::text[]) = '{}'::jsonb),
    -- value-level checks for the TOP-LEVEL TEXT-valued detail keys only: fixed vocabularies and fixed shapes. A key that is absent passes; a key
    -- that is present must be a JSON string in the permitted form (JSON null and numbers fail). This is NOT a guarantee that arbitrary nested
    -- text cannot be stored: the nested values of kinds and tool_calls, and all numeric values, are enforced by the Python model only. In SQL
    -- they are limited just by the 22-key top-level allow-list, the 4096-byte cap and the sensitive-marker check below.
    CONSTRAINT ck_detail_values CHECK (
        ((detail->'source') IS NULL OR (jsonb_typeof(detail->'source') = 'string'
            AND detail->>'source' IN ('tavily', 'gdelt', 'policy', 'filtering', 'other')))
        AND ((detail->'status_class') IS NULL OR (jsonb_typeof(detail->'status_class') = 'string'
            AND detail->>'status_class' IN ('ok', 'partial', 'unavailable', 'not_configured', 'not_used', 'not_run', 'fallback', 'filtered', 'other')))
        AND ((detail->'status') IS NULL OR (jsonb_typeof(detail->'status') = 'string'
            AND detail->>'status' ~ '^[A-Z][A-Z0-9_]{1,39}$'))
        AND ((detail->'benchmark_status') IS NULL OR (jsonb_typeof(detail->'benchmark_status') = 'string'
            AND detail->>'benchmark_status' IN ('available', 'stale', 'unavailable')))
        AND ((detail->'financial_status') IS NULL OR (jsonb_typeof(detail->'financial_status') = 'string'
            AND detail->>'financial_status' IN ('available', 'unavailable')))
        AND ((detail->'notice_kind') IS NULL OR (jsonb_typeof(detail->'notice_kind') = 'string'
            AND detail->>'notice_kind' IN ('report', 'operator')))
        AND ((detail->'notion_page_id') IS NULL OR (jsonb_typeof(detail->'notion_page_id') = 'string'
            AND detail->>'notion_page_id' ~ '^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$'))
        AND ((detail->'report_sha256') IS NULL OR (jsonb_typeof(detail->'report_sha256') = 'string'
            AND detail->>'report_sha256' ~ '^[0-9a-f]{64}$'))),
    -- the same marker list as SENSITIVE_MARKERS in src/api/events.py (tests compare them): nothing that looks like a URL, address, credential or
    -- connection string may appear anywhere in the serialised detail, including inside nested keys.
    CONSTRAINT ck_detail_no_sensitive CHECK (
        detail::text !~* '(://|@|bearer|api[_-]?key|passw(or)?d|secret|token|authorization|sk-[a-z0-9]|postgres(ql)?:)'),

    -- coupling rules (mirrors OperationalEvent._coupling)
    CONSTRAINT ck_component_matches_type CHECK ((component = 'n8n') = (event_type ~ '^(archive|delivery)[.]')),
    CONSTRAINT ck_assessment_status_only_on_finished CHECK ((event_type = 'assessment.finished') = (assessment_status IS NOT NULL)),
    CONSTRAINT ck_archive_status_matches CHECK (
        (event_type = 'archive.stored'  AND archive_status IS NOT DISTINCT FROM 'stored')
        OR (event_type = 'archive.failed' AND COALESCE(archive_status IN ('incomplete', 'failed'), false))
        OR (event_type NOT LIKE 'archive.%' AND archive_status IS NULL)),
    CONSTRAINT ck_delivery_status_matches CHECK (
        (event_type = 'delivery.sent'   AND delivery_status IS NOT DISTINCT FROM 'sent')
        OR (event_type = 'delivery.failed' AND delivery_status IS NOT DISTINCT FROM 'failed')
        OR (event_type NOT LIKE 'delivery.%' AND delivery_status IS NULL)),
    CONSTRAINT ck_metrics_placement CHECK (
        (duration_ms IS NULL OR event_type IN ('assessment.finished', 'assessment.timeout'))
        AND ((input_tokens IS NULL AND output_tokens IS NULL AND cost_usd IS NULL) OR event_type = 'assessment.finished'))
);

CREATE INDEX ix_operational_events_request ON ops.operational_events (request_id, occurred_at);

-- Row-level security: defence in depth. The owner (postgres) bypasses it; ops_writer may only INSERT; nobody else has any policy.
ALTER TABLE ops.operational_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY ops_writer_insert ON ops.operational_events FOR INSERT TO ops_writer WITH CHECK (true);

-- Privileges: start from nothing, then grant the minimum.
REVOKE ALL ON SCHEMA ops FROM PUBLIC;
REVOKE ALL ON ops.operational_events FROM PUBLIC;
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP   -- Supabase API roles; absent on plain PostgreSQL
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA ops FROM %I', r);
            EXECUTE format('REVOKE ALL ON ops.operational_events FROM %I', r);
        END IF;
    END LOOP;
END $$;
GRANT USAGE ON SCHEMA ops TO ops_writer;
GRANT INSERT ON ops.operational_events TO ops_writer;     -- no SELECT, UPDATE, DELETE, TRUNCATE, no RETURNING; nothing on schema procurement

INSERT INTO procurement.schema_migrations (version) VALUES ('002');

COMMIT;
