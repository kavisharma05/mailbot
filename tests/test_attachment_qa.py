"""Tests for attachment-aware chat Q&A context."""

from collections import OrderedDict
from unittest.mock import patch

from ai.assistant import (
    _apply_attachment_budget,
    _build_user_prompt,
    _email_for_prompt,
    _extract_person_query,
    _fetch_relevant_emails,
    _MAX_ATTACHMENT_EMAILS,
    _MAX_USER_CHARS,
    _wants_attachment_text,
)
from services import telegram_dedupe


def test_wants_attachment_text_marks_question():
    assert _wants_attachment_text("Tell me about the marks i got in shanthi mam")
    assert _wants_attachment_text("what are my grades from OB")
    assert not _wants_attachment_text("what's due tomorrow")


def test_extract_person_query_mam():
    assert _extract_person_query("marks i got in shanthi mam") == "shanthi"
    assert _extract_person_query("Tell me about marks from Santhi mam") == "Santhi"


def test_email_for_prompt_includes_attachment_text():
    details = {
        "message_id": "abc123",
        "subject": "Group assignment marks",
        "sender": "Dr. Santhi Perumal <santhi@college.edu>",
        "received_at": "2026-07-21 05:55 PM IST",
        "body": "Your marks vary between 6 and 8 marks.",
        "attachments": [{"filename": "group_marks.pdf"}],
        "attachment_text": "--- Attachment: group_marks.pdf ---\nGroup A | 8\nGroup B | 6",
    }
    item = _email_for_prompt(details, include_attachments=True)
    assert "attachment_text" in item
    assert "Group A" in item["attachment_text"]
    assert item["attachment_filenames"] == ["group_marks.pdf"]


@patch("ai.assistant.search_message_ids")
@patch("ai.assistant.get_message_details")
def test_fetch_relevant_emails_includes_attachment_text(mock_details, mock_search):
    mock_search.return_value = ["msg1"]
    mock_details.return_value = {
        "message_id": "msg1",
        "subject": "Marks",
        "sender": "prof@college.edu",
        "received_at": "2026-07-21 05:55 PM IST",
        "body": "See attached marksheet.",
        "attachments": [{"filename": "marks.pdf"}],
        "attachment_text": "Kavish Sharma | 8",
    }

    emails = _fetch_relevant_emails("what marks did I get")
    assert len(emails) == 1
    assert emails[0]["attachment_text"] == "Kavish Sharma | 8"
    mock_details.assert_called_once_with("msg1", load_attachments=True)


def test_apply_attachment_budget_limits_email_count():
    emails = [
        {"message_id": f"m{i}", "attachment_text": f"row{i} | {i}"}
        for i in range(5)
    ]
    budgeted = _apply_attachment_budget(emails, wants_attachments=True)
    assert len(budgeted) == 5
    assert "attachment_text" in budgeted[0]
    assert "attachment_text" in budgeted[_MAX_ATTACHMENT_EMAILS - 1]
    assert "attachment_text" not in budgeted[_MAX_ATTACHMENT_EMAILS]


def test_build_user_prompt_keeps_question_with_huge_attachment():
    question = "Tell me about the marks i got in shanthi mam"
    marks_token = "Kavish Sharma | 8"
    huge_sheet = marks_token + ("x" * 20000)
    emails = [
        {
            "subject": "Group assignment marks",
            "sender": "Dr. Santhi Perumal",
            "received_at": "2026-07-21 05:55 PM IST",
            "body": "See attached.",
            "attachment_text": huge_sheet,
        }
    ]
    prompt = _build_user_prompt(question, [], emails, None, None)
    assert question in prompt
    assert prompt.endswith(question)
    assert len(prompt) <= _MAX_USER_CHARS + 50
    assert marks_token in prompt or "Kavish Sharma" in prompt


def test_telegram_dedupe_ordered_dict_popitem():
    telegram_dedupe._seen.clear()
    telegram_dedupe._seen.update({i: float(i) for i in range(telegram_dedupe._MAX_ENTRIES + 1)})
    assert isinstance(telegram_dedupe._seen, OrderedDict)
    telegram_dedupe._seen.popitem(last=False)
    telegram_dedupe._seen.clear()
