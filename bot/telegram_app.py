import logging

from telegram import BotCommand
from telegram.ext import Application

from bot.handlers import register_handlers
from config import get_settings
from services.persistence import SupabasePersistence

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Welcome + help"),
    BotCommand("due", "Open deadlines & tasks"),
    BotCommand("done", "Mark item #n from /due as complete"),
    BotCommand("classes", "Today's classes for your section"),
    BotCommand("section", "Change or check your section"),
    BotCommand("ask", "Ask about deadlines, mail, senders, or content"),
    BotCommand("send", "Compose and send an email"),
    BotCommand("cancel", "Cancel email draft"),
    BotCommand("reset", "Clear conversation context"),
]


async def register_bot_commands(application: Application) -> None:
    """Populate the Telegram '/' command menu.

    Called explicitly from the app lifespan because this app drives PTB's
    lifecycle manually (initialize/start under FastAPI), and PTB only invokes
    ``post_init`` from run_polling/run_webhook — not from ``initialize()``.
    """
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Registered %d Telegram bot commands", len(BOT_COMMANDS))
    except Exception as exc:
        logger.warning("Could not set bot commands: %s", exc)


def build_telegram_application() -> Application:
    settings = get_settings()
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .persistence(SupabasePersistence())
        .post_init(register_bot_commands)
        .build()
    )
    register_handlers(app)
    return app
