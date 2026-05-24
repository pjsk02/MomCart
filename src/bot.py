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
# {
#   "cart_id": "ABCD1234",
#   "last_page_ids": ["page-id-1", ...],
#   "wishlist_prompted": false
# }

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


def _get_or_create_cart_id() -> tuple[str, bool]:
    """Return (cart_id, is_new). is_new=True when a fresh CartID was just created."""
    data = _load_cart()
    if data.get("cart_id"):
        return data["cart_id"], False
    from src.notion_tools import _short_id
    cart_id = _short_id()
    _save_cart({"cart_id": cart_id, "last_page_ids": [], "wishlist_prompted": False})
    return cart_id, True


# ── wishlist nudge tracking ────────────────────────────────────────────────────
# how many messages since last nudge (max 3 to honour reply)
_wishlist_nudge_pending: bool = False


# ── last-sent order tracking (in-memory, for /last and /status) ───────────────
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

# natural-language triggers
_SEND_PHRASES = {
    "send to shop", "bhej do", "shop ko bhej do", "send karo",
    "send kar do", "order karo", "order kar do",
}
_WISHLIST_ADD_ALL_PHRASES = {
    "add all from wishlist", "wishlist se sab add karo",
    "wishlist add karo", "wishlist se add karo",
}
_WISHLIST_AFFIRM = {"haan", "yes", "y", "ha", "haa", "haan ji"}


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


def _sub_keyboard(order_id: str, idx: int, cand1: str, cand2: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"1️⃣ {cand1}", callback_data=f"sub:{order_id}:{idx}:1"),
        InlineKeyboardButton(f"2️⃣ {cand2}", callback_data=f"sub:{order_id}:{idx}:2"),
        InlineKeyboardButton("❌ Skip", callback_data=f"sub:{order_id}:{idx}:skip"),
    ]])


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
    _STATUS_EMOJI = {
        "packed": "✅", "partial": "⚠️", "out": "❌",
        "pending": "⏳", "cart": "🛒", "wishlist": "💭",
    }
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


# ── helpers ────────────────────────────────────────────────────────────────────

def _pages_to_items(pages: list) -> List:
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


def _page_category(page: dict) -> str:
    """Extract category metadata from a Notion page's properties if present.
    Falls back to querying Chroma by item name."""
    props = page.get("properties", {}) if isinstance(page, dict) else {}
    name = (props.get("Item", {}).get("title") or [{}])[0].get("text", {}).get("content", "")
    if not name:
        return ""
    try:
        from src.memory import search_pantry
        hits = search_pantry(name, n=1)
        if hits:
            return hits[0].get("category", "")
    except Exception:
        pass
    return ""


async def _get_substitution_candidates(item_name: str, category: str) -> list[str]:
    """Return up to 2 similar items in the same category, excluding item_name itself."""
    try:
        from src.memory import search_pantry_by_category
        import asyncio
        hits = await asyncio.to_thread(search_pantry_by_category, item_name, category, 5)
        candidates = [
            h["name_en"] for h in hits
            if h.get("name_en", "").lower() != item_name.lower()
        ]
        return candidates[:2]
    except Exception as e:
        logger.warning(f"Substitution search failed for {item_name!r}: {e}")
        return []


# ── wishlist helpers ───────────────────────────────────────────────────────────

async def _add_all_from_wishlist(update: Update) -> None:
    """Move all wishlist items into active cart."""
    global _wishlist_nudge_pending
    _wishlist_nudge_pending = False
    try:
        from src.notion_tools import get_wishlist_items, add_to_cart, clear_wishlist
        from src.agent import GroceryItem

        pages = await get_wishlist_items()
        if not pages:
            await update.effective_message.reply_text("Wishlist khaali hai.")
            return

        items = _pages_to_items(pages)
        if not items:
            await update.effective_message.reply_text("Wishlist khaali hai.")
            return

        cart_id, _ = _get_or_create_cart_id()
        new_page_ids = await add_to_cart(items, cart_id)

        # update last_page_ids for undo
        cart_data = _load_cart()
        cart_data["last_page_ids"] = new_page_ids
        _save_cart(cart_data)

        # archive wishlist rows
        await clear_wishlist()

        n = len(items)
        await update.effective_message.reply_text(
            f"{n} item{'s' if n != 1 else ''} wishlist se cart mein add ho gaye / "
            f"{n} item{'s' if n != 1 else ''} added from wishlist to cart."
        )
    except Exception as e:
        logger.error(f"add_all_from_wishlist failed: {e}")
        await update.effective_message.reply_text("Wishlist se add karne mein problem. Try again.")


async def _maybe_nudge_wishlist(update: Update) -> None:
    """After the first add on a fresh cart, check wishlist and nudge if non-empty."""
    global _wishlist_nudge_pending
    try:
        from src.notion_tools import get_wishlist_items
        pages = await get_wishlist_items()
        if not pages:
            return

        items = _pages_to_items(pages)
        if not items:
            return

        names = [i.name_en for i in items]
        preview = ", ".join(names[:3]) + ("..." if len(names) > 3 else "")
        n = len(items)
        _wishlist_nudge_pending = True

        # mark prompted in cart file
        cart_data = _load_cart()
        cart_data["wishlist_prompted"] = True
        _save_cart(cart_data)

        await update.effective_message.reply_text(
            f"Wishlist mein {n} item{'s' if n != 1 else ''} hain pichli baar ke "
            f"({preview}). Add karu?\n"
            "Reply 'haan' / 'yes' / 'wishlist add karo' to add all, "
            "ya '/wishlist' se manage karo."
        )
    except Exception as e:
        logger.warning(f"wishlist nudge failed: {e}")


# ── /start ────────────────────────────────────────────────────────────────────

async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(update)
    if role == "unknown":
        await update.effective_message.reply_text("not authorized")
        return
    if role == "mom":
        await update.effective_message.reply_text(
            "Namaste Mummy! 🙏 Voice note, photo, ya text bhejo — main cart mein "
            "add karta jaunga. Jab ready ho, '/send' ya 'bhej do' bolna.\n\n"
            "/cart - cart dekho\n"
            "/remove <item> - item hatao\n"
            "/undo - last add wapas lo\n"
            "/clear - sab khaali karo\n"
            "/send - shop ko bhej do\n"
            "/last - pichli order dekho\n"
            "/status - shop ki progress dekho\n"
            "/wishlist - dekho missed items / view missed items"
        )
    else:
        await update.effective_message.reply_text(
            "Order aane par yahan dikhega. "
            "Har item ke samne buttons hain — tap karo status update karne ke liye."
        )


# ── /cart ─────────────────────────────────────────────────────────────────────

async def _handle_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return

    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    uid = update.effective_user.id if update.effective_user else None

    # ── DIAG 1 ────────────────────────────────────────────────────────────────
    logger.info(f"[DIAG /cart] active_cart.json raw: {json.dumps(cart_data)}")
    logger.info(f"[DIAG /cart] resolved cart_id={cart_id!r}  user_id={uid}")
    # ─────────────────────────────────────────────────────────────────────────

    if not cart_id:
        await update.effective_message.reply_text(
            "Cart khaali hai. Voice note ya text bhejo items add karne ke liye."
        )
        return

    try:
        from src.notion_tools import get_cart_items
        pages = await get_cart_items(cart_id)

        # ── DIAG 5 (bot side) ─────────────────────────────────────────────────
        logger.info(f"[DIAG /cart] get_cart_items returned {len(pages)} page(s)")
        items_from_pages = _pages_to_items(pages)
        logger.info(f"[DIAG /cart] _pages_to_items produced {len(items_from_pages)} GroceryItem(s)")
        # ──────────────────────────────────────────────────────────────────────

        if not pages:
            await update.effective_message.reply_text(
                "Cart khaali hai. Voice note ya text bhejo items add karne ke liye."
            )
            return
        items = items_from_pages
        table = _cart_table(items, header=f"Cart #{cart_id}")
        await update.effective_message.reply_text(
            f"{table}\n\nSend 'bhej do' ya '/send' jab ready ho, "
            "'/clear' se khaali karo, '/remove &lt;item&gt;' se ek item hatao.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"/cart failed: {e}")
        await update.effective_message.reply_text("Cart fetch karne mein problem. Try again.")


# ── /last ─────────────────────────────────────────────────────────────────────

async def _handle_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return

    order_id = _last_order.get("order_id")
    if not order_id:
        await update.effective_message.reply_text("Koi confirmed order nahi mila abhi tak.")
        return

    try:
        from src.notion_tools import get_order_summary, _get_tools, _require
        from src.config import settings as cfg
        import json as _json

        tools = await _get_tools()
        query = _require(tools, "API-query-data-source")
        result = await query.ainvoke({
            "data_source_id": cfg.NOTION_DATABASE_ID,
            "filter": {
                "and": [
                    {"property": "OrderID", "rich_text": {"equals": order_id}},
                    # exclude wishlist/cart rows from /last view
                    {"property": "Status", "select": {"does_not_equal": "wishlist"}},
                    {"property": "Status", "select": {"does_not_equal": "cart"}},
                ]
            },
        })

        pages = result if isinstance(result, list) else result.get("results", [])
        if pages and isinstance(pages[0], dict) and "text" in pages[0]:
            try:
                data = _json.loads(pages[0]["text"])
                pages = data.get("results", [])
            except _json.JSONDecodeError:
                pass

        if not pages:
            await update.effective_message.reply_text(f"Order #{order_id} ka data Notion mein nahi mila.")
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
        await update.effective_message.reply_text(table, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"/last failed: {e}")
        await update.effective_message.reply_text("Last order fetch karne mein problem. Try again.")


# ── /status ───────────────────────────────────────────────────────────────────

async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return

    order_id = _last_order.get("order_id")
    if not order_id:
        await update.effective_message.reply_text("Koi active order nahi hai abhi.")
        return

    try:
        from src.notion_tools import get_order_summary
        counts = await get_order_summary(order_id)
        emoji = {"packed": "✅", "partial": "⚠️", "out": "❌", "pending": "⏳"}
        lines = [f"Order #{order_id} status:"]
        for s in ("packed", "partial", "out", "pending"):
            lines.append(f"  {emoji[s]} {s}: {counts.get(s, 0)}")
        await update.effective_message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"/status failed: {e}")
        await update.effective_message.reply_text("Status fetch karne mein problem. Try again.")


# ── /send ─────────────────────────────────────────────────────────────────────

async def _do_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    if not cart_id:
        await update.effective_message.reply_text("Cart khaali hai — pehle kuch items add karo.")
        return

    try:
        from src.notion_tools import get_cart_items, send_cart
        pages = await get_cart_items(cart_id)
        if not pages:
            await update.effective_message.reply_text("Cart khaali hai — pehle kuch items add karo.")
            return

        items = _pages_to_items(pages)
        await update.effective_message.reply_text("Order bhej raha hoon... ⏳")

        order_id = await send_cart(cart_id)
        _clear_cart_file()

        global _last_order
        _last_order = {"order_id": order_id, "items": items, "done": {}}

        await update.effective_message.reply_text(f"Order #{order_id} shop ko bhej diya ✅")
        await _notify_shopkeeper(context.application, order_id, items)
    except Exception as e:
        logger.error(f"/send failed: {e}")
        await update.effective_message.reply_text("Order bhejne mein problem. Try again.")


async def _handle_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return
    await _do_send(update, context)


# ── /clear ────────────────────────────────────────────────────────────────────

async def _handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return

    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    if not cart_id:
        await update.effective_message.reply_text("Cart pehle se khaali hai.")
        return

    try:
        from src.notion_tools import clear_cart
        count = await clear_cart(cart_id)
        _clear_cart_file()
        await update.effective_message.reply_text(f"Cart khaali kar diya ({count} items removed).")
    except Exception as e:
        logger.error(f"/clear failed: {e}")
        await update.effective_message.reply_text("Cart clear karne mein problem. Try again.")


# ── /remove <item> ────────────────────────────────────────────────────────────

async def _handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return

    item_arg = " ".join(context.args).strip() if context.args else ""
    if not item_arg:
        await update.effective_message.reply_text("Usage: /remove <item name>  (e.g. /remove potato)")
        return

    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    if not cart_id:
        await update.effective_message.reply_text("Cart khaali hai.")
        return

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
            await update.effective_message.reply_text(f"'{canonical}' cart se hata diya.")
        else:
            await update.effective_message.reply_text(
                f"'{canonical}' cart mein nahi mila. '/cart' se current items dekho."
            )
    except Exception as e:
        logger.error(f"/remove failed: {e}")
        await update.effective_message.reply_text("Remove karne mein problem. Try again.")


# ── /undo ─────────────────────────────────────────────────────────────────────

async def _handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return

    cart_data = _load_cart()
    cart_id = cart_data.get("cart_id")
    last_page_ids: list = cart_data.get("last_page_ids", [])

    if not cart_id or not last_page_ids:
        await update.effective_message.reply_text("Kuch undo karne ke liye nahi hai.")
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

        cart_data["last_page_ids"] = []
        _save_cart(cart_data)

        await update.effective_message.reply_text(f"Last add undo kar diya ({count} items removed).")
    except Exception as e:
        logger.error(f"/undo failed: {e}")
        await update.effective_message.reply_text("Undo karne mein problem. Try again.")


# ── /wishlist ─────────────────────────────────────────────────────────────────

async def _handle_wishlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return

    # sub-commands: /wishlist clear, /wishlist remove <item>
    args = context.args or []
    if args and args[0].lower() == "clear":
        await _handle_wishlist_clear(update, context)
        return
    if args and args[0].lower() == "remove":
        item_arg = " ".join(args[1:]).strip()
        if not item_arg:
            await update.effective_message.reply_text("Usage: /wishlist remove <item>")
            return
        await _do_wishlist_remove(update, item_arg)
        return

    try:
        from src.notion_tools import get_wishlist_items
        pages = await get_wishlist_items()
        if not pages:
            await update.effective_message.reply_text(
                "Wishlist khaali hai. / Wishlist is empty."
            )
            return
        items = _pages_to_items(pages)
        table = _cart_table(items, header="💭 Wishlist")
        await update.effective_message.reply_text(
            f"{table}\n\n"
            "Reply 'add all from wishlist' to put everything back in cart, "
            "ya '/wishlist remove &lt;item&gt;' / '/wishlist clear' se manage karo.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"/wishlist failed: {e}")
        await update.effective_message.reply_text("Wishlist fetch karne mein problem. Try again.")


async def _handle_wishlist_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return
    try:
        from src.notion_tools import clear_wishlist
        count = await clear_wishlist()
        await update.effective_message.reply_text(f"Wishlist khaali kar diya ({count} items removed).")
    except Exception as e:
        logger.error(f"/wishlist_clear failed: {e}")
        await update.effective_message.reply_text("Wishlist clear karne mein problem. Try again.")


async def _do_wishlist_remove(update: Update, item_arg: str) -> None:
    try:
        from src.memory import search_pantry
        hits = search_pantry(item_arg, n=1)
        canonical = hits[0].get("name_en", item_arg) if hits and hits[0]["distance"] < 0.35 else item_arg
    except Exception:
        canonical = item_arg

    try:
        from src.notion_tools import remove_wishlist_item
        found = await remove_wishlist_item(canonical)
        if found:
            await update.effective_message.reply_text(f"'{canonical}' wishlist se hata diya.")
        else:
            await update.effective_message.reply_text(
                f"'{canonical}' wishlist mein nahi mila. '/wishlist' se dekho."
            )
    except Exception as e:
        logger.error(f"wishlist_remove failed: {e}")
        await update.effective_message.reply_text("Remove karne mein problem. Try again.")


async def _handle_wishlist_remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return
    item_arg = " ".join(context.args).strip() if context.args else ""
    if not item_arg:
        await update.effective_message.reply_text("Usage: /wishlist_remove <item>")
        return
    await _do_wishlist_remove(update, item_arg)


# ── core add flow ──────────────────────────────────────────────────────────────

async def _add_items(update: Update, text: str) -> None:
    """Parse text → canonicalize → append to persistent cart."""
    await update.effective_message.reply_text("Cart mein add kar raha hoon... 🛒")
    try:
        from src.agent import format_item_list, parse_grocery_text
        items = await parse_grocery_text(text, user_id=settings.MOM_ID)
        if not items:
            await update.effective_message.reply_text(
                "Koi items samajh nahi aaya. Dobara bolo? (e.g. 'do kilo aata, ek paav haldi')"
            )
            return

        cart_id, is_new = _get_or_create_cart_id()

        from src.notion_tools import add_to_cart
        new_page_ids = await add_to_cart(items, cart_id)

        cart_data = _load_cart()
        cart_data["last_page_ids"] = new_page_ids
        _save_cart(cart_data)

        added_list = format_item_list(items)
        await update.effective_message.reply_text(
            f"Cart mein add ho gaya:\n{added_list}\n\n"
            "'/undo' se wapas lo, ya aur items bhejo."
        )

        # proactive wishlist nudge on first add of a fresh cart
        if is_new and not _load_cart().get("wishlist_prompted", False):
            await _maybe_nudge_wishlist(update)

    except Exception as e:
        logger.error(f"Add items error: {e}")
        await update.effective_message.reply_text("Processing mein problem. Try again.")


# ── message handlers ───────────────────────────────────────────────────────────

async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
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
            await update.effective_message.reply_text("Couldn't hear anything clearly. Try again?")
            return
        await update.effective_message.reply_text(f"Heard: {transcript}")
        await _add_items(update, transcript)
    except Exception as e:
        logger.error(f"Voice handler error: {e}")
        await update.effective_message.reply_text("Something went wrong processing your voice note.")


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _role(update) != "mom":
        await update.effective_message.reply_text("not authorized")
        return
    logger.info("photo from mom")
    await update.effective_message.reply_text("got photo (Photo OCR coming in bonus prompt)")


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _wishlist_nudge_pending
    role = _role(update)
    if role == "unknown":
        await update.effective_message.reply_text("not authorized")
        return
    if role == "shopkeeper":
        await update.effective_message.reply_text("Orders yahan inline buttons ke through aayenge.")
        return

    text = update.message.text.strip()
    tl = text.lower().strip()
    logger.info(f"text from mom: {text!r}")

    # natural-language send
    if tl in _SEND_PHRASES:
        await _do_send(update, context)
        return

    # wishlist add-all trigger (explicit phrase)
    if tl in _WISHLIST_ADD_ALL_PHRASES:
        await _add_all_from_wishlist(update)
        return

    # wishlist nudge affirm (within 3 messages of nudge)
    if _wishlist_nudge_pending and tl in _WISHLIST_AFFIRM:
        await _add_all_from_wishlist(update)
        return

    # clear nudge pending on any other text
    if _wishlist_nudge_pending:
        _wishlist_nudge_pending = False

    await _add_items(update, text)


# ── callback handler ───────────────────────────────────────────────────────────

async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query.data == "noop":
        await query.answer()
        return

    # ── substitution response from mom ────────────────────────────────────────
    if query.data.startswith("sub:"):
        await _handle_sub_callback(query, context)
        return

    if not query.data.startswith("p:"):
        await query.answer()
        return

    # ── shopkeeper status tap ─────────────────────────────────────────────────
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
        await query.answer("Already marked ✓")
        return

    # ── handle flip AWAY from 'out' → delete corresponding wishlist row ───────
    if current_status == "out" and notion_status != "out":
        try:
            from src.notion_tools import delete_wishlist_for_order_item
            await delete_wishlist_for_order_item(order_id, item.name_en)
        except Exception as e:
            logger.warning(f"Could not delete wishlist row on status flip: {e}")

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

    # ── 'out' branch: add to wishlist + send substitution prompt to mom ───────
    if notion_status == "out":
        await _trigger_out_flow(query, context, order_id, idx, item)


async def _trigger_out_flow(
    query, context: ContextTypes.DEFAULT_TYPE, order_id: str, idx: int, item
) -> None:
    """Add item to wishlist and send substitution dialog to mom."""
    # 1. add to wishlist immediately
    try:
        from src.notion_tools import add_to_wishlist
        await add_to_wishlist(item.name_en, item.qty, item.unit, order_id)
        logger.info(f"Wishlist: added {item.name_en!r} from order {order_id}")
    except Exception as e:
        logger.error(f"add_to_wishlist failed for {item.name_en!r}: {e}")

    # 2. find category and substitution candidates
    category = ""
    try:
        from src.memory import search_pantry
        hits = search_pantry(item.name_en, n=1)
        if hits:
            category = hits[0].get("category", "")
    except Exception:
        pass

    if not category:
        logger.info(f"No category for {item.name_en!r} — skipping sub prompt")
        # still notify mom item is out + in wishlist, no sub dialog
        try:
            await context.bot.send_message(
                chat_id=settings.MOM_ID,
                text=f"❌ {item.name_en} nahi mila. Wishlist mein add ho gaya."
            )
        except Exception as e:
            logger.error(f"Failed to notify mom of out item: {e}")
        return

    candidates = await _get_substitution_candidates(item.name_en, category)
    if len(candidates) < 2:
        logger.info(f"Not enough sub candidates for {item.name_en!r} — skipping sub prompt")
        try:
            await context.bot.send_message(
                chat_id=settings.MOM_ID,
                text=f"❌ {item.name_en} nahi mila. Wishlist mein add ho gaya."
            )
        except Exception as e:
            logger.error(f"Failed to notify mom: {e}")
        return

    cand1, cand2 = candidates[0], candidates[1]
    # store candidates in context for callback resolution
    context.bot_data[f"sub:{order_id}:{idx}"] = {
        "item": item,
        "cand1": cand1,
        "cand2": cand2,
        "order_id": order_id,
        "idx": idx,
    }

    text = (
        f"❌ {item.name_en} nahi mila / Out of stock.\n"
        f"Substitute lena hai?\n\n"
        f"1️⃣ {cand1}\n"
        f"2️⃣ {cand2}\n"
    )
    markup = _sub_keyboard(order_id, idx, cand1, cand2)
    try:
        await context.bot.send_message(
            chat_id=settings.MOM_ID,
            text=text,
            reply_markup=markup,
        )
    except Exception as e:
        logger.error(f"Failed to send substitution prompt to mom: {e}")


async def _handle_sub_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle mom's response to substitution dialog."""
    try:
        _, order_id, idx_str, choice = query.data.split(":")
        idx = int(idx_str)
    except ValueError:
        await query.answer()
        return

    sub_key = f"sub:{order_id}:{idx}"
    sub_data = context.bot_data.get(sub_key)
    if not sub_data:
        await query.answer("Prompt expired or not found.")
        try:
            await query.edit_message_text("⏳ Prompt expired.")
        except Exception:
            pass
        return

    item = sub_data["item"]
    cand1 = sub_data["cand1"]
    cand2 = sub_data["cand2"]

    await query.answer()

    if choice == "skip":
        try:
            await query.edit_message_text(
                f"Skip kiya. {item.name_en} wishlist mein add ho gaya."
            )
        except Exception:
            pass
        context.bot_data.pop(sub_key, None)
        return

    chosen = cand1 if choice == "1" else cand2

    # add substitute to active cart
    try:
        from src.agent import GroceryItem
        from src.notion_tools import add_to_cart

        sub_item = GroceryItem(name_en=chosen, qty=item.qty, unit=item.unit)
        cart_id, _ = _get_or_create_cart_id()
        new_page_ids = await add_to_cart([sub_item], cart_id)

        # track for undo
        cart_data = _load_cart()
        cart_data["last_page_ids"] = new_page_ids
        _save_cart(cart_data)

        try:
            await query.edit_message_text(
                f"✅ {chosen} cart mein add ho gaya in place of {item.name_en}."
            )
        except Exception:
            pass
        logger.info(f"Substitution: {item.name_en!r} -> {chosen!r} added to cart")
    except Exception as e:
        logger.error(f"Substitution add failed: {e}")
        try:
            await query.edit_message_text(
                f"Cart mein add karne mein problem. Try '/wishlist' se manually add karo."
            )
        except Exception:
            pass

    context.bot_data.pop(sub_key, None)


# ── app builder ────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",           "Start / help"),
        BotCommand("cart",            "Show current cart"),
        BotCommand("send",            "Send cart to shop"),
        BotCommand("clear",           "Empty the cart"),
        BotCommand("remove",          "Remove one item from cart"),
        BotCommand("undo",            "Undo last add"),
        BotCommand("last",            "Show last sent order"),
        BotCommand("status",          "Check shopkeeper's progress"),
        BotCommand("wishlist",        "View missed / out-of-stock items"),
        BotCommand("wishlist_remove", "Remove item from wishlist"),
    ])
    logger.info("Bot commands registered with Telegram")


def build_app() -> Application:
    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",           _handle_start))
    app.add_handler(CommandHandler("cart",            _handle_cart))
    app.add_handler(CommandHandler("send",            _handle_send))
    app.add_handler(CommandHandler("clear",           _handle_clear))
    app.add_handler(CommandHandler("remove",          _handle_remove))
    app.add_handler(CommandHandler("undo",            _handle_undo))
    app.add_handler(CommandHandler("last",            _handle_last))
    app.add_handler(CommandHandler("status",          _handle_status))
    app.add_handler(CommandHandler("wishlist",        _handle_wishlist))
    app.add_handler(CommandHandler("wishlist_remove", _handle_wishlist_remove_cmd))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    app.add_handler(MessageHandler(filters.VOICE,              _handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO,              _handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    return app


if __name__ == "__main__":
    logger.info("Starting MomCart bot...")
    build_app().run_polling(drop_pending_updates=True)
