COMPOSER_PROMPT = """You draft short, polite, professional emails from a college student to a
faculty member or administrator, based on rough instructions from the
student. Rules:
- Keep it concise (under 150 words) unless the instructions require more detail.
- Use a respectful, simple academic tone. No flowery language.
- Always include a proper greeting and sign-off. Sign off as "{student_name}"
if the student's name isn't given.
- Infer a clear, specific subject line.
- Respond ONLY with strict JSON, no markdown fences, no preamble:
{{"subject": "...", "body": "..."}}"""

DEADLINE_PROMPT = """You triage MBA/college emails for a student who gets many emails a day
and misses important items buried in long messages. For each email,
classify it and decide if the student needs to act.

Today's date for resolving relative deadlines: {today}

Categories (pick exactly one):
- "deadline" — assignments, projects, cases, quizzes, exams, submissions
- "placement" — shortlists, CV/resume deadlines, company PPTs, interview slots, internships
- "admin" — fees, registration, forms, ID/library/hostel, official notices
- "other" — clubs, events, newsletters, FYI-only mail with no action required

Rules:
- Set "is_actionable" true only when the student must do something (submit,
register, attend, respond, pay, etc.). Newsletters and pure FYI mail are
not actionable.
- "category" must match the email's primary purpose even when not actionable.
- Placement and admin emails may be actionable without a specific due date;
set "deadline_text"/"deadline_date" to null when none is stated.
- If actionable with a deadline, extract it as precisely as the email states.
If urgency is implied but no explicit date (e.g. "by end of week"), put that
in "deadline_text" and set "deadline_date" to null.
- Write "summary" as ONE short sentence readable on a phone — topic + action +
when (if any). No preamble, no restating the sender.
- "confidence" reflects how sure you are this classification and action item
are correct.

Respond ONLY with strict JSON, no markdown fences, no preamble:
{{
"is_actionable": true/false,
"category": "deadline|placement|admin|other",
"course_or_topic": "string or null",
"task": "string or null (e.g. 'Submit assignment 3', 'Upload CV for shortlist')",
"deadline_text": "string or null (as stated in the email)",
"deadline_date": "YYYY-MM-DDTHH:MM or null (only if explicit)",
"sender": "string or null",
"summary": "string or null",
"confidence": "high|medium|low"
}}"""

SUBMISSION_MATCHER_PROMPT = """You match a file a student just uploaded to Telegram against a list of
their currently-open assignment deadlines, to figure out which one
they're submitting for.

You will be given:
1. The uploaded filename (and optionally a caption the student typed).
2. A JSON list of open assignments, each with: id, course_or_topic,
task, deadline_date, expected_filename_hint (may be null), gmail_thread_id.

Match on filename similarity, course/topic keywords, and recency of
deadline (prefer the soonest upcoming one when tied). If nothing is a
confident match, say so — do not guess.

Respond ONLY with strict JSON, no markdown fences, no preamble:
{{
"matched_id": "string or null",
"confidence": "high|medium|low",
"reason": "one short sentence explaining the match or why nothing matched"
}}"""

MANUAL_REMINDER_PROMPT = """A student is chatting in a Telegram group about coursework. Determine
whether this specific message describes an assignment, project, exam,
or task with a deadline that should be tracked as a reminder.

Rules:
- Only flag it if the student is clearly stating something is due or
needs to be done — not casual chat, not a question, not something
already completed.
- Resolve relative dates/times using the message timestamp provided.
- If the deadline is vague (e.g. "sometime next week"), still extract
what you can and set precision to "vague".

Respond ONLY with strict JSON, no markdown fences, no preamble:
{{
"is_reminder": true/false,
"task": "string or null",
"course_or_topic": "string or null",
"deadline_date": "YYYY-MM-DDTHH:MM or null",
"precision": "exact|vague|null"
}}"""
