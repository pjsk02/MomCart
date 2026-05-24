"""Utility for seeding the pantry_items Chroma collection from BigBasket CSV."""
from pathlib import Path

import pandas as pd
from loguru import logger

# Common Indian fresh produce not present in BigBasket's packaged-goods categories.
# Seeded in addition to the CSV rows so canonicalization works for everyday items.
FRESH_PRODUCE = [
    # vegetables — sold by weight
    ("potato",         "Fruits & Vegetables", "kg"),
    ("onion",          "Fruits & Vegetables", "kg"),
    ("tomato",         "Fruits & Vegetables", "kg"),
    ("ginger",         "Fruits & Vegetables", "kg"),
    ("garlic",         "Fruits & Vegetables", "kg"),
    ("green chilli",   "Fruits & Vegetables", "kg"),
    ("lady finger",    "Fruits & Vegetables", "kg"),
    ("brinjal",        "Fruits & Vegetables", "kg"),
    ("cauliflower",    "Fruits & Vegetables", "kg"),
    ("cabbage",        "Fruits & Vegetables", "kg"),
    ("carrot",         "Fruits & Vegetables", "kg"),
    ("capsicum",       "Fruits & Vegetables", "kg"),
    ("beetroot",       "Fruits & Vegetables", "kg"),
    ("french beans",   "Fruits & Vegetables", "kg"),
    ("green peas",     "Fruits & Vegetables", "kg"),
    ("spinach",        "Fruits & Vegetables", "kg"),
    ("fenugreek leaves", "Fruits & Vegetables", "kg"),
    ("coriander leaves", "Fruits & Vegetables", "kg"),
    ("mint leaves",    "Fruits & Vegetables", "kg"),
    ("curry leaves",   "Fruits & Vegetables", "kg"),
    ("drumstick",      "Fruits & Vegetables", "kg"),
    ("raw banana",     "Fruits & Vegetables", "kg"),
    ("raw papaya",     "Fruits & Vegetables", "kg"),
    ("ridge gourd",    "Fruits & Vegetables", "kg"),
    ("bottle gourd",   "Fruits & Vegetables", "kg"),
    ("bitter gourd",   "Fruits & Vegetables", "kg"),
    ("ash gourd",      "Fruits & Vegetables", "kg"),
    ("snake gourd",    "Fruits & Vegetables", "kg"),
    ("cluster beans",  "Fruits & Vegetables", "kg"),
    ("sweet potato",   "Fruits & Vegetables", "kg"),
    # fruits — sold by weight
    ("banana",         "Fruits & Vegetables", "kg"),
    ("apple",          "Fruits & Vegetables", "kg"),
    ("mango",          "Fruits & Vegetables", "kg"),
    ("orange",         "Fruits & Vegetables", "kg"),
    ("pomegranate",    "Fruits & Vegetables", "kg"),
    ("papaya",         "Fruits & Vegetables", "kg"),
    ("watermelon",     "Fruits & Vegetables", "kg"),
    ("grapes",         "Fruits & Vegetables", "kg"),
    ("guava",          "Fruits & Vegetables", "kg"),
    # counted items
    ("lemon",          "Fruits & Vegetables", "pcs"),
    ("coconut",        "Fruits & Vegetables", "pcs"),
]

CATEGORIES = [
    "Foodgrains, Oil & Masala",   # exact CSV value (not "Oils")
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

    # filter categories — exact matches take priority over partial
    cat_col = next((c for c in df.columns if c == "category"), None) or \
              next((c for c in df.columns if "categor" in c), None)
    type_col = next((c for c in df.columns if c == "type"), None)
    name_col = next((c for c in df.columns if c == "product"), None) or \
               next((c for c in df.columns if "product" in c or c == "name"), None)

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

    # Append fresh produce (not in BigBasket packaged-goods categories)
    fp_ids, fp_docs, fp_meta = [], [], []
    for name, category, unit in FRESH_PRODUCE:
        fp_ids.append(f"fp_{abs(hash(name)) % 1_000_000}")
        fp_docs.append(name.lower())
        fp_meta.append({"name_en": name, "category": category, "unit": unit})
    pantry.upsert(ids=fp_ids, documents=fp_docs, metadatas=fp_meta)
    logger.info(f"Added {len(FRESH_PRODUCE)} fresh produce items")

    count = pantry.count()
    logger.info(f"pantry_items now has {count} entries")
    return count
