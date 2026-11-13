"""Notion block rendering and the notion-blocks / notion-verify endpoints. Offline: no Notion call, no network, no database, no paid call."""
from __future__ import annotations

import hashlib
import json
import threading

import pytest

from src.api import notion_blocks as nb
from tests.api_fixtures import BODY, HEADERS, Runner, client_for, make_app


# ───────── renderer ─────────
DOC = """# Title

Intro line one
intro line two

## Section
- first
- second

* third
> a quote
> second quote line

| a | b |
|---|---|
| 1 | 2 |

#### deeper heading
  - indented bullet
Trailing text
"""


def test_rendering_rules_on_a_mixed_document():
    kinds = [(b["type"], nb.block_text(b)) for b in nb.render_blocks(DOC)]
    assert kinds == [("heading_1", "Title"), ("paragraph", "Intro line one\nintro line two"), ("heading_2", "Section"),
                     ("bulleted_list_item", "first"), ("bulleted_list_item", "second"), ("bulleted_list_item", "third"),
                     ("quote", "a quote\nsecond quote line"), ("code", "| a | b |\n|---|---|\n| 1 | 2 |"),
                     ("paragraph", "#### deeper heading\n  - indented bullet\nTrailing text")]
    code = [b for b in nb.render_blocks(DOC) if b["type"] == "code"][0]
    assert code["code"]["language"] == "plain text"


def test_nothing_is_dropped_the_non_blank_line_sequence_is_preserved():
    star_to_dash = [("- " + ln[2:]) if ln.startswith("* ") else ln for ln in nb.nonblank_lines(DOC)]        # the one marker normalisation: "* " -> "- "
    assert nb.nonblank_lines(nb.canonical(DOC)) == star_to_dash


def test_canonical_is_idempotent_and_exact_for_its_own_output():
    c = nb.canonical(DOC)
    assert nb.canonical(c) == c and nb.blocks_to_markdown(nb.render_blocks(c)) == c
    assert nb.render_blocks(c) == nb.render_blocks(DOC)


def test_canonical_differs_from_the_source_only_in_layout():
    src = "# T\ntext  \n\n\n\n- a\n\n- b\n"
    c = nb.canonical(src)
    assert c != src and c == "# T\n\ntext\n\n- a\n- b"
    assert nb.nonblank_lines(c) == nb.nonblank_lines(src)
    assert nb.sha256_text(src) != nb.sha256_text(c)


def test_rendering_is_deterministic():
    assert nb.render_blocks(DOC) == nb.render_blocks(DOC) and json.dumps(nb.render_blocks(DOC)) == json.dumps(nb.render_blocks(DOC))


@pytest.mark.parametrize("length", [1, 1999, 2000, 2001, 4000, 4001, 5000])
def test_text_objects_never_exceed_2000_characters_and_no_text_is_lost(length):
    text = "".join(chr(0x41 + i % 26) for i in range(length))
    blocks = nb.render_blocks(text)
    objs = [r["text"]["content"] for b in blocks for r in b["paragraph"]["rich_text"]]
    assert all(len(o) <= nb.TEXT_LIMIT for o in objs) and "".join(objs) == text and len(blocks) == 1
    assert len(objs) == -(-length // 2000)


def test_a_block_never_has_more_than_100_text_objects_and_continuations_keep_all_text():
    text = "x" * (2000 * 250 + 7)
    blocks = nb.render_blocks(text)
    assert len(blocks) == 3 and all(len(b["paragraph"]["rich_text"]) <= 100 for b in blocks)
    assert "".join(nb.block_text(b) for b in blocks) == text


def test_long_tables_are_split_by_text_objects_and_not_truncated():
    rows = "\n".join("| " + "c" * 60 + f" | {i} |" for i in range(200))
    code = nb.render_blocks(rows)
    assert len(code) == 1 and code[0]["type"] == "code" and nb.block_text(code[0]) == rows
    assert all(len(r["text"]["content"]) <= 2000 for r in code[0]["code"]["rich_text"])


def test_unicode_crlf_and_odd_input():
    src = "# Título ✓\r\n\r\ncafé — naïve 日本語 😀\r\n- ítem\r\n"
    assert [nb.block_text(b) for b in nb.render_blocks(src)] == ["Título ✓", "café — naïve 日本語 😀", "ítem"]
    assert nb.render_blocks("") == [] and nb.render_blocks("\n\n  \n") == []
    assert [b["type"] for b in nb.render_blocks("#\n-\n>\n|")] == ["paragraph", "quote", "code"]


def test_rendered_blocks_contain_plain_text_only():
    blocks = nb.render_blocks("[link](http://x.test) **bold** `code` @someone <script>")
    rich = blocks[0]["paragraph"]["rich_text"][0]
    assert set(rich) == {"type", "text"} and set(rich["text"]) == {"content"}                  # no link, no annotations, no mention


# ───────── prefix verification (counting alone is not enough) ─────────
def notion_like(blocks, *, extra=None):
    out = []
    for i, b in enumerate(blocks):
        k = b["type"]
        rich = [{"type": "text", "plain_text": r["text"]["content"], "text": r["text"], "annotations": {}} for r in b[k]["rich_text"]]
        out.append({"object": "block", "id": f"id-{i}", "type": k, k: {"rich_text": rich, **({"language": "plain text"} if k == "code" else {})}, **(extra or {})})
    return out


def test_verify_prefix_accepts_exact_prefixes_empty_and_complete():
    exp = nb.render_blocks(DOC)
    assert nb.verify_prefix(exp, [])["prefix_ok"] and nb.verify_prefix(exp, [])["next_offset"] == 0
    r = nb.verify_prefix(exp, notion_like(exp[:3]))
    assert r == {"existing": 3, "total_blocks": len(exp), "prefix_ok": True, "mismatch_index": None, "complete": False, "next_offset": 3}
    full = nb.verify_prefix(exp, notion_like(exp))
    assert full["complete"] and full["next_offset"] == len(exp)


def test_verify_prefix_rejects_a_same_count_page_with_wrong_content():
    exp = nb.render_blocks(DOC)
    page = notion_like(exp[:4])
    page[2][page[2]["type"]]["rich_text"][0]["plain_text"] = "tampered"
    r = nb.verify_prefix(exp, page)
    assert not r["prefix_ok"] and r["mismatch_index"] == 2 and r["next_offset"] is None and not r["complete"]


def test_verify_prefix_rejects_wrong_type_reordering_extra_blocks_and_duplicates():
    exp = nb.render_blocks(DOC)
    wrong_type = notion_like(exp[:3])
    wrong_type[0]["type"], wrong_type[0]["paragraph"] = "paragraph", wrong_type[0].pop("heading_1")
    assert nb.verify_prefix(exp, wrong_type)["mismatch_index"] == 0
    assert nb.verify_prefix(exp, notion_like([exp[1], exp[0]]))["mismatch_index"] == 0
    assert nb.verify_prefix(exp, notion_like(exp + [exp[0]])) ["mismatch_index"] == len(exp)         # more blocks than expected
    dup = notion_like(exp[:3] + exp[2:5])                                                              # a duplicated append
    r = nb.verify_prefix(exp, dup)
    assert not r["prefix_ok"] and r["mismatch_index"] == 3


def test_verify_ignores_notion_metadata_but_not_text():
    exp = nb.render_blocks(DOC)
    assert nb.verify_prefix(exp, notion_like(exp, extra={"created_time": "2026-11-12T10:00:00Z", "has_children": False, "archived": False}))["complete"]


# ───────── endpoints ─────────
def posted(tmp_path, **kw):
    app, runner, sink = make_app(tmp_path, **kw)
    c = client_for(app)
    env = c.post("/assessments", json=BODY, headers=HEADERS).json()
    return c, env, runner


def test_endpoint_returns_pages_that_concatenate_to_the_full_rendering(tmp_path):
    c, env, _ = posted(tmp_path)
    src = env["report_markdown"]
    expected = nb.render_blocks(src)
    got, offset, pages = [], 0, 0
    while offset is not None:
        r = c.get(f"/assessments/api-001/notion-blocks?offset={offset}&limit=40", headers=HEADERS).json()
        assert r["offset"] == offset and r["count"] == len(r["blocks"]) <= 40 and r["total_blocks"] == len(expected)
        got += r["blocks"]
        offset, pages = r["next_offset"], pages + 1
    assert got == expected and pages == -(-len(expected) // 40)


def test_endpoint_keeps_source_hash_and_canonical_hash_apart(tmp_path):
    c, env, _ = posted(tmp_path)
    r = c.get("/assessments/api-001/notion-blocks", headers=HEADERS).json()
    md = c.get("/assessments/api-001/report.md", headers=HEADERS)
    assert md.text == env["report_markdown"]                                                             # exact source, byte for byte
    assert r["report_sha256"] == hashlib.sha256(md.content).hexdigest() == nb.sha256_text(env["report_markdown"])
    assert r["canonical_sha256"] == nb.sha256_text(nb.canonical(env["report_markdown"]))
    assert r["report_sha256"] != r["canonical_sha256"]                                                    # the real mock report differs in layout only
    assert nb.nonblank_lines(nb.canonical(env["report_markdown"])) == nb.nonblank_lines(env["report_markdown"])
    assert r["blocks_sha256"] == nb.sha256_text(json.dumps(nb.render_blocks(env["report_markdown"]), sort_keys=True, separators=(",", ":")))


def test_every_block_respects_notion_limits_for_a_real_report(tmp_path):
    c, env, _ = posted(tmp_path)
    blocks = nb.render_blocks(env["report_markdown"])
    assert blocks and all(len(r["text"]["content"]) <= 2000 for b in blocks for r in b[b["type"]]["rich_text"])
    assert all(len(b[b["type"]]["rich_text"]) <= 100 for b in blocks)
    assert nb.blocks_to_markdown(blocks) == nb.canonical(env["report_markdown"])


def test_page_properties_come_from_the_record_and_hold_no_report_text(tmp_path):
    c, env, _ = posted(tmp_path)
    page = c.get("/assessments/api-001/notion-blocks?limit=1", headers=HEADERS).json()["page"]
    assert page["properties"] == {"request_id": "api-001", "ingredient": "sunflower_oil", "horizon_months": 3, "assessment_date": env["assessment_date"],
                                  "mode": "mock", "assessment_status": env["assessment_status"], "warnings": len(env["warnings"]),
                                  "claims_removed": len(env["report"]["removed_claims"]), "schema_version": env["schema_version"]}
    assert page["title"] == env["report_title"]


@pytest.mark.parametrize("qs", ["offset=-1", "limit=0", "limit=101", "offset=x", "limit=x", "offset=1000001"])
def test_invalid_pagination_is_a_422(tmp_path, qs):
    c, _, _ = posted(tmp_path)
    r = c.get(f"/assessments/api-001/notion-blocks?{qs}", headers=HEADERS)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST"


def test_offset_beyond_the_end_is_an_empty_final_page(tmp_path):
    c, _, _ = posted(tmp_path)
    total = c.get("/assessments/api-001/notion-blocks?limit=1", headers=HEADERS).json()["total_blocks"]
    r = c.get(f"/assessments/api-001/notion-blocks?offset={total}", headers=HEADERS).json()
    assert r["blocks"] == [] and r["count"] == 0 and r["next_offset"] is None


def test_auth_and_unknown_ids(tmp_path):
    c, _, _ = posted(tmp_path)
    assert c.get("/assessments/api-001/notion-blocks").status_code == 401
    assert c.post("/assessments/api-001/notion-verify", json={"children": []}).status_code == 401
    assert c.get("/assessments/nope/notion-blocks", headers=HEADERS).status_code == 404
    assert c.post("/assessments/nope/notion-verify", json={"children": []}, headers=HEADERS).status_code == 404
    assert c.get("/assessments/bad id/notion-blocks", headers=HEADERS).status_code in (404, 422)


def test_a_running_assessment_passes_through_202(tmp_path):
    gate = threading.Event()
    app, runner, _ = make_app(tmp_path, Runner(gate=gate), deadline_seconds=0.2)
    c = client_for(app)
    assert c.post("/assessments", json=BODY, headers=HEADERS).status_code == 504
    r = c.get("/assessments/api-001/notion-blocks", headers=HEADERS)
    assert r.status_code == 202 and r.json()["run_state"] == "running"
    gate.set()


def test_a_failed_assessment_has_nothing_to_archive(tmp_path):
    from src.agent.llm import LLMError
    from tests.agent_fixtures import ScriptedLLM, deps
    llm = ScriptedLLM(drafts=[LLMError("LLM_DOWN"), LLMError("LLM_DOWN")])
    app, _, _ = make_app(tmp_path, Runner(deps_factory=lambda: deps(llm=llm)))
    c = client_for(app)
    assert c.post("/assessments", json=BODY, headers=HEADERS).json()["status"] == "failed"
    r = c.get("/assessments/api-001/notion-blocks", headers=HEADERS)
    assert r.status_code == 404 and r.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"
    assert c.post("/assessments/api-001/notion-verify", json={"children": []}, headers=HEADERS).status_code == 404


def test_verify_endpoint_end_to_end(tmp_path):
    c, env, runner = posted(tmp_path)
    exp = nb.render_blocks(env["report_markdown"])
    ok = c.post("/assessments/api-001/notion-verify", json={"children": notion_like(exp[:10])}, headers=HEADERS).json()
    assert ok["prefix_ok"] and ok["existing"] == 10 and ok["next_offset"] == 10 and not ok["complete"] and ok["report_sha256"] == nb.sha256_text(env["report_markdown"])
    done = c.post("/assessments/api-001/notion-verify", json={"children": notion_like(exp)}, headers=HEADERS).json()
    assert done["complete"] and done["total_blocks"] == len(exp)
    bad = notion_like(exp[:10])
    bad[4][bad[4]["type"]]["rich_text"][0]["plain_text"] += "!"
    r = c.post("/assessments/api-001/notion-verify", json={"children": bad}, headers=HEADERS).json()
    assert not r["prefix_ok"] and r["mismatch_index"] == 4 and r["next_offset"] is None
    assert runner.calls == 1                                                                              # verification never runs an assessment


def test_verify_rejects_unknown_fields_and_oversized_bodies(tmp_path):
    c, _, _ = posted(tmp_path)
    assert c.post("/assessments/api-001/notion-verify", json={"children": [], "x": 1}, headers=HEADERS).status_code == 422
    assert c.post("/assessments/api-001/notion-verify", json={"children": [{}] * 5001}, headers=HEADERS).status_code == 422
    assert c.post("/assessments/api-001/notion-verify", json={}, headers=HEADERS).status_code == 422


def test_responses_contain_no_credentials_or_paths(tmp_path):
    c, _, _ = posted(tmp_path)
    text = c.get("/assessments/api-001/notion-blocks", headers=HEADERS).text + c.post(
        "/assessments/api-001/notion-verify", json={"children": []}, headers=HEADERS).text
    for leak in ("test-key-for-offline-tests", str(tmp_path), "DATABASE_URL", "Traceback"):
        assert leak not in text


def test_no_endpoint_starts_or_changes_an_assessment(tmp_path):
    c, env, runner = posted(tmp_path)
    for _ in range(3):
        c.get("/assessments/api-001/notion-blocks", headers=HEADERS)
        c.post("/assessments/api-001/notion-verify", json={"children": []}, headers=HEADERS)
    assert runner.calls == 1 and c.get("/assessments/api-001", headers=HEADERS).json()["report_markdown"] == env["report_markdown"]
