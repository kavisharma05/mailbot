"""Download-oriented helpers: extract readable text from common campus file types."""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Literal

logger = logging.getLogger(__name__)

AttachmentIntent = Literal["marks", "reading", "other"]

_MAX_TEXT_CHARS = 12000
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB Telegram-friendly limit
_MAX_MARKS_TELEGRAM_CHARS = 3500
_SKIP_NAME_RE = re.compile(
    r"(logo|signature|spacer|untitled|image0|footer|header)",
    re.IGNORECASE,
)

_MARKS_KEYWORDS = re.compile(
    r"\b(marks?|marksheet|grade|grades|score|scores|result|results|"
    r"cgpa|sgpa|gpa|percentile|rank|roll\s*no|roll\s*number|"
    r"total\s*marks|internal\s*assessment|ia\s*marks|mid\s*term\s*marks)\b",
    re.IGNORECASE,
)

_READING_KEYWORDS = re.compile(
    r"\b(read|reading|readings|chapter|ch\.|case\s*study|case|"
    r"book|article|paper|textbook|handout|prep\s*material|study\s*material)\b",
    re.IGNORECASE,
)

_READING_EXTENSIONS = (".pdf", ".docx", ".doc", ".epub", ".pptx", ".ppt")


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
    for page in reader.pages[:30]:
        text = ""
        try:
            # Layout mode keeps timetable columns closer together
            text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            text = page.extract_text() or ""
        except Exception:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
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


def classify_attachment_intent(
    filename: str,
    extracted_text: str | None,
    *,
    email_subject: str = "",
    email_body: str = "",
) -> AttachmentIntent:
    """Heuristic: marks -> post as text; reading -> forward file."""
    name = (filename or "").lower()
    text = (extracted_text or "").strip()
    context = f"{email_subject} {email_body}".lower()

    if text and _MARKS_KEYWORDS.search(text):
        return "marks"
    if _MARKS_KEYWORDS.search(name) or _MARKS_KEYWORDS.search(context):
        if text and len(text) > 80:
            return "marks"

    if any(name.endswith(ext) for ext in _READING_EXTENSIONS):
        if _READING_KEYWORDS.search(name) or _READING_KEYWORDS.search(context):
            return "reading"
        if _READING_KEYWORDS.search(text[:500] if text else ""):
            return "reading"
        if name.endswith(".pdf") and text and len(text) > 200:
            return "reading"

    if _READING_KEYWORDS.search(name):
        return "reading"

    return "other"


def annotate_attachment_intents(
    attachments: list[dict[str, Any]],
    *,
    email_subject: str = "",
    email_body: str = "",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for att in attachments:
        intent = classify_attachment_intent(
            att.get("filename") or "",
            att.get("extracted_text"),
            email_subject=email_subject,
            email_body=email_body,
        )
        out.append({**att, "intent": intent})
    return out


def format_marks_for_telegram(filename: str, extracted_text: str) -> str:
    """Format marks/score text for inline Telegram display."""
    from services.formatting import escape_html

    body = extracted_text.strip()
    if len(body) > _MAX_MARKS_TELEGRAM_CHARS:
        body = body[:_MAX_MARKS_TELEGRAM_CHARS] + "\n…(truncated)"
    return (
        f"📊 <b>Marks / results</b> — <i>{escape_html(filename)}</i>\n\n"
        f"<pre>{escape_html(body)}</pre>"
    )
