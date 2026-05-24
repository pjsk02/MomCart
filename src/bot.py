"""MomCart Telegram bot — entrypoint."""
import tempfile
from pathlib import Path

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import settings
from src.prompts import NOT_AUTHORIZED, START_MOM, START_SHOPKEEPER

# In-memory session state: user_id → {items, order_id, awaiting_confirm}
_sessions: dict[int, dict] = {}


def _is_mom(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == settings.MOM_ID


def _is_shopkeeper(update: Update) -> bool:
    return (
        update.effective_user is not None
        and update.effective_user.id == settings.SHOPKEEPER_ID
    )


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_mom(update):
        await update.message.reply_text(START_MOM)
    elif _is_shopkeeper(update):
        await update.message.reply_text(START_SHOPKEEPER)
    else:
        await update.message.reply_text(NOT_AUTHORIZED)


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_mom(update):
        await update.message.reply_text(NOT_AUTHORIZED)
        return

    session = _sessions.get(settings.MOM_ID, {})
    order_id = session.get("order_id")
    if not order_id:
        await update.message.reply_text("Koi active order nahi hai abhi.")
        return

    try:
        from src.notion_tools import get_order_summary

        counts = await get_order_summary(order_id)
        lines = [f"Order #{order_id} status:"]
        for status, count in counts.items():
            emoji = {"packed": "✅", "partial": "⚠️", "out": "❌", "pending": "⏳"}.get(status, "•")
            lines.append(f"  {emoji} {status}: {count}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"/status failed: {e}")
        await update.message.reply_text("Status fetch karne mein problem hui. Try again.")


async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_mom(update):
        logger.info(f"Unauthorized voice from {update.effective_user.id}")
        await update.message.reply_text(NOT_AUTHORIZED)
        return

    logger.info("Voice message received from mom")
    await update.message.reply_text("Suno... 🎙️")

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        await file.download_to_drive(str(tmp_path))
        logger.debug(f"Voice saved to {tmp_path}")

        from src.stt import transcribe

        transcript = await transcribe(tmp_path)
        tmp_path.unlink(missing_ok=True)

        if not transcript:
            await update.message.reply_text("Sunai nahi diya. Dobara bhejo?")
            return

        await update.message.reply_text(f"Suna: _{transcript}_", parse_mode="Markdown")
        await _process_grocery_text(update, transcript)

    except Exception as e:
        logger.error(f"Voice handler failed: {e}")
        await update.message.reply_text("Kuch problem aayi. Dobara try karo.")


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_mom(update):
        await update.message.reply_text(NOT_AUTHORIZED)
        return

    logger.info("Photo received from mom")
    await update.message.reply_text("📷 Photo mili! (Photo OCR coming soon — abhi voice ya text use karo.)")


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_shopkeeper(update):
        logger.info(f"Text from shopkeeper: {update.message.text!r}")
        await update.message.reply_text("Orders yahan inline buttons ke through aayenge.")
        return

    if not _is_mom(update):
        await update.message.reply_text(NOT_AUTHORIZED)
        return

    text = update.message.text.strip()
    logger.info(f"Text from mom: {text!r}")

    session = _sessions.get(settings.MOM_ID, {})

    # Confirmation flow
    if session.get("awaiting_confirm"):
        await _handle_confirmation(update, text, session)
        return

    await _process_grocery_text(update, text)


async def _process_grocery_text(update: Update, text: str) -> None:
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

        item_list = format_item_list(items)
        await update.message.reply_text(
            f"Got it! 📋\n\n{item_list}\n\nConfirm karein? *yes* / *no*\n"
            "Aur kuch add karna ho toh likh do (e.g. 'haan, aur do kg gud add karo')",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Grocery processing failed: {e}")
        await update.message.reply_text("Processing mein problem. Try again.")


async def _handle_confirmation(update: Update, text: str, session: dict) -> None:
    text_lower = text.lower().strip()

    # Rejection
    if any(w in text_lower for w in ["no", "nahi", "nope", "cancel"]):
        _sessions.pop(settings.MOM_ID, None)
        await update.message.reply_text("Order cancel ho gaya. Naya order dene ke liye phir bhejo.")
        return

    # Addition to existing list (e.g. "haan, aur do kg gud add karo")
    if any(w in text_lower for w in ["haan", "yes", "ha ", "ha,", "ok", "send", "bhejo"]):
        # Check if there are additions after confirmation word
        additions = text_lower
        for word in ["haan", "yes", "ha", "ok", "aur", "and", "plus", ","]:
            additions = additions.replace(word, " ")
        additions = additions.strip()

        if additions and len(additions) > 3:
            try:
                from src.agent import format_item_list, parse_grocery_text

                new_items = await parse_grocery_text(additions)
                if new_items:
                    existing = session["items"]
                    existing_map = {i.name_en: i for i in existing}
                    for item in new_items:
                        existing_map[item.name_en] = item
                    session["items"] = list(existing_map.values())
                    _sessions[settings.MOM_ID] = session

                    item_list = format_item_list(session["items"])
                    await update.message.reply_text(
                        f"Updated list 📋\n\n{item_list}\n\nShop ko bhej doon? *yes* / *no*",
                        parse_mode="Markdown",
                    )
                    return
            except Exception as e:
                logger.error(f"Addition parsing failed: {e}")

        # Plain confirmation — send to shop
        await _send_order_to_shop(update, session["items"])
    else:
        # Treat as an addition/edit
        try:
            from src.agent import format_item_list, parse_grocery_text

            extra = await parse_grocery_text(text)
            if extra:
                existing_map = {i.name_en: i for i in session["items"]}
                for item in extra:
                    existing_map[item.name_en] = item
                session["items"] = list(existing_map.values())
                _sessions[settings.MOM_ID] = session

                item_list = format_item_list(session["items"])
                await update.message.reply_text(
                    f"Updated list 📋\n\n{item_list}\n\nConfirm? *yes* / *no*",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error(f"Edit handling failed: {e}")
            await update.message.reply_text("Samajh nahi aaya. 'yes' bolein bhejne ke liye ya 'no' cancel ke liye.")


async def _send_order_to_shop(update: Update, items: list) -> None:
    from src.agent import format_item_list

    await update.message.reply_text("Shop ko bhej raha hoon... 🚀")

    try:
        from src.notion_tools import push_order

        order_id = await push_order(items)

        from src.memory import record_order

        await record_order(settings.MOM_ID, items)

        _sessions[settings.MOM_ID] = {"order_id": order_id, "items": items, "awaiting_confirm": False}
        await update.message.reply_text(f"Order #{order_id} sent to shop ✅")

        await _notify_shopkeeper(update._application, order_id, items)

    except Exception as e:
        logger.error(f"Order send failed: {e}")
        await update.message.reply_text("Order bhejne mein problem aayi. Try again.")


async def _notify_shopkeeper(app, order_id: str, items: list) -> None:
    keyboard = []
    for idx, item in enumerate(items):
        row = [
            InlineKeyboardButton("✅", callback_data=f"p:{order_id}:{idx}:ok"),
            InlineKeyboardButton("⚠️", callback_data=f"p:{order_id}:{idx}:partial"),
            InlineKeyboardButton("❌", callback_data=f"p:{order_id}:{idx}:out"),
            InlineKeyboardButton(f"{item.qty}{item.unit} {item.name_en}", callback_data="noop"),
        ]
        keyboard.append(row)

    markup = InlineKeyboardMarkup(keyboard)
    header = f"🛒 New Order #{order_id}\n" + "─" * 20

    try:
        await app.bot.send_message(
            chat_id=settings.SHOPKEEPER_ID,
            text=header,
            reply_markup=markup,
        )
        logger.info(f"Order {order_id} dispatched to shopkeeper")
    except Exception as e:
        logger.error(f"Failed to notify shopkeeper: {e}")


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "noop" or not data.startswith("p:"):
        return

    _, order_id, idx_str, status = data.split(":")
    idx = int(idx_str)

    session = _sessions.get(settings.MOM_ID, {})
    items = session.get("items", [])
    if idx >= len(items):
        logger.warning(f"Callback idx {idx} out of range for order {order_id}")
        return

    item = items[idx]
    status_label = {"ok": "packed", "partial": "partial", "out": "out"}.get(status, status)

    try:
        from src.notion_tools import update_item_status

        await update_item_status(order_id, item.name_en, status_label)
    except Exception as e:
        logger.error(f"Status update failed: {e}")

    # Rebuild keyboard — show only chosen state for this row
    emoji_map = {"ok": "✅ Packed", "partial": "⚠️ Partial", "out": "❌ Out"}
    current_keyboard = query.message.reply_markup.inline_keyboard

    new_keyboard = []
    for row_idx, row in enumerate(current_keyboard):
        if row_idx == idx:
            label = f"{emoji_map[status]} — {item.qty}{item.unit} {item.name_en}"
            new_keyboard.append([InlineKeyboardButton(label, callback_data="noop")])
        else:
            new_keyboard.append(row)

    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_keyboard))
    except Exception as e:
        logger.warning(f"Could not edit message markup: {e}")


def build_app() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", _handle_start))
    app.add_handler(CommandHandler("status", _handle_status))
    app.add_handler(MessageHandler(filters.VOICE, _handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, _handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_handler(CallbackQueryHandler(_handle_callback))

    return app


if __name__ == "__main__":
    import asyncio

    logger.info("Starting MomCart bot...")
    app = build_app()
    app.run_polling(drop_pending_updates=True)
