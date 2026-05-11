import os
import base64
import csv
import json
import re
import requests
import pdfplumber
from datetime import date
from flask import Flask, render_template, request, jsonify, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)

# ---------------------------------------------------------------------------
# HTTP Basic Auth — set DASHBOARD_PASSWORD env var to enable.
# Username is ignored; any non-empty password match grants access.
# ---------------------------------------------------------------------------
_DASH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
print(f"[NSLS] Auth enabled: {bool(_DASH_PASSWORD)}")


def _check_auth(req):
    if not _DASH_PASSWORD:
        return True
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8", errors="replace")
        password = decoded.split(":", 1)[1] if ":" in decoded else decoded
        return password == _DASH_PASSWORD
    except Exception:
        return False


def _auth_required():
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="NSLS COGS Dashboard"'},
    )


@app.before_request
def require_auth():
    if not _check_auth(request):
        return _auth_required()


@app.errorhandler(Exception)
def _json_exception(e):
    import traceback
    tb = traceback.format_exc()
    print(f"[NSLS] Unhandled exception: {e}\n{tb}")
    return jsonify({"error": str(e), "detail": tb[-800:]}), 500


@app.errorhandler(404)
def _json_404(e):
    return jsonify({"error": "Not found", "path": request.path}), 404


@app.errorhandler(405)
def _json_405(e):
    return jsonify({"error": "Method not allowed"}), 405

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

# ---------------------------------------------------------------------------
# JSONBin.io — remote persistence for sku_map and app data (approved invoices
# + reorder settings).  Used automatically on Render where the local
# filesystem is ephemeral.  Falls back to local files when env vars are unset
# so local dev is completely unaffected.
# ---------------------------------------------------------------------------
_JSONBIN_BASE = "https://api.jsonbin.io/v3"


def _jb_headers():
    return {"X-Master-Key": os.environ.get("JSONBIN_API_KEY", ""),
            "Content-Type": "application/json"}


def _jb_read(bin_id):
    try:
        r = requests.get(f"{_JSONBIN_BASE}/b/{bin_id}/latest",
                         headers=_jb_headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get("record")
        print(f"[NSLS] JSONBin read {bin_id}: HTTP {r.status_code} — {r.text[:200]}")
    except Exception as exc:
        print(f"[NSLS] JSONBin read {bin_id}: {exc}")
    return None


def _jb_write(bin_id, data):
    try:
        r = requests.put(f"{_JSONBIN_BASE}/b/{bin_id}", json=data,
                         headers=_jb_headers(), timeout=10)
        if r.status_code == 200:
            return True
        print(f"[NSLS] JSONBin write {bin_id}: HTTP {r.status_code} — {r.text[:200]}")
    except Exception as exc:
        print(f"[NSLS] JSONBin write {bin_id}: {exc}")
    return False


def _jb_create(name, initial_data):
    try:
        r = requests.post(f"{_JSONBIN_BASE}/b", json=initial_data,
                          headers={**_jb_headers(),
                                   "X-Bin-Name": name,
                                   "X-Bin-Private": "true"},
                          timeout=10)
        if r.status_code == 200:
            return r.json()["metadata"]["id"]
        print(f"[NSLS] JSONBin create {name!r}: HTTP {r.status_code} — {r.text[:200]}")
    except Exception as exc:
        print(f"[NSLS] JSONBin create {name!r}: {exc}")
    return None


def _init_jsonbin():
    """
    Called once on startup.  If JSONBIN_API_KEY is present but the bin IDs
    are not yet set, create the bins, seed them with whatever is in the local
    files, and print the new IDs so the operator can add them as env vars.
    """
    if not os.environ.get("JSONBIN_API_KEY"):
        return

    if not os.environ.get("JSONBIN_SKU_MAP_BIN_ID"):
        seed = {}
        if os.path.exists(SKU_MAP_PATH):
            with open(SKU_MAP_PATH) as f:
                seed = json.load(f)
        bin_id = _jb_create("nsls-sku-map", seed)
        if bin_id:
            print("[NSLS] ══════════════════════════════════════════════════════")
            print("[NSLS]  New bin created — add this Render env var:")
            print(f"[NSLS]  JSONBIN_SKU_MAP_BIN_ID = {bin_id}")
            print("[NSLS] ══════════════════════════════════════════════════════")

    if not os.environ.get("JSONBIN_APP_DATA_BIN_ID"):
        seed: dict = {"approved_invoices": {}, "reorder_settings": {}}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                stored = json.load(f)
            seed["approved_invoices"] = stored.get("approved_invoices", {})
            seed["reorder_settings"]  = stored.get("reorder_settings", {})
        bin_id = _jb_create("nsls-app-data", seed)
        if bin_id:
            print("[NSLS] ══════════════════════════════════════════════════════")
            print("[NSLS]  New bin created — add this Render env var:")
            print(f"[NSLS]  JSONBIN_APP_DATA_BIN_ID = {bin_id}")
            print("[NSLS] ══════════════════════════════════════════════════════")


# ---------------------------------------------------------------------------
# Config / SKU-map helpers
# ---------------------------------------------------------------------------

def load_config():
    cfg = {"shopify_store": "", "shopify_client_id": "", "shopify_client_secret": "",
           "google_sheet_url": "", "reorder_settings": {}}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    # Environment variables override the file so the app works on Render
    # without a committed config.json.
    for env_var, cfg_key in (
        ("SHOPIFY_STORE",         "shopify_store"),
        ("SHOPIFY_CLIENT_ID",     "shopify_client_id"),
        ("SHOPIFY_CLIENT_SECRET", "shopify_client_secret"),
        ("GOOGLE_SHEET_URL",      "google_sheet_url"),
    ):
        val = os.environ.get(env_var, "").strip()
        if val:
            cfg[cfg_key] = val
    # Overlay approved_invoices + reorder_settings from JSONBin when available
    bin_id = os.environ.get("JSONBIN_APP_DATA_BIN_ID", "")
    if os.environ.get("JSONBIN_API_KEY") and bin_id:
        app_data = _jb_read(bin_id)
        if app_data is not None:
            if "approved_invoices" in app_data:
                cfg["approved_invoices"] = app_data["approved_invoices"]
            if "reorder_settings" in app_data:
                cfg["reorder_settings"] = app_data["reorder_settings"]
    return cfg


def save_config(cfg):
    # Persist approved_invoices + reorder_settings to JSONBin on Render
    bin_id = os.environ.get("JSONBIN_APP_DATA_BIN_ID", "")
    if os.environ.get("JSONBIN_API_KEY") and bin_id:
        _jb_write(bin_id, {
            "approved_invoices": cfg.get("approved_invoices", {}),
            "reorder_settings":  cfg.get("reorder_settings", {}),
        })
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_sku_map():
    bin_id = os.environ.get("JSONBIN_SKU_MAP_BIN_ID", "")
    if os.environ.get("JSONBIN_API_KEY") and bin_id:
        data = _jb_read(bin_id)
        if data is not None:
            return data
    if os.path.exists(SKU_MAP_PATH):
        with open(SKU_MAP_PATH) as f:
            return json.load(f)
    return {}


def save_sku_map(m):
    bin_id = os.environ.get("JSONBIN_SKU_MAP_BIN_ID", "")
    if os.environ.get("JSONBIN_API_KEY") and bin_id:
        _jb_write(bin_id, m)
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

        # Phase 4b: color-as-size fallback (USS invoices put actual size in the color column)
        _SIZE_LIKE = {"XS","S","M","L","XL","XXL","2XL","XXXL","3XL","4XL","5XL","SM","MD","LG","XSM","SML","MED","LRG"}
        if color and color.strip().upper() in _SIZE_LIKE:
            alt_size = _norm_size(color.strip())
            for base in base_codes:
                k = f"{base}-{alt_size}"
                if k in sku_map: return sku_map[k]

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
# Reference-based SKU matching (Priority 1 in the 3-tier lookup)
# ---------------------------------------------------------------------------

_SIZE_NORM = {
    "SM": "S", "MD": "M", "LG": "L",
    "XSM": "XS", "SML": "S", "MED": "M", "LRG": "L",
}

_COLOR_WORDS = {
    "black", "navy", "white", "red", "blue", "grey", "gray", "green",
    "yellow", "orange", "purple", "pink", "brown", "maroon", "royal",
    "cardinal", "gold", "blacktop", "charcoal", "heather", "natural",
    "forest", "cobalt", "silver", "coral", "teal", "cranberry",
}

def _norm_size(s):
    s = s.strip().upper() if s else ""
    return _SIZE_NORM.get(s, s)

def _gender_of(text):
    t = text.lower()
    if any(w in t for w in ("women", "woman", "ladies", "lady", "girl", "female")):
        return "women"
    if re.search(r"\bmen\b|\bman\b|\bmale\b|\bboy\b", t):
        return "men"
    return None

def _load_csv_products():
    """
    Load inventory_export_1.csv into a flat list of variant dicts.
    Each entry: {title, sku, size, color, handle}
    """
    for filename in ("inventory_export_1.csv", "products_export_1.csv"):
        path = os.path.join(BASE_DIR, filename)
        if os.path.exists(path):
            break
    else:
        return []

    rows = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sku = (row.get("SKU") or row.get("Variant SKU") or "").strip()
                if not sku:
                    continue
                title  = row.get("Title", "").strip()
                handle = row.get("Handle", "").strip()
                # Build option lookup: {option_name_lower: option_value}
                opts = {}
                for i in (1, 2, 3):
                    name  = (row.get(f"Option{i} Name")  or "").strip().lower()
                    value = (row.get(f"Option{i} Value") or "").strip()
                    if name and value and value.lower() not in ("default title", ""):
                        opts[name] = value
                rows.append({
                    "title":  title,
                    "sku":    sku,
                    "size":   opts.get("size",  ""),
                    "color":  opts.get("color", ""),
                    "handle": handle,
                })
    except Exception:
        pass
    return rows


def _score_csv_title(ref_name, csv_title):
    """
    Count meaningful words (>=4 chars, not generic stop-words) from ref_name
    that appear in csv_title.
    """
    STOP = {"with", "from", "this", "that", "nsls", "logo"}
    words = [
        w for w in re.split(r"[\s\-/,.\'\"()\u00ae\u2122&]+", ref_name.lower())
        if len(w) >= 4 and w not in STOP
    ]
    title_lc = csv_title.lower()
    return sum(1 for w in words if w in title_lc)


def _find_sku_in_csv(ref_product_name, target_size, csv_rows):
    """
    Search csv_rows for the variant whose title best matches ref_product_name
    and whose size option equals target_size.

    Returns {sku, title, color} or None.
    Minimum score of 2 required to avoid false matches.
    """
    target_size_up = _norm_size(target_size)
    ref_gender = _gender_of(ref_product_name)
    ref_lc = ref_product_name.lower()

    ref_color = next(
        (c for c in _COLOR_WORDS if re.search(r"\b" + c + r"\b", ref_lc)), None
    )

    scored = []
    for row in csv_rows:
        if not row["sku"]:
            continue
        if target_size_up and _norm_size(row["size"]) != target_size_up:
            continue
        row_gender = _gender_of(row["title"])
        if ref_gender and row_gender and ref_gender != row_gender:
            continue

        score = _score_csv_title(ref_product_name, row["title"])
        if score <= 0:
            continue
        if ref_gender and row_gender == ref_gender:
            score += 2
        if ref_color and row["color"] and ref_color in row["color"].lower():
            score += 2

        scored.append((score, row))

    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    best_score, best_row = scored[0]
    if best_score < 2:
        return None
    return {"sku": best_row["sku"], "title": best_row["title"], "color": best_row["color"]}


# Words that appear in virtually every reference entry or are too generic to
# discriminate between products.
_REF_STOP = {
    '', 'the', 'and', 'for', 'a', 'an', 'of', 'to', 'in', 'by', 'at',
    'is', 'it', 'its', 'or', 's', 'nsls',
}


def _desc_words(text):
    """Split text into a set of normalised words, filtering stop words and
    short tokens that carry no discriminating signal."""
    return {
        w for w in re.split(r"[\s\-/,.()'\"®™&]+", text.lower())
        if len(w) >= 3 and w not in _REF_STOP
    }


def _word_overlap_score(desc, ref_name):
    """
    Word overlap ratio between an invoice description and a reference product name.

    Score = |common words| / max(|desc_words|, |ref_words|)

    A gender conflict (e.g. description says "Ladies" but ref says "Men's")
    forces the score to 0.  A confirmed gender match adds +0.15 to break ties.

    Returns 0.0 .. ~1.15.
    """
    words_d = _desc_words(desc)
    words_r = _desc_words(ref_name)
    if not words_d or not words_r:
        return 0.0

    common = words_d & words_r
    score = len(common) / max(len(words_d), len(words_r)) if max(len(words_d), len(words_r)) else 0.0
    if score == 0.0:
        return 0.0

    # Gender awareness — _gender_of understands synonyms (ladies → women, etc.)
    desc_gender = _gender_of(desc)
    ref_gender  = _gender_of(ref_name)
    if desc_gender and ref_gender:
        if desc_gender != ref_gender:
            return 0.0    # hard conflict — wrong product
        score += 0.15     # confirmed gender match bonus

    return score


def match_by_reference(item, ref_entries, csv_rows):
    """
    Priority-1 matching: description word overlap → size filter → CSV lookup.

    Algorithm:
      1. Collect unique product names from the reference table.
      2. Score each name against the invoice item description using word overlap
         ratio (see _word_overlap_score).  Require best score >= 0.30.
      3. Within the winning product name, require a reference entry whose size
         matches the invoice item's size.
      4. Search inventory_export_1.csv for the variant with that product title
         and size option value.
      6. Return {sku, product_name, sku_map_key}.

    Logs every match attempt to the Flask console for debugging.
    """
    if not ref_entries or not csv_rows:
        return None

    item_size  = _norm_size((item.get("size")         or "").strip())
    item_code  = (item.get("item_code")   or "").strip()
    item_desc  = (item.get("description") or "").strip()
    item_color = (item.get("color")       or "").strip()

    match_text = item_desc or item_code
    if not match_text:
        return None

    # --- Step 2: score every unique reference product name ----------------
    seen: dict = {}
    for entry in ref_entries:
        ref_name = (entry.get("product_name") or "").strip()
        if not ref_name or ref_name in seen:
            continue
        seen[ref_name] = _word_overlap_score(match_text, ref_name)

    if not seen:
        return None

    ranked = sorted(seen.items(), key=lambda x: -x[1])
    best_name, best_score = ranked[0]

    print(f"[NSLS REF] {item_code!r} | desc={item_desc!r} | size={item_size!r}")
    for name, sc in ranked[:3]:
        marker = " <- BEST" if name == best_name else ""
        print(f"           score={sc:.2f}  ref={name!r}{marker}")

    # --- Step 3: threshold check ------------------------------------------
    if best_score < 0.30:
        print(f"[NSLS REF]   best score {best_score:.2f} < 0.30 -> no reference match")
        return None

    # --- Step 4: require a size-matched entry for the winning product ------
    if item_size:
        size_entries = [
            e for e in ref_entries
            if (e.get("product_name") or "").strip() == best_name
            and _norm_size(e.get("size", "")) == item_size
        ]
        if not size_entries:
            print(f"[NSLS REF]   no ref entry for size {item_size!r} "
                  f"under {best_name!r} -> no reference match")
            return None

    # --- Step 5: CSV lookup -----------------------------------------------
    csv_match = _find_sku_in_csv(best_name, item_size, csv_rows)
    if not csv_match:
        print(f"[NSLS REF]   CSV lookup failed: {best_name!r} size={item_size!r}")
        return None

    print(f"[NSLS REF]   -> SKU={csv_match['sku']!r}  title={csv_match['title']!r}")

    # --- Step 6: build sku_map key ----------------------------------------
    # Use invoice's color field for the key when available — it's the value
    # find_sku_for_item will see in future invoices (Phase 2: item_code-size-color).
    color_for_key = item_color or csv_match.get("color", "")
    if item_size and color_for_key:
        sku_map_key = f"{item_code}-{item_size}-{color_for_key}"
    elif item_size:
        sku_map_key = f"{item_code}-{item_size}"
    else:
        sku_map_key = item_code

    return {
        "sku":          csv_match["sku"],
        "product_name": csv_match["title"],
        "sku_map_key":  sku_map_key,
    }

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

_token_cache: dict = {}          # keyed by store domain → access_token string
_products_cache: dict = {}       # keyed by store domain → list of products
_uss_location_cache: dict = {}   # keyed by store domain → USS location_id
_sku_iid_cache: dict = {}        # keyed by store domain → {sku: inventory_item_id}


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


def get_uss_location_id(store, client_id, client_secret):
    """Return the Shopify location_id for 'Underground Sports Shop', cached per store."""
    if store in _uss_location_cache:
        return _uss_location_cache[store]
    url = f"https://{store}/admin/api/2026-01/locations.json"
    print(f"[NSLS] locations.json — GET {url}")
    try:
        r = _shopify_request("get", url, store, client_id, client_secret, timeout=30)
        print(f"[NSLS] locations.json — HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"[NSLS] locations.json — error body: {r.text[:500]}")
            return None
        locations = r.json().get("locations", [])
        names = [loc.get("name") for loc in locations]
        print(f"[NSLS] locations.json — found {len(locations)} location(s): {names}")
        for loc in locations:
            if (loc.get("name") or "").strip().lower() == "underground sports shop":
                loc_id = loc["id"]
                _uss_location_cache[store] = loc_id
                print(f"[NSLS] locations.json — USS location_id cached: {loc_id}")
                return loc_id
        print("[NSLS] locations.json — 'Underground Sports Shop' not found in list above")
    except Exception as exc:
        print(f"[NSLS] locations.json — exception: {exc}")
    return None


def build_sku_iid_map(store, client_id, client_secret):
    """
    Build a {{sku: inventory_item_id}} dict by paginating /variants.json.
    Cached per store for the app session; only fetched once.
    """
    if store in _sku_iid_cache:
        return _sku_iid_cache[store]
    result = {}
    url = (f"https://{store}/admin/api/2026-01/variants.json"
           f"?fields=id,sku,inventory_item_id&limit=250")
    page = 0
    while url:
        page += 1
        print(f"[NSLS] variants.json page {page} — GET {url}")
        try:
            r = _shopify_request("get", url, store, client_id, client_secret, timeout=30)
            print(f"[NSLS] variants.json page {page} — HTTP {r.status_code}")
            if r.status_code != 200:
                print(f"[NSLS] variants.json page {page} — error body: {r.text[:500]}")
                break
            for v in r.json().get("variants", []):
                sku = (v.get("sku") or "").strip()
                iid = v.get("inventory_item_id")
                if sku and iid:
                    result[sku] = iid
            link = r.headers.get("Link", "")
            url = None
            if 'rel="next"' in link:
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.strip().split(";")[0].strip(" <>")
        except Exception as exc:
            print(f"[NSLS] variants.json page {page} — exception: {exc}")
            break
    print(f"[NSLS] variants.json — built SKU→IID map: {len(result)} SKU(s) across {page} page(s)")
    _sku_iid_cache[store] = result
    return result


def get_live_on_hand_batch(store, client_id, client_secret, skus):
    """
    Fetch live available quantities at the Underground Sports Shop location for the
    given SKUs. Returns {{sku: int}}. Empty dict on any failure — caller falls back to CSV.

    Sequence:
      1. GET /locations.json  — resolve USS location_id (cached)
      2. GET /variants.json   — resolve SKUs → inventory_item_ids (cached)
      3. GET /inventory_levels.json?inventory_item_ids=...&location_ids=...
    """
    if not skus:
        return {}

    # Step 1 — USS location ID
    location_id = get_uss_location_id(store, client_id, client_secret)
    if not location_id:
        print("[NSLS] inventory_levels — skipping: USS location_id unavailable")
        return {}

    # Step 2 — SKU → inventory_item_id
    sku_iid_map = build_sku_iid_map(store, client_id, client_secret)
    iid_to_sku: dict = {}
    missing: list = []
    for sku in skus:
        iid = sku_iid_map.get(sku)
        if iid:
            iid_to_sku[iid] = sku
        else:
            missing.append(sku)
    if missing:
        print(f"[NSLS] inventory_levels — {len(missing)} SKU(s) not in variants map: {missing}")
    if not iid_to_sku:
        return {}

    # Step 3 — inventory_levels in batches of 50
    result: dict = {}
    all_iids = list(iid_to_sku.keys())
    for i in range(0, len(all_iids), 50):
        chunk = all_iids[i:i + 50]
        ids_str = ",".join(str(x) for x in chunk)
        url = (f"https://{store}/admin/api/2026-01/inventory_levels.json"
               f"?inventory_item_ids={ids_str}&location_ids={location_id}&limit=250")
        print(f"[NSLS] inventory_levels.json — GET {url}")
        try:
            r = _shopify_request("get", url, store, client_id, client_secret, timeout=30)
            print(f"[NSLS] inventory_levels.json — HTTP {r.status_code}")
            if r.status_code != 200:
                print(f"[NSLS] inventory_levels.json — error body: {r.text[:500]}")
                continue
            levels = r.json().get("inventory_levels", [])
            print(f"[NSLS] inventory_levels.json — {len(levels)} level(s) returned")
            for lv in levels:
                iid = lv["inventory_item_id"]
                sku = iid_to_sku.get(iid)
                if sku is not None:
                    result[sku] = lv.get("available") or 0
        except Exception as exc:
            print(f"[NSLS] inventory_levels.json — exception: {exc}")
            continue

    print(f"[NSLS] inventory_levels — resolved {len(result)}/{len(skus)} SKU(s)")
    return result


def get_all_inventory_costs(store, client_id, client_secret, iids):
    """Batch-fetch cost for a list of inventory_item_ids.  Returns {iid: cost_float}."""
    if not iids:
        return {}
    costs = {}
    for i in range(0, len(iids), 100):
        chunk = iids[i:i + 100]
        ids_str = ",".join(str(x) for x in chunk)
        try:
            r = _shopify_request(
                "get",
                f"https://{store}/admin/api/2026-01/inventory_items.json"
                f"?ids={ids_str}&limit=250",
                store, client_id, client_secret, timeout=30,
            )
            if r.status_code == 200:
                for inv_item in r.json().get("inventory_items", []):
                    cost = inv_item.get("cost")
                    if cost is not None:
                        costs[inv_item["id"]] = round(float(cost), 2)
        except Exception:
            pass
    return costs


# ---------------------------------------------------------------------------
# Inventory CSV helpers
# ---------------------------------------------------------------------------

def load_inventory_on_hand():
    """
    Read inventory_export_1.csv and return {sku: on_hand_qty} for
    Location = 'Underground Sports Shop'.  'not stocked' rows → 0.
    """
    path = os.path.join(BASE_DIR, "inventory_export_1.csv")
    if not os.path.exists(path):
        return {}
    result = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("Location") or "").strip() != "Underground Sports Shop":
                    continue
                sku = (row.get("SKU") or "").strip()
                if not sku:
                    continue
                raw = (row.get("On hand (current)") or "").strip()
                try:
                    qty = int(float(raw)) if raw and raw.lower() not in ("not stocked", "") else 0
                except (ValueError, TypeError):
                    qty = 0
                result[sku] = qty
    except Exception:
        pass
    return result


def calc_weighted_avg(prev_cogs, on_hand, invoice_cost, qty):
    """Return weighted-average COGS, rounded to 2 dp.  Falls back to invoice_cost when on_hand <= 0."""
    if on_hand <= 0:
        return round(float(invoice_cost), 2)
    return round(((on_hand * float(prev_cogs)) + (qty * float(invoice_cost))) / (on_hand + qty), 2)


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
        # Filename fallback: if PDF text parser couldn't find an invoice number,
        # extract it from the filename pattern "Inv_{number}_from..." or "Est_{number}_from..."
        inv_num = parsed.get("invoice_number", "").strip()
        if not inv_num:
            m = re.search(r"(?:Inv|Est)_(\d+)_", f.filename, re.I)
            if m:
                inv_num = m.group(1)
                parsed["invoice_number"] = inv_num
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
    data = request.get_json(silent=True) or {}
    groups = data.get("groups", [])
    try:
        cogs_groups = calculate_cogs(groups)
        return jsonify({"success": True, "cogs_groups": cogs_groups})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cogs-preview", methods=["POST"])
def cogs_preview():
    """
    Batch-compute weighted-avg COGS for the COGS Calculator tab.
    Returns {gi}-{ii} keyed items with prev_cogs, on_hand, weighted_avg.
    """
    data = request.get_json(silent=True) or {}
    cogs_groups = data.get("cogs_groups", [])

    sku_map = load_sku_map()
    try:
        store, client_id, client_secret = get_shopify_creds()
        products = get_all_shopify_products(store, client_id, client_secret, use_cache=True)
    except Exception:
        return jsonify({"success": False, "items": {}})

    on_hand_map = load_inventory_on_hand()

    # Resolve SKU and inventory_item_id for every line item
    pending = {}   # "{gi}-{ii}" → {item, sku, iid}
    for gi, group in enumerate(cogs_groups):
        for ii, item in enumerate(group.get("items", [])):
            mapping = find_sku_for_item(item, sku_map)
            sku = (mapping.get("sku", "") if isinstance(mapping, dict) else str(mapping or ""))
            if not sku:
                continue
            variant = find_variant_by_sku(products, sku)
            if not variant:
                continue
            pending[f"{gi}-{ii}"] = {"item": item, "sku": sku, "iid": variant["inventory_item_id"]}

    # Batch-fetch all costs in one round-trip per 100 items
    iids = [v["iid"] for v in pending.values()]
    costs = get_all_inventory_costs(store, client_id, client_secret, iids)

    result = {}
    for key, entry in pending.items():
        sku          = entry["sku"]
        iid          = entry["iid"]
        item         = entry["item"]
        prev_cogs    = costs.get(iid, 0.0)
        on_hand      = on_hand_map.get(sku, 0)
        invoice_cost = round(float(item.get("final_cogs", 0)), 2)
        qty          = item.get("qty", 0)
        weighted_avg = calc_weighted_avg(prev_cogs, on_hand, invoice_cost, qty)
        result[key]  = {
            "sku":          sku,
            "prev_cogs":    prev_cogs,
            "on_hand":      on_hand,
            "weighted_avg": weighted_avg,
        }

    return jsonify({"success": True, "items": result})


@app.route("/sku-map", methods=["GET"])
def get_sku_map():
    return jsonify(load_sku_map())


@app.route("/sku-map/add", methods=["POST"])
def add_sku_mapping():
    data = request.get_json(silent=True) or {}
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
    data = request.get_json(silent=True) or {}
    item_code = data.get("item_code", "").strip()
    m = load_sku_map()
    if item_code in m:
        del m[item_code]
        save_sku_map(m)
    return jsonify({"success": True})


@app.route("/approval-data", methods=["POST"])
def approval_data():
    data = request.get_json(silent=True) or {}
    cogs_groups    = data.get("cogs_groups", [])
    invoice_number = data.get("invoice_number", "")
    invoice_date   = data.get("invoice_date", "")
    ref_entries    = data.get("reference", [])  # parsed SKU Reference entries from UI

    sku_map = load_sku_map()
    try:
        store, client_id, client_secret = get_shopify_creds()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        products = get_all_shopify_products(store, client_id, client_secret)
    except Exception as e:
        return jsonify({"error": f"Shopify API error: {e}"}), 500

    # Load CSV once for reference matching and CSV on-hand fallback
    csv_rows = _load_csv_products() if ref_entries else []
    on_hand_map = load_inventory_on_hand()

    # Collect new sku_map entries learned from reference matches; batch-save at end
    new_sku_map_entries = {}

    # -----------------------------------------------------------------------
    # Phase 1 — Resolve every invoice item to a SKU + variant, preserving order.
    # Unmapped / not-found items go straight to output; mapped items are queued
    # so we can batch-fetch their live on-hand in a single API round-trip.
    # -----------------------------------------------------------------------
    resolved = []   # [{type: "done"|"pending", ...}]

    for group in cogs_groups:
        for item in group.get("items", []):
            item_code = item.get("item_code", "")
            invoice_cost = round(float(item.get("final_cogs", 0)), 2)

            base = {
                "item_code":    item_code,
                "description":  item.get("description", ""),
                "size":         item.get("size", ""),
                "color":        item.get("color", ""),
                "qty":          item.get("qty", 0),
                "new_cogs":     invoice_cost,
                "invoice_cost": invoice_cost,
            }

            # Priority 1 — Reference match
            ref_result  = None
            ref_matched = False
            if ref_entries and csv_rows:
                ref_result = match_by_reference(item, ref_entries, csv_rows)
                if ref_result:
                    ref_matched = True
                    map_key = ref_result["sku_map_key"]
                    new_sku_map_entries[map_key] = {
                        "sku":          ref_result["sku"],
                        "product_name": ref_result["product_name"],
                        "size":         (item.get("size") or "").strip(),
                    }

            # Priority 2 — SKU map lookup
            if ref_result:
                sku                   = ref_result["sku"]
                product_name_override = ref_result["product_name"]
            else:
                mapping               = find_sku_for_item(item, sku_map)
                sku                   = (mapping.get("sku", "") if isinstance(mapping, dict) else str(mapping or ""))
                product_name_override = (mapping.get("product_name", "") if isinstance(mapping, dict) else "")

            # Priority 3 — Unmapped
            if not sku:
                resolved.append({"type": "done", "row": {
                    **base,
                    "sku": "", "prev_cogs": None,
                    "inventory_item_id": None, "changed": False,
                    "product_title": product_name_override or item.get("description", ""),
                    "unmapped": True, "ref_matched": ref_matched,
                }})
                continue

            variant = find_variant_by_sku(products, sku)
            if not variant:
                resolved.append({"type": "done", "row": {
                    **base,
                    "sku": sku, "prev_cogs": None,
                    "inventory_item_id": None, "changed": False,
                    "product_title": product_name_override or item.get("description", ""),
                    "not_found": True, "ref_matched": ref_matched,
                }})
                continue

            resolved.append({"type": "pending",
                              "base": base, "sku": sku, "variant": variant,
                              "iid": variant["inventory_item_id"],
                              "product_name_override": product_name_override,
                              "ref_matched": ref_matched})

    # -----------------------------------------------------------------------
    # Phase 2 — Batch-fetch live on-hand for all pending items in one request.
    # Any SKU missing from the response falls back to the CSV export value.
    # -----------------------------------------------------------------------
    pending = [r for r in resolved if r["type"] == "pending"]
    all_skus = [r["sku"] for r in pending]
    try:
        live_on_hand = get_live_on_hand_batch(store, client_id, client_secret, all_skus)
    except Exception as ex:
        live_on_hand = {}
        print(f"[NSLS] Live on-hand fetch raised unexpectedly, using CSV fallback: {ex}")

    # -----------------------------------------------------------------------
    # Phase 3 — Build final rows (prev_cogs still fetched per-item via API).
    # -----------------------------------------------------------------------
    rows = []
    for r in resolved:
        if r["type"] == "done":
            rows.append(r["row"])
            continue

        iid          = r["iid"]
        sku          = r["sku"]
        base         = r["base"]
        invoice_cost = base["invoice_cost"]

        try:
            prev_cogs = round(get_inventory_item_cost(store, client_id, client_secret, iid), 2)
        except Exception:
            prev_cogs = 0.0

        if sku in live_on_hand:
            on_hand           = live_on_hand[sku]
            on_hand_estimated = False
        else:
            on_hand           = on_hand_map.get(sku, 0)
            on_hand_estimated = True

        weighted_avg = calc_weighted_avg(prev_cogs, on_hand, invoice_cost, base["qty"])
        changed      = abs(weighted_avg - prev_cogs) > 0.001

        rows.append({
            **base,
            "sku":               sku,
            "prev_cogs":         prev_cogs,
            "inventory_item_id": iid,
            "on_hand":           on_hand,
            "on_hand_estimated": on_hand_estimated,
            "weighted_avg":      weighted_avg,
            "changed":           changed,
            "change_amount":     round(weighted_avg - prev_cogs, 2),
            "product_title":     r["product_name_override"] or r["variant"].get("product_title", ""),
            "ref_matched":       r["ref_matched"],
        })

    # Persist any new mappings learned from the reference table
    if new_sku_map_entries:
        sku_map.update(new_sku_map_entries)
        save_sku_map(sku_map)
        print(f"[NSLS] Auto-saved {len(new_sku_map_entries)} new sku_map entries from reference: "
              f"{list(new_sku_map_entries.keys())}")

    return jsonify({"success": True, "rows": rows,
                    "invoice_number": invoice_number,
                    "invoice_date": invoice_date,
                    "ref_learned": len(new_sku_map_entries)})


@app.route("/approve", methods=["POST"])
def approve():
    data = request.get_json(silent=True) or {}
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

    # 1. Update Shopify — push weighted_avg (falls back to new_cogs for legacy rows)
    for row in approved_rows:
        try:
            cost_to_push = float(row.get("weighted_avg") or row.get("new_cogs") or 0)
            update_inventory_item_cost(store, client_id, client_secret, row["inventory_item_id"], cost_to_push)
            results["shopify"].append({"sku": row["sku"], "status": "updated"})
        except Exception as e:
            results["errors"].append(f"Shopify [{row['sku']}]: {e}")

    # 2. Push to Google Sheet
    if sheet_url and approved_rows:
        sheet_rows = []
        for row in approved_rows:
            prev         = round(float(row.get("prev_cogs",    0) or 0), 2)
            invoice_cost = round(float(row.get("invoice_cost") or row.get("new_cogs", 0) or 0), 2)
            weighted_avg = round(float(row.get("weighted_avg") or row.get("new_cogs", 0) or 0), 2)
            change       = round(weighted_avg - prev, 2)
            product_name = row.get("product_title") or row.get("description", "")
            if is_duplicate:
                product_name = f"DUPLICATE - Invoice previously approved | {product_name}"
            sheet_rows.append({
                "Product Name":    product_name,
                "SKU":             row.get("sku", ""),
                "Date Changed":    today,
                "Quote #":         invoice_number,
                "Quote Date":      invoice_date,
                "Quoted Quantity":   str(row.get("qty", 0)),
                "On Hand":           str(int(row.get("on_hand", 0) or 0)),
                "Previous COGS":     f"${prev:.2f}",
                "Invoice Cost":      f"${invoice_cost:.2f}",
                "Weighted Avg COGS": f"${weighted_avg:.2f}",
                "Change $":          f"${change:.2f}",
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
            print(f"[NSLS] Sheet response: HTTP {resp.status_code} — {resp.text[:500]}")
            if not (200 <= resp.status_code < 300):
                results["errors"].append(f"Sheet push failed: HTTP {resp.status_code} — {resp.text[:500]}")
            results["sheet"] = {
                "status": resp.status_code,
                "body": resp.text[:500],
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
        data = request.get_json(silent=True) or {}
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
        _uss_location_cache.clear()
        _sku_iid_cache.clear()
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
        live = get_live_on_hand_batch(store, client_id, client_secret, [sku])
        if sku in live:
            on_hand           = live[sku]
            on_hand_estimated = False
        else:
            on_hand_map       = load_inventory_on_hand()
            on_hand           = on_hand_map.get(sku, 0)
            on_hand_estimated = True
        return jsonify({
            "found": True,
            "inventory_item_id": iid,
            "prev_cogs": prev_cogs,
            "on_hand": on_hand,
            "on_hand_estimated": on_hand_estimated,
            "product_title": variant.get("product_title", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_init_jsonbin()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
