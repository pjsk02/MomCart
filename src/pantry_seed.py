"""Utility for seeding the pantry_items Chroma collection from BigBasket CSV."""
from pathlib import Path

import pandas as pd
from loguru import logger

CATEGORIES = [
    "Foodgrains, Oils & Masala",
    "Snacks & Branded Foods",
    "Gourmet & World Food",
    "Bakery, Cakes & Dairy",
    "Beverages",
]

UNIT_PATTERNS = [
    (r"(\d+)\s*kg", "kg"),
    (r"(\d+)\s*g(?:m|ms?|ram)?", "g"),
    (r"(\d+)\s*l(?:tr|itre)?", "L"),
    (r"(\d+)\s*ml", "ml"),
    (r"(\d+)\s*pcs?", "pcs"),
    (r"(\d+)\s*pack(?:et)?", "packet"),
]


def _infer_unit(name: str) -> str:
    import re

    name_lower = name.lower()
    for pattern, unit in UNIT_PATTERNS:
        if re.search(pattern, name_lower):
            return unit
    return "pcs"


def seed_from_csv(csv_path: Path, limit: int = 200) -> int:
    from src.memory import get_pantry

    if not csv_path.exists():
        raise FileNotFoundError(f"BigBasket CSV not found at {csv_path}")

    logger.info(f"Reading {csv_path}")
    df = pd.read_csv(csv_path)

    # normalise column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    logger.debug(f"Columns: {list(df.columns)}")

    # filter categories
    cat_col = next((c for c in df.columns if "categor" in c), None)
    type_col = next((c for c in df.columns if c in ("type", "sub_category", "sub-category")), None)
    name_col = next((c for c in df.columns if "product" in c or c == "name"), None)

    if cat_col:
        mask = df[cat_col].str.contains("|".join(CATEGORIES), case=False, na=False)
        df = df[mask]
        logger.info(f"After category filter: {len(df)} rows")

    if type_col:
        df = df.drop_duplicates(subset=[type_col])
        logger.info(f"After dedup on {type_col!r}: {len(df)} rows")

    df = df.head(limit)
    logger.info(f"Seeding {len(df)} items into Chroma")

    pantry = get_pantry()
    ids, documents, metadatas = [], [], []

    for _, row in df.iterrows():
        name = str(row.get(name_col or "product", row.iloc[0])).strip()
        category = str(row.get(cat_col, "")).strip() if cat_col else ""
        unit = _infer_unit(name)
        item_id = f"bb_{abs(hash(name)) % 1_000_000}"

        ids.append(item_id)
        documents.append(name.lower())
        metadatas.append({"name_en": name, "category": category, "unit": unit})

    pantry.upsert(ids=ids, documents=documents, metadatas=metadatas)
    count = pantry.count()
    logger.info(f"pantry_items now has {count} entries")
    return count
