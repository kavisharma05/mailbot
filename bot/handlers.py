import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ai.assistant import answer_question, split_question_and_paste
from ai.composer import compose_email
from config import get_settings
from gmail_api.client import send_email
from services.db import (
    get_assignments_for_qa,
    get_open_assignments,
    log_low_confidence,
    mark_assignment_done,
)
from services.schedule import format_today_classes
from services.timeutil import IST, format_deadline_display, today_ist

logger = logging.getLogger(__name__)

INSTRUCTIONS, RECIPIENT, CONFIRM = range(3)

APPROVE_CB = "send_approve"
CANCEL_CB = "send_cancel"

CATEGORY_TITLES = {
    "deadline": "Deadlines",
    "placement": "Placements",
    "admin": "Admin",
    "other": "Other",
}


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
    mapping = {
        str(settings.telegram_chat_deadlines): "deadline",
        str(settings.telegram_chat_placements): "placement",
        str(settings.telegram_chat_admin): "admin",
        str(settings.telegram_chat_other): "other",
    }
    return mapping.get(chat)


def _format_due_list(items: list[dict], category: str | None) -> str:
    title = CATEGORY_TITLES.get(category or "", "Open items")
    if not items:
        return f"<b>{html.escape(title)}</b>\n\nNo open items right now."

    lines = [f"<b>{html.escape(title)}</b> — {len(items)} open", ""]
    for i, item in enumerate(items[:20], start=1):
        task = item.get("task") or item.get("summary") or "Untitled"
        course = item.get("course_or_topic") or "—"
        due = format_deadline_display(item.get("deadline_date"), item.get("deadline_text"), tz=IST)
        cat = item.get("category") or "other"
        prefix = f"[{CATEGORY_TITLES.get(cat, cat)}] " if not category else ""
        lines.append(f"{i}. {prefix}<b>{html.escape(str(task))}</b>")
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return

    await update.message.reply_text(
        "Mailbot ready.\n\n"
        "Commands:\n"
        "/due — open items for this chat/category\n"
        "/list — same as /due\n"
        "/done <n> — mark item #n from /due as complete\n"
        "/classes — today's classes (time + room)\n"
        "/ask <question> — ask about deadlines, mails today, senders, or email content\n"
        "/send — compose and send an email (DM preferred)\n"
        "/cancel — cancel the current /send flow\n\n"
        "In groups: reply to my messages, or mention me:\n"
        "@IMTAssistant_bot what's due this week?\n\n"
        "Tip: In BotFather → /setprivacy → Disable, so I can see all group messages."
    )


async def due_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return

    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)
    items = get_open_assignments(category=category)
    context.user_data["due_items"] = [i["id"] for i in items[:20]]
    await update.message.reply_text(_format_due_list(items, category), parse_mode="HTML")


async def classes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return

    text = format_today_classes(today_ist())
    if not text:
        await update.message.reply_text(
            "No classes stored for today yet.\n"
            "When college sends a timetable (body or PDF/Excel), I'll parse it and "
            "show rooms/times here + in the morning digest."
        )
        return
    await update.message.reply_text(text, parse_mode="HTML")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or not update.message:
        return

    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)

    if not context.args:
        items = get_open_assignments(category=category)
        context.user_data["due_items"] = [i["id"] for i in items[:20]]
        await update.message.reply_text(
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
        items = get_open_assignments(category=category)
        due_ids = [i["id"] for i in items[:20]]
        context.user_data["due_items"] = due_ids

    if index < 1 or index > len(due_ids):
        items = get_open_assignments(category=category)
        context.user_data["due_items"] = [i["id"] for i in items[:20]]
        await update.message.reply_text(
            f"No item #{index}. Pick from the list:\n\n" + _format_due_list(items, category),
            parse_mode="HTML",
        )
        return

    assignment_id = due_ids[index - 1]
    updated = mark_assignment_done(assignment_id)
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


async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer free-text questions in DMs, mentions, or replies to the bot."""
    if not _is_owner(update) or not update.message or not update.message.text:
        return

    # Don't steal messages from /send conversation states
    if context.user_data.get("instructions") or context.user_data.get("draft"):
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

    await _answer_chat_question(update, context, question)


async def _answer_chat_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    question: str,
) -> None:
    await update.message.chat.send_action("typing")
    category = _category_for_chat(update.effective_chat.id if update.effective_chat else None)

    # Keep payload small for Groq free-tier TPD limits
    items = get_assignments_for_qa(category=None, limit=25)
    if category:
        cat_items = get_assignments_for_qa(category=category, limit=15)
        seen = {i["id"] for i in cat_items}
        items = cat_items + [i for i in items if i["id"] not in seen]

    question_text, pasted = split_question_and_paste(question)
    answer = answer_question(
        question_text,
        items,
        category=category,
        pasted_email=pasted,
    )
    await update.message.reply_text(answer)


async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_owner(update):
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "What do you want to say? Give me rough instructions and I'll draft the email."
    )
    return INSTRUCTIONS


async def receive_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["instructions"] = update.message.text
    await update.message.reply_text("Who should I send it to? (email address)")
    return RECIPIENT


async def receive_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    recipient = update.message.text.strip()
    if "@" not in recipient:
        await update.message.reply_text("That doesn't look like an email. Try again:")
        return RECIPIENT

    context.user_data["recipient"] = recipient
    instructions = context.user_data["instructions"]

    await update.message.reply_text("Drafting your email...")
    draft = compose_email(instructions)

    if not draft or "subject" not in draft or "body" not in draft:
        log_low_confidence("composer", {"instructions": instructions, "draft": draft})
        await update.message.reply_text("Couldn't draft the email. Try rephrasing your instructions.")
        return ConversationHandler.END

    context.user_data["draft"] = draft

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve & Send", callback_data=APPROVE_CB),
                InlineKeyboardButton("Cancel", callback_data=CANCEL_CB),
            ]
        ]
    )

    await update.message.reply_text(
        f"To: {recipient}\n"
        f"Subject: {draft['subject']}\n\n"
        f"{draft['body']}",
        reply_markup=keyboard,
    )
    return CONFIRM


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == CANCEL_CB:
        context.user_data.clear()
        await query.edit_message_text("Cancelled. Draft discarded.")
        return ConversationHandler.END

    draft = context.user_data.get("draft")
    recipient = context.user_data.get("recipient")

    if not draft or not recipient:
        await query.edit_message_text("Something went wrong. Run /send again.")
        return ConversationHandler.END

    try:
        send_email(to=recipient, subject=draft["subject"], body=draft["body"])
        await query.edit_message_text(f"Sent to {recipient}.")
    except Exception as exc:
        logger.exception("Failed to send email")
        await query.edit_message_text(f"Failed to send: {exc}")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("send", send_start)],
        states={
            INSTRUCTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_instructions)],
            RECIPIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_recipient)],
            CONFIRM: [CallbackQueryHandler(confirm_callback, pattern=f"^({APPROVE_CB}|{CANCEL_CB})$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )


def register_handlers(application) -> None:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("due", due_command))
    application.add_handler(CommandHandler("list", due_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("classes", classes_command))
    application.add_handler(CommandHandler("ask", ask_command))
    application.add_handler(build_conversation_handler())
    # Free-text Q&A after conversation handler so /send flow keeps priority
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message))
