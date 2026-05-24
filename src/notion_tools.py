from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, List

from loguru import logger

if TYPE_CHECKING:
    from src.agent import GroceryItem

_tools: dict | None = None


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
    return _tools


def _short_id() -> str:
    return str(uuid.uuid4())[:8].upper()


def _require(tools: dict, name: str):
    if name not in tools:
        raise RuntimeError(
            f"Notion MCP tool {name!r} not found. Available: {sorted(tools)}"
        )
    return tools[name]


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
            "data_source_id": settings.NOTION_DATABASE_ID,
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

    pages = result if isinstance(result, list) else result.get("results", [])
    if not pages:
        logger.warning(f"No Notion page found for order={order_id} item={item_name!r}")
        return

    page_id = pages[0]["id"] if isinstance(pages[0], dict) else pages[0].id
    errors = 0
    try:
        await patch.ainvoke({
            "page_id": page_id,
            "properties": {
                "Status": {"select": {"name": status}}
            },
        })
        logger.info(f"Updated {item_name!r} -> {status} (order {order_id})")
    except Exception as e:
        errors += 1
        logger.error(f"Notion patch failed for {item_name!r}: {e}")
        if errors >= 2:
            raise RuntimeError(f"Notion update aborted after 2 errors. Last: {e}")
        raise


async def get_order_summary(order_id: str) -> dict[str, int]:
    from src.config import settings

    tools = await _get_tools()
    query = _require(tools, "API-query-data-source")

    try:
        result = await query.ainvoke({
            "data_source_id": settings.NOTION_DATABASE_ID,
            "filter": {
                "property": "OrderID",
                "rich_text": {"equals": order_id},
            },
        })
    except Exception as e:
        logger.error(f"Notion query failed for order summary {order_id}: {e}")
        raise

    pages = result if isinstance(result, list) else result.get("results", [])
    counts: dict[str, int] = {"packed": 0, "partial": 0, "out": 0, "pending": 0}
    for page in pages:
        props = page.get("properties", {}) if isinstance(page, dict) else {}
        status_prop = props.get("Status", {})
        select = status_prop.get("select") or {}
        s = select.get("name", "pending")
        counts[s] = counts.get(s, 0) + 1

    return counts
