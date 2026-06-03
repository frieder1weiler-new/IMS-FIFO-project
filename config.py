import os
import json

CONFIG_FILE_PATH = os.path.join("data", "config.json")

# Default fallback if config file doesn't exist yet
def _load_config():
    if not os.path.exists(CONFIG_FILE_PATH):
        raise FileNotFoundError(f"Critical Error: {CONFIG_FILE_PATH} is missing.")
    with open(CONFIG_FILE_PATH, 'r') as f:
        return json.load(f)

_config_data = _load_config()

# Global Constants exposed to the app
DB_FILE_PATH = _config_data.get("db_file_path", "data/stock_db.json")
PRODUCT_REGISTRY = _config_data.get("products", {})

def get_product_config(product_id: str) -> dict:
    """Returns the configuration rules (names, quantities) for a specific product ID."""
    if product_id not in PRODUCT_REGISTRY:
        raise ValueError(f"Product ID '{product_id}' is not registered in config.json")
    return PRODUCT_REGISTRY[product_id]