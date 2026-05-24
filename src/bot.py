"""MomCart Telegram bot — entrypoint."""
from __future__ import annotations

from loguru import logger
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import settings


def _role(update: Update) -> str:
    uid = update.effective_user.id if update.effective_user else None
    if uid == settings.MOM_ID:
        return "mom"
    if uid == settings.SHOPKEEPER_ID:
        return "shopkeeper"
    return "unknown"


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    await update.message.reply_text(f"MomCart ready. You are: {role}")


async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    logger.info(f"voice from {role}")
    duration = update.message.voice.duration
    await update.message.reply_text(f"got voice ({duration} seconds)")


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    logger.info(f"photo from {role}")
    await update.message.reply_text("got photo")


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    logger.info(f"text from {role}: {update.message.text!r}")
    await update.message.reply_text(f"got text: {update.message.text}")


def build_app() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", _handle_start))
    app.add_handler(MessageHandler(filters.VOICE, _handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, _handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    return app


if __name__ == "__main__":
    logger.info("Starting MomCart bot (echo mode)...")
    build_app().run_polling(drop_pending_updates=True)
