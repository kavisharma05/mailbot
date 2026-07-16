"""Find Saleena Wright/Fehr mail and whether it was processed."""

from gmail_api.client import get_message_details, search_message_ids
from services.db import get_db, is_email_processed


def main() -> None:
    queries = [
        "from:skhan@imtnag.ac.in newer_than:2d",
        "Wright Fehr newer_than:2d",
        "from:skhan subject:reading newer_than:2d",
    ]
    seen: set[str] = set()
    for q in queries:
        try:
            ids = search_message_ids(q, max_results=15)
        except Exception as exc:
            print("search fail", q, exc)
            continue
        print("QUERY", q, "->", len(ids))
        for mid in ids:
            if mid in seen:
                continue
            seen.add(mid)
            d = get_message_details(mid)
            body = d.get("body") or ""
            print("---", mid)
            print("subject:", d.get("subject"))
            print("from:", d.get("sender"))
            print("received:", d.get("received_at"), "|", d.get("received_at_iso"))
            print("processed:", is_email_processed(mid))
            print("has Wright:", "Wright" in body or "wright" in body.lower())
            print("snippet:", body[:240].replace("\n", " "))

    db = get_db()
    pe = (
        db.table("processed_emails")
        .select("message_id,is_actionable,confidence,assignment_id,created_at")
        .order("created_at", desc=True)
        .limit(20)
        .execute()
        .data
        or []
    )
    print("\nRECENT PROCESSED")
    for row in pe:
        print(row)

    a = (
        db.table("assignments")
        .select("id,task,summary,deadline_date,deadline_text,created_at,gmail_message_id")
        .order("created_at", desc=True)
        .limit(15)
        .execute()
        .data
        or []
    )
    print("\nRECENT ASSIGNMENTS")
    for row in a:
        print(row.get("created_at"), row.get("task"), row.get("deadline_date"), row.get("gmail_message_id"))


if __name__ == "__main__":
    main()
