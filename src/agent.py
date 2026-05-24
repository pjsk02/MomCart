from __future__ import annotations

import asyncio
import json
from typing import List, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from loguru import logger
from pydantic import BaseModel, ValidationError

from src.config import settings
from src.prompts import GROCERY_PARSER_SYSTEM, RECALL_DETECTION_SYSTEM


class GroceryItem(BaseModel):
    name_en: str
    name_native: Optional[str] = None
    qty: float
    unit: Literal["kg", "g", "L", "ml", "pcs", "packet"]


def _get_llm(format: str = "json") -> ChatOllama:
    return ChatOllama(
        model=settings.GEMMA_MODEL,
        base_url=settings.OLLAMA_HOST,
        temperature=0,
        format=format,
    )


def _canonicalize(items: List[GroceryItem]) -> List[GroceryItem]:
    from src.memory import search_pantry

    out = []
    for item in items:
        try:
            hits = search_pantry(item.name_en, n=1)
            if hits and hits[0]["distance"] < 0.6:  # cosine: lower = closer
                canonical = hits[0].get("name_en", item.name_en)
                if canonical != item.name_en:
                    logger.debug(f"Canonicalized {item.name_en!r} -> {canonical!r} (dist={hits[0]['distance']:.3f})")
                item = item.model_copy(update={"name_en": canonical})
        except Exception as e:
            logger.warning(f"Pantry lookup failed for {item.name_en!r}: {e}")
        out.append(item)
    return out


def _is_recall_request(text: str) -> bool:
    llm = ChatOllama(
        model=settings.GEMMA_MODEL,
        base_url=settings.OLLAMA_HOST,
        temperature=0,
    )
    try:
        resp = llm.invoke(
            [SystemMessage(content=RECALL_DETECTION_SYSTEM), HumanMessage(content=text)]
        )
        answer = resp.content.strip().upper()
        logger.debug(f"Recall detection for {text!r}: {answer!r}")
        return answer.startswith("YES")
    except Exception as e:
        logger.warning(f"Recall detection failed: {e}")
        return False


def _invoke_llm(text: str) -> List[GroceryItem]:
    """Sync LLM call — run via asyncio.to_thread from async contexts."""
    llm = _get_llm(format="json")
    response = llm.invoke(
        [SystemMessage(content=GROCERY_PARSER_SYSTEM), HumanMessage(content=text)]
    )
    logger.debug(f"LLM raw response: {response.content!r}")

    raw = json.loads(response.content)
    if isinstance(raw, dict):
        # Gemma sometimes wraps the array: {"items": [...]}
        raw = next((v for v in raw.values() if isinstance(v, list)), [raw])
    if not isinstance(raw, list):
        raw = [raw]

    items = []
    for entry in raw:
        try:
            items.append(GroceryItem(**entry))
        except (ValidationError, TypeError) as e:
            logger.warning(f"Skipping invalid item {entry}: {e}")
    return items


async def _parse_raw(text: str) -> List[GroceryItem]:
    try:
        items = await asyncio.to_thread(_invoke_llm, text)
        return items
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}")
        return []
    except Exception as e:
        logger.error(f"_parse_raw failed: {e}")
        raise


async def parse_grocery_text(text: str, user_id: Optional[int] = None) -> List[GroceryItem]:
    # Detect "same as last time" phrases
    if user_id and await asyncio.to_thread(_is_recall_request, text):
        from src.memory import recall_last_order

        logger.info("Recall request detected — fetching last order")
        base_items = await recall_last_order(user_id) or []

        stripped = text.lower()
        for phrase in [
            "last time jaisa", "last week jaisa", "pichli baar wala",
            "same as before", "wahi wala", "same order",
        ]:
            stripped = stripped.replace(phrase, "").strip(" ,")

        extra_items: List[GroceryItem] = []
        if stripped and len(stripped) > 3:
            extra_items = await _parse_raw(stripped)

        base_map = {i.name_en: i for i in base_items}
        for item in extra_items:
            base_map[item.name_en] = item
        return _canonicalize(list(base_map.values()))

    return _canonicalize(await _parse_raw(text))


def format_item_list(items: List[GroceryItem]) -> str:
    """Format as the spec requires: '- 2 kg atta' per line."""
    lines = []
    for item in items:
        qty = int(item.qty) if item.qty == int(item.qty) else item.qty
        lines.append(f"- {qty} {item.unit} {item.name_en}")
    return "\n".join(lines)
