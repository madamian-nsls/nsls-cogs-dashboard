import os
import csv
import json
import re
import requests
import pdfplumber
from datetime import date
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.urandom(24)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SKU_MAP_PATH = os.path.join(BASE_DIR, "sku_map.json")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Config / SKU-map helpers
# ---------------------------------------------------------------------------

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {"shopify_store": "", "shopify_client_id": "", "shopify_client_secret": "", "google_sheet_url": "", "reorder_settings": {}}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_sku_map():
    if os.path.exists(SKU_MAP_PATH):
        with open(SKU_MAP_PATH) as f:
            return json.load(f)
    return {}


def save_sku_map(m):
    with open(SKU_MAP_PATH, "w") as f:
        json.dump(m, f, indent=2)


def shopify_store_domain(store):
    """Normalise store to full .myshopify.com domain."""
    store = store.strip().rstrip("/")
    if not store:
        return ""
    if "." not in store:
        store = store + ".myshopify.com"
    return store


def get_shopify_creds():
    """
    Read store, client_id, and client_secret from config.
    Returns (store, client_id, client_secret).
    Raises ValueError with a human-readable message if anything is missing.
    """
    cfg = load_config()
    store = shopify_store_domain(cfg.get("shopify_store", ""))
    client_id = cfg.get("shopify_client_id", "").strip()
    client_secret = cfg.get("shopify_client_secret", "").strip()
    missing = []
    if not store:
        missing.append("Shopify Store")
    if not client_id:
        missing.append("Client ID")
    if not client_secret:
        missing.append("Client Secret")
    if missing:
        raise ValueError(f"Missing Shopify credentials in Settings: {', '.join(missing)}")
    return store, client_id, client_secret


# ---------------------------------------------------------------------------
# SKU matching with multi-strategy fallback
# ---------------------------------------------------------------------------

def find_sku_for_item(item, sku_map):
    """
    Resolve a parsed invoice item to a sku_map entry using multi-phase fallback.
    Returns the mapping dict or None.
    """
    item_code = (item.get("item_code") or "").strip()
    size      = (item.get("size")      or "").strip()
    color     = (item.get("color")     or "").strip()
    desc      = (item.get("description") or "").strip().lower()
    unit_cost = float(item.get("unit_cost", 0) or 0)

    # ---- Special-case: Pen type disambiguation ----
    # Check description for eco/recycled keywords and price threshold to decide
    # between the premium pen (nsls-prem-pen) and the recycled pen (pen-recycled-red).
    if item_code.lower() in ("pens", "pen", "nsls-prem-pen"):
        eco_keywords = ("eco", "recycl", "eco-friendly", "recyclable")
        is_eco = any(k in desc for k in eco_keywords) or (unit_cost > 0 and unit_cost <= 0.50)
        if is_eco:
            # pen-recycled-red must be in sku_map; if missing fall back to Pens entry
            if "pen-recycled-red" in sku_map:
                return sku_map["pen-recycled-red"]
        if "Pens" in sku_map:
            return sku_map["Pens"]

    # ---- Special-case: DT607 cap — differentiate by logo type in the Size field ----
    # USS invoices put the logo/decoration type in the Size column for this item.
    if item_code.upper() == "DT607":
        # Check size first, then description
        logo_field = (size + " " + desc).lower()
        if "nsls" in logo_field:
            if "DT607-NSLS Letters" in sku_map:
                return sku_map["DT607-NSLS Letters"]
        elif "greek" in logo_field:
            if "DT607-Greek Letters" in sku_map:
                return sku_map["DT607-Greek Letters"]
        if "DT607" in sku_map:
            return sku_map["DT607"]

    # ---- Special-case: NE904 beanie — differentiate by logo type.
    # The logo type appears in the Size field (sometimes Description).
    # Always check both to ensure each row resolves independently.
    if item_code.upper() == "NE904":
        logo_field = (size + " " + desc).lower()
        if "crest" in logo_field:
            if "NE904-Crest Logo" in sku_map:
                return sku_map["NE904-Crest Logo"]
        elif "nsls" in logo_field:
            if "NE904-NSLS Logo" in sku_map:
                return sku_map["NE904-NSLS Logo"]
        # Do NOT fall back to a bare "NE904" key — that would stomp both variants.
        # Return None so the row shows as unmapped and the user picks the right one.
        return None

    parts = item_code.split("-")
    # All base codes to try (item_code itself + progressively-stripped prefixes)
    base_codes = [item_code] + ["-".join(parts[:i]) for i in range(len(parts)-1, 0, -1)]

    # ---- When NO size or color info available: try exact/base keys first ----
    if not size and not color:
        for base in base_codes:
            if base and base in sku_map:
                return sku_map[base]
        # fall through to description match

    # ---- When size and/or color IS available: prefer composites over base key ----
    else:
        # Phase 2: full composites (most specific)
        for base in base_codes:
            if size and color:
                for k in (f"{base}-{size}-{color}", f"{base}-{color}-{size}"):
                    if k in sku_map: return sku_map[k]
            if size:
                k = f"{base}-{size}"
                if k in sku_map: return sku_map[k]

        # Phase 3: prefix scan when color column is blank but size is known
        # e.g. "LOG101-S-*" matches "LOG101-S-Navy"
        if size and not color:
            for base in base_codes:
                pfx = f"{base}-{size}-"
                for key, val in sku_map.items():
                    if key.startswith(pfx):
                        return val

        # Phase 4: fall back to bare base-code entry (e.g. "K810" → default variant)
        for base in base_codes:
            if base and base in sku_map:
                return sku_map[base]

    # ---- Key-substring check: e.g. item_code "License Plate Holder" → key "License Plate" ----
    if item_code:
        ic_lower = item_code.lower()
        for key, val in sku_map.items():
            if not isinstance(val, dict):
                continue
            key_lower = key.strip().lower()
            if len(key_lower) >= 3 and (
                ic_lower.startswith(key_lower) or key_lower.startswith(ic_lower)
            ):
                return val

    # ---- Partial description match against product_name values ----
    if desc:
        desc_words = [w for w in desc.split() if len(w) >= 6]
        for val in sku_map.values():
            if not isinstance(val, dict):
                continue
            pn = (val.get("product_name") or "").lower()
            if pn and desc_words and any(w in pn for w in desc_words):
                return val

    return None


# ---------------------------------------------------------------------------
# Reference-based SKU refinement
# ---------------------------------------------------------------------------

_SIZE_NORM = {
    "SM": "S", "MD": "M", "LG": "L",
    "XSM": "XS", "SML": "S", "MED": "M", "LRG": "L",
}

def _norm_size(s):
    return _SIZE_NORM.get(s.upper(), s.upper())

def _gender_keywords(text):
    t = text.lower()
    if any(w in t for w in ("women", "woman", "ladies", "lady", "girl", "female", "wmn", "w's")):
        return "women"
    if any(w in t for w in ("men", "man", "male", "guy", "boy", "m's")):
        return "men"
    return None

def _score_name_match(ref_name, candidate):
    """
    Simple keyword overlap score between a reference product name and a
    candidate string (description or product_title).  Returns 0..N.
    """
    ref_words  = [w for w in re.split(r"[\s\-/]+", ref_name.lower()) if len(w) >= 3]
    cand_lower = candidate.lower()
    return sum(1 for w in ref_words if w in cand_lower)

def refine_with_reference(item, current_mapping, ref_entries, sku_map):
    """
    Given a parsed invoice item and the current sku_map mapping (may be None),
    try to find a better match using ref_entries (list of {product_name, size, qty}).

    Returns (mapping, ref_matched: bool).
    """
    if not ref_entries:
        return current_mapping, False

    size      = _norm_size((item.get("size") or "").strip())
    item_code = (item.get("item_code") or "").strip()
    desc      = (item.get("description") or "").strip()

    # Find reference entries that match by size
    size_matches = [e for e in ref_entries if _norm_size(e["size"]) == size] if size else ref_entries

    if not size_matches:
        return current_mapping, False

    # Score each size-matched reference entry against the current mapping's product_name
    # and the item description, to pick the best reference line.
    def best_ref_candidate():
        scored = []
        for entry in size_matches:
            ref_name = entry.get("product_name", "")
            # Score against current mapping product_name if available
            cand1 = (current_mapping.get("product_name") or "") if isinstance(current_mapping, dict) else ""
            cand2 = desc
            score = _score_name_match(ref_name, cand1 + " " + cand2)
            scored.append((score, entry))
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] > 0:
            return scored[0][1]
        # If no keyword overlap, only use reference if there's exactly one entry for this size
        if len(size_matches) == 1:
            return size_matches[0]
        return None

    best = best_ref_candidate()
    if best is None:
        return current_mapping, False

    ref_name   = best.get("product_name", "")
    ref_gender = _gender_keywords(ref_name)

    # If we already have a mapping and gender doesn't conflict, just confirm it
    if current_mapping:
        if ref_gender is None:
            return current_mapping, True
        mapping_text = (current_mapping.get("product_name") or "") if isinstance(current_mapping, dict) else ""
        mapping_gender = _gender_keywords(mapping_text)
        if mapping_gender is None or mapping_gender == ref_gender:
            return current_mapping, True
        # Gender conflict — try to find a better matching sku_map entry
        # fall through to search below

    # Try to find a sku_map entry that matches both size and gender from reference
    # First look for composite keys for the item code
    parts = item_code.split("-")
    base_codes = [item_code] + ["-".join(parts[:i]) for i in range(len(parts)-1, 0, -1)]

    best_alt = None
    best_alt_score = 0
    for key, val in sku_map.items():
        if not isinstance(val, dict):
            continue
        pn = (val.get("product_name") or "").lower()
        if not pn:
            continue
        # Must match item base code prefix
        key_upper = key.upper()
        if not any(key_upper.startswith(bc.upper()) for bc in base_codes if bc):
            continue
        pn_gender = _gender_keywords(pn)
        # Skip if gender conflicts with reference
        if ref_gender and pn_gender and pn_gender != ref_gender:
            continue
        # Score by keyword overlap with reference name
        score = _score_name_match(ref_name, pn)
        if ref_gender and pn_gender == ref_gender:
            score += 3  # bonus for gender match
        if score > best_alt_score:
            best_alt_score = score
            best_alt = val

    if best_alt and best_alt_score > 0:
        return best_alt, True

    return current_mapping, bool(current_mapping)


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------

MONEY_RE = re.compile(r"^\$?-?[\d,]+\.?\d*$")
QTY_RE = re.compile(r"^\d{1,5}$")

SKIP_KEYWORDS = {
    "total", "subtotal", "sub-total", "balance due", "amount due",
    "tax", "thank you", "page", "bill to", "ship to", "po number",
    "invoice", "date", "terms", "customer", "order", "account",
    "remit", "payment",
}

SHIPPING_KEYWORDS = ("shipping", "freight", "ship charge", "delivery charge", "handling")
SETUP_KEYWORDS = ("setup", "set up", "set-up")
PMS_KEYWORDS = ("pms", "pms match", "color match", "colour match", "pantone")


def _text(row):
    return " ".join(str(c) for c in row if c).lower()


def is_shipping_row(row):
    t = _text(row)
    return any(k in t for k in SHIPPING_KEYWORDS)


def is_fee_row(row):
    t = _text(row)
    is_setup = any(k in t for k in SETUP_KEYWORDS) and ("fee" in t or "charge" in t or t.strip().startswith("setup"))
    is_pms = any(k in t for k in PMS_KEYWORDS)
    return is_setup or is_pms


def fee_label(row):
    t = _text(row)
    if any(k in t for k in PMS_KEYWORDS):
        return "PMS Match Fee"
    return "Setup Fee"


def parse_money(s):
    if not s:
        return 0.0
    try:
        return float(str(s).strip().replace("$", "").replace(",", ""))
    except ValueError:
        return 0.0


def last_money(row):
    """Return the last money value found in a row (right-most)."""
    for cell in reversed(row):
        if cell and MONEY_RE.match(str(cell).strip()):
            v = parse_money(cell)
            if v > 0:
                return v
    return 0.0


def parse_line_item(row):
    """Try to parse a row as a product line item. Returns dict or None."""
    if not row or len(row) < 3:
        return None
    t = _text(row)
    # Skip rows that are clearly non-item
    if any(kw in t for kw in SKIP_KEYWORDS):
        return None
    non_empty = [str(c).strip() for c in row if c and str(c).strip()]
    if len(non_empty) < 3:
        return None

    # Collect money positions
    money_cells = [(i, parse_money(c)) for i, c in enumerate(row)
                   if c and MONEY_RE.match(str(c).strip())]
    # Need at least one money value
    if not money_cells:
        return None

    total = money_cells[-1][1] if money_cells else 0.0
    unit_cost = money_cells[-2][1] if len(money_cells) >= 2 else 0.0

    # If both are 0 it's probably not a line item
    if total == 0.0 and unit_cost == 0.0:
        return None

    money_idx = {i for i, _ in money_cells}

    # Find qty: small integer not in money positions
    qty = 0
    qty_idx = -1
    for i, cell in enumerate(row):
        if i in money_idx or not cell:
            continue
        cs = str(cell).strip()
        if QTY_RE.match(cs):
            v = int(cs)
            if 1 <= v <= 9999:
                qty = v
                qty_idx = i
                break

    # Remaining text cells → item_code, description, color, size
    text_cells = [str(c).strip() for i, c in enumerate(row)
                  if c and str(c).strip() and i not in money_idx and i != qty_idx]

    item = {
        "item_code": text_cells[0] if len(text_cells) > 0 else "",
        "description": text_cells[1] if len(text_cells) > 1 else "",
        "color": text_cells[2] if len(text_cells) > 2 else "",
        "size": text_cells[3] if len(text_cells) > 3 else "",
        "qty": qty,
        "unit_cost": unit_cost,
        "total": total,
    }

    # Derive qty from unit_cost/total when missing
    if qty == 0 and unit_cost > 0 and total > 0:
        derived = total / unit_cost
        if abs(derived - round(derived)) < 0.01:
            item["qty"] = int(round(derived))

    # Must have some identifier
    if not item["item_code"] and not item["description"]:
        return None

    return item


def parse_invoice_pdf(filepath):
    """Parse a USS invoice PDF into groups keyed by shipping line."""
    invoice_number = ""
    invoice_date = ""
    all_rows = []
    header_rows = []   # table rows extracted before we start line-item parsing

    with pdfplumber.open(filepath) as pdf:
        full_text = ""
        for page in pdf.pages:
            text = page.extract_text() or ""
            full_text += text + "\n"
            for table in (page.extract_tables() or []):
                for row in table:
                    cleaned = [str(c).strip() if c else "" for c in row]
                    if any(cleaned):
                        all_rows.append(cleaned)
                        header_rows.append(cleaned)

    # -----------------------------------------------------------------------
    # Extract invoice number.
    # Priority order matters: "INVOICE #" is always the right field on USS
    # invoices. We must not fall through to patterns that match other number
    # fields like "Estimate No.", "PO #", or "Sales Order #".
    # -----------------------------------------------------------------------

    # Strategy 1: explicit "INVOICE #" in plain text — digits only after the label
    # e.g. "INVOICE # 78892" or "Invoice #: 78892"
    m = re.search(r"INVOICE\s*#\s*[:\-]?\s*(\d+)", full_text, re.I)
    if m:
        invoice_number = m.group(1).strip()

    # Strategy 2: table-row scan — look for a row where one cell is exactly
    # "Invoice #" and the number is in the next cell OR the next row same column.
    # This covers the USS header-block-as-table format.
    if not invoice_number:
        INV_LABEL_RE = re.compile(r"^invoice\s*#?\s*$", re.I)
        for i, row in enumerate(header_rows):
            cells = [c.strip() for c in row if c and str(c).strip()]
            for j, cell in enumerate(cells):
                if INV_LABEL_RE.match(cell):
                    # Same row, next cell
                    if j + 1 < len(cells) and re.match(r"^\d+$", cells[j + 1]):
                        invoice_number = cells[j + 1]
                        break
                    # Next row, same column index (label row / value row pattern)
                    if i + 1 < len(header_rows):
                        raw_next = [str(c).strip() for c in header_rows[i + 1]]
                        if j < len(raw_next) and re.match(r"^\d+$", raw_next[j]):
                            invoice_number = raw_next[j]
                            break
            if invoice_number:
                break

    # Strategy 3: "Invoice" followed by a bare number on the next line
    # (handles text-only PDFs without a table header block)
    if not invoice_number:
        m = re.search(r"^Invoice\s*#?\s*\n\s*(\d{4,})", full_text, re.I | re.M)
        if m:
            invoice_number = m.group(1).strip()

    # Extract date — prefer labelled date first
    m = re.search(r"(?:date|dated)[:\s]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", full_text, re.I)
    if not m:
        m = re.search(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})", full_text)
    if m:
        invoice_date = m.group(1).strip()

    # Header-detection: rows where >= 3 cells match column-header words
    HEADER_WORDS = {"item", "code", "style", "description", "desc", "qty",
                    "quantity", "price", "cost", "total", "size", "color", "colour"}

    groups = []
    current_items = []
    current_fees = []
    group_idx = 1

    for row in all_rows:
        # Skip header rows
        words_in_row = set(re.split(r"\W+", _text(row)))
        if len(words_in_row & HEADER_WORDS) >= 3:
            continue
        if not any(row):
            continue

        if is_shipping_row(row):
            amount = last_money(row)
            if current_items or current_fees:
                groups.append({
                    "group_id": group_idx,
                    "items": current_items[:],
                    "fees": current_fees[:],
                    "shipping": amount,
                })
                group_idx += 1
            current_items = []
            current_fees = []

        elif is_fee_row(row):
            amount = last_money(row)
            if amount > 0:
                current_fees.append({"type": fee_label(row), "amount": amount,
                                     "description": " ".join(c for c in row if c)[:80]})

        else:
            item = parse_line_item(row)
            if item:
                current_items.append(item)

    # Trailing group (no final shipping line)
    if current_items or current_fees:
        groups.append({
            "group_id": group_idx,
            "items": current_items,
            "fees": current_fees,
            "shipping": 0.0,
        })

    return {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# COGS calculation
# ---------------------------------------------------------------------------

def calculate_cogs(groups):
    result = []
    for group in groups:
        items = group.get("items", [])
        fees = group.get("fees", [])
        shipping = float(group.get("shipping", 0))

        total_units = sum(item.get("qty", 0) for item in items)
        total_fees = sum(f.get("amount", 0) for f in fees) + shipping
        fee_per_unit = total_fees / total_units if total_units > 0 else 0.0

        cogs_items = []
        for item in items:
            unit_cost = float(item.get("unit_cost", 0))
            # Use full float precision for fee_per_unit; round only the final COGS
            final_cogs = round(unit_cost + fee_per_unit, 2)
            cogs_items.append({
                **item,
                "fee_per_unit": fee_per_unit,
                "final_cogs": final_cogs,
            })

        result.append({
            "group_id": group["group_id"],
            "items": cogs_items,
            "fees": fees,
            "shipping": shipping,
            "total_units": total_units,
            "total_fees": round(total_fees, 2),
            "fee_per_unit": fee_per_unit,
        })
    return result


# ---------------------------------------------------------------------------
# Shopify helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shopify OAuth – client credentials token cache
# ---------------------------------------------------------------------------

_token_cache: dict = {}     # keyed by store domain → access_token string
_products_cache: dict = {}  # keyed by store domain → list of products


def _fetch_token(store, client_id, client_secret):
    """Exchange client credentials for an access token."""
    r = requests.post(
        f"https://{store}/admin/oauth/access_token",
        json={"client_id": client_id, "client_secret": client_secret,
              "grant_type": "client_credentials"},
        timeout=15,
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise ValueError(f"No access_token in response: {r.text[:200]}")
    return token


def get_shopify_token(store, client_id, client_secret, force_refresh=False):
    """Return a cached token, fetching a fresh one when absent or forced."""
    if not force_refresh and store in _token_cache:
        return _token_cache[store]
    token = _fetch_token(store, client_id, client_secret)
    _token_cache[store] = token
    return token


def _shopify_headers(token):
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}


def _shopify_request(method, url, store, client_id, client_secret, **kwargs):
    """
    Make an authenticated Shopify request.
    If a 401 is returned, clears the token cache and retries once.
    """
    token = get_shopify_token(store, client_id, client_secret)
    r = getattr(requests, method)(url, headers=_shopify_headers(token), **kwargs)
    if r.status_code == 401:
        token = get_shopify_token(store, client_id, client_secret, force_refresh=True)
        r = getattr(requests, method)(url, headers=_shopify_headers(token), **kwargs)
    return r


def get_all_shopify_products(store, client_id, client_secret, use_cache=False):
    if use_cache and store in _products_cache:
        return _products_cache[store]
    products = []
    url = f"https://{store}/admin/api/2026-01/products.json?limit=250&fields=id,title,variants"
    while url:
        r = _shopify_request("get", url, store, client_id, client_secret, timeout=30)
        r.raise_for_status()
        products.extend(r.json().get("products", []))
        link = r.headers.get("Link", "")
        url = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.strip().split(";")[0].strip(" <>")
    _products_cache[store] = products
    return products


def find_variant_by_sku(products, sku):
    for product in products:
        for variant in product.get("variants", []):
            if (variant.get("sku") or "").strip() == sku.strip():
                return {
                    "product_id": product["id"],
                    "product_title": product["title"],
                    "variant_id": variant["id"],
                    "inventory_item_id": variant.get("inventory_item_id"),
                    "sku": variant["sku"],
                }
    return None


def get_inventory_item_cost(store, client_id, client_secret, iid):
    r = _shopify_request(
        "get",
        f"https://{store}/admin/api/2026-01/inventory_items/{iid}.json",
        store, client_id, client_secret, timeout=30,
    )
    if r.status_code == 200:
        cost = r.json().get("inventory_item", {}).get("cost")
        return float(cost) if cost else 0.0
    return 0.0


def update_inventory_item_cost(store, client_id, client_secret, iid, new_cost):
    r = _shopify_request(
        "put",
        f"https://{store}/admin/api/2026-01/inventory_items/{iid}.json",
        store, client_id, client_secret,
        json={"inventory_item": {"id": iid, "cost": str(round(float(new_cost), 2))}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_inventory_levels(store, client_id, client_secret, iids):
    if not iids:
        return {}
    levels = {}
    for i in range(0, len(iids), 50):
        chunk = iids[i:i + 50]
        ids_str = ",".join(str(x) for x in chunk)
        r = _shopify_request(
            "get",
            f"https://{store}/admin/api/2026-01/inventory_levels.json"
            f"?inventory_item_ids={ids_str}&limit=250",
            store, client_id, client_secret, timeout=30,
        )
        if r.status_code != 200:
            continue
        for lv in r.json().get("inventory_levels", []):
            iid = lv["inventory_item_id"]
            levels[iid] = levels.get(iid, 0) + (lv.get("available") or 0)
    return levels


# ---------------------------------------------------------------------------
# Inventory CSV helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    cfg = load_config()
    sku_map = load_sku_map()
    return render_template("index.html", config=cfg, sku_map=sku_map,
                           sku_count=len(sku_map))


@app.route("/upload", methods=["POST"])
def upload_pdf():
    if "pdf" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["pdf"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "File must be a PDF"}), 400
    fname = secure_filename(f.filename)
    fpath = os.path.join(app.config["UPLOAD_FOLDER"], fname)
    f.save(fpath)
    try:
        parsed = parse_invoice_pdf(fpath)
        parsed["cogs_groups"] = calculate_cogs(parsed["groups"])
        # Check if this invoice has already been approved
        inv_num = parsed.get("invoice_number", "").strip()
        duplicate_warning = None
        if inv_num:
            approved = load_config().get("approved_invoices", {})
            if inv_num in approved:
                duplicate_warning = approved[inv_num]
        return jsonify({"success": True, "data": parsed,
                        "duplicate_warning": duplicate_warning})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recalculate", methods=["POST"])
def recalculate():
    data = request.json or {}
    groups = data.get("groups", [])
    try:
        cogs_groups = calculate_cogs(groups)
        return jsonify({"success": True, "cogs_groups": cogs_groups})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/sku-map", methods=["GET"])
def get_sku_map():
    return jsonify(load_sku_map())


@app.route("/sku-map/add", methods=["POST"])
def add_sku_mapping():
    data = request.json or {}
    item_code = data.get("item_code", "").strip()
    sku = data.get("sku", "").strip()
    product_name = data.get("product_name", "").strip()
    size = data.get("size", "").strip()
    if not item_code or not sku:
        return jsonify({"error": "item_code and sku are required"}), 400
    m = load_sku_map()
    m[item_code] = {"sku": sku, "product_name": product_name, "size": size}
    save_sku_map(m)
    return jsonify({"success": True, "sku_map": m})


@app.route("/sku-map/delete", methods=["POST"])
def delete_sku_mapping():
    data = request.json or {}
    item_code = data.get("item_code", "").strip()
    m = load_sku_map()
    if item_code in m:
        del m[item_code]
        save_sku_map(m)
    return jsonify({"success": True})


@app.route("/approval-data", methods=["POST"])
def approval_data():
    data = request.json or {}
    cogs_groups = data.get("cogs_groups", [])
    invoice_number = data.get("invoice_number", "")
    invoice_date = data.get("invoice_date", "")
    ref_entries = data.get("reference", [])  # parsed SKU Reference entries from UI

    sku_map = load_sku_map()
    try:
        store, client_id, client_secret = get_shopify_creds()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        products = get_all_shopify_products(store, client_id, client_secret)
    except Exception as e:
        return jsonify({"error": f"Shopify API error: {e}"}), 500

    rows = []
    for group in cogs_groups:
        for item in group.get("items", []):
            item_code = item.get("item_code", "")
            raw_mapping = find_sku_for_item(item, sku_map)
            mapping, ref_matched = refine_with_reference(item, raw_mapping, ref_entries, sku_map)
            sku = (mapping.get("sku", "") if isinstance(mapping, dict) else str(mapping or ""))
            product_name_override = (mapping.get("product_name", "") if isinstance(mapping, dict) else "")

            base = {
                "item_code": item_code,
                "description": item.get("description", ""),
                "size": item.get("size", ""),
                "color": item.get("color", ""),
                "qty": item.get("qty", 0),
                "new_cogs": round(float(item.get("final_cogs", 0)), 2),
            }

            if not sku:
                rows.append({**base, "sku": "", "prev_cogs": None,
                              "inventory_item_id": None, "changed": False,
                              "product_title": product_name_override or item.get("description", ""),
                              "unmapped": True, "ref_matched": ref_matched})
                continue

            variant = find_variant_by_sku(products, sku)
            if not variant:
                rows.append({**base, "sku": sku, "prev_cogs": None,
                              "inventory_item_id": None, "changed": False,
                              "product_title": product_name_override or item.get("description", ""),
                              "not_found": True, "ref_matched": ref_matched})
                continue

            iid = variant["inventory_item_id"]
            try:
                prev_cogs = round(get_inventory_item_cost(store, client_id, client_secret, iid), 2)
            except Exception:
                prev_cogs = 0.0

            new_cogs = base["new_cogs"]
            changed = abs(new_cogs - prev_cogs) > 0.001

            rows.append({
                **base,
                "sku": sku,
                "prev_cogs": prev_cogs,
                "inventory_item_id": iid,
                "changed": changed,
                "change_amount": round(new_cogs - prev_cogs, 2),
                "product_title": product_name_override or variant.get("product_title", ""),
                "ref_matched": ref_matched,
            })

    return jsonify({"success": True, "rows": rows,
                    "invoice_number": invoice_number,
                    "invoice_date": invoice_date})


@app.route("/approve", methods=["POST"])
def approve():
    data = request.json or {}
    approved_skus = set(data.get("approved_skus", []))
    invoice_number = data.get("invoice_number", "")
    invoice_date = data.get("invoice_date", "")
    rows = data.get("rows", [])
    is_duplicate = bool(data.get("is_duplicate", False))

    cfg = load_config()
    try:
        store, client_id, client_secret = get_shopify_creds()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    sheet_url = cfg.get("google_sheet_url", "")

    results = {"shopify": [], "sheet": None, "errors": []}
    _today = date.today()
    today = f"{_today.month}/{_today.day}/{_today.year}"  # M/D/YYYY e.g. "4/15/2026"

    approved_rows = [r for r in rows
                     if r.get("sku") in approved_skus and r.get("changed") and r.get("inventory_item_id")]

    # 1. Update Shopify
    for row in approved_rows:
        try:
            update_inventory_item_cost(store, client_id, client_secret, row["inventory_item_id"], row["new_cogs"])
            results["shopify"].append({"sku": row["sku"], "status": "updated"})
        except Exception as e:
            results["errors"].append(f"Shopify [{row['sku']}]: {e}")

    # 2. Push to Google Sheet
    if sheet_url and approved_rows:
        sheet_rows = []
        for row in approved_rows:
            prev  = round(float(row.get("prev_cogs",  0) or 0), 2)
            new_c = round(float(row.get("new_cogs",   0) or 0), 2)
            change = round(new_c - prev, 2)
            product_name = row.get("product_title") or row.get("description", "")
            if is_duplicate:
                product_name = f"DUPLICATE - Invoice previously approved | {product_name}"
            sheet_rows.append({
                "Product Name":    product_name,
                "SKU":             row.get("sku", ""),
                "Date Changed":    today,
                "Quote #":         invoice_number,
                "Quote Date":      invoice_date,
                "Quoted Quantity": str(row.get("qty", 0)),
                "Previous COGS":   f"${prev:.2f}",
                "New COGS":        f"${new_c:.2f}",
                "Change $":        f"${change:.2f}",
            })
        # Payload is a bare JSON array — matches what the Apps Script doPost expects.
        payload = sheet_rows
        payload_json = json.dumps(payload)
        print("\n[NSLS] ── Google Sheet payload ──────────────────────────")
        print(f"[NSLS] URL: {sheet_url}")
        print(f"[NSLS] Exact JSON being sent:\n{payload_json}")
        print("[NSLS] ─────────────────────────────────────────────────\n")
        try:
            resp = requests.post(
                sheet_url,
                data=payload_json.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            print(f"[NSLS] Sheet response: HTTP {resp.status_code} — {resp.text[:300]}")
            results["sheet"] = {
                "status": resp.status_code,
                "body": resp.text[:300],
                "payload": payload,
            }
        except Exception as e:
            print(f"[NSLS] Sheet push error: {e}")
            results["errors"].append(f"Sheet push: {e}")
            results["sheet"] = {"status": None, "body": str(e), "payload": payload}

    # 3. Auto-save item_code → sku mappings so these items are never unmapped again.
    # Items with known logo-type variants (NE904, DT607) must never be saved under
    # a bare item_code key because that would override both variants on the next run.
    VARIANT_ITEMS = {"NE904", "DT607"}
    sku_map = load_sku_map()
    sku_map_updated = False
    for row in approved_rows:
        ic  = (row.get("item_code") or "").strip()
        sz  = (row.get("size")      or "").strip()
        cl  = (row.get("color")     or "").strip()
        sku = (row.get("sku")       or "").strip()
        if not ic or not sku:
            continue
        if sz and cl:
            key = f"{ic}-{sz}-{cl}"
        elif sz:
            key = f"{ic}-{sz}"
        else:
            # For variant items without a size, try to extract logo type from product_title
            pt = (row.get("product_title", "") or "").lower()
            if ic.upper() in VARIANT_ITEMS:
                if "crest" in pt:
                    key = f"{ic}-Crest Logo"
                elif "nsls" in pt and ic.upper() == "NE904":
                    key = f"{ic}-NSLS Logo"
                elif "greek" in pt:
                    key = f"{ic}-Greek Letters"
                elif "nsls" in pt and ic.upper() == "DT607":
                    key = f"{ic}-NSLS Letters"
                else:
                    continue  # can't disambiguate — skip to avoid stomping variants
            else:
                key = ic
        if key not in sku_map:
            sku_map[key] = {
                "sku": sku,
                "product_name": row.get("product_title", ""),
                "size": sz,
            }
            sku_map_updated = True
    if sku_map_updated:
        save_sku_map(sku_map)

    # 4. Record invoice approval so future uploads can detect duplicates.
    # Gate on approved_skus (user intent), not approved_rows (which requires changed=True
    # and may be empty on a second approval of the same invoice when COGS are unchanged).
    if invoice_number and approved_skus:
        cfg = load_config()
        approved = cfg.setdefault("approved_invoices", {})
        if invoice_number not in approved:
            approved[invoice_number] = {"first_approved": today, "approval_count": 1}
        else:
            approved[invoice_number]["approval_count"] = (
                approved[invoice_number].get("approval_count", 1) + 1
            )
            approved[invoice_number]["last_duplicate"] = today
        save_config(cfg)

    return jsonify({"success": True, "results": results,
                    "updated_count": len(results["shopify"])})



@app.route("/config", methods=["GET", "POST"])
def config_route():
    if request.method == "POST":
        data = request.json or {}
        cfg = load_config()
        # Always save store and sheet URL (user can blank them out intentionally).
        # For secret credentials, only overwrite if the new value is non-empty
        # so that leaving a password field blank preserves the stored value.
        for key in ("shopify_store", "shopify_client_id", "google_sheet_url"):
            if key in data:
                cfg[key] = data[key]
        if data.get("shopify_client_secret"):
            cfg["shopify_client_secret"] = data["shopify_client_secret"]
        # Clear caches so the next API call re-authenticates with fresh data
        _token_cache.clear()
        _products_cache.clear()
        save_config(cfg)
        return jsonify({"success": True})
    cfg = load_config()
    safe = {k: v for k, v in cfg.items() if k != "shopify_client_secret"}
    safe["has_secret"] = bool(cfg.get("shopify_client_secret"))
    return jsonify(safe)


@app.route("/shopify-auth-test")
def shopify_auth_test():
    try:
        store, client_id, client_secret = get_shopify_creds()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        token = get_shopify_token(store, client_id, client_secret, force_refresh=True)
        return jsonify({"success": True, "token_prefix": token[:8] + "…"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def load_products_csv():
    """
    Read Shopify inventory export CSV (inventory_export_1.csv).
    Falls back to products_export_1.csv if present.
    Returns list of {sku, label} dicts for the SKU picker dropdown.
    """
    for filename in ("inventory_export_1.csv", "products_export_1.csv"):
        path = os.path.join(BASE_DIR, filename)
        if os.path.exists(path):
            break
    else:
        return []

    results = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                # inventory_export uses "SKU"; products_export uses "Variant SKU"
                sku = (row.get("SKU") or row.get("Variant SKU") or "").strip()
                if not sku:
                    continue
                title = row.get("Title", "").strip()
                opts = [row.get(f"Option{i} Value", "").strip() for i in (1, 2, 3)]
                opt_str = " / ".join(
                    o for o in opts if o and o.lower() not in ("default title", "")
                )
                label = title + (f" — {opt_str}" if opt_str else "")
                results.append({"sku": sku, "label": label})
    except Exception:
        pass
    return results


@app.route("/sku-options")
def sku_options():
    """Combined SKU list for the unmapped-row picker: sku_map + products_export_1.csv."""
    sku_map = load_sku_map()
    seen = {}
    # From sku_map (deduplicated by SKU value)
    for val in sku_map.values():
        if not isinstance(val, dict):
            continue
        sku = val.get("sku", "").strip()
        if not sku or sku in seen:
            continue
        name = val.get("product_name", "")
        size = val.get("size", "")
        label = name + (f" — {size}" if size else "")
        seen[sku] = label
    # From products CSV — only add SKUs not already covered by sku_map
    for item in load_products_csv():
        sku = item["sku"]
        if sku and sku not in seen:
            seen[sku] = item["label"]
    options = sorted(
        [{"sku": k, "label": v} for k, v in seen.items()],
        key=lambda x: x["label"].lower(),
    )
    return jsonify(options)


@app.route("/resolve-sku")
def resolve_sku():
    """Look up a Shopify SKU and return its inventory_item_id and current cost."""
    sku = request.args.get("sku", "").strip()
    if not sku:
        return jsonify({"error": "sku required"}), 400
    try:
        store, client_id, client_secret = get_shopify_creds()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        products = get_all_shopify_products(store, client_id, client_secret, use_cache=True)
        variant = find_variant_by_sku(products, sku)
        if not variant:
            return jsonify({"found": False})
        iid = variant["inventory_item_id"]
        prev_cogs = round(get_inventory_item_cost(store, client_id, client_secret, iid), 2)
        return jsonify({
            "found": True,
            "inventory_item_id": iid,
            "prev_cogs": prev_cogs,
            "product_title": variant.get("product_title", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
