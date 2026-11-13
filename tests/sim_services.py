"""SIMULATED Notion and Gmail for the offline n8n integration tests. In-memory; no network; no credentials.

SimNotion enforces the Notion limits the renderer assumes (<= 100 children per append, <= 2000 characters per text object, <= 100 text objects
per block) so a renderer that violated them would fail here, and it can inject the failures that matter for duplicate safety:
  error429 / error500 / error400  the call fails BEFORE any effect
  lost_response                   the call is APPLIED, then fails (the caller never sees the success)
  partial (k=n)                   an append applies only the first n children, then fails (crash mid-upload)
SimGmail can only create DRAFTS; `send` raises, and a draft to any address other than the configured test recipient is rejected.
Everything here is simulation evidence, not proof of how real Notion or Gmail behave."""
from __future__ import annotations

import copy
from typing import Any


class NotionError(Exception):
    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"notion {status}")
        self.status, self.retry_after = status, retry_after


class GmailError(Exception):
    pass


def _rich(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        content = (it.get("text") or {}).get("content", "")
        out.append({"type": "text", "text": {"content": content}, "plain_text": content, "annotations": {}})
    return out


def _prop(value: dict) -> dict:
    """Request-shape property -> response-shape property (adds plain_text)."""
    v = copy.deepcopy(value)
    for k in ("title", "rich_text"):
        if k in v:
            v[k] = _rich(v[k])
    return v


class _Faults:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def inject(self, op: str, kind: str, times: int = 1, k: int | None = None, retry_after: float | None = None) -> None:
        self.items.append({"op": op, "kind": kind, "times": times, "k": k, "retry_after": retry_after})

    def take(self, op: str) -> dict | None:
        for f in self.items:
            if f["op"] == op and f["times"] > 0:
                f["times"] -= 1
                return f
        return None


class SimNotion:
    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.children: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []          # (op, page_id or "")
        self.faults = _Faults()
        self._seq = 0

    # ── helpers ──
    def _fail(self, f: dict) -> None:
        raise NotionError({"error429": 429, "error500": 500, "error400": 400}[f["kind"]], f.get("retry_after"))

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def seed_page(self, request_id: str, **props: Any) -> str:
        """Create a page directly (e.g. a duplicate left by a race, or a page from an earlier crashed run)."""
        pid = self._new_id("page")
        base = {"Request ID": {"rich_text": _rich([{"text": {"content": request_id}}])}, "Archive status": {"select": {"name": "incomplete"}},
                "Delivery status": {"select": {"name": "pending"}}, "Report SHA-256": {"rich_text": _rich([{"text": {"content": ""}}])}}
        base.update({k: _prop(v) for k, v in props.items()})
        self.pages[pid] = {"id": pid, "created_seq": self._seq, "properties": base}
        self.children[pid] = []
        return pid

    # ── API surface used by the workflow model ──
    def query(self, request_id: str) -> list[dict]:
        self.calls.append(("query", ""))
        f = self.faults.take("query")
        if f:
            self._fail(f)
        hits = [p for p in self.pages.values() if "".join(t["plain_text"] for t in p["properties"]["Request ID"]["rich_text"]) == request_id]
        return copy.deepcopy(sorted(hits, key=lambda p: p["created_seq"]))

    def create_page(self, properties: dict) -> dict:
        self.calls.append(("create", ""))
        f = self.faults.take("create")
        if f and f["kind"] != "lost_response":
            self._fail(f)
        pid = self._new_id("page")
        self.pages[pid] = {"id": pid, "created_seq": self._seq, "properties": {k: _prop(v) for k, v in properties.items()}}
        self.children[pid] = []
        if f and f["kind"] == "lost_response":
            raise NotionError(504)
        return {"id": pid}

    def list_children(self, page_id: str) -> list[dict]:
        self.calls.append(("list", page_id))
        f = self.faults.take("list")
        if f:
            self._fail(f)
        return copy.deepcopy(self.children[page_id])

    def append_children(self, page_id: str, children: list[dict]) -> None:
        self.calls.append(("append", page_id))
        if len(children) > 100:
            raise NotionError(400)
        for b in children:
            rich = (b.get(b["type"]) or {}).get("rich_text") or []
            if len(rich) > 100 or any(len((r.get("text") or {}).get("content", "")) > 2000 for r in rich):
                raise NotionError(400)
        f = self.faults.take("append")
        if f and f["kind"] in ("error429", "error500", "error400"):
            self._fail(f)
        take = children[: f["k"]] if (f and f["kind"] == "partial") else children
        for b in take:
            kind = b["type"]
            stored = {"object": "block", "id": self._new_id("blk"), "type": kind, kind: {**{k: v for k, v in b[kind].items() if k != "rich_text"}, "rich_text": _rich(b[kind]["rich_text"])}}
            self.children[page_id].append(stored)
        if f and f["kind"] in ("lost_response", "partial"):
            raise NotionError(504)

    def patch_page(self, page_id: str, properties: dict) -> None:
        self.calls.append(("patch", page_id))
        f = self.faults.take("patch")
        if f and f["kind"] != "lost_response":
            self._fail(f)
        for k, v in properties.items():
            self.pages[page_id]["properties"][k] = _prop(v)
        if f and f["kind"] == "lost_response":
            raise NotionError(504)

    # ── inspection ──
    def prop(self, page_id: str, name: str) -> Any:
        p = self.pages[page_id]["properties"][name]
        return (p.get("select") or {}).get("name") if "select" in p else p


class SimGmail:
    def __init__(self, allowed_recipient: str) -> None:
        self.allowed, self.drafts, self.attempts = allowed_recipient, [], 0
        self.faults = _Faults()

    def create_draft(self, to: str, subject: str, body: str, attachments: list[tuple[str, bytes]] | None = None) -> str:
        self.attempts += 1
        f = self.faults.take("draft")
        if f:
            raise GmailError("simulated Gmail failure")
        if to != self.allowed:
            raise AssertionError("a draft was addressed to someone other than the configured test recipient")
        self.drafts.append({"to": to, "subject": subject, "body": body, "attachments": list(attachments or [])})
        return f"draft-{len(self.drafts)}"

    def send(self, *a: Any, **k: Any) -> None:                       # the workflow has no send node; any use is a test failure
        raise AssertionError("sending e-mail is forbidden in this slice")
