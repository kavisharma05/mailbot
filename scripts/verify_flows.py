"""End-to-end verification of the user-facing flows that were broken."""

from __future__ import annotations

from config import get_settings

get_settings.cache_clear()

print("=" * 60)
print("1) MAIL DRAFTING (compose_email)")
print("=" * 60)
from ai.composer import compose_email

draft = compose_email(
    "Ask Professor Anitha for leave tomorrow because I have a fever. Keep it short and polite."
)
if draft and draft.get("subject") and draft.get("body"):
    print("  OK — subject:", draft["subject"])
    print("  body preview:", draft["body"][:120].replace("\n", " "))
else:
    print("  FAIL:", draft)

print()
print("=" * 60)
print("2) DATABASE Q&A (answer_question on tracked items)")
print("=" * 60)
from services.db import get_assignments_for_qa
from ai.assistant import answer_question

items = get_assignments_for_qa(limit=25)
print(f"  tracked items loaded: {len(items)}")
answer = answer_question("what are my deadlines today and tomorrow?", items, scan_gmail=False)
print("  answer preview:", (answer or "")[:300].replace("\n", " | "))

print()
print("=" * 60)
print("3) CONTACT / EMAIL-ADDRESS LOOKUP")
print("=" * 60)
import time

from services.db import get_db
from services.contacts import resolve_contact

rows = []
for attempt in range(3):
    try:
        db = get_db()
        rows = (
            db.table("contacts")
            .select("display_name, email, message_count")
            .order("message_count", desc=True)
            .limit(5)
            .execute()
            .data
            or []
        )
        break
    except Exception as exc:
        print(f"  DB read attempt {attempt + 1} failed (transient DNS?): {str(exc)[:80]}")
        time.sleep(2)
print(f"  contacts in DB (top 5): {len(rows)}")
for r in rows:
    print(f"    {r.get('display_name')} <{r.get('email')}> x{r.get('message_count')}")
if rows:
    name = rows[0].get("display_name") or rows[0].get("email")
    matches = resolve_contact(name, min_score=0.3)
    print(f"  resolve_contact({name!r}) -> {len(matches)} match(es)")
    if matches:
        print("    best:", matches[0].get("display_name"), matches[0].get("email"))

print()
print("=" * 60)
print("4) CLASSES (schedule from DB template, no LLM refresh)")
print("=" * 60)
from services.schedule import load_template_sessions, format_upcoming_classes
from services.db import get_preferred_section

try:
    template = load_template_sessions()
except Exception as exc:
    template = []
    print("  template load failed (transient DNS?):", str(exc)[:80])
print(f"  template sessions loaded: {len(template)}")
if template:
    section = get_preferred_section()
    text = format_upcoming_classes(days=7, section=section, template=template)
    print("  upcoming classes preview:", (text or "(none in next 7 days)")[:300].replace("\n", " | "))
else:
    print("  (no timetable stored yet — needs a timetable mail + /classes refresh)")
