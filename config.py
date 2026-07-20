import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("IMS_DATA_DIR", str(BASE_DIR / "data"))).resolve()

DEFAULT_CONFIG = {
    "db_file_path": str(DATA_DIR / "stock_db.sqlite3"),
    "products": {
        "PROD_A": {"name": "Gearbox Type A", "housing_pallet_qty": 645, "cover_pallet_qty": 1440},
        "PROD_B": {"name": "Drive Unit B", "housing_pallet_qty": 500, "cover_pallet_qty": 1200},
    },
}

CONFIG_FILE_PATH = str(Path(os.getenv("IMS_CONFIG_FILE", str(DATA_DIR / "config.json"))).resolve())
DB_FILE_PATH = str(Path(os.getenv("IMS_DB_FILE", os.getenv("IMS_DB_PATH", str(DATA_DIR / "stock_db.sqlite3")))).resolve())
LOG_FILE_PATH = str(Path(os.getenv("IMS_LOG_FILE", str(BASE_DIR / "logs" / "ims_fifo.log"))).resolve())


def _load_config() -> dict:
    """Load configuration from disk or fall back to default values."""
    if not os.path.exists(CONFIG_FILE_PATH):
        return DEFAULT_CONFIG
    with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


_config_data = _load_config()

# Global constants exposed to the app
PRODUCT_REGISTRY = _config_data.get("products", {})


def get_product_config(product_id: str) -> dict:
    """Return the configuration rules (names, quantities) for a specific product ID."""
    if product_id not in PRODUCT_REGISTRY:
        raise ValueError(f"Product ID '{product_id}' is not registered in config.json")
    return PRODUCT_REGISTRY[product_id]