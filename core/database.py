import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from config import CONFIG_FILE_PATH, DB_FILE_PATH


@dataclass
class PalletRecord:
    """Represents a pallet row in the SQLite inventory table."""

    batch_id: str
    part_type: str
    quantity: int
    timestamp: str
    fifo_number: Optional[str] = None


@dataclass
class ProductState:
    """Represents the top-level stock state for a product."""

    product_id: str
    name: str
    is_active: bool = True


def _resolve_db_path() -> str:
    """Resolve the database file path from environment overrides first."""
    raw_path = os.getenv("IMS_DB_FILE") or os.getenv("IMS_DB_PATH") or DB_FILE_PATH
    if not os.path.isabs(raw_path):
        raw_path = os.path.abspath(raw_path)
    return raw_path


DB_PATH = _resolve_db_path()
_INITIALIZING_DATABASE = False


def _ensure_parent_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _get_connection() -> sqlite3.Connection:
    _ensure_parent_dir(DB_PATH)
    connection = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def _write_transaction() -> Iterator[sqlite3.Connection]:
    """Provide an explicit write transaction with rollback support."""
    connection = _get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    """Create tables and migrate any legacy JSON data on first startup."""
    global _INITIALIZING_DATABASE
    if _INITIALIZING_DATABASE:
        return

    _INITIALIZING_DATABASE = True
    try:
        _ensure_parent_dir(DB_PATH)
        with _write_transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pallets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    part_type TEXT NOT NULL,
                    batch_id TEXT NOT NULL UNIQUE,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    timestamp TEXT NOT NULL,
                    fifo_number TEXT,
                    FOREIGN KEY(product_id) REFERENCES products(product_id) ON DELETE CASCADE
                )
                """
            )

        migrate_legacy_json_if_needed()
    finally:
        _INITIALIZING_DATABASE = False


def migrate_legacy_json_if_needed() -> None:
    """Import the legacy stock_db.json snapshot into SQLite when the database is empty."""
    legacy_path = os.getenv("IMS_LEGACY_DB_FILE") or os.path.join(os.path.dirname(__file__), "..", "data", "stock_db.json")
    legacy_path = os.path.abspath(legacy_path)
    if not os.path.exists(legacy_path):
        return

    with _get_connection() as connection:
        has_rows = connection.execute("SELECT 1 FROM pallets LIMIT 1").fetchone()
        if has_rows:
            return

    initialize_database()
    with open(legacy_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    save_stock_snapshot(payload)


def load_stock_snapshot() -> Dict[str, Any]:
    """Return the inventory as the original JSON-style nested structure."""
    initialize_database()
    connection = _get_connection()
    try:
        products = connection.execute("SELECT product_id, name, is_active FROM products ORDER BY product_id").fetchall()
        snapshot: Dict[str, Any] = {"products": {}}
        for product in products:
            row_data = {
                "name": product["name"],
                "is_active": bool(product["is_active"]),
                "stock": {"housings": [], "covers": []},
            }
            pallets = connection.execute(
                "SELECT part_type, batch_id, quantity, timestamp, fifo_number FROM pallets WHERE product_id = ? ORDER BY timestamp ASC",
                (product["product_id"],),
            ).fetchall()
            for pallet in pallets:
                record = {
                    "batch_id": pallet["batch_id"],
                    "part_type": pallet["part_type"],
                    "quantity": pallet["quantity"],
                    "timestamp": pallet["timestamp"],
                }
                if pallet["fifo_number"]:
                    record["fifo_number"] = pallet["fifo_number"]
                if pallet["part_type"] == "housing":
                    row_data["stock"]["housings"].append(record)
                else:
                    row_data["stock"]["covers"].append(record)
            snapshot["products"][product["product_id"]] = row_data
        return snapshot
    finally:
        connection.close()


def save_stock_snapshot(snapshot: Dict[str, Any]) -> None:
    """Replace the database contents with the supplied JSON-style snapshot."""
    if not isinstance(snapshot, dict):
        raise ValueError("Inventory snapshot must be a dictionary.")

    products_payload = snapshot.get("products", {}) or {}
    with _write_transaction() as connection:
        connection.execute("DELETE FROM pallets")
        connection.execute("DELETE FROM products")
        for product_id, product_data in products_payload.items():
            name = product_data.get("name", product_id)
            is_active = 1 if product_data.get("is_active", True) else 0
            connection.execute(
                "INSERT INTO products (product_id, name, is_active) VALUES (?, ?, ?)",
                (product_id, name, is_active),
            )
            stock_data = product_data.get("stock", {}) or {}
            for part_type, pallet_list in (("housing", stock_data.get("housings", [])), ("cover", stock_data.get("covers", []))):
                for pallet in pallet_list or []:
                    connection.execute(
                        """
                        INSERT INTO pallets (product_id, part_type, batch_id, quantity, timestamp, fifo_number)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            product_id,
                            part_type,
                            pallet.get("batch_id"),
                            int(pallet.get("quantity", 1)),
                            pallet.get("timestamp", datetime.now().isoformat()),
                            pallet.get("fifo_number"),
                        ),
                    )


def ensure_product_exists(product_id: str, name: str = "", is_active: bool = True) -> None:
    """Create a product record if it is missing."""
    initialize_database()
    with _write_transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO products (product_id, name, is_active)
            VALUES (?, ?, ?)
            """,
            (product_id, name or product_id, 1 if is_active else 0),
        )
        connection.execute(
            "UPDATE products SET name = ?, is_active = ? WHERE product_id = ?",
            (name or product_id, 1 if is_active else 0, product_id),
        )


def add_pallet(product_id: str, part_type: str, batch_id: str, quantity: int, fifo_number: Optional[str] = None, timestamp: Optional[str] = None) -> None:
    """Insert a pallet into the inventory under a product and part type."""
    ensure_product_exists(product_id, product_id)
    final_part_type = part_type.lower()
    if final_part_type not in {"housing", "cover"}:
        raise ValueError("Invalid part type. Must be 'housing' or 'cover'.")
    if batch_exists(batch_id):
        raise ValueError(f"Duplicate Scan Error: Batch ID '{batch_id}' is already registered in stock!")

    with _write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO pallets (product_id, part_type, batch_id, quantity, timestamp, fifo_number)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                final_part_type,
                batch_id,
                int(quantity),
                timestamp or datetime.now().isoformat(),
                fifo_number.strip() if fifo_number else None,
            ),
        )


def remove_pallet_by_batch(batch_id: str) -> None:
    """Remove the pallet with the supplied batch_id if it exists."""
    initialize_database()
    with _write_transaction() as connection:
        cursor = connection.execute("SELECT id FROM pallets WHERE batch_id = ?", (batch_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Error: Batch ID '{batch_id}' not found in current inventory.")
        connection.execute("DELETE FROM pallets WHERE batch_id = ?", (batch_id,))


def batch_exists(batch_id: str) -> bool:
    """Check whether a batch_id is already registered."""
    initialize_database()
    connection = _get_connection()
    try:
        row = connection.execute("SELECT 1 FROM pallets WHERE batch_id = ?", (batch_id,)).fetchone()
        return row is not None
    finally:
        connection.close()


def get_product_state(product_id: str) -> ProductState:
    """Return the metadata associated with a product."""
    initialize_database()
    connection = _get_connection()
    try:
        row = connection.execute("SELECT product_id, name, is_active FROM products WHERE product_id = ?", (product_id,)).fetchone()
        if row is None:
            raise ValueError(f"Product '{product_id}' is not registered in the stock database.")
        return ProductState(product_id=row["product_id"], name=row["name"], is_active=bool(row["is_active"]))
    finally:
        connection.close()
