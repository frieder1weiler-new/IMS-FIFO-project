from flask import Flask, render_template, request, jsonify
import json
import os
from datetime import datetime
from core.storage_manager import book_pallet_in, book_pallet_out, is_batch_id_duplicate
from core.data_handler import load_raw_json, get_product_stock

HARDWARE_TRANSACTION_LOGS = [] # In-memory track log for all scanner processing events (Max 50)
CONFIG_FILE_PATH = "data/config.json"
DB_FILE_PATH = "data/stock_db.json"

app = Flask(__name__)

# Helper helper dependency validation check
def load_live_config_products():
    """Reads configuration profiles directly from disk to keep queries dynamic."""
    if not os.path.exists(CONFIG_FILE_PATH):
        return {
            "PROD_A": {"name": "Gearbox Type A", "housing_pallet_qty": 645, "cover_pallet_qty": 1440},
            "PROD_B": {"name": "Drive Unit B", "housing_pallet_qty": 500, "cover_pallet_qty": 1200}
        }
    try:
        with open(CONFIG_FILE_PATH, "r") as f:
            return json.load(f).get("products", {})
    except Exception:
        return {}

def record_log(channel: str, message: str, status: str):
    """Saves transaction histories to rolling memory list for UI diagnostics."""
    HARDWARE_TRANSACTION_LOGS.insert(0, {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "channel": channel,    # "INBOUND" or "OUTBOUND"
        "message": message,
        "status": status       # "SUCCESS" or "ERROR"
    })
    if len(HARDWARE_TRANSACTION_LOGS) > 50:
        HARDWARE_TRANSACTION_LOGS.pop()

# ---------------------------------------------------------
# 📊 WEB UI ROUTES (Serving HTML Pages)
# ---------------------------------------------------------

@app.route('/')
def index():
    # The home route now serves as the Assembly Takeout FIFO dashboard
    return render_template('display.html')

@app.route('/production-input')
def production_input():
    # Dedicated input buffer route
    return render_template('production_input.html')

@app.route('/management')
def management():
    """Renders the Maintenance/Stock keeping control panel."""
    # Pull dynamic parameters directly from live json instead of static python registries
    return render_template('management.html', products=load_live_config_products())

@app.route('/scanner-terminal')
def hw_terminal_listener_view():
    """Serves the hidden background interface tracking active scan endpoints."""
    return render_template('scanner_listener.html')

# ---------------------------------------------------------
# 🔌 API ENDPOINTS (For Barcode Scanners & Frontend JS)
# ---------------------------------------------------------

@app.route('/api/admin/logs', methods=['GET'])
def get_hardware_activity_logs():
    """Provides maintenance view with the absolute latest scan records."""
    return jsonify(HARDWARE_TRANSACTION_LOGS), 200

@app.route('/api/scan/in', methods=['POST'])
def api_scan_in():
    """Endpoint for booking pallets IN."""
    data = request.get_json() or {}
    prod_id = data.get('product_id')
    part_type = data.get('part_type')  
    batch_id = data.get('batch_id')
    fifo_number = data.get('fifo_number')

    print(f"\n[DEBUG API] Incoming Scan Payload: prod_id={prod_id}, part_type={part_type}, batch_id={batch_id}, fifo_number={fifo_number}")

    if not all([prod_id, part_type, batch_id]):
        print("[DEBUG API] Validation Failed: One or more fields are empty!")
        return jsonify({"status": "error", "message": "Missing required scan data fields."}), 400

    if not fifo_number:
        print("[DEBUG API] Validation Failed: fifo_number is missing!")
        return jsonify({"status": "error", "message": "Missing FIFO number. Please scan the physical pallet label."}), 400

    # Strip whitespace from fifo_number
    fifo_number = str(fifo_number).strip()
    if not fifo_number:
        print("[DEBUG API] Validation Failed: fifo_number is empty after stripping!")
        return jsonify({"status": "error", "message": "FIFO number cannot be blank or whitespace only."}), 400

    try:
        book_pallet_in(prod_id, part_type, batch_id, fifo_number)
        record_log("INBOUND", f"Registered {prod_id} ({part_type}) | Batch: {batch_id} | FIFO#: {fifo_number}", "SUCCESS")
        print("[DEBUG API] Successfully booked to inventory file database.")
        return jsonify({"status": "success", "message": f"Pallet {batch_id} (FIFO#{fifo_number}) stored."}), 200
    except Exception as e:
        print(f"[DEBUG API] Storage Exception occurred: {str(e)}")
        record_log("INBOUND", f"Failed Scan Storage: {str(e)}", "ERROR")
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/api/scan/out', methods=['POST'])
def api_scan_out():
    data = request.get_json() or {}
    batch_id = data.get('batch_id')

    if not batch_id:
        return jsonify({"status": "error", "message": "Missing Batch ID."}), 400

    active_products = load_live_config_products()
    fifo_violation = False
    correct_fifo_batch = None
    target_product = ""
    target_type = ""

    # 🔍 OBJECT MAP FIX: Evaluate FIFO rules safely through get_product_stock() class instances
    for prod_id in active_products.keys():
        try:
            stock_obj = get_product_stock(prod_id)
            target_lists = {
                "housings": stock_obj.housings,
                "covers": stock_obj.covers
            }
            
            for p_type, pallets in target_lists.items():
                if not pallets: 
                    continue
                
                # Check using object properties instead of string dictionary keys
                has_batch = any(p.batch_id == batch_id for p in pallets)
                
                if has_batch:
                    sorted_pallets = sorted(pallets, key=lambda x: x.timestamp)
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

    try:
        message = book_pallet_out(batch_id)
        
        if fifo_violation:
            warn_msg = f"⚠️ FIFO VIOLATION: Scanned batch {batch_id} for {target_product} {target_type}. An older batch ({correct_fifo_batch}) was available!"
            record_log("OUTBOUND", warn_msg, "ERROR")
            return jsonify({
                "status": "warning", 
                "message": message, 
                "fifo_violation": True,
                "expected_batch": correct_fifo_batch
            }), 200
            
        record_log("OUTBOUND", f"Batch {batch_id} consumed from line stock.", "SUCCESS")
        return jsonify({"status": "success", "message": message}), 200
    except ValueError as e:
        record_log("OUTBOUND", f"Failed Consumption: {str(e)}", "ERROR")
        return jsonify({"status": "error", "message": str(e)}), 404

@app.route('/api/stock/takeout-fifo', methods=['GET'])
def api_takeout_fifo():
    """Calculates the absolute oldest active batches for FIFO selection per product and part type."""
    active_products = load_live_config_products()
    fifo_matrix = []

    for prod_id, meta in active_products.items():
        try:
            # 🔍 CLASS SCHEMATICS FIX: Rehydrate into ProductStock objects to dodge AttributeError bugs
            stock_obj = get_product_stock(prod_id)
            
            def get_oldest_batch(pallet_list):
                if not pallet_list:
                    return None
                # Sort by object timestamp attribute
                sorted_pallets = sorted(pallet_list, key=lambda x: x.timestamp)
                oldest = sorted_pallets[0]
                
                # Clean up display timestamp formatting smoothly
                display_time = oldest.timestamp.split(".")[0].replace("T", " ") if "T" in oldest.timestamp else oldest.timestamp
                
                return {
                    "fifo_number": oldest.fifo_number,
                    "batch_id": oldest.batch_id,
                    "timestamp": display_time,
                    "qty": oldest.quantity
                }

            fifo_matrix.append({
                "name": meta.get("name", "Unknown Unit"),
                "oldest_housing": get_oldest_batch(stock_obj.housings),
                "oldest_cover": get_oldest_batch(stock_obj.covers)
            })
        except ValueError:
            # Fallback structure if the product exists in config but has no inventory tracking block yet
            fifo_matrix.append({
                "name": meta.get("name", "Unknown Unit"),
                "oldest_housing": None,
                "oldest_cover": None
            })

    return jsonify(fifo_matrix), 200

@app.route('/api/stock/data', methods=['GET'])
def api_stock_data():
    """
    Fetches processed raw calculations for the dashboard interface.
    Returns a calculated breakdown of true assembly sets for every product.
    Includes next-up (oldest) FIFO number for each pallet type.
    Includes operational state and stock thresholds.
    """
    active_products = load_live_config_products()
    compiled_dashboard = {}

    for product_id, meta in active_products.items():
        try:
            # Rehydrate into Our Object Model
            stock_obj = get_product_stock(product_id)
            
            # Extract next-up (oldest) FIFO number for housings and covers
            next_housing_fifo = None
            next_cover_fifo = None
            if stock_obj.housings:
                sorted_housings = sorted(stock_obj.housings, key=lambda x: x.timestamp)
                next_housing_fifo = sorted_housings[0].fifo_number
            if stock_obj.covers:
                sorted_covers = sorted(stock_obj.covers, key=lambda x: x.timestamp)
                next_cover_fifo = sorted_covers[0].fifo_number
            
            compiled_dashboard[product_id] = {
                "name": meta.get("name", "Unknown Unit Module"),
                "is_active": meta.get("is_active", True),
                "total_housings": stock_obj.total_housing_pcs,
                "total_covers": stock_obj.total_cover_pcs,
                "ready_sets": stock_obj.ready_assembly_sets,
                "raw_pallet_count": {
                    "housings": len(stock_obj.housings),
                    "covers": len(stock_obj.covers)
                },
                "next_fifo_numbers": {
                    "housing": next_housing_fifo,
                    "cover": next_cover_fifo
                },
                "thresholds": {
                    "min_stock_pcs": meta.get("min_stock_pcs", 1000),
                    "max_stock_pcs": meta.get("max_stock_pcs", 5000),
                    "housing_pallet_qty": meta.get("housing_pallet_qty", 500),
                    "cover_pallet_qty": meta.get("cover_pallet_qty", 1000)
                }
            }
        except ValueError:
            # Fallback block if profile exists in config but has no active db database transactions yet
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
                    "cover_pallet_qty": meta.get("cover_pallet_qty", 1000)
                }
            }

    return jsonify(compiled_dashboard), 200

@app.route('/api/admin/active-stock', methods=['GET'])
def get_active_stock_manifest():
    """Flattens nested stock database layers to serve data tables directly."""
    if not os.path.exists(DB_FILE_PATH):
        return jsonify([]), 200
        
    try:
        db_data = load_raw_json()
        active_pallets = []
        products_dict = db_data.get("products", {})
        
        for prod_id, prod_meta in products_dict.items():
            stock_node = prod_meta.get("stock", {})
            target_lists = {
                "housing": stock_node.get("housings", []),
                "cover": stock_node.get("covers", [])
            }
            
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
                            "quantity": pallet.get("quantity", 1)
                        })
                        
        active_pallets.sort(key=lambda x: x['timestamp'], reverse=True)
        return jsonify(active_pallets), 200
    except Exception as e:
        print(f"[CRITICAL ERROR] Stock file parsing metrics failed: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/db-status', methods=['GET'])
def api_db_status():
    """Returns storage management diagnostic configuration layers."""
    raw_data = load_raw_json()
    return jsonify({
        "file_path": DB_FILE_PATH,
        "file_exists": os.path.exists(DB_FILE_PATH),
        "tracked_products": list(raw_data.get("products", {}).keys()),
        "raw_structure": raw_data
    }), 200

@app.route('/api/admin/db-reset', methods=['POST'])
def api_db_reset():
    """Wipes the database file back to a clean testing configuration canvas."""
    from core.data_handler import save_raw_json
    save_raw_json({"products": {}})
    return jsonify({"status": "success", "message": "Database cleared successfully."}), 200

@app.route('/api/admin/config', methods=['GET'])
def get_system_config():
    """Reads and returns the active config.json parameters safely."""
    if not os.path.exists(CONFIG_FILE_PATH):
        default_config = {
            "db_file_path": "data/stock_db.json",
            "products": {
                "PROD_A": {"name": "Gearbox Type A", "housing_pallet_qty": 645, "cover_pallet_qty": 1440},
                "PROD_B": {"name": "Drive Unit B", "housing_pallet_qty": 500, "cover_pallet_qty": 1200}
            }
        }
        return jsonify(default_config), 200
        
    try:
        with open(CONFIG_FILE_PATH, "r") as f:
            return jsonify(json.load(f)), 200
    except Exception as e:
        return jsonify({"status": "error", "message": f"Read failed: {str(e)}"}), 500

@app.route('/api/admin/config/save', methods=['POST'])
def save_system_config():
    """Overwrites config.json configuration files while protecting and updating all product properties."""
    try:
        incoming_data = request.get_json()
        if not incoming_data or "products" not in incoming_data:
            return jsonify({"status": "error", "message": "Invalid configuration payload."}), 400
            
        current_config = {}
        if os.path.exists(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, "r") as f:
                current_config = json.load(f)
        else:
            current_config = {"db_file_path": "data/stock_db.json", "products": {}}

        # Merge updates cleanly while preserving and updating all properties
        for prod_id, incoming_meta in incoming_data["products"].items():
            if prod_id in current_config.get("products", {}):
                # Preserve existing config and selectively update fields
                existing = current_config["products"][prod_id]
                existing["name"] = incoming_meta.get("name", existing.get("name", "Unknown"))
                existing["is_active"] = incoming_meta.get("is_active", existing.get("is_active", True))
                
                # Handle numeric fields with validation
                if incoming_meta.get("housing_pallet_qty"):
                    try:
                        existing["housing_pallet_qty"] = int(incoming_meta["housing_pallet_qty"])
                    except (ValueError, TypeError):
                        pass  # Keep existing value if invalid
                        
                if incoming_meta.get("cover_pallet_qty"):
                    try:
                        existing["cover_pallet_qty"] = int(incoming_meta["cover_pallet_qty"])
                    except (ValueError, TypeError):
                        pass
                        
                if incoming_meta.get("min_stock_pcs"):
                    try:
                        existing["min_stock_pcs"] = int(incoming_meta["min_stock_pcs"])
                    except (ValueError, TypeError):
                        pass
                        
                if incoming_meta.get("max_stock_pcs"):
                    try:
                        existing["max_stock_pcs"] = int(incoming_meta["max_stock_pcs"])
                    except (ValueError, TypeError):
                        pass
            else:
                # Create new product entry with defaults
                try:
                    current_config["products"][prod_id] = {
                        "name": incoming_meta.get("name", "Unknown"),
                        "is_active": incoming_meta.get("is_active", True),
                        "housing_pallet_qty": int(incoming_meta.get("housing_pallet_qty", 500)),
                        "cover_pallet_qty": int(incoming_meta.get("cover_pallet_qty", 1000)),
                        "min_stock_pcs": int(incoming_meta.get("min_stock_pcs", 1000)),
                        "max_stock_pcs": int(incoming_meta.get("max_stock_pcs", 5000))
                    }
                except (ValueError, TypeError):
                    # Fallback if numeric conversion fails
                    current_config["products"][prod_id] = {
                        "name": incoming_meta.get("name", "Unknown"),
                        "is_active": incoming_meta.get("is_active", True),
                        "housing_pallet_qty": 500,
                        "cover_pallet_qty": 1000,
                        "min_stock_pcs": 1000,
                        "max_stock_pcs": 5000
                    }

        # Remove products not in the update payload
        for prod_id in list(current_config.get("products", {}).keys()):
            if prod_id not in incoming_data["products"]:
                del current_config["products"][prod_id]

        with open(CONFIG_FILE_PATH, "w") as f:
            json.dump(current_config, f, indent=4)
            
        return jsonify({"status": "success", "message": "System configuration updated safely!"}), 200
    except Exception as e:
        print(f"[CONFIG ERROR] Write validation breakdown: {str(e)}")
        return jsonify({"status": "error", "message": f"Write failed: {str(e)}"}), 500

@app.route('/api/admin/config/toggle-active', methods=['POST'])
def toggle_product_active():
    """Toggles the is_active flag for a specific product."""
    try:
        incoming_data = request.get_json()
        product_id = incoming_data.get("product_id")
        is_active = incoming_data.get("is_active")
        
        if not product_id or is_active is None:
            return jsonify({"status": "error", "message": "Missing product_id or is_active flag."}), 400
        
        current_config = {}
        if os.path.exists(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, "r") as f:
                current_config = json.load(f)
        else:
            return jsonify({"status": "error", "message": "Configuration file not found."}), 404
        
        if product_id not in current_config.get("products", {}):
            return jsonify({"status": "error", "message": f"Product '{product_id}' not found in config."}), 404
        
        # Toggle the is_active flag
        current_config["products"][product_id]["is_active"] = bool(is_active)
        
        with open(CONFIG_FILE_PATH, "w") as f:
            json.dump(current_config, f, indent=4)
        
        return jsonify({
            "status": "success",
            "message": f"Product '{product_id}' operational state set to {is_active}.",
            "product_id": product_id,
            "is_active": is_active
        }), 200
    except Exception as e:
        print(f"[CONFIG ERROR] Toggle operation failed: {str(e)}")
        return jsonify({"status": "error", "message": f"Toggle failed: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)