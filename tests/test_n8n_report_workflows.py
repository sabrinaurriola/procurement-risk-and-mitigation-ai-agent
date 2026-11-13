"""Offline tests for the two MVP report workflows (n8n/procurement-report-archive.json, n8n/procurement-report-email.json).

STRUCTURAL: graph, failure paths, credentials/ids/recipient hygiene, retry policy, Notion property names/types/options against the approved schema.
BEHAVIOUR: the real JavaScript of every Code node is executed with Node.js (tests/n8n_code_runner.js) against payloads built by the Python bridge,
against an in-memory Notion, and the produced webhook answers are fed back into the Python models/client.
NOT covered (needs n8n itself): HTTP Request pagination, Convert to File, Crypto, Gmail, Notion behaviour, expression evaluation of non-Code nodes."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import deque

import httpx
import pytest

from src.agent.graph import run_assessment
from src.bridge.client import WebhookClient, verify_archive_response
from src.bridge.models import ArchiveRequest, ArchiveResponse, EmailAttachment, EmailRequest, EmailResponse
from src.bridge.payload import build_archive_request
from tests.agent_fixtures import REQUEST, deps
from tests.n8n_report_sim import ARCHIVE, CONFIG, DB_ID, EMAIL, ERR_DB_ID, FakePage, as_notion, j, load, node, run_code, run_js

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to run the Code-node JavaScript offline")

SCHEMA = {
    "assessments": {
        "Name": "title", "Request ID": "rich_text", "Ingredient": "select", "Assessment date": "date", "Horizon months": "number", "Mode": "select",
        "Assessment status": "select", "Archive status": "select", "Report SHA-256": "rich_text", "Total blocks": "number", "Email status": "select",
        "Email message id": "rich_text", "Delivery ID": "rich_text"},
    "errors": {
        "Name": "title", "Timestamp": "date", "Request ID": "rich_text", "Workflow": "select", "Component": "select", "Error code": "rich_text",
        "Severity": "select", "Status": "select", "Execution ID": "rich_text", "Retry count": "number", "Occurred during": "select", "Error ID": "rich_text",
        "Resolution notes": "rich_text"}}
OPTIONS = {
    "Ingredient": {"sunflower_oil"}, "Mode": {"mock", "live"}, "Assessment status": {"completed", "partial", "failed"},
    "Archive status": {"incomplete", "stored", "failed"}, "Email status": {"not_requested", "sending", "sent", "failed", "unknown"},
    "Workflow": {"archive", "email"}, "Component": {"webhook", "validation", "notion", "gmail", "workflow"}, "Severity": {"warning", "error", "critical"},
    "Status": {"open", "resolved"}, "Occurred during": {"validation", "lookup", "create", "append", "verify", "send", "response"}}
ALL_PROPS = {**SCHEMA["assessments"], **SCHEMA["errors"]}
FORBIDDEN_NAMES = ["Delivery status", "Email sent", "Failure stage", "Sanitized error code", "Human review"]
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PAGE32 = "11111111222233334444555555555555"
POST_RESPONSE = {"Has error?", "Build error record", "Log error"}


@pytest.fixture(scope="module")
def archive_wf():
    return load(ARCHIVE)


@pytest.fixture(scope="module")
def email_wf():
    return load(EMAIL)


@pytest.fixture(scope="module", params=["archive", "email"])
def wf(request, archive_wf, email_wf):
    return archive_wf if request.param == "archive" else email_wf


def graph(wf):
    names = {n["name"] for n in wf["nodes"] if not n["type"].endswith("stickyNote")}
    edges = {n: set() for n in names}
    for src, outs in wf["connections"].items():
        for lst in outs["main"]:
            for c in lst:
                edges[src].add(c["node"])
    return names, edges


def reachable(edges, start, without=()):
    seen, q = set(), deque([start])
    while q:
        n = q.popleft()
        if n in seen or n in without:
            continue
        seen.add(n)
        q.extend(edges.get(n, ()))
    return seen


# ═════════════════════════════ structural ═════════════════════════════
def test_files_are_the_committed_output_of_the_generator():
    before = {p: p.read_text(encoding="utf-8") for p in (ARCHIVE, EMAIL)}
    subprocess.run([sys.executable, str(ARCHIVE.parent / "build_report_workflows.py")], check=True, capture_output=True)
    assert {p: p.read_text(encoding="utf-8") for p in before} == before, "regenerate with: python n8n/build_report_workflows.py"


def test_node_counts_are_mvp_sized(archive_wf, email_wf):
    assert len(archive_wf["nodes"]) == 21 and len(email_wf["nodes"]) == 23
    for w in (archive_wf, email_wf):
        assert sum(n["parameters"].get("jsCode", "").count("\n") for n in w["nodes"]) < 150           # small enough to read in one sitting


def test_unique_names_and_ids_and_valid_connections(wf):
    names = [n["name"] for n in wf["nodes"]]
    assert len(set(names)) == len(names) and len({n["id"] for n in wf["nodes"]}) == len(names)
    real, edges = graph(wf)
    for src, outs in wf["connections"].items():
        assert src in real and all(c["node"] in real for lst in outs["main"] for c in lst)
    assert wf["active"] is False and wf["settings"]["saveDataSuccessExecution"] == "none"


def test_every_node_is_reachable_and_every_path_gets_an_answer(wf):
    real, edges = graph(wf)
    entry = next(n["name"] for n in wf["nodes"] if n["type"].endswith(".webhook"))
    assert reachable(edges, entry) == real
    for name in real - POST_RESPONSE:
        assert "Respond" in reachable(edges, name), f"{name} cannot reach the response"
    assert edges["Respond"] == {"Has error?"}
    assert [n for n in real if not edges[n]] == ["Log error"]                       # the only leaf runs AFTER the answer was sent


def test_all_branch_outputs_are_wired(wf):
    for n in wf["nodes"]:
        outs = wf["connections"].get(n["name"], {}).get("main", [])
        if n["type"].endswith(".if") and n["name"] != "Has error?":
            assert len(outs) == 2 and all(outs), n["name"]
        if n["type"].endswith(".switch"):
            assert len(outs) == len(n["parameters"]["rules"]["values"]) and all(outs), n["name"]
        if n.get("onError") == "continueErrorOutput":
            assert len(outs) == 2 and outs[1], f"{n['name']}: error output is not wired"


def test_webhook_is_authenticated_post_with_a_response_node(archive_wf, email_wf):
    for w, path in ((archive_wf, "procurement-archive"), (email_wf, "procurement-email")):
        hook = next(n for n in w["nodes"] if n["type"].endswith(".webhook"))
        assert hook["parameters"] == {"httpMethod": "POST", "path": path, "authentication": "headerAuth", "responseMode": "responseNode", "options": {}}


def test_no_credentials_ids_secrets_or_real_addresses_in_the_json(email_wf):
    for path in (ARCHIVE, EMAIL):
        text = path.read_text(encoding="utf-8")
        assert '"credentials"' not in text
        assert not re.search(r"(?<![0-9a-fA-F-])[0-9a-fA-F]{32}(?![0-9a-fA-F-])", text), "a 32-hex Notion id must not be embedded"
        assert not re.search(r"secret_|ntn_|Bearer|api[_-]?key|(?<![A-Za-z0-9])sk-[A-Za-z0-9]{10}|require\(", text, re.IGNORECASE)
        assert set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)) == set()
    cfg = {n["name"]: n for n in email_wf["nodes"]}["Config"]["parameters"]["assignments"]["assignments"]
    values = {a["name"]: a["value"] for a in cfg}
    assert values["recipient"] == "REPLACE_WITH_APPROVED_TEST_RECIPIENT" and values["notion_version"] == "2022-06-28"
    assert values["assessments_db_id"].startswith("REPLACE_") and values["errors_db_id"].startswith("REPLACE_")


def test_one_notion_api_version_and_consistent_endpoints(wf):
    http = [n for n in wf["nodes"] if n["type"].endswith("httpRequest")]
    assert http
    for n in http:
        p = n["parameters"]
        assert p["authentication"] == "predefinedCredentialType" and p["nodeCredentialType"] == "notionApi"
        assert {"name": "Notion-Version", "value": "={{ $('Config').first().json.notion_version }}"} in p["headerParameters"]["parameters"]
        assert re.search(r"https://api\.notion\.com/v1/(databases/'? ?\+? ?.*query|pages|blocks/)", p["url"]), p["url"]
        assert "data_sources" not in p["url"]


def test_retry_policy_safe_operations_only(wf):
    never = {"Create page", "Append blocks", "Gmail send", "Log error"}
    allowed = {"Lookup page", "List children", "Mark stored", "Claim send", "Record sent", "Record failed or unknown"}
    for n in wf["nodes"]:
        if n["name"] in never:
            assert n.get("retryOnFail") is False, n["name"]
        if n.get("retryOnFail"):
            assert n["name"] in allowed and n["maxTries"] <= 3, n["name"]
    assert {n["name"]: n for n in wf["nodes"]}["Log error"]["onError"] == "continueRegularOutput"


def test_error_log_runs_after_the_answer_and_can_never_change_it(wf):
    real, edges = graph(wf)
    fails = [n for n in real if n.startswith("Fail:")]
    assert fails
    for n in fails:
        assert "Finalize outcome" in reachable(edges, n)
    assert edges["Finalize outcome"] == {"Respond"} and edges["Respond"] == {"Has error?"}
    assert edges["Has error?"] == {"Build error record"} and edges["Build error record"] == {"Log error"} and edges["Log error"] == set()
    assert "Log error" not in reachable(edges, "Archive Webhook" if "Archive Webhook" in real else "Email Webhook", without={"Respond"})
    resp = node(wf, "Respond")["parameters"]
    assert "$('Finalize outcome')" in resp["responseBody"] and "$('Finalize outcome')" in resp["options"]["responseCode"]


def test_notion_property_names_types_and_fixed_select_values_match_the_schema(wf):
    text = json.dumps(wf).replace('\\"', '"')
    used = re.findall(r"'([A-Za-z][A-Za-z0-9 \-]*)': \{ ?(title|rich_text|select|number|date)", text)
    used += [(n, "rich_text") for n in re.findall(r"'([A-Za-z][A-Za-z0-9 \-]*)': (?:rt\(|\{ rich_text)", text)]
    assert used
    for name, kind in used:
        assert name in ALL_PROPS and ALL_PROPS[name] == kind, (name, kind)
    for name, value in re.findall(r"'([A-Za-z][A-Za-z0-9 \-]*)': \{ select: \{ name: '([a-z_]+)' \}", text):
        assert value in OPTIONS[name], (name, value)
    for bad in FORBIDDEN_NAMES:
        assert bad not in text, bad


def test_every_expression_refers_only_to_nodes_that_exist(wf):
    names = {n["name"] for n in wf["nodes"]}
    for n in wf["nodes"]:
        for ref in re.findall(r"\$\('([^']+)'\)", json.dumps(n["parameters"]).replace('\\"', '"')):
            assert ref in names, (n["name"], ref)


def test_gmail_is_reachable_only_through_every_safety_step(email_wf):
    _, edges = graph(email_wf)
    assert "Gmail send" in reachable(edges, "Email Webhook")
    for step in ("Validate email", "Valid?", "PDF to binary", "Hash PDF", "Hash matches?", "Lookup page", "Ledger decision", "Send?", "Claim send", "Restore PDF"):
        assert "Gmail send" not in reachable(edges, "Email Webhook", without={step}), f"Gmail reachable without {step}"
    cond = node(email_wf, "Hash matches?")["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert "computed_attachment_sha256" in cond and "attachment.sha256" in cond and "===" in cond
    assert node(email_wf, "Hash PDF")["parameters"]["binaryData"] is True                    # hash of the decoded BINARY file
    gm = node(email_wf, "Gmail send")
    assert gm["parameters"]["operation"] == "send" and gm["parameters"]["resource"] == "message" and "draft" not in json.dumps(gm["parameters"]).lower()
    assert gm["parameters"]["options"]["attachmentsUi"]["attachmentsBinary"] == [{"property": "data"}]
    assert gm["parameters"]["sendTo"] == "={{ $('Config').first().json.recipient }}" and gm.get("retryOnFail") is False


def test_archive_is_marked_stored_only_after_a_complete_verified_listing(archive_wf):
    _, edges = graph(archive_wf)
    for step in ("Validate", "Lookup page", "Page ready", "List children", "Compare", "Next step"):
        assert "Mark stored" not in reachable(edges, "Archive Webhook", without={step}), step
    assert edges["Append blocks"] == {"List children", "Fail: notion"}                       # an append is always followed by a fresh listing
    assert [r["outputKey"] for r in node(archive_wf, "Next step")["parameters"]["rules"]["values"]] == ["append", "done", "stop"]
    assert node(archive_wf, "List children")["parameters"]["options"]["pagination"]["pagination"]["completeExpression"] == "={{ $response.body.has_more !== true }}"


def test_email_content_is_summary_link_and_pdf_only(email_wf):
    gm = node(email_wf, "Gmail send")["parameters"]
    assert "executive_summary" in gm["message"] and "Ledger decision" in gm["message"] and "page_url" in gm["message"]        # the link comes from Notion itself
    assert "report_markdown" not in gm["message"] and "report_sha256" not in gm["message"]
    assert gm["emailType"] == "text" and "mode.toUpperCase()" in gm["subject"] and gm["options"]["appendAttribution"] is False
    assert node(email_wf, "Claim send")["parameters"]["jsonBody"].count("'sending'") == 1 and "'Delivery ID'" in node(email_wf, "Claim send")["parameters"]["jsonBody"]
    assert "'Email message id'" in node(email_wf, "Record sent")["parameters"]["jsonBody"]


# ═════════════════════════════ behaviour: archive ═════════════════════════════
@pytest.fixture(scope="module")
def payload():
    resp, _ = run_assessment(dict(REQUEST), deps())
    return json.loads(build_archive_request(resp, mode="mock").model_dump_json())


def a_nodes(body, cfg=CONFIG):
    return {"Archive Webhook": {"json": {"body": body}}, "Config": {"json": cfg}}


def validate_archive(wf, body, cfg=CONFIG):
    return j(run_code(wf, "Validate", {}, a_nodes(body, cfg)))


def test_archive_validation_accepts_the_python_payload(archive_wf, payload):
    v = validate_archive(archive_wf, payload)
    assert v["valid"] is True and "failure" not in v and v["request_id"] == payload["request_id"] and v["expected_blocks"] == payload["total_blocks"]


@pytest.mark.parametrize("mutate,code", [
    (lambda b: b.update(action="email"), "INVALID_ACTION"), (lambda b: b.update(request_id="bad id!"), "INVALID_REQUEST_ID"),
    (lambda b: b.update(assessment_status="failed"), "INVALID_ASSESSMENT_STATUS"), (lambda b: b.update(mode="prod"), "INVALID_MODE"),
    (lambda b: b.update(report_sha256="xyz"), "REPORT_HASH_INVALID"), (lambda b: b["metadata"].update(ingredient="wheat"), "INGREDIENT_UNSUPPORTED"),
    (lambda b: b["metadata"].update(horizon_months="3"), "HORIZON_INVALID"), (lambda b: b["metadata"].update(assessment_date="12/11/2026"), "ASSESSMENT_DATE_INVALID"),
    (lambda b: b.update(blocks=[]), "BLOCKS_INVALID"), (lambda b: b.update(total_blocks=b["total_blocks"] + 1), "BLOCKS_INVALID")])
def test_archive_validation_rejects_each_defect_with_a_422_answer(archive_wf, payload, mutate, code):
    body = json.loads(json.dumps(payload))
    mutate(body)
    v = validate_archive(archive_wf, body)
    assert v["valid"] is False and v["failure"]["code"] == "INVALID_PAYLOAD" and code in v["failure"]["details"]
    nodes = {"Validate": {"json": v}, "Config": {"json": CONFIG}}
    fin = j(run_code(archive_wf, "Finalize outcome", v, nodes))
    assert fin["http_status"] == 422 and fin["response"]["ok"] is False and code in fin["response"]["details"]


def test_archive_refuses_placeholder_configuration(archive_wf, payload):
    v = validate_archive(archive_wf, payload, {**CONFIG, "assessments_db_id": "REPLACE_WITH_ASSESSMENTS_DATABASE_ID"})
    assert v["valid"] is False and v["failure"]["code"] == "CONFIG_NOT_SET"
    assert j(run_code(archive_wf, "Finalize outcome", v, {"Validate": {"json": v}}))["http_status"] == 503


def decide(wf, body, results):
    nodes = {"Validate": {"json": {"request_id": body["request_id"], "body": body}}, "Config": {"json": CONFIG}}
    return j(run_code(wf, "Decide", {"results": results}, nodes))


def page_row(body, sha=None, pid="11111111-2222-3333-4444-555555555555"):
    return {"object": "page", "id": pid, "url": "https://www.notion.so/Assessment-" + pid.replace("-", ""),
            "properties": {"Report SHA-256": {"rich_text": [{"plain_text": sha or body["report_sha256"]}]}}}


def test_archive_create_properties_use_the_exact_schema(archive_wf, payload):
    out = decide(archive_wf, payload, [])
    assert out["route"] == "create"
    props = out["notion_body"]["properties"]
    assert out["notion_body"]["parent"] == {"database_id": DB_ID} and "children" not in out["notion_body"]        # the body is written block by block afterwards
    assert set(props) == {"Name", "Request ID", "Ingredient", "Assessment date", "Horizon months", "Mode", "Assessment status", "Archive status",
                          "Report SHA-256", "Total blocks", "Email status"}
    for name, value in props.items():
        assert list(value)[0] == SCHEMA["assessments"][name], name
        if name in OPTIONS:
            assert value["select"]["name"] in OPTIONS[name], name
    assert props["Archive status"]["select"]["name"] == "incomplete" and props["Email status"]["select"]["name"] == "not_requested"
    assert props["Name"]["title"][0]["text"]["content"] == f"[MOCK] Procurement risk assessment {payload['request_id']}"
    assert props["Report SHA-256"]["rich_text"][0]["text"]["content"] == payload["report_sha256"]


def test_archive_reuses_the_oldest_page_and_never_overwrites_a_different_report(archive_wf, payload):
    older, newer = page_row(payload, pid="aaaaaaaa-0000-0000-0000-000000000001"), page_row(payload, pid="bbbbbbbb-0000-0000-0000-000000000002")
    out = decide(archive_wf, payload, [older, newer])
    assert out["route"] == "ready" and out["page_id"] == older["id"] and out["created"] is False
    conflict = decide(archive_wf, payload, [page_row(payload, sha="f" * 64)])
    assert conflict["route"] == "fail" and conflict["failure"]["code"] == "ARCHIVE_CONTENT_MISMATCH" and conflict["failure"]["echo"] == "f" * 64


def drive(wf, body, page, created=True, faults=None, max_loops=30):
    """One workflow EXECUTION: Compare -> (Append | done | stop), with the real JavaScript and the in-memory page.
    An append that errors ends the execution (Fail: notion), exactly as wired; calling drive() again is the caller's retry."""
    nodes = {"Validate": {"json": {"request_id": body["request_id"], "body": body, "expected_blocks": len(body["blocks"])}},
             "Page ready": {"json": {"page_id": page.id, "page_url": page.url, "created": created}}}
    faults, rounds, appended = list(faults or []), 0, []
    for _ in range(max_loops):
        out = j(run_code(wf, "Compare", input_items=page.listing(), nodes=nodes, run_index=rounds))
        rounds += 1
        if out["action"] != "append":
            return out, rounds, appended
        assert 1 <= len(out["append_body"]["children"]) <= 100
        appended.append(len(out["append_body"]["children"]))
        if not page.append(out["append_body"], **(faults.pop(0) if faults else {})):
            return {"action": "append_failed", "existing": out["existing"]}, rounds, appended
    raise AssertionError("the loop did not terminate")


def fake_blocks(n):
    return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"paragraph {i}"}}]}} for i in range(n)]


def body_with(blocks, base):
    return {**base, "blocks": blocks, "total_blocks": len(blocks)}


def texts(page):
    return [b["paragraph"]["rich_text"][0]["plain_text"] for b in page.children]


def test_archive_fresh_page_is_filled_in_batches_of_at_most_100(archive_wf, payload):
    page = FakePage()
    out, rounds, appended = drive(archive_wf, body_with(fake_blocks(250), payload), page)
    assert out["action"] == "done" and out["existing"] == 250 and appended == [100, 100, 50] and rounds == 4
    assert texts(page) == [f"paragraph {i}" for i in range(250)]


def test_archive_real_report_blocks_roundtrip_exactly(archive_wf, payload):
    out, _, appended = drive(archive_wf, payload, FakePage())
    assert out["action"] == "done" and out["existing"] == payload["total_blocks"] and sum(appended) == payload["total_blocks"] and max(appended) <= 100


def test_archive_resumes_an_interrupted_upload_without_duplicating_blocks(archive_wf, payload):
    body = body_with(fake_blocks(230), payload)
    page = FakePage()
    page.children += [as_notion(b) for b in body["blocks"][:100]]
    out, _, appended = drive(archive_wf, body, page, created=False)
    assert out["action"] == "done" and appended == [100, 30] and len(page.children) == 230


@pytest.mark.parametrize("fault", [{"lose_answer": True}, {"partial": 37}, {"fail_before": True}])
def test_archive_failed_append_ends_as_incomplete_and_the_next_call_resumes_cleanly(archive_wf, payload, fault):
    body = body_with(fake_blocks(150), payload)
    page = FakePage()
    out, _, _ = drive(archive_wf, body, page, faults=[fault])
    assert out["action"] == "append_failed"
    nodes = {"Validate": {"json": {"request_id": body["request_id"], "body": body, "expected_blocks": 150}}, "Config": {"json": CONFIG},
             "Page ready": {"json": {"page_id": page.id, "page_url": page.url, "created": True}}, "Compare": {"json": {"existing": out["existing"]}}}
    fail = j(run_code(archive_wf, "Fail: notion", {}, nodes))
    assert fail["failure"]["code"] == "ARCHIVE_INCOMPLETE" and fail["failure"]["page_url"] == page.url
    fin = j(run_code(archive_wf, "Finalize outcome", fail, nodes))
    resp = ArchiveResponse.model_validate(fin["response"])
    assert fin["http_status"] == 200 and resp.archive_status == "incomplete" and resp.ok is False
    again, _, _ = drive(archive_wf, body, page, created=False)                              # the caller calls again (a new execution)
    assert again["action"] == "done" and texts(page) == [f"paragraph {i}" for i in range(150)]      # no duplicate and no missing block


def test_archive_loop_is_bounded_even_if_notion_never_grows_the_page(archive_wf, payload):
    body = body_with(fake_blocks(150), payload)
    nodes = {"Validate": {"json": {"request_id": body["request_id"], "body": body}}, "Page ready": {"json": {"page_id": "p", "page_url": "u", "created": True}}}
    last = j(run_code(archive_wf, "Compare", input_items=FakePage().listing(), nodes=nodes, run_index=3))      # 4th round for 2 batches
    assert last["action"] == "stop" and last["failure"]["code"] == "ARCHIVE_INCOMPLETE"


def test_archive_never_modifies_a_page_whose_content_differs(archive_wf, payload):
    body = body_with(fake_blocks(5), payload)
    for tamper in ("text", "type", "extra"):
        page = FakePage()
        page.append({"children": body["blocks"]})
        if tamper == "text":
            page.children[2]["paragraph"]["rich_text"][0]["plain_text"] = "changed"
        elif tamper == "type":
            page.children[1]["type"] = "quote"
            page.children[1]["quote"] = page.children[1].pop("paragraph")
        else:
            page.append({"children": fake_blocks(1)})
        before = page.appends
        out, _, appended = drive(archive_wf, body, page, created=False)
        assert out["action"] == "stop" and out["failure"]["code"] == "ARCHIVE_CONTENT_MISMATCH" and appended == [] and page.appends == before


def stored(wf, body, created, sha=None, status="stored", existing=None):
    page = {"object": "page", "url": "https://www.notion.so/Assessment-" + PAGE32,
            "properties": {"Archive status": {"select": {"name": status}}, "Report SHA-256": {"rich_text": [{"plain_text": sha or body["report_sha256"]}]}}}
    nodes = {"Validate": {"json": {"request_id": body["request_id"], "body": body, "expected_blocks": len(body["blocks"])}},
             "Page ready": {"json": {"page_id": "p", "page_url": "https://www.notion.so/x-" + PAGE32, "created": created}},
             "Compare": {"json": {"existing": len(body["blocks"]) if existing is None else existing}}}
    return j(run_code(wf, "Finalize outcome", page, nodes))


def test_archive_success_answers_satisfy_the_python_verifier(archive_wf, payload):
    req = ArchiveRequest.model_validate(payload)
    for created, label in ((True, "stored"), (False, "duplicate_existing")):
        fin = stored(archive_wf, payload, created)
        assert fin["http_status"] == 200 and fin["error"] is None
        resp = ArchiveResponse.model_validate(fin["response"])
        assert resp.archive_status == label and verify_archive_response(req, resp) == "OK"
        assert resp.blocks_found == resp.blocks_expected == req.total_blocks and resp.report_sha256_echo == req.report_sha256


def test_archive_status_not_confirmed_by_notion_is_not_success(archive_wf, payload):
    req = ArchiveRequest.model_validate(payload)
    for kw in ({"status": "incomplete"}, {"sha": "e" * 64}):
        fin = stored(archive_wf, payload, True, **kw)
        resp = ArchiveResponse.model_validate(fin["response"])
        assert fin["response"]["ok"] is False and resp.archive_status == "incomplete" and verify_archive_response(req, resp) != "OK"
        assert fin["error"]["code"] == "ARCHIVE_INCOMPLETE"


def test_archive_fail_notion_distinguishes_before_and_after_the_page_exists(archive_wf):
    early = j(run_code(archive_wf, "Fail: notion", {}, {}))
    assert early["failure"]["code"] == "NOTION_PAGE_FAILED"


ARCHIVE_FAILURES = ["INVALID_PAYLOAD", "CONFIG_NOT_SET", "NOTION_PAGE_FAILED", "ARCHIVE_CONTENT_MISMATCH", "ARCHIVE_INCOMPLETE"]


@pytest.mark.parametrize("code", ARCHIVE_FAILURES)
def test_archive_failures_give_distinct_answers_and_valid_error_records(archive_wf, payload, code):
    nodes = {"Validate": {"json": {"request_id": payload["request_id"], "body": payload, "expected_blocks": payload["total_blocks"]}}, "Config": {"json": CONFIG}}
    fin = j(run_code(archive_wf, "Finalize outcome", {"failure": {"code": code, "retry_count": 2, "found": 7, "page_url": "https://www.notion.so/x-" + PAGE32}}, nodes))
    resp = fin["response"]
    assert resp["ok"] is False and resp["error_code"] == code and resp["archive_status"] in ("failed", "incomplete") and resp["request_id"] == payload["request_id"]
    assert fin["http_status"] in (200, 422, 502, 503) and resp["blocks_expected"] == payload["total_blocks"] and resp["blocks_found"] == 7
    if fin["http_status"] == 200:                              # processed: the Python client parses it and never calls it success
        assert verify_archive_response(ArchiveRequest.model_validate(payload), ArchiveResponse.model_validate(resp)) != "OK"
    rec = j(run_code(archive_wf, "Build error record", {}, {"Config": {"json": CONFIG}, "Finalize outcome": {"json": fin}}, execution_id="exec-42"))
    props = rec["notion_body"]["properties"]
    assert rec["notion_body"]["parent"] == {"database_id": ERR_DB_ID} and set(props) == set(SCHEMA["errors"]) - {"Resolution notes"}
    for name, value in props.items():
        assert list(value)[0] == SCHEMA["errors"][name], name
        if name in OPTIONS:
            assert value["select"]["name"] in OPTIONS[name], (name, value)
    assert props["Workflow"]["select"]["name"] == "archive" and props["Status"]["select"]["name"] == "open" and props["Retry count"]["number"] == 2
    assert props["Error code"]["rich_text"][0]["text"]["content"] == code and props["Execution ID"]["rich_text"][0]["text"]["content"] == "exec-42"
    assert props["Error ID"]["rich_text"][0]["text"]["content"] == f"E-archive-{payload['request_id']}-{code}"        # stable
    assert payload["report_markdown"][:60] not in json.dumps(rec) and "report_markdown" not in json.dumps(rec)


def test_error_record_for_an_invalid_request_id_leaves_the_field_empty(archive_wf, payload):
    body = {**json.loads(json.dumps(payload)), "request_id": "bad id!"}
    v = validate_archive(archive_wf, body)
    fin = j(run_code(archive_wf, "Finalize outcome", v, {"Validate": {"json": v}}))
    assert fin["response"]["request_id"] is None
    props = j(run_code(archive_wf, "Build error record", {}, {"Config": {"json": CONFIG}, "Finalize outcome": {"json": fin}}))["notion_body"]["properties"]
    assert props["Request ID"] == {"rich_text": []} and props["Error ID"]["rich_text"][0]["text"]["content"] == "E-archive-noid-INVALID_PAYLOAD"


# ═════════════════════════════ behaviour: e-mail ═════════════════════════════
def email_payload(**kw):
    att = EmailAttachment(filename="procurement-risk-assessment-req-1.pdf", size_bytes=len(PDF), sha256=hashlib.sha256(PDF).hexdigest(), pages=1,
                          content_base64=base64.b64encode(PDF).decode())
    req = EmailRequest(request_id="req-1", report_sha256="a" * 64, notion_page_url=f"https://www.notion.so/Assessment-{PAGE32}",
                       executive_summary="Exposure over the next 3 months: EUR 1,961,496.55.", mode="mock", attachment=att)
    d = json.loads(req.model_dump_json())
    d.update(kw)
    return d


def bad_att(**kw):
    d = email_payload()
    d["attachment"].update(kw)
    return d


def e_nodes(body, cfg=CONFIG):
    return {"Email Webhook": {"json": {"body": body}}, "Config": {"json": cfg}}


def validate_email(wf, body, cfg=CONFIG):
    return j(run_code(wf, "Validate email", {}, e_nodes(body, cfg)))


def test_email_validation_accepts_the_python_contract_and_strips_the_pdf_from_the_body(email_wf):
    v = validate_email(email_wf, email_payload())
    assert v["valid"] is True and "failure" not in v and v["request_id"] == "req-1"
    assert base64.b64decode(v["pdf_base64"]) == PDF and v["pdf_filename"] == "procurement-risk-assessment-req-1.pdf"
    assert "content_base64" not in v["body"]["attachment"] and v["body"]["attachment"]["sha256"] == hashlib.sha256(PDF).hexdigest()


@pytest.mark.parametrize("body,code", [
    (email_payload(action="archive"), "INVALID_ACTION"), (email_payload(request_id="bad id"), "INVALID_REQUEST_ID"),
    (email_payload(report_sha256="12"), "REPORT_HASH_INVALID"), (email_payload(notion_page_url="http://www.notion.so/x"), "NOTION_URL_INVALID"),
    (email_payload(executive_summary=""), "SUMMARY_MISSING"), (email_payload(mode="x"), "INVALID_MODE"),
    (email_payload(to="someone@example.invalid"), "RECIPIENT_FIELD_FORBIDDEN"), (email_payload(cc="x"), "RECIPIENT_FIELD_FORBIDDEN"),
    (bad_att(filename="procurement-risk-assessment-other.pdf"), "ATTACHMENT_FILENAME_INVALID"), (bad_att(content_type="text/html"), "ATTACHMENT_FORMAT_INVALID"),
    (bad_att(size_bytes=len(PDF) + 1), "ATTACHMENT_SIZE_MISMATCH"), (bad_att(size_bytes=5_000_001), "ATTACHMENT_SIZE_INVALID"),
    (bad_att(sha256="zz"), "ATTACHMENT_HASH_INVALID"), (bad_att(content_base64="***"), "ATTACHMENT_BASE64_INVALID"),
    (bad_att(content_base64=base64.b64encode(b"<html>" + PDF[6:]).decode(), size_bytes=len(PDF)), "PDF_HEADER_INVALID"),
    (bad_att(content_base64=base64.b64encode(PDF[:-8]).decode(), size_bytes=len(PDF) - 8), "PDF_EOF_INVALID")])
def test_email_validation_rejects_each_defect(email_wf, body, code):
    v = validate_email(email_wf, body)
    assert v["valid"] is False and v["failure"]["code"] == "INVALID_PAYLOAD" and code in v["failure"]["details"] and v["pdf_base64"] is None


def test_email_validation_refuses_placeholder_configuration(email_wf):
    for key, value in (("recipient", "REPLACE_ME"), ("assessments_db_id", "REPLACE_ME"), ("recipient", "REPLACE_WITH_APPROVED_TEST_RECIPIENT")):
        v = validate_email(email_wf, email_payload(), {**CONFIG, key: value})
        assert v["valid"] is False and v["failure"]["code"] == "CONFIG_NOT_SET"


def ledger_page(state="not_requested", archive="stored", sha="a" * 64, pid="11111111-2222-3333-4444-555555555555", message=None):
    props = {"Archive status": {"select": {"name": archive}}, "Report SHA-256": {"rich_text": [{"plain_text": sha}]},
             "Email status": {"select": {"name": state}} if state else {"select": None}, "Delivery ID": {"rich_text": []},
             "Email message id": {"rich_text": [{"plain_text": message}] if message else []}}
    return {"object": "page", "id": pid, "url": f"https://www.notion.so/Assessment-{pid.replace('-', '')}", "properties": props}


def ledger(wf, results):
    v = validate_email(wf, email_payload())
    return j(run_code(wf, "Ledger decision", {"results": results}, {"Validate email": {"json": v}}))


@pytest.mark.parametrize("state", ["not_requested", "failed", None])
def test_email_ledger_allows_a_send_only_for_untouched_or_definitely_failed_deliveries(email_wf, state):
    out = ledger(email_wf, [ledger_page(state)])
    assert out["route"] == "send" and out["page_id"] == "11111111-2222-3333-4444-555555555555" and out["page_url"].endswith(PAGE32)


def test_email_ledger_blocks_duplicates_and_uncertain_states(email_wf):
    sent = ledger(email_wf, [ledger_page("sent", message="gm-1")])
    assert sent["route"] == "final" and sent["outcome"]["response"] == {"ok": True, "request_id": "req-1", "email_status": "already_sent", "message_id": "gm-1", "delivery_id": "del-req-1"}
    for state in ("sending", "unknown"):
        out = ledger(email_wf, [ledger_page(state)])
        assert out["outcome"]["response"]["email_status"] == "unknown" and out["outcome"]["response"]["ok"] is False
        assert out["outcome"]["warning"]["code"] == "EMAIL_STATE_UNCERTAIN"


@pytest.mark.parametrize("results,reason", [
    ([], "ARCHIVE_NOT_FOUND"), ([ledger_page(archive="incomplete")], "ARCHIVE_NOT_STORED"), ([ledger_page(sha="b" * 64)], "ARCHIVE_HASH_MISMATCH")])
def test_email_ledger_refuses_unarchived_references(email_wf, results, reason):
    out = ledger(email_wf, results)
    assert out["failure"]["code"] == "ARCHIVE_NOT_READY" and out["failure"]["details"] == [reason]


def test_email_ledger_uses_the_oldest_page(email_wf):
    older, newer = ledger_page("sent", message="old"), ledger_page("not_requested", pid="aaaaaaaa-0000-0000-0000-000000000009")
    assert ledger(email_wf, [older, newer])["outcome"]["response"]["message_id"] == "old"


def test_email_restore_pdf_fails_closed_without_the_verified_binary(email_wf):
    with pytest.raises(RuntimeError, match="PDF_BINARY_MISSING"):
        run_code(email_wf, "Restore PDF", {}, {"PDF to binary": {"json": {}}})
    out = run_code(email_wf, "Restore PDF", {}, {"PDF to binary": {"json": {}, "binary": {"data": {"mimeType": "application/pdf", "data": "AAAA"}}}})
    assert out[0]["binary"]["data"]["data"] == "AAAA"


def classify(wf, error):
    body = node(wf, "Record failed or unknown")["parameters"]["jsonBody"]
    expr = re.search(r"select: \{ name: (.+?) \} \} \} \}\) \}\}$", body).group(1)
    return j(run_js(f"return [{{ json: {{ state: ({expr}) }} }}];", {"error": error}))["state"]


@pytest.mark.parametrize("error,state", [({"httpCode": "400"}, "failed"), ({"httpCode": 403}, "failed"), ({"httpCode": "429"}, "failed"), ({"httpCode": "500"}, "unknown"),
                                         ({"code": "ETIMEDOUT"}, "unknown"), ({"message": "socket hang up"}, "unknown"), ({}, "unknown")])
def test_gmail_errors_are_classified_and_uncertain_ones_are_never_called_failed(email_wf, error, state):
    assert classify(email_wf, error) == state


def email_nodes(wf, **extra):
    v = validate_email(wf, email_payload())
    base = {"Validate email": {"json": v}, "Gmail send": {"json": {"id": "gm-123"}}, "Hash PDF": {"json": {"computed_attachment_sha256": hashlib.sha256(PDF).hexdigest()}}, "Config": {"json": CONFIG}}
    base.update(extra)
    return base


def finalize(wf, input_json, **nodes):
    return j(run_code(wf, "Finalize outcome", input_json, email_nodes(wf, **nodes)))


def post_client(response_json, status, body=None):
    """Feeds an n8n answer to the Python client and returns its EmailResult."""
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json=response_json))
    client = WebhookClient("https://n8n.example.test/a", "https://n8n.example.test/e", "s", transport=transport, sleep=lambda s: None)
    return client.email(EmailRequest.model_validate(body or email_payload()))


def notion_page(state):
    return {"object": "page", "properties": {"Email status": {"select": {"name": state}}}}


def test_email_sent_answer_satisfies_the_python_client_only_with_the_attachment_echo(email_wf):
    fin = finalize(email_wf, notion_page("sent"), **{"Record sent": {"json": {}}})
    assert fin["http_status"] == 200 and fin["error"] is None and fin["response"]["message_id"] == "gm-123" and fin["response"]["delivery_id"] == "del-req-1"
    EmailResponse.model_validate(fin["response"])
    r = post_client(fin["response"], 200)
    assert r.ok and r.state == "sent" and r.message_id == "gm-123"
    assert post_client({k: v for k, v in fin["response"].items() if k != "attachment_sha256_echo"}, 200).state == "unknown"


def test_email_ledger_write_failure_after_a_send_is_reported_without_hiding_the_send(email_wf):
    fin = finalize(email_wf, {"error": "notion down"}, **{"Record sent": {"json": {}}})
    assert fin["response"]["email_status"] == "sent" and fin["response"]["ledger_warning"] is True
    assert fin["error"]["code"] == "EMAIL_LEDGER_UPDATE_FAILED" and fin["error"]["severity"] == "critical" and fin["error"]["workflow"] == "email"


@pytest.mark.parametrize("notion_answer,state,http", [(notion_page("failed"), "failed", 502), (notion_page("unknown"), "unknown", 200), ({"error": "notion down"}, "unknown", 200)])
def test_gmail_failures_are_distinct_and_understood_by_the_python_client(email_wf, notion_answer, state, http):
    fin = finalize(email_wf, notion_answer, **{"Record failed or unknown": {"json": {}}})
    assert fin["http_status"] == http and fin["response"]["email_status"] == state and fin["response"]["ok"] is False
    assert fin["error"]["component"] == "gmail" and fin["error"]["during"] == "send"
    assert fin["response"].get("ledger_warning", False) == ("error" in notion_answer)
    r = post_client(fin["response"], fin["http_status"])
    assert not r.ok and r.state == state and r.code in ("EMAIL_FAILED", "EMAIL_UNKNOWN")


def test_python_client_treats_an_already_sent_answer_as_success_without_a_new_send(email_wf):
    fin = finalize(email_wf, ledger(email_wf, [ledger_page("sent", message="gm-9")]))
    r = post_client(fin["response"], fin["http_status"])
    assert r.ok and r.state == "already_sent"
    unknown = finalize(email_wf, ledger(email_wf, [ledger_page("sending")]))
    assert post_client(unknown["response"], unknown["http_status"]).state == "unknown"


def test_email_fail_notion_distinguishes_lookup_from_claim(email_wf):
    assert j(run_code(email_wf, "Fail: notion", {}, {}))["failure"]["code"] == "NOTION_LOOKUP_FAILED"
    assert j(run_code(email_wf, "Fail: notion", {}, {"Ledger decision": {"json": {}}}))["failure"]["code"] == "NOTION_LEDGER_FAILED"


EMAIL_FAILURES = ["INVALID_PAYLOAD", "CONFIG_NOT_SET", "PDF_INVALID", "NOTION_LOOKUP_FAILED", "NOTION_LEDGER_FAILED", "ARCHIVE_NOT_READY"]


@pytest.mark.parametrize("code", EMAIL_FAILURES)
def test_email_failures_never_claim_a_send_and_log_valid_records(email_wf, code):
    fin = finalize(email_wf, {"failure": {"code": code, "retry_count": 1}})
    assert fin["response"]["ok"] is False and fin["response"]["email_status"] == "failed" and fin["response"]["error_code"] == code
    assert "message_id" not in fin["response"] and "attachment_sha256_echo" not in fin["response"]
    r = post_client(fin["response"], fin["http_status"])
    assert not r.ok and r.state == "failed"
    rec = j(run_code(email_wf, "Build error record", {}, {"Config": {"json": CONFIG}, "Finalize outcome": {"json": fin}}))["notion_body"]["properties"]
    assert set(rec) == set(SCHEMA["errors"]) - {"Resolution notes"} and rec["Workflow"]["select"]["name"] == "email"
    for name, value in rec.items():
        if name in OPTIONS:
            assert value["select"]["name"] in OPTIONS[name], (name, value)
    assert rec["Error ID"]["rich_text"][0]["text"]["content"] == f"E-email-req-1-{code}"
    assert "Exposure over" not in json.dumps(rec)


def test_the_email_body_is_clean_plain_text_with_the_summary_the_notion_link_and_the_pdf_note(email_wf, payload):
    expr = node(email_wf, "Gmail send")["parameters"]["message"]
    assert expr.startswith("={{ ") and expr.endswith(" }}")
    nodes = {"Validate email": {"json": {"body": {"executive_summary": payload["executive_summary"]}}},
             "Ledger decision": {"json": {"page_url": "https://www.notion.so/Assessment-" + PAGE32}}}
    text = j(run_js(f"return [{{ json: {{ text: ({expr[4:-3]}) }} }}];", {}, nodes))["text"]
    assert "**" not in text and "`" not in text and "\n- " not in text
    assert text.startswith("Executive summary\n\n\u2022 Exposure over the next 3 months:")
    assert text.count("\u2022 ") == payload["executive_summary"].count("\n- ") + 1 and "\n\n\u2022 Priority 1 " in text
    assert "1,961,496.55" in text and "107,744.17" in text and "Procurement Director" in text                    # the words are unchanged
    assert text.endswith("\n\nNotion page: https://www.notion.so/Assessment-" + PAGE32 + "\n\nThe complete report is attached as a PDF.")
    assert "report_markdown" not in expr and "report_sha256" not in expr
