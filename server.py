"""
ATI Motors US Operations — Finance Dashboard Server
Run: python3 server.py
Then open: http://localhost:5000
"""

import json
import os
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request
import requests

app = Flask(__name__,
            static_folder=os.path.dirname(os.path.abspath(__file__)),
            static_url_path="")

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "erpnext_config.json")

def load_config(instance="us"):
    # Try local config file first (development)
    try:
        with open(CONFIG_PATH) as f:
            raw = f.read()
        parts = raw.strip().split("\n\n")
        configs = [json.loads(p) for p in parts if p.strip()]
        if instance == "india":
            return configs[0]
        return configs[1] if len(configs) > 1 else configs[0]
    except FileNotFoundError:
        pass
    # Fall back to environment variables (Vercel / production)
    if instance == "india":
        return {
            "site_url":   os.environ.get("ERP_INDIA_SITE_URL", ""),
            "api_key":    os.environ.get("ERP_INDIA_API_KEY", ""),
            "api_secret": os.environ.get("ERP_INDIA_API_SECRET", ""),
        }
    return {
        "site_url":   os.environ.get("ERP_US_SITE_URL", ""),
        "api_key":    os.environ.get("ERP_US_API_KEY", ""),
        "api_secret": os.environ.get("ERP_US_API_SECRET", ""),
    }

def erp_headers(cfg):
    return {
        "Authorization": f"token {cfg['api_key']}:{cfg['api_secret']}",
        "Accept": "application/json",
    }

def strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()

# ── Categories ──────────────────────────────────────────────────────────────────
# Non-customer categories stay hardcoded — these are internal ERP location names
# that won't change based on Google Sheet data.
_FIXED_CATEGORIES = {
    "Business Development": [
        "Business Development - Sales Meeting",
        "Demo Meeting - USA", "Demo Meeting - Mexico", "Demo Meeting - Thailand",
    ],
    "Exhibitions & Events": ["Exhibition & Events"],
    "ATI Internal": ["ATI - Internal"],
}

# Customer categories — loaded from the Google Sheet foundation at startup.
# Falls back to empty lists if the sheet is unreachable.
_CUSTOMER_CATEGORIES = {
    "US Customers": [],
    "International Customers": [],
}

def _build_categories():
    """Merge customer lists (from foundation) with fixed internal categories."""
    cats = {}
    cats.update(_CUSTOMER_CATEGORIES)
    cats.update(_FIXED_CATEGORIES)
    return cats

CATEGORIES = _build_categories()

def reload_categories_from_foundation():
    """Fetch Customers tab from Google Sheet and rebuild US/Intl categories."""
    global _CUSTOMER_CATEGORIES, CATEGORIES
    url = get_apps_script_url()
    if not url:
        return False
    try:
        r = requests.get(url, params={"sheet": "customers", "action": "read"}, timeout=60)
        rows = r.json().get("rows", [])
        us, intl = [], []
        for row in rows:
            # ERP key: explicit override > Customer(ERP) with " - " stripped > Location
            cust_erp = str(row.get("Customer (ERP)", "") or "").strip()
            erp_ov   = str(row.get("ERP Location",   "") or "").strip()
            loc      = erp_ov or cust_erp.replace(" - ", " ") or str(row.get("Location", "")).strip()
            zone     = str(row.get("Zone", "")).strip()
            if not loc or zone in ("", "India"):
                continue
            if zone == "US":
                us.append(loc)
            else:
                intl.append(loc)
        _CUSTOMER_CATEGORIES["US Customers"]          = us
        _CUSTOMER_CATEGORIES["International Customers"] = intl
        CATEGORIES = _build_categories()
        print(f"✅ Categories reloaded from foundation — US: {len(us)}, Intl: {len(intl)}")
        return True
    except Exception as e:
        print(f"⚠️  Could not load foundation categories: {e}")
        return False

# Try to load from foundation at startup (non-blocking — server still starts if it fails)
try:
    reload_categories_from_foundation()
except Exception:
    print("⚠️  Foundation categories unavailable at startup — using empty lists. Restart after sheet is accessible.")

def get_category(location):
    for cat, locs in CATEGORIES.items():
        if location in locs:
            return cat
    return "Other"

# ── In-memory cache ────────────────────────────────────────────────────────────
_cache = {
    "line_items": None,
    "fetched_at": None,
}
CACHE_TTL = 3600  # 1 hour

def cache_valid():
    return _cache["line_items"] is not None and (time.time() - _cache["fetched_at"]) < CACHE_TTL

# ── Core ERPNext fetch ──────────────────────────────────────────────────────────
def fetch_claim_detail(cfg, name):
    try:
        r = requests.get(
            f"{cfg['site_url']}/api/resource/Expense Claim/{name}",
            headers=erp_headers(cfg), timeout=15,
        )
        return r.json().get("data", {})
    except Exception:
        return {}

def fetch_all_line_items(cfg):
    """Fetch every expense claim + line items. Cached for 1 hour."""
    if cache_valid():
        return _cache["line_items"]

    r = requests.get(
        f"{cfg['site_url']}/api/resource/Expense Claim",
        headers=erp_headers(cfg),
        params={
            "fields": json.dumps(["name", "status", "posting_date", "employee_name"]),
            "filters": json.dumps([["status", "!=", "Rejected"]]),
            "limit_page_length": 500,
            "order_by": "posting_date desc",
        },
        timeout=15,
    )
    claims = r.json().get("data", [])
    claim_meta = {c["name"]: c for c in claims}

    line_items = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(fetch_claim_detail, cfg, n): n for n in claim_meta}
        for future in as_completed(futures):
            name = futures[future]
            doc  = future.result()
            meta = claim_meta[name]
            for item in doc.get("expenses", []):
                loc = (item.get("customer_location") or "Untagged").strip()
                line_items.append({
                    "claim":         name,
                    "claim_status":  meta.get("status", ""),
                    "employee":      meta.get("employee_name", ""),
                    "posting_date":  meta.get("posting_date", ""),
                    "date":          item.get("expense_date", ""),
                    "location":      loc,
                    "category":      get_category(loc),
                    "expense_type":  item.get("expense_type", "Other"),
                    "amount":        item.get("sanctioned_amount") or item.get("amount") or 0,
                    "description":   strip_html(item.get("description", "")),
                })

    _cache["line_items"] = line_items
    _cache["fetched_at"] = time.time()
    return line_items

# ── Routes ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/api/status")
def status():
    """Quick connection check."""
    try:
        cfg = load_config()
        r = requests.get(
            f"{cfg['site_url']}/api/resource/Company",
            headers=erp_headers(cfg),
            params={"fields": json.dumps(["name"]), "limit_page_length": 1},
            timeout=8,
        )
        if r.status_code == 200:
            return jsonify({"status": "connected", "site": cfg["site_url"]})
        return jsonify({"status": "error", "code": r.status_code}), r.status_code
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/expenses/summary")
def expenses_summary():
    """
    Full expense breakdown grouped by category → location → expense_type.
    Cached. Add ?refresh=1 to force a reload from ERPNext.
    """
    try:
        cfg = load_config()
        if request.args.get("refresh") == "1":
            _cache["line_items"] = None

        items = fetch_all_line_items(cfg)

        # Aggregate
        cat_data = defaultdict(lambda: {
            "total": 0.0,
            "line_item_count": 0,
            "locations": defaultdict(lambda: {
                "total": 0.0,
                "line_item_count": 0,
                "by_expense_type": defaultdict(float),
            }),
        })

        for item in items:
            cat  = item["category"]
            loc  = item["location"]
            amt  = item["amount"]
            etype = item["expense_type"]
            cat_data[cat]["total"]           += amt
            cat_data[cat]["line_item_count"] += 1
            cat_data[cat]["locations"][loc]["total"]           += amt
            cat_data[cat]["locations"][loc]["line_item_count"] += 1
            cat_data[cat]["locations"][loc]["by_expense_type"][etype] += amt

        # Serialize
        result = []
        for cat in CATEGORIES:
            d = cat_data.get(cat, {"total": 0, "line_item_count": 0, "locations": {}})
            locs = []
            for loc in CATEGORIES[cat]:
                ld = d["locations"].get(loc)
                if not ld:
                    continue
                locs.append({
                    "location":       loc,
                    "total":          round(ld["total"], 2),
                    "line_item_count": ld["line_item_count"],
                    "by_expense_type": {k: round(v, 2) for k, v in ld["by_expense_type"].items()},
                })
            result.append({
                "category":        cat,
                "total":           round(d["total"], 2),
                "line_item_count": d["line_item_count"],
                "locations":       locs,
            })

        grand_total = round(sum(item["amount"] for item in items), 2)
        cached_at   = _cache.get("fetched_at")

        return jsonify({
            "status":      "ok",
            "grand_total": grand_total,
            "total_claims": len(set(i["claim"] for i in items)),
            "cached_at":   cached_at,
            "categories":  result,
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/expenses/location/<path:location_name>")
def expenses_by_location(location_name):
    """All line items for a specific customer_location."""
    try:
        cfg  = load_config()
        items = fetch_all_line_items(cfg)
        filtered = [i for i in items if i["location"].lower() == location_name.lower()]
        total = round(sum(i["amount"] for i in filtered), 2)
        by_type = defaultdict(float)
        for i in filtered:
            by_type[i["expense_type"]] += i["amount"]
        return jsonify({
            "status":          "ok",
            "location":        location_name,
            "category":        get_category(location_name),
            "total":           total,
            "line_item_count": len(filtered),
            "by_expense_type": {k: round(v, 2) for k, v in by_type.items()},
            "line_items":      sorted(filtered, key=lambda x: x["date"] or "", reverse=True),
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/expenses/categories")
def expense_categories():
    """Top-level category totals only — fast summary for dashboard cards."""
    try:
        cfg   = load_config()
        items = fetch_all_line_items(cfg)
        by_cat = defaultdict(float)
        for i in items:
            by_cat[i["category"]] += i["amount"]
        return jsonify({
            "status":     "ok",
            "categories": {k: round(v, 2) for k, v in by_cat.items()},
            "grand_total": round(sum(by_cat.values()), 2),
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/revenue/all")
def revenue_all():
    """
    Fetch all Sales Invoices (excluding Cancelled) with full detail.
    Returns invoices grouped by customer and month.
    """
    try:
        cfg = load_config()

        # Fetch all non-cancelled sales invoices
        resp = requests.get(
            f"{cfg['site_url']}/api/resource/Sales Invoice",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps([
                    "name", "customer", "customer_name", "grand_total",
                    "outstanding_amount", "status", "posting_date",
                    "due_date", "currency", "remarks"
                ]),
                "filters": json.dumps([["status", "!=", "Cancelled"]]),
                "limit_page_length": 500,
                "order_by": "posting_date desc",
            },
            timeout=15,
        )
        invoices = resp.json().get("data", [])

        # Also fetch one invoice in full to see available fields
        sample_detail = {}
        if invoices:
            det = requests.get(
                f"{cfg['site_url']}/api/resource/Sales Invoice/{invoices[0]['name']}",
                headers=erp_headers(cfg),
                timeout=10,
            )
            sample_detail = det.json().get("data", {})

        # Summary stats
        total_invoiced  = sum(i.get("grand_total", 0) for i in invoices)
        total_outstanding = sum(i.get("outstanding_amount", 0) for i in invoices)
        total_collected = total_invoiced - total_outstanding

        # Group by customer
        from collections import defaultdict
        by_customer = defaultdict(lambda: {"invoiced": 0, "collected": 0, "outstanding": 0, "count": 0, "invoices": []})
        for inv in invoices:
            c = inv.get("customer_name") or inv.get("customer")
            amt = inv.get("grand_total", 0)
            out = inv.get("outstanding_amount", 0)
            by_customer[c]["invoiced"]     += amt
            by_customer[c]["collected"]    += (amt - out)
            by_customer[c]["outstanding"]  += out
            by_customer[c]["count"]        += 1
            by_customer[c]["invoices"].append({
                "name":     inv.get("name"),
                "date":     inv.get("posting_date"),
                "due_date": inv.get("due_date"),
                "amount":   amt,
                "outstanding": out,
                "status":   inv.get("status"),
            })

        customers = sorted([
            {
                "customer":     k,
                "invoiced":     round(v["invoiced"], 2),
                "collected":    round(v["collected"], 2),
                "outstanding":  round(v["outstanding"], 2),
                "invoice_count": v["count"],
                "invoices":     sorted(v["invoices"], key=lambda x: x["date"] or "", reverse=True),
            }
            for k, v in by_customer.items()
        ], key=lambda x: -x["invoiced"])

        # Unique statuses seen
        statuses = list(set(i.get("status") for i in invoices))

        # Sample invoice fields (to check what's available)
        sample_fields = list(sample_detail.keys()) if sample_detail else []

        return jsonify({
            "status":            "ok",
            "total_invoices":    len(invoices),
            "total_invoiced":    round(total_invoiced, 2),
            "total_collected":   round(total_collected, 2),
            "total_outstanding": round(total_outstanding, 2),
            "statuses_seen":     statuses,
            "customers":         customers,
            "sample_invoice_fields": sample_fields,
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/customer/profile")
def customer_profile():
    """
    Pull Sales Orders, Delivery Notes, and Sales Invoices for a customer.
    ?customer=Tenneco  (partial match)
    """
    query = request.args.get("customer", "Tenneco")
    try:
        cfg = load_config()

        # Sales Orders
        so_resp = requests.get(
            f"{cfg['site_url']}/api/resource/Sales Order",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps([
                    "name", "customer", "customer_name", "transaction_date",
                    "delivery_date", "grand_total", "status", "custom_customer_location"
                ]),
                "filters": json.dumps([["customer_name", "like", f"%{query}%"]]),
                "limit_page_length": 100,
                "order_by": "transaction_date desc",
            },
            timeout=10,
        )
        sales_orders = so_resp.json().get("data", [])

        # Fetch line items for each Sales Order to see bot models/quantities
        so_details = []
        for so in sales_orders:
            det = requests.get(
                f"{cfg['site_url']}/api/resource/Sales Order/{so['name']}",
                headers=erp_headers(cfg),
                timeout=10,
            )
            doc = det.json().get("data", {})
            items = [
                {
                    "item_code":  i.get("item_code"),
                    "item_name":  i.get("item_name"),
                    "description": i.get("description", ""),
                    "qty":        i.get("qty"),
                    "rate":       i.get("rate"),
                    "amount":     i.get("amount"),
                }
                for i in doc.get("items", [])
            ]
            so_details.append({
                "name":               so.get("name"),
                "customer":           so.get("customer_name"),
                "customer_location":  so.get("custom_customer_location"),
                "date":               so.get("transaction_date"),
                "delivery_date":      so.get("delivery_date"),
                "total":              so.get("grand_total"),
                "status":             so.get("status"),
                "items":              items,
            })

        # Delivery Notes
        dn_resp = requests.get(
            f"{cfg['site_url']}/api/resource/Delivery Note",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps([
                    "name", "customer", "customer_name", "posting_date",
                    "status", "grand_total", "custom_customer_location"
                ]),
                "filters": json.dumps([["customer_name", "like", f"%{query}%"]]),
                "limit_page_length": 100,
                "order_by": "posting_date desc",
            },
            timeout=10,
        )
        delivery_notes = dn_resp.json().get("data", [])

        # Fetch line items for each Delivery Note
        dn_details = []
        for dn in delivery_notes:
            det = requests.get(
                f"{cfg['site_url']}/api/resource/Delivery Note/{dn['name']}",
                headers=erp_headers(cfg),
                timeout=10,
            )
            doc = det.json().get("data", {})
            items = [
                {
                    "item_code":  i.get("item_code"),
                    "item_name":  i.get("item_name"),
                    "serial_no":  i.get("serial_no"),
                    "qty":        i.get("qty"),
                    "rate":       i.get("rate"),
                    "amount":     i.get("amount"),
                }
                for i in doc.get("items", [])
            ]
            dn_details.append({
                "name":              dn.get("name"),
                "customer":          dn.get("customer_name"),
                "customer_location": dn.get("custom_customer_location"),
                "date":              dn.get("posting_date"),
                "status":            dn.get("status"),
                "items":             items,
            })

        return jsonify({
            "status":         "ok",
            "query":          query,
            "sales_orders":   so_details,
            "delivery_notes": dn_details,
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/bom-direct")
def bom_direct():
    """Fetch a BOM directly by exact name."""
    name = request.args.get("name", "BOM-999-00001-001")
    try:
        cfg = load_config(instance="india")
        resp = requests.get(
            f"{cfg['site_url']}/api/resource/BOM/{name}",
            headers=erp_headers(cfg),
            timeout=10,
        )
        return jsonify({"status": "ok", "http_code": resp.status_code, "data": resp.json()})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/bom-list")
def bom_list_all():
    """List all BOMs from India instance — no filter."""
    try:
        cfg = load_config(instance="india")
        resp = requests.get(
            f"{cfg['site_url']}/api/resource/BOM",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps(["name", "item", "item_name", "is_active", "is_default", "total_cost", "currency"]),
                "limit_page_length": 50,
                "order_by": "modified desc",
            },
            timeout=10,
        )
        return jsonify({"status": "ok", "data": resp.json()})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/bom")
def bom_lookup():
    """
    Fetch BOM(s) for an item from the India ERP instance.
    ?item=Sherpa 10K (partial match)
    Returns BOM header + all component line items with costs.
    """
    query = request.args.get("item", "Sherpa 10K")
    try:
        cfg = load_config(instance="india")

        # Find matching BOMs — search by item_name, item_code, or exact name
        bom_list = []
        for filter_field in [["item_name", "like", f"%{query}%"],
                              ["item",      "like", f"%{query}%"],
                              ["name",      "like", f"%{query}%"]]:
            resp = requests.get(
                f"{cfg['site_url']}/api/resource/BOM",
                headers=erp_headers(cfg),
                params={
                    "fields": json.dumps([
                        "name", "item", "item_name", "quantity",
                        "total_cost", "total_mop_cost", "total_rm_cost",
                        "is_active", "is_default", "currency"
                    ]),
                    "filters": json.dumps([[filter_field[0], filter_field[1], filter_field[2]]]),
                    "limit_page_length": 20,
                },
                timeout=10,
            )
            results = resp.json().get("data", [])
            seen = {b["name"] for b in bom_list}
            bom_list += [b for b in results if b["name"] not in seen]

        if not bom_list:
            return jsonify({"status": "ok", "message": f"No active BOMs found for '{query}'", "boms": []})

        # Fetch full detail for each BOM
        boms = []
        for bom in bom_list:
            det = requests.get(
                f"{cfg['site_url']}/api/resource/BOM/{bom['name']}",
                headers=erp_headers(cfg),
                timeout=10,
            )
            doc = det.json().get("data", {})
            items = [
                {
                    "item_code":    i.get("item_code"),
                    "item_name":    i.get("item_name"),
                    "qty":          i.get("qty"),
                    "uom":          i.get("uom"),
                    "rate":         i.get("rate"),
                    "amount":       i.get("amount"),
                    "bom_no":       i.get("bom_no"),
                }
                for i in doc.get("items", [])
            ]
            # Operations cost if any
            operations = [
                {
                    "operation":    o.get("operation"),
                    "workstation":  o.get("workstation"),
                    "time_in_mins": o.get("time_in_mins"),
                    "operating_cost": o.get("operating_cost"),
                }
                for o in doc.get("operations", [])
            ]
            boms.append({
                "name":            bom.get("name"),
                "item":            bom.get("item"),
                "item_name":       bom.get("item_name"),
                "qty":             bom.get("quantity"),
                "total_cost":      bom.get("total_cost"),
                "total_rm_cost":   bom.get("total_rm_cost"),
                "total_mop_cost":  bom.get("total_mop_cost"),
                "currency":        bom.get("currency"),
                "is_default":      bom.get("is_default"),
                "items":           items,
                "operations":      operations,
            })

        return jsonify({"status": "ok", "count": len(boms), "boms": boms})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/revenue/customer-monthly")
def revenue_customer_monthly():
    """
    Monthly revenue for a specific customer, matched by custom_customer_location.
    ?location=Tenneco Litchfield USA
    """
    location = request.args.get("location", "")
    try:
        cfg = load_config()
        resp = requests.get(
            f"{cfg['site_url']}/api/resource/Sales Invoice",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps([
                    "name", "customer_name", "custom_customer_location",
                    "grand_total", "outstanding_amount", "status", "posting_date"
                ]),
                "filters": json.dumps([
                    ["custom_customer_location", "like", f"%{location}%"],
                    ["status", "!=", "Cancelled"],
                    ["docstatus", "=", 1],
                ]),
                "limit_page_length": 200,
                "order_by": "posting_date asc",
            },
            timeout=10,
        )
        invoices = resp.json().get("data", [])

        # No fallback — if custom_customer_location doesn't match, return $0
        # (prevents cross-contamination between customers sharing the same company name)

        # Group by month
        monthly = defaultdict(float)
        for inv in invoices:
            month = (inv.get("posting_date") or "")[:7]
            if month:
                monthly[month] += inv.get("grand_total", 0) or 0

        return jsonify({
            "status":   "ok",
            "location": location,
            "invoices": len(invoices),
            "monthly":  {k: round(v, 2) for k, v in sorted(monthly.items())},
            "total":    round(sum(monthly.values()), 2),
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/smr/list")
def smr_list():
    """
    Fetch all Service Material Requests (or Material Requests) from ERPNext.
    Tries 'Service Material Request' custom doctype first, then falls back to
    standard 'Material Request' filtered by type or customer location.
    Add ?refresh=1 to bypass cache.
    """
    try:
        cfg = load_config()

        # ── Try custom 'Service Material Request' doctype first ──────────────
        results = []
        doctype_used = None

        for doctype in ["Service Material Request", "Material Request"]:
            try:
                fields = ["name", "status", "creation", "modified"]
                if doctype == "Material Request":
                    fields += ["material_request_type", "custom_customer_location",
                               "schedule_date", "transaction_date", "company"]
                else:
                    fields += ["customer_location", "schedule_date",
                               "transaction_date", "purpose", "company"]

                r = requests.get(
                    f"{cfg['site_url']}/api/resource/{doctype.replace(' ', '%20')}",
                    headers=erp_headers(cfg),
                    params={
                        "fields": json.dumps(fields),
                        "limit_page_length": 500,
                        "order_by": "creation desc",
                    },
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    results = data
                    doctype_used = doctype
                    break
            except Exception:
                continue

        if not results and doctype_used is None:
            return jsonify({"status": "error", "detail": "Could not find SMR doctype"}), 404

        # ── Fetch full detail for each record ────────────────────────────────
        def fetch_detail(rec):
            try:
                r = requests.get(
                    f"{cfg['site_url']}/api/resource/{doctype_used.replace(' ', '%20')}/{rec['name']}",
                    headers=erp_headers(cfg),
                    timeout=10,
                )
                return r.json().get("data", rec)
            except Exception:
                return rec

        detailed = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_detail, rec): rec for rec in results}
            for future in as_completed(futures):
                detailed.append(future.result())

        # ── Normalize location field across doctypes ─────────────────────────
        for doc in detailed:
            if "customer_location" in doc:
                doc["_location"] = doc.get("customer_location", "")
            elif "custom_customer_location" in doc:
                doc["_location"] = doc.get("custom_customer_location", "")
            else:
                doc["_location"] = ""
            doc["_category"] = get_category(doc["_location"])

        # Group by location
        by_loc = defaultdict(list)
        for doc in detailed:
            by_loc[doc["_location"]].append(doc)

        return jsonify({
            "status":       "ok",
            "doctype_used": doctype_used,
            "total":        len(detailed),
            "by_location":  {k: v for k, v in sorted(by_loc.items())},
            "records":      sorted(detailed, key=lambda x: x.get("creation",""), reverse=True),
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/smr/all-costs")
def smr_all_costs():
    """
    All approved SMR material costs grouped by customer_location and month.
    Used by the dashboard expense breakdown to show SMR as a cost component.
    """
    try:
        cfg = load_config()

        # Fetch all approved SMRs
        r = requests.get(
            f"{cfg['site_url']}/api/resource/Service%20Material%20Request",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps(["name", "customer_location", "date",
                                      "status", "workflow_state"]),
                "filters": json.dumps([["workflow_state", "not in", ["Draft", "Rejected", "Cancelled"]]]),
                "limit_page_length": 500,
                "order_by": "date desc",
            },
            timeout=15,
        )
        records = r.json().get("data", [])

        # Fetch full item details
        def fetch_items(rec):
            try:
                det = requests.get(
                    f"{cfg['site_url']}/api/resource/Service%20Material%20Request/{rec['name']}",
                    headers=erp_headers(cfg), timeout=10,
                )
                doc = det.json().get("data", {})
                return {**rec, "items": doc.get("items", [])}
            except Exception:
                return {**rec, "items": []}

        detailed = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(fetch_items, rec) for rec in records]
            for f in as_completed(futures):
                detailed.append(f.result())

        # Fetch rate from SLE — most recent entry on or before the SMR date.
        # If none found (SMR predates first stock entry), fall back to nearest
        # SLE regardless of date so we never return 0 when a rate exists.
        def fetch_sle_rate(item_code, smr_date):
            def query_sle(extra_filters):
                r = requests.get(
                    f"{cfg['site_url']}/api/resource/Stock Ledger Entry",
                    headers=erp_headers(cfg),
                    params={
                        "fields":  json.dumps(["valuation_rate", "incoming_rate", "posting_date"]),
                        "filters": json.dumps([
                            ["item_code",     "=", item_code],
                            ["incoming_rate", ">", 0],
                            ["is_cancelled",  "=", 0],
                        ] + extra_filters),
                        "order_by":          "posting_date desc, name desc",
                        "limit_page_length": 1,
                    },
                    timeout=10,
                )
                return r.json().get("data", [])

            try:
                # Try date-bounded lookup first (historically accurate)
                rows = query_sle([["posting_date", "<=", smr_date]])
                if rows:
                    return rows[0].get("valuation_rate") or rows[0].get("incoming_rate") or 0
                # Fall back to nearest SLE regardless of date
                rows = query_sle([])
                if rows:
                    return rows[0].get("valuation_rate") or rows[0].get("incoming_rate") or 0
                return 0
            except Exception:
                return 0

        # Group costs by location → month, pricing each item via SLE at SMR date
        by_loc_month = defaultdict(lambda: defaultdict(float))
        item_rates   = {}   # cache: (item_code, smr_date) → rate

        def price_item(item_code, smr_date):
            key = (item_code, smr_date)
            if key not in item_rates:
                item_rates[key] = fetch_sle_rate(item_code, smr_date)
            return item_rates[key]

        # Build all (item_code, smr_date) pairs for parallel fetch
        all_pairs = list({
            (i.get("item_code"), d.get("date", ""))
            for d in detailed
            for i in d["items"]
            if i.get("item_code") and d.get("date")
        })

        def fetch_pair(pair):
            code, date = pair
            return pair, fetch_sle_rate(code, date)

        with ThreadPoolExecutor(max_workers=20) as ex:
            for pair, rate in ex.map(fetch_pair, all_pairs):
                item_rates[pair] = rate

        for d in detailed:
            loc      = d.get("customer_location", "Unknown")
            smr_date = d.get("date", "")
            month    = smr_date[:7]
            if not month:
                continue
            for item in d["items"]:
                code = item.get("item_code", "")
                qty  = float(item.get("qty") or 1)
                rate = item_rates.get((code, smr_date), 0)
                by_loc_month[loc][month] += rate * qty

        # Serialize
        result = {
            loc: {month: round(cost, 2) for month, cost in months.items()}
            for loc, months in by_loc_month.items()
        }

        return jsonify({"status": "ok", "by_location": result,
                        "smr_count": len(detailed)})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/smr/summary")
def smr_summary():
    """
    SMR summary for last 3 months — customer, SMR name, date, status, items requested.
    """
    try:
        cfg = load_config()
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        r = requests.get(
            f"{cfg['site_url']}/api/resource/Service%20Material%20Request",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps(["name", "customer_location", "date", "status",
                                      "workflow_state", "service_type", "requested_by"]),
                "filters": json.dumps([["date", ">=", cutoff]]),
                "limit_page_length": 500,
                "order_by": "date desc",
            },
            timeout=15,
        )
        records = r.json().get("data", [])

        def fetch_items(rec):
            try:
                det = requests.get(
                    f"{cfg['site_url']}/api/resource/Service%20Material%20Request/{rec['name']}",
                    headers=erp_headers(cfg), timeout=10,
                )
                doc = det.json().get("data", {})
                items = [{"item_code": i.get("item_code"), "item_name": i.get("item_name"),
                          "qty": i.get("qty"), "bot_id": i.get("bot_id")}
                         for i in doc.get("items", [])]
                return {**rec, "items": items}
            except Exception:
                return {**rec, "items": []}

        detailed = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(fetch_items, rec) for rec in records]
            for f in as_completed(futures):
                detailed.append(f.result())

        detailed.sort(key=lambda x: x.get("date",""), reverse=True)

        # Group by customer_location
        by_loc = defaultdict(list)
        for d in detailed:
            by_loc[d.get("customer_location","Unknown")].append(d)

        output = []
        for loc in sorted(by_loc.keys()):
            smrs = by_loc[loc]
            output.append({
                "customer": loc,
                "smr_count": len(smrs),
                "smrs": [{
                    "name": s["name"],
                    "date": s["date"],
                    "status": s.get("workflow_state") or s.get("status"),
                    "service_type": s.get("service_type"),
                    "requested_by": s.get("requested_by"),
                    "items": s["items"],
                } for s in smrs]
            })

        return jsonify({"status": "ok", "cutoff": cutoff, "total": len(detailed), "customers": output})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/smr/costed")
def smr_costed():
    """
    SMR details with item costs for a specific customer location.
    ?location=Valeo Greensburg&status=Approved
    Fetches item valuation rate from Item Price or Item master.
    """
    location = request.args.get("location", "")
    filter_status = request.args.get("status", "")
    try:
        cfg = load_config()
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        filters = [["date", ">=", cutoff], ["customer_location", "=", location]]
        r = requests.get(
            f"{cfg['site_url']}/api/resource/Service%20Material%20Request",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps(["name", "date", "status", "workflow_state",
                                      "service_type", "requested_by", "customer_location"]),
                "filters": json.dumps(filters),
                "limit_page_length": 200,
                "order_by": "date desc",
            },
            timeout=15,
        )
        records = r.json().get("data", [])

        # Filter by workflow_state if requested
        if filter_status:
            records = [rec for rec in records
                       if (rec.get("workflow_state") or rec.get("status","")).lower() == filter_status.lower()]

        # Fetch full item details for each SMR
        def fetch_full(rec):
            try:
                det = requests.get(
                    f"{cfg['site_url']}/api/resource/Service%20Material%20Request/{rec['name']}",
                    headers=erp_headers(cfg), timeout=10,
                )
                doc = det.json().get("data", {})
                return {**rec, "items": doc.get("items", [])}
            except Exception:
                return {**rec, "items": []}

        detailed = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(fetch_full, rec) for rec in records]
            for f in as_completed(futures):
                detailed.append(f.result())

        # Collect unique item codes
        item_codes = list({i.get("item_code") for d in detailed for i in d["items"] if i.get("item_code")})

        # Fetch item valuation rate from Item master
        def fetch_item_rate(code):
            try:
                r = requests.get(
                    f"{cfg['site_url']}/api/resource/Item/{code}",
                    headers=erp_headers(cfg), timeout=8,
                )
                doc = r.json().get("data", {})
                return code, {
                    "valuation_rate":    doc.get("valuation_rate") or 0,
                    "standard_rate":     doc.get("standard_rate") or 0,
                    "last_purchase_rate": doc.get("last_purchase_rate") or 0,
                    "item_name":         doc.get("item_name", code),
                }
            except Exception:
                return code, {}

        item_rates = {}
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_item_rate, code): code for code in item_codes}
            for f in as_completed(futures):
                code, data = f.result()
                item_rates[code] = data

        # Build costed output
        result = []
        for d in sorted(detailed, key=lambda x: x.get("date",""), reverse=True):
            costed_items = []
            for i in d["items"]:
                code = i.get("item_code","")
                qty  = i.get("qty", 1)
                rate_info = item_rates.get(code, {})
                rate = rate_info.get("valuation_rate") or 0
                costed_items.append({
                    "item_code": code,
                    "item_name": i.get("item_name",""),
                    "bot_id":    i.get("bot_id",""),
                    "qty":       qty,
                    "unit_rate": round(rate, 2),
                    "total":     round(rate * qty, 2),
                    "rate_source": "valuation" if rate_info.get("valuation_rate") else
                                   "last_purchase" if rate_info.get("last_purchase_rate") else
                                   "standard" if rate_info.get("standard_rate") else "unknown",
                })
            result.append({
                "smr":          d["name"],
                "date":         d["date"],
                "month":        d["date"][:7] if d.get("date") else "",
                "status":       d.get("workflow_state") or d.get("status"),
                "service_type": d.get("service_type",""),
                "requested_by": d.get("requested_by",""),
                "items":        costed_items,
                "smr_total":    round(sum(i["total"] for i in costed_items), 2),
            })

        grand_total = round(sum(r["smr_total"] for r in result), 2)
        return jsonify({
            "status":       "ok",
            "location":     location,
            "smr_count":    len(result),
            "grand_total":  grand_total,
            "item_rates":   item_rates,
            "smrs":         result,
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/item/inspect")
def item_inspect():
    """Inspect all fields on a specific item — useful for finding where price lives."""
    code = request.args.get("code", "60010101")
    try:
        cfg = load_config()
        r = requests.get(
            f"{cfg['site_url']}/api/resource/Item/{code}",
            headers=erp_headers(cfg), timeout=10,
        )
        doc = r.json().get("data", {})
        # Also check Item Price list
        price_resp = requests.get(
            f"{cfg['site_url']}/api/resource/Item Price",
            headers=erp_headers(cfg),
            params={
                "fields": json.dumps(["name", "item_code", "price_list", "price_list_rate",
                                      "currency", "valid_from", "valid_upto"]),
                "filters": json.dumps([["item_code", "=", code]]),
                "limit_page_length": 20,
            },
            timeout=10,
        )
        prices = price_resp.json().get("data", [])
        # Return all non-null fields from item + price list entries
        non_null = {k: v for k, v in doc.items() if v not in (None, "", 0, [], {})}
        return jsonify({"status": "ok", "item_code": code,
                        "item_fields": non_null, "item_prices": prices})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/smr/fields")
def smr_fields():
    """Inspect available fields on the SMR doctype — useful for discovery."""
    try:
        cfg = load_config()
        for doctype in ["Service Material Request", "Material Request"]:
            r = requests.get(
                f"{cfg['site_url']}/api/resource/{doctype.replace(' ', '%20')}",
                headers=erp_headers(cfg),
                params={"limit_page_length": 1, "fields": json.dumps(["name"])},
                timeout=8,
            )
            if r.status_code == 200:
                # Fetch one full record to see all fields
                data = r.json().get("data", [])
                if data:
                    det = requests.get(
                        f"{cfg['site_url']}/api/resource/{doctype.replace(' ', '%20')}/{data[0]['name']}",
                        headers=erp_headers(cfg), timeout=8,
                    )
                    doc = det.json().get("data", {})
                    return jsonify({
                        "status":   "ok",
                        "doctype":  doctype,
                        "fields":   list(doc.keys()),
                        "sample":   {k: v for k, v in list(doc.items())[:30]},
                    })
        return jsonify({"status": "error", "detail": "No SMR records found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


# ── Foundation (Google Sheet via Apps Script) ──────────────────────────────────

def get_apps_script_url():
    # Try local config file first (development)
    try:
        with open(CONFIG_PATH) as f:
            raw = f.read()
        parts = raw.strip().split("\n\n")
        for p in parts:
            try:
                cfg = json.loads(p)
                if cfg.get("apps_script_url"):
                    return cfg["apps_script_url"]
            except Exception:
                pass
    except FileNotFoundError:
        pass
    # Fall back to environment variable (Vercel / production)
    return os.environ.get("APPS_SCRIPT_URL", None)

@app.route("/api/expenses/all-locations")
def all_locations():
    """List every unique customer_location value seen in ERP expense claims."""
    try:
        cfg   = load_config()
        items = fetch_all_line_items(cfg)
        from collections import Counter
        counts = Counter(i["location"] for i in items)
        locs   = sorted(counts.items(), key=lambda x: -x[1])
        return jsonify({"status": "ok", "locations": [{"location": l, "count": c} for l, c in locs]})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/foundation/reload-categories", methods=["POST", "GET"])
def foundation_reload_categories():
    """Reload US/Intl customer categories from Google Sheet without restarting the server."""
    ok = reload_categories_from_foundation()
    _cache["line_items"] = None  # also bust the expense cache so get_category() is re-applied
    return jsonify({
        "status": "ok" if ok else "error",
        "us_customers":   _CUSTOMER_CATEGORIES["US Customers"],
        "intl_customers": _CUSTOMER_CATEGORIES["International Customers"],
    })

@app.route("/api/foundation/<sheet>")
def foundation_read(sheet):
    """Read a foundation sheet. ?sheet=customers|po_history|bots|zones|service_team"""
    url = get_apps_script_url()
    if not url:
        return jsonify({"status": "error", "detail": "apps_script_url not set in erpnext_config.json"}), 500
    try:
        r = requests.get(url, params={"sheet": sheet, "action": "read"}, timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/foundation/<sheet>", methods=["POST"])
def foundation_write(sheet):
    """Write to a foundation sheet. Body: {action, row/rows, match_column, match_value}"""
    url = get_apps_script_url()
    if not url:
        return jsonify({"status": "error", "detail": "apps_script_url not set in erpnext_config.json"}), 500
    try:
        payload = request.json or {}
        payload["sheet"] = sheet
        r = requests.post(url, json=payload, timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/foundation")
def foundation_all():
    """Read all foundation sheets at once — used for dashboard init.
    Fetches sheets in parallel with a generous timeout to handle Apps Script cold starts.
    """
    url = get_apps_script_url()
    if not url:
        return jsonify({"status": "error", "detail": "apps_script_url not set in erpnext_config.json"}), 500
    sheets = ["customers", "po_history", "bots", "zones", "service_team"]

    def fetch_sheet(sheet):
        try:
            r = requests.get(url, params={"sheet": sheet, "action": "read"}, timeout=60)
            return sheet, r.json()
        except Exception as e:
            return sheet, {"status": "error", "detail": str(e)}

    result = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for sheet, data in ex.map(fetch_sheet, sheets):
            result[sheet] = data
    return jsonify({"status": "ok", "data": result})


@app.route("/api/smrs")
def get_smrs():
    """
    Fetch all Service Material Requests for US customers.
    Returns each SMR with its items enriched with valuation_rate from the Item doctype.
    """
    cfg = load_config()
    hdrs = erp_headers(cfg)
    base = cfg["site_url"]

    # ── 1. Fetch all SMR list entries ─────────────────────────────────────────
    try:
        r = requests.get(
            f"{base}/api/resource/Service Material Request",
            headers=hdrs,
            params={
                "fields": json.dumps(["name", "customer", "customer_location",
                                      "date", "status", "service_type",
                                      "delivery_type", "workflow_state"]),
                "limit_page_length": 500,
                "filters": json.dumps([
                    ["docstatus",      "!=", 2],           # exclude cancelled
                    ["workflow_state", "not in", ["Draft", "Rejected"]],
                ]),
            },
            timeout=20,
        )
        smr_list = r.json().get("data", [])
    except Exception as e:
        return jsonify({"status": "error", "detail": f"SMR list fetch failed: {e}"}), 500

    # ── 2. Filter to US customers ─────────────────────────────────────────────
    us_locs = set(loc.lower() for loc in _CUSTOMER_CATEGORIES.get("US Customers", []))
    us_smrs = [s for s in smr_list
               if s.get("customer_location", "").lower() in us_locs
               or any(s.get("customer_location", "").lower().startswith(loc.lower()[:6])
                      for loc in _CUSTOMER_CATEGORIES.get("US Customers", []))]

    if not us_smrs:
        # Fallback: return all if filter yields nothing (categories may not be loaded)
        us_smrs = smr_list

    # ── 3. Fetch full detail for each SMR (to get items) ──────────────────────
    def fetch_smr_detail(name):
        try:
            r = requests.get(f"{base}/api/resource/Service Material Request/{name}",
                             headers=hdrs, timeout=15)
            return r.json().get("data", {})
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        details = list(ex.map(lambda s: fetch_smr_detail(s["name"]), us_smrs))

    # ── 4. Build unique item_code list and fetch valuation rates ─────────────
    all_item_codes = set()
    for d in details:
        for item in (d.get("items") or []):
            if item.get("item_code"):
                all_item_codes.add(item["item_code"])

    # ── 4. Fetch SLE rates per (item_code, smr_date) pair ────────────────────
    # Price each item at its SLE valuation on or before the SMR date — consistent
    # with how /api/smr/all-costs works and avoids using stale Item master rates.
    all_pairs = list({
        (i.get("item_code"), d.get("date", ""))
        for d in details
        for i in (d.get("items") or [])
        if i.get("item_code") and d.get("date")
    })

    # Also collect item_group per item code (display only)
    all_item_codes = {i.get("item_code") for d in details
                      for i in (d.get("items") or []) if i.get("item_code")}

    def fetch_item_group(item_code):
        try:
            r = requests.get(f"{base}/api/resource/Item/{item_code}",
                             headers=hdrs, timeout=8)
            return item_code, r.json().get("data", {}).get("item_group", "")
        except Exception:
            return item_code, ""

    def fetch_sle_rate_pair(pair):
        item_code, smr_date = pair
        def query(extra_filters):
            r = requests.get(
                f"{base}/api/resource/Stock Ledger Entry",
                headers=hdrs,
                params={
                    "fields":  json.dumps(["valuation_rate", "incoming_rate"]),
                    "filters": json.dumps([
                        ["item_code",     "=", item_code],
                        ["incoming_rate", ">", 0],
                        ["is_cancelled",  "=", 0],
                    ] + extra_filters),
                    "order_by":          "posting_date desc, name desc",
                    "limit_page_length": 1,
                },
                timeout=10,
            )
            return r.json().get("data", [])
        try:
            rows = query([["posting_date", "<=", smr_date]])
            if rows:
                return pair, rows[0].get("valuation_rate") or rows[0].get("incoming_rate") or 0
            # Fall back: nearest SLE regardless of date
            rows = query([])
            if rows:
                return pair, rows[0].get("valuation_rate") or rows[0].get("incoming_rate") or 0
            return pair, 0
        except Exception:
            return pair, 0

    with ThreadPoolExecutor(max_workers=20) as ex:
        sle_rates   = dict(ex.map(fetch_sle_rate_pair, all_pairs))
        item_groups = dict(ex.map(fetch_item_group, all_item_codes))

    # ── 5. Assemble result ────────────────────────────────────────────────────
    result = []
    for smr_stub, detail in zip(us_smrs, details):
        if not detail:
            continue
        smr_date  = detail.get("date", "")
        items_out = []
        for item in (detail.get("items") or []):
            ic   = item.get("item_code", "")
            rate = sle_rates.get((ic, smr_date), 0)
            qty  = float(item.get("qty") or 1)
            items_out.append({
                "item_code":      ic,
                "item_name":      item.get("item_name", ""),
                "qty":            qty,
                "uom":            item.get("uom", ""),
                "bot_id":         item.get("bot_id", ""),
                "valuation_rate": rate,
                "rate_source":    "stock_ledger" if rate else "missing",
                "line_total":     round(rate * qty, 2),
                "item_group":     item_groups.get(ic, ""),
            })

        smr_total = sum(i["line_total"] for i in items_out)
        smr_name = detail.get("name", "")
        result.append({
            "name":             smr_name,
            "url":              f"{base}/app/service-material-request/{smr_name}",
            "customer":         detail.get("customer", ""),
            "customer_location": detail.get("customer_location", ""),
            "date":             detail.get("date", ""),
            "status":           detail.get("status", ""),
            "service_type":     detail.get("service_type", ""),
            "delivery_type":    detail.get("delivery_type", ""),
            "workflow_state":   detail.get("workflow_state", ""),
            "items":            items_out,
            "total":            smr_total,
        })

    # Sort by date descending
    result.sort(key=lambda x: x.get("date", ""), reverse=True)
    return jsonify({"status": "ok", "count": len(result), "smrs": result})


@app.route("/api/erp/probe")
def erp_probe():
    """Generic ERP probe — pass ?doctype=X&fields=f1,f2&limit=N&filters=JSON"""
    cfg = load_config()
    doctype = request.args.get("doctype", "")
    fields  = request.args.get("fields", "name")
    limit   = int(request.args.get("limit", 20))
    filters = request.args.get("filters", "[]")
    if not doctype:
        return jsonify({"status": "error", "detail": "doctype required"}), 400
    try:
        field_list = [f.strip() for f in fields.split(",")]
        r = requests.get(
            f"{cfg['site_url']}/api/resource/{doctype}",
            headers=erp_headers(cfg),
            params={"fields": json.dumps(field_list), "limit_page_length": limit,
                    "filters": filters},
            timeout=20,
        )
        return jsonify({"status": "ok", "doctype": doctype, "data": r.json()})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/api/erp/probe/detail")
def erp_probe_detail():
    """Fetch a single ERP doc — pass ?doctype=X&name=Y"""
    cfg  = load_config()
    doctype = request.args.get("doctype", "")
    name    = request.args.get("name", "")
    if not doctype or not name:
        return jsonify({"status": "error", "detail": "doctype and name required"}), 400
    try:
        r = requests.get(
            f"{cfg['site_url']}/api/resource/{doctype}/{name}",
            headers=erp_headers(cfg), timeout=15,
        )
        return jsonify({"status": "ok", "data": r.json().get("data", {})})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

if __name__ == "__main__":
    print("=" * 50)
    print("  ATI Motors Finance Dashboard Server")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(debug=True, port=5000)
