"""CLI: populate Chroma pantry_items from BigBasket CSV."""
from pathlib import Path

from loguru import logger


def main() -> None:
    csv_path = Path("data/bigbasket.csv")
    if not csv_path.exists():
        print(
            f"ERROR: {csv_path} not found.\n"
            "Download from: https://www.kaggle.com/datasets/surajjha101/bigbasket-entire-product-list-28k-datapoints\n"
            "and place it at data/bigbasket.csv"
        )
        raise SystemExit(1)

    from src.pantry_seed import seed_from_csv

    count = seed_from_csv(csv_path)
    print(f"Done. pantry_items collection now has {count} entries.")


if __name__ == "__main__":
    main()
