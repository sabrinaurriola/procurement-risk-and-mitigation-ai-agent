"""Generates the two importable n8n workflows of the Gradio + n8n design (docs/day4-architecture.md section 22):

    python n8n/build_report_workflows.py        ->  n8n/procurement-report-archive.json, n8n/procurement-report-email.json

MVP-sized on purpose (see section 22.5 for what was cut and why). The JSON files are the artifacts; this script only keeps the Code-node
JavaScript readable and the wiring consistent. A test (tests/test_n8n_report_workflows.py) fails if the committed JSON differs from its output.
Nothing here contains a credential, a Notion id or a recipient address: they are placeholders in each workflow's `Config` node and n8n credentials."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent
NOTION_VERSION = "2022-06-28"
NS = uuid.UUID("6f1c2d3e-0000-4000-8000-00000000d4a4")


def nid(workflow: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{workflow}/{name}"))


class Flow:
    def __init__(self, key: str, name: str):
        self.key, self.name = key, name
        self.nodes: list[dict] = []
        self.links: dict[str, dict[int, list[dict]]] = {}

    def add(self, name: str, type_: str, version: float | int, params: dict, pos: tuple[int, int], **extra) -> str:
        node = {"parameters": params, "id": nid(self.key, name), "name": name, "type": type_, "typeVersion": version, "position": list(pos)}
        node.update(extra)
        self.nodes.append(node)
        return name

    def link(self, src: str, dst: str, out: int = 0) -> None:
        self.links.setdefault(src, {}).setdefault(out, []).append({"node": dst, "type": "main", "index": 0})

    def doc(self) -> dict:
        conns = {src: {"main": [outs.get(i, []) for i in range(max(outs) + 1)]} for src, outs in self.links.items()}
        return {"name": self.name, "nodes": self.nodes, "connections": conns, "active": False,
                "settings": {"executionOrder": "v1", "saveDataSuccessExecution": "none", "saveDataErrorExecution": "all",
                             "saveManualExecutions": False, "executionTimeout": 300},
                "versionId": nid(self.key, "version"), "meta": {"templateCredsSetupCompleted": False}, "pinData": {}, "tags": []}


# ───────────────────────── node factories ─────────────────────────
def webhook(f: Flow, name: str, path: str, pos) -> str:
    return f.add(name, "n8n-nodes-base.webhook", 2, {"httpMethod": "POST", "path": path, "authentication": "headerAuth",
                                                      "responseMode": "responseNode", "options": {}}, pos, webhookId=nid(f.key, "webhook"))


def config(f: Flow, pos, extra: list[tuple[str, str]] = ()) -> str:
    rows = [("notion_version", NOTION_VERSION), ("assessments_db_id", "REPLACE_WITH_ASSESSMENTS_DATABASE_ID"),
            ("errors_db_id", "REPLACE_WITH_ERRORS_DATABASE_ID"), *extra]
    return f.add("Config", "n8n-nodes-base.set", 3.4, {"assignments": {"assignments": [
        {"id": nid(f.key, f"cfg-{k}"), "name": k, "value": v, "type": "string"} for k, v in rows]}, "options": {}}, pos,
        notes="Set the Notion database ids here (and the recipient in the e-mail workflow) after import. Placeholders make the workflow refuse to run.")


def code(f: Flow, name: str, js: str, pos, **extra) -> str:
    return f.add(name, "n8n-nodes-base.code", 2, {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": js.strip() + "\n"}, pos, **extra)


def if_bool(f: Flow, name: str, expr: str, pos) -> str:
    return f.add(name, "n8n-nodes-base.if", 2.2, {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                                                               "conditions": [{"id": nid(f.key, f"if-{name}"), "leftValue": expr, "rightValue": True,
                                                                               "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                                                               "combinator": "and"}, "options": {}}, pos)


def switch(f: Flow, name: str, field: str, values: list[str], pos) -> str:
    rules = [{"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                             "conditions": [{"id": nid(f.key, f"sw-{name}-{v}"), "leftValue": f"={{{{ $json.{field} }}}}", "rightValue": v,
                                             "operator": {"type": "string", "operation": "equals"}}], "combinator": "and"},
              "renameOutput": True, "outputKey": v} for v in values]
    return f.add(name, "n8n-nodes-base.switch", 3.2, {"rules": {"values": rules}, "options": {}}, pos)


def notion(f: Flow, name: str, method: str, url: str, pos, body: str | None = None, qs: dict | None = None, retry: bool = False,
           on_error: str = "continueErrorOutput", pagination: bool = False) -> str:
    p: dict = {"method": method, "url": url, "authentication": "predefinedCredentialType", "nodeCredentialType": "notionApi",
               "sendHeaders": True, "headerParameters": {"parameters": [{"name": "Notion-Version", "value": "={{ $('Config').first().json.notion_version }}"}]},
               "options": {"timeout": 30000}}
    if body:
        p.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
    if qs:
        p.update({"sendQuery": True, "queryParameters": {"parameters": [{"name": k, "value": v} for k, v in qs.items()]}})
    if pagination:   # follow Notion's cursor until has_more is false; one output item per page of results
        p["options"]["pagination"] = {"pagination": {"paginationMode": "updateAParameterInEachRequest",
                                                     "parameters": {"parameters": [{"type": "qs", "name": "start_cursor", "value": "={{ $response.body.next_cursor }}"}]},
                                                     "paginationCompleteWhen": "other", "completeExpression": "={{ $response.body.has_more !== true }}",
                                                     "limitPagesFetched": True, "maxRequests": 30, "requestInterval": 350}}
    extra: dict = {"onError": on_error}
    if retry:   # bounded retries ONLY for idempotent calls (reads and absolute PATCH updates)
        extra.update({"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000})
    else:
        extra["retryOnFail"] = False
    return f.add(name, "n8n-nodes-base.httpRequest", 4.2, p, pos, **extra)


def sticky(f: Flow, text: str, pos, size=(560, 300)) -> None:
    f.add("Setup notes", "n8n-nodes-base.stickyNote", 1, {"content": text, "height": size[1], "width": size[0]}, pos)


FIN = "$('Finalize outcome').first().json"


def tail(f: Flow, x: int, y: int) -> None:
    """Finalize outcome -> Respond (the caller gets the answer first) -> Has error? -> Build error record -> Log error (best effort, after the response)."""
    f.add("Respond", "n8n-nodes-base.respondToWebhook", 1.1,
          {"respondWith": "json", "responseBody": f"={{{{ JSON.stringify({FIN}.response) }}}}", "options": {"responseCode": f"={{{{ {FIN}.http_status }}}}"}}, (x + 240, y))
    if_bool(f, "Has error?", f"={{{{ !!{FIN}.error }}}}", (x + 480, y))
    code(f, "Build error record", ERROR_RECORD_JS, (x + 720, y))
    notion(f, "Log error", "POST", "https://api.notion.com/v1/pages", (x + 960, y), body="={{ JSON.stringify($json.notion_body) }}", on_error="continueRegularOutput")
    f.link("Finalize outcome", "Respond")
    f.link("Respond", "Has error?")
    f.link("Has error?", "Build error record", 0)
    f.link("Build error record", "Log error")


# ───────────────────────── shared JavaScript ─────────────────────────
ERROR_RECORD_JS = r'''
// Best-effort operational error record, written AFTER the answer was sent. Only codes, ids and counters: never exception text, report text or PDF content.
const o = $('Finalize outcome').first().json;
const e = o.error;
const cfg = $('Config').first().json;
const rid = o.response && typeof o.response.request_id === 'string' && o.response.request_id ? o.response.request_id : null;
const rt = s => (s ? { rich_text: [{ text: { content: String(s).slice(0, 300) } }] } : { rich_text: [] });
const properties = {
  'Name': { title: [{ text: { content: (e.code + ' ' + (rid || '')).trim() } }] },
  'Timestamp': { date: { start: new Date().toISOString() } },
  'Request ID': rt(rid),
  'Workflow': { select: { name: e.workflow } },
  'Component': { select: { name: e.component } },
  'Error code': rt(e.code),
  'Severity': { select: { name: e.severity } },
  'Status': { select: { name: 'open' } },
  'Execution ID': rt($execution.id),
  'Retry count': { number: Number.isInteger(e.retry_count) ? e.retry_count : 0 },
  'Occurred during': { select: { name: e.during } },
  'Error ID': rt(('E-' + e.workflow + '-' + (rid || 'noid') + '-' + e.code).slice(0, 120)),
};
return [{ json: { notion_body: { parent: { database_id: cfg.errors_db_id }, properties } } }];
'''

# ───────────────────────── Workflow A: archive ─────────────────────────
ARCHIVE_VALIDATE_JS = r'''
const wh = $('Archive Webhook').first().json;
const body = (wh.body && typeof wh.body === 'object' && !Array.isArray(wh.body)) ? wh.body : {};
const cfg = $('Config').first().json;
const isStr = v => typeof v === 'string';
const RID = /^[A-Za-z0-9_-]{1,64}$/, HEX64 = /^[0-9a-f]{64}$/, DATE = /^\d{4}-\d{2}-\d{2}$/;
const rid = isStr(body.request_id) && RID.test(body.request_id) ? body.request_id : null;
const m = body.metadata || {};
const errors = [];
if (body.action !== 'archive') errors.push('INVALID_ACTION');
if (!rid) errors.push('INVALID_REQUEST_ID');
if (!['completed', 'partial'].includes(body.assessment_status)) errors.push('INVALID_ASSESSMENT_STATUS');
if (!['mock', 'live'].includes(body.mode)) errors.push('INVALID_MODE');
if (!isStr(body.report_sha256) || !HEX64.test(body.report_sha256)) errors.push('REPORT_HASH_INVALID');
if (m.ingredient !== 'sunflower_oil') errors.push('INGREDIENT_UNSUPPORTED');
if (!Number.isInteger(m.horizon_months)) errors.push('HORIZON_INVALID');
if (!isStr(m.assessment_date) || !DATE.test(m.assessment_date)) errors.push('ASSESSMENT_DATE_INVALID');
if (!Array.isArray(body.blocks) || !body.blocks.length || body.total_blocks !== body.blocks.length) errors.push('BLOCKS_INVALID');
const dbOk = v => isStr(v) && /^[0-9a-f]{32}$/i.test(v.replace(/-/g, ''));
const configBad = !dbOk(cfg.assessments_db_id) || !DATE.test(String(cfg.notion_version || ''));
const json = { valid: errors.length === 0 && !configBad, request_id: rid, expected_blocks: Number.isInteger(body.total_blocks) ? body.total_blocks : 0, body };
if (!json.valid) json.failure = { code: configBad ? 'CONFIG_NOT_SET' : 'INVALID_PAYLOAD', retry_count: 0, details: errors };
return [{ json }];
'''

ARCHIVE_DECIDE_JS = r'''
// Oldest page with this Request ID wins; a page is never created twice and never overwritten.
const v = $('Validate').first().json;
const body = v.body;
const cfg = $('Config').first().json;
const results = $input.first().json.results || [];
const text = (p, name) => ((p.properties && p.properties[name] && p.properties[name].rich_text) || []).map(r => r.plain_text ?? '').join('');
if (results.length === 0) {
  const rt = s => ({ rich_text: [{ text: { content: s } }] });
  const m = body.metadata;
  const properties = {
    'Name': { title: [{ text: { content: '[' + body.mode.toUpperCase() + '] Procurement risk assessment ' + v.request_id } }] },
    'Request ID': rt(v.request_id),
    'Ingredient': { select: { name: m.ingredient } },
    'Assessment date': { date: { start: m.assessment_date } },
    'Horizon months': { number: m.horizon_months },
    'Mode': { select: { name: body.mode } },
    'Assessment status': { select: { name: body.assessment_status } },
    'Archive status': { select: { name: 'incomplete' } },
    'Report SHA-256': rt(body.report_sha256),
    'Total blocks': { number: body.total_blocks },
    'Email status': { select: { name: 'not_requested' } },
  };
  return [{ json: { route: 'create', notion_body: { parent: { database_id: cfg.assessments_db_id }, properties } } }];
}
const page = results[0];
const pageSha = text(page, 'Report SHA-256');
if (pageSha && pageSha !== body.report_sha256) return [{ json: { route: 'fail', failure: { code: 'ARCHIVE_CONTENT_MISMATCH', retry_count: 0, page_url: page.url, echo: pageSha } } }];
return [{ json: { route: 'ready', page_id: page.id, page_url: page.url, created: false } }];
'''

ARCHIVE_PAGE_READY_JS = r'''
const j = $input.first().json;
if (j.object === 'page') return [{ json: { page_id: j.id, page_url: j.url, created: true } }];
return [{ json: { page_id: j.page_id, page_url: j.page_url, created: false } }];
'''

ARCHIVE_COMPARE_JS = r'''
// Ground truth is what Notion returns now: every decision (append, finish, stop) comes from a fresh listing of the page body.
const v = $('Validate').first().json;
const blocks = v.body.blocks;
const total = blocks.length;
const pr = $('Page ready').first().json;
const existing = [];
for (const it of $input.all()) for (const b of (it.json.results || [])) existing.push(b);
const text = b => ((b[b.type] || {}).rich_text || []).map(r => r.plain_text ?? (r.text && r.text.content) ?? '').join('');
const sig = b => b.type + '\u0001' + text(b);
const round = $runIndex + 1;
let bad = existing.length > total ? total : -1;
for (let i = 0; i < Math.min(existing.length, total) && bad < 0; i++) if (sig(existing[i]) !== sig(blocks[i])) bad = i;
const base = { existing: existing.length, round, page_url: pr.page_url, page_id: pr.page_id };
const stop = code => ({ ...base, action: 'stop', failure: { code, retry_count: round - 1, found: existing.length, page_url: pr.page_url } });
if (bad >= 0) return [{ json: stop('ARCHIVE_CONTENT_MISMATCH') }];
if (existing.length === total) return [{ json: { ...base, action: 'done' } }];
if (round > Math.ceil(total / 100) + 1) return [{ json: stop('ARCHIVE_INCOMPLETE') }];
return [{ json: { ...base, action: 'append', append_body: { children: blocks.slice(existing.length, existing.length + 100) } } }];
'''

ARCHIVE_FAIL_NOTION_JS = r'''
// Any Notion call that failed. Before the page exists it is a lookup/create problem; afterwards the archive is simply not complete (the caller can resume).
const pr = $('Page ready').isExecuted ? $('Page ready').first().json : null;
const cmp = $('Compare').isExecuted ? $('Compare').last().json : null;
if (!pr) return [{ json: { failure: { code: 'NOTION_PAGE_FAILED', retry_count: 3 } } }];
return [{ json: { failure: { code: 'ARCHIVE_INCOMPLETE', retry_count: 0, found: cmp ? cmp.existing : 0, page_url: pr.page_url } } }];
'''

ARCHIVE_FINALIZE_JS = r'''
const v = $('Validate').first().json;
const rid = v.request_id ?? null;
const j = $input.first().json;
const MAP = {
  INVALID_PAYLOAD:          { http: 422, status: 'failed',     component: 'validation', during: 'validation', severity: 'warning' },
  CONFIG_NOT_SET:           { http: 503, status: 'failed',     component: 'workflow',   during: 'validation', severity: 'critical' },
  NOTION_PAGE_FAILED:       { http: 502, status: 'failed',     component: 'notion',     during: 'lookup',     severity: 'error' },
  ARCHIVE_CONTENT_MISMATCH: { http: 200, status: 'failed',     component: 'notion',     during: 'verify',     severity: 'critical' },
  ARCHIVE_INCOMPLETE:       { http: 200, status: 'incomplete', component: 'notion',     during: 'append',     severity: 'error' },
};
let f = j.failure || null;
const pr = $('Page ready').isExecuted ? $('Page ready').first().json : null;
const cmp = $('Compare').isExecuted ? $('Compare').last().json : null;
if (!f) {   // reached through "Mark stored": Notion's own answer is the confirmation
  const props = j.properties || {};
  const status = props['Archive status'] && props['Archive status'].select && props['Archive status'].select.name;
  const echo = ((props['Report SHA-256'] && props['Report SHA-256'].rich_text) || []).map(r => r.plain_text ?? '').join('');
  if (j.object !== 'page' || status !== 'stored' || echo !== v.body.report_sha256) {
    f = { code: 'ARCHIVE_INCOMPLETE', retry_count: 3, found: cmp ? cmp.existing : 0, page_url: pr ? pr.page_url : null, echo: echo || null };
  } else {
    return [{ json: { http_status: 200, error: null, response: { ok: true, request_id: rid, archive_status: pr.created ? 'stored' : 'duplicate_existing',
      notion_page_url: j.url || pr.page_url, blocks_expected: v.body.total_blocks, blocks_found: cmp.existing, report_sha256_echo: echo } } }];
  }
}
const spec = MAP[f.code] || { http: 502, status: 'failed', component: 'workflow', during: 'response', severity: 'error' };
const response = { ok: false, request_id: rid, archive_status: spec.status, notion_page_url: f.page_url ?? null, blocks_expected: v.expected_blocks || 0,
  blocks_found: f.found ?? 0, report_sha256_echo: f.echo ?? null, error_code: f.code };
if (f.details) response.details = f.details;
return [{ json: { http_status: spec.http, response,
  error: { code: f.code, severity: spec.severity, component: spec.component, during: spec.during, retry_count: f.retry_count || 0, workflow: 'archive' } } }];
'''


def build_archive() -> dict:
    f = Flow("archive", "Procurement Report Archive (webhook)")
    webhook(f, "Archive Webhook", "procurement-archive", (0, 300))
    config(f, (220, 300))
    code(f, "Validate", ARCHIVE_VALIDATE_JS, (440, 300))
    if_bool(f, "Valid?", "={{ $json.valid === true }}", (660, 300))
    notion(f, "Lookup page", "POST", "={{ 'https://api.notion.com/v1/databases/' + $('Config').first().json.assessments_db_id + '/query' }}", (880, 300),
           body="={{ JSON.stringify({ filter: { property: 'Request ID', rich_text: { equals: $('Validate').first().json.request_id } }, sorts: [{ timestamp: 'created_time', direction: 'ascending' }], page_size: 10 }) }}",
           retry=True)
    code(f, "Decide", ARCHIVE_DECIDE_JS, (1100, 300))
    switch(f, "Route", "route", ["create", "ready", "fail"], (1320, 300))
    notion(f, "Create page", "POST", "https://api.notion.com/v1/pages", (1540, 160), body="={{ JSON.stringify($json.notion_body) }}")
    code(f, "Page ready", ARCHIVE_PAGE_READY_JS, (1760, 300))
    notion(f, "List children", "GET", "={{ 'https://api.notion.com/v1/blocks/' + $('Page ready').first().json.page_id + '/children' }}", (1980, 300),
           qs={"page_size": "100"}, retry=True, pagination=True)
    code(f, "Compare", ARCHIVE_COMPARE_JS, (2200, 300))
    switch(f, "Next step", "action", ["append", "done", "stop"], (2420, 300))
    notion(f, "Append blocks", "PATCH", "={{ 'https://api.notion.com/v1/blocks/' + $json.page_id + '/children' }}", (2640, 160), body="={{ JSON.stringify($json.append_body) }}")
    notion(f, "Mark stored", "PATCH", "={{ 'https://api.notion.com/v1/pages/' + $('Page ready').first().json.page_id }}", (2640, 300),
           body="={{ JSON.stringify({ properties: { 'Archive status': { select: { name: 'stored' } }, 'Total blocks': { number: $('Validate').first().json.body.total_blocks } } }) }}",
           retry=True)
    code(f, "Fail: notion", ARCHIVE_FAIL_NOTION_JS, (2640, 520))
    code(f, "Finalize outcome", ARCHIVE_FINALIZE_JS, (2880, 300))

    f.link("Archive Webhook", "Config"); f.link("Config", "Validate"); f.link("Validate", "Valid?")
    f.link("Valid?", "Lookup page", 0); f.link("Valid?", "Finalize outcome", 1)
    f.link("Lookup page", "Decide", 0); f.link("Lookup page", "Fail: notion", 1)
    f.link("Decide", "Route")
    f.link("Route", "Create page", 0); f.link("Route", "Page ready", 1); f.link("Route", "Finalize outcome", 2)
    f.link("Create page", "Page ready", 0); f.link("Create page", "Fail: notion", 1)
    f.link("Page ready", "List children")
    f.link("List children", "Compare", 0); f.link("List children", "Fail: notion", 1)
    f.link("Compare", "Next step")
    f.link("Next step", "Append blocks", 0); f.link("Next step", "Mark stored", 1); f.link("Next step", "Finalize outcome", 2)
    f.link("Append blocks", "List children", 0); f.link("Append blocks", "Fail: notion", 1)
    f.link("Mark stored", "Finalize outcome", 0); f.link("Mark stored", "Fail: notion", 1)
    f.link("Fail: notion", "Finalize outcome")
    tail(f, 2880, 300)
    sticky(f, "## Archive workflow (webhook)\n**After import:** bind the *Header Auth* credential to the webhook (header `X-Webhook-Secret`), bind the *Notion API* credential to every Notion node, "
              "and set `assessments_db_id` / `errors_db_id` in **Config**. Schemas: docs/notion-database-schemas.md.\n"
              "Flow: find page by Request ID (oldest wins) -> create without body -> loop: list body, compare with the sent blocks, append <= 100 -> `stored` only after a complete listing matches.\n"
              "Any Notion failure answers `incomplete`/`failed`; calling again resumes. **The production URL exists only while the workflow is ACTIVE.**", (0, -260), (820, 240))
    return f.doc()


# ───────────────────────── Workflow B: e-mail ─────────────────────────
EMAIL_VALIDATE_JS = r'''
const wh = $('Email Webhook').first().json;
const body = (wh.body && typeof wh.body === 'object' && !Array.isArray(wh.body)) ? wh.body : {};
const cfg = $('Config').first().json;
const a = (body.attachment && typeof body.attachment === 'object' && !Array.isArray(body.attachment)) ? body.attachment : {};
const isStr = v => typeof v === 'string';
const RID = /^[A-Za-z0-9_-]{1,64}$/, HEX64 = /^[0-9a-f]{64}$/, DATE = /^\d{4}-\d{2}-\d{2}$/;
const rid = isStr(body.request_id) && RID.test(body.request_id) ? body.request_id : null;
const errors = [];
if (body.action !== 'email') errors.push('INVALID_ACTION');
if (!rid) errors.push('INVALID_REQUEST_ID');
if (!isStr(body.report_sha256) || !HEX64.test(body.report_sha256)) errors.push('REPORT_HASH_INVALID');
if (!isStr(body.notion_page_url) || !body.notion_page_url.startsWith('https://')) errors.push('NOTION_URL_INVALID');
if (!isStr(body.executive_summary) || !body.executive_summary.trim()) errors.push('SUMMARY_MISSING');
if (!['mock', 'live'].includes(body.mode)) errors.push('INVALID_MODE');
if (['to', 'cc', 'bcc', 'recipient', 'recipients', 'sendTo', 'from'].some(k => Object.prototype.hasOwnProperty.call(body, k))) errors.push('RECIPIENT_FIELD_FORBIDDEN');
if (!rid || a.filename !== 'procurement-risk-assessment-' + rid + '.pdf') errors.push('ATTACHMENT_FILENAME_INVALID');
if (a.content_type !== 'application/pdf' || a.encoding !== 'base64') errors.push('ATTACHMENT_FORMAT_INVALID');
if (!Number.isInteger(a.size_bytes) || a.size_bytes < 1 || a.size_bytes > 5000000) errors.push('ATTACHMENT_SIZE_INVALID');
if (!isStr(a.sha256) || !HEX64.test(a.sha256)) errors.push('ATTACHMENT_HASH_INVALID');
let bytes = null;
if (isStr(a.content_base64) && a.content_base64.length && a.content_base64.length % 4 === 0 && /^[A-Za-z0-9+/]+={0,2}$/.test(a.content_base64)) bytes = Buffer.from(a.content_base64, 'base64');
else errors.push('ATTACHMENT_BASE64_INVALID');
if (bytes) {
  if (bytes.length !== a.size_bytes) errors.push('ATTACHMENT_SIZE_MISMATCH');
  if (bytes.subarray(0, 5).toString('latin1') !== '%PDF-') errors.push('PDF_HEADER_INVALID');
  if (bytes.toString('latin1').trimEnd().slice(-5) !== '%%EOF') errors.push('PDF_EOF_INVALID');
}
const dbOk = v => isStr(v) && /^[0-9a-f]{32}$/i.test(v.replace(/-/g, ''));
const mailOk = v => isStr(v) && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v) && !/REPLACE/i.test(v);
const configBad = !dbOk(cfg.assessments_db_id) || !DATE.test(String(cfg.notion_version || '')) || !mailOk(cfg.recipient);
const valid = errors.length === 0 && !configBad;
const slim = { ...body, attachment: { ...a, content_base64: undefined } };   // the PDF travels once, as pdf_base64
const json = { valid, request_id: rid, pdf_base64: valid ? a.content_base64 : null, pdf_filename: valid ? a.filename : null, body: slim };
if (!valid) json.failure = { code: configBad ? 'CONFIG_NOT_SET' : 'INVALID_PAYLOAD', retry_count: 0, details: errors };
return [{ json }];
'''

EMAIL_FAIL_PDF_JS = "return [{ json: { failure: { code: 'PDF_INVALID', retry_count: 0 } } }];"

EMAIL_FAIL_NOTION_JS = r'''
// A Notion call failed: before the ledger decision it is the lookup; afterwards it is the claim (nothing was sent).
return [{ json: { failure: { code: $('Ledger decision').isExecuted ? 'NOTION_LEDGER_FAILED' : 'NOTION_LOOKUP_FAILED', retry_count: 3 } } }];
'''

EMAIL_LEDGER_JS = r'''
// Duplicate protection: the Notion page is the persistent delivery ledger (Email status). Nothing is sent unless it says not_requested, failed or empty.
const v = $('Validate email').first().json;
const b = v.body;
const results = $input.first().json.results || [];
const delivery = 'del-' + v.request_id;
const notReady = reason => ({ json: { route: 'final', failure: { code: 'ARCHIVE_NOT_READY', retry_count: 0, details: [reason] } } });
if (results.length === 0) return [notReady('ARCHIVE_NOT_FOUND')];
const page = results[0];
const props = page.properties || {};
const sel = n => (props[n] && props[n].select && props[n].select.name) || null;
const txt = n => ((props[n] && props[n].rich_text) || []).map(r => r.plain_text ?? '').join('');
if (sel('Archive status') !== 'stored') return [notReady('ARCHIVE_NOT_STORED')];
if (txt('Report SHA-256') !== b.report_sha256) return [notReady('ARCHIVE_HASH_MISMATCH')];
const state = sel('Email status');
if (state === 'sent') {
  return [{ json: { route: 'final', outcome: { http_status: 200, response: { ok: true, request_id: v.request_id, email_status: 'already_sent', message_id: txt('Email message id') || null, delivery_id: txt('Delivery ID') || delivery } } } }];
}
if (state === 'sending' || state === 'unknown') {
  return [{ json: { route: 'final', outcome: { http_status: 200, response: { ok: false, request_id: v.request_id, email_status: 'unknown', delivery_id: txt('Delivery ID') || delivery, error_code: 'EMAIL_STATE_UNCERTAIN' },
    warning: { code: 'EMAIL_STATE_UNCERTAIN', severity: 'error', component: 'workflow', during: 'lookup', retry_count: 0 } } } }];
}
return [{ json: { route: 'send', page_id: page.id, page_url: page.url } }];
'''

EMAIL_RESTORE_PDF_JS = r'''
// The binary PDF was verified before the ledger claim; take exactly that file. Throwing here stops the workflow before any send.
const src = $('PDF to binary').first();
if (!src || !src.binary || !src.binary.data) throw new Error('PDF_BINARY_MISSING');
return [{ json: { send: true }, binary: { data: src.binary.data } }];
'''

# A Gmail 4xx answer means the message was not accepted -> 'failed' (a later resend is allowed). Anything else is uncertain -> 'unknown' (never resent here).
GMAIL_HTTP = "parseInt($json.error?.httpCode ?? $json.error?.statusCode ?? $json.error?.code, 10)"
GMAIL_STATE_EXPR = f"({GMAIL_HTTP} >= 400 && {GMAIL_HTTP} < 500) ? 'failed' : 'unknown'"

EMAIL_FINALIZE_JS = r'''
const v = $('Validate email').first().json;
const rid = v.request_id ?? null;
const delivery = rid ? 'del-' + rid : null;
const j = $input.first().json;
const MAP = {
  INVALID_PAYLOAD:       { http: 422, component: 'validation', during: 'validation', severity: 'warning' },
  CONFIG_NOT_SET:        { http: 503, component: 'workflow',   during: 'validation', severity: 'critical' },
  PDF_INVALID:           { http: 422, component: 'validation', during: 'validation', severity: 'error' },
  NOTION_LOOKUP_FAILED:  { http: 502, component: 'notion',     during: 'lookup',     severity: 'error' },
  NOTION_LEDGER_FAILED:  { http: 502, component: 'notion',     during: 'send',       severity: 'error' },
  ARCHIVE_NOT_READY:     { http: 409, component: 'notion',     during: 'lookup',     severity: 'warning' },
};
const statusOf = p => (p && p.object === 'page' && p.properties && p.properties['Email status'] && p.properties['Email status'].select && p.properties['Email status'].select.name) || null;
if (j.outcome) {                       // decided by the ledger: already_sent, or an uncertain earlier state
  const o = j.outcome;
  return [{ json: { http_status: o.http_status, response: o.response, error: o.warning ? { ...o.warning, workflow: 'email' } : null } }];
}
if (!j.failure && $('Record sent').isExecuted) {          // Gmail accepted the message; Notion's answer says whether the ledger was updated
  const ledgerOk = statusOf(j) === 'sent';
  const response = { ok: true, request_id: rid, email_status: 'sent', message_id: $('Gmail send').first().json.id ?? null, delivery_id: delivery,
    attachment_sha256_echo: $('Hash PDF').first().json.computed_attachment_sha256 };
  if (!ledgerOk) response.ledger_warning = true;
  return [{ json: { http_status: 200, response, error: ledgerOk ? null : { code: 'EMAIL_LEDGER_UPDATE_FAILED', severity: 'critical', component: 'notion', during: 'response', retry_count: 3, workflow: 'email' } } }];
}
if (!j.failure && $('Record failed or unknown').isExecuted) {   // Gmail errored; the ledger says failed (not accepted) or unknown (uncertain)
  const state = statusOf(j) === 'failed' ? 'failed' : 'unknown';
  const code = state === 'failed' ? 'GMAIL_SEND_FAILED' : 'GMAIL_SEND_UNKNOWN';
  const response = { ok: false, request_id: rid, email_status: state, delivery_id: delivery, error_code: code };
  if (j.object !== 'page') response.ledger_warning = true;
  return [{ json: { http_status: state === 'failed' ? 502 : 200, response, error: { code, severity: state === 'failed' ? 'error' : 'critical', component: 'gmail', during: 'send', retry_count: 0, workflow: 'email' } } }];
}
const f = j.failure || { code: 'WORKFLOW_ERROR', retry_count: 0 };
const spec = MAP[f.code] || { http: 502, component: 'workflow', during: 'response', severity: 'error' };
const response = { ok: false, request_id: rid, email_status: 'failed', delivery_id: delivery, error_code: f.code };
if (f.details) response.details = f.details;
return [{ json: { http_status: spec.http, response,
  error: { code: f.code, severity: spec.severity, component: spec.component, during: spec.during, retry_count: f.retry_count || 0, workflow: 'email' } } }];
'''


# Plain-text body: the summary's Markdown is cleaned for a text e-mail (no ** or `, bullets as "•", a blank line between items); the words are not changed.
GMAIL_MESSAGE_EXPR = (r"={{ 'Executive summary\n\n' + $('Validate email').first().json.body.executive_summary.replace(/\*\*/g, '').replace(/`/g, '')"
                      r".split('\n').map(l => l.startsWith('- ') ? '\u2022 ' + l.slice(2) : l).join('\n\n')"
                      r" + '\n\nNotion page: ' + $('Ledger decision').first().json.page_url + '\n\nThe complete report is attached as a PDF.' }}")


def build_email() -> dict:
    f = Flow("email", "Procurement Report Email (webhook)")
    webhook(f, "Email Webhook", "procurement-email", (0, 300))
    config(f, (220, 300), [("recipient", "REPLACE_WITH_APPROVED_TEST_RECIPIENT")])
    code(f, "Validate email", EMAIL_VALIDATE_JS, (440, 300))
    if_bool(f, "Valid?", "={{ $json.valid === true }}", (660, 300))
    f.add("PDF to binary", "n8n-nodes-base.convertToFile", 1.1,
          {"operation": "toBinary", "sourceProperty": "pdf_base64", "binaryPropertyName": "data",
           "options": {"fileName": "={{ $json.pdf_filename }}", "mimeType": "application/pdf", "dataIsBase64": True}}, (880, 300), onError="continueErrorOutput")
    f.add("Hash PDF", "n8n-nodes-base.crypto", 1,
          {"action": "hash", "type": "SHA256", "binaryData": True, "binaryPropertyName": "data", "dataPropertyName": "computed_attachment_sha256", "encoding": "hex"}, (1100, 300))
    if_bool(f, "Hash matches?", "={{ $json.computed_attachment_sha256 === $('Validate email').first().json.body.attachment.sha256 }}", (1320, 300))
    code(f, "Fail: PDF", EMAIL_FAIL_PDF_JS, (1320, 520))
    notion(f, "Lookup page", "POST", "={{ 'https://api.notion.com/v1/databases/' + $('Config').first().json.assessments_db_id + '/query' }}", (1540, 300),
           body="={{ JSON.stringify({ filter: { property: 'Request ID', rich_text: { equals: $('Validate email').first().json.request_id } }, sorts: [{ timestamp: 'created_time', direction: 'ascending' }], page_size: 10 }) }}",
           retry=True)
    code(f, "Ledger decision", EMAIL_LEDGER_JS, (1760, 300))
    if_bool(f, "Send?", "={{ $json.route === 'send' }}", (1980, 300))
    notion(f, "Claim send", "PATCH", "={{ 'https://api.notion.com/v1/pages/' + $('Ledger decision').first().json.page_id }}", (2200, 300),
           body="={{ JSON.stringify({ properties: { 'Email status': { select: { name: 'sending' } }, 'Delivery ID': { rich_text: [{ text: { content: 'del-' + $('Validate email').first().json.request_id } }] } } }) }}",
           retry=True)
    code(f, "Restore PDF", EMAIL_RESTORE_PDF_JS, (2420, 300))
    f.add("Gmail send", "n8n-nodes-base.gmail", 2.2,
          {"resource": "message", "operation": "send", "sendTo": "={{ $('Config').first().json.recipient }}",
           "subject": "={{ '[' + $('Validate email').first().json.body.mode.toUpperCase() + '] Procurement risk assessment: ' + $('Validate email').first().json.request_id }}",
           "emailType": "text",
           "message": GMAIL_MESSAGE_EXPR,
           "options": {"appendAttribution": False, "attachmentsUi": {"attachmentsBinary": [{"property": "data"}]}}},
          (2640, 300), onError="continueErrorOutput", retryOnFail=False,
          notes="Native Gmail SEND (not a draft). Never enable retry-on-fail: an uncertain send must not be repeated.")
    notion(f, "Record sent", "PATCH", "={{ 'https://api.notion.com/v1/pages/' + $('Ledger decision').first().json.page_id }}", (2860, 180),
           body="={{ JSON.stringify({ properties: { 'Email status': { select: { name: 'sent' } }, 'Email message id': { rich_text: [{ text: { content: String($('Gmail send').first().json.id ?? '') } }] } } }) }}",
           retry=True, on_error="continueRegularOutput")
    notion(f, "Record failed or unknown", "PATCH", "={{ 'https://api.notion.com/v1/pages/' + $('Ledger decision').first().json.page_id }}", (2860, 420),
           body="={{ JSON.stringify({ properties: { 'Email status': { select: { name: " + GMAIL_STATE_EXPR + " } } } }) }}", retry=True, on_error="continueRegularOutput")
    code(f, "Fail: notion", EMAIL_FAIL_NOTION_JS, (2200, 520))
    code(f, "Finalize outcome", EMAIL_FINALIZE_JS, (3080, 300))

    f.link("Email Webhook", "Config"); f.link("Config", "Validate email"); f.link("Validate email", "Valid?")
    f.link("Valid?", "PDF to binary", 0); f.link("Valid?", "Finalize outcome", 1)
    f.link("PDF to binary", "Hash PDF", 0); f.link("PDF to binary", "Fail: PDF", 1)
    f.link("Hash PDF", "Hash matches?")
    f.link("Hash matches?", "Lookup page", 0); f.link("Hash matches?", "Fail: PDF", 1); f.link("Fail: PDF", "Finalize outcome")
    f.link("Lookup page", "Ledger decision", 0); f.link("Lookup page", "Fail: notion", 1)
    f.link("Ledger decision", "Send?")
    f.link("Send?", "Claim send", 0); f.link("Send?", "Finalize outcome", 1)
    f.link("Claim send", "Restore PDF", 0); f.link("Claim send", "Fail: notion", 1); f.link("Fail: notion", "Finalize outcome")
    f.link("Restore PDF", "Gmail send")
    f.link("Gmail send", "Record sent", 0); f.link("Gmail send", "Record failed or unknown", 1)
    f.link("Record sent", "Finalize outcome"); f.link("Record failed or unknown", "Finalize outcome")
    tail(f, 3080, 300)
    sticky(f, "## E-mail workflow (webhook)\n**After import:** bind *Header Auth* (header `X-Webhook-Secret`) to the webhook, *Notion API* to every Notion node and the authorized *Gmail OAuth2* "
              "credential to **Gmail send**; set `assessments_db_id`, `errors_db_id` and the fixed approved `recipient` in **Config**.\n"
              "Flow: validate -> verify the PDF (SHA-256 of the decoded binary) -> check the Notion ledger (`Email status`) -> claim `sending` -> Gmail Send -> record result. "
              "One real e-mail only when the Gradio user clicks the button. **The production URL exists only while ACTIVE.**", (0, -300), (900, 240))
    return f.doc()


def main() -> None:
    for name, doc in (("procurement-report-archive.json", build_archive()), ("procurement-report-email.json", build_email())):
        (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(name, len(doc["nodes"]), "nodes")


if __name__ == "__main__":
    main()
