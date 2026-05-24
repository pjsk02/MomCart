import uuid
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.agent import GroceryItem

_mcp_tools: dict | None = None


async def _get_tools() -> dict:
    global _mcp_tools
    if _mcp_tools is None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        from src.config import settings

        client = MultiServerMCPClient(
            {
                "notion": {
                    "command": "npx",
                    "args": ["-y", "@notionhq/notion-mcp-server"],
                    "env": {"OPENAPI_MCP_HEADERS": f'{{"Authorization": "Bearer {settings.NOTION_API_TOKEN}", "Notion-Version": "2022-06-28"}}'},
                    "transport": "stdio",
                }
            }
        )
        tools = await client.get_tools()
        _mcp_tools = {t.name: t for t in tools}
        logger.info(f"Notion MCP tools loaded: {list(_mcp_tools.keys())}")
    return _mcp_tools


def _short_id() -> str:
    return str(uuid.uuid4()).split("-")[0].upper()


async def push_order(items: list) -> str:
    from src.config import settings

    order_id = _short_id()
    tools = await _get_tools()

    create_tool = tools.get("notion_create_page") or tools.get("create-page")
    if create_tool is None:
        available = list(tools.keys())
        raise RuntimeError(f"No create-page tool found. Available: {available}")

    for item in items:
        try:
            await create_tool.ainvoke({
                "parent": {"database_id": settings.NOTION_DATABASE_ID},
                "properties": {
                    "Item": {"title": [{"text": {"content": item.name_en}}]},
                    "Qty": {"number": item.qty},
                    "Unit": {"select": {"name": item.unit}},
                    "Status": {"select": {"name": "pending"}},
                    "OrderID": {"rich_text": [{"text": {"content": order_id}}]},
                },
            })
            logger.info(f"Pushed {item.name_en} to Notion (order {order_id})")
        except Exception as e:
            logger.error(f"Failed to push {item.name_en} to Notion: {e}")
            raise

    return order_id


async def update_item_status(order_id: str, item_name: str, status: str) -> None:
    from src.config import settings

    tools = await _get_tools()

    query_tool = tools.get("notion_query_database") or tools.get("query-database")
    update_tool = tools.get("notion_update_page") or tools.get("update-page")

    if not query_tool or not update_tool:
        raise RuntimeError(f"Required tools missing. Available: {list(tools.keys())}")

    try:
        result = await query_tool.ainvoke({
            "database_id": settings.NOTION_DATABASE_ID,
            "filter": {
                "and": [
                    {"property": "OrderID", "rich_text": {"equals": order_id}},
                    {"property": "Item", "title": {"equals": item_name}},
                ]
            },
        })
        pages = result.get("results", [])
        if not pages:
            logger.warning(f"No page found for order={order_id} item={item_name!r}")
            return

        page_id = pages[0]["id"]
        await update_tool.ainvoke({
            "page_id": page_id,
            "properties": {"Status": {"select": {"name": status}}},
        })
        logger.info(f"Updated {item_name!r} status → {status} (order {order_id})")
    except Exception as e:
        logger.error(f"update_item_status failed order={order_id} item={item_name!r}: {e}")
        raise


async def get_order_summary(order_id: str) -> dict[str, int]:
    from src.config import settings

    tools = await _get_tools()
    query_tool = tools.get("notion_query_database") or tools.get("query-database")
    if not query_tool:
        raise RuntimeError("query-database tool not found")

    try:
        result = await query_tool.ainvoke({
            "database_id": settings.NOTION_DATABASE_ID,
            "filter": {"property": "OrderID", "rich_text": {"equals": order_id}},
        })
        counts: dict[str, int] = {"packed": 0, "partial": 0, "out": 0, "pending": 0}
        for page in result.get("results", []):
            status = page["properties"].get("Status", {}).get("select", {}) or {}
            s = status.get("name", "pending")
            counts[s] = counts.get(s, 0) + 1
        return counts
    except Exception as e:
        logger.error(f"get_order_summary failed for order={order_id}: {e}")
        raise
