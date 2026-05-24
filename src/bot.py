"""MomCart Telegram bot — entrypoint."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List

from loguru import logger
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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


# ── confirm vocabulary ────────────────────────────────────────────────────────

_AFFIRM = {
    "yes", "y", "yep", "yeah", "yup",
    "haan", "haa", "ji", "ok", "okay",
    "sure", "theek hai", "thik hai",
    "send", "send it", "confirm",
    "haan bhej do", "bhej do",
}
_CANCEL = {"no", "n", "nahi", "cancel", "ruk"}


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


async def _parse_as_correction(update: Update, text: str, old_session: dict) -> None:
    """Re-parse a correction; keep the old draft alive if the new parse yields nothing."""
    await update.message.reply_text("Theek hai, naya list bana raha hoon...")
    try:
        from src.agent import format_item_list, parse_grocery_text
        items = await parse_grocery_text(text, user_id=settings.MOM_ID)
        if not items:
            # restore old draft — don't lose it on a bad correction
            _sessions[settings.MOM_ID] = old_session
            from src.agent import format_item_list as fmt
            await update.message.reply_text(
                "Samajh nahi aaya. Original list still pending:\n"
                f"{fmt(old_session['items'])}\n\n"
                "Reply 'yes' to confirm or send new items."
            )
            return
        _sessions[settings.MOM_ID] = {"items": items, "awaiting_confirm": True}
        await update.message.reply_text(
            f"Got it:\n{format_item_list(items)}\n\nConfirm? yes/no"
        )
    except Exception as e:
        logger.error(f"Correction parse error: {e}")
        _sessions[settings.MOM_ID] = old_session
        await update.message.reply_text("Processing mein problem. Original list still pending — reply 'yes' to confirm.")


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
        tl = text.lower().strip()
        if tl in _AFFIRM:
            await _confirm_and_push(update, context, session)
        elif tl in _CANCEL:
            _sessions.pop(settings.MOM_ID, None)
            await update.message.reply_text("Order cancel ho gaya.")
        else:
            # correction — discard draft only if new parse succeeds
            await _parse_as_correction(update, text, old_session=session)
        return

    await _parse_and_reply(update, text)


# ── cart table formatter ──────────────────────────────────────────────────────

def _cart_table(items: List, header: str = "MomCart Order",
                statuses: dict | None = None) -> str:
    """Return an HTML <pre> block with right-aligned qty column.

    statuses: optional {idx: notion_status_str} for /last command.
    """
    _STATUS_EMOJI = {"packed": "✅", "partial": "⚠️", "out": "❌", "pending": "⏳"}
    sep = "─" * 26
    name_width = max((len(i.name_en) for i in items), default=10)
    name_width = max(name_width, 12)

    rows = [header, sep]
    for idx, item in enumerate(items):
        qty = int(item.qty) if item.qty == int(item.qty) else item.qty
        qty_str = f"{qty} {item.unit}"
        name_col = item.name_en[:name_width].ljust(name_width)
        if statuses:
            s = statuses.get(item.name_en, "pending")
            emoji = _STATUS_EMOJI.get(s, "⏳")
            rows.append(f"{emoji} {name_col}  {qty_str}")
        else:
            rows.append(f"{name_col}  {qty_str}")
    rows.append(sep)
    rows.append(f"Total: {len(items)} item{'s' if len(items) != 1 else ''}")
    inner = "\n".join(rows)
    return f"<pre>{inner}</pre>"


# ── /cart and /last handlers ──────────────────────────────────────────────────

async def _handle_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    session = _sessions.get(settings.MOM_ID, {})
    items = session.get("items") if session.get("awaiting_confirm") else None

    if not items:
        await update.message.reply_text("Cart khaali hai. Voice note ya text bhejo.")
        return

    table = _cart_table(items)
    await update.message.reply_text(
        f"{table}\nReply 'yes' to send to shop, 'no' to cancel, or send new items to correct.",
        parse_mode=ParseMode.HTML,
    )


async def _handle_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    session = _sessions.get(settings.MOM_ID, {})
    order_id = session.get("order_id")

    if not order_id:
        await update.message.reply_text("Koi confirmed order nahi mila abhi tak.")
        return

    try:
        from src.notion_tools import get_order_summary, _get_tools, _require
        from src.config import settings as cfg
        import json

        tools = await _get_tools()
        query = _require(tools, "API-query-data-source")
        result = await query.ainvoke({
            "data_source_id": cfg.NOTION_DATABASE_ID,
            "filter": {"property": "OrderID", "rich_text": {"equals": order_id}},
        })

        pages = result if isinstance(result, list) else result.get("results", [])
        # MCP returns text blocks — unwrap if needed
        if pages and isinstance(pages[0], dict) and "text" in pages[0]:
            try:
                data = json.loads(pages[0]["text"])
                pages = data.get("results", [])
            except json.JSONDecodeError:
                pass

        if not pages:
            await update.message.reply_text(f"Order #{order_id} ka data Notion mein nahi mila.")
            return

        # build item list + status map from Notion rows
        from src.agent import GroceryItem
        items_out: List[GroceryItem] = []
        statuses: dict = {}
        for page in pages:
            props = page.get("properties", {}) if isinstance(page, dict) else {}
            name = (props.get("Item", {}).get("title") or [{}])[0].get("text", {}).get("content", "?")
            qty = props.get("Qty", {}).get("number") or 0
            unit = (props.get("Unit", {}).get("select") or {}).get("name", "pcs")
            status = (props.get("Status", {}).get("select") or {}).get("name", "pending")
            try:
                items_out.append(GroceryItem(name_en=name, qty=float(qty), unit=unit))
                statuses[name] = status
            except Exception:
                pass

        counts = await get_order_summary(order_id)
        header = (
            f"Last order #{order_id} — "
            f"{counts.get('packed',0)} packed, "
            f"{counts.get('partial',0)} partial, "
            f"{counts.get('out',0)} out, "
            f"{counts.get('pending',0)} pending"
        )
        table = _cart_table(items_out, header=header, statuses=statuses)
        await update.message.reply_text(table, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"/last failed: {e}")
        await update.message.reply_text("Last order fetch karne mein problem. Try again.")


# ── callback handler ───────────────────────────────────────────────────────────

async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query.data == "noop":
        await query.answer()
        return

    if not query.data.startswith("p:"):
        await query.answer()
        return

    # 1. parse callback_data before acking so we can give meaningful feedback
    try:
        _, order_id, idx_str, status_key = query.data.split(":")
        idx = int(idx_str)
    except ValueError:
        logger.warning(f"Bad callback_data: {query.data!r}")
        await query.answer()
        return

    session = _sessions.get(settings.MOM_ID, {})
    items = session.get("items", [])
    if not items or idx >= len(items):
        await query.answer("Order data not found", show_alert=True)
        return

    item = items[idx]
    notion_status = _STATUS_NOTION[status_key]

    # 2. idempotency check — query current status before writing
    try:
        from src.notion_tools import get_item_status, update_item_status
        current_status = await get_item_status(order_id, item.name_en)
    except Exception as e:
        logger.error(f"Notion status check failed: {e}")
        await query.answer("Notion check failed — check logs", show_alert=True)
        return

    if current_status == notion_status:
        logger.debug(f"no-op: {item.name_en!r} already {notion_status}")
        await query.answer("Already marked ✓")
        return

    # 3. update Notion
    try:
        await update_item_status(order_id, item.name_en, notion_status)
    except Exception as e:
        logger.error(f"Notion status update failed: {e}")
        await query.answer("Notion update failed — check logs", show_alert=True)
        return

    await query.answer()

    # track locally so keyboard rebuild is consistent
    done = session.setdefault("done", {})
    done[idx] = status_key
    _sessions[settings.MOM_ID] = session

    # 4. rebuild keyboard — resolved row collapses to single label button
    new_markup = _order_keyboard(order_id, items, done=done)
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception as e:
        import telegram
        if isinstance(e, telegram.error.BadRequest) and "not modified" in str(e).lower():
            logger.debug(f"Markup already up-to-date (no-op edit): {e}")
        else:
            logger.warning(f"Could not edit markup: {e}")


# ── app builder ────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",  "Start over"),
        BotCommand("cart",   "Show pending cart"),
        BotCommand("last",   "Show last sent order"),
        BotCommand("status", "Check shopkeeper's progress"),
    ])
    logger.info("Bot commands registered with Telegram")


def build_app() -> Application:
    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",  _handle_start))
    app.add_handler(CommandHandler("cart",   _handle_cart))
    app.add_handler(CommandHandler("last",   _handle_last))
    app.add_handler(CommandHandler("status", _handle_status))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    app.add_handler(MessageHandler(filters.VOICE,              _handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO,              _handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    return app


if __name__ == "__main__":
    logger.info("Starting MomCart bot...")
    build_app().run_polling(drop_pending_updates=True)
