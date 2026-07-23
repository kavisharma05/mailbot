import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai.assistant import (
    _is_due_list_question,
    answer_question,
    lookup_contact_email,
    split_question_and_paste,
)
from ai.composer import compose_email
from ai.intent import (
    INTENT_COMPOSE,
    INTENT_CONTACT_LOOKUP,
    INTENT_DUE_LIST,
    INTENT_MAILBOX_QA,
    INTENT_SCHEDULE,
    INTENT_SMALLTALK,
    _is_specific_deadline_question,
    classify_message,
)
from config import get_settings
from gmail_api.client import send_email
from services.contacts import format_contact_lookup_answer, resolve_contact, resolve_contact_email
from services.conversation import clear_convo, get_convo, remember_turn
from services.alert_actions import (
    ACT_DISMISS,
    ACT_DONE,
    ACT_REMIND,
    parse_action_callback,
)
from services.db import (
    dismiss_assignment,
    get_assignments_for_qa,
    get_open_assignments,
    get_preferred_section,
    log_low_confidence,
    mark_assignment_done,
    schedule_assignment_reminder,
    set_preferred_section,
)
from services.formatting import CATEGORY_LABELS, is_priority_item
from services.schedule import (
    _format_session_block,
    _session_section,
    classes_command_text,
    load_template_sessions,
    project_sessions_to_day,
    refresh_schedule_from_gmail,
    sessions_for_day,
)
from services.telegram_util import reply_long
from services.timeutil import IST, format_deadline_display, today_ist

logger = logging.getLogger(__name__)

INSTRUCTIONS, RECIPIENT, CONFIRM = range(3)

COMPOSE_NEED_CONTENT = "need_content"
COMPOSE_NEED_RECIPIENT = "need_recipient"
COMPOSE_CONFIRM = "confirm"

APPROVE_CB = "send_approve"
CANCEL_CB = "send_cancel"
CLEAR_CHAT_CB = "ui:clear_chat"

# How many recent message IDs to try when clearing bot clutter in owner DM
_CLEAR_CHAT_SCAN_LIMIT = 25

CATEGORY_TITLES = CATEGORY_LABELS

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


def _is_owner(update: Update) -> bool:
    settings = get_settings()
    user = update.effective_user
    if not user:
        return False
    return str(user.id) == str(settings.telegram_owner_chat_id)


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
    def key(item: dict) -> tuple[int, str]:
        cat = item.get("category") or "other"
        pri = 0 if is_priority_item(
            cat,
            task=item.get("task"),
            summary=item.get("summary"),
            deadline_date=item.get("deadline_date"),
        ) else 1
        due = item.get("deadline_date") or "9999"
        return (pri, due)

    return sorted(items, key=key)


def _format_due_list(items: list[dict], category: str | None) -> str:
    title = CATEGORY_TITLES.get(category or "", "Open items")
    items = _sort_items_priority_first(items)
    if not items:
        return f"<b>{html.escape(title)}</b>\n\nNo open items right now."

    lines = [f"<b>{html.escape(title)}</b> — {len(items)} open", ""]
    for i, item in enumerate(items[:20], start=1):
        task = item.get("task") or item.get("summary") or "Untitled"
        course = item.get("course_or_topic") or "—"
        due = format_deadline_display(item.get("deadline_date"), item.get("deadline_text"), tz=IST)
        cat = item.get("category") or "other"
        prefix = f"[{CATEGORY_TITLES.get(cat, cat)}] " if not category else ""
        pri = "🔴 " if is_priority_item(
            cat,
            task=task,
            summary=item.get("summary"),
            deadline_date=item.get("deadline_date"),
        ) else ""
        lines.append(f"{i}. {pri}{prefix}<b>{html.escape(str(task))}</b>")
        lines.append(f"   Course: {html.escape(str(course))}")
        lines.append(f"   Due: {html.escape(str(due))}")
        lines.append("")
    return "\n".join(lines).strip()


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


def _resolve_target_day(question: str, *, fallback_iso: str | None = None):
    from datetime import date as date_cls

    q = question.lower()
    day = today_ist()
    if "tomorrow" in q:
        return day + timedelta(days=1)
    if "today" in q:
        return day
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", q)
    if m:
        try:
            return date_cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # "21st", "21 Jul", "for 21"
    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s*(?:of\s+)?(jan|january|feb|february|mar|march|"
        r"apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|"
        r"nov|november|dec|december))?\b",
        q,
    )
    if m and ("class" in q or "schedule" in q or "for" in q or m.group(2)):
        try:
            dom = int(m.group(1))
            if m.group(2):
                months = {
                    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
                    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
                    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
                    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
                }
                month = months[m.group(2)]
            else:
                month = day.month
            return date_cls(day.year, month, dom)
        except ValueError:
            pass
    for i, name in enumerate(WEEKDAYS):
        if name in q:
            days_ahead = (i - day.weekday()) % 7
            days_ahead = days_ahead or 7  # "monday" said on a Monday means next Monday
            return day + timedelta(days=days_ahead)
    if fallback_iso:
        try:
            return date_cls.fromisoformat(fallback_iso)
        except ValueError:
            pass
    return day


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
    await reply_long(update.message, text, parse_mode="HTML")


async def action_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    parsed = parse_action_callback(query.data)
    if not parsed:
        await query.answer()
        return

    action, assignment_id = parsed
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


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🧹 Clear chat", callback_data=CLEAR_CHAT_CB)]]
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
    if not _is_owner(update):
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
    if not _is_owner(update):
        return
    await start_command(update, context)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return

    msg = await update.message.reply_text(
        "Mailbot ready.\n\n"
        "Morning digest (7:00 IST) and evening digest (20:00 IST) land here in your DM.\n"
        "Free text works here too — ask about deadlines, mail, or senders.\n\n"
        "Commands:\n"
        "/due — open deadlines & tasks (priority first)\n"
        "/done <n> — mark item #n from /due as complete\n"
        "/classes — today's classes for your section (tap to set it first time)\n"
        "/section — change or check your section\n"
        "/ask <question> — ask about deadlines, mails, senders, or email content\n"
        "Add #verify to any question for a fresh Gmail answer with cited sources\n"
        "/send — compose and send an email\n"
        "/cancel — cancel an in-progress email draft only\n"
        "/reset (or /new) — clear conversation context; discards any draft\n\n"
        "Actionable alerts have DONE / NOT IMPORTANT / REMIND AGAIN buttons.\n"
        "Marks in attachments show as text; readings are forwarded as files.\n\n"
        "In groups: reply to my messages, or mention me:\n"
        "@IMTAssistant_bot what's due this week?\n\n"
        "Tip: In BotFather → /setprivacy → Disable, so I can see all group messages.",
        reply_markup=_start_keyboard(),
    )
    _track_bot_message(context, msg.message_id)


async def due_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return

    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
    items = await asyncio.to_thread(get_open_assignments, category=category)
    context.user_data["due_items"] = [i["id"] for i in items[:20]]
    await reply_long(update.message, _format_due_list(items, category), parse_mode="HTML")


async def classes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
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
    if not _is_owner(update) or not update.message:
        return
    current = await asyncio.to_thread(get_preferred_section)
    label = f"Current: Section {current}\n\n" if current else ""
    available = await _available_sections() or ["A", "B", "C", "D"]
    await update.message.reply_text(f"{label}Pick your section:", reply_markup=_section_buttons(available))


async def section_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    query = update.callback_query
    await query.answer()
    section = query.data.split(":", 1)[1]
    await asyncio.to_thread(set_preferred_section, section)
    await query.edit_message_text(
        f"Got it — showing Section {section} from now on. Run /classes anytime."
    )


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return

    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)

    if not context.args:
        items = await asyncio.to_thread(get_open_assignments, category=category)
        context.user_data["due_items"] = [i["id"] for i in items[:20]]
        await reply_long(
            update.message,
            "Usage: /done <number>\n\n" + _format_due_list(items, category),
            parse_mode="HTML",
        )
        return

    try:
        index = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /done <number> — use the number from /due")
        return

    due_ids: list[str] = context.user_data.get("due_items") or []
    if not due_ids:
        items = await asyncio.to_thread(get_open_assignments, category=category)
        due_ids = [i["id"] for i in items[:20]]
        context.user_data["due_items"] = due_ids

    if index < 1 or index > len(due_ids):
        items = await asyncio.to_thread(get_open_assignments, category=category)
        context.user_data["due_items"] = [i["id"] for i in items[:20]]
        await reply_long(
            update.message,
            f"No item #{index}. Pick from the list:\n\n" + _format_due_list(items, category),
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
    if not _is_owner(update) or not update.message:
        return

    question = " ".join(context.args).strip() if context.args else ""
    if not question:
        await update.message.reply_text("Usage: /ask what's due this week?")
        return

    await _answer_chat_question(update, context, question)


def _get_compose(user_data: dict) -> dict | None:
    compose = user_data.get("compose")
    return compose if isinstance(compose, dict) else None


def _clear_compose(user_data: dict) -> None:
    user_data.pop("compose", None)


_COMPOSE_CANCEL_WORDS = frozenset({"cancel", "stop", "discard", "abort", "/cancel"})


def _looks_like_compose_breakout(text: str) -> bool:
    """True when free text during compose is clearly not part of drafting."""
    q = text.lower().strip()
    if any(m in q for m in SCHEDULE_KEYWORDS):
        return True
    if _is_due_list_question(text):
        return True
    if any(
        m in q
        for m in (
            "what's her email",
            "what is her email",
            "what's his email",
            "what is his email",
            "what did ",
            " from ",
            "when is",
            "when's",
            "what's the deadline",
            "what is the deadline",
            "#verify",
        )
    ):
        return True
    classified = classify_message(text, None)
    return classified.get("intent") in {
        INTENT_CONTACT_LOOKUP,
        INTENT_MAILBOX_QA,
        INTENT_SCHEDULE,
        INTENT_DUE_LIST,
    }


def _compose_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve & Send", callback_data=APPROVE_CB),
                InlineKeyboardButton("Cancel", callback_data=CANCEL_CB),
            ]
        ]
    )


async def _resolve_recipient_hint(recipient: str | None) -> tuple[str | None, str | None]:
    """Return (email, disambiguation_message)."""
    if not recipient:
        return None, None
    text = recipient.strip()
    if "@" in text:
        return text.lower(), None
    email = await asyncio.to_thread(resolve_contact_email, text)
    if email:
        return email, None
    matches = await asyncio.to_thread(resolve_contact, text)
    if len(matches) == 1:
        return matches[0].get("email"), None
    if len(matches) > 1:
        return None, await asyncio.to_thread(format_contact_lookup_answer, text, matches)
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
    draft = await asyncio.to_thread(compose_email, instructions)
    if not draft or "subject" not in draft or "body" not in draft:
        await asyncio.to_thread(
            log_low_confidence,
            "composer",
            {"instructions": instructions, "draft": draft},
        )
        await update.message.reply_text("Couldn't draft the email. Try rephrasing your instructions.")
        _clear_compose(context.user_data)
        return

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
    compose: dict = {"step": COMPOSE_NEED_CONTENT, "instructions": None, "recipient": None, "draft": None}
    if content:
        compose["instructions"] = content.strip()
    if recipient:
        email, note = await _resolve_recipient_hint(recipient)
        if note:
            compose["recipient_name"] = recipient
            if compose["instructions"]:
                compose["pending_recipient_name"] = recipient
            await update.message.reply_text(note)
            if not compose["instructions"]:
                compose["step"] = COMPOSE_NEED_CONTENT
                context.user_data["compose"] = compose
                await update.message.reply_text("What do you want to say in the email?")
                remember_turn(
                    context.user_data,
                    user_text=update.message.text,
                    bot_text=note,
                    intent="compose",
                )
                return
        elif email:
            compose["recipient"] = email

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

    if compose.get("instructions") and not compose.get("recipient"):
        compose["step"] = COMPOSE_NEED_RECIPIENT
        context.user_data["compose"] = compose
        await update.message.reply_text("Who should I send it to? (name or email address)")
        remember_turn(context.user_data, user_text=update.message.text, intent="compose", query=compose["instructions"])
        return

    compose["step"] = COMPOSE_NEED_CONTENT
    context.user_data["compose"] = compose
    if recipient and not compose.get("recipient"):
        compose["recipient_name"] = recipient
        await update.message.reply_text("What do you want to say in the email?")
    else:
        await update.message.reply_text(
            "What do you want to say? Give me rough instructions and I'll draft the email."
        )
    remember_turn(context.user_data, user_text=update.message.text, intent="compose", person=recipient)


async def _continue_compose_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    compose = _get_compose(context.user_data)
    if not compose:
        return

    step = compose.get("step")
    if step == COMPOSE_NEED_CONTENT:
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
        email, note = await _resolve_recipient_hint(text.strip())
        if note and not email:
            await update.message.reply_text(note)
            remember_turn(context.user_data, user_text=text, intent="compose")
            return
        compose["recipient"] = email or text.strip()
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

    if step == COMPOSE_CONFIRM:
        lower = text.strip().lower()
        if lower in _COMPOSE_CANCEL_WORDS:
            _clear_compose(context.user_data)
            await update.message.reply_text("Cancelled. Draft discarded.")
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
            "Draft waiting for approval. Tap Approve & Send, Cancel, or type edit / cancel.",
            reply_markup=_compose_keyboard(),
        )
        remember_turn(context.user_data, user_text=text, intent="compose")
        return


async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer free-text questions in DMs, mentions, or replies to the bot."""
    if not _is_owner(update) or not update.message or not update.message.text:
        return

    if _get_compose(context.user_data):
        text = update.message.text
        lower = text.strip().lower()
        if lower in _COMPOSE_CANCEL_WORDS:
            _clear_compose(context.user_data)
            await update.message.reply_text("Cancelled. Draft discarded.")
            return
        if _looks_like_compose_breakout(text):
            _clear_compose(context.user_data)
        else:
            await _continue_compose_flow(update, context, text)
            return

    text = update.message.text
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

    convo = get_convo(context.user_data)
    classified = await asyncio.to_thread(classify_message, question, convo)
    intent = classified.get("intent", INTENT_MAILBOX_QA)

    if intent == INTENT_SMALLTALK:
        reply = "Hey — ask me about deadlines, mail, /send to compose, or /classes."
        await update.message.reply_text(reply)
        remember_turn(context.user_data, user_text=question, bot_text=reply, intent=intent)
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
        person = classified.get("person")
        answer = await asyncio.to_thread(lookup_contact_email, person)
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
        await due_command(update, context)
        remember_turn(context.user_data, user_text=question, intent="due_list", query=question)
        return

    await _answer_chat_question(
        update,
        context,
        question,
        person_hint=classified.get("person"),
        intent=intent,
    )


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
        items = await asyncio.to_thread(get_assignments_for_qa, category=None, limit=25)
        if category:
            cat_items = await asyncio.to_thread(get_assignments_for_qa, category=category, limit=15)
            seen = {i["id"] for i in cat_items}
            items = cat_items + [i for i in items if i["id"] not in seen]

        question_text, pasted = await asyncio.to_thread(split_question_and_paste, question_for_qa)
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
    person = person_hint or convo.get("last_person")
    person_email = convo.get("last_person_email")
    if person_hint:
        resolved = await asyncio.to_thread(resolve_contact_email, person_hint)
        if resolved:
            person_email = resolved
    remember_turn(
        context.user_data,
        user_text=question,
        bot_text=answer[:800],
        intent=intent,
        person=person_hint or person,
        person_email=person_email,
        query=question,
    )


async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return

    convo = get_convo(context.user_data)
    seed_recipient = convo.get("last_person_email") if convo else None

    args = " ".join(context.args).strip() if context.args else ""
    if args:
        if "@" in args:
            await _begin_compose_flow(update, context, recipient=args)
        else:
            await _begin_compose_flow(update, context, content=args)
        return

    await _begin_compose_flow(
        update,
        context,
        recipient=seed_recipient,
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == CANCEL_CB:
        _clear_compose(context.user_data)
        await query.edit_message_text("Cancelled. Draft discarded.")
        return

    compose = _get_compose(context.user_data)
    draft = (compose or {}).get("draft")
    recipient = (compose or {}).get("recipient")

    if not draft or not recipient:
        await query.edit_message_text("Something went wrong. Run /send again.")
        return

    try:
        await asyncio.to_thread(
            send_email,
            to=recipient,
            subject=draft["subject"],
            body=draft["body"],
        )
        await query.edit_message_text(f"Sent to {recipient}.")
        remember_turn(
            context.user_data,
            bot_text=f"Sent to {recipient}.",
            intent="compose",
            person_email=recipient,
        )
    except Exception as exc:
        logger.exception("Failed to send email")
        await query.edit_message_text(f"Failed to send: {exc}")

    _clear_compose(context.user_data)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return
    if not _get_compose(context.user_data):
        await update.message.reply_text("No compose in progress.")
        return
    _clear_compose(context.user_data)
    await update.message.reply_text("Cancelled. Draft discarded.")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return
    had_compose = bool(_get_compose(context.user_data))
    clear_convo(context.user_data)
    msg = "Context cleared. You can start a fresh topic — deadlines, mail lookup, or /send."
    if had_compose:
        msg = (
            "Context cleared (any in-progress draft was discarded). "
            "Start fresh — deadlines, mail lookup, or /send."
        )
    await update.message.reply_text(msg)


def register_handlers(application) -> None:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("due", due_command))
    application.add_handler(CommandHandler("list", due_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("classes", classes_command))
    application.add_handler(CommandHandler("section", section_command))
    application.add_handler(CallbackQueryHandler(section_button, pattern=r"^section:"))
    application.add_handler(CallbackQueryHandler(clear_chat_button, pattern=f"^{CLEAR_CHAT_CB}$"))
    application.add_handler(CallbackQueryHandler(action_button, pattern=r"^act:"))
    application.add_handler(CommandHandler("ask", ask_command))
    application.add_handler(CommandHandler("send", send_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("new", reset_command))
    application.add_handler(
        CallbackQueryHandler(confirm_callback, pattern=f"^({APPROVE_CB}|{CANCEL_CB})$")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message))
