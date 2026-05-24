"""MomCart Telegram bot — entrypoint."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import List, Optional

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

# ── active cart persistence ────────────────────────────────────────────────────
# data/active_cart.json schema:
# { "cart_id": "ABCD1234", "last_page_ids": ["page-id-1", ...] }

_CART_FILE = Path("data/active_cart.json")


def _load_cart() -> dict:
    if _CART_FILE.exists():
        try:
            return json.loads(_CART_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cart(data: dict) -> None:
    _CART_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CART_FILE.write_text(json.dumps(data), encoding="utf-8")


def _clear_cart_file() -> None:
    if _CART_FILE.exists():
        _CART_FILE.unlink()


def _get_or_create_cart_id() -> str:
    data = _load_cart()
    if data.get("cart_id"):
        return data["cart_id"]
    from src.notion_tools import _short_id
    cart_id = _short_id()
    _save_cart({"cart_id": cart_id, "last_page_ids": []})
    return cart_id


# ── last-sent order tracking (in-memory, for /last and /status) ───────────────
# { "order_id": str, "items": List[GroceryItem], "done": {idx: status_key} }
_last_order: dict = {}

# ── label sets ────────────────────────────────────────────────────────────────
_STATUS_LABEL = {
    "ok":      "✅ Packed",
    "partial": "⚠️ Partial",
    "out":     "❌ Out",
}
_STATUS_NOTION = {
    "ok":      "packed",
    "partial": "partial",
    "out":     "out",
}

# natural-language triggers for /send
_SEND_PHRASES = {
    "send to shop", "bhej do", "shop ko bhej do", "send karo",
    "send kar do", "order karo", "order kar do",
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


# ── cart table formatter ──────────────────────────────────────────────────────

def _cart_table(items: List, header: str = "MomCart Cart",
                statuses: dict | None = None) -> str:
    """Return an HTML <pre> block with right-aligned qty column."""
    _STATUS_EMOJI = {"packed": "✅", "partial": "⚠️", "out": "❌", "pending": "⏳", "cart": "🛒"}
    sep = "─" * 26
    name_width = max((len(i.name_en) for i in items), default=10)
    name_width = max(name_width, 12)

    rows = [header, sep]
    for item in items:
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


# ── /start ────────────────────────────────────────────────────────────────────

async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.message.reply_text("not authorized")
        return
    if role == "mom":
        await update.message.reply_text(
            "Namaste Mummy! 🙏 Voice note, photo, ya text bhejo — main cart mein "
            "add karta jaunga. Jab ready ho, '/send' ya 'bhej do' bolna.\n\n"
            "/cart - cart dekho\n"
            "/remove <item> - item hatao\n"
            "/undo - last add wapas lo\n"
            "/clear - sab khaali karo\n"
            "/send - shop ko bhej do\n"
            "/last - pichli order dekho\n"
            "/status - shop ki progress dekho"
        )
    else:
        await update.message.reply_text(
            "Order aane par yahan dikhega. "
            "Har item ke samne buttons hain — tap karo status update karne ke liye."
        )


# ── /cart ─────────────────────────────────────────────────────────────────────

async def _handle_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    if not cart_id:
        await update.message.reply_text(
            "Cart khaali hai. Voice note ya text bhejo items add karne ke liye."
        )
        return

    try:
        from src.notion_tools import get_cart_items
        from src.agent import GroceryItem
        pages = await get_cart_items(cart_id)
        if not pages:
            await update.message.reply_text(
                "Cart khaali hai. Voice note ya text bhejo items add karne ke liye."
            )
            return
        items = _pages_to_items(pages)
        table = _cart_table(items, header=f"Cart #{cart_id}")
        await update.message.reply_text(
            f"{table}\n\nSend 'bhej do' ya '/send' jab ready ho, "
            "'/clear' se khaali karo, '/remove &lt;item&gt;' se ek item hatao.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"/cart failed: {e}")
        await update.message.reply_text("Cart fetch karne mein problem. Try again.")


# ── /last ─────────────────────────────────────────────────────────────────────

async def _handle_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    order_id = _last_order.get("order_id")
    if not order_id:
        await update.message.reply_text("Koi confirmed order nahi mila abhi tak.")
        return

    try:
        from src.notion_tools import get_order_summary, _get_tools, _require
        from src.config import settings as cfg
        import json as _json

        tools = await _get_tools()
        query = _require(tools, "API-query-data-source")
        result = await query.ainvoke({
            "data_source_id": cfg.NOTION_DATABASE_ID,
            "filter": {"property": "OrderID", "rich_text": {"equals": order_id}},
        })

        pages = result if isinstance(result, list) else result.get("results", [])
        if pages and isinstance(pages[0], dict) and "text" in pages[0]:
            try:
                data = _json.loads(pages[0]["text"])
                pages = data.get("results", [])
            except _json.JSONDecodeError:
                pass

        if not pages:
            await update.message.reply_text(f"Order #{order_id} ka data Notion mein nahi mila.")
            return

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
            f"{counts.get('packed', 0)} packed, "
            f"{counts.get('partial', 0)} partial, "
            f"{counts.get('out', 0)} out, "
            f"{counts.get('pending', 0)} pending"
        )
        table = _cart_table(items_out, header=header, statuses=statuses)
        await update.message.reply_text(table, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"/last failed: {e}")
        await update.message.reply_text("Last order fetch karne mein problem. Try again.")


# ── /status ───────────────────────────────────────────────────────────────────

async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    order_id = _last_order.get("order_id")
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


# ── /send ─────────────────────────────────────────────────────────────────────

async def _do_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Core logic for /send command and natural-language send triggers."""
    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    if not cart_id:
        await update.message.reply_text("Cart khaali hai — pehle kuch items add karo.")
        return

    try:
        from src.notion_tools import get_cart_items, send_cart
        pages = await get_cart_items(cart_id)
        if not pages:
            await update.message.reply_text("Cart khaali hai — pehle kuch items add karo.")
            return

        items = _pages_to_items(pages)
        await update.message.reply_text("Order bhej raha hoon... ⏳")

        order_id = await send_cart(cart_id)

        # clear cart file so next add starts fresh
        _clear_cart_file()

        # store for /last and /status
        global _last_order
        _last_order = {"order_id": order_id, "items": items, "done": {}}

        await update.message.reply_text(f"Order #{order_id} shop ko bhej diya ✅")
        await _notify_shopkeeper(context.application, order_id, items)
    except Exception as e:
        logger.error(f"/send failed: {e}")
        await update.message.reply_text("Order bhejne mein problem. Try again.")


async def _handle_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return
    await _do_send(update, context)


# ── /clear ────────────────────────────────────────────────────────────────────

async def _handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    if not cart_id:
        await update.message.reply_text("Cart pehle se khaali hai.")
        return

    try:
        from src.notion_tools import clear_cart
        count = await clear_cart(cart_id)
        _clear_cart_file()
        await update.message.reply_text(f"Cart khaali kar diya ({count} items removed).")
    except Exception as e:
        logger.error(f"/clear failed: {e}")
        await update.message.reply_text("Cart clear karne mein problem. Try again.")


# ── /remove <item> ────────────────────────────────────────────────────────────

async def _handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    item_arg = " ".join(context.args).strip() if context.args else ""
    if not item_arg:
        await update.message.reply_text("Usage: /remove <item name>  (e.g. /remove potato)")
        return

    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    if not cart_id:
        await update.message.reply_text("Cart khaali hai.")
        return

    # canonicalize via pantry so "potatoes" -> "potato"
    try:
        from src.memory import search_pantry
        hits = search_pantry(item_arg, n=1)
        canonical = hits[0].get("name_en", item_arg) if hits and hits[0]["distance"] < 0.35 else item_arg
    except Exception:
        canonical = item_arg

    try:
        from src.notion_tools import remove_cart_item
        found = await remove_cart_item(cart_id, canonical)
        if found:
            await update.message.reply_text(f"'{canonical}' cart se hata diya.")
        else:
            await update.message.reply_text(
                f"'{canonical}' cart mein nahi mila. "
                "'/cart' se current items dekho."
            )
    except Exception as e:
        logger.error(f"/remove failed: {e}")
        await update.message.reply_text("Remove karne mein problem. Try again.")


# ── /undo ─────────────────────────────────────────────────────────────────────

async def _handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.message.reply_text("not authorized")
        return

    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    last_page_ids: list = cart_data.get("last_page_ids", [])

    if not cart_id or not last_page_ids:
        await update.message.reply_text("Kuch undo karne ke liye nahi hai.")
        return

    try:
        from src.notion_tools import _get_tools, _require
        tools = await _get_tools()
        patch = _require(tools, "API-patch-page")

        count = 0
        for page_id in last_page_ids:
            try:
                await patch.ainvoke({"page_id": page_id, "archived": True})
                count += 1
            except Exception as e:
                logger.error(f"Undo archive failed for {page_id}: {e}")

        # clear last_page_ids but keep cart_id (other items remain)
        cart_data["last_page_ids"] = []
        _save_cart(cart_data)

        await update.message.reply_text(f"Last add undo kar diya ({count} items removed).")
    except Exception as e:
        logger.error(f"/undo failed: {e}")
        await update.message.reply_text("Undo karne mein problem. Try again.")


# ── core add flow ──────────────────────────────────────────────────────────────

async def _add_items(update: Update, text: str) -> None:
    """Parse text → canonicalize → append to persistent cart."""
    await update.message.reply_text("Cart mein add kar raha hoon... 🛒")
    try:
        from src.agent import format_item_list, parse_grocery_text
        items = await parse_grocery_text(text, user_id=settings.MOM_ID)
        if not items:
            await update.message.reply_text(
                "Koi items samajh nahi aaya. Dobara bolo? (e.g. 'do kilo aata, ek paav haldi')"
            )
            return

        cart_id = _get_or_create_cart_id()

        from src.notion_tools import add_to_cart
        new_page_ids = await add_to_cart(items, cart_id)

        # persist last-added IDs for /undo
        cart_data = _load_cart()
        cart_data["last_page_ids"] = new_page_ids
        _save_cart(cart_data)

        added_list = format_item_list(items)
        await update.message.reply_text(
            f"Cart mein add ho gaya:\n{added_list}\n\n"
            "'/undo' se wapas lo, ya aur items bhejo."
        )
    except Exception as e:
        logger.error(f"Add items error: {e}")
        await update.message.reply_text("Processing mein problem. Try again.")


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
        await _add_items(update, transcript)
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

    # check for natural-language send triggers
    if text.lower() in _SEND_PHRASES:
        await _do_send(update, context)
        return

    await _add_items(update, text)


# ── callback handler ───────────────────────────────────────────────────────────

async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query.data == "noop":
        await query.answer()
        return

    if not query.data.startswith("p:"):
        await query.answer()
        return

    try:
        _, order_id, idx_str, status_key = query.data.split(":")
        idx = int(idx_str)
    except ValueError:
        logger.warning(f"Bad callback_data: {query.data!r}")
        await query.answer()
        return

    items = _last_order.get("items", [])
    if not items or idx >= len(items):
        await query.answer("Order data not found", show_alert=True)
        return

    item = items[idx]
    notion_status = _STATUS_NOTION[status_key]

    # idempotency check
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

    try:
        await update_item_status(order_id, item.name_en, notion_status)
    except Exception as e:
        logger.error(f"Notion status update failed: {e}")
        await query.answer("Notion update failed — check logs", show_alert=True)
        return

    await query.answer()

    done = _last_order.setdefault("done", {})
    done[idx] = status_key

    new_markup = _order_keyboard(order_id, items, done=done)
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception as e:
        import telegram
        if isinstance(e, telegram.error.BadRequest) and "not modified" in str(e).lower():
            logger.debug(f"Markup already up-to-date: {e}")
        else:
            logger.warning(f"Could not edit markup: {e}")


# ── helpers ────────────────────────────────────────────────────────────────────

def _pages_to_items(pages: list) -> List:
    """Convert Notion page dicts to GroceryItem list."""
    from src.agent import GroceryItem
    items = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        props = page.get("properties", {})
        name = (props.get("Item", {}).get("title") or [{}])[0].get("text", {}).get("content", "?")
        qty = props.get("Qty", {}).get("number") or 1
        unit = (props.get("Unit", {}).get("select") or {}).get("name", "pcs")
        try:
            items.append(GroceryItem(name_en=name, qty=float(qty), unit=unit))
        except Exception:
            pass
    return items


# ── app builder ────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",  "Start / help"),
        BotCommand("cart",   "Show current cart"),
        BotCommand("send",   "Send cart to shop"),
        BotCommand("clear",  "Empty the cart"),
        BotCommand("remove", "Remove one item from cart"),
        BotCommand("undo",   "Undo last add"),
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
    app.add_handler(CommandHandler("send",   _handle_send))
    app.add_handler(CommandHandler("clear",  _handle_clear))
    app.add_handler(CommandHandler("remove", _handle_remove))
    app.add_handler(CommandHandler("undo",   _handle_undo))
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
