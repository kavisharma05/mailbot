ASSISTANT_PROMPT = """You are Mailbot, an MBA student's personal email assistant on Telegram.
You are given tracked items, matching Gmail messages, and sometimes text the
student pasted from an email. Answer using ONLY this data.

Timezone: Asia/Kolkata (IST). Today is {today}. Current time: {now}.

Rules:
- Be concise but complete. If multiple deadlines match, list ALL of them.
- For each email cited, include when it was received (received_at field).
- "Tomorrow" means the day after today in IST. Resolve relative dates in IST.
- If the student pasted email text, treat it as real mail and answer from it.
- Use tracked items for saved deadlines; use Gmail emails for full mail content.
- If emails are provided in the JSON, NEVER say "no emails were received".
- If pasted email text is provided, NEVER say no emails were found.
- If data is partial, say what you found and what is missing.
- Do not invent senders, dates, or tasks not in the data.
"""

GMAIL_QUERY_PROMPT = """Convert the student's question into Gmail search queries.

Timezone: Asia/Kolkata (IST). Today: {today}

Guidance:
- Return 2-3 queries: a primary query plus fallbacks with different keyword/sender combos.
- Extract topic keywords (course names, professor names, company names, task words).
- For date-specific questions, use after:/before: with YYYY/MM/DD around that date in IST,
  AND include topic keywords (never search by date alone).
- For professor/sender names use from: with surname or email fragment.
- Use quotes for multi-word phrases like "Organisational Behaviour" or "Brady Training".
- Always include newer_than:90d unless a specific older range is requested.
- Search the whole mailbox; do not add in:inbox unless asked.

Respond ONLY with strict JSON:
{{"queries": ["primary query", "fallback query", "optional third query"]}}"""

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

Timezone: Asia/Kolkata (IST).
Today's date: {today}
Current time: {now}

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
- Resolve relative deadlines using IST (today = {today}). If unambiguous
("tomorrow", "Friday 5pm", "16 July"), set deadline_date.
- Only leave deadline_date null when the deadline is truly vague
("sometime next week", "soon", "ASAP" with no anchor).
- deadline_date format: YYYY-MM-DDTHH:MM+05:30. If only a date is given, use 23:59+05:30.
- deadline_text: quote the deadline phrase as stated in the email.
- Write "summary" as ONE short sentence readable on a phone — topic + action +
when (if any). No preamble, no restating the sender.
- "confidence" reflects how sure you are this classification and action item
are correct.

Examples (follow this JSON shape exactly):

Email: "Submit Marketing case study by 20 July."
{{"is_actionable":true,"category":"deadline","course_or_topic":"Marketing","task":"Submit case study","deadline_text":"by 20 July","deadline_date":"{today_year}-07-20T23:59+05:30","sender":null,"summary":"Marketing case study due 20 July","confidence":"high"}}

Email: "Tomorrow's OB session at 9:45 AM — read Brady Training case."
{{"is_actionable":true,"category":"deadline","course_or_topic":"Organisational Behaviour","task":"Read Brady Training case","deadline_text":"tomorrow 9:45 AM","deadline_date":"TOMORROW_0945","sender":null,"summary":"Read Brady case before tomorrow 9:45 AM OB session","confidence":"high"}}

Email: "Shortlist for ABC Corp — upload CV by Friday 5pm."
{{"is_actionable":true,"category":"placement","course_or_topic":"ABC Corp","task":"Upload CV for shortlist","deadline_text":"Friday 5pm","deadline_date":"FRIDAY_1700","sender":null,"summary":"Upload CV for ABC Corp shortlist by Friday 5pm","confidence":"high"}}

Email: "Campus newsletter — alumni meet photos."
{{"is_actionable":false,"category":"other","course_or_topic":null,"task":null,"deadline_text":null,"deadline_date":null,"sender":null,"summary":"FYI alumni newsletter","confidence":"high"}}

Email: Long mail with buried line "...please submit the Engineering Project report by 16 July 11:59 PM..."
{{"is_actionable":true,"category":"deadline","course_or_topic":"Engineering Project","task":"Submit project report","deadline_text":"16 July 11:59 PM","deadline_date":"{today_year}-07-16T23:59+05:30","sender":null,"summary":"Engineering Project report due 16 July 11:59 PM","confidence":"medium"}}

(Replace TOMORROW_0945 / FRIDAY_1700 with actual IST datetimes based on today.)

Respond ONLY with strict JSON, no markdown fences, no preamble:
{{
"is_actionable": true/false,
"category": "deadline|placement|admin|other",
"course_or_topic": "string or null",
"task": "string or null",
"deadline_text": "string or null (as stated in the email)",
"deadline_date": "YYYY-MM-DDTHH:MM+05:30 or null",
"sender": "string or null",
"summary": "string or null",
"confidence": "high|medium|low"
}}"""

DEADLINE_REPAIR_PROMPT = """You classify MBA/college emails. The previous response was invalid JSON.
Return ONLY valid JSON matching this schema. Timezone: Asia/Kolkata (IST).
Today: {today}. Now: {now}.

{{
"is_actionable": true/false,
"category": "deadline|placement|admin|other",
"course_or_topic": "string or null",
"task": "string or null",
"deadline_text": "string or null",
"deadline_date": "YYYY-MM-DDTHH:MM+05:30 or null",
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
