"""One-shot: fix Brady OB deadline anchored to email received day."""

from ai.deadline import normalize_classification
from services.db import get_db
from services.timeutil import item_effective_deadline_dt

AID = "24928774-c7d5-4aee-afdd-2ae6943e6234"


def main() -> None:
    db = get_db()
    row = db.table("assignments").select("*").eq("id", AID).limit(1).execute().data[0]
    print("before effective:", item_effective_deadline_dt(row))

    fixed = normalize_classification(
        {
            "is_actionable": True,
            "category": "deadline",
            "course_or_topic": row.get("course_or_topic"),
            "task": row.get("task"),
            "deadline_text": "tomorrow's session at 9:45 am",
            "deadline_date": None,
            "summary": row.get("summary"),
            "confidence": "high",
        },
        received_at=row["created_at"],
    )
    print("normalized:", fixed["deadline_date"], fixed["deadline_text"])

    db.table("assignments").update(
        {
            "deadline_date": fixed["deadline_date"],
            "deadline_text": "tomorrow's session at 9:45 am",
        }
    ).eq("id", AID).execute()

    row2 = (
        db.table("assignments")
        .select("deadline_date,deadline_text,task,created_at")
        .eq("id", AID)
        .limit(1)
        .execute()
        .data[0]
    )
    print("after:", row2)
    print("effective:", item_effective_deadline_dt(row2))


if __name__ == "__main__":
    main()
