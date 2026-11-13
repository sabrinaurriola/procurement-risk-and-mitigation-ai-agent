"""Deterministic rendering of a stored report (Markdown) into flat Notion blocks, plus the checks n8n needs to archive it safely.
Pure functions: no network, no Notion call, no file access. n8n carries the blocks to Notion; FastAPI never talks to Notion.

Two different things are kept apart on purpose:
  SOURCE   the exact report_markdown stored in the authoritative record and served by GET /assessments/{id}/report.md.
           `report_sha256` is the SHA-256 of its UTF-8 bytes. It is never altered.
  CANONICAL the Markdown that the Notion blocks reproduce: canonical(source) = blocks_to_markdown(render_blocks(source)).
           It differs from the source ONLY in layout: trailing spaces, runs of blank lines, blank lines between consecutive
           bullets, a blank line inserted after a heading/paragraph that had none, and the bullet marker "* " written as "- ".
           The sequence of non-blank lines (right-stripped, "* " read as "- ") is identical, i.e. no text is dropped or reordered.
           `canonical_sha256` is its SHA-256.

Rendering rules (flat top-level blocks only; no nesting):
  "# " "## " "### "   -> heading_1/2/3          "- " "* " (column 0) -> bulleted_list_item (one line each)
  "> "                -> quote (consecutive lines join)               lines starting "|" (column 0) -> ONE code block (tables stay aligned; D6)
  anything else       -> paragraph with the raw text (consecutive lines join with \\n), so nothing is ever dropped
Limits (assumptions about Notion, to verify in the controlled test): <= 2000 characters per rich_text text object, <= 100 text objects per block,
<= 100 blocks per append request. Longer text is split into several text objects (and, past 100, into continuation blocks); never truncated.
Only plain text is emitted: report text derives from web content and is never turned into links, mentions or annotations."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

TEXT_LIMIT = 2000
TEXTS_PER_BLOCK = 100
MAX_PAGE = 100
_HEADING = re.compile(r"^(#{1,3}) (.*)$")
_BULLET = re.compile(r"^[-*] (.*)$")
_QUOTE = re.compile(r"^> ?(.*)$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(markdown: str) -> list[str]:
    return [ln.rstrip() for ln in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def _rich(text: str) -> list[list[dict[str, Any]]]:
    """Split text into text objects of <= TEXT_LIMIT characters, grouped into blocks of <= TEXTS_PER_BLOCK objects."""
    chunks = [text[i:i + TEXT_LIMIT] for i in range(0, len(text), TEXT_LIMIT)] or [""]
    groups = [chunks[i:i + TEXTS_PER_BLOCK] for i in range(0, len(chunks), TEXTS_PER_BLOCK)]
    return [[{"type": "text", "text": {"content": c}} for c in g] for g in groups]


def _block(kind: str, text: str) -> list[dict[str, Any]]:
    out = []
    for rich in _rich(text):
        body: dict[str, Any] = {"rich_text": rich}
        if kind == "code":
            body["language"] = "plain text"
        out.append({"object": "block", "type": kind, kind: body})
    return out


def render_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    lines = _lines(markdown)
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if not ln:
            i += 1
            continue
        if ln.startswith("|"):
            j = i
            while j < n and lines[j].startswith("|"):
                j += 1
            blocks += _block("code", "\n".join(lines[i:j]))
            i = j
            continue
        m = _HEADING.match(ln)
        if m:
            blocks += _block(f"heading_{len(m.group(1))}", m.group(2))
            i += 1
            continue
        m = _BULLET.match(ln)
        if m:
            blocks += _block("bulleted_list_item", m.group(1))
            i += 1
            continue
        if _QUOTE.match(ln):
            j = i
            while j < n and _QUOTE.match(lines[j]):
                j += 1
            blocks += _block("quote", "\n".join(_QUOTE.match(x).group(1) for x in lines[i:j]))
            i = j
            continue
        j = i
        while j < n and lines[j] and not (lines[j].startswith("|") or _HEADING.match(lines[j]) or _BULLET.match(lines[j]) or _QUOTE.match(lines[j])):
            j += 1
        blocks += _block("paragraph", "\n".join(lines[i:j]))
        i = j
    return blocks


def block_text(block: dict[str, Any]) -> str:
    """Plain text of a block, from either our rendering (text.content) or Notion's response (plain_text)."""
    kind = block.get("type")
    rich = (block.get(kind) or {}).get("rich_text") or []
    return "".join(r.get("plain_text") if r.get("plain_text") is not None else (r.get("text") or {}).get("content", "") for r in rich)


def blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Inverse of render_blocks (exact unless one logical block was split into continuation blocks, i.e. > 200,000 characters)."""
    parts: list[tuple[str, str]] = []
    for b in blocks:
        kind, text = b["type"], block_text(b)
        if kind.startswith("heading_"):
            md = "#" * int(kind[-1]) + " " + text
        elif kind == "bulleted_list_item":
            md = "- " + text
        elif kind == "quote":
            md = "\n".join("> " + x for x in text.split("\n"))
        else:
            md = text
        parts.append((kind, md))
    out = ""
    for idx, (kind, md) in enumerate(parts):
        if idx:
            out += "\n" if (kind == "bulleted_list_item" and parts[idx - 1][0] == "bulleted_list_item") else "\n\n"
        out += md
    return out


def canonical(markdown: str) -> str:
    return blocks_to_markdown(render_blocks(markdown))


def nonblank_lines(markdown: str) -> list[str]:
    return [ln for ln in _lines(markdown) if ln]


def signature(block: dict[str, Any]) -> tuple[str, str]:
    return block["type"], block_text(block)


def verify_prefix(expected: list[dict[str, Any]], existing: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the blocks already on a Notion page with the expected rendering. `existing` may be Notion's response objects.
    prefix_ok is True only if every existing block equals (type, text) of the expected block at the same index and there are not more existing
    blocks than expected. Counting alone is never enough: a page with the right number of wrong blocks is a mismatch."""
    total, have = len(expected), len(existing)
    mismatch: int | None = None
    for idx in range(min(total, have)):
        if signature(existing[idx]) != signature(expected[idx]):
            mismatch = idx
            break
    if mismatch is None and have > total:
        mismatch = total
    ok = mismatch is None
    return {"existing": have, "total_blocks": total, "prefix_ok": ok, "mismatch_index": mismatch, "complete": ok and have == total,
            "next_offset": have if ok else None}
