"""Fictional policy documents (Markdown with front matter) -> stable, hashed chunks for `procurement.policy_chunks`.

A chunk is identified by `doc_id#chunk_index` (the `policy_ref` used in citations). Chunks split on `## ` headings;
long sections are split on paragraph boundaries. Content hashes make loading idempotent."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

DOC_TYPES = {"policy", "supplier_assessment"}
DOC_ID_RE = re.compile(r"^[A-Z]{3}-\d{3}$")
MAX_CHARS = 1400


class DocumentError(ValueError):
    pass


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    doc_title: str
    doc_type: str
    chunk_index: int
    content: str
    content_hash: str
    ingredient_codes: tuple[str, ...]
    is_synthetic: bool = True

    @property
    def policy_ref(self) -> str:
        return f"{self.doc_id}#{self.chunk_index}"


def _front_matter(text: str, name: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DocumentError(f"{name}: missing front matter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise DocumentError(f"{name}: front matter is not closed") from None
    meta: dict[str, str] = {}
    for ln in lines[1:end]:
        if ":" not in ln:
            raise DocumentError(f"{name}: malformed front matter line {ln!r}")
        k, v = ln.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, "\n".join(lines[end + 1:])


def _split_section(heading: str, body: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        candidate = f"{current}\n\n{p}" if current else p
        if current and len(f"{heading}\n{candidate}") > MAX_CHARS:
            chunks.append(f"{heading}\n{current}")
            current = p
        else:
            current = candidate
    if current:
        chunks.append(f"{heading}\n{current}")
    return chunks


def parse_document(path: Path, known_ingredients: set[str] | None = None) -> list[Chunk]:
    name = path.name
    meta, body = _front_matter(path.read_text(encoding="utf-8"), name)
    for key in ("doc_id", "title", "doc_type", "synthetic"):
        if not meta.get(key):
            raise DocumentError(f"{name}: front matter needs '{key}'")
    if not DOC_ID_RE.match(meta["doc_id"]):
        raise DocumentError(f"{name}: doc_id {meta['doc_id']!r} must look like POL-001")
    if not name.startswith(meta["doc_id"]):
        raise DocumentError(f"{name}: file name must start with its doc_id {meta['doc_id']}")
    if meta["doc_type"] not in DOC_TYPES:
        raise DocumentError(f"{name}: doc_type must be one of {sorted(DOC_TYPES)}")
    if meta["synthetic"].lower() != "true":
        raise DocumentError(f"{name}: every document must be marked synthetic: true")
    if "FICTIONAL" not in body:
        raise DocumentError(f"{name}: the body must state that the document is FICTIONAL")
    ingredients = tuple(x.strip() for x in meta.get("ingredients", "").split(",") if x.strip())
    if known_ingredients is not None and not set(ingredients) <= known_ingredients:
        raise DocumentError(f"{name}: unknown ingredient(s) {sorted(set(ingredients) - known_ingredients)}")

    sections: list[tuple[str, str]] = []
    heading, buf = None, []
    for line in body.splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections.append((heading, "\n".join(buf)))
            heading, buf = line.strip(), []
        elif heading is not None:
            buf.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(buf)))
    if not sections:
        raise DocumentError(f"{name}: no '## ' sections found")

    chunks: list[Chunk] = []
    for h, b in sections:
        for content in _split_section(h, b):
            chunks.append(Chunk(meta["doc_id"], meta["title"], meta["doc_type"], len(chunks), content,
                                hashlib.sha256(content.encode("utf-8")).hexdigest(), ingredients))
    return chunks


def load_documents(directory: Path, known_ingredients: set[str] | None = None) -> tuple[list[Chunk], list[str]]:
    """Parse every *.md file; return (chunks, errors). Duplicate doc_ids are an error."""
    chunks: list[Chunk] = []
    errors: list[str] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.md")):
        try:
            doc = parse_document(path, known_ingredients)
        except DocumentError as exc:
            errors.append(str(exc))
            continue
        if doc[0].doc_id in seen:
            errors.append(f"{path.name}: doc_id {doc[0].doc_id} is already used by {seen[doc[0].doc_id]}")
            continue
        seen[doc[0].doc_id] = path.name
        chunks += doc
    if not chunks and not errors:
        errors.append(f"no policy documents found in {directory}")
    return chunks, errors
