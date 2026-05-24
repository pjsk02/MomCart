"""MomCart Telegram bot — entrypoint."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List

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

# per-user session: MOM_ID -> {items, order_id, awaiting_confirm}
_sessions: dict = {}

# ── label sets ────────────────────────────────────────────────────────────────
_STATUS_LABEL = {
    "ok":      "✅ Packed",
    "partial": "⚠️ Partial",
    "out":     "❌ Out",
}
_STATUS_NOTION = {          # callback status → Notion Status select value
    "ok":      "packed",
    "partial": "partial",
    "out":     "out",
}


# ── auth helper ───────────────────────────────────────────────────────────────

def _role(update: Update) -> str:
    uid = update.effective_user.id if update.effective_user else None
    if uid == settings.MOM_ID:
        return "mom"
    if uid == settings.SHOPKEEPER_ID:
        return "shopkeeper"
    return "unknown"


# ── keyboard builder ──────────────────────────────────────────────────────────

def _order_keyboard(order_id: str, items: List, done: dict | None = None) -> InlineKeyboardMarkup:
    """Build one row per item. done={idx: status_key} marks resolved rows."""
    done = done or {}
    rows = []
    for idx, item in enumerate(items):
        qty = int(item.qty) if item.qty == int(item.qty) else item.qty
        label = f"{qty} {item.unit} {item.name_en}"
        if idx in done:
            chosen = done[idx]
            rows.append([InlineKeyboardButton(
                f"{_STATUS_LABEL[chosen]} — {label}", callback_data="noop"
            )])
        else:
            rows.append([
                InlineKeyboardButton("✅", callback_data=f"p:{order_id}:{idx}:ok"),
                InlineKeyboardButton("⚠️", callback_data=f"p:{order_id}:{idx}:partial"),
                InlineKeyboardButton("❌", callback_data=f"p:{order_id}:{idx}:out"),
                InlineKeyboardButton(label,  callback_data="noop"),
            ])
    return InlineKeyboardMarkup(rows)


# ── shopkeeper notify ──────────────────────────────────────────────────────────

async def _notify_shopkeeper(app: Application, order_id: str, items: List) -> None:
    header = f"🛒 New Order #{order_id}\n{'─' * 22}\nTap a status for each item:"
    markup = _order_keyboard(order_id, items)
    try:
        await app.bot.send_message(
            chat_id=settings.SHOPKEEPER_ID,
            text=header,
            reply_markup=markup,
        )
        logger.info(f"Order {order_id} dispatched to shopkeeper")
    except Exception as e:
        logger.error(f"Failed to notify shopkeeper: {e}")


# ── commands ──────────────────────────────────────────────────────────────────

async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    if role == "mom":
        await update.message.reply_text(
            "Namaste Mummy! 🙏 Voice note bhejo ya photo, main list bana dunga. "
            "Confirm karne ke baad shop ko bhej dunga!"
        )
    else:
        await update.message.reply_text(
            "Order aane par yahan dikhega. "
            "Har item ke samne buttons hain — tap karo status update karne ke liye."
        )


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    session = _sessions.get(settings.MOM_ID, {})
    order_id = session.get("order_id")
    if not order_id:
        await update.message.reply_text("Koi active order nahi hai abhi.")
        return

    try:
        from src.notion_tools import get_order_summary
        counts = await get_order_summary(order_id)
        emoji = {"packed": "✅", "partial": "⚠️", "out": "❌", "pending": "⏳"}
        lines = [f"Order #{order_id} status:"]
        for s in ("packed", "partial", "out", "pending"):
            lines.append(f"  {emoji[s]} {s}: {counts.get(s, 0)}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"/status failed: {e}")
        await update.message.reply_text("Status fetch karne mein problem. Try again.")


# ── core flow helpers ──────────────────────────────────────────────────────────

async def _parse_and_reply(update: Update, text: str) -> None:
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
        await update.message.reply_text(
            f"Got it:\n{format_item_list(items)}\n\nConfirm? yes/no"
        )
    except Exception as e:
        logger.error(f"Parse error: {e}")
        await update.message.reply_text("Processing mein problem. Try again.")


async def _confirm_and_push(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict) -> None:
    items = session["items"]
    await update.message.reply_text("Notion mein save kar raha hoon... ⏳")
    try:
        from src.notion_tools import push_order
        order_id = await push_order(items)
        _sessions[settings.MOM_ID] = {
            "order_id": order_id,
            "items": items,
            "awaiting_confirm": False,
            "done": {},          # idx -> status_key for shopkeeper taps
        }
        await update.message.reply_text(f"Order #{order_id} sent to shop ✅")
        await _notify_shopkeeper(context.application, order_id, items)
    except Exception as e:
        logger.error(f"push_order failed: {e}")
        await update.message.reply_text("Notion mein save nahi hua. Check logs aur try again.")


# ── message handlers ───────────────────────────────────────────────────────────

async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return
    logger.info(f"voice from mom ({update.message.voice.duration}s)")
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
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return
    logger.info("photo from mom")
    await update.message.reply_text("got photo (Photo OCR coming in bonus prompt)")


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    if role == "shopkeeper":
        await update.message.reply_text("Orders yahan inline buttons ke through aayenge.")
        return

    text = update.message.text.strip()
    logger.info(f"text from mom: {text!r}")
    session = _sessions.get(settings.MOM_ID, {})

    if session.get("awaiting_confirm"):
        tl = text.lower()
        if any(w in tl for w in ["yes", "haan", "ha", "ok", "send", "bhejo"]):
            await _confirm_and_push(update, context, session)
        elif any(w in tl for w in ["no", "nahi", "cancel"]):
            _sessions.pop(settings.MOM_ID, None)
            await update.message.reply_text("Order cancel ho gaya.")
        else:
            await _parse_and_reply(update, text)
        return

    await _parse_and_reply(update, text)


# ── callback handler ───────────────────────────────────────────────────────────

async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()                    # 1. ack immediately

    if query.data == "noop":
        return

    if not query.data.startswith("p:"):
        return

    # 2. parse callback_data
    try:
        _, order_id, idx_str, status_key = query.data.split(":")
        idx = int(idx_str)
    except ValueError:
        logger.warning(f"Bad callback_data: {query.data!r}")
        return

    session = _sessions.get(settings.MOM_ID, {})
    items = session.get("items", [])
    if not items or idx >= len(items):
        await query.answer("Order data not found", show_alert=True)
        return

    item = items[idx]
    notion_status = _STATUS_NOTION[status_key]

    # 3. update Notion
    try:
        from src.notion_tools import update_item_status
        await update_item_status(order_id, item.name_en, notion_status)
    except Exception as e:
        logger.error(f"Notion status update failed: {e}")
        await query.answer("Notion update failed — check logs", show_alert=True)
        return

    # track locally so keyboard rebuild is consistent
    done = session.setdefault("done", {})
    done[idx] = status_key
    _sessions[settings.MOM_ID] = session

    # 4. rebuild keyboard — resolved row collapses to single label button
    new_markup = _order_keyboard(order_id, items, done=done)
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception as e:
        logger.warning(f"Could not edit markup: {e}")


# ── app builder ────────────────────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  _handle_start))
    app.add_handler(CommandHandler("status", _handle_status))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    app.add_handler(MessageHandler(filters.VOICE,              _handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO,              _handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    return app


if __name__ == "__main__":
    logger.info("Starting MomCart bot...")
    build_app().run_polling(drop_pending_updates=True)
