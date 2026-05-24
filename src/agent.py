from __future__ import annotations

import json
from typing import Literal, Optional

from langchain_ollama import ChatOllama
from loguru import logger
from pydantic import BaseModel, ValidationError

from src.config import settings
from src.prompts import GROCERY_PARSER_SYSTEM, RECALL_DETECTION_SYSTEM


class GroceryItem(BaseModel):
    name_en: str
    name_native: str | None = None
    qty: float
    unit: Literal["kg", "g", "L", "ml", "pcs", "packet"]


def _get_llm() -> ChatOllama:
    return ChatOllama(
        model=settings.GEMMA_MODEL,
        base_url=settings.OLLAMA_HOST,
        temperature=0,
        format="json",
    )


def _canonicalize(items: list[GroceryItem]) -> list[GroceryItem]:
    from src.memory import search_pantry

    canonicalized = []
    for item in items:
        try:
            hits = search_pantry(item.name_en, n=1)
            if hits and hits[0]["distance"] < 0.35:
                canonical = hits[0].get("name_en", item.name_en)
                logger.debug(f"Canonicalized {item.name_en!r} → {canonical!r}")
                item = item.model_copy(update={"name_en": canonical})
        except Exception as e:
            logger.warning(f"Pantry lookup failed for {item.name_en!r}: {e}")
        canonicalized.append(item)
    return canonicalized


def _is_recall_request(text: str) -> bool:
    llm = ChatOllama(
        model=settings.GEMMA_MODEL,
        base_url=settings.OLLAMA_HOST,
        temperature=0,
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = llm.invoke(
            [SystemMessage(content=RECALL_DETECTION_SYSTEM), HumanMessage(content=text)]
        )
        answer = resp.content.strip().upper()
        logger.debug(f"Recall detection for {text!r}: {answer}")
        return answer.startswith("YES")
    except Exception as e:
        logger.warning(f"Recall detection failed: {e}")
        return False


async def parse_grocery_text(text: str, user_id: int | None = None) -> list[GroceryItem]:
    from langchain_core.messages import HumanMessage, SystemMessage

    # Check for "same as last time" pattern
    if user_id and _is_recall_request(text):
        from src.memory import recall_last_order

        logger.info("Recall request detected — fetching last order")
        base_items = await recall_last_order(user_id) or []

        # Strip the recall phrase, parse any additions
        stripped = text
        for phrase in [
            "last time jaisa", "last week jaisa", "pichli baar wala",
            "same as before", "wahi wala", "same order",
        ]:
            stripped = stripped.lower().replace(phrase, "").strip(" ,")

        extra_items: list[GroceryItem] = []
        if stripped:
            extra_items = await _parse_raw(stripped)

        # merge: extra items override base by name_en
        base_map = {i.name_en: i for i in base_items}
        for item in extra_items:
            base_map[item.name_en] = item
        return _canonicalize(list(base_map.values()))

    return _canonicalize(await _parse_raw(text))


async def _parse_raw(text: str) -> list[GroceryItem]:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = _get_llm()
    try:
        response = llm.invoke(
            [SystemMessage(content=GROCERY_PARSER_SYSTEM), HumanMessage(content=text)]
        )
        logger.debug(f"LLM raw response: {response.content!r}")
        raw = json.loads(response.content)
        if not isinstance(raw, list):
            raw = [raw]
        items = []
        for entry in raw:
            try:
                items.append(GroceryItem(**entry))
            except ValidationError as ve:
                logger.warning(f"Skipping invalid item {entry}: {ve}")
        return items
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}\nContent: {response.content!r}")
        return []
    except Exception as e:
        logger.error(f"parse_grocery_text failed: {e}")
        raise


def format_item_list(items: list[GroceryItem]) -> str:
    lines = []
    for i, item in enumerate(items, 1):
        native = f" ({item.name_native})" if item.name_native else ""
        lines.append(f"{i}. {item.qty} {item.unit} {item.name_en}{native}")
    return "\n".join(lines)
