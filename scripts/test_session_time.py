from ai.deadline import normalize_classification

BODY = """
Dear students,
Please complete the case on the Brady Training Program and the chapter on values.
We will complete both in tomorrow's session.
Looking forward to seeing you tomorrow at 9:45 am.
"""

r = normalize_classification(
    {
        "is_actionable": True,
        "category": "deadline",
        "course_or_topic": "OB",
        "task": "Prepare case and chapter readings",
        "deadline_text": "tomorrow",
        "deadline_date": None,
        "summary": "Complete readings for tomorrow session",
        "confidence": "high",
    },
    received_at="2026-07-15T15:07:00+00:00",
    body=BODY,
)
print("deadline_text:", r["deadline_text"])
print("deadline_date:", r["deadline_date"])
assert r["deadline_date"] and "09:45" in r["deadline_date"].replace(" ", "")
print("OK: session time 9:45 applied")
