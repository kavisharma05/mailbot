"""Tests for classification helpers, attachment intent, and formatting."""

from ai.deadline import _boost_category_from_signals, normalize_classification
from services.attachments import classify_attachment_intent
from services.formatting import is_priority_item, normalize_category


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
    assert is_priority_item("placement", task="Upload CV")
    assert is_priority_item("certification", task="Complete NSE cert")
    assert is_priority_item("deadline", task="Read Brady case", summary="Prepare reading")
    assert not is_priority_item("other", task="Newsletter")


def test_normalize_category_includes_certification():
    assert normalize_category("certification") == "certification"
    assert normalize_category("bogus") == "other"


def test_normalize_classification_preserves_certification():
    out = normalize_classification(
        {
            "is_actionable": True,
            "category": "certification",
            "task": "Submit certificate",
            "confidence": "high",
        }
    )
    assert out["category"] == "certification"
