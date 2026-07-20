import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_session import Session

from config import CONFIG_FILE_PATH, DB_FILE_PATH, LOG_FILE_PATH
from core.data_handler import get_product_stock, load_raw_json
from core.database import initialize_database
from core.storage_manager import book_pallet_in, book_pallet_out

load_dotenv()

HARDWARE_TRANSACTION_LOGS = []


def configure_logging() -> logging.Logger:
    """Create a simple rotating file logger for production diagnostics."""
    os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
    logger = logging.getLogger("ims_fifo")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, os.getenv("IMS_LOG_LEVEL", "INFO").upper(), logging.INFO))
    handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


logger = configure_logging()
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me")
app.config["SESSION_TYPE"] = os.getenv("SESSION_TYPE", "filesystem")
app.config["SESSION_FILE_DIR"] = os.path.join(os.path.dirname(__file__), "data", "sessions")
Session(app)

initialize_database()


def load_live_config_products() -> dict:
    """Read configuration profiles directly from disk to keep queries dynamic."""
    if not os.path.exists(CONFIG_FILE_PATH):
        return {
            "PROD_A": {"name": "Gearbox Type A", "housing_pallet_qty": 645, "cover_pallet_qty": 1440},
            "PROD_B": {"name": "Drive Unit B", "housing_pallet_qty": 500, "cover_pallet_qty": 1200},
        }
    try:
        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle).get("products", {})
    except Exception as exc:
        logger.exception("Failed to read config file: %s", exc)
        return {}


def record_log(channel: str, message: str, status: str) -> None:
    """Save transaction histories to a rolling in-memory log for UI diagnostics."""
    HARDWARE_TRANSACTION_LOGS.insert(0, {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "channel": channel,
        "message": message,
        "status": status,
    })
    if len(HARDWARE_TRANSACTION_LOGS) > 50:
        HARDWARE_TRANSACTION_LOGS.pop()


@app.route("/")
def index():
    """Render the main dashboard."""
    return render_template("display.html")


@app.route("/production-input")
def production_input():
    """Render the production input page."""
    return render_template("production_input.html")


@app.route("/management")
def management():
    """Render the management panel with live product settings."""
    return render_template("management.html", products=load_live_config_products())


@app.route("/scanner-terminal")
def hw_terminal_listener_view():
    """Render the scanner terminal view."""
    return render_template("scanner_listener.html")


@app.route("/api/admin/logs", methods=["GET"])
def get_hardware_activity_logs():
    """Return the recent scanner event log."""
    return jsonify(HARDWARE_TRANSACTION_LOGS), 200


@app.route("/api/scan/in", methods=["POST"])
def api_scan_in():
    """Endpoint for booking pallets inbound."""
    data = request.get_json(silent=True) or {}
    prod_id = data.get("product_id")
    part_type = data.get("part_type")
    batch_id = data.get("batch_id")
    fifo_number = data.get("fifo_number")

    logger.info("Inbound scan payload received: product=%s part=%s batch=%s fifo=%s", prod_id, part_type, batch_id, fifo_number)

    if not all([prod_id, part_type, batch_id]):
        return jsonify({"status": "error", "message": "Missing required scan data fields."}), 400

    if not fifo_number:
        return jsonify({"status": "error", "message": "Missing FIFO number. Please scan the physical pallet label."}), 400

    fifo_number = str(fifo_number).strip()
    if not fifo_number:
        return jsonify({"status": "error", "message": "FIFO number cannot be blank or whitespace only."}), 400

    try:
        book_pallet_in(prod_id, part_type, batch_id, fifo_number)
        record_log("INBOUND", f"Registered {prod_id} ({part_type}) | Batch: {batch_id} | FIFO#: {fifo_number}", "SUCCESS")
        return jsonify({"status": "success", "message": f"Pallet {batch_id} (FIFO#{fifo_number}) stored."}), 200
    except Exception as exc:
        logger.exception("Inbound scan failed: %s", exc)
        record_log("INBOUND", f"Failed Scan Storage: {str(exc)}", "ERROR")
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/scan/out", methods=["POST"])
def api_scan_out():
    """Handle outbound pallet consumption with optional FIFO enforcement."""
    data = request.get_json(silent=True) or {}
    batch_id = data.get("batch_id")
    enforce_fifo = bool(data.get("enforce_fifo", False))

    if not batch_id:
        return jsonify({"status": "error", "message": "Missing Batch ID."}), 400

    active_products = load_live_config_products()
    fifo_violation = False
    correct_fifo_batch = None
    target_product = ""
    target_type = ""

    for prod_id in active_products.keys():
        try:
            stock_obj = get_product_stock(prod_id)
            target_lists = {"housings": stock_obj.housings, "covers": stock_obj.covers}

            for p_type, pallets in target_lists.items():
                if not pallets:
                    continue

                has_batch = any(p.batch_id == batch_id for p in pallets)
                if has_batch:
                    sorted_pallets = sorted(pallets, key=lambda item: item.timestamp)
                    oldest_entry = sorted_pallets[0]
                    target_product = active_products.get(prod_id, {}).get("name", prod_id)
                    target_type = "Housing" if p_type == "housings" else "Cover"
                    if oldest_entry.batch_id != batch_id:
                        fifo_violation = True
                        correct_fifo_batch = oldest_entry.batch_id
                    break
            if target_product:
                break
        except ValueError:
            continue

    if fifo_violation and enforce_fifo:
        reject_msg = f"❌ FIFO REJECTION: Scanned {batch_id} for {target_product} {target_type}. MUST use oldest batch: {correct_fifo_batch}."
        record_log("OUTBOUND", reject_msg, "ERROR")
        return jsonify({
            "status": "error",
            "message": f"FIFO Enforcement Active! Scan rejected. Please locate oldest batch: {correct_fifo_batch}.",
            "fifo_violation": True,
            "expected_batch": correct_fifo_batch,
        }), 400

    try:
        message = book_pallet_out(batch_id)
        if fifo_violation:
            warn_msg = f"⚠️ FIFO VIOLATION: Scanned batch {batch_id} for {target_product} {target_type}. An older batch ({correct_fifo_batch}) was available!"
            record_log("OUTBOUND", warn_msg, "ERROR")
            return jsonify({
                "status": "warning",
                "message": message,
                "fifo_violation": True,
                "expected_batch": correct_fifo_batch,
            }), 200

        record_log("OUTBOUND", f"Batch {batch_id} consumed from line stock.", "SUCCESS")
        return jsonify({"status": "success", "message": message}), 200
    except ValueError as exc:
        logger.exception("Outbound scan failed: %s", exc)
        record_log("OUTBOUND", f"Failed Consumption: {str(exc)}", "ERROR")
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/stock/takeout-fifo", methods=["GET"])
def api_takeout_fifo():
    """Calculate the absolute oldest active pallet batches per product and part type."""
    active_products = load_live_config_products()
    fifo_matrix = []

    for prod_id, meta in active_products.items():
        try:
            stock_obj = get_product_stock(prod_id)

            def get_oldest_batch(pallet_list):
                if not pallet_list:
                    return None
                sorted_pallets = sorted(pallet_list, key=lambda item: item.timestamp)
                oldest = sorted_pallets[0]
                display_time = oldest.timestamp.split(".")[0].replace("T", " ") if "T" in oldest.timestamp else oldest.timestamp
                return {
                    "fifo_number": oldest.fifo_number,
                    "batch_id": oldest.batch_id,
                    "timestamp": display_time,
                    "qty": oldest.quantity,
                }

            fifo_matrix.append({
                "name": meta.get("name", "Unknown Unit"),
                "oldest_housing": get_oldest_batch(stock_obj.housings),
                "oldest_cover": get_oldest_batch(stock_obj.covers),
            })
        except ValueError:
            fifo_matrix.append({
                "name": meta.get("name", "Unknown Unit"),
                "oldest_housing": None,
                "oldest_cover": None,
            })

    return jsonify(fifo_matrix), 200


@app.route("/api/stock/data", methods=["GET"])
def api_stock_data():
    """Return calculations for the dashboard interface."""
    active_products = load_live_config_products()
    compiled_dashboard = {}

    for product_id, meta in active_products.items():
        try:
            stock_obj = get_product_stock(product_id)

            next_housing_fifo = None
            next_cover_fifo = None
            if stock_obj.housings:
                sorted_housings = sorted(stock_obj.housings, key=lambda item: item.timestamp)
                next_housing_fifo = sorted_housings[0].fifo_number
            if stock_obj.covers:
                sorted_covers = sorted(stock_obj.covers, key=lambda item: item.timestamp)
                next_cover_fifo = sorted_covers[0].fifo_number

            compiled_dashboard[product_id] = {
                "name": meta.get("name", "Unknown Unit Module"),
                "is_active": meta.get("is_active", True),
                "total_housings": stock_obj.total_housing_pcs,
                "total_covers": stock_obj.total_cover_pcs,
                "ready_sets": stock_obj.ready_assembly_sets,
                "raw_pallet_count": {"housings": len(stock_obj.housings), "covers": len(stock_obj.covers)},
                "next_fifo_numbers": {"housing": next_housing_fifo, "cover": next_cover_fifo},
                "thresholds": {
                    "min_stock_pcs": meta.get("min_stock_pcs", 1000),
                    "max_stock_pcs": meta.get("max_stock_pcs", 5000),
                    "housing_pallet_qty": meta.get("housing_pallet_qty", 500),
                    "cover_pallet_qty": meta.get("cover_pallet_qty", 1000),
                },
            }
        except ValueError:
            compiled_dashboard[product_id] = {
                "name": meta.get("name", "Unknown Unit Module"),
                "is_active": meta.get("is_active", True),
                "total_housings": 0,
                "total_covers": 0,
                "ready_sets": 0,
                "raw_pallet_count": {"housings": 0, "covers": 0},
                "next_fifo_numbers": {"housing": None, "cover": None},
                "thresholds": {
                    "min_stock_pcs": meta.get("min_stock_pcs", 1000),
                    "max_stock_pcs": meta.get("max_stock_pcs", 5000),
                    "housing_pallet_qty": meta.get("housing_pallet_qty", 500),
                    "cover_pallet_qty": meta.get("cover_pallet_qty", 1000),
                },
            }

    return jsonify(compiled_dashboard), 200


@app.route("/api/admin/active-stock", methods=["GET"])
def get_active_stock_manifest():
    """Flatten the nested stock database into a data-ready list."""
    if not os.path.exists(DB_FILE_PATH):
        return jsonify([]), 200

    try:
        db_data = load_raw_json()
        active_pallets = []
        products_dict = db_data.get("products", {})

        for prod_id, prod_meta in products_dict.items():
            stock_node = prod_meta.get("stock", {})
            target_lists = {"housing": stock_node.get("housings", []), "cover": stock_node.get("covers", [])}

            for part_type, pallet_list in target_lists.items():
                if isinstance(pallet_list, list):
                    for pallet in pallet_list:
                        raw_time = pallet.get("timestamp", "N/A")
                        display_time = raw_time.split(".")[0].replace("T", " ") if "T" in raw_time else raw_time
                        active_pallets.append({
                            "timestamp": display_time,
                            "product_id": prod_id,
                            "part_type": part_type,
                            "batch_id": pallet.get("batch_id", "UNKNOWN"),
                            "quantity": pallet.get("quantity", 1),
                        })

        active_pallets.sort(key=lambda item: item["timestamp"], reverse=True)
        return jsonify(active_pallets), 200
    except Exception as exc:
        logger.exception("Failed to build active stock manifest: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/db-status", methods=["GET"])
def api_db_status():
    """Return diagnostics about the SQLite stock store."""
    raw_data = load_raw_json()
    return jsonify({
        "file_path": DB_FILE_PATH,
        "file_exists": os.path.exists(DB_FILE_PATH),
        "tracked_products": list(raw_data.get("products", {}).keys()),
        "raw_structure": raw_data,
    }), 200


@app.route("/api/admin/db-reset", methods=["POST"])
def api_db_reset():
    """Wipe the database back to a clean slate."""
    from core.data_handler import save_raw_json

    save_raw_json({"products": {}})
    return jsonify({"status": "success", "message": "Database cleared successfully."}), 200


@app.route("/api/admin/config", methods=["GET"])
def get_system_config():
    """Return the active config.json structure safely."""
    if not os.path.exists(CONFIG_FILE_PATH):
        default_config = {
            "db_file_path": "data/stock_db.json",
            "products": {
                "PROD_A": {"name": "Gearbox Type A", "housing_pallet_qty": 645, "cover_pallet_qty": 1440},
                "PROD_B": {"name": "Drive Unit B", "housing_pallet_qty": 500, "cover_pallet_qty": 1200},
            },
        }
        return jsonify(default_config), 200

    try:
        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as handle:
            return jsonify(json.load(handle)), 200
    except Exception as exc:
        logger.exception("Configuration read failed: %s", exc)
        return jsonify({"status": "error", "message": f"Read failed: {str(exc)}"}), 500


@app.route("/api/admin/config/save", methods=["POST"])
def save_system_config():
    """Persist updated product settings in config.json."""
    try:
        incoming_data = request.get_json(silent=True)
        if not incoming_data or "products" not in incoming_data:
            return jsonify({"status": "error", "message": "Invalid configuration payload."}), 400

        current_config = {}
        if os.path.exists(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as handle:
                current_config = json.load(handle)
        else:
            current_config = {"db_file_path": "data/stock_db.sqlite3", "products": {}}

        for prod_id, incoming_meta in incoming_data["products"].items():
            if prod_id in current_config.get("products", {}):
                existing = current_config["products"][prod_id]
                existing["name"] = incoming_meta.get("name", existing.get("name", "Unknown"))
                existing["is_active"] = incoming_meta.get("is_active", existing.get("is_active", True))

                if incoming_meta.get("housing_pallet_qty") is not None:
                    try:
                        existing["housing_pallet_qty"] = int(incoming_meta["housing_pallet_qty"])
                    except (ValueError, TypeError):
                        pass

                if incoming_meta.get("cover_pallet_qty") is not None:
                    try:
                        existing["cover_pallet_qty"] = int(incoming_meta["cover_pallet_qty"])
                    except (ValueError, TypeError):
                        pass

                if incoming_meta.get("min_stock_pcs") is not None:
                    try:
                        existing["min_stock_pcs"] = int(incoming_meta["min_stock_pcs"])
                    except (ValueError, TypeError):
                        pass

                if incoming_meta.get("max_stock_pcs") is not None:
                    try:
                        existing["max_stock_pcs"] = int(incoming_meta["max_stock_pcs"])
                    except (ValueError, TypeError):
                        pass
            else:
                try:
                    current_config["products"][prod_id] = {
                        "name": incoming_meta.get("name", "Unknown"),
                        "is_active": incoming_meta.get("is_active", True),
                        "housing_pallet_qty": int(incoming_meta.get("housing_pallet_qty", 500)),
                        "cover_pallet_qty": int(incoming_meta.get("cover_pallet_qty", 1000)),
                        "min_stock_pcs": int(incoming_meta.get("min_stock_pcs", 1000)),
                        "max_stock_pcs": int(incoming_meta.get("max_stock_pcs", 5000)),
                    }
                except (ValueError, TypeError):
                    current_config["products"][prod_id] = {
                        "name": incoming_meta.get("name", "Unknown"),
                        "is_active": incoming_meta.get("is_active", True),
                        "housing_pallet_qty": 500,
                        "cover_pallet_qty": 1000,
                        "min_stock_pcs": 1000,
                        "max_stock_pcs": 5000,
                    }

        for prod_id in list(current_config.get("products", {}).keys()):
            if prod_id not in incoming_data["products"]:
                del current_config["products"][prod_id]

        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as handle:
            json.dump(current_config, handle, indent=4)

        return jsonify({"status": "success", "message": "System configuration updated safely!"}), 200
    except Exception as exc:
        logger.exception("Configuration write failed: %s", exc)
        return jsonify({"status": "error", "message": f"Write failed: {str(exc)}"}), 500


@app.route("/api/admin/config/toggle-active", methods=["POST"])
def toggle_product_active():
    """Toggle the is_active flag for a specific product."""
    try:
        incoming_data = request.get_json(silent=True) or {}
        product_id = incoming_data.get("product_id")
        is_active = incoming_data.get("is_active")

        if not product_id or is_active is None:
            return jsonify({"status": "error", "message": "Missing product_id or is_active flag."}), 400

        if not os.path.exists(CONFIG_FILE_PATH):
            return jsonify({"status": "error", "message": "Configuration file not found."}), 404

        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as handle:
            current_config = json.load(handle)

        if product_id not in current_config.get("products", {}):
            return jsonify({"status": "error", "message": f"Product '{product_id}' not found in config."}), 404

        current_config["products"][product_id]["is_active"] = bool(is_active)
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as handle:
            json.dump(current_config, handle, indent=4)

        return jsonify({
            "status": "success",
            "message": f"Product '{product_id}' operational state set to {is_active}.",
            "product_id": product_id,
            "is_active": is_active,
        }), 200
    except Exception as exc:
        logger.exception("Configuration toggle failed: %s", exc)
        return jsonify({"status": "error", "message": f"Toggle failed: {str(exc)}"}), 500


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "on"}
    app.run(host=host, port=port, debug=debug)
