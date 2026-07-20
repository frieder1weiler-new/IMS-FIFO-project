import json
import os
from typing import Any, Dict

from config import CONFIG_FILE_PATH
from core.database import load_stock_snapshot, save_stock_snapshot, ensure_product_exists
from models.pallet import ProductStock


def load_raw_json() -> dict:
    """Read the inventory snapshot from SQLite and return the legacy JSON-like structure."""
    return load_stock_snapshot()


def save_raw_json(data: dict) -> None:
    """Persist the supplied JSON-like inventory snapshot to the SQLite database."""
    save_stock_snapshot(data)


# --- High-Level Helper Functions for the App ---

def get_product_stock(product_id: str) -> ProductStock:
    """
    Fetch inventory for a product and return it as a ProductStock object.
    Auto-initializes the product entry if it exists in config but not in storage.
    """
    if os.path.exists(CONFIG_FILE_PATH):
        try:
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as handle:
                config_data = json.load(handle)
                registered_products = config_data.get("products", {})
        except Exception:
            registered_products = {}
    else:
        registered_products = {}

    if product_id not in registered_products:
        raise ValueError(f"Product ID '{product_id}' is completely unrecognized by config.json")

    db_data = load_raw_json()
    products = db_data.setdefault("products", {})

    if product_id not in products:
        prod_config = registered_products[product_id]
        ensure_product_exists(product_id, prod_config.get("name", product_id), prod_config.get("is_active", True))
        products[product_id] = {
            "name": prod_config.get("name", product_id),
            "is_active": prod_config.get("is_active", True),
            "stock": {"housings": [], "covers": []},
        }
        save_raw_json(db_data)

    prod_data = products[product_id]
    stock_data = prod_data.get("stock", {})
    return ProductStock(
        product_id=product_id,
        name=prod_data.get("name", product_id),
        housings=stock_data.get("housings", []),
        covers=stock_data.get("covers", []),
    )


def save_product_stock(product_stock: ProductStock) -> None:
    """Save a modified ProductStock object back into the SQLite-backed inventory."""
    db_data = load_raw_json()
    db_data.setdefault("products", {})[product_stock.product_id] = product_stock.to_dict()
    save_raw_json(db_data)