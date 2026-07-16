"""Resolve open assignments that still have relative deadline_text and null deadline_date."""

from services.db import get_db
from services.timeutil import item_effective_deadline_dt, to_storage_iso


def main() -> None:
    db = get_db()
    rows = (
        db.table("assignments")
        .select("id,deadline_text,deadline_date,summary,created_at,task,status")
        .eq("status", "open")
        .is_("deadline_date", "null")
        .execute()
        .data
        or []
    )
    updated = 0
    for row in rows:
        text = (row.get("deadline_text") or "").lower()
        if not any(w in text for w in ("tomorrow", "today", "friday", "monday", "tuesday", "wednesday", "thursday", "saturday", "sunday", "end of week")):
            continue
        dt = item_effective_deadline_dt(row)
        if dt is None:
            print("skip unresolved", row["id"], row.get("deadline_text"))
            continue
        iso = to_storage_iso(dt)
        db.table("assignments").update({"deadline_date": iso}).eq("id", row["id"]).execute()
        print("updated", row.get("task"), "->", iso)
        updated += 1
    print(f"done, updated={updated}")


if __name__ == "__main__":
    main()
