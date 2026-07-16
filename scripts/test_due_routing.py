from ai.assistant import (
    _deterministic_deadline_answer,
    _is_due_list_question,
    _is_mailbox_question,
    _llm_answer_is_useless,
)

qs = [
    "Check pending deadlines for today and tomorrow",
    "Bro deadlines for today and tomorrow please check",
]
for q in qs:
    print(q)
    print(" due", _is_due_list_question(q), "mailbox", _is_mailbox_question(q))

print(
    "useless",
    _llm_answer_is_useless(
        "You haven't asked a question yet. Please go ahead and ask."
    ),
)
items = [
    {
        "task": "Brady",
        "deadline_date": "2026-07-16T09:45:00+05:30",
        "deadline_text": "9:45",
        "status": "open",
        "course_or_topic": "OB",
    }
]
print(_deterministic_deadline_answer(qs[0], items))
