from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, List

from loguru import logger

if TYPE_CHECKING:
    from src.agent import GroceryItem

_tools: dict | None = None
_data_source_id: str | None = None


def _parse_mcp_text_block(result) -> dict:
    """Extract the JSON payload from an MCP text-block response."""
    if isinstance(result, list):
        for block in result:
            if isinstance(block, dict) and "text" in block:
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    pass
    elif isinstance(result, dict):
        return result
    return {}


async def _get_data_source_id() -> str:
    """Resolve and cache the data_source_id needed by API-query-data-source.

    Tries API-retrieve-a-data-source first (passing the database UUID directly),
    then falls back to API-retrieve-a-database and walking its response.
    Logs full responses so mismatches are immediately visible.
    """
    global _data_source_id
    if _data_source_id is not None:
        return _data_source_id

    from src.config import settings
    tools = await _get_tools()

    # ── Path 1: API-retrieve-a-data-source(database UUID) ────────────────────
    retrieve_ds = tools.get("API-retrieve-a-data-source")
    if retrieve_ds:
        try:
            result = await retrieve_ds.ainvoke({"data_source_id": settings.NOTION_DATABASE_ID})
            data = _parse_mcp_text_block(result)
            logger.info(f"[DIAG] API-retrieve-a-data-source response keys: {list(data.keys())}")
            logger.info(f"[DIAG] API-retrieve-a-data-source full:\n{json.dumps(data, indent=2, default=str)}")
            candidate = data.get("id") or data.get("data_source_id")
            if candidate and '"status":4' not in json.dumps(data):
                _data_source_id = candidate
                logger.info(f"data_source_id resolved via API-retrieve-a-data-source: {_data_source_id!r}")
                return _data_source_id
        except Exception as e:
            logger.warning(f"API-retrieve-a-data-source failed: {e}")

    # ── Path 2: API-retrieve-a-database → walk for data source id ────────────
    retrieve_db = _require(tools, "API-retrieve-a-database")
    try:
        result = await retrieve_db.ainvoke({"database_id": settings.NOTION_DATABASE_ID})
        data = _parse_mcp_text_block(result)
        logger.info(f"[DIAG] API-retrieve-a-database response keys: {list(data.keys())}")
        logger.info(f"[DIAG] API-retrieve-a-database full:\n{json.dumps(data, indent=2, default=str)}")

        sources = data.get("data_sources") or []
        if sources and isinstance(sources, list):
            _data_source_id = sources[0].get("id") or sources[0].get("data_source_id")
        if not _data_source_id:
            _data_source_id = data.get("data_source_id")

        if _data_source_id:
            logger.info(f"data_source_id resolved via API-retrieve-a-database: {_data_source_id!r}")
            logger.info(f"same as database UUID? {_data_source_id == settings.NOTION_DATABASE_ID}")
            return _data_source_id
    except Exception as e:
        logger.error(f"API-retrieve-a-database failed: {e}")

    logger.error(
        f"Could not resolve data_source_id. NOTION_DATABASE_ID={settings.NOTION_DATABASE_ID!r}. "
        f"Tried API-retrieve-a-data-source and API-retrieve-a-database."
    )
    raise RuntimeError("Could not resolve data_source_id from Notion")


def _raise_if_error(result, context: str = "") -> None:
    """MCP tools return errors as text content rather than raising. Detect and raise."""
    if not isinstance(result, list):
        return
    for block in result:
        if not isinstance(block, dict):
            continue
        text = block.get("text", "")
        if isinstance(text, str) and '"status":4' in text:
            try:
                payload = json.loads(text)
                msg = payload.get("message", text)
            except json.JSONDecodeError:
                msg = text
            raise RuntimeError(f"Notion API error ({context}): {msg}")


async def _get_tools() -> dict:
    global _tools
    if _tools is not None:
        return _tools

    from langchain_mcp_adapters.client import MultiServerMCPClient
    from src.config import settings

    client = MultiServerMCPClient({
        "notion": {
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {
                "OPENAPI_MCP_HEADERS": json.dumps({
                    "Authorization": f"Bearer {settings.NOTION_API_TOKEN}",
                    "Notion-Version": "2022-06-28",
                })
            },
            "transport": "stdio",
        }
    })

    try:
        tool_list = await client.get_tools()
    except Exception as e:
        logger.error(f"Notion MCP failed to connect: {e}")
        raise

    _tools = {t.name: t for t in tool_list}
    logger.info(f"Notion MCP connected — {len(_tools)} tools available")
    logger.info(f"MCP tool names: {sorted([t.name for t in tool_list])}")
    return _tools


def _short_id() -> str:
    return str(uuid.uuid4())[:8].upper()


def _require(tools: dict, name: str):
    if name not in tools:
        raise RuntimeError(
            f"Notion MCP tool {name!r} not found. Available: {sorted(tools)}"
        )
    return tools[name]


# ── cart operations ────────────────────────────────────────────────────────────

async def add_to_cart(items: List, cart_id: str) -> list[str]:
    """Add items to Notion with Status='cart'. Returns list of created page IDs."""
    from src.config import settings

    tools = await _get_tools()
    create = _require(tools, "API-post-page")

    page_ids: list[str] = []
    errors = 0

    for item in items:
        payload = {
            "parent": {"database_id": settings.NOTION_DATABASE_ID},
            "properties": {
                "Item": {
                    "title": [{"text": {"content": item.name_en}}]
                },
                "Qty": {"number": item.qty},
                "Unit": {"select": {"name": item.unit}},
                "Status": {"select": {"name": "cart"}},
                "OrderID": {
                    "rich_text": [{"text": {"content": cart_id}}]
                },
            },
        }
        try:
            result = await create.ainvoke(payload)
            _raise_if_error(result, f"add_to_cart for {item.name_en!r}")
            # extract page_id from response
            page_id = None
            if isinstance(result, list):
                for block in result:
                    if isinstance(block, dict) and "text" in block:
                        try:
                            data = json.loads(block["text"])
                            page_id = data.get("id")
                        except json.JSONDecodeError:
                            pass
            if page_id:
                page_ids.append(page_id)
            logger.info(f"Cart row created: {item.name_en} (cart {cart_id})")
        except Exception as e:
            errors += 1
            logger.error(f"Cart add failed for {item.name_en!r}: {e}")
            if errors >= 2:
                raise RuntimeError(
                    f"Cart push aborted after 2 consecutive errors. Last: {e}"
                )

    return page_ids


async def get_cart_items(cart_id: str) -> list[dict]:
    """Return raw Notion page dicts for all Status='cart' rows under cart_id."""
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")

    # ── DIAG 2: tool name ────────────────────────────────────────────────────
    logger.info(f"[DIAG get_cart_items] MCP tool name: {query.name!r}")

    query_args = {
        "data_source_id": await _get_data_source_id(),
        "filter": {
            "and": [
                {"property": "OrderID", "rich_text": {"equals": cart_id}},
                {"property": "Status", "select": {"equals": "cart"}},
            ]
        },
    }
    # ── DIAG 3: full arguments ───────────────────────────────────────────────
    logger.info(f"[DIAG get_cart_items] query args:\n{json.dumps(query_args, indent=2)}")

    try:
        result = await query.ainvoke(query_args)
    except Exception as e:
        logger.error(f"get_cart_items failed (cart_id={cart_id}): {e}")
        raise

    # ── DIAG 4: full raw response ─────────────────────────────────────────────
    logger.info(f"[DIAG get_cart_items] raw MCP response:\n{json.dumps(result, indent=2, default=str)}")

    pre_unwrap = result if isinstance(result, list) else result.get("results", [])
    pre_unwrap_count = len(pre_unwrap)

    pages = _unwrap_pages(result)

    # ── DIAG 5: row counts ────────────────────────────────────────────────────
    logger.info(
        f"[DIAG get_cart_items] pre-unwrap count={pre_unwrap_count}, "
        f"post-unwrap count={len(pages)}"
    )

    return pages


async def send_cart(cart_id: str) -> str:
    """Flip all Status='cart' rows for cart_id to Status='pending'. Returns order_id (same as cart_id)."""
    tools = await _get_tools()
    patch = _require(tools, "API-patch-page")

    pages = await get_cart_items(cart_id)
    if not pages:
        raise RuntimeError(f"No cart items found for cart_id={cart_id}")

    errors = 0
    for page in pages:
        page_id = page.get("id") if isinstance(page, dict) else None
        if not page_id:
            continue
        try:
            result = await patch.ainvoke({
                "page_id": page_id,
                "properties": {"Status": {"select": {"name": "pending"}}},
            })
            _raise_if_error(result, f"send_cart page {page_id}")
        except Exception as e:
            errors += 1
            logger.error(f"send_cart patch failed for page {page_id}: {e}")
            if errors >= 2:
                raise RuntimeError(f"send_cart aborted after 2 errors. Last: {e}")

    logger.info(f"Cart {cart_id} sent — {len(pages)} items flipped to pending")
    return cart_id


async def clear_cart(cart_id: str) -> int:
    """Archive all Status='cart' rows for cart_id. Returns count deleted."""
    tools = await _get_tools()
    patch = _require(tools, "API-patch-page")

    pages = await get_cart_items(cart_id)
    count = 0
    for page in pages:
        page_id = page.get("id") if isinstance(page, dict) else None
        if not page_id:
            continue
        try:
            await patch.ainvoke({"page_id": page_id, "archived": True})
            count += 1
        except Exception as e:
            logger.error(f"clear_cart archive failed for page {page_id}: {e}")

    logger.info(f"Cleared {count} cart rows for cart_id={cart_id}")
    return count


async def remove_cart_item(cart_id: str, item_name: str) -> bool:
    """Archive the first cart row matching item_name. Returns True if found."""
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")
    patch = _require(tools, "API-patch-page")

    try:
        result = await query.ainvoke({
            "data_source_id": await _get_data_source_id(),
            "filter": {
                "and": [
                    {"property": "OrderID", "rich_text": {"equals": cart_id}},
                    {"property": "Status", "select": {"equals": "cart"}},
                    {"property": "Item", "title": {"equals": item_name}},
                ]
            },
        })
    except Exception as e:
        logger.error(f"remove_cart_item query failed: {e}")
        raise

    pages = _unwrap_pages(result)

    if not pages:
        return False

    page_id = pages[0].get("id") if isinstance(pages[0], dict) else None
    if not page_id:
        return False

    try:
        await patch.ainvoke({"page_id": page_id, "archived": True})
        logger.info(f"Removed cart item {item_name!r} from cart {cart_id}")
        return True
    except Exception as e:
        logger.error(f"remove_cart_item archive failed: {e}")
        raise


# ── wishlist operations ────────────────────────────────────────────────────────

def _unwrap_pages(result) -> list:
    """Normalise MCP result to a list of page dicts.

    MCP tools return results as [{"type":"text","text":"<json>"}].
    The JSON may be {"results":[...]}, a bare list, or a single page object.
    """
    pages = result if isinstance(result, list) else result.get("results", [])
    if pages and isinstance(pages[0], dict) and "text" in pages[0]:
        try:
            data = json.loads(pages[0]["text"])
            if isinstance(data, dict):
                if "results" in data:
                    pages = data["results"]
                elif data.get("object") == "page":
                    pages = [data]
                else:
                    for v in data.values():
                        if isinstance(v, list) and v:
                            pages = v
                            break
                    else:
                        pages = []
            elif isinstance(data, list):
                pages = data
        except json.JSONDecodeError:
            pass
    return pages


async def add_to_wishlist(item_name: str, qty: float, unit: str, order_id: str) -> None:
    """Add item to wishlist, accumulating qty if it already exists."""
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")
    create = _require(tools, "API-post-page")
    patch = _require(tools, "API-patch-page")

    # check for existing wishlist row for this item
    try:
        result = await query.ainvoke({
            "data_source_id": await _get_data_source_id(),
            "filter": {
                "and": [
                    {"property": "Status", "select": {"equals": "wishlist"}},
                    {"property": "Item", "title": {"equals": item_name}},
                ]
            },
        })
        existing = _unwrap_pages(result)
    except Exception as e:
        logger.error(f"wishlist query failed for {item_name!r}: {e}")
        existing = []

    if existing:
        page_id = existing[0].get("id") if isinstance(existing[0], dict) else None
        if page_id:
            # accumulate qty
            old_qty = (existing[0].get("properties", {}).get("Qty", {}).get("number") or 0)
            new_qty = old_qty + qty
            try:
                await patch.ainvoke({
                    "page_id": page_id,
                    "properties": {"Qty": {"number": new_qty}},
                })
                logger.info(f"Wishlist {item_name!r} qty updated {old_qty} -> {new_qty}")
            except Exception as e:
                logger.error(f"wishlist qty update failed: {e}")
            return

    # create new wishlist row
    payload = {
        "parent": {"database_id": settings.NOTION_DATABASE_ID},
        "properties": {
            "Item": {"title": [{"text": {"content": item_name}}]},
            "Qty": {"number": qty},
            "Unit": {"select": {"name": unit}},
            "Status": {"select": {"name": "wishlist"}},
            "OrderID": {"rich_text": [{"text": {"content": order_id}}]},
        },
    }
    try:
        result = await create.ainvoke(payload)
        _raise_if_error(result, f"add_to_wishlist {item_name!r}")
        logger.info(f"Wishlist row created: {item_name!r} (order {order_id})")
    except Exception as e:
        logger.error(f"add_to_wishlist failed for {item_name!r}: {e}")
        raise


async def get_wishlist_items() -> list[dict]:
    """Return all Status='wishlist' page dicts."""
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")

    try:
        result = await query.ainvoke({
            "data_source_id": await _get_data_source_id(),
            "filter": {"property": "Status", "select": {"equals": "wishlist"}},
        })
        return _unwrap_pages(result)
    except Exception as e:
        logger.error(f"get_wishlist_items failed: {e}")
        raise


async def remove_wishlist_item(item_name: str) -> bool:
    """Archive the first wishlist row matching item_name. Returns True if found."""
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")
    patch = _require(tools, "API-patch-page")

    try:
        result = await query.ainvoke({
            "data_source_id": await _get_data_source_id(),
            "filter": {
                "and": [
                    {"property": "Status", "select": {"equals": "wishlist"}},
                    {"property": "Item", "title": {"equals": item_name}},
                ]
            },
        })
        pages = _unwrap_pages(result)
    except Exception as e:
        logger.error(f"remove_wishlist_item query failed: {e}")
        raise

    if not pages:
        return False

    page_id = pages[0].get("id") if isinstance(pages[0], dict) else None
    if not page_id:
        return False

    await patch.ainvoke({"page_id": page_id, "archived": True})
    logger.info(f"Removed wishlist item {item_name!r}")
    return True


async def clear_wishlist() -> int:
    """Archive all wishlist rows. Returns count."""
    tools = await _get_tools()
    patch = _require(tools, "API-patch-page")

    pages = await get_wishlist_items()
    count = 0
    for page in pages:
        page_id = page.get("id") if isinstance(page, dict) else None
        if not page_id:
            continue
        try:
            await patch.ainvoke({"page_id": page_id, "archived": True})
            count += 1
        except Exception as e:
            logger.error(f"clear_wishlist archive failed for {page_id}: {e}")

    logger.info(f"Cleared {count} wishlist rows")
    return count


async def delete_wishlist_for_order_item(order_id: str, item_name: str) -> None:
    """Delete wishlist row matching order_id + item_name (for ❌ -> ✅ flip-back)."""
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")
    patch = _require(tools, "API-patch-page")

    try:
        result = await query.ainvoke({
            "data_source_id": await _get_data_source_id(),
            "filter": {
                "and": [
                    {"property": "Status", "select": {"equals": "wishlist"}},
                    {"property": "OrderID", "rich_text": {"equals": order_id}},
                    {"property": "Item", "title": {"equals": item_name}},
                ]
            },
        })
        pages = _unwrap_pages(result)
    except Exception as e:
        logger.warning(f"delete_wishlist_for_order_item query failed: {e}")
        return

    for page in pages:
        page_id = page.get("id") if isinstance(page, dict) else None
        if page_id:
            try:
                await patch.ainvoke({"page_id": page_id, "archived": True})
                logger.info(f"Deleted wishlist row for {item_name!r} order {order_id}")
            except Exception as e:
                logger.warning(f"delete_wishlist_for_order_item archive failed: {e}")


# ── legacy push (kept for shopkeeper flow compatibility) ─────────────────────

async def push_order(items: List) -> str:
    from src.config import settings

    tools = await _get_tools()
    create = _require(tools, "API-post-page")

    order_id = _short_id()
    errors = 0

    for item in items:
        payload = {
            "parent": {"database_id": settings.NOTION_DATABASE_ID},
            "properties": {
                "Item": {
                    "title": [{"text": {"content": item.name_en}}]
                },
                "Qty": {"number": item.qty},
                "Unit": {"select": {"name": item.unit}},
                "Status": {"select": {"name": "pending"}},
                "OrderID": {
                    "rich_text": [{"text": {"content": order_id}}]
                },
            },
        }
        try:
            result = await create.ainvoke(payload)
            _raise_if_error(result, f"create row for {item.name_en!r}")
            logger.info(f"Created Notion row: {item.name_en} (order {order_id})")
        except Exception as e:
            errors += 1
            logger.error(f"Notion create failed for {item.name_en!r}: {e}")
            if errors >= 2:
                raise RuntimeError(
                    f"Notion push aborted after 2 consecutive errors. Last: {e}"
                )

    return order_id


async def update_item_status(order_id: str, item_name: str, status: str) -> None:
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")
    patch = _require(tools, "API-patch-page")

    # Find the page for this order + item
    try:
        result = await query.ainvoke({
            "data_source_id": await _get_data_source_id(),
            "filter": {
                "and": [
                    {"property": "OrderID", "rich_text": {"equals": order_id}},
                    {"property": "Item", "title": {"equals": item_name}},
                ]
            },
        })
    except Exception as e:
        logger.error(f"Notion query failed (order={order_id}, item={item_name!r}): {e}")
        raise

    pages = _unwrap_pages(result)
    if not pages:
        logger.warning(f"No Notion page found for order={order_id} item={item_name!r}")
        return

    page_id = pages[0]["id"] if isinstance(pages[0], dict) else pages[0].id

    # ── DIAGNOSTIC 1: log exact args being sent to API-patch-page ────────────
    patch_args = {
        "page_id": page_id,
        "properties": {
            "Status": {"select": {"name": status}}
        },
    }
    logger.info(f"[DIAG] Calling API-patch-page with args:\n{json.dumps(patch_args, indent=2)}")

    errors = 0
    try:
        patch_result = await patch.ainvoke(patch_args)

        # ── DIAGNOSTIC 2: log full raw response ──────────────────────────────
        logger.info(f"[DIAG] API-patch-page raw response:\n{json.dumps(patch_result, indent=2, default=str)}")

    except Exception as e:
        errors += 1
        logger.error(f"Notion patch failed for {item_name!r}: {e}")
        if errors >= 2:
            raise RuntimeError(f"Notion update aborted after 2 errors. Last: {e}")
        raise

    # ── DIAGNOSTIC 3: re-fetch the page and log current Status value ─────────
    try:
        retrieve = tools.get("API-retrieve-a-page")
        if retrieve:
            fetch_result = await retrieve.ainvoke({"page_id": page_id})
            logger.info(f"[DIAG] API-retrieve-a-page raw response:\n{json.dumps(fetch_result, indent=2, default=str)}")
            # parse Status out of the response for a quick readable summary
            if isinstance(fetch_result, list):
                for block in fetch_result:
                    if isinstance(block, dict) and "text" in block:
                        try:
                            page_data = json.loads(block["text"])
                            current_status = (
                                page_data.get("properties", {})
                                .get("Status", {})
                                .get("select", {})
                                or {}
                            ).get("name", "<not found>")
                            logger.info(f"[DIAG] Status on page after patch: {current_status!r}")
                        except json.JSONDecodeError:
                            logger.info(f"[DIAG] Could not parse page response: {block['text'][:200]}")
        else:
            logger.warning("[DIAG] API-retrieve-a-page tool not available — skipping re-fetch")
    except Exception as e:
        logger.warning(f"[DIAG] Re-fetch failed: {e}")

    logger.info(f"Updated {item_name!r} -> {status} (order {order_id})")


async def get_item_status(order_id: str, item_name: str) -> str | None:
    """Return the current Notion Status value for a single item, or None if not found."""
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")

    try:
        result = await query.ainvoke({
            "data_source_id": await _get_data_source_id(),
            "filter": {
                "and": [
                    {"property": "OrderID", "rich_text": {"equals": order_id}},
                    {"property": "Item", "title": {"equals": item_name}},
                ]
            },
        })
    except Exception as e:
        logger.error(f"Notion query failed (get_item_status order={order_id}, item={item_name!r}): {e}")
        raise

    pages = _unwrap_pages(result)
    if not pages:
        return None

    props = pages[0].get("properties", {}) if isinstance(pages[0], dict) else {}
    select = (props.get("Status", {}).get("select") or {})
    return select.get("name")


async def get_order_summary(order_id: str) -> dict[str, int]:
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")

    try:
        result = await query.ainvoke({
            "data_source_id": await _get_data_source_id(),
            "filter": {
                "property": "OrderID",
                "rich_text": {"equals": order_id},
            },
        })
    except Exception as e:
        logger.error(f"Notion query failed for order summary {order_id}: {e}")
        raise

    pages = _unwrap_pages(result)
    counts: dict[str, int] = {"packed": 0, "partial": 0, "out": 0, "pending": 0}
    for page in pages:
        props = page.get("properties", {}) if isinstance(page, dict) else {}
        status_prop = props.get("Status", {})
        select = status_prop.get("select") or {}
        s = select.get("name", "pending")
        counts[s] = counts.get(s, 0) + 1

    return counts
