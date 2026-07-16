"""Download-oriented helpers: extract readable text from common campus file types."""

from __future__ import annotations

import io
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 6000
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB Telegram-friendly limit
_SKIP_NAME_RE = re.compile(
    r"(logo|signature|spacer|untitled|image0|footer|header)",
    re.IGNORECASE,
)


def should_forward_attachment(att: dict[str, Any]) -> bool:
    filename = att.get("filename") or ""
    size = int(att.get("size") or 0)
    mime = (att.get("mime_type") or "").lower()
    if size <= 0 or size > _MAX_FILE_BYTES:
        return False
    if _SKIP_NAME_RE.search(filename) and mime.startswith("image/"):
        return False
    # Skip tiny inline signature images
    if mime.startswith("image/") and size < 15_000:
        return False
    return bool(filename)


def extract_text_from_bytes(filename: str, mime_type: str, data: bytes) -> str:
    if not data:
        return ""
    name = (filename or "").lower()
    mime = (mime_type or "").lower()

    try:
        if name.endswith(".pdf") or "pdf" in mime:
            return _extract_pdf(data)
        if name.endswith((".xlsx", ".xlsm")) or "spreadsheet" in mime:
            return _extract_xlsx(data)
        if name.endswith(".csv") or mime == "text/csv":
            return data.decode("utf-8", errors="replace")[:_MAX_TEXT_CHARS]
        if name.endswith(".docx") or "wordprocessingml" in mime:
            return _extract_docx(data)
        if name.endswith((".txt", ".md")) or mime.startswith("text/"):
            return data.decode("utf-8", errors="replace")[:_MAX_TEXT_CHARS]
    except Exception as exc:
        logger.warning("Failed extracting text from %s: %s", filename, exc)
    return ""


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    chunks: list[str] = []
    for page in reader.pages[:20]:
        text = page.extract_text() or ""
        if text.strip():
            chunks.append(text.strip())
    return "\n".join(chunks)[:_MAX_TEXT_CHARS]


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in wb.worksheets[:5]:
        lines.append(f"[Sheet: {sheet.title}]")
        for i, row in enumerate(sheet.iter_rows(values_only=True)):
            if i >= 80:
                break
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)[:_MAX_TEXT_CHARS]


def _extract_docx(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    # Tables often hold timetables
    for table in doc.tables[:10]:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)[:_MAX_TEXT_CHARS]


def combine_attachment_text(attachments: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for att in attachments:
        text = (att.get("extracted_text") or "").strip()
        if not text:
            continue
        name = att.get("filename") or "attachment"
        blocks.append(f"--- Attachment: {name} ---\n{text}")
    return "\n\n".join(blocks)[:_MAX_TEXT_CHARS]
