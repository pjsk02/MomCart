from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, List, Optional

import chromadb
from loguru import logger

from src.config import settings

if TYPE_CHECKING:
    from src.agent import GroceryItem

_client: chromadb.ClientAPI | None = None
_pantry: chromadb.Collection | None = None
_past_orders: chromadb.Collection | None = None


def _get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        settings.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(settings.CHROMA_PATH))
        logger.info(f"Chroma client initialised at {settings.CHROMA_PATH}")
    return _client


def get_pantry() -> chromadb.Collection:
    global _pantry
    if _pantry is None:
        _pantry = _get_client().get_or_create_collection(
            name="pantry_items",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"pantry_items collection: {_pantry.count()} items")
    return _pantry


def get_past_orders() -> chromadb.Collection:
    global _past_orders
    if _past_orders is None:
        _past_orders = _get_client().get_or_create_collection(
            name="past_orders",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"past_orders collection: {_past_orders.count()} items")
    return _past_orders


def search_pantry(query: str, n: int = 3) -> list[dict]:
    results = get_pantry().query(query_texts=[query], n_results=n)
    hits = []
    if results["metadatas"]:
        for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
            hits.append({**meta, "distance": dist})
    return hits


async def record_order(user_id: int, items: list) -> None:
    try:
        from src.agent import GroceryItem  # local import avoids circular

        item_names = " ".join(
            f"{i.name_native or i.name_en}" for i in items
        )
        items_json = json.dumps([i.model_dump() for i in items], ensure_ascii=False)
        today = date.today().isoformat()
        doc_id = f"{user_id}_{today}_{abs(hash(items_json)) % 100000}"

        get_past_orders().upsert(
            ids=[doc_id],
            documents=[item_names],
            metadatas=[{"user_id": str(user_id), "date_iso": today, "items_json": items_json}],
        )
        logger.info(f"Recorded order {doc_id} for user {user_id}")
    except Exception as e:
        logger.error(f"record_order failed: {e}")
        raise


async def recall_last_order(user_id: int) -> list | None:
    try:
        from src.agent import GroceryItem

        col = get_past_orders()
        results = col.get(where={"user_id": str(user_id)}, include=["metadatas"])
        if not results["metadatas"]:
            return None
        # sort by date descending, take the latest
        sorted_meta = sorted(results["metadatas"], key=lambda m: m["date_iso"], reverse=True)
        items_json = sorted_meta[0]["items_json"]
        items = [GroceryItem(**d) for d in json.loads(items_json)]
        logger.info(f"Recalled {len(items)} items from last order for user {user_id}")
        return items
    except Exception as e:
        logger.error(f"recall_last_order failed: {e}")
        return None
