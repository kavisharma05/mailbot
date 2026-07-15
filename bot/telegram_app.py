from telegram.ext import Application

from bot.handlers import register_handlers
from config import get_settings


def build_telegram_application() -> Application:
    settings = get_settings()
    app = Application.builder().token(settings.telegram_bot_token).build()
    register_handlers(app)
    return app
