import sqlite3
import json
import os
import urllib.request
import urllib.error
import time
import uuid
import gc
from datetime import date, timedelta, datetime
from typing import List, Dict, Any, Tuple
import streamlit as st

try:
    import js
    IS_BROWSER = True
except ImportError:
    IS_BROWSER = False

DB_NAME = 'mobile_catalog.db' if os.path.exists('mobile_catalog.db') else 'pokemon_tcg.db'
DELTA_SERVER_URL = "https://pub-81d2f5a4ba9a4821bc03f0c3375f9536.r2.dev/deltas/latest_delta.json"

DEFAULT_SETTINGS = {
    "ui_mode": "Vendor (Retail)",
    "buy_tiers": [
        {"min": 0.0, "max": 2.0, "rate": 50},
        {"min": 2.0, "max": 20.0, "rate": 60},
        {"min": 20.0, "max": 50.0, "rate": 70},
        {"min": 50.0, "max": 150.0, "rate": 75},
        {"min": 150.0, "max": 999999.0, "rate": 80},
    ],
    "condition_ratios": {
        "Near Mint": 1.00,
        "Lightly Played": 0.85,
        "Moderately Played": 0.70,
        "Heavily Played": 0.50,
        "Damaged": 0.30,
        "Unknown": 1.00
    },
    "sticker_rules": {
        "mode": "Custom Cutoff",
        "cutoff_threshold": 0.30,
        "min_sticker_price": 1.00
    }
}

# --- SERVICE WORKER HARD DISK BRIDGES ---
def _hard_save(key: str, data: Any):
    if not IS_BROWSER: return
    try:
        origin = js.self.location.origin
        url = f"{origin}/offline-db/save"
        payload = json.dumps({"key": key, "value": data}).encode('utf-8')
        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass

def _hard_load(key: str) -> Any:
    if not IS_BROWSER: return None
    try:
        origin = js.self.location.origin
        url = f"{origin}/offline-db/load?key={key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as res:
            response_data = json.loads(res.read().decode('utf-8'))
            return response_data.get("value")
    except Exception:
        return None

def get_beta_key() -> str:
    # 1. Prioritize active Streamlit session (fixes local desktop testing)
    if "beta_key" in st.session_state and st.session_state.beta_key:
        return st.session_state.beta_key
        
    # 2. Fallback to browser hard-disk (PWA)
    if IS_BROWSER:
        key = _hard_load("pokequant_beta_key")
        if key: return str(key)
        
    return "default_vendor"

# --- LOCAL STORAGE ENGINE ---
def load_local_inventory() -> List[Dict[str, Any]]:
    data = _hard_load("pokequant_inventory") if IS_BROWSER else None
    
    if not data and os.path.exists("local_inv.json"):
        try:
            with open("local_inv.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
            
    return data if data else []

def save_local_inventory(inventory_list: List[Dict[str, Any]]):
    if IS_BROWSER:
        _hard_save("pokequant_inventory", inventory_list)
        
    try:
        with open("local_inv.json", "w", encoding="utf-8") as f:
            json.dump(inventory_list, f, indent=2)
    except Exception:
        pass

def get_pending_syncs() -> List[Dict[str, Any]]:
    data = _hard_load("pokequant_pending_sync") if IS_BROWSER else None
    
    if not data and os.path.exists("local_syncs.json"):
        try:
            with open("local_syncs.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
            
    return data if data else []

def add_pending_sync(sql: str, args: list):
    syncs = get_pending_syncs()
    syncs.append({"sql": sql, "args": args})
    
    if IS_BROWSER:
        _hard_save("pokequant_pending_sync", syncs)
        
    try:
        with open("local_syncs.json", "w", encoding="utf-8") as f:
            json.dump(syncs, f, indent=2)
    except Exception:
        pass

def get_pending_sync_count() -> int:
    return len(get_pending_syncs())

# --- GLOBAL SYNC TIMESTAMPS ---
def get_local_sync_time() -> float:
    data = _hard_load("pokequant_sync_time") if IS_BROWSER else None
    if data is not None:
        return float(data)
            
    if os.path.exists("local_sync_time.json"):
        try:
            with open("local_sync_time.json", "r") as f:
                return float(json.load(f).get("last_sync", 0.0))
        except Exception:
            pass
            
    return 0.0

def save_local_sync_time(timestamp: float):
    if IS_BROWSER:
        _hard_save("pokequant_sync_time", str(timestamp))
            
    try:
        with open("local_sync_time.json", "w") as f:
            json.dump({"last_sync": timestamp}, f)
    except Exception:
        pass

def get_remote_sync_time() -> float:
    try:
        res = turso_execute_sync([
            {"sql": "CREATE TABLE IF NOT EXISTS sync_metadata (id INTEGER PRIMARY KEY, last_updated REAL)", "args": []},
            {"sql": "SELECT last_updated FROM sync_metadata WHERE id = 1", "args": []}
        ])
        if len(res) > 1 and len(res[1]) > 0:
            return float(res[1][0].get("last_updated", 0.0))
    except Exception:
        pass
    return 0.0

# --- TURSO HTTP REST CLIENT ---
def get_turso_credentials() -> Tuple[str, str]:
    url, token = "", ""
    if IS_BROWSER:
        url = _hard_load("turso_url") or ""
        token = _hard_load("turso_token") or ""
            
    if not url or not token:
        if os.path.exists("local_creds.json"):
            try:
                with open("local_creds.json", "r", encoding="utf-8") as f:
                    creds = json.load(f)
                    url = url or creds.get("url", "")
                    token = token or creds.get("token", "")
            except Exception:
                pass
    
    if not url or not token:
        try:
            url = url or st.secrets.get("TURSO_DATABASE_URL", "")
            token = token or st.secrets.get("TURSO_AUTH_TOKEN", "")
        except Exception:
            pass
            
    if url:
        url = url.strip().replace("libsql://", "https://")
        if not url.startswith("http"):
            url = f"https://{url}"
            
    return url, token.strip() if token else ""

def save_turso_credentials(url: str, token: str):
    if IS_BROWSER:
        _hard_save("turso_url", url.strip())
        _hard_save("turso_token", token.strip())
            
    try:
        with open("local_creds.json", "w", encoding="utf-8") as f:
            json.dump({"url": url.strip(), "token": token.strip()}, f)
    except Exception:
        pass

def parse_turso_results(response_text: str) -> List[List[Dict[str, Any]]]:
    data = json.loads(response_text)
    results = []
    
    for res in data.get("results", []):
        if res.get("type") == "ok" and "response" in res:
            resp = res["response"]
            if resp.get("type") == "execute" and "result" in resp:
                cols = [c["name"] for c in resp["result"].get("cols", [])]
                rows = []
                for row in resp["result"].get("rows", []):
                    row_dict = {}
                    for col_name, cell in zip(cols, row):
                        val = cell.get("value")
                        if cell.get("type") == "integer": 
                            val = int(val) if val is not None else None
                        elif cell.get("type") == "float": 
                            val = float(val) if val is not None else None
                        row_dict[col_name] = val
                    rows.append(row_dict)
                results.append(rows)
    return results

def turso_execute_sync(statements: List[Dict[str, Any]], override_url: str = None, override_token: str = None) -> List[List[Dict[str, Any]]]:
    url, token = get_turso_credentials()
    
    if override_url is not None:
        url = override_url
    if override_token is not None:
        token = override_token
        
    if not url:
        raise Exception("Missing Database URL (Cloudflare Worker endpoint). Set it in Vendor Settings.")
        
    endpoint = f"{url.rstrip('/')}/v2/pipeline"
    if url.endswith(".workers.dev"):
        endpoint = url.rstrip('/') # Use root for Cloudflare Worker proxy
        
    requests_payload = []
    
    for stmt in statements:
        turso_args = []
        for arg in stmt.get("args", []):
            if isinstance(arg, bool): 
                turso_args.append({"type": "integer", "value": str(int(arg))})
            elif isinstance(arg, int): 
                turso_args.append({"type": "integer", "value": str(arg)})
            elif isinstance(arg, float): 
                turso_args.append({"type": "float", "value": float(arg)})
            elif arg is None: 
                turso_args.append({"type": "null"})
            else: 
                turso_args.append({"type": "text", "value": str(arg)})
        
        requests_payload.append({
            "type": "execute",
            "stmt": {"sql": stmt["sql"], "args": turso_args}
        })
    requests_payload.append({"type": "close"})
    
    payload_json = json.dumps({"requests": requests_payload})
    beta_key = get_beta_key()
    
    if IS_BROWSER:
        try:
            req = js.XMLHttpRequest.new()
            req.open("POST", endpoint, False)
            if token:
                req.setRequestHeader("Authorization", f"Bearer {token}")
            req.setRequestHeader("X-Beta-Key", beta_key)
            req.setRequestHeader("Content-Type", "application/json")
            req.send(payload_json)
            
            if req.status >= 400:
                raise Exception(f"HTTP {req.status}: {req.responseText}")
            res_text = req.responseText
        except Exception as e:
            raise Exception(f"Browser Network Error: {str(e)}")
    else:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 PokeQuant/1.0",
            "X-Beta-Key": beta_key
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
            
        req = urllib.request.Request(
            endpoint, 
            data=payload_json.encode("utf-8"), 
            headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                res_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise Exception(f"HTTP {e.code}: {err_body}")
        except Exception as e:
            raise Exception(f"Network Error: {str(e)}")
            
    res_data = json.loads(res_text)
    for res in res_data.get("results", []):
        if res.get("type") == "error":
            err_msg = res.get("error", {}).get("message", "Unknown Turso Error")
            raise Exception(err_msg)
            
    return parse_turso_results(res_text)

def sync_with_cloud() -> Tuple[bool, str]:
    try:
        syncs = get_pending_syncs()
        push_time = time.time()
        beta_key = get_beta_key()
        
        if syncs:
            syncs.insert(0, {"sql": "CREATE TABLE IF NOT EXISTS sync_metadata (id INTEGER PRIMARY KEY, last_updated REAL)", "args": []})
            syncs.append({"sql": "INSERT OR REPLACE INTO sync_metadata (id, last_updated) VALUES (1, ?)", "args": [push_time]})
            
            turso_execute_sync(syncs)
            
            if IS_BROWSER:
                _hard_save("pokequant_pending_sync", [])
            
            try:
                with open("local_syncs.json", "w", encoding="utf-8") as f:
                    json.dump([], f)
            except Exception: pass
                
        init_stmts = [
            {
                "sql": """
                CREATE TABLE IF NOT EXISTS inventory (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    product_id INTEGER,
                    card_name TEXT,
                    card_number TEXT,
                    set_name TEXT,
                    variant TEXT,
                    condition TEXT,
                    purchase_price REAL,
                    sticker_price REAL,
                    date_bought TEXT,
                    is_bulk_deal INTEGER,
                    is_sold INTEGER DEFAULT 0,
                    sold_price REAL DEFAULT 0.0,
                    date_sold TEXT DEFAULT '',
                    custom_image_data TEXT,
                    is_deleted INTEGER DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """,
                "args": []
            },
            {"sql": "CREATE TABLE IF NOT EXISTS vendor_settings (user_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL, updated_at REAL DEFAULT 0.0)", "args": []},
            {"sql": "CREATE TABLE IF NOT EXISTS sync_metadata (id INTEGER PRIMARY KEY, last_updated REAL)", "args": []}
        ]
        turso_execute_sync(init_stmts)
        
        # Migrations to support UUID/LWW & Tenancy on legacy tables
        try: turso_execute_sync([{"sql": f"ALTER TABLE inventory ADD COLUMN user_id TEXT DEFAULT '{beta_key}'", "args": []}])
        except Exception: pass
        
        # Auto-heal orphaned desktop data
        try: turso_execute_sync([{"sql": "UPDATE inventory SET user_id = ? WHERE user_id = 'default_vendor'", "args": [beta_key]}])
        except Exception: pass
        try: turso_execute_sync([{"sql": "UPDATE vendor_settings SET user_id = ? WHERE user_id = 'default_vendor'", "args": [beta_key]}])
        except Exception: pass
        
        try: turso_execute_sync([{"sql": "ALTER TABLE inventory ADD COLUMN is_deleted INTEGER DEFAULT 0", "args": []}])
        except Exception: pass
        try: turso_execute_sync([{"sql": "ALTER TABLE inventory ADD COLUMN updated_at REAL DEFAULT 0.0", "args": []}])
        except Exception: pass
        try: turso_execute_sync([{"sql": "ALTER TABLE inventory ADD COLUMN custom_image_data TEXT", "args": []}])
        except Exception: pass
        try: turso_execute_sync([{"sql": "ALTER TABLE inventory ADD COLUMN sold_price REAL DEFAULT 0.0", "args": []}])
        except Exception: pass
        try: turso_execute_sync([{"sql": "ALTER TABLE inventory ADD COLUMN date_sold TEXT", "args": []}])
        except Exception: pass
        try: turso_execute_sync([{"sql": "ALTER TABLE inventory ADD COLUMN is_sold INTEGER DEFAULT 0", "args": []}])
        except Exception: pass
        try: turso_execute_sync([{"sql": "ALTER TABLE vendor_settings ADD COLUMN updated_at REAL DEFAULT 0.0", "args": []}])
        except Exception: pass
        
        pull_stmts = [
            {"sql": "SELECT * FROM inventory WHERE is_deleted = 0 AND user_id = ? ORDER BY updated_at DESC", "args": [beta_key]},
            {"sql": "SELECT settings_json FROM vendor_settings WHERE user_id = ?", "args": [beta_key]},
            {"sql": "SELECT last_updated FROM sync_metadata WHERE id = 1", "args": []}
        ]
        results = turso_execute_sync(pull_stmts)
        
        if len(results) > 0:
            merge_cloud_inventory(results[0])
            
        if len(results) > 1 and len(results[1]) > 0:
            settings_json = results[1][0].get("settings_json")
            if settings_json:
                if isinstance(settings_json, str):
                    try:
                        settings_obj = json.loads(settings_json)
                        if IS_BROWSER:
                            _hard_save("pokequant_vendor_settings", settings_obj)
                    except:
                        pass
                try:
                    with open("local_settings.json", "w", encoding="utf-8") as f:
                        f.write(settings_json)
                except Exception: pass

        if len(results) > 2 and len(results[2]) > 0:
            remote_time = float(results[2][0].get("last_updated", push_time))
            save_local_sync_time(remote_time)
        else:
            save_local_sync_time(push_time)
                
        return True, "Cloud sync complete!"
    except Exception as e:
        return False, str(e)

# --- DAILY CATALOG DELTA ENGINE ---
def apply_daily_catalog_delta() -> Tuple[bool, str]:
    if not os.path.exists(DB_NAME):
        return False, "Catalog DB not mounted."

    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        if 'cards' not in tables or 'price_history' not in tables:
            return False, "Catalog database missing required tables."
    except Exception:
        return False, "Catalog database missing required tables."

    try:
        delta_url = f"{DELTA_SERVER_URL}?t={int(time.time())}"
        headers = {
            'User-Agent': 'PokeQuant-PWA',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }

        if IS_BROWSER:
            req = js.XMLHttpRequest.new()
            req.open("GET", delta_url, False)
            req.setRequestHeader("User-Agent", headers['User-Agent'])
            req.setRequestHeader("Cache-Control", headers['Cache-Control'])
            req.setRequestHeader("Pragma", headers['Pragma'])
            req.send()
            if req.status >= 400:
                raise Exception(f"HTTP {req.status}: {req.responseText}")
            raw_data = req.responseText
        else:
            req = urllib.request.Request(delta_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                raw_data = response.read().decode('utf-8')

        delta_data = json.loads(raw_data)

        # Immediately free the raw string memory
        del raw_data
        gc.collect()

        new_cards = delta_data.get("new_cards", [])
        price_updates = delta_data.get("price_updates", [])
        delta_date = delta_data.get("delta_date", "Today")

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        # PREVENT MOBILE MEMORY CRASH: Disable the massive SQLite rollback journal
        cursor.execute("PRAGMA journal_mode = OFF")
        cursor.execute("PRAGMA synchronous = OFF")

        # Process the inserts in small chunks of 500 so iOS/Android doesn't run out of RAM
        def chunker(seq, size):
            return (seq[pos:pos + size] for pos in range(0, len(seq), size))

        if new_cards:
            for batch in chunker(new_cards, 500):
                cursor.executemany(
                    "INSERT OR REPLACE INTO cards (product_id, card_name, card_number, set_name, rarity) VALUES (?, ?, ?, ?, ?)",
                    [(c["product_id"], c["card_name"], c["card_number"], c["set_name"], c["rarity"]) for c in batch]
                )
                conn.commit()

        if price_updates:
            for batch in chunker(price_updates, 500):
                cursor.executemany(
                    "INSERT OR REPLACE INTO price_history (product_id, sub_type, market_price, date) VALUES (?, ?, ?, ?)",
                    [(p["product_id"], p["sub_type"], float(p["market_price"]), p["date"]) for p in batch]
                )
                conn.commit()

        conn.close()

        # Force Python garbage collection to clean up the browser memory
        del new_cards
        del price_updates
        del delta_data
        gc.collect()

        _hard_save("pokequant_last_catalog_delta", delta_date)
        return True, f"Successfully patched cards and prices ({delta_date})!"
    except Exception as e:
        return False, f"Delta update failed: {str(e)}"

# --- INVENTORY CRUD OPERATIONS (LWW + UUID) ---
def get_inventory() -> List[Dict[str, Any]]:
    inventory_list = load_local_inventory()
    if not inventory_list: 
        return []

    product_ids = list({item['product_id'] for item in inventory_list if item.get('product_id', 0) > 0})
    if product_ids and os.path.exists(DB_NAME):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(product_ids))
            
            cursor.execute(f"SELECT product_id, rarity FROM cards WHERE product_id IN ({placeholders})", product_ids)
            rarity_map = dict(cursor.fetchall())
            
            cursor.execute(f"SELECT product_id, sub_type, market_price, date FROM price_history WHERE product_id IN ({placeholders}) ORDER BY date DESC", product_ids)
            
            price_history_map = {}
            for pid, stype, mp, dt in cursor.fetchall():
                if pid not in price_history_map: 
                    price_history_map[pid] = {}
                if stype not in price_history_map[pid]: 
                    price_history_map[pid][stype] = []
                price_history_map[pid][stype].append((str(dt), float(mp)))
            conn.close()
            
            for item in inventory_list:
                pid = item['product_id']
                item['rarity'] = rarity_map.get(pid, 'Promo' if 'Promo' in str(item.get('set_name', '')) else 'N/A')
                
                var = item.get('variant', 'Normal')
                var_history = price_history_map.get(pid, {}).get(var)
                if not var_history and pid in price_history_map and price_history_map[pid]:
                    var_history = list(price_history_map[pid].values())[0]
                    
                if var_history:
                    latest_date_str, latest_price = var_history[0]
                    item['live_market'] = latest_price
                    item['market_date'] = latest_date_str.split(" ")[0].split("T")[0]
                    try: 
                        latest_date_obj = date.fromisoformat(item['market_date'])
                    except ValueError: 
                        latest_date_obj = date.today()
                        
                    def get_past_price(days_back):
                        target = (latest_date_obj - timedelta(days=days_back)).isoformat()
                        for d_str, pr in var_history:
                            if d_str.split(" ")[0].split("T")[0] <= target: 
                                return pr
                        return var_history[-1][1]

                    item['market_1d'] = get_past_price(1)
                    item['market_3d'] = get_past_price(3)
                    item['market_7d'] = get_past_price(7)
                else:
                    item['live_market'], item['market_date'] = 0.0, "N/A"
                    item['market_1d'], item['market_3d'], item['market_7d'] = 0.0, 0.0, 0.0
        except Exception:
            for item in inventory_list:
                item['rarity'], item['live_market'], item['market_date'] = item.get('rarity', 'N/A'), 0.0, "N/A"
                item['market_1d'], item['market_3d'], item['market_7d'] = 0.0, 0.0, 0.0
    else:
        for item in inventory_list:
            item['rarity'], item['live_market'], item['market_date'] = item.get('rarity', 'N/A'), 0.0, "N/A"
            item['market_1d'], item['market_3d'], item['market_7d'] = 0.0, 0.0, 0.0

    return inventory_list

def add_inventory_item(product_id, card_name, card_number, set_name, variant, condition, purchase_price, sticker_price, date_bought, is_bulk, custom_image_data=None):
    now_ts = time.time()
    item_id = uuid.uuid4().hex
    bulk_int = 1 if is_bulk else 0
    beta_key = get_beta_key()
    
    sql = """
    INSERT INTO inventory (id, user_id, product_id, card_name, card_number, set_name, variant, condition, purchase_price, sticker_price, date_bought, is_bulk_deal, is_sold, custom_image_data, is_deleted, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    args = [item_id, beta_key, product_id, card_name, card_number, set_name, variant, condition, purchase_price, sticker_price, str(date_bought), bulk_int, 0, custom_image_data, 0, now_ts]
    add_pending_sync(sql, args)
    
    local_inv = load_local_inventory()
    local_inv.insert(0, {
        "id": item_id,
        "product_id": product_id, "card_name": card_name, "card_number": card_number,
        "set_name": set_name, "variant": variant, "condition": condition,
        "purchase_price": purchase_price, "sticker_price": sticker_price,
        "date_bought": str(date_bought), "is_bulk_deal": bulk_int, "is_sold": 0,
        "custom_image_data": custom_image_data, "sold_price": 0.0, "date_sold": "",
        "is_deleted": 0, "updated_at": now_ts
    })
    save_local_inventory(local_inv)

def mark_inventory_sold(item_ids: List[str], sold_price_per_item: float, date_sold: str):
    if not item_ids: return
    now_ts = time.time()
    local_inv = load_local_inventory()
    
    for item_id in item_ids:
        add_pending_sync(
            "UPDATE inventory SET is_sold = 1, sold_price = ?, date_sold = ?, updated_at = ? WHERE id = ?",
            [float(sold_price_per_item), str(date_sold), now_ts, str(item_id)]
        )
        for item in local_inv:
            if str(item.get("id")) == str(item_id):
                item["is_sold"], item["sold_price"], item["date_sold"], item["updated_at"] = 1, float(sold_price_per_item), str(date_sold), now_ts
    save_local_inventory(local_inv)

def undo_inventory_sale(item_id: str):
    now_ts = time.time()
    add_pending_sync("UPDATE inventory SET is_sold = 0, sold_price = 0.0, date_sold = '', updated_at = ? WHERE id = ?", [now_ts, str(item_id)])
    local_inv = load_local_inventory()
    for item in local_inv:
        if str(item.get("id")) == str(item_id):
            item["is_sold"], item["sold_price"], item["date_sold"], item["updated_at"] = 0, 0.0, "", now_ts
    save_local_inventory(local_inv)

def update_inventory_bulk(edited_df):
    now_ts = time.time()
    local_inv = load_local_inventory()
    for _, row in edited_df.iterrows():
        bulk_int = 1 if row["Bulk Deal"] else 0
        add_pending_sync(
            "UPDATE inventory SET condition = ?, purchase_price = ?, sticker_price = ?, is_bulk_deal = ?, date_bought = ?, updated_at = ? WHERE id = ?", 
            [row["Condition"], float(row["Paid ($)"]), float(row["Sticker ($)"]), bulk_int, str(row["Date"]), now_ts, str(row["ID"])]
        )
        for item in local_inv:
            if str(item.get("id")) == str(row["ID"]):
                item["condition"], item["purchase_price"], item["sticker_price"], item["is_bulk_deal"], item["date_bought"], item["updated_at"] = row["Condition"], float(row["Paid ($)"]), float(row["Sticker ($)"]), bulk_int, str(row["Date"]), now_ts
    save_local_inventory(local_inv)

def update_inventory_item_full(item_id: str, product_id: int, card_name: str, card_number: str, set_name: str, variant: str, condition: str, purchase_price: float, sticker_price: float, date_bought: str, custom_image_data: str = None):
    now_ts = time.time()
    add_pending_sync(
        "UPDATE inventory SET product_id = ?, card_name = ?, card_number = ?, set_name = ?, variant = ?, condition = ?, purchase_price = ?, sticker_price = ?, date_bought = ?, custom_image_data = ?, updated_at = ? WHERE id = ?", 
        [product_id, card_name, card_number, set_name, variant, condition, purchase_price, sticker_price, str(date_bought), custom_image_data, now_ts, str(item_id)]
    )
    local_inv = load_local_inventory()
    for item in local_inv:
        if str(item.get("id")) == str(item_id):
            item["product_id"], item["card_name"], item["card_number"], item["set_name"], item["variant"], item["condition"], item["purchase_price"], item["sticker_price"], item["date_bought"], item["custom_image_data"], item["updated_at"] = product_id, card_name, card_number, set_name, variant, condition, purchase_price, sticker_price, str(date_bought), custom_image_data, now_ts
    save_local_inventory(local_inv)

def delete_inventory_items_bulk(item_ids: List[str]):
    if not item_ids: return
    now_ts = time.time()
    local_inv = load_local_inventory()
    
    for item_id in item_ids:
        add_pending_sync("UPDATE inventory SET is_deleted = 1, updated_at = ? WHERE id = ?", [now_ts, str(item_id)])
        local_inv = [i for i in local_inv if str(i.get("id")) != str(item_id)]
    save_local_inventory(local_inv)

def update_sticker_prices_bulk(updates: List[Tuple[float, str]]):
    if not updates: return
    now_ts = time.time()
    local_inv = load_local_inventory()
    for new_sticker, item_id in updates:
        add_pending_sync("UPDATE inventory SET sticker_price = ?, updated_at = ? WHERE id = ?", [float(new_sticker), now_ts, str(item_id)])
        for item in local_inv:
            if str(item.get("id")) == str(item_id):
                item["sticker_price"], item["updated_at"] = float(new_sticker), now_ts
    save_local_inventory(local_inv)

def merge_cloud_inventory(remote_items: List[Dict[str, Any]]):
    local_inv = load_local_inventory()
    local_map = {str(item["id"]): item for item in local_inv}
    
    for r in remote_items:
        r_id = str(r["id"])
        r_updated = float(r.get("updated_at", 0.0))
        r_deleted = int(r.get("is_deleted", 0))
        
        if r_id in local_map:
            l_updated = float(local_map[r_id].get("updated_at", 0.0))
            if r_updated >= l_updated:
                if r_deleted == 1:
                    del local_map[r_id]
                else:
                    local_map[r_id] = r
        else:
            if r_deleted == 0:
                local_map[r_id] = r

    merged = sorted(list(local_map.values()), key=lambda x: x.get("updated_at", 0.0), reverse=True)
    save_local_inventory(merged)

# --- CONFIG AND LOCAL DB SEARCH ---
def get_vendor_settings(user_id: str = "default_vendor") -> dict:
    data = _hard_load("pokequant_vendor_settings") if IS_BROWSER else None
    
    if data and isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            pass

    if not data and os.path.exists("local_settings.json"):
        try:
            with open("local_settings.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
            
    return data if data else DEFAULT_SETTINGS

def save_vendor_settings(settings: dict, user_id: str = "default_vendor"):
    if IS_BROWSER: 
        _hard_save("pokequant_vendor_settings", settings)
        
    try:
        with open("local_settings.json", "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass
        
    now_ts = time.time()
    beta_key = get_beta_key()
    add_pending_sync("INSERT OR REPLACE INTO vendor_settings (user_id, settings_json, updated_at) VALUES (?, ?, ?)", [beta_key, json.dumps(settings), now_ts])

def get_last_updated_date() -> str:
    if not os.path.exists(DB_NAME):
        return "N/A"
    try:
        conn = sqlite3.connect(DB_NAME, timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT date FROM price_history ORDER BY date DESC, rowid DESC LIMIT 1")
        res = cursor.fetchone()
        conn.close()
        
        if res and res[0]:
            raw_date = str(res[0]).strip()
            clean_date = raw_date.replace("T", " ").split(".")[0]
            try:
                if " " in clean_date:
                    return datetime.strptime(clean_date, "%Y-%m-%d %H:%M:%S").strftime("%b %d, %Y %I:%M %p")
                else:
                    return datetime.strptime(clean_date, "%Y-%m-%d").strftime("%b %d, %Y")
            except Exception:
                return raw_date
        return "N/A"
    except Exception:
        return "N/A"

def calculate_buy_offer(market_price: float, buy_tiers: list = None) -> Dict[str, Any]:
    if buy_tiers is None: 
        buy_tiers = DEFAULT_SETTINGS["buy_tiers"]
    if market_price is None or market_price <= 0: 
        return {"buy_rate_pct": 0, "cash_offer": 0.0}
    rate = 60
    for tier in buy_tiers:
        if tier["min"] <= market_price < tier["max"]:
            rate = tier["rate"]
            break
    return {"buy_rate_pct": int(rate), "cash_offer": round(market_price * (rate / 100.0), 2)}

def search_cards_paginated(query: str = "", rarity: str = "All", max_price: float = 0.0, product_type: str = "All", sort_by: str = "Newest", page: int = 1, page_size: int = 20, buy_tiers: list = None) -> Tuple[List[Dict[str, Any]], int, int]:
    if not os.path.exists(DB_NAME):
        return [], 1, 0

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    sql_from = "FROM cards c WHERE 1=1"
    params = []

    if query:
        for word in query.split():
            clean_word = word.replace("'", "").replace("-", "").replace(".", "")
            sql_from += " AND (REPLACE(REPLACE(REPLACE(c.card_name, '''', ''), '-', ''), '.', '') LIKE ? OR REPLACE(c.card_number, '-', '') LIKE ? OR REPLACE(REPLACE(REPLACE(c.set_name, '''', ''), '-', ''), '.', '') LIKE ?)"
            params.extend([f"%{clean_word}%", f"%{clean_word}%", f"%{clean_word}%"])

    if rarity and rarity != "All":
        sql_from += " AND c.rarity = ?"
        params.append(rarity)

    if product_type == "Cards Only": 
        sql_from += " AND c.card_number != 'N/A'"
    elif product_type == "Sealed Only": 
        sql_from += " AND c.card_number = 'N/A'"

    if max_price > 0:
        sql_from += " AND EXISTS (SELECT 1 FROM price_history p1 WHERE p1.product_id = c.product_id AND p1.market_price <= ? AND p1.date = (SELECT MAX(p2.date) FROM price_history p2 WHERE p2.product_id = p1.product_id AND p2.sub_type = p1.sub_type))"
        params.append(max_price)

    cursor.execute(f"SELECT COUNT(*) {sql_from}", params)
    total_cards = cursor.fetchone()[0]
    total_pages = max(1, (total_cards + page_size - 1) // page_size)

    order_params = []
    if sort_by == "Oldest": 
        order_clause = "ORDER BY c.product_id ASC"
    elif sort_by == "Price: High to Low": 
        if max_price > 0:
            order_clause = "ORDER BY (SELECT MAX(p.market_price) FROM price_history p WHERE p.product_id = c.product_id AND p.market_price <= ? AND p.date = (SELECT MAX(date) FROM price_history WHERE product_id = c.product_id)) DESC NULLS LAST"
            order_params.append(max_price)
        else:
            order_clause = "ORDER BY (SELECT MAX(p.market_price) FROM price_history p WHERE p.product_id = c.product_id AND p.date = (SELECT MAX(date) FROM price_history WHERE product_id = c.product_id)) DESC NULLS LAST"
    elif sort_by == "Price: Low to High": 
        if max_price > 0:
            order_clause = "ORDER BY (SELECT MIN(p.market_price) FROM price_history p WHERE p.product_id = c.product_id AND p.market_price <= ? AND p.date = (SELECT MAX(date) FROM price_history WHERE product_id = c.product_id)) ASC NULLS LAST"
            order_params.append(max_price)
        else:
            order_clause = "ORDER BY (SELECT MIN(p.market_price) FROM price_history p WHERE p.product_id = c.product_id AND p.date = (SELECT MAX(date) FROM price_history WHERE product_id = c.product_id)) ASC NULLS LAST"
    else: 
        order_clause = "ORDER BY c.product_id DESC"

    cursor.execute("PRAGMA table_info(cards)")
    columns = [info[1] for info in cursor.fetchall()]
    has_img = 'image_base64' in columns

    offset = (page - 1) * page_size
    query_sql = f"SELECT c.product_id, c.card_name, c.card_number, c.set_name, c.image_base64 {sql_from} {order_clause} LIMIT ? OFFSET ?" if has_img else f"SELECT c.product_id, c.card_name, c.card_number, c.set_name {sql_from} {order_clause} LIMIT ? OFFSET ?"
        
    cursor.execute(query_sql, params + order_params + [page_size, offset])
    matched_cards = cursor.fetchall()
    results = []

    for row in matched_cards:
        product_id, name, number, c_set = row[0], row[1], row[2], row[3]
        img_b64 = row[4] if has_img else None
        
        cursor.execute("SELECT sub_type, market_price, date FROM price_history WHERE product_id = ? ORDER BY date DESC", (product_id,))
        all_records = cursor.fetchall()
        if not all_records: continue

        subtypes = {}
        for sub_type, price, dt in all_records:
            if sub_type not in subtypes: 
                subtypes[sub_type] = {"latest_price": price, "latest_date": dt, "history_points": []}
            subtypes[sub_type]["history_points"].append((dt, price))

        variants_data = []
        for sub_type, p_info in subtypes.items():
            market_price = p_info["latest_price"]
            if max_price > 0 and market_price > max_price: 
                continue
                
            buy_data = calculate_buy_offer(market_price, buy_tiers)
            latest_date_str = p_info["latest_date"]
            try: 
                latest_date_obj = date.fromisoformat(latest_date_str.split(" ")[0])
            except ValueError: 
                latest_date_obj = date.today()
                
            history = p_info["history_points"]
            window_90_prices = []
            for dt_str, pr in history:
                try:
                    d_obj = date.fromisoformat(dt_str.split(" ")[0])
                    if (latest_date_obj - d_obj).days <= 90: 
                        window_90_prices.append(pr)
                except Exception: 
                    pass
            
            high_90 = max(window_90_prices) if window_90_prices else market_price
            low_90 = min(window_90_prices) if window_90_prices else market_price
            
            def get_trend(days_back):
                target_date = (latest_date_obj - timedelta(days=days_back)).isoformat()
                past_price = None
                for dt_str, pr in history:
                    if dt_str.split(" ")[0] <= target_date:
                        past_price = pr
                        break
                if past_price is None: return "N/A"
                if past_price == 0: return "0.0%"
                return f"{(((market_price - past_price) / past_price) * 100):+.2f}%"

            variants_data.append({
                "variant": sub_type, "market_price": market_price, "buy_percentage": f"{buy_data['buy_rate_pct']}%",
                "cash_offer": buy_data["cash_offer"], "1d_trend": get_trend(1), "3d_trend": get_trend(3), 
                "7d_trend": get_trend(7), "30d_trend": get_trend(30), "90d_trend": get_trend(90), 
                "90d_high": high_90, "90d_low": low_90, "last_updated": latest_date_str
            })
            
        if variants_data:
            results.append({"product_id": product_id, "card_name": name, "card_number": number, "set": c_set, "pricing": variants_data, "image_base64": img_b64})

    conn.close()
    return results, total_pages, total_cards

def search_card_and_pricing(query: str, limit: int = 1, buy_tiers: list = None) -> List[Dict[str, Any]]:
    results, _, _ = search_cards_paginated(query=query, page_size=limit, buy_tiers=buy_tiers)
    return results