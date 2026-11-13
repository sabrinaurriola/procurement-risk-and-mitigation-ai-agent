"""Markdown -> PDF export of the FINAL report with fpdf2 and embedded DejaVu fonts (Unicode: Latin, Greek, Cyrillic, arrows, math).

What it does (docs/day4-architecture.md section 21):
* Lays out the exact report Markdown: headings, paragraphs, bullets, quotes and tables (wrapped cells, repeated header row, numeric columns right-aligned),
  warnings, citations and financial values included. Only light inline markup is interpreted: `**bold**`, `code` spans and a whole line in `_italic_`.
* Adds layout elements that are NOT report content: a footer (request id, "Decision support only", "Page n of N") and, when the report status is not
  COMPLETED, a highlighted banner that points to the Warnings and Unverified-concerns sections.
* Characters the font cannot draw (outside DejaVu's coverage, e.g. CJK, emoji) become '?', are counted and announced by a visible NOTICE at the end.
* Deterministic: same report and same `created` date -> same bytes -> same SHA-256 (verified by a test).
* Verifies its own output before returning (structure, then the text extracted from the PDF must contain every heading, paragraph, bullet, quote and
  table-cell fragment of the report). Any failure raises PdfError; nothing partial is returned.
Fonts: assets/fonts (not committed), pinned by SHA-256, see src/bridge/fonts.py."""
from __future__ import annotations

import functools
import hashlib
import logging
import re
import warnings
from dataclasses import dataclass
from datetime import datetime

from fontTools.ttLib import TTFont
from fpdf import FPDF
from fpdf.enums import Align, TableBordersLayout, TableCellFillMode, XPos, YPos
from fpdf.fonts import FontFace

from src.bridge.fonts import FontError, verified_fonts

MAX_MARKDOWN_BYTES = 2_000_000
MAX_PDF_BYTES = 5_000_000          # attachment limit (base64 adds ~33% inside the webhook JSON; Gmail itself allows 25 MB)
MAX_PAGES = 200
PAGE_MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 18.0, 16.0, 20.0     # mm
BODY_PT, TABLE_PT, FOOTER_PT = 9.0, 7.6, 7.5
NAVY, AMBER, GREY = (31, 56, 100), (156, 87, 0), (110, 110, 110)
FOOTER_TEXT = "Decision support only - human review required"
_HEADING = re.compile(r"^(#{1,6}) +(.*)$")
_BULLET = re.compile(r"^(\s*)[-*] +(.*)$")
_QUOTE = re.compile(r"^> ?(.*)$")
_ITALIC_LINE = re.compile(r"^_(.+)_$")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
_NUMERIC = re.compile(r"^[-+]?\s*[\d.,]+\s*(%|t|EUR|EUR/t|days?)?$", re.IGNORECASE)
_STATUS = re.compile(r"\*\*Status:\*\*\s*([A-Z]+)")
_WARN_SECTIONS = ("warnings", "unverified concerns", "missing data")
log = logging.getLogger("procurement.pdf")


class PdfError(Exception):
    """The PDF could not be produced or failed its own verification. The message is fixed text and never echoes report content."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class PdfDocument:
    data: bytes
    sha256: str
    pages: int
    replaced_chars: int
    text_pieces: int

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


class _Clean:
    """Makes text drawable with the embedded fonts: drops control characters, replaces glyphs the font lacks by '?' (counted)."""

    def __init__(self, covered: set[int]) -> None:
        self.covered, self.replaced = covered, 0

    def __call__(self, text: str) -> str:
        out = []
        for ch in text.replace("\t", "    "):
            if ch in "\r\n":
                continue
            if ord(ch) < 32 or ord(ch) == 127 or ord(ch) not in self.covered:
                self.replaced += 1
                out.append("?")
            else:
                out.append(ch)
        return "".join(out)


_MARKERS = re.compile(r"(\*\*|`|(?<![\w])_(?=\S)|(?<=\S)_(?![\w]))")


def _runs(text: str) -> list[tuple[str, str]]:
    """Inline markup -> (flags, text) runs: '**bold**' -> B, '`code`' -> C, '_italic_' (underscores at word boundaries) -> I.
    A marker kind without a partner stays literal."""
    parts = _MARKERS.split(text)
    marks = [x for x in parts[1::2]]
    use = {"**": marks.count("**") % 2 == 0, "`": marks.count("`") % 2 == 0, "_": marks.count("_") % 2 == 0}
    on = {"**": False, "`": False, "_": False}
    runs: list[tuple[str, str]] = []
    for k, part in enumerate(parts):
        if k % 2 and use[part]:
            on[part] = not on[part]
            continue
        if part:
            runs.append((("B" if on["**"] else "") + ("I" if on["_"] else "") + ("C" if on["`"] else ""), part))
    return runs or [("", "")]


def _plain(text: str) -> str:
    return "".join(t for _, t in _runs(text))


class _Report(FPDF):
    def __init__(self, label: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.label = label
        self.set_margins(PAGE_MARGIN_X, MARGIN_TOP, PAGE_MARGIN_X)
        self.set_auto_page_break(True, margin=MARGIN_BOTTOM)
        self.alias_nb_pages("{nb}")

    def footer(self) -> None:                                  # called by fpdf2 at the end of every page
        self.set_y(-13)
        self.set_font("DejaVu", "", FOOTER_PT)
        self.set_text_color(*GREY)
        self.set_draw_color(200, 200, 200)
        self.line(self.l_margin, self.get_y() - 1.5, self.w - self.r_margin, self.get_y() - 1.5)
        self.cell(self.epw - 25, 4, f"{self.label}  |  {FOOTER_TEXT}", new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.cell(25, 4, f"Page {self.page_no()} of {{nb}}", align=Align.R)
        self.set_text_color(0, 0, 0)


def _write_runs(pdf: _Report, text: str, size: float, base_style: str = "", lh: float = 4.9, color: tuple[int, int, int] = (0, 0, 0)) -> None:
    """Writes inline-formatted text word by word: a word that does not fit the rest of the line moves to the next line (fpdf2 alone would split
    it at a run boundary)."""
    pdf.set_text_color(*color)
    right = pdf.w - pdf.r_margin
    for flags, chunk in _runs(text):
        if "C" in flags:
            pdf.set_font("DejaVuMono", "", size - 0.8)
        else:
            style = "".join(c for c in "BI" if c in flags or c in base_style)
            pdf.set_font("DejaVu", style, size)
        for tok in re.findall(r"\s+|\S+", chunk):
            if tok.isspace():
                if pdf.get_x() > pdf.l_margin + 0.01:
                    pdf.write(lh, " ")
                continue
            if pdf.get_x() + pdf.get_string_width(tok) + 2 * pdf.c_margin > right and pdf.get_x() > pdf.l_margin + 0.5:     # fpdf2 keeps a cell margin free
                pdf.ln(lh)
                pdf.set_x(pdf.l_margin)
            pdf.write(lh, tok)
    pdf.set_text_color(0, 0, 0)


def _rows(block: list[str]) -> list[list[str]] | None:
    rows = []
    for ln in block:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if all(_SEPARATOR_CELL.match(c) for c in cells):
            continue
        rows.append(cells)
    if not rows or len({len(r) for r in rows}) != 1:
        return None
    return rows


def _word_mm(token: str, size: float, bold: bool) -> float:
    em = sum(0.74 if ch.isupper() else 0.66 for ch in token)           # conservative DejaVu advances
    return em * size * 0.3528 * (1.08 if bold else 1.0)


def _plan_table(rows: list[list[str]], total: float) -> tuple[float, list[float]]:
    """(font size, column widths in mm). Widths follow the content and never go below the longest word; if that does not fit the page at the
    normal size the font is reduced step by step (down to 6.0 pt) before any word would be split."""
    n, head, body = len(rows[0]), rows[0], rows[1:]
    pad = 2.9
    for size in (TABLE_PT, 7.0, 6.5, 6.0):
        minimum = [max([_word_mm(t[:26], size, True) for t in _plain(head[i]).split()] +
                       [_word_mm(t[:26], size, False) for r in body for t in _plain(r[i]).split()] + [size * 0.7]) + pad for i in range(n)]
        if sum(minimum) <= total or size == 6.0:
            break
    if sum(minimum) > total:
        return size, [m * total / sum(minimum) for m in minimum]
    desired = [min(max(len(_plain(r[i])) for r in rows), 70) * size * 0.3528 * 0.62 + pad for i in range(n)]
    extra = [max(d - m, 0.0) for d, m in zip(desired, minimum)]
    spare = total - sum(minimum)
    if sum(extra) <= 0:
        return size, [m + spare / n for m in minimum]
    return size, [m + spare * e / sum(extra) for m, e in zip(minimum, extra)]       # spare width goes to the columns that want more


def _render_table(pdf: _Report, rows: list[list[str]], pieces: list[str], clean: _Clean) -> None:
    cleaned = [[clean(c.replace("**", "")) for c in r] for r in rows]
    size, widths = _plan_table(cleaned, pdf.epw)
    body = cleaned[1:]
    numeric = [bool(body) and any(r[i] for r in body) and all(_NUMERIC.match(r[i]) or not r[i] for r in body) for i in range(len(cleaned[0]))]
    pdf.set_font("DejaVu", "", size)
    pdf.ln(1.5)
    with pdf.table(col_widths=tuple(widths), text_align=tuple("RIGHT" if x else "LEFT" for x in numeric), line_height=size * 0.3528 * 1.45,
                   first_row_as_headings=True, borders_layout=TableBordersLayout.HORIZONTAL_LINES, width=pdf.epw, align="L",
                   headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=NAVY, size_pt=size),
                   cell_fill_color=(240, 244, 250), cell_fill_mode=TableCellFillMode.EVEN_ROWS, padding=(0.9, 1.0), v_align="TOP") as table:
        for r in cleaned:
            row = table.row()
            for c in r:
                row.cell(c)
                if c:
                    pieces.append(c)
    pdf.ln(2.5)


def _banner(pdf: _Report, status: str, warnings_count: int) -> None:
    pdf.set_fill_color(255, 243, 205)
    pdf.set_draw_color(*AMBER)
    pdf.set_line_width(0.4)
    msg = (f"One or more checks did not fully pass ({warnings_count} warning(s) listed). A source may have been unavailable, claims may have been "
           "removed because they could not be verified, or data may be missing. Read the Warnings and Unverified concerns sections before relying on "
           "this report. Human review is required.")
    pdf.set_x(pdf.l_margin)
    pdf.set_font("DejaVu", "B", 10)
    pdf.set_text_color(*AMBER)
    pdf.multi_cell(pdf.epw, 5.5, f"{status} ASSESSMENT", border="LTR", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT, padding=(1.5, 3, 0, 3), align=Align.L)
    pdf.set_font("DejaVu", "", 8.5)
    pdf.set_text_color(60, 40, 0)
    pdf.multi_cell(pdf.epw, 4.4, msg, border="LBR", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT, padding=(0, 3, 1.8, 3), align=Align.L)
    pdf.set_text_color(0, 0, 0)
    pdf.set_fill_color(255, 255, 255)                 # the banner colour must not leak into table rows or other fills
    pdf.set_line_width(0.2)
    pdf.ln(3)


def _room_for_line(pdf: _Report, height: float = 6.0) -> None:
    """Start a new page BEFORE a bullet or quote whose first line would not fit (otherwise the marker and its text end up on different pages)."""
    if pdf.get_y() + height > pdf.page_break_trigger:
        pdf.add_page()


def _end_block(pdf: _Report) -> None:
    pdf.ln(5.2)
    pdf.set_left_margin(PAGE_MARGIN_X)
    pdf.set_x(PAGE_MARGIN_X)


def _render(pdf: _Report, markdown: str, clean: _Clean) -> list[str]:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pieces: list[str] = []
    gap, i, n = 0.0, 0, len(lines)
    sm = _STATUS.search(markdown)
    if sm and sm.group(1) != "COMPLETED":
        in_warn, count = False, 0
        for ln in lines:
            if ln.startswith("## "):
                in_warn = ln[3:].strip().lower() == "warnings"
            elif in_warn and _BULLET.match(ln):
                count += 1
        _banner(pdf, sm.group(1), count)
    while i < n:
        raw = lines[i].rstrip()
        if not raw.strip():
            gap, i = 1.6, i + 1
            continue
        if _RULE.match(raw):
            pdf.ln(1)
            pdf.set_draw_color(200, 200, 200)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(2)
            gap, i = 0.0, i + 1
            continue
        if raw.lstrip().startswith("|"):
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            rows = _rows(lines[i:j])
            if rows is None:                                         # irregular table: keep every row as a plain line
                for ln in lines[i:j]:
                    text = clean(ln.strip())
                    pieces.append(text)
                    pdf.set_font("DejaVu", "", TABLE_PT)
                    pdf.multi_cell(0, 3.8, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align=Align.L)
            else:
                _render_table(pdf, rows, pieces, clean)
            gap, i = 1.6, j
            continue
        text = clean(raw)
        hm = _HEADING.match(text)
        if hm:
            level, title = len(hm.group(1)), hm.group(2).strip()
            size = {1: 17.0, 2: 12.5, 3: 10.5}.get(level, 9.5)
            if pdf.get_y() > pdf.h - pdf.b_margin - 32:             # keep a heading with the text that follows it
                pdf.add_page()
            pdf.ln({1: 1.0, 2: 4.0, 3: 2.5}.get(level, 2.0) + gap)
            low = title.lower()
            color = AMBER if any(low.startswith(s) for s in _WARN_SECTIONS) else NAVY
            pdf.set_font("DejaVu", "B", size)
            pdf.set_text_color(*color)
            pdf.multi_cell(0, size * 0.52, _plain(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align=Align.L)
            if level <= 2:
                pdf.set_draw_color(*color)
                pdf.set_line_width(0.35 if level == 1 else 0.2)
                pdf.line(pdf.l_margin, pdf.get_y() + 0.6, pdf.w - pdf.r_margin, pdf.get_y() + 0.6)
                pdf.set_line_width(0.2)
                pdf.ln(2.2)
            else:
                pdf.ln(0.8)
            pdf.set_text_color(0, 0, 0)
            pieces.append(_plain(title))
            gap, i = 0.0, i + 1
            continue
        pdf.ln(gap)
        bm = _BULLET.match(text)
        if bm:
            indent = 4.5 + 4.5 * (len(bm.group(1)) // 2)
            body = bm.group(2).strip()
            pdf.set_font("DejaVu", "", BODY_PT)
            _room_for_line(pdf)
            y0 = pdf.get_y()
            pdf.set_x(PAGE_MARGIN_X + indent - 3.8)
            pdf.cell(3.6, 4.9, "•")
            pdf.set_left_margin(PAGE_MARGIN_X + indent)
            pdf.set_xy(PAGE_MARGIN_X + indent, y0)
            _write_runs(pdf, body, BODY_PT)
            _end_block(pdf)
            pieces.append(_plain(body))
            gap, i = 0.0, i + 1
            continue
        qm = _QUOTE.match(text)
        if qm:
            body = qm.group(1).strip()
            _room_for_line(pdf)
            pdf.set_left_margin(PAGE_MARGIN_X + 5)
            pdf.set_x(PAGE_MARGIN_X + 5)
            page0, y0 = pdf.page_no(), pdf.get_y()
            _write_runs(pdf, body, BODY_PT, "I", color=(80, 80, 80))
            if pdf.page_no() == page0:
                pdf.set_draw_color(160, 170, 190)
                pdf.set_line_width(0.6)
                pdf.line(PAGE_MARGIN_X + 1.5, y0, PAGE_MARGIN_X + 1.5, pdf.get_y() + 4.4)
                pdf.set_line_width(0.2)
            _end_block(pdf)
            pieces.append(_plain(body))
            gap, i = 0.0, i + 1
            continue
        im = _ITALIC_LINE.match(text.strip())
        body, style = (im.group(1), "I") if im else (text.strip(), "")
        _write_runs(pdf, body, BODY_PT, style, color=(90, 90, 90) if style else (0, 0, 0))
        _end_block(pdf)
        pieces.append(_plain(body))
        gap, i = 0.0, i + 1
    return pieces


@functools.lru_cache(maxsize=8)
def _codepoints(path: str) -> frozenset[int]:
    font = TTFont(str(path), lazy=True)
    try:
        return frozenset(font.getBestCmap())
    finally:
        font.close()


def markdown_to_pdf(markdown: str, *, title: str, footer_label: str, created: datetime) -> PdfDocument:
    """Render the exact report Markdown. `footer_label` is printed in every footer (use the request_id)."""
    if not markdown.strip():
        raise PdfError("PDF_EMPTY", "There is no report text to export.")
    if len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise PdfError("PDF_INPUT_TOO_LARGE", "The report is too large to export as a PDF.")
    try:
        fonts = verified_fonts()
    except FontError as exc:
        raise PdfError(exc.code, exc.message) from None
    covered = set.intersection(*(set(_codepoints(str(fonts[f]))) for f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSansMono.ttf")))
    clean = _Clean(covered)
    pdf = _Report(clean(footer_label))
    for style, name in (("", "DejaVuSans.ttf"), ("B", "DejaVuSans-Bold.ttf"), ("I", "DejaVuSans-Oblique.ttf"), ("BI", "DejaVuSans-BoldOblique.ttf")):
        pdf.add_font("DejaVu", style, str(fonts[name]))
    pdf.add_font("DejaVuMono", "", str(fonts["DejaVuSansMono.ttf"]))
    pdf.set_title(clean(title))
    pdf.set_author("Procurement Risk Agent (decision support)")
    pdf.set_creator("procurement-risk-agent PDF export (fpdf2)")
    pdf.set_creation_date(created)
    pdf.set_compression(True)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")                            # a font or layout warning signals a defect: never ship it silently
            pdf.add_page()
            pieces = _render(pdf, markdown, clean)
            if clean.replaced:
                notice = (f"NOTICE: {clean.replaced} character(s) outside the PDF font's coverage were replaced by '?'. "
                          "The Notion page holds the exact text.")
                pdf.ln(3)
                pdf.set_font("DejaVu", "B", BODY_PT)
                pdf.multi_cell(0, 4.9, notice, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align=Align.L)
                pieces.append(notice)
            if pdf.page_no() > MAX_PAGES:
                raise PdfError("PDF_TOO_MANY_PAGES", "The report is too long to export as a PDF.")
            data = bytes(pdf.output())
    except PdfError:
        raise
    except Exception as exc:  # noqa: BLE001 - fixed message; only the class name reaches the log
        log.error("pdf rendering failed (%s)", type(exc).__name__)
        raise PdfError("PDF_RENDER_FAILED", "The PDF could not be rendered.") from None
    if len(data) > MAX_PDF_BYTES:
        raise PdfError("PDF_TOO_LARGE", "The generated PDF exceeds the attachment size limit.")
    pages = verify_pdf_structure(data)
    from src.bridge.pdf_text import extract_pdf_text
    body = _squash("".join("".join(body_lines(page)) for page in extract_pdf_text(data)))
    if any(_squash(p) not in body for p in pieces):
        raise PdfError("PDF_INCOMPLETE", "The generated PDF does not contain the complete report text; it was not produced.")
    return PdfDocument(data, hashlib.sha256(data).hexdigest(), pages, clean.replaced, len(pieces))


def body_lines(page_lines: list[str]) -> list[str]:
    """The strings of one page without its footer (the footer is the last thing drawn on every page)."""
    for k, ln in enumerate(page_lines):
        if FOOTER_TEXT in ln:
            return page_lines[:k]
    return page_lines


def verify_pdf_structure(data: bytes) -> int:
    """Header, trailer, every xref offset and the page count. Returns the number of pages. Raises PdfError."""
    if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
        raise PdfError("PDF_INVALID", "The generated file is not a well-formed PDF (header or end marker).")
    m = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data)
    if not m or not data[int(m.group(1)):].startswith(b"xref"):
        raise PdfError("PDF_INVALID", "The generated PDF has an invalid cross-reference table.")
    pos = int(m.group(1))
    head = re.match(rb"xref\s+0 (\d+)\s+", data[pos:])
    if not head:
        raise PdfError("PDF_INVALID", "The generated PDF has an invalid cross-reference table.")
    count = int(head.group(1))
    entries = re.findall(rb"(\d{10}) (\d{5}) ([nf])", data[pos + head.end():pos + head.end() + 21 * count])
    if len(entries) != count:
        raise PdfError("PDF_INVALID", "The generated PDF cross-reference table is incomplete.")
    for num, (off, _gen, kind) in enumerate(entries):
        if kind == b"n" and not data[int(off):].startswith(b"%d 0 obj" % num):
            raise PdfError("PDF_INVALID", "A PDF object offset does not match the cross-reference table.")
    at = data.find(b"/Type /Pages")
    if at < 0:
        raise PdfError("PDF_INVALID", "The generated PDF page tree is missing.")
    node = data[data.rfind(b"<<", 0, at):data.find(b">>", at)]          # the page-tree dictionary (key order is not assumed)
    count = re.search(rb"/Count (\d+)", node)
    kids = re.search(rb"/Kids \[(.*?)\]", node, re.DOTALL)
    if not count or not kids or len(re.findall(rb"\d+ 0 R", kids.group(1))) != int(count.group(1)):
        raise PdfError("PDF_INVALID", "The generated PDF page tree is inconsistent.")
    return int(count.group(1))
