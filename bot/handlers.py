import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ai.composer import compose_email
from gmail_api.client import send_email
from services.db import log_low_confidence

logger = logging.getLogger(__name__)

INSTRUCTIONS, RECIPIENT, CONFIRM = range(3)

APPROVE_CB = "send_approve"
CANCEL_CB = "send_cancel"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Mailbot ready.\n\n"
        "Commands:\n"
        "/send — compose and send an email to faculty\n"
        "/cancel — cancel the current /send flow"
    )


async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    application.add_handler(build_conversation_handler())
