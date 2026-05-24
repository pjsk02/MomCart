"""MomCart Telegram bot — entrypoint."""
from __future__ import annotations

import tempfile
from pathlib import Path

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

# per-user session: user_id -> {items, awaiting_confirm}
_sessions: dict = {}


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


async def _parse_and_reply(update: Update, text: str) -> None:
    """Parse grocery text and send formatted list with confirm prompt."""
    await update.message.reply_text("List bana raha hoon... 🛒")
    try:
        from src.agent import format_item_list, parse_grocery_text

        items = await parse_grocery_text(text, user_id=settings.MOM_ID)
        if not items:
            await update.message.reply_text(
                "Koi items samajh nahi aaya. Dobara bolo? (e.g. 'do kilo aata, ek paav haldi')"
            )
            return

        _sessions[settings.MOM_ID] = {"items": items, "awaiting_confirm": True}
        item_lines = format_item_list(items)
        await update.message.reply_text(
            f"Got it:\n{item_lines}\n\nConfirm? yes/no"
        )
    except Exception as e:
        logger.error(f"Parse error: {e}")
        await update.message.reply_text("Processing mein problem. Try again.")


async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    logger.info(f"voice from {role} ({update.message.voice.duration}s)")

    try:
        tg_file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        await tg_file.download_to_drive(str(tmp_path))

        from src.stt import transcribe

        transcript = await transcribe(tmp_path)
        tmp_path.unlink(missing_ok=True)

        if not transcript:
            await update.message.reply_text("Couldn't hear anything clearly. Try again?")
            return

        await update.message.reply_text(f"Heard: {transcript}")
        await _parse_and_reply(update, transcript)

    except Exception as e:
        logger.error(f"Voice handler error: {e}")
        await update.message.reply_text("Something went wrong processing your voice note.")


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    logger.info(f"photo from {role}")
    await update.message.reply_text("got photo (Photo OCR coming soon)")


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return

    text = update.message.text.strip()
    logger.info(f"text from {role}: {text!r}")

    session = _sessions.get(settings.MOM_ID, {})

    if session.get("awaiting_confirm"):
        text_lower = text.lower()
        if any(w in text_lower for w in ["yes", "haan", "ha", "ok", "send", "bhejo"]):
            _sessions[settings.MOM_ID] = {**session, "awaiting_confirm": False}
            await update.message.reply_text("Order confirmed ✅ (Notion + shopkeeper notify coming in Prompt 6)")
        elif any(w in text_lower for w in ["no", "nahi", "cancel"]):
            _sessions.pop(settings.MOM_ID, None)
            await update.message.reply_text("Order cancel ho gaya.")
        else:
            # Treat as addition/edit to the current list
            await _parse_and_reply(update, text)
        return

    await _parse_and_reply(update, text)


def build_app() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", _handle_start))
    app.add_handler(MessageHandler(filters.VOICE, _handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, _handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    return app


if __name__ == "__main__":
    logger.info("Starting MomCart bot...")
    build_app().run_polling(drop_pending_updates=True)
