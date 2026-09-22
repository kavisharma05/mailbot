import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai.assistant import (
    _extract_person_query,
    _is_due_list_question,
    _is_missed_list_question,
    answer_question,
    infer_person_email,
    lookup_contact_email,
    split_question_and_paste,
)
from ai.composer import compose_email
from ai.intent import (
    INTENT_ADD_TASK,
    INTENT_COMPOSE,
    INTENT_CONTACT_LOOKUP,
    INTENT_DUE_LIST,
    INTENT_MAILBOX_QA,
    INTENT_MISSED_LIST,
    INTENT_SCHEDULE,
    INTENT_SMALLTALK,
    INTENT_UNDATED_LIST,
    _is_specific_deadline_question,
    classify_message,
)
from config import get_settings
from gmail_api.client import send_email
from services.user_ids import OWNER_SEED_USER_ID
from services.contacts import (
    backfill_contacts_from_inbox,
    extract_person_reference,
    looks_like_recipient_switch,
    name_tokens,
    normalize_name_key,
    parse_sender,
    resolve_email_robust,
    canonical_person_name,
    upsert_contact_from_sender,
)
from services.media_ai import analyze_image, transcribe_audio
from services.watcher import watcher_lock
from services.conversation import clear_convo, get_convo, remember_turn
from services.alert_actions import (
    ACT_DISMISS,
    ACT_DONE,
    ACT_REMIND,
    ACT_REPLY,
    ACT_SNOOZE,
    SNOOZE_TOMORROW,
    parse_action_callback,
)
from services.db import (
    count_dead_letter_emails,
    count_pending_emails,
    count_pending_scheduled_reminders,
    create_manual_assignment,
    dismiss_assignment,
    get_assignment_by_id,
    get_assignments_due_between,
    get_assignments_due_on,
    get_assignments_for_qa,
    get_digest_state,
    get_missed_assignments,
    get_preferred_section,
    get_undated_assignments,
    get_upcoming_assignments,
    get_watch_state,
    log_low_confidence,
    mark_assignment_done,
    schedule_assignment_reminder,
    set_preferred_section,
    update_assignment,
)
from services.formatting import (
    CATEGORY_LABELS,
    VALID_CATEGORIES,
    escape_html,
    format_dated_due_list,
    format_deadline,
    format_due_list,
    format_missed_list,
    format_undated_list,
    normalize_category,
)
from services.schedule import (
    _format_session_block,
    _session_section,
    classes_command_text,
    load_template_sessions,
    is_schedule_challenge,
    next_sessions_after,
    project_sessions_to_day,
    refresh_schedule_from_gmail,
    resolve_schedule_day,
    sessions_for_day,
)
from services.telegram_util import reply_long, safe_user_error_message
from services.timeutil import DayWindow, IST, now_ist, parse_day_window, today_ist

logger = logging.getLogger(__name__)

INSTRUCTIONS, RECIPIENT, CONFIRM = range(3)

COMPOSE_NEED_CONTENT = "need_content"
COMPOSE_NEED_RECIPIENT = "need_recipient"
COMPOSE_CONFIRM = "confirm"

ADD_TASK_NEED_TASK = "need_task"
ADD_TASK_NEED_DEADLINE = "need_deadline"

EDIT_TASK_EDITING_TASK = "editing_task"
EDIT_TASK_EDITING_DEADLINE = "editing_deadline"
EDIT_TASK_EDITING_CATEGORY = "editing_category"

ADDTASK_CB = "addtask"
EDITTASK_CB = "edittask"

APPROVE_CB = "send_approve"
CANCEL_CB = "send_cancel"
CLEAR_CHAT_CB = "ui:clear_chat"

QUICK_ACTION_LABELS = ("📚 Classes", "⏰ Due", "➕ Add", "✉️ Send", "📋 Section")

_TASK_CANCEL_WORDS = frozenset({"cancel", "stop", "discard", "abort", "/cancel"})

# How many recent message IDs to try when clearing bot clutter in owner DM
_CLEAR_CHAT_SCAN_LIMIT = 25

GREETINGS = {"hi", "hey", "hello", "yo", "sup", "hii", "hiii", "heya"}

SCHEDULE_KEYWORDS = ("class", "classes", "schedule", "timetable", "time table", "lecture")

REFRESH_KEYWORDS = (
    "check again",
    "look again",
    "try again",
    "refresh",
    "recheck",
    "update",
    "reload",
    "why not check",
)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Inline tags / prefixes for live Gmail-verified answers (#verify is the documented token)
_VERIFY_TAG_RE = re.compile(
    r"(?:^|\s)(?:#(?:verify|live|realtime)\b|(?:verify|live):\s*)",
    re.IGNORECASE,
)


def _extract_verify_tag(text: str) -> tuple[bool, str]:
    """Return (is_verified, question_with_tag_stripped)."""
    if not text:
        return False, ""
    if not _VERIFY_TAG_RE.search(text):
        return False, text.strip()
    cleaned = _VERIFY_TAG_RE.sub(" ", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return True, cleaned


def _resolve_user(update: Update) -> dict[str, Any] | None:
    from services.users import ensure_owner_user, get_owner_user, get_user_by_telegram_id

    tg = update.effective_user
    if not tg:
        return None
    settings = get_settings()
    owner_id = (
        settings.owner_user_id()
        if hasattr(settings, "owner_user_id")
        else getattr(settings, "telegram_owner_user_id", "")
        or getattr(settings, "telegram_owner_chat_id", "")
    )
    if str(tg.id) == str(owner_id):
        return get_owner_user() or {
            "id": OWNER_SEED_USER_ID,
            "role": "owner",
            "telegram_user_id": str(tg.id),
            "telegram_chat_id": str(getattr(update.effective_chat, "id", tg.id)),
            "is_active": True,
        }
    ensure_owner_user()
    row = get_user_by_telegram_id(tg.id)
    if row:
        return row
    return None


def _require_user(update: Update) -> dict[str, Any] | None:
    user = _resolve_user(update)
    if user:
        from services.mailbox_user import set_user

        set_user(user)
    return user


def _is_owner(update: Update) -> bool:
    from services.users import is_owner_user

    user = _resolve_user(update)
    return is_owner_user(user)


def _category_for_chat(chat_id: int | str | None) -> str | None:
    if chat_id is None:
        return None
    settings = get_settings()
    chat = str(chat_id)
    # Build imperatively so certification's "fallback to placements" env default
    # never overwrites the placements group's category mapping.
    mapping: dict[str, str] = {}
    for cid, category in (
        (settings.telegram_chat_deadlines, "deadline"),
        (settings.telegram_chat_placements, "placement"),
        (settings.telegram_chat_admin, "admin"),
        (settings.telegram_chat_other, "other"),
    ):
        if cid:
            mapping[str(cid)] = category
    cert_chat = settings.telegram_chat_certification
    if cert_chat:
        mapping[str(cert_chat)] = "certification"
    return mapping.get(chat)


def _sort_items_priority_first(items: list[dict]) -> list[dict]:
    from services.formatting import sort_assignments

    return sort_assignments(items)


def _format_due_list(items: list[dict], category: str | None) -> tuple[str, list[str]]:
    return format_due_list(items, category)


def _format_missed_list(items: list[dict], category: str | None) -> tuple[str, list[str]]:
    return format_missed_list(items, category)


def _format_undated_list(items: list[dict], category: str | None) -> tuple[str, list[str]]:
    return format_undated_list(items, category)


async def _fetch_assignment_list(
    category: str | None,
    *,
    source: str = "due",
) -> tuple[str, list[str]]:
    if source == "missed":
        items = await asyncio.to_thread(get_missed_assignments, category=category)
        return _format_missed_list(items, category)
    if source == "undated":
        items = await asyncio.to_thread(get_undated_assignments, category=category)
        return _format_undated_list(items, category)
    items = await asyncio.to_thread(get_upcoming_assignments, category=category)
    return _format_due_list(items, category)


async def _fetch_dated_due_list(
    window: DayWindow,
    category: str | None,
) -> tuple[str, list[str]]:
    if window.single:
        items = await asyncio.to_thread(get_assignments_due_on, window.start, IST, category)
    else:
        items = await asyncio.to_thread(
            get_assignments_due_between,
            window.start,
            window.end,
            IST,
            category,
        )
    return format_dated_due_list(items, window.label, category)


async def _reply_dated_due_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    window: DayWindow,
) -> None:
    if not update.message:
        return
    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
    text, due_ids = await _fetch_dated_due_list(window, category)
    _store_assignment_list(context, due_ids, source="due")
    await reply_long(update.message, text, parse_mode="HTML")


def _store_assignment_list(context: ContextTypes.DEFAULT_TYPE, due_ids: list[str], *, source: str) -> None:
    context.user_data["due_items"] = due_ids
    context.user_data["due_items_source"] = source


def _get_add_task(user_data: dict) -> dict | None:
    state = user_data.get("add_task")
    return state if isinstance(state, dict) else None


def _clear_add_task(user_data: dict) -> None:
    user_data.pop("add_task", None)


def _get_edit_task(user_data: dict) -> dict | None:
    state = user_data.get("edit_task")
    return state if isinstance(state, dict) else None


def _clear_edit_task(user_data: dict) -> None:
    user_data.pop("edit_task", None)


def _category_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(CATEGORY_LABELS[c], callback_data=f"{prefix}:cat:{c}")
        for c in VALID_CATEGORIES
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def _add_deadline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("No deadline", callback_data=f"{ADDTASK_CB}:nodeadline")]]
    )


def _edit_task_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Task", callback_data=f"{EDITTASK_CB}:field:task"),
                InlineKeyboardButton("Deadline", callback_data=f"{EDITTASK_CB}:field:deadline"),
            ],
            [
                InlineKeyboardButton("Category", callback_data=f"{EDITTASK_CB}:field:category"),
                InlineKeyboardButton("Done editing", callback_data=f"{EDITTASK_CB}:done"),
            ],
        ]
    )


def _parse_add_one_line(text: str) -> dict[str, str | None]:
    parts = [p.strip() for p in text.split("|", 2)]
    while len(parts) < 3:
        parts.append("")
    task = parts[0]
    deadline = parts[1] or None
    category = parts[2] or "other"
    return {"task": task, "deadline_text": deadline, "category": category}


def _extract_task_from_add_phrase(question: str) -> str | None:
    q = question.strip()
    lower = q.lower()
    for prefix in (
        "remind me to ",
        "add task ",
        "add a task ",
        "new task ",
        "track a task ",
        "add deadline ",
        "add a deadline ",
        "add a reminder to ",
    ):
        if lower.startswith(prefix):
            return q[len(prefix) :].strip()
    return None


async def _save_manual_task_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    task: str,
    deadline_text: str | None,
    category: str,
) -> bool:
    if not update.message:
        return False
    row = await asyncio.to_thread(
        create_manual_assignment,
        task,
        deadline_text=deadline_text,
        category=category,
    )
    if not row:
        await update.message.reply_text("Couldn't save that task. Try again with /add.")
        return False
    _clear_add_task(context.user_data)
    due_display = format_deadline(row.get("deadline_date"), row.get("deadline_text"))
    cat_label = CATEGORY_LABELS.get(normalize_category(row.get("category")), "Other")
    await update.message.reply_text(
        f"Added: <b>{html.escape(str(task))}</b>\n"
        f"Category: {html.escape(cat_label)}\n"
        f"Due: {html.escape(due_display)}",
        parse_mode="HTML",
    )
    return True


async def _show_edit_task_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    assignment_id: str,
    notice: str = "",
) -> None:
    item = await asyncio.to_thread(get_assignment_by_id, assignment_id)
    if not item or item.get("source") != "manual":
        target = update.callback_query.message if update.callback_query else update.message
        if target:
            await target.reply_text("Only tasks you added yourself can be edited.")
        _clear_edit_task(context.user_data)
        return

    context.user_data["edit_task"] = {
        "assignment_id": assignment_id,
        "step": None,
    }
    due_display = format_deadline(item.get("deadline_date"), item.get("deadline_text"))
    cat_label = CATEGORY_LABELS.get(normalize_category(item.get("category")), "Other")
    task = item.get("task") or item.get("summary") or "Untitled"
    lines = []
    if notice:
        lines.append(notice)
    lines.extend(
        [
            f"<b>Editing:</b> {html.escape(str(task))}",
            f"Category: {html.escape(cat_label)}",
            f"Due: {html.escape(due_display)}",
            "",
            "Tap a field to change it, or Done editing when finished.",
        ]
    )
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=_edit_task_keyboard()
        )
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=_edit_task_keyboard())


async def _begin_add_task_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    task: str | None = None,
) -> None:
    if not update.message:
        return
    if task:
        context.user_data["add_task"] = {
            "step": ADD_TASK_NEED_DEADLINE,
            "task": task,
            "deadline_text": None,
            "category": None,
        }
        await update.message.reply_text(
            f"Got it: <b>{html.escape(task)}</b>\nWhen is it due? (e.g. tomorrow 5pm)\n"
            "Or tap No deadline.",
            parse_mode="HTML",
            reply_markup=_add_deadline_keyboard(),
        )
        return
    context.user_data["add_task"] = {
        "step": ADD_TASK_NEED_TASK,
        "task": None,
        "deadline_text": None,
        "category": None,
    }
    await update.message.reply_text(
        "What task do you want to track?\n\n"
        "Tip: one-line shortcut — /add Submit report | tomorrow 5pm | deadline"
    )


async def _continue_add_task_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    state = _get_add_task(context.user_data)
    if not state or not update.message:
        return
    step = state.get("step")
    if step == ADD_TASK_NEED_TASK:
        task = text.strip()
        if not task:
            await update.message.reply_text("Please describe the task.")
            return
        state["task"] = task
        state["step"] = ADD_TASK_NEED_DEADLINE
        context.user_data["add_task"] = state
        await update.message.reply_text(
            f"Got it: <b>{html.escape(task)}</b>\nWhen is it due? (e.g. tomorrow 5pm)\n"
            "Or tap No deadline.",
            parse_mode="HTML",
            reply_markup=_add_deadline_keyboard(),
        )
        return
    if step == ADD_TASK_NEED_DEADLINE:
        state["deadline_text"] = text.strip()
        state["step"] = "need_category"
        context.user_data["add_task"] = state
        await update.message.reply_text("Pick a category:", reply_markup=_category_keyboard(ADDTASK_CB))


async def _continue_edit_task_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    state = _get_edit_task(context.user_data)
    if not state or not update.message:
        return
    assignment_id = state.get("assignment_id")
    step = state.get("step")
    if not assignment_id or not step:
        await update.message.reply_text("Tap a field on the edit panel, or /cancel.")
        return

    fields: dict[str, str | None] = {}
    if step == EDIT_TASK_EDITING_TASK:
        task = text.strip()
        if not task:
            await update.message.reply_text("Task text can't be empty.")
            return
        fields["task"] = task
    elif step == EDIT_TASK_EDITING_DEADLINE:
        lower = text.strip().lower()
        if lower in {"none", "no deadline", "skip", "-"}:
            fields["deadline_text"] = None
        else:
            fields["deadline_text"] = text.strip()
    else:
        await update.message.reply_text("Pick a category using the buttons below.")
        return

    updated = await asyncio.to_thread(update_assignment, str(assignment_id), fields)
    if not updated:
        await update.message.reply_text("Couldn't update that task.")
        return
    state["step"] = None
    context.user_data["edit_task"] = state
    await _show_edit_task_panel(update, context, assignment_id=str(assignment_id), notice="Updated.")


def _strip_bot_mention(text: str, bot_username: str | None) -> str:
    if not text:
        return ""
    cleaned = text
    if bot_username:
        cleaned = re.sub(rf"@{re.escape(bot_username)}\b", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _section_buttons(sections: list[str]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(f"Section {s}", callback_data=f"section:{s}") for s in sections]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def _available_sections() -> list[str]:
    info = await asyncio.to_thread(refresh_schedule_from_gmail)
    template = load_template_sessions(info if info.get("sessions") else None)
    sessions = template or sessions_for_day(today_ist(), section=None, template=template)
    found: set[str] = set()
    for s in sessions:
        sec = _session_section(
            s.get("course_or_topic") or "",
            s.get("notes"),
            s.get("room"),
            s.get("faculty"),
        )
        if sec:
            found.add(sec)
    return sorted(found)


_BARE_EMAIL_RE = re.compile(
    r"^\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\s*$"
)
_LLM_STATUS_RE = re.compile(
    r"\b(?:claude|groq|nemotron|provider|ai provider)\b",
    re.IGNORECASE,
)
_LLM_STATUS_ASK_RE = re.compile(
    r"\b(?:why|reach|use|try|not working|down|fail|instead)\b",
    re.IGNORECASE,
)


def _bare_email(text: str) -> str | None:
    match = _BARE_EMAIL_RE.match(text or "")
    return match.group(1).lower() if match else None


def _is_llm_status_question(text: str) -> bool:
    q = text or ""
    return bool(_LLM_STATUS_RE.search(q) and _LLM_STATUS_ASK_RE.search(q))


def _llm_status_reply() -> str:
    from ai.client import get_llm_status

    status = get_llm_status()
    configured = status.get("providers_configured") or []
    cooled = status.get("cooldowns_seconds") or {}
    last_ok = status.get("last_ok_provider") or "none recently"
    order = ", ".join(configured) if configured else "none configured"
    parts = [f"I try providers in this order: {order}."]
    parts.append(f"Last one that worked: {last_ok}.")
    if cooled:
        bits = [f"{name} ({seconds}s left)" for name, seconds in cooled.items()]
        parts.append("On cooldown: " + ", ".join(bits) + ".")
    else:
        parts.append("None are on cooldown — if a reply just failed I will try Claude next.")
    return " ".join(parts)


def _resolve_target_day(question: str, *, fallback_iso: str | None = None):
    return resolve_schedule_day(question, fallback_iso=fallback_iso)


def _wants_schedule_refresh(question: str) -> bool:
    q = question.lower()
    return any(k in q for k in REFRESH_KEYWORDS)


def _is_schedule_question(question: str, *, last_intent: str | None = None) -> bool:
    q = question.lower()
    if any(k in q for k in SCHEDULE_KEYWORDS):
        return True
    # Follow-up like "why not check again" after a schedule answer
    if last_intent == "schedule" and _wants_schedule_refresh(question):
        return True
    return False


def _sessions_for_day(day_iso: str, section: str | None, template: list[dict] | None = None) -> list[dict]:
    from datetime import date as date_cls

    try:
        target = date_cls.fromisoformat(day_iso)
    except ValueError:
        return []
    return sessions_for_day(target, section=section, template=template)


async def _reply_schedule_for_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    question: str,
) -> None:
    """Answer class/schedule questions from class_sessions; refresh Gmail when needed."""
    await update.message.chat.send_action("typing")
    target_day = _resolve_target_day(
        question, fallback_iso=context.user_data.get("last_schedule_day")
    )
    section = await asyncio.to_thread(get_preferred_section)
    force_refresh = _wants_schedule_refresh(question)

    sessions = _sessions_for_day(target_day.isoformat(), section)
    refreshed = False
    template = load_template_sessions()
    if not sessions or force_refresh:
        try:
            info = await asyncio.to_thread(refresh_schedule_from_gmail)
            refreshed = True
            template = load_template_sessions(info if info.get("sessions") else None)
            sessions = _sessions_for_day(target_day.isoformat(), section, template=template)
        except Exception as exc:
            logger.exception("Schedule refresh failed during chat Q&A: %s", exc)

    context.user_data["last_intent"] = "schedule"
    context.user_data["last_schedule_day"] = target_day.isoformat()

    if not sessions:
        next_day, next_sessions = next_sessions_after(
            target_day, section=section, template=template
        )
        if next_day and next_sessions:
            label = target_day.strftime("%a, %d %b")
            if is_schedule_challenge(question):
                preface = (
                    f"Sorry — I reused an old day. Today is {label} and there are no classes. "
                    "Next sessions:"
                )
            else:
                preface = f"No classes on {label}. Next sessions:"
            next_label = next_day.strftime("%a, %d %b %Y")
            title = f"<b>📚 Next classes — {next_label}</b>"
            if section:
                title = f"<b>📚 Next classes — {next_label} (Sec. {section})</b>"
            text = f"{preface}\n\n{_format_session_block(next_sessions, title)}"
            await reply_long(update.message, text, parse_mode="HTML")
            return
        raw = project_sessions_to_day(template, target_day) if template else []
        label = target_day.strftime("%a, %d %b")
        if raw and section:
            await update.message.reply_text(
                f"No Section {section} classes found for {label}.\n"
                f"There are {len(raw)} session(s) that day without a matching Sec. {section} label.\n"
                "Try /section to change section, or /classes to refresh the timetable."
            )
            return
        tip = " I re-checked Gmail for the latest timetable." if refreshed else ""
        await update.message.reply_text(
            f"No classes found for {label}.{tip}\n"
            "Run /classes to pull the latest timetable PDF, or /section if your section isn't set."
        )
        return

    label = target_day.strftime("%a, %d %b %Y")
    title = f"<b>📚 Classes — {label}</b>"
    if section:
        title = f"<b>📚 Classes — {label} (Sec. {section})</b>"
    text = _format_session_block(sessions, title)
    if is_schedule_challenge(question):
        text = f"That was the last day I had stored. Today is {label}.\n\n{text}"
    await reply_long(update.message, text, parse_mode="HTML")


async def action_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if not _require_user(update):
        await query.answer("Only the bot owner can use these actions.", show_alert=True)
        return
    parsed = parse_action_callback(query.data)
    if not parsed:
        await query.answer()
        return

    action, assignment_id, extra = parsed
    settings = get_settings()

    if action == ACT_DONE:
        updated = await asyncio.to_thread(mark_assignment_done, assignment_id)
        if updated:
            task = updated.get("task") or updated.get("summary") or "item"
            await query.answer("Marked done.")
            try:
                await query.edit_message_text(
                    f"✅ Done: {html.escape(str(task))}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            try:
                await context.bot.unpin_chat_message(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                )
            except Exception:
                pass
        else:
            await query.answer("Already closed or not found.")
        return

    if action == ACT_DISMISS:
        updated = await asyncio.to_thread(dismiss_assignment, assignment_id)
        if updated:
            await query.answer("Marked not important.")
            try:
                await query.edit_message_text("🚫 Marked not important.")
            except Exception:
                pass
            try:
                await context.bot.unpin_chat_message(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                )
            except Exception:
                pass
        else:
            await query.answer("Already closed or not found.")
        return

    if action == ACT_REMIND:
        hours = max(1, int(settings.remind_again_hours or 4))
        remind_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        updated = await asyncio.to_thread(schedule_assignment_reminder, assignment_id, remind_at)
        if updated:
            confirm = f"⏰ Reminding in {hours}h"
            try:
                prior = query.message.text or query.message.caption or ""
                await query.edit_message_text(
                    f"{prior}\n\n<i>{confirm}</i>",
                    parse_mode="HTML",
                    reply_markup=query.message.reply_markup,
                )
            except Exception:
                pass
            await query.answer(confirm)
        else:
            await query.answer("Could not schedule reminder.")
        return

    if action == ACT_SNOOZE:
        if (extra or "") == SNOOZE_TOMORROW:
            target = (now_ist() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            remind_at = target.astimezone(timezone.utc)
            confirm = "💤 Snoozed until tomorrow 9:00 AM IST"
        else:
            try:
                hours = max(1, int(extra or "1"))
            except ValueError:
                hours = 1
            remind_at = datetime.now(timezone.utc) + timedelta(hours=hours)
            confirm = f"💤 Snoozed for {hours}h"
        updated = await asyncio.to_thread(schedule_assignment_reminder, assignment_id, remind_at)
        if updated:
            try:
                prior = query.message.text or query.message.caption or ""
                await query.edit_message_text(
                    f"{prior}\n\n<i>{confirm}</i>",
                    parse_mode="HTML",
                    reply_markup=query.message.reply_markup,
                )
            except Exception:
                pass
            await query.answer(confirm)
        else:
            await query.answer("Could not snooze.")
        return

    if action == ACT_REPLY:
        item = await asyncio.to_thread(get_assignment_by_id, assignment_id)
        if not item:
            await query.answer("Item not found — it may already be closed.", show_alert=True)
            return
        sender_name, sender_email = parse_sender(item.get("sender"))
        if not sender_email:
            await query.answer("No sender address stored for this item.", show_alert=True)
            return
        subject = str(item.get("subject") or "").strip()
        context.user_data["compose"] = {
            "step": COMPOSE_NEED_CONTENT,
            "instructions": None,
            "recipient": sender_email,
            "recipient_name": sender_name,
            "reply_subject": subject,
            "reply_thread_id": item.get("gmail_thread_id"),
            "draft": None,
        }
        await query.answer()
        lines = [f"Replying to <b>{html.escape(sender_name or sender_email)}</b>"]
        if subject:
            lines.append(f"Re: <i>{html.escape(subject)}</i>")
        lines.append("")
        lines.append("What do you want to say? (type /cancel to stop)")
        await query.message.reply_text("\n".join(lines), parse_mode="HTML")
        return


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🧹 Clear chat", callback_data=CLEAR_CHAT_CB)]]
    )


def _quick_action_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in QUICK_ACTION_LABELS]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _track_bot_message(context: ContextTypes.DEFAULT_TYPE, message_id: int | None) -> None:
    if message_id is None:
        return
    ids: list[int] = context.user_data.setdefault("bot_message_ids", [])
    if message_id not in ids:
        ids.append(message_id)
    # Keep list bounded
    if len(ids) > 200:
        del ids[:-200]


async def _clear_bot_messages(
    bot,
    chat_id: int | str,
    *,
    anchor_message_id: int,
    extra_ids: list[int] | None = None,
) -> int:
    """Delete tracked bot messages plus a small scan window (best-effort)."""
    seen: set[int] = set()
    to_try: list[int] = list(extra_ids or [])
    # Small backward scan from the button message as a fallback for untracked sends.
    for mid in range(anchor_message_id, max(anchor_message_id - _CLEAR_CHAT_SCAN_LIMIT, 0), -1):
        to_try.append(mid)

    deleted = 0
    for mid in to_try:
        if mid in seen or mid <= 0:
            continue
        seen.add(mid)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except Exception:
            pass
    return deleted


async def clear_chat_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update):
        return
    query = update.callback_query
    if not query or not query.message:
        return

    chat = query.message.chat
    if chat.type != ChatType.PRIVATE:
        await query.answer("Clear chat works in your DM with the bot.", show_alert=True)
        return

    await query.answer("Clearing…")
    tracked: list[int] = context.user_data.pop("bot_message_ids", [])
    deleted = await _clear_bot_messages(
        context.bot,
        chat.id,
        anchor_message_id=query.message.message_id,
        extra_ids=tracked,
    )
    try:
        await query.message.delete()
        deleted += 1
    except Exception:
        pass

    note = await context.bot.send_message(
        chat_id=chat.id,
        text=f"🧹 Cleared {deleted} bot message(s). Send /start anytime.",
    )
    _track_bot_message(context, note.message_id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update):
        return
    await start_command(update, context)


def _web_login_url() -> str:
    base = (get_settings().render_url or "https://mailbot-rl4s.onrender.com").rstrip("/")
    return f"{base}/login"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.users import claim_invite, is_owner_user, link_telegram_to_user, request_login_code

    tg = update.effective_user
    chat = update.effective_chat
    if not tg or not chat or not update.message:
        return

    args = context.args or []
    if args and str(args[0]).startswith("invite_"):
        if not (tg.username or "").strip():
            await update.message.reply_text(
                "Set a Telegram @username in your profile first, then open the invite link again."
            )
            return
        code = str(args[0])[len("invite_") :]
        claimed = claim_invite(
            code,
            telegram_user_id=str(tg.id),
            telegram_chat_id=str(chat.id),
            telegram_username=tg.username,
        )
        if isinstance(claimed, dict) and claimed.get("_claim_error") == "username_mismatch":
            expected = claimed.get("expected") or "the invited username"
            await update.message.reply_text(
                f"This invite is for @{expected}. You're @{tg.username}. "
                "Ask the owner to send a link for your username."
            )
            return
        if claimed:
            username = str(claimed.get("telegram_username") or tg.username or "").lstrip("@")
            login = request_login_code(username) if username else {}
            otp = login.pop("_code_for_bot", None) if isinstance(login, dict) else None
            lines = [
                "You're registered.",
                "",
                f"1. Open {_web_login_url()}",
                f"2. Username: @{username}" if username else "2. Send /login for a web code",
            ]
            if otp:
                lines.append(f"3. Code (10 minutes): {otp}")
            else:
                lines.append("3. Send /login here if you need a new code")
            lines.append("4. After you sign in, tap Connect Gmail. Mail alerts start once that finishes.")
            await update.message.reply_text("\n".join(lines))
        else:
            await update.message.reply_text("That invite link is invalid or expired.")
        return

    user = _resolve_user(update)
    if not user:
        await update.message.reply_text(
            "This bot is invite-only. Ask the owner for an invite, then open the link they send you."
        )
        return

    link_telegram_to_user(
        user["id"],
        telegram_user_id=str(tg.id),
        telegram_chat_id=str(chat.id),
        telegram_username=tg.username,
    )

    if is_owner_user(user):
        msg = await update.message.reply_text(
            "Mailbot ready.\n\n"
            "Morning digest (7:00 IST) and evening digest (20:00 IST) land here in your DM.\n"
            "Free text works here too — ask about deadlines, mail, or senders.\n\n"
            "Owner: /invite @username — invite a student\n"
            "/users — list registered users\n"
            "/login — web app login code\n\n"
            "Commands:\n"
            "/due — upcoming deadlines & tasks (nearest first)\n"
            "/missed — open items whose deadline has passed\n"
            "/undated — open tasks with no due date\n"
            "/add — add a task yourself (wizard or one-line shortcut)\n"
            "/edit <n> — edit a task you added (from the last list)\n"
            "/done <n> — mark item #n from the last list (/due, /missed, or /undated)\n"
            "/classes — today's classes for your section (tap to set it first time)\n"
            "/section — change or check your section\n"
            "/library — library e-resources & latest briefing\n"
            "/story <n> — full brief of any story (or just reply with its number)\n"
            "/ask <question> — ask about deadlines, mails, senders, or email content\n"
            "Add #verify to any question for a fresh Gmail answer with cited sources\n"
            "/send — compose and send an email\n"
            "/cancel — cancel an in-progress draft or task wizard\n"
            "/reset (or /new) — clear conversation context; discards any draft\n"
            "/status — last check times and system health\n\n"
            "Actionable alerts have Done / Snooze / Remind again buttons.\n"
            "Marks in attachments show as text; readings are forwarded as files.\n\n"
            "In groups: reply to my messages, or mention me:\n"
            "@IMTAssistant_bot what's due this week?\n\n"
            "Tip: In BotFather → /setprivacy → Disable, so I can see all group messages.",
            reply_markup=_quick_action_keyboard(),
        )
    else:
        msg = await update.message.reply_text(
            "Welcome to Mailbot.\n\n"
            f"1. Open {_web_login_url()} and sign in with your Telegram username.\n"
            "2. Send /login here if you need a fresh code.\n"
            "3. After you sign in, tap Connect Gmail. Deadline alerts come here once that finishes.\n\n"
            "/due /classes /ask /send — same commands as the owner.\n"
            "/status — sync health",
            reply_markup=_quick_action_keyboard(),
        )
    _track_bot_message(context, msg.message_id)
    clear_row = await update.message.reply_text(
        "Quick actions are pinned above your keyboard.",
        reply_markup=_start_keyboard(),
    )
    _track_bot_message(context, clear_row.message_id)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    state, digest, pending, pending_emails, dead_letters = await asyncio.gather(
        asyncio.to_thread(get_watch_state),
        asyncio.to_thread(get_digest_state),
        asyncio.to_thread(count_pending_scheduled_reminders),
        asyncio.to_thread(count_pending_emails),
        asyncio.to_thread(count_dead_letter_emails),
    )
    state = state or {}
    digest = digest or {}

    def _fmt_ts(raw) -> str:
        if not raw:
            return "never"
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt.astimezone(IST).strftime("%a %d %b %I:%M %p IST")
        except ValueError:
            return str(raw)[:40]

    def _checked_age_minutes(raw) -> float | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60.0
        except ValueError:
            return None

    gmail_status = "✅ connected"
    if state.get("gmail_disconnected_at"):
        gmail_status = f"❌ disconnected since {_fmt_ts(state.get('gmail_disconnected_at'))}"

    lines = [
        "<b>📊 Mailbot status</b>",
        "",
        f"<b>Gmail:</b> {gmail_status}",
        f"<b>Last mail check:</b> {_fmt_ts(state.get('last_checked_at'))}",
        f"<b>Last heartbeat:</b> {_fmt_ts(state.get('last_heartbeat_at'))}",
        f"<b>Last morning digest:</b> {digest.get('last_morning') or 'never'}",
        f"<b>Last evening digest:</b> {digest.get('last_evening') or 'never'}",
        f"<b>Pending scheduled reminders:</b> {pending}",
        f"<b>Pending email retries:</b> {pending_emails}",
        f"<b>Dead-letter emails:</b> {dead_letters}",
    ]

    checked_age = _checked_age_minutes(state.get("last_checked_at"))
    if checked_age is not None and checked_age > 15:
        lines.extend(
            [
                "",
                "⚠️ <b>Automatic polling looks stale</b> — last mail check was "
                f"{int(checked_age)} minutes ago. The 5-minute cron/pinger may not be reaching the app.",
            ]
        )
    elif state.get("last_checked_at") is None:
        lines.extend(
            [
                "",
                "⚠️ <b>No automatic mail check recorded yet.</b> Confirm GitHub Actions "
                "Mailbot Scheduler is enabled and RENDER_URL/TASK_SECRET are set.",
            ]
        )
    await reply_long(update.message, "\n".join(lines), parse_mode="HTML")


async def quick_action_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message or not update.message.text:
        return
    label = update.message.text.strip()
    if _get_compose(context.user_data) and label != "✉️ Send":
        await update.message.reply_text(
            "You have an email in progress. Keep going, or type cancel to drop the draft."
        )
        return
    if label == "📚 Classes":
        await classes_command(update, context)
    elif label == "⏰ Due":
        await due_command(update, context)
    elif label == "➕ Add":
        await add_command(update, context)
    elif label == "✉️ Send":
        if _get_compose(context.user_data):
            await update.message.reply_text(
                "You already have an email in progress. "
                "Keep going, or type cancel and tap Send again."
            )
            return
        await send_command(update, context)
    elif label == "📋 Section":
        await section_command(update, context)


async def due_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
    text, due_ids = await _fetch_assignment_list(category, source="due")
    _store_assignment_list(context, due_ids, source="due")
    await reply_long(update.message, text, parse_mode="HTML")


async def missed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
    text, due_ids = await _fetch_assignment_list(category, source="missed")
    _store_assignment_list(context, due_ids, source="missed")
    await reply_long(update.message, text, parse_mode="HTML")


async def undated_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
    text, due_ids = await _fetch_assignment_list(category, source="undated")
    _store_assignment_list(context, due_ids, source="undated")
    await reply_long(update.message, text, parse_mode="HTML")


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    args_text = " ".join(context.args).strip() if context.args else ""
    if args_text:
        parsed = _parse_add_one_line(args_text)
        task = parsed.get("task") or ""
        if not task:
            await update.message.reply_text(
                "Usage: /add Task name | deadline (optional) | category (optional)\n"
                "Example: /add Submit report | tomorrow 5pm | deadline"
            )
            return
        await _save_manual_task_message(
            update,
            context,
            task=task,
            deadline_text=parsed.get("deadline_text"),
            category=parsed.get("category") or "other",
        )
        return

    await _begin_add_task_flow(update, context)


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /edit <number> — use the number from your last /due, /missed, or /undated list"
        )
        return

    try:
        index = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /edit <number>")
        return

    due_ids: list[str] = context.user_data.get("due_items") or []
    if not due_ids:
        category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
        list_source = context.user_data.get("due_items_source") or "due"
        _, due_ids = await _fetch_assignment_list(category, source=list_source)
        _store_assignment_list(context, due_ids, source=list_source)

    if index < 1 or index > len(due_ids):
        await update.message.reply_text(
            f"No item #{index} in the last list. Run /due, /missed, or /undated first."
        )
        return

    assignment_id = due_ids[index - 1]
    item = await asyncio.to_thread(get_assignment_by_id, assignment_id)
    if not item:
        await update.message.reply_text("Couldn't find that item.")
        return
    if item.get("source") != "manual":
        await update.message.reply_text(
            "Only tasks you added yourself can be edited. Email-tracked items stay read-only."
        )
        return

    await _show_edit_task_panel(update, context, assignment_id=assignment_id)


async def addtask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.callback_query:
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    state = _get_add_task(context.user_data)
    if not state:
        await query.edit_message_text("Add-task flow expired. Run /add again.")
        return

    if data == f"{ADDTASK_CB}:nodeadline":
        state["deadline_text"] = None
        state["step"] = "need_category"
        context.user_data["add_task"] = state
        await query.edit_message_text("Pick a category:", reply_markup=_category_keyboard(ADDTASK_CB))
        return

    if data.startswith(f"{ADDTASK_CB}:cat:"):
        category = data.split(":", 2)[2]
        task = state.get("task") or ""
        deadline_text = state.get("deadline_text")
        row = await asyncio.to_thread(
            create_manual_assignment,
            task,
            deadline_text=deadline_text,
            category=category,
        )
        if not row:
            await query.edit_message_text("Couldn't save that task. Try /add again.")
            _clear_add_task(context.user_data)
            return
        _clear_add_task(context.user_data)
        due_display = format_deadline(row.get("deadline_date"), row.get("deadline_text"))
        cat_label = CATEGORY_LABELS.get(normalize_category(category), "Other")
        await query.edit_message_text(
            f"Added: <b>{html.escape(str(task))}</b>\n"
            f"Category: {html.escape(cat_label)}\n"
            f"Due: {html.escape(due_display)}",
            parse_mode="HTML",
        )


async def edittask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.callback_query:
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    state = _get_edit_task(context.user_data)
    if not state:
        await query.edit_message_text("Edit flow expired. Run /edit <n> again.")
        return

    assignment_id = str(state.get("assignment_id") or "")
    if not assignment_id:
        await query.edit_message_text("Edit flow expired. Run /edit <n> again.")
        return

    if data == f"{EDITTASK_CB}:done":
        _clear_edit_task(context.user_data)
        await query.edit_message_text("Done editing.")
        return

    if data == f"{EDITTASK_CB}:field:task":
        state["step"] = EDIT_TASK_EDITING_TASK
        context.user_data["edit_task"] = state
        await query.edit_message_text("Send the new task text:")
        return

    if data == f"{EDITTASK_CB}:field:deadline":
        state["step"] = EDIT_TASK_EDITING_DEADLINE
        context.user_data["edit_task"] = state
        await query.edit_message_text(
            "Send the new deadline (e.g. tomorrow 5pm), or type 'none' for no deadline:"
        )
        return

    if data == f"{EDITTASK_CB}:field:category":
        state["step"] = EDIT_TASK_EDITING_CATEGORY
        context.user_data["edit_task"] = state
        await query.edit_message_text("Pick a new category:", reply_markup=_category_keyboard(EDITTASK_CB))
        return

    if data.startswith(f"{EDITTASK_CB}:cat:"):
        category = data.split(":", 2)[2]
        updated = await asyncio.to_thread(update_assignment, assignment_id, {"category": category})
        if not updated:
            await query.edit_message_text("Couldn't update category.")
            return
        state["step"] = None
        context.user_data["edit_task"] = state
        await _show_edit_task_panel(update, context, assignment_id=assignment_id, notice="Category updated.")


async def classes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    await update.message.chat.send_action("typing")
    section = await asyncio.to_thread(get_preferred_section)
    if not section:
        available = await _available_sections()
        if not available:
            await update.message.reply_text(
                "Couldn't find any sections in the latest timetable yet — try again shortly."
            )
            return
        await update.message.reply_text(
            "Which section are you in?", reply_markup=_section_buttons(available)
        )
        return
    try:
        text = await asyncio.to_thread(classes_command_text, refresh=True, section=section)
    except Exception as exc:
        logger.exception("/classes failed: %s", exc)
        await update.message.reply_text(
            "Couldn't load the class schedule right now. Try again in a minute."
        )
        return
    await reply_long(update.message, text, parse_mode="HTML")


async def section_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return
    current = await asyncio.to_thread(get_preferred_section)
    label = f"Current: Section {current}\n\n" if current else ""
    available = await _available_sections() or ["A", "B", "C", "D"]
    await update.message.reply_text(f"{label}Pick your section:", reply_markup=_section_buttons(available))


async def section_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update):
        return
    query = update.callback_query
    await query.answer()
    section = query.data.split(":", 1)[1]
    await asyncio.to_thread(set_preferred_section, section)
    await query.edit_message_text(
        f"Got it — showing Section {section} from now on. Run /classes anytime."
    )


_STORY_NUM_RE = re.compile(r"^(?:story|brief|detail|details)?\s*#?\s*(\d{1,2})\s*$", re.IGNORECASE)


async def _send_story_brief_reply(message, n: int) -> bool:
    """Send the full brief for story #n from the latest library digest.

    Returns True when n matched a story (message handled), False when there is
    no recent digest or the number is out of range (caller should fall through
    to normal chat handling).
    """
    from imt_access import stories as imt_stories

    manifest = await asyncio.to_thread(imt_stories.load_manifest)
    size = imt_stories.manifest_size(manifest)
    if not manifest or n < 1 or n > size:
        return False

    await message.chat.send_action("typing")
    try:
        result = await asyncio.to_thread(imt_stories.get_story_brief, n)
    except Exception as exc:
        logger.exception("Story brief failed for #%s: %s", n, exc)
        result = None
    if not result:
        await message.reply_text("Couldn't load that story right now. Try again in a minute.")
        return True

    text = imt_stories.format_story_brief_message(result)
    if not result.get("brief"):
        text += "\n\n<i>Couldn't fetch the story text — the link above opens it directly.</i>"
    await reply_long(message, text, parse_mode="HTML", disable_web_page_preview=True)
    return True


async def story_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full brief of any story from the latest library briefing (/story 7)."""
    if not _require_user(update) or not update.message:
        return

    from imt_access import stories as imt_stories

    args = " ".join(context.args).strip().lstrip("#") if context.args else ""
    if args:
        try:
            n = int(args)
        except ValueError:
            await update.message.reply_text("Usage: /story <number> — e.g. /story 7")
            return
        if not await _send_story_brief_reply(update.message, n):
            await update.message.reply_text(
                "No story with that number. Run /story to see the latest story list."
            )
        return

    # No argument: resend the compact numbered list as a picker.
    manifest = await asyncio.to_thread(imt_stories.load_manifest)
    if not manifest:
        await update.message.reply_text(
            "No library briefing yet — it runs every morning at 7:20 AM IST."
        )
        return
    from imt_access.stories import KIND_LABELS as _STORY_KIND_LABELS

    lines = [f"<b>🗞 Stories — {escape_html(str(manifest['date']))}</b>", ""]
    current_kind: str | None = None
    for item in manifest["items"]:
        kind = str(item.get("kind") or "")
        if kind != current_kind:
            current_kind = kind
            lines.append(f"<b>{escape_html(_STORY_KIND_LABELS.get(kind, kind))}</b>")
        lines.append(f"{item.get('n')}. {escape_html(str(item.get('title') or ''))}")
    lines.append("")
    lines.append("<i>Reply with a number or /story &lt;n&gt; for the full brief.</i>")
    await reply_long(
        update.message, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


async def story_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline '⭐ #n' button under the digest -> send that story's brief."""
    if not _require_user(update):
        return
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        n = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer()
        return
    await query.answer("Fetching the story brief…")
    if query.message:
        await _send_story_brief_reply(query.message, n)


async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the latest library briefing: publications + catalog stats."""
    if not _require_user(update) or not update.message:
        return

    await update.message.chat.send_action("typing")
    from imt_access import store as imt_store
    from imt_access.settings import get_imt_settings

    settings = get_imt_settings()
    if not settings.configured:
        await update.message.reply_text(
            "Library integration isn't configured yet — set MAPMYACCESS_EMAIL and "
            "MAPMYACCESS_PASSWORD on the server."
        )
        return

    def _load() -> dict:
        state = imt_store.get_imt_state()
        pubs = imt_store.list_publications(limit=60)
        resources = imt_store.list_resources()
        return {"state": state, "pubs": pubs, "resources": resources}

    try:
        data = await asyncio.to_thread(_load)
    except Exception as exc:
        logger.exception("/library failed: %s", exc)
        await update.message.reply_text("Couldn't load the library right now. Try again in a minute.")
        return

    state, pubs, resources = data["state"], data["pubs"], data["resources"]
    lines: list[str] = ["<b>📖 Library — e-resources</b>", ""]

    if resources:
        cats: dict[str, int] = {}
        for r in resources:
            cats[r.get("category") or "Other"] = cats.get(r.get("category") or "Other", 0) + 1
        lines.append(f"<b>{len(resources)} resources</b> in the catalog:")
        lines.append(" · ".join(f"{escape_html(k)} ({v})" for k, v in sorted(cats.items())))
        lines.append("")

    if pubs:
        latest_date = max(str(p.get("publication_date") or "") for p in pubs)
        todays = [p for p in pubs if str(p.get("publication_date") or "") == latest_date]
        if todays:
            lines.append(f"<b>Latest briefing — {escape_html(latest_date)}</b>")
            by_kind: dict[str, list[dict]] = {}
            for p in todays:
                by_kind.setdefault(p.get("kind") or "other", []).append(p)
            kind_labels = {"epw": "EPW", "ft": "Financial Times", "et_prime": "ET Prime"}
            for kind, items in by_kind.items():
                lines.append(f"<u>{kind_labels.get(kind, kind)}</u>")
                for p in items[:6]:
                    title = escape_html(str(p.get("title") or ""))
                    url = escape_html(str(p.get("url") or ""))
                    lines.append(f'• <a href="{url}">{title}</a>')
                if len(items) > 6:
                    lines.append(f"  …and {len(items) - 6} more")
            lines.append("")
    else:
        lines.append("No publications fetched yet — the daily briefing runs at 7:15 AM.")
        lines.append("")

    last_sync = state.get("last_catalog_sync_at") or "never"
    lines.append(f"<i>Catalog synced: {escape_html(str(last_sync)[:16])}</i>")
    lines.append("Full catalog + analysis: web app → Library tab.")
    await reply_long(update.message, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
    list_source = context.user_data.get("due_items_source") or "due"

    if not context.args:
        text, due_ids = await _fetch_assignment_list(category, source=list_source)
        _store_assignment_list(context, due_ids, source=list_source)
        await reply_long(
            update.message,
            "Usage: /done <number>\n\n" + text,
            parse_mode="HTML",
        )
        return

    try:
        index = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /done <number> — use the number from /due, /missed, or /undated")
        return

    due_ids: list[str] = context.user_data.get("due_items") or []
    if not due_ids:
        text, due_ids = await _fetch_assignment_list(category, source=list_source)
        _store_assignment_list(context, due_ids, source=list_source)

    if index < 1 or index > len(due_ids):
        text, due_ids = await _fetch_assignment_list(category, source=list_source)
        _store_assignment_list(context, due_ids, source=list_source)
        await reply_long(
            update.message,
            f"No item #{index}. Pick from the list:\n\n" + text,
            parse_mode="HTML",
        )
        return

    assignment_id = due_ids[index - 1]
    updated = await asyncio.to_thread(mark_assignment_done, assignment_id)
    if not updated:
        await update.message.reply_text("Could not mark that item done — it may already be closed.")
        return

    task = updated.get("task") or updated.get("summary") or "Untitled"
    course = updated.get("course_or_topic") or "—"
    await update.message.reply_text(
        f"Marked done: <b>{html.escape(str(task))}</b>\nCourse: {html.escape(str(course))}",
        parse_mode="HTML",
    )


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    question = " ".join(context.args).strip() if context.args else ""
    if not question:
        await update.message.reply_text("Usage: /ask what's due this week?")
        return

    convo = get_convo(context.user_data)
    classified = await asyncio.to_thread(classify_message, question, convo)
    if classified.get("intent") == INTENT_COMPOSE:
        await _begin_compose_flow(
            update,
            context,
            recipient=classified.get("recipient"),
            content=classified.get("content"),
        )
        return
    await _answer_chat_question(
        update,
        context,
        question,
        person_hint=classified.get("person"),
        intent=classified.get("intent") or INTENT_MAILBOX_QA,
    )


def _get_compose(user_data: dict) -> dict | None:
    compose = user_data.get("compose")
    return compose if isinstance(compose, dict) else None


def _clear_compose(user_data: dict) -> None:
    user_data.pop("compose", None)


_COMPOSE_CANCEL_WORDS = frozenset({"cancel", "stop", "discard", "abort", "/cancel"})
_COMPOSE_APPROVE_WORDS = frozenset(
    {
        "yes",
        "y",
        "ok",
        "okay",
        "send",
        "send it",
        "send now",
        "send email",
        "approve",
        "approve & send",
        "approve and send",
        "go ahead",
        "do it",
        "yeah",
        "yep",
        "yes send",
    }
)


def _looks_like_compose_breakout(text: str) -> bool:
    """True when free text during compose is clearly not part of drafting."""
    q = text.lower().strip()
    if _looks_like_recipient_only_message(text):
        return False
    if re.search(r"\b(?:received\s+)?(?:mail|email)s?\s+from\b", q):
        return False
    if re.search(r"\b(?:send|sending|sent)\s+(?:her|him|them)\s+(?:a\s+)?(?:mail|email)\b", q):
        return False
    if any(m in q for m in SCHEDULE_KEYWORDS):
        return True
    if _is_due_list_question(text):
        return True
    if _is_missed_list_question(text):
        return True
    if any(
        m in q
        for m in (
            "what's her email",
            "what is her email",
            "what's his email",
            "what is his email",
            "what did ",
            "when is",
            "when's",
            "what's the deadline",
            "what is the deadline",
            "#verify",
        )
    ):
        return True
    if re.search(r"\b(?:all\s+)?(?:mails?|emails?)\s+from\b", q):
        return True
    if re.search(r"\b(?:show|list|how many|count)\b.+\b(?:mail|email)", q):
        return True
    # The classifier fallback below is too aggressive for short answers that
    # are really recipient names/content (e.g. "saleena" -> mailbox_qa by
    # default). Only run it when the text reads like an actual question or
    # command, not a short reply to "who should I send it to?".
    looks_like_question = "?" in q or q.startswith(
        ("what", "when", "who", "where", "why", "how", "is ", "are ", "do ", "did ", "can ", "could ", "show ", "list ")
    )
    if not looks_like_question and len(q.split()) <= 4:
        return False
    classified = classify_message(text, None)
    return classified.get("intent") in {
        INTENT_CONTACT_LOOKUP,
        INTENT_MAILBOX_QA,
        INTENT_SCHEDULE,
        INTENT_DUE_LIST,
        INTENT_MISSED_LIST,
    }


def _is_compose_recipient_reply(
    text: str,
    convo: dict | None,
    compose: dict | None,
) -> bool:
    """During compose, treat person/email clarifications as recipient input."""
    if not compose:
        return False
    step = compose.get("step")
    # Only intercept as recipient reply when we're actively asking for a
    # recipient. When we're asking for content, the text is content — even
    # if it happens to contain "from" or a person name.
    if step != COMPOSE_NEED_RECIPIENT:
        return False
    if "@" in text:
        return True
    q = text.lower()
    if re.search(r"\b(?:received\s+)?(?:mail|email)s?\s+from\b", q):
        return True
    if re.search(r"\b(?:from|to)\s+[a-z]", q) and not re.search(
        r"\b(?:send|write|draft|compose|reply)\b.+\bto\b", q, re.I
    ):
        return True
    mention = extract_person_reference(text, convo)
    if mention:
        return True
    if convo and convo.get("last_person"):
        person = convo["last_person"]
        if any(tok in q for tok in name_tokens(person)):
            return True
        req = normalize_name_key(text)
        last = normalize_name_key(person)
        if req and last and (req in last or last in req):
            return True
    return False


def _looks_like_recipient_only_message(text: str, convo: dict | None = None) -> bool:
    """True when the message clarifies who to email, not what to say."""
    q = text.lower().strip()
    if "@" in text:
        return False
    content_markers = ("saying", "that ", "please ", "request", "ask ", "tell ", "inform ")
    if any(m in q for m in content_markers) and len(q.split()) > 6:
        return False
    if "?" in text or re.search(r"\b(?:what|when|who|how|why|which|where)'?s?\b", q):
        return False
    if extract_person_reference(text, convo):
        return True
    return bool(
        re.search(r"\b(?:received\s+)?(?:mail|email)s?\s+from\b", q)
        or re.search(r"\b(?:we|i)\s+(?:have|got|received)\b", q)
        or re.search(r"\b(?:her|his|their)\s+(?:mail|email)\b", q)
    )


def _compose_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve & Send", callback_data=APPROVE_CB),
                InlineKeyboardButton("Cancel", callback_data=CANCEL_CB),
            ]
        ]
    )


async def _resolve_recipient_hint(
    recipient: str | None,
    convo: dict | None = None,
) -> tuple[str | None, str | None]:
    """Return (email, disambiguation_message)."""
    if not recipient:
        return None, None
    text = recipient.strip()
    mention = extract_person_reference(text, convo) or text
    if "@" in mention:
        return mention.lower(), None

    email, _matches, note = await asyncio.to_thread(resolve_email_robust, mention, convo=convo)
    if email:
        return email, None
    if note:
        return None, note

    return None, (
        f"I don't have an email for {text} yet. "
        "Paste their email address, or wait until you've received mail from them."
    )


async def _draft_and_show_compose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    compose = _get_compose(context.user_data)
    if not compose:
        return
    recipient = compose.get("recipient")
    instructions = compose.get("instructions")
    if not recipient or not instructions:
        return

    await update.message.reply_text("Drafting your email...")
    reply_subject = str(compose.get("reply_subject") or "").strip()
    display = compose.get("recipient_name") or recipient
    prompt_instructions = f"To: {recipient}\nRecipient name: {display}\n\n{instructions}"
    if reply_subject:
        reply_target = display
        prompt_instructions = (
            f"To: {recipient}\nRecipient name: {reply_target}\n"
            f"This is a reply to an email from {reply_target} "
            f"with subject \"{reply_subject}\".\n\nReply instructions:\n{instructions}"
        )
    draft = await asyncio.to_thread(compose_email, prompt_instructions)
    if not draft or "subject" not in draft or "body" not in draft:
        await asyncio.to_thread(
            log_low_confidence,
            "composer",
            {"instructions": instructions, "draft": draft},
        )
        await update.message.reply_text(
            "Couldn't draft the email right now — the AI service is briefly "
            "unavailable. Please try /send again in a minute or two."
        )
        _clear_compose(context.user_data)
        return

    if reply_subject:
        base_subject = re.sub(r"^(?:\s*re\s*:\s*)+", "", reply_subject, flags=re.IGNORECASE).strip()
        draft["subject"] = f"Re: {base_subject or reply_subject}"

    compose["draft"] = draft
    compose["step"] = COMPOSE_CONFIRM
    context.user_data["compose"] = compose

    msg = (
        f"To: {recipient}\n"
        f"Subject: {draft['subject']}\n\n"
        f"{draft['body']}"
    )
    sent = await update.message.reply_text(msg, reply_markup=_compose_keyboard())
    _track_bot_message(context, sent.message_id)


async def _begin_compose_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    recipient: str | None = None,
    content: str | None = None,
) -> None:
    convo = get_convo(context.user_data)
    compose: dict = {"step": COMPOSE_NEED_CONTENT, "instructions": None, "recipient": None, "draft": None}
    raw_text = (getattr(update.message, "text", "") or "").strip()
    if not recipient and not content and re.search(
        r"\b(?:to|mail|email)\s+(?:a|an|the|my|any|some)?\s*"
        r"(?:friend|friendly|buddy|faculty|professor|teacher|someone|somebody|person)\b",
        raw_text,
        flags=re.IGNORECASE,
    ):
        compose["step"] = COMPOSE_NEED_RECIPIENT
        context.user_data["compose"] = compose
        await update.message.reply_text("Who should I send it to? (name or email address)")
        remember_turn(context.user_data, user_text=raw_text, intent="compose")
        return
    if content:
        compose["instructions"] = content.strip()
    if recipient:
        email, note = await _resolve_recipient_hint(recipient, convo)
        if note:
            compose["recipient_name"] = recipient
            if compose["instructions"]:
                compose["pending_recipient_name"] = recipient
            await update.message.reply_text(note)
            if not compose["instructions"]:
                compose["step"] = COMPOSE_NEED_RECIPIENT
                context.user_data["compose"] = compose
                await update.message.reply_text(
                    "Paste their email address, or say their name again once I've seen mail from them."
                )
                remember_turn(
                    context.user_data,
                    user_text=update.message.text,
                    bot_text=note,
                    intent="compose",
                    person=recipient,
                )
                return
        elif email:
            compose["recipient"] = email
            compose["recipient_name"] = recipient

    if compose.get("instructions") and compose.get("recipient"):
        context.user_data["compose"] = compose
        await _draft_and_show_compose(update, context)
        remember_turn(
            context.user_data,
            user_text=update.message.text,
            intent="compose",
            person=compose.get("recipient_name") or recipient,
            person_email=compose.get("recipient"),
            query=compose.get("instructions"),
        )
        return

    if not compose.get("recipient"):
        compose["step"] = COMPOSE_NEED_RECIPIENT
        context.user_data["compose"] = compose
        await update.message.reply_text("Who should I send it to? (name or email address)")
        remember_turn(
            context.user_data,
            user_text=update.message.text,
            intent="compose",
            person=recipient,
            query=compose.get("instructions"),
        )
        return

    compose["step"] = COMPOSE_NEED_CONTENT
    context.user_data["compose"] = compose
    display = compose.get("recipient_name") or compose.get("recipient") or recipient
    email = compose.get("recipient")
    if email and display and str(display) != str(email):
        await update.message.reply_text(
            f"Got it — I'll send to {display} ({email}). What do you want to say in the email?"
        )
    else:
        await update.message.reply_text("What do you want to say in the email?")
    remember_turn(context.user_data, user_text=update.message.text, intent="compose", person=recipient, person_email=email)


async def _continue_compose_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    compose = _get_compose(context.user_data)
    if not compose:
        return

    convo = get_convo(context.user_data)
    step = compose.get("step")

    if step == COMPOSE_NEED_RECIPIENT or (
        not compose.get("recipient") and _is_compose_recipient_reply(text, convo, compose)
    ):
        email, note = await _resolve_recipient_hint(text.strip(), convo)
        if note and not email:
            await update.message.reply_text(note)
            compose["step"] = COMPOSE_NEED_RECIPIENT
            context.user_data["compose"] = compose
            remember_turn(context.user_data, user_text=text, intent="compose")
            return
        if email:
            mention = extract_person_reference(text, convo) or text.strip()
            compose["recipient"] = email
            compose["recipient_name"] = mention
            display = mention or email
            if step == COMPOSE_NEED_RECIPIENT and not compose.get("instructions"):
                compose["step"] = COMPOSE_NEED_CONTENT
                context.user_data["compose"] = compose
                await update.message.reply_text(
                    f"Got it — I'll send to {display} ({email}). What do you want to say in the email?"
                )
                remember_turn(
                    context.user_data,
                    user_text=text,
                    intent="compose",
                    person=mention,
                    person_email=email,
                )
                return
            if _looks_like_recipient_only_message(text, convo) and not compose.get("instructions"):
                compose["step"] = COMPOSE_NEED_CONTENT
                context.user_data["compose"] = compose
                await update.message.reply_text(
                    f"Got it — I'll send to {display} ({email}). What do you want to say in the email?"
                )
                remember_turn(
                    context.user_data,
                    user_text=text,
                    intent="compose",
                    person=compose.get("recipient_name") or text.strip(),
                    person_email=email,
                )
                return

    if step == COMPOSE_NEED_CONTENT:
        if looks_like_recipient_switch(text):
            mention = extract_person_reference(text, convo)
            if mention:
                email, note = await _resolve_recipient_hint(mention, convo)
                if email:
                    compose["recipient"] = email
                    compose["recipient_name"] = mention
                    extra = classify_message(text, convo).get("content")
                    if extra:
                        compose["instructions"] = extra
                    if compose.get("instructions"):
                        context.user_data["compose"] = compose
                        await update.message.reply_text(f"Updated the recipient to {mention} ({email}).")
                        await _draft_and_show_compose(update, context)
                    else:
                        compose["step"] = COMPOSE_NEED_CONTENT
                        context.user_data["compose"] = compose
                        await update.message.reply_text(
                            f"Got it — I'll send to {mention} ({email}). What do you want to say in the email?"
                        )
                    remember_turn(
                        context.user_data,
                        user_text=text,
                        intent="compose",
                        person=mention,
                        person_email=email,
                    )
                    return
                if note:
                    await update.message.reply_text(note)
                    remember_turn(context.user_data, user_text=text, intent="compose")
                    return
        compose["instructions"] = text.strip()
        if compose.get("recipient"):
            context.user_data["compose"] = compose
            await _draft_and_show_compose(update, context)
            remember_turn(context.user_data, user_text=text, intent="compose", query=text)
            return
        compose["step"] = COMPOSE_NEED_RECIPIENT
        context.user_data["compose"] = compose
        await update.message.reply_text("Who should I send it to? (name or email address)")
        remember_turn(context.user_data, user_text=text, intent="compose", query=text)
        return

    if step == COMPOSE_NEED_RECIPIENT:
        email, note = await _resolve_recipient_hint(text.strip(), convo)
        if note and not email:
            await update.message.reply_text(note)
            remember_turn(context.user_data, user_text=text, intent="compose")
            return
        compose["recipient"] = email or text.strip()
        if compose.get("instructions"):
            context.user_data["compose"] = compose
            await _draft_and_show_compose(update, context)
            remember_turn(
                context.user_data,
                user_text=text,
                intent="compose",
                person=text.strip(),
                person_email=compose.get("recipient"),
            )
            return
        compose["step"] = COMPOSE_NEED_CONTENT
        context.user_data["compose"] = compose
        await update.message.reply_text(
            f"Got it — I'll send to {compose['recipient']}. What do you want to say in the email?"
        )
        remember_turn(
            context.user_data,
            user_text=text,
            intent="compose",
            person=text.strip(),
            person_email=compose.get("recipient"),
        )
        return

    if step == COMPOSE_CONFIRM:
        lower = text.strip().lower().replace("&", "and")
        if lower in _COMPOSE_CANCEL_WORDS:
            _clear_compose(context.user_data)
            await update.message.reply_text("Cancelled. Draft discarded.")
            return
        if lower in _COMPOSE_APPROVE_WORDS:
            await _approve_compose_from_chat(update, context)
            return
        if re.search(
            r"\b(?:instead|not (?:her|him|them)|change (?:the )?(?:to|recipient)|send (?:it |this |that )?to)\b",
            lower,
        ):
            mention = extract_person_reference(text, convo)
            if mention:
                email, note = await _resolve_recipient_hint(mention, convo)
                if email:
                    compose["recipient"] = email
                    compose["recipient_name"] = mention
                    compose.pop("draft", None)
                    context.user_data["compose"] = compose
                    await update.message.reply_text(f"Updated the recipient to {mention} ({email}). Redrafting…")
                    await _draft_and_show_compose(update, context)
                    remember_turn(
                        context.user_data,
                        user_text=text,
                        intent="compose",
                        person=mention,
                        person_email=email,
                    )
                    return
                if note:
                    await update.message.reply_text(note)
                    remember_turn(context.user_data, user_text=text, intent="compose")
                    return
        if any(w in lower for w in ("edit", "change", "revise", "regenerate", "redo")):
            if any(w in lower for w in ("regenerate", "redo")):
                compose.pop("draft", None)
                context.user_data["compose"] = compose
                await _draft_and_show_compose(update, context)
            else:
                compose["step"] = COMPOSE_NEED_CONTENT
                context.user_data["compose"] = compose
                await update.message.reply_text("What should I change? Describe the new content.")
            remember_turn(context.user_data, user_text=text, intent="compose")
            return
        await update.message.reply_text(
            "Draft waiting for approval. Tap Approve & Send, or type send / edit / cancel.",
            reply_markup=_compose_keyboard(),
        )
        remember_turn(context.user_data, user_text=text, intent="compose")
        return


async def chat_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text_override: str | None = None,
) -> None:
    """Answer free-text questions in DMs, mentions, or replies to the bot."""
    if not update.message or not (text_override or update.message.text):
        return
    if not _require_user(update):
        if not getattr(update, "is_whatsapp", False):
            return
        from services.mailbox_user import set_user
        from services.user_ids import OWNER_SEED_USER_ID
        from services.users import get_owner_user

        set_user(get_owner_user() or {"id": OWNER_SEED_USER_ID, "role": "owner", "is_active": True})
    input_text = text_override or update.message.text

    if _get_compose(context.user_data):
        text = input_text
        lower = text.strip().lower()
        if lower in _COMPOSE_CANCEL_WORDS:
            _clear_compose(context.user_data)
            await update.message.reply_text("Cancelled. Draft discarded.")
            return
        convo = get_convo(context.user_data)
        compose = _get_compose(context.user_data)
        step = compose.get("step")

        if step == COMPOSE_NEED_CONTENT:
            # User was asked "What do you want to say?" — treat ANY text as
            # email content. Do NOT run recipient-reply or breakout checks
            # here; those misrouted legitimate content (e.g. text containing
            # "from" or "class") and silently killed the compose draft.
            await _continue_compose_flow(update, context, text)
            return

        if step == COMPOSE_NEED_RECIPIENT:
            if _is_compose_recipient_reply(text, convo, compose):
                await _continue_compose_flow(update, context, text)
                return
            if _looks_like_compose_breakout(text):
                _clear_compose(context.user_data)
            else:
                await _continue_compose_flow(update, context, text)
                return

        # Draft is waiting — keep the compose flow (instead / send / edit / cancel).
        await _continue_compose_flow(update, context, text)
        return

    if _get_add_task(context.user_data):
        text = input_text
        if text.strip().lower() in _TASK_CANCEL_WORDS:
            _clear_add_task(context.user_data)
            await update.message.reply_text("Cancelled.")
            return
        await _continue_add_task_flow(update, context, text)
        return

    if _get_edit_task(context.user_data):
        text = input_text
        state = _get_edit_task(context.user_data)
        if text.strip().lower() in _TASK_CANCEL_WORDS:
            _clear_edit_task(context.user_data)
            await update.message.reply_text("Edit cancelled.")
            return
        if state and state.get("step"):
            await _continue_edit_task_flow(update, context, text)
            return

    text = input_text
    bot_username = context.bot.username
    chat = update.effective_chat
    is_private = chat and chat.type == ChatType.PRIVATE
    mentioned = bool(
        bot_username
        and update.message.entities
        and any(
            e.type == "mention"
            and text[e.offset : e.offset + e.length].lower() == f"@{bot_username.lower()}"
            for e in update.message.entities
        )
    )
    is_reply_to_bot = bool(
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
    is_category_group = category is not None

    if not (is_private or mentioned or is_reply_to_bot or is_category_group):
        return

    question = _strip_bot_mention(text, bot_username)
    if not question:
        await update.message.reply_text("Ask me about your deadlines, placements, or admin items.")
        return

    # Library story-brief shortcut: a bare number ("7") or "story 7" right
    # after the daily briefing resolves to that story's full brief.
    story_match = _STORY_NUM_RE.match(question)
    if story_match:
        if await _send_story_brief_reply(update.message, int(story_match.group(1))):
            remember_turn(context.user_data, user_text=question, intent="story_brief")
            return
        # No such story — fall through to normal chat handling.

    greeting = re.fullmatch(r"(?:hi|hello|hey|thanks|thank you)[.!]?", question, flags=re.IGNORECASE)
    if greeting:
        reply = "Hey — ask about deadlines, mail, or classes, or send /menu."
        await update.message.reply_text(reply)
        remember_turn(context.user_data, user_text=question, bot_text=reply, intent="smalltalk")
        return

    if re.search(r"\bswitch\s+to\b", question, flags=re.IGNORECASE):
        reply = "I choose the AI automatically. Ask me about mail, deadlines, or classes."
        await update.message.reply_text(reply)
        remember_turn(context.user_data, user_text=question, bot_text=reply, intent="smalltalk")
        return

    convo = get_convo(context.user_data)
    classified = await asyncio.to_thread(classify_message, question, convo)
    intent = classified.get("intent", INTENT_MAILBOX_QA)

    if intent == INTENT_SMALLTALK:
        reply = "Hey — ask me about deadlines, mail, /send to compose, or /classes."
        await update.message.reply_text(reply)
        remember_turn(context.user_data, user_text=question, bot_text=reply, intent=intent)
        return

    if _is_llm_status_question(question):
        reply = _llm_status_reply()
        await update.message.reply_text(reply)
        remember_turn(context.user_data, user_text=question, bot_text=reply, intent="smalltalk")
        return

    bare = _bare_email(question)
    if bare:
        person = convo.get("last_person")
        if person:
            await asyncio.to_thread(upsert_contact_from_sender, f"{person} <{bare}>")
        last_intent = convo.get("last_intent")
        if last_intent in {INTENT_COMPOSE, INTENT_CONTACT_LOOKUP} or _get_compose(context.user_data):
            await _begin_compose_flow(update, context, recipient=bare)
            return
        await _answer_chat_question(
            update,
            context,
            f"mails from {bare}",
            person_hint=person or bare,
            intent=INTENT_MAILBOX_QA,
        )
        return

    if intent == INTENT_COMPOSE:
        await _begin_compose_flow(
            update,
            context,
            recipient=classified.get("recipient"),
            content=classified.get("content"),
        )
        return

    if intent == INTENT_CONTACT_LOOKUP:
        person = extract_person_reference(classified.get("person"), convo) or classified.get("person")
        answer = await asyncio.to_thread(lookup_contact_email, person, convo=convo)
        await reply_long(update.message, answer)
        remember_turn(
            context.user_data,
            user_text=question,
            bot_text=answer,
            intent=intent,
            person=person,
            query=question,
        )
        return

    if intent == INTENT_SCHEDULE:
        await _reply_schedule_for_question(update, context, question)
        remember_turn(context.user_data, user_text=question, intent="schedule", query=question)
        return

    if intent == INTENT_DUE_LIST:
        if _is_specific_deadline_question(question):
            await _answer_chat_question(
                update,
                context,
                question,
                person_hint=classified.get("person"),
                intent=INTENT_MAILBOX_QA,
            )
            return
        window = parse_day_window(question)
        if window is not None:
            await _reply_dated_due_list(update, context, window)
        else:
            await due_command(update, context)
        remember_turn(context.user_data, user_text=question, intent="due_list", query=question)
        return

    if intent == INTENT_MISSED_LIST:
        await missed_command(update, context)
        remember_turn(context.user_data, user_text=question, intent="missed_list", query=question)
        return

    if intent == INTENT_UNDATED_LIST:
        await undated_command(update, context)
        remember_turn(context.user_data, user_text=question, intent="undated_list", query=question)
        return

    if intent == INTENT_ADD_TASK:
        extracted = _extract_task_from_add_phrase(question)
        await _begin_add_task_flow(update, context, task=extracted)
        remember_turn(context.user_data, user_text=question, intent="add_task", query=question)
        return

    await _answer_chat_question(
        update,
        context,
        question,
        person_hint=classified.get("person"),
        intent=intent,
    )


async def media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process owner images and voice/audio messages, replying in text."""
    if not _require_user(update) or not update.message:
        return
    message = update.message
    try:
        if message.photo:
            photo = message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            data = bytes(await file.download_as_bytearray())
            await message.reply_text("Processing the image…")
            result = await asyncio.to_thread(
                analyze_image,
                data,
                mime_type="image/jpeg",
                caption=message.caption,
            )
            if result:
                await reply_long(message, result)
            else:
                await message.reply_text(
                    "I couldn't read that image accurately. Please try a clearer image."
                )
            return

        media = message.voice or message.audio
        if media:
            file = await context.bot.get_file(media.file_id)
            data = bytes(await file.download_as_bytearray())
            mime_type = getattr(media, "mime_type", None) or (
                "audio/ogg" if message.voice else "audio/mpeg"
            )
            filename = getattr(media, "file_name", None) or (
                "voice.ogg" if message.voice else "audio.mp3"
            )
            await message.reply_text("Transcribing the audio…")
            transcript = await asyncio.to_thread(
                transcribe_audio,
                data,
                filename=filename,
                mime_type=mime_type,
            )
            if not transcript:
                await message.reply_text(
                    "I couldn't transcribe that audio accurately. Please try again "
                    "with less background noise."
                )
                return
            # Voice notes are user commands/questions: feed the transcript into
            # the same text pipeline, which always replies in text.
            await chat_message(update, context, text_override=transcript)
    except Exception as exc:
        logger.exception("Telegram media processing failed: %s", exc)
        await message.reply_text("I couldn't process that media. Please try again.")


async def _answer_chat_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    question: str,
    *,
    person_hint: str | None = None,
    intent: str = "mailbox_qa",
) -> None:
    verified, cleaned = _extract_verify_tag(question)
    if verified and not cleaned:
        await update.message.reply_text(
            'Add a question after #verify, e.g. "#verify what\'s the CV deadline?"'
        )
        return

    question_for_qa = cleaned if verified else question
    last_intent = context.user_data.get("last_intent")
    if not verified and _is_schedule_question(question_for_qa, last_intent=last_intent):
        await _reply_schedule_for_question(update, context, question_for_qa)
        remember_turn(context.user_data, user_text=question, intent="schedule", query=question_for_qa)
        return

    context.user_data["last_intent"] = intent
    convo = get_convo(context.user_data)
    await update.message.chat.send_action("typing")
    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)

    try:
        if category:
            items, cat_items = await asyncio.gather(
                asyncio.to_thread(get_assignments_for_qa, category=None, limit=25),
                asyncio.to_thread(get_assignments_for_qa, category=category, limit=15),
            )
            seen = {i["id"] for i in cat_items}
            items = cat_items + [i for i in items if i["id"] not in seen]
        else:
            items = await asyncio.to_thread(get_assignments_for_qa, category=None, limit=25)

        question_text, pasted = split_question_and_paste(question_for_qa)
        answer = await asyncio.to_thread(
            answer_question,
            question_text,
            items,
            category=category,
            pasted_email=pasted,
            verified=verified,
            convo=convo,
            person_hint=person_hint,
        )
    except Exception as exc:
        logger.exception("Q&A failed: %s", exc)
        answer = (
            "Something went wrong while answering that. "
            "Try /due, /classes, or ask again in a moment."
        )

    if not answer or not answer.strip():
        answer = "I couldn't find a clear answer. Try /due or rephrase your question."

    # Plain text avoids Telegram HTML parse errors from LLM output; chunk if long.
    await reply_long(update.message, answer)
    resolved_person = person_hint or _extract_person_query(question_for_qa) or convo.get("last_person")
    resolved_person = canonical_person_name(resolved_person) or resolved_person
    person_email = await asyncio.to_thread(
        infer_person_email,
        resolved_person,
        convo=convo,
        live=False,
    )
    remember_turn(
        context.user_data,
        user_text=question,
        bot_text=answer[:800],
        intent=intent,
        person=resolved_person,
        person_email=person_email,
        query=question,
    )


async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return

    existing = _get_compose(context.user_data)
    args = " ".join(context.args).strip() if context.args else ""
    if existing and not args:
        step = existing.get("step")
        if step == COMPOSE_CONFIRM:
            await update.message.reply_text(
                "Draft still waiting. Tap Approve & Send, or type send / edit / cancel.",
                reply_markup=_compose_keyboard(),
            )
            return
        if step == COMPOSE_NEED_RECIPIENT:
            await update.message.reply_text("Who should I send it to? (name or email address)")
            return
        await update.message.reply_text("What do you want to say in the email?")
        return

    convo = get_convo(context.user_data)
    seed_recipient = None
    if convo:
        seed_recipient = convo.get("last_person") or convo.get("last_person_email")

    if args:
        if "@" in args:
            await _begin_compose_flow(update, context, recipient=args)
        elif len(args.split()) <= 4 and extract_person_reference(args, convo):
            await _begin_compose_flow(
                update, context, recipient=extract_person_reference(args, convo)
            )
        else:
            await _begin_compose_flow(update, context, content=args)
        return

    await _begin_compose_flow(
        update,
        context,
        recipient=seed_recipient,
    )


async def _send_compose_draft(user_data: dict) -> str:
    compose = _get_compose(user_data)
    draft = (compose or {}).get("draft")
    recipient = (compose or {}).get("recipient")
    if not draft or not recipient:
        raise ValueError("missing draft")
    await asyncio.to_thread(
        send_email,
        to=recipient,
        subject=draft["subject"],
        body=draft["body"],
        thread_id=(compose or {}).get("reply_thread_id"),
    )
    display = (compose or {}).get("recipient_name") or recipient
    _clear_compose(user_data)
    if display and str(display) != str(recipient):
        return f"Sent to {display} ({recipient})."
    return f"Sent to {recipient}."


async def _approve_compose_from_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        sent = await _send_compose_draft(context.user_data)
    except ValueError:
        await update.message.reply_text("Something went wrong. Run /send again.")
        return
    except Exception:
        logger.exception("Failed to send email")
        await update.message.reply_text(safe_user_error_message("send that email"))
        return
    await update.message.reply_text(sent)
    remember_turn(context.user_data, bot_text=sent, intent="compose")


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == CANCEL_CB:
        _clear_compose(context.user_data)
        await query.edit_message_text("Cancelled. Draft discarded.")
        return

    try:
        sent = await _send_compose_draft(context.user_data)
    except ValueError:
        await query.edit_message_text("Something went wrong. Run /send again.")
        return
    except Exception:
        logger.exception("Failed to send email")
        await query.edit_message_text(safe_user_error_message("send that email"))
        return
    await query.edit_message_text(sent)
    remember_turn(context.user_data, bot_text=sent, intent="compose")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return
    cleared: list[str] = []
    if _get_compose(context.user_data):
        _clear_compose(context.user_data)
        cleared.append("email draft")
    if _get_add_task(context.user_data):
        _clear_add_task(context.user_data)
        cleared.append("add-task wizard")
    if _get_edit_task(context.user_data):
        _clear_edit_task(context.user_data)
        cleared.append("edit flow")
    if not cleared:
        await update.message.reply_text("Nothing in progress to cancel.")
        return
    await update.message.reply_text(f"Cancelled {' and '.join(cleared)}.")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_user(update) or not update.message:
        return
    had_compose = bool(_get_compose(context.user_data))
    had_task_flow = bool(_get_add_task(context.user_data) or _get_edit_task(context.user_data))
    clear_convo(context.user_data)
    msg = "Context cleared. You can start a fresh topic — deadlines, mail lookup, or /send."
    if had_compose or had_task_flow:
        msg = (
            "Context cleared (any in-progress draft or task flow was discarded). "
            "Start fresh — deadlines, mail lookup, or /send."
        )
    await update.message.reply_text(msg)


async def backfill_contacts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-time admin: populate contacts from recent inbox senders."""
    if not _is_owner(update) or not update.message:
        return
    await update.message.reply_text("Scanning recent inbox to learn contacts…")
    async with watcher_lock:
        stats = await asyncio.to_thread(backfill_contacts_from_inbox)
    if stats.get("error"):
        await update.message.reply_text("Contact backfill failed. Check logs.")
        return
    await update.message.reply_text(
        f"Done. Scanned {stats.get('scanned', 0)} messages; "
        f"learned/updated {stats.get('upserted', 0)} contacts."
    )


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return
    from services.users import create_invite

    raw = " ".join(context.args or []).strip().lstrip("@")
    if not raw:
        await update.message.reply_text("Usage: /invite @telegram_username")
        return
    invite = create_invite(raw)
    if not invite:
        await update.message.reply_text("Could not create invite.")
        return
    link = f"https://t.me/{context.bot.username}?start=invite_{invite['invite_code']}"
    await update.message.reply_text(
        f"Invite for @{invite['username']} (expires {invite['expires_at'][:10]}):\n{link}\n\n"
        "They tap that link and press Start. Then they open "
        f"{_web_login_url()} with the same username and tap Connect Gmail."
    )


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _require_user(update)
    if not user or not update.message:
        return
    from services.users import request_login_code

    username = user.get("telegram_username") or (update.effective_user.username if update.effective_user else "")
    if not username:
        await update.message.reply_text("Set a Telegram @username in your profile, then try again.")
        return
    result = request_login_code(username)
    code = result.pop("_code_for_bot", None)
    if not result.get("ok") or not code:
        await update.message.reply_text(result.get("message") or "Could not create code.")
        return
    await update.message.reply_text(
        f"Web login code: {code}\n\nExpires in 10 minutes. Enter it on the Mailbot web login page."
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return
    from services.users import list_users

    rows = list_users()
    lines = ["Registered users:", ""]
    for row in rows:
        un = row.get("telegram_username") or "—"
        lines.append(f"• {row.get('display_name')} (@{un}) — {row.get('role')}")
    await update.message.reply_text("\n".join(lines) or "No users yet.")


def register_handlers(application) -> None:
    application.add_handler(CommandHandler("invite", invite_command))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("due", due_command))
    application.add_handler(CommandHandler("list", due_command))
    application.add_handler(CommandHandler("missed", missed_command))
    application.add_handler(CommandHandler("undated", undated_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("edit", edit_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("classes", classes_command))
    application.add_handler(CommandHandler("library", library_command))
    application.add_handler(CommandHandler("story", story_command))
    application.add_handler(CommandHandler("section", section_command))
    application.add_handler(CallbackQueryHandler(section_button, pattern=r"^section:"))
    application.add_handler(CallbackQueryHandler(story_button, pattern=r"^story:\d+$"))
    application.add_handler(CallbackQueryHandler(clear_chat_button, pattern=f"^{CLEAR_CHAT_CB}$"))
    application.add_handler(CallbackQueryHandler(action_button, pattern=r"^act:"))
    application.add_handler(CallbackQueryHandler(addtask_callback, pattern=r"^addtask:"))
    application.add_handler(CallbackQueryHandler(edittask_callback, pattern=r"^edittask:"))
    application.add_handler(CommandHandler("ask", ask_command))
    application.add_handler(CommandHandler("send", send_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("new", reset_command))
    application.add_handler(CommandHandler("backfill_contacts", backfill_contacts_command))
    application.add_handler(
        CallbackQueryHandler(confirm_callback, pattern=f"^({APPROVE_CB}|{CANCEL_CB})$")
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^(" + "|".join(map(re.escape, QUICK_ACTION_LABELS)) + ")$"),
            quick_action_message,
        )
    )
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.VOICE | filters.AUDIO, media_message)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message))
