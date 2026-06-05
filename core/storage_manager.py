from core.data_handler import load_raw_json, get_product_stock, save_product_stock
from models.pallet import Pallet
import json
import os

def is_batch_id_duplicate(batch_id: str) -> bool:
    """Scans the entire database to ensure a Batch ID hasn't been scanned before."""
    db_data = load_raw_json()
    for prod_id, prod_info in db_data.get("products", {}).items():
        for part_type in ["housings", "covers"]:
            for pallet in prod_info.get("stock", {}).get(part_type, []):
                if pallet["batch_id"] == batch_id:
                    return True
    return False


def book_pallet_in(product_id: str, part_type: str, batch_id: str, fifo_number: str = None) -> str:
    """
    Handles scanning a pallet into the system.
    Determines standard quantities automatically based on config.json.
    Accepts fifo_number as the physical pallet label identifier.
    """
    config_path = "data/config.json"
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config_data = json.load(f)
            registered_products = config_data.get("products", {})
    else:
        registered_products = {}

    # FIXED: Changed prod_id to product_id to match function arguments
    if product_id not in registered_products:
        raise ValueError(f"Product ID '{product_id}' is not registered in config.json")
    
    # 1. Clean data inputs
    part_type = part_type.lower()
    if part_type not in ["housing", "cover"]:
        raise ValueError("Invalid part type. Must be 'housing' or 'cover'.")
    
    # 2. Check for duplicate scan
    if is_batch_id_duplicate(batch_id):
        raise ValueError(f"Duplicate Scan Error: Batch ID '{batch_id}' is already registered in stock!")

    # 3. Pull quantities from configuration rules
    # FIXED: Swapped static config module for the dynamic text file lookups we loaded above
    prod_config = registered_products[product_id]
    qty_key = "housing_pallet_qty" if part_type == "housing" else "cover_pallet_qty"
    
    # Use config values, fallback safely if fields are missing for some reason
    standard_quantity = prod_config.get(qty_key, 500)

    # 4. Load, update, and save via Data Models
    product_stock = get_product_stock(product_id)
    assigned_fifo = fifo_number if fifo_number else batch_id

    new_pallet = Pallet(
        batch_id=batch_id, 
        part_type=part_type, 
        quantity=standard_quantity,
        fifo_number=assigned_fifo  # <-- 04062026 added fifo storage!
    )
    
    if part_type == "housing":
        product_stock.housings.append(new_pallet)
    else:
        product_stock.covers.append(new_pallet)
        
    save_product_stock(product_stock)
    return f"Successfully booked pallet {batch_id} (FIFO #{assigned_fifo}) with {standard_quantity} pcs."


def book_pallet_out(batch_id: str) -> str:
    """
    Finds a pallet by its unique Batch ID across all products and removes it (Consumption).
    """
    db_data = load_raw_json()
    found = False
    
    for prod_id, prod_info in db_data.get("products", {}).items():
        for part_type in ["housings", "covers"]:
            pallets_list = prod_info.get("stock", {}).get(part_type, [])
            
            # Look for the pallet matching the unique batch_id
            for index, pallet in enumerate(pallets_list):
                if pallet["batch_id"] == batch_id:
                    # Remove it from the list
                    pallets_list.pop(index)
                    found = True
                    break
            if found: break
        if found: break

    if not found:
        raise ValueError(f"Error: Batch ID '{batch_id}' not found in current inventory.")
        
    # Save the updated data back down to the file
    from core.data_handler import save_raw_json
    save_raw_json(db_data)
    return f"Successfully removed pallet {batch_id} from inventory."