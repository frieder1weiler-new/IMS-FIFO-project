import json
import os

from config import CONFIG_FILE_PATH
from core.data_handler import get_product_stock, load_raw_json, save_product_stock
from core.database import add_pallet, batch_exists, remove_pallet_by_batch
from models.pallet import Pallet


def is_batch_id_duplicate(batch_id: str) -> bool:
    """Check whether a batch_id already exists in the SQL-backed inventory."""
    return batch_exists(batch_id)


def book_pallet_in(product_id: str, part_type: str, batch_id: str, fifo_number: str = None) -> str:
    """
    Handle inbound pallet scanning and persist the pallet transaction atomically.
    """
    if not os.path.exists(CONFIG_FILE_PATH):
        raise ValueError("Configuration file is missing. Please configure config.json first.")

    with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as handle:
        config_data = json.load(handle)
        registered_products = config_data.get("products", {})

    if product_id not in registered_products:
        raise ValueError(f"Product ID '{product_id}' is not registered in config.json")

    part_type = part_type.lower()
    if part_type not in ["housing", "cover"]:
        raise ValueError("Invalid part type. Must be 'housing' or 'cover'.")

    if is_batch_id_duplicate(batch_id):
        raise ValueError(f"Duplicate Scan Error: Batch ID '{batch_id}' is already registered in stock!")

    prod_config = registered_products[product_id]
    qty_key = "housing_pallet_qty" if part_type == "housing" else "cover_pallet_qty"
    standard_quantity = prod_config.get(qty_key, 500)

    assigned_fifo = fifo_number if fifo_number else batch_id
    add_pallet(product_id, part_type, batch_id, standard_quantity, fifo_number=assigned_fifo)

    return f"Successfully booked pallet {batch_id} (FIFO #{assigned_fifo}) with {standard_quantity} pcs."


def book_pallet_out(batch_id: str) -> str:
    """Remove a pallet from inventory using its unique batch_id."""
    remove_pallet_by_batch(batch_id)
    return f"Successfully removed pallet {batch_id} from inventory."