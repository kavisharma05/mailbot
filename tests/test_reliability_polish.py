"""Tests for classification helpers, attachment intent, and formatting."""

from ai.deadline import (
    _boost_category_from_signals,
    correct_category_from_text,
    normalize_classification,
)
from services.attachments import classify_attachment_intent
from services.formatting import is_priority_item, normalize_category


def test_class_presentation_is_not_certification():
    result = _boost_category_from_signals(
        "OB group presentation Wednesday",
        "Deliver a 10-12 min class presentation. Rubric attached. Certificate of participation for attendees.",
        "faculty@college.edu",
        {
            "category": "certification",
            "task": "Deliver OB group presentation",
            "summary": "10–12 min OB group presentation covering concepts and teamwork",
            "is_actionable": True,
            "confidence": "medium",
        },
    )
    assert result["category"] == "deadline"


def test_correct_category_from_text_presentation():
    assert (
        correct_category_from_text(
            "certification",
            "Deliver OB group presentation",
            "Review rubric covering OB concepts",
        )
        == "deadline"
    )
    assert (
        correct_category_from_text(
            "certification",
            "Complete Bloomberg certification",
            "Bloomberg Market Concepts module",
        )
        == "certification"
    )


def test_certification_keyword_boost():
    result = _boost_category_from_signals(
        "NSE certification deadline",
        "Please complete Bloomberg certification by Friday.",
        "admin@college.edu",
        {"category": "admin", "is_actionable": True, "confidence": "low"},
    )
    assert result["category"] == "certification"
    assert result["confidence"] == "medium"


def test_marks_attachment_intent():
    intent = classify_attachment_intent(
        "Mid_term_marks.pdf",
        "Roll No 123 Total Marks 85 CGPA 8.2",
        email_subject="Internal assessment marks",
    )
    assert intent == "marks"


def test_reading_attachment_intent():
    intent = classify_attachment_intent(
        "Brady_Training_Case.pdf",
        "Case study on organizational behaviour chapter reading assignment",
        email_subject="Read Brady case for tomorrow",
    )
    assert intent == "reading"


def test_priority_categories():
    from datetime import datetime, timedelta, timezone

    near = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    assert is_priority_item("placement", task="Upload CV")
    assert is_priority_item("certification", task="Complete NSE cert")
    assert is_priority_item("deadline", task="Submit report", deadline_date=near)
    assert not is_priority_item("deadline", task="Read Brady case", summary="Prepare reading")
    assert not is_priority_item("other", task="Newsletter")


def test_normalize_category_includes_certification():
    assert normalize_category("certification") == "certification"
    assert normalize_category("bogus") == "other"


def test_normalize_classification_rewrites_presentation():
    out = normalize_classification(
        {
            "is_actionable": True,
            "category": "certification",
            "task": "Deliver OB group presentation",
            "summary": "10–12 min presentation on OB concepts",
            "confidence": "high",
        }
    )
    assert out["category"] == "deadline"
    assert out["item_type"] == "assignment"
    out = normalize_classification(
        {
            "is_actionable": True,
            "category": "certification",
            "task": "Submit certificate",
            "confidence": "high",
        }
    )
    assert out["category"] == "certification"
