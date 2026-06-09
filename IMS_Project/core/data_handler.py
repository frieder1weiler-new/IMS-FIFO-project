import os
import json
from models.pallet import ProductStock

# Centralized hardcoded fallback paths matching app defaults
DB_FILE_PATH = "data/stock_db.json"
CONFIG_FILE_PATH = "data/config.json"

def load_raw_json() -> dict:
    """Reads the JSON storage file. Returns empty structure if file doesn't exist."""
    if not os.path.exists(DB_FILE_PATH):
        # Default boilerplate if the file is missing
        return {"products": {}}
    
    try:
        with open(DB_FILE_PATH, 'r') as file:
            return json.load(file)
    except json.JSONDecodeError:
        # Prevents crash if file gets corrupted; returns empty state
        return {"products": {}}


def save_raw_json(data: dict) -> None:
    """Writes the updated data dictionary back to the JSON file cleanly formatted."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(DB_FILE_PATH), exist_ok=True)
    
    with open(DB_FILE_PATH, 'w') as file:
        json.dump(data, file, indent=4)


# --- High-Level Helper Functions for the App ---

def get_product_stock(product_id: str) -> ProductStock:
    """
    Fetches data from the JSON file and returns it as a ProductStock object.
    Auto-initializes the product entry if it exists in config but not in storage.
    """
    # DYNAMIC FIX: Read config.json directly from disk instead of memory config variables
    if os.path.exists(CONFIG_FILE_PATH):
        try:
            with open(CONFIG_FILE_PATH, "r") as f:
                config_data = json.load(f)
                registered_products = config_data.get("products", {})
        except Exception:
            registered_products = {}
    else:
        registered_products = {}

    # Check if the product exists in our active dynamic config registry
    if product_id not in registered_products:
        raise ValueError(f"Product ID '{product_id}' is completely unrecognized by config.json")
        
    db_data = load_raw_json()
    products = db_data.setdefault("products", {})
        
    # AUTO-INITIALIZE: If it's in config but missing from stock database file, build a blank slot
    if product_id not in products:
        prod_config = registered_products[product_id]
        products[product_id] = {
            "name": prod_config["name"],
            "stock": {
                "housings": [],
                "covers": []
            }
        }
        save_raw_json(db_data) # Commit changes down to file instantly
    
    prod_data = products[product_id]
    stock_data = prod_data.get("stock", {})
    
    # We pass the raw list of dicts directly. 
    # ProductStock's __init__ will safely run [Pallet(**h) for h in housings] internally!
    return ProductStock(
        product_id=product_id,
        name=prod_data["name"],
        housings=stock_data.get("housings", []),
        covers=stock_data.get("covers", [])
    )


def save_product_stock(product_stock: ProductStock) -> None:
    """Saves a modified ProductStock object back into the JSON file."""
    db_data = load_raw_json()
    
    # Update or insert the product data using the model's built-in to_dict serialization
    db_data.setdefault("products", {})[product_stock.product_id] = product_stock.to_dict()
    
    save_raw_json(db_data)