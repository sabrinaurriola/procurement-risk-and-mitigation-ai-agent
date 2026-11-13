"""Minimal text extraction for PDFs written by src/bridge/pdf.py (fpdf2, Identity-H fonts with ToUnicode maps).

Used for the exporter's self-check and by the tests: it reads the page tree, the content streams and each font's ToUnicode CMap, and returns the
strings shown on each page in drawing order. It is NOT a general PDF parser; anything it cannot read raises PdfError (the self-check then fails)."""
from __future__ import annotations

import re
import zlib

_ESC = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, ord("("): 40, ord(")"): 41, ord("\\"): 92}


def _error(message: str):
    from src.bridge.pdf import PdfError
    return PdfError("PDF_INVALID", message)


def _objects(data: bytes) -> dict[int, tuple[bytes, bytes | None]]:
    """object number -> (dictionary text, decoded stream or None)."""
    out: dict[int, tuple[bytes, bytes | None]] = {}
    for m in re.finditer(rb"(?<![\d])(\d+) 0 obj\s*(.*?)endobj", data, re.DOTALL):
        body = m.group(2)
        s = re.search(rb"stream\r?\n", body)
        if not s:
            out[int(m.group(1))] = (body, None)
            continue
        head = body[:s.start()]
        length = re.search(rb"/Length (\d+)", head)
        raw = body[s.end():]
        raw = raw[:int(length.group(1))] if length else raw[:raw.rindex(b"endstream")]      # binary data: never strip trailing bytes
        if b"/FlateDecode" in head:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raise _error("A PDF stream cannot be decompressed.") from None
        out[int(m.group(1))] = (head, raw)
    return out


def _cmap(stream: bytes) -> dict[int, str]:
    mapping: dict[int, str] = {}

    def uni(h: bytes) -> str:
        return bytes.fromhex(h.decode()).decode("utf-16-be")
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.DOTALL):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[int(src, 16)] = uni(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.DOTALL):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            base = uni(dst)
            for k in range(int(lo, 16), int(hi, 16) + 1):
                mapping[k] = base[:-1] + chr(ord(base[-1]) + k - int(lo, 16))
    return mapping


def _scan(content: bytes, fonts: dict[str, dict[int, str]]) -> list[str]:
    """Strings shown by Tj / TJ, in order, decoded through the current font."""
    lines: list[str] = []
    current: dict[int, str] = {}
    group: list[bytes] | None = None
    i, n = 0, len(content)

    def decode(parts: list[bytes]) -> str:
        raw = b"".join(parts)
        return "".join(current.get((raw[k] << 8) | raw[k + 1], "�") for k in range(0, len(raw) - 1, 2))
    pending: list[bytes] = []
    while i < n:
        c = content[i]
        if c == 0x28:                                           # ( ... ) literal string
            buf, depth, i = bytearray(), 1, i + 1
            while i < n and depth:
                ch = content[i]
                if ch == 0x5C:
                    nxt = content[i + 1]
                    if 0x30 <= nxt <= 0x37:
                        digits = re.match(rb"[0-7]{1,3}", content[i + 1:i + 4]).group(0)
                        buf.append(int(digits, 8) & 0xFF)
                        i += 1 + len(digits)
                        continue
                    buf.append(_ESC.get(nxt, nxt))
                    i += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if not depth:
                        i += 1
                        break
                buf.append(ch)
                i += 1
            (group if group is not None else pending).append(bytes(buf))
            continue
        if c == 0x5B:
            group, i = [], i + 1
            continue
        if c == 0x5D:
            pending, group, i = group or [], None, i + 1
            continue
        m = re.match(rb"/(F\d+) [\d.]+ Tf", content[i:i + 40])
        if m:
            current = fonts.get(m.group(1).decode(), {})
            pending, i = [], i + m.end()
            continue
        if content[i:i + 2] in (b"Tj", b"TJ") and group is None:
            if pending:
                lines.append(decode(pending))
            pending, i = [], i + 2
            continue
        i += 1
    return lines


def extract_pdf_text(data: bytes) -> list[list[str]]:
    objs = _objects(data)
    font_names: dict[str, int] = {}
    for head, _ in objs.values():
        m = re.search(rb"/Font\s*<<(.*?)>>", head, re.DOTALL)
        if m:
            font_names.update({k.decode(): int(v) for k, v in re.findall(rb"/(F\d+) (\d+) 0 R", m.group(1))})
    fonts: dict[str, dict[int, str]] = {}
    for name, num in font_names.items():
        tu = re.search(rb"/ToUnicode (\d+) 0 R", objs[num][0])
        if not tu or objs[int(tu.group(1))][1] is None:
            raise _error("A PDF font has no ToUnicode map.")
        fonts[name] = _cmap(objs[int(tu.group(1))][1])
    pages_root = [num for num, (head, _) in objs.items() if b"/Type /Pages" in head]
    if not pages_root:
        raise _error("The PDF page tree is missing.")
    kids = [int(k) for k in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids \[(.*?)\]", objs[pages_root[0]][0], re.DOTALL).group(1))]
    out: list[list[str]] = []
    for kid in kids:
        contents = re.search(rb"/Contents (\d+) 0 R", objs[kid][0])
        stream = objs[int(contents.group(1))][1] if contents else None
        if stream is None:
            raise _error("A PDF page has no readable content stream.")
        out.append(_scan(stream, fonts))
    return out
