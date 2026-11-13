"""Helpers for the offline tests of the two report workflows: run a Code node's real JavaScript with Node.js, and a tiny in-memory Notion."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(__file__).with_name("n8n_code_runner.js")
ARCHIVE = ROOT / "n8n" / "procurement-report-archive.json"
EMAIL = ROOT / "n8n" / "procurement-report-email.json"
DB_ID = "0123456789abcdef0123456789abcdef"
ERR_DB_ID = "fedcba9876543210fedcba9876543210"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def node(wf: dict, name: str) -> dict:
    return next(n for n in wf["nodes"] if n["name"] == name)


def run_js(code: str, input_json: dict | None = None, nodes: dict | None = None, run_index: int = 0, execution_id: str = "777",
           input_items: list | None = None) -> Any:
    items = input_items if input_items is not None else ([{"json": input_json}] if input_json is not None else [])
    spec = {"code": code, "input": items,
            "nodes": {k: {"items": v if isinstance(v, list) else [v], "executed": True} for k, v in (nodes or {}).items()},
            "runIndex": run_index, "executionId": execution_id}
    proc = subprocess.run(["node", str(RUNNER)], input=json.dumps(spec).encode("utf-8"), capture_output=True, timeout=60)
    out = json.loads(proc.stdout.decode("utf-8") or "{}")
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or proc.stderr.decode("utf-8", "replace")[:300])
    return out["result"]


def run_code(wf: dict, name: str, input_json: dict | None = None, nodes: dict | None = None, run_index: int = 0, execution_id: str = "777",
             input_items: list | None = None) -> Any:
    try:
        return run_js(node(wf, name)["parameters"]["jsCode"], input_json, nodes, run_index, execution_id, input_items)
    except RuntimeError as exc:
        raise RuntimeError(f"{name}: {exc}") from None


def j(items: Any) -> dict:
    return items[0]["json"]


CONFIG = {"notion_version": "2022-06-28", "assessments_db_id": DB_ID, "errors_db_id": ERR_DB_ID, "recipient": "tester@example.invalid"}


# ───────── in-memory Notion (children only; enough to exercise resume, duplicates and uncertain answers) ─────────
def plain(block: dict) -> str:
    return "".join(r["text"]["content"] for r in block[block["type"]]["rich_text"])


def as_notion(block: dict) -> dict:
    """What Notion returns for a block we sent: same type, rich_text with plain_text."""
    rich = [{"type": "text", "text": r["text"], "plain_text": r["text"]["content"]} for r in block[block["type"]]["rich_text"]]
    return {"object": "block", "id": "b-" + str(abs(hash(plain(block))))[:8], "type": block["type"], block["type"]: {"rich_text": rich}}


class FakePage:
    def __init__(self, page_id: str = "11111111-2222-3333-4444-555555555555"):
        self.id, self.children, self.appends, self.url = page_id, [], 0, "https://www.notion.so/Assessment-" + page_id.replace("-", "")

    def listing(self) -> list[dict]:
        """Listing as the HTTP node with pagination returns it: one item per page of 100 results."""
        pages = [self.children[i:i + 100] for i in range(0, len(self.children), 100)] or [[]]
        return [{"json": {"object": "list", "results": p, "has_more": k < len(pages) - 1}} for k, p in enumerate(pages)]

    def append(self, body: dict, *, lose_answer: bool = False, partial: int | None = None, fail_before: bool = False) -> bool:
        """Returns True when the HTTP node would see success. lose_answer = applied but the response is lost; partial = only N blocks applied."""
        if fail_before:
            return False
        assert len(body["children"]) <= 100, "Notion allows at most 100 blocks per append"
        kids = body["children"] if partial is None else body["children"][:partial]
        self.children += [as_notion(b) for b in kids]
        self.appends += 1
        return not lose_answer and partial is None
