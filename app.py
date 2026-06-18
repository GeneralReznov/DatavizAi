import os
import re
import json
import math
import base64
import traceback
import io
import hashlib
import time
import numpy as np
import pandas as pd
import pdfplumber
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import plotly.graph_objects as go
from scipy.stats import spearmanr
from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for
from werkzeug.utils import secure_filename
from flask_login import current_user, login_required

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "datavizai-secret-key")


# ── Numpy-safe JSON encoder ───────────────────────────────────────────────────
# Flask's jsonify will fail on numpy int64/float64/bool_ scalars and ndarrays.
# Override the default provider so every jsonify() call in the entire app is safe.
try:
    from flask.json.provider import DefaultJSONProvider

    class _NumpyJSONProvider(DefaultJSONProvider):
        def default(self, obj):
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    app.json_provider_class = _NumpyJSONProvider
    app.json = _NumpyJSONProvider(app)
except ImportError:
    # Flask < 2.2 fallback
    from flask.json import JSONEncoder as _BaseEncoder

    class _NumpyEncoder(_BaseEncoder):
        def default(self, obj):
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    app.json_encoder = _NumpyEncoder

# ── Auth integration ─────────────────────────────────────────────────────────
# Imported after `app` is created to avoid any circular-reference issues.
from auth import init_auth  # noqa: E402
init_auth(app)

# Paths that bypass login check
_AUTH_EXEMPT       = frozenset(["/login", "/home", "/favicon.ico"])
_AUTH_EXEMPT_START = ("/auth/", "/static/")


@app.before_request
def _require_login():
    if request.path in _AUTH_EXEMPT:
        return None
    if any(request.path.startswith(p) for p in _AUTH_EXEMPT_START):
        return None
    if not current_user.is_authenticated:
        # Send unauthenticated visitors to the landing page (/home) rather than
        # straight to the login form — /home has Sign In / Get Started buttons.
        return redirect(url_for("home"))


@app.route("/login")
def login_page():
    """Serve the login / signup / OTP page."""
    if current_user.is_authenticated:
        if current_user.role == "admin":
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/me")
@login_required
def me():
    """Return basic info about the currently logged-in user."""
    return jsonify({
        "name":  current_user.name,
        "email": current_user.email,
        "role":  current_user.role,
    })

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_FOLDER = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

ALLOWED_CSV = {"csv"}
ALLOWED_EXCEL = {"xlsx", "xls"}
ALLOWED_PDF = {"pdf"}


def allowed_file(filename, allowed):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 – CSV ingestion
# ─────────────────────────────────────────────────────────────────────────────


def parse_csv(filepath):
    """Quick scan: detect encoding, columns, level values."""
    detected_enc = None
    df = None
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            df = pd.read_csv(filepath, nrows=10, encoding=enc)
            detected_enc = enc
            break
        except Exception:
            continue

    if df is None:
        raise ValueError(
            "Could not decode CSV with any of utf-8, latin-1, cp1252 encodings."
        )

    level_col = None
    levels = []
    for c in df.columns:
        if c.strip().lower() == "level":
            level_col = c
            break

    if level_col:
        full = pd.read_csv(
            filepath, usecols=[level_col], encoding=detected_enc, low_memory=False
        )
        raw = full[level_col].dropna().unique().tolist()
        # CSV numeric columns come back as floats (e.g. 1.0); convert via float→int
        _lvls = []
        for v in raw:
            try:
                _lvls.append(int(float(str(v).strip())))
            except (ValueError, TypeError):
                pass
        levels = sorted(set(_lvls))

    return {
        "columns": list(df.columns),
        "rows": sum(1 for _ in open(filepath, encoding="latin-1")) - 1,
        "levels": levels,
        "level_col": level_col,
        "sample_rows": df.head(3).fillna("").to_dict(orient="records"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 – Excel layout (master metadata source)
# ─────────────────────────────────────────────────────────────────────────────


def _header_score(row_vals):
    """Score a row as a likely header row."""
    text = " ".join(str(v).lower() for v in row_vals)
    score = 0
    for kw in (
        "field name",
        "full name",
        "variable",
        "block",
        "item",
        "level",
        "position",
        "length",
        "data type",
        "srl",
        "col",
    ):
        if kw in text:
            score += 1
    return score


def _norm_block(bstr):
    """Normalize block string to a simple integer string. '3A/3B' -> '3'."""
    if not bstr:
        return ""
    m = re.match(r"(\d+)", bstr.strip())
    return m.group(1) if m else ""


def parse_excel_layout_v2(filepath):
    """
    Parse the NSS Data Layout Excel and return layout_mapping:
      {variable_name: {field_name, block_no, block_name, item_no,
                       level, position, length, data_type, col_no, codebook_key}}
    Also returns raw sheet rows for backward-compat.

    Handles the NSS two-row header where:
      Row N  : srl.no. | Full name | Schedule reference | Field Length | Byte position | Remarks | Field name
      Row N+1:          |           | Block | Item | Col.|
    and level-section context lines like "LEVEL - 02 (Block 3A/3B)".
    """
    xl = pd.ExcelFile(filepath)
    raw_sheets = {}
    all_rows = []

    for sheet in xl.sheet_names:
        try:
            df = xl.parse(sheet, header=None)
            rows = [
                [str(v).strip() if pd.notna(v) else "" for v in row]
                for _, row in df.iterrows()
            ]
            raw_sheets[sheet] = rows
            all_rows.extend(rows)
        except Exception:
            pass

    # ── Find header row: best score + must have 'field name' ─────────────────
    best_idx = None
    best_score = 0
    for i, row in enumerate(all_rows):
        s = _header_score(row)
        row_text = " ".join(row).lower()
        if s > best_score and "field name" in row_text:
            best_score = s
            best_idx = i

    if best_idx is None:
        # Fallback: use first high-score row
        for i, row in enumerate(all_rows):
            s = _header_score(row)
            if s > best_score:
                best_score = s
                best_idx = i

    if best_idx is None:
        return {}, raw_sheets

    # ── Merge sub-header row (row N+1 may hold Block/Item/Col. labels) ───────
    # Sub-header wins whenever it has a non-empty, non-numeric value — this handles
    # NSS Excel's "Schedule reference" spanning header where 'Block', 'Item', 'Col.'
    # appear in the sub-row but the main row has the spanning label 'Schedule reference'.
    main_hdr = all_rows[best_idx]
    sub_hdr = all_rows[best_idx + 1] if best_idx + 1 < len(all_rows) else []
    merged = list(main_hdr)
    for j, sv in enumerate(sub_hdr):
        sv_stripped = sv.strip()
        if j < len(merged) and sv_stripped and sv_stripped.lower() not in ("", "nan"):
            merged[j] = sv_stripped
    h_lower = [h.lower().strip() for h in merged]

    def col_idx(*names):
        for n in names:
            for i, h in enumerate(h_lower):
                if n in h:
                    return i
        return None

    idx_var = col_idx("field name")
    idx_full = col_idx("full name", "description", "name of field")
    idx_block = col_idx("block")
    idx_item = col_idx("item")
    idx_col = col_idx("col.")
    idx_len = col_idx("field length", "length")
    idx_pos = col_idx("byte position", "position", "pos")
    idx_rem = col_idx("remarks")

    # Data starts after both header rows
    data_start = best_idx + 2

    def cell(row, idx, default=""):
        if idx is None or idx >= len(row):
            return default
        v = row[idx].strip()
        return "" if v.lower() in ("nan", "-") else v

    # Pre-scan rows before data_start for initial level context
    layout_mapping = {}
    last_level = ""
    last_block_no = ""

    for pre_row in all_rows[:data_start]:
        rt = " ".join(pre_row).lower()
        lv_m = re.search(r"level\s*[-:–]?\s*0*(\d+)", rt, re.IGNORECASE)
        if lv_m:
            last_level = lv_m.group(1).zfill(2)

    for row in all_rows[data_start:]:
        row_text = " ".join(row).lower()

        # ── Detect level context lines ────────────────────────────────────────
        lv_match = re.search(r"level\s*[-:–]?\s*(\d+)", row_text, re.IGNORECASE)
        if lv_match and not cell(row, idx_var):
            last_level = lv_match.group(1).zfill(2)
            last_block_no = ""  # reset block carry-forward at level boundary
            continue

        # ── Detect section separators / new header rows ───────────────────────
        if _header_score(row) >= 3 and "field name" in row_text:
            # Another header block — skip
            continue

        if not any(v.strip() for v in row):
            continue

        var = cell(row, idx_var)
        full = cell(row, idx_full)
        blk = _norm_block(cell(row, idx_block))
        item = cell(row, idx_item)
        coln = cell(row, idx_col)
        ln = cell(row, idx_len)
        pos = cell(row, idx_pos)
        rem = cell(row, idx_rem)

        # Carry block context forward when current row has no block value
        if blk and blk != "0":
            last_block_no = blk
        elif not blk:
            blk = last_block_no

        # Skip header-like sub-rows (Block/Item/Col. labels repeating)
        if var.lower() in ("nan", "field name", "") or not var:
            continue

        # ── Determine codebook key ────────────────────────────────────────────
        # Prefer col_no (schedule column ref) over item_no for key
        ck_second = coln if coln and coln.lower() not in ("all", "nan", "") else item
        codebook_key = f"{blk}.{ck_second}" if blk and ck_second else ""

        layout_mapping[var] = {
            "field_name": full or var,
            "block_no": blk,
            "block_name": "",  # filled after from context
            "item_no": item,
            "col_no": coln,
            "level": last_level,
            "position": pos,
            "length": ln,
            "data_type": "N" if ln.isdigit() else "",
            "remarks": rem,
            "codebook_key": codebook_key,
            "value_labels": [],
        }

    return layout_mapping, raw_sheets


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 – PDF: blocks, questions, codebook
# ─────────────────────────────────────────────────────────────────────────────


def parse_pdf_full(filepath):
    """
    Returns:
      blocks    = {block_no_str: block_name}
      questions = {block_no_str: [{q_no, q_text}]}
      codebook  = {col_no_str_or_field: [{code, label}]}   keyed by "BN.CN"
    Also returns per-block raw codebook text.
    """
    all_text = ""
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                all_text += t + "\n"

    # ── Block names ──────────────────────────────────────────────────────────
    blocks = {}
    block_name_pat = re.compile(
        r"BLOCK\s+(\d+)\s*[:\-–]\s*([A-Z][^\n]+)", re.IGNORECASE
    )
    for m in block_name_pat.finditer(all_text):
        bno = m.group(1).strip()
        bnam = m.group(2).strip().rstrip(".")
        if bno not in blocks:
            blocks[bno] = bnam

    # ── Questions ────────────────────────────────────────────────────────────
    questions = {}

    # ── Codebook from "CODES FOR BLOCK N" sections ───────────────────────────
    codebook = {}

    # Split text at CODES FOR BLOCK N boundaries
    code_section_pat = re.compile(
        r"CODES\s+FOR\s+BLOCK\s+(\d+)(.*?)(?=CODES\s+FOR\s+BLOCK\s+\d+|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in code_section_pat.finditer(all_text):
        bno = m.group(1).strip()
        bsection = m.group(2)
        _parse_codebook_section(bno, bsection, codebook)

    return blocks, questions, codebook


def _parse_codebook_section(bno, text, codebook):
    """
    Parse one 'CODES FOR BLOCK N' section into codebook dict.
    Handles both 'col. N:' (blocks 1-5) and 'item N:' / 'items N,M:' (blocks 6+).
    """
    lines = text.split("\n")

    # Matches: "col. 3:", "cols. 3,4:", "item 7:", "items 6, 18, 22:"
    entry_pat = re.compile(
        r"^(?:col[s]?|item[s]?)[\.\s]+(\d+(?:\s*[,&]\s*\d+)*(?:\s*&\s*\d+)?)"
        r"(?:\s*[:\-–]\s*(.*)|:?\s*$)",
        re.IGNORECASE,
    )

    current_keys = []
    current_label = ""
    current_vals = []

    def flush():
        for k in current_keys:
            key = f"{bno}.{k}"
            codebook[key] = {
                "label": current_label.strip(),
                "values": list(current_vals),
            }

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        em = entry_pat.match(line)
        if em:
            if current_keys:
                flush()
            nums_raw = em.group(1)
            current_keys = re.findall(r"\d+", nums_raw)
            current_label = (em.group(2) or "").strip()
            current_vals = []
            # Inline values on same header line
            if current_label:
                current_vals.extend(_extract_value_pairs(current_label))
                if current_vals:
                    # Clean label — keep text before first code reference
                    current_label = re.split(r"[:,]\s*\d", current_label)[0].strip()
        elif current_keys:
            vals = _extract_value_pairs(line)
            if vals:
                current_vals.extend(vals)
            else:
                # Continuation of label description
                current_label += " " + line

    if current_keys:
        flush()


def _extract_value_pairs(text):
    """Extract (code, label) pairs from a text fragment."""
    pairs = []
    # Pattern A: label - code   e.g. "Male - 1"
    for m in re.finditer(
        r"([A-Za-z][A-Za-z /\(\)&\.]{0,50}?)\s*[\-–]\s*(\d{1,3})\b", text
    ):
        label = m.group(1).strip()
        code = m.group(2).strip()
        if label and len(label) < 80:
            pairs.append({"code": code, "label": label})
    if pairs:
        return pairs
    # Pattern B: code - label   e.g. "1 - Male"
    for m in re.finditer(
        r"\b(\d{1,3})\s*[\-–:]\s*([A-Za-z][A-Za-z /\(\)&\.]{0,50})", text
    ):
        code = m.group(1).strip()
        label = m.group(2).strip()
        if label and len(label) < 80:
            pairs.append({"code": code, "label": label})
    return pairs


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 helpers – backward-compat wrappers
# ─────────────────────────────────────────────────────────────────────────────


def parse_excel_layout(filepath, levels):
    """Backward-compat wrapper used by existing /upload/excel route."""
    layout_mapping, raw_sheets = parse_excel_layout_v2(filepath)
    level_blocks = {}
    for var, meta in layout_mapping.items():
        lv = meta.get("level", "")
        bn = meta.get("block_no", "")
        if lv and bn:
            try:
                lv_int = int(lv)
                bn_int = int(bn)
                level_blocks.setdefault(str(lv_int), set()).add(bn_int)
            except ValueError:
                pass
    return {
        "sheets": list(raw_sheets.keys()),
        "level_blocks": {k: list(v) for k, v in level_blocks.items()},
        "fields_by_level": {},
        "raw": raw_sheets,
        "layout_mapping": layout_mapping,
    }


def parse_pdf_codes(filepath, block_numbers):
    """Backward-compat wrapper."""
    blocks, questions, codebook = parse_pdf_full(filepath)
    codes_by_block = {}
    for bn in block_numbers:
        bn_str = str(bn)
        columns = {}
        for key, val in codebook.items():
            parts = key.split(".")
            if parts[0] == bn_str and len(parts) == 2:
                columns[parts[1]] = val
        codes_by_block[bn] = {"columns": columns}
    return codes_by_block


def find_column_metadata(excel_raw, csv_columns, levels):
    """Backward-compat: rebuild metadata from raw rows."""
    all_rows = []
    for rows in excel_raw.values():
        all_rows.extend(rows)
    metadata = {}
    best_idx = 0
    best_score = 0
    for i, row in enumerate(all_rows):
        s = _header_score(row)
        if s > best_score:
            best_score = s
            best_idx = i
    headers = all_rows[best_idx]
    h_lower = [h.lower().strip() for h in headers]

    def ci(*names):
        for n in names:
            for i, h in enumerate(h_lower):
                if n in h:
                    return i
        return None

    idx_var = ci("field name", "variable name", "fieldname")
    idx_full = ci("full name", "description")
    idx_blk = ci("block no", "block number", "block")
    idx_item = ci("item", "srl")
    idx_col = ci("col.", "col no")

    def cell(row, idx):
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    for row in all_rows[best_idx + 1 :]:
        if not any(v.strip() for v in row):
            continue
        var = cell(row, idx_var)
        if var and var in csv_columns:
            metadata[var] = {
                "field_name": var,
                "full_name": cell(row, idx_full),
                "block": cell(row, idx_blk),
                "item": cell(row, idx_item),
                "col": cell(row, idx_col),
            }
    return metadata


def build_metadata_repository(csv_info, column_map, pdf_codes, levels, excel_raw):
    """Backward-compat: build the metadata list for the UI table."""
    repository = []
    for col in csv_info.get("columns", []):
        col_meta = column_map.get(col, {})
        block_num = col_meta.get("block", "")
        col_num = col_meta.get("col", "")
        pdf_code_info = {}
        if block_num and str(block_num).strip().isdigit():
            bn = int(str(block_num).strip())
            bc = pdf_codes.get(bn, {})
            if col_num and str(col_num).strip().isdigit():
                pdf_code_info = bc.get("columns", {}).get(col_num.strip(), {})
        repository.append(
            {
                "column_name": col,
                "field_name": col_meta.get("field_name", col),
                "full_name": col_meta.get("full_name", ""),
                "block": block_num,
                "item": col_meta.get("item", ""),
                "col": col_num,
                "level": col_meta.get("level", levels[0] if levels else ""),
                "question_text": pdf_code_info.get("label", ""),
                "value_labels": pdf_code_info.get("values", []),
            }
        )
    return repository


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4-8 – Full pipeline
# ─────────────────────────────────────────────────────────────────────────────


def _unique_resolved_name(base_name, block_name, seen):
    """Ensure resolved column names are unique."""
    if base_name not in seen:
        seen.add(base_name)
        return base_name
    candidate = f"{block_name}_{base_name}" if block_name else base_name
    if candidate not in seen:
        seen.add(candidate)
        return candidate
    i = 2
    while f"{candidate}_{i}" in seen:
        i += 1
    final = f"{candidate}_{i}"
    seen.add(final)
    return final


def _attach_codebook_to_layout(layout_mapping, codebook):
    """
    Match PDF codebook entries to layout_mapping entries.
    Uses the pre-computed codebook_key (block.col or block.item),
    falling back to trying both combinations.
    """
    for var, meta in layout_mapping.items():
        bn = meta.get("block_no", "").strip()
        cn = meta.get("col_no", "").strip()
        item = meta.get("item_no", "").strip()
        ck = meta.get("codebook_key", "").strip()

        # Try keys in priority order
        keys_to_try = []
        if ck:
            keys_to_try.append(ck)
        if bn:
            if cn and cn.lower() not in ("all", ""):
                keys_to_try.append(f"{bn}.{cn}")
            if item and item.lower() not in ("all", ""):
                keys_to_try.append(f"{bn}.{item}")

        for key in keys_to_try:
            if key in codebook:
                vals = codebook[key].get("values", [])
                if vals:
                    meta["value_labels"] = vals
                lbl = codebook[key].get("label", "")
                if lbl and (not meta.get("question_text")):
                    meta["question_text"] = lbl
                break


def _identify_linking_keys(columns_lower):
    """
    Detect HH-level and member-level linking key columns.
    Returns dict: {role: actual_column_name}
    """
    key_patterns = {
        "state": ["state", "state code", "state_code", "state no"],
        "district": ["district", "dist_code", "dist no", "district code"],
        "fsu": ["fsu", "fsu no", "fsu_sl_no", "first stage unit", "village sl"],
        "sub_stratum": ["sub-stratum", "sub_stratum", "substratum", "sub stratum"],
        "hh_no": [
            "hh no",
            "hh_no",
            "hh_sl_no",
            "household no",
            "sample hh",
            "serial no of hhld",
            "household number",
            "hhid",
        ],
        "member": [
            "person sl",
            "person_sl",
            "member sl",
            "member_sl",
            "srl no of member",
            "mem sl",
            "member no",
            "person serial",
        ],
    }
    found = {}
    for role, patterns in key_patterns.items():
        for col_lower, col_orig in columns_lower.items():
            for pat in patterns:
                if pat in col_lower:
                    found[role] = col_orig
                    break
            if role in found:
                break
    return found


def resolve_and_decode_dataset(csv_path, layout_mapping, codebook, progress_cb=None):
    """
    Full pipeline: read CSV, rename columns, decode values, add IDs.
    Returns resolved DataFrame.
    """
    if progress_cb:
        progress_cb("Reading CSV dataset…")

    # Read full CSV
    df = pd.read_csv(csv_path, encoding="latin-1", low_memory=False)

    columns_lower = {c.lower(): c for c in df.columns}

    # ── Build resolved name map ───────────────────────────────────────────────
    seen_names = set()
    col_rename = {}  # original -> resolved
    for col in df.columns:
        meta = (
            layout_mapping.get(col)
            or layout_mapping.get(col.upper())
            or layout_mapping.get(col.lower())
        )
        if meta:
            full = meta.get("field_name", "").strip() or col
            resolved = _unique_resolved_name(
                full, meta.get("block_name", ""), seen_names
            )
        else:
            resolved = _unique_resolved_name(col, "", seen_names)
        col_rename[col] = resolved

    if progress_cb:
        progress_cb("Resolving column names and decoding values…")

    # ── Build per-column codebook (original col -> {code_str: label}) ─────────
    col_codebook = {}
    for orig_col in df.columns:
        meta = (
            layout_mapping.get(orig_col)
            or layout_mapping.get(orig_col.upper())
            or layout_mapping.get(orig_col.lower())
        )
        if meta and meta.get("value_labels"):
            col_codebook[orig_col] = {
                str(v["code"]): v["label"] for v in meta["value_labels"]
            }

    # ── Build resolved DataFrame ──────────────────────────────────────────────
    # For categorical columns: insert <resolved_name>_Code before decoded column
    new_frames = []
    for orig_col in df.columns:
        resolved = col_rename[orig_col]
        if orig_col in col_codebook:
            # Raw code column
            new_frames.append((f"{resolved}_Code", df[orig_col].copy()))

            # Decoded column — CSV values may be floats (1.0 → '1'); try int key first
            def _decode(x, cb=col_codebook[orig_col]):
                if pd.isna(x):
                    return x
                s = str(x).strip()
                # Normalise float representation: 1.0 → '1', -2.0 → '-2'
                if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
                    s = s[:-2]
                # Return decoded label, or fall back to the normalised code string
                return cb.get(s, cb.get(str(x).strip(), s))

            decoded = df[orig_col].map(_decode)
            new_frames.append((resolved, decoded))
        else:
            new_frames.append((resolved, df[orig_col].copy()))

    resolved_df = pd.DataFrame(dict(new_frames))

    # ── Linking keys ──────────────────────────────────────────────────────────
    if progress_cb:
        progress_cb("Identifying linking keys (HH_ID, MEMBER_ID)…")

    resolved_lower = {c.lower(): c for c in resolved_df.columns}
    key_map = _identify_linking_keys(resolved_lower)

    hh_parts_roles = ["state", "district", "fsu", "sub_stratum", "hh_no"]
    hh_cols = [key_map[r] for r in hh_parts_roles if r in key_map]

    if hh_cols:
        hh_id = resolved_df[hh_cols].astype(str).agg("_".join, axis=1)
        resolved_df.insert(0, "HH_ID", hh_id)

        if "member" in key_map:
            mem_col = key_map["member"]
            resolved_df.insert(
                1, "MEMBER_ID", hh_id + "_" + resolved_df[mem_col].astype(str)
            )

    return resolved_df


def build_full_metadata_repo(layout_mapping, codebook, blocks):
    """Build metadata_repository.json structure."""
    repo = {}
    for var, meta in layout_mapping.items():
        bn = meta.get("block_no", "")
        ck = meta.get("codebook_key", "")
        cn = meta.get("col_no", "")
        item = meta.get("item_no", "")
        # Try codebook_key first, then fallbacks
        pdf_codes = (
            codebook.get(ck)
            or codebook.get(f"{bn}.{cn}")
            or codebook.get(f"{bn}.{item}")
            or {}
        )
        vl = meta.get("value_labels") or pdf_codes.get("values", [])
        repo[var] = {
            "field_name": meta.get("field_name", var),
            "block_no": bn,
            "block_name": meta.get("block_name", blocks.get(bn, "")),
            "item_no": item,
            "col_no": cn,
            "level": meta.get("level", ""),
            "position": meta.get("position", ""),
            "length": meta.get("length", ""),
            "data_type": meta.get("data_type", ""),
            "question_text": meta.get("question_text", pdf_codes.get("label", "")),
            "codes": {v["code"]: v["label"] for v in vl},
        }
    return repo


def build_column_mapping(col_rename):
    """Build column_mapping.json: original -> resolved."""
    return [
        {"original_name": orig, "resolved_name": res}
        for orig, res in col_rename.items()
    ]


def build_resolved_codebook(layout_mapping):
    """Build resolved_codebook.json from layout_mapping value_labels."""
    cb = {}
    for var, meta in layout_mapping.items():
        if meta.get("value_labels"):
            cb[meta.get("field_name", var)] = {
                "original_variable": var,
                "block_no": meta.get("block_no", ""),
                "block_name": meta.get("block_name", ""),
                "values": meta["value_labels"],
            }
    return cb


def generate_data_dictionary(layout_mapping, codebook, blocks, output_path):
    """Export survey_data_dictionary.xlsx."""
    rows = []
    for var, meta in layout_mapping.items():
        bn = meta.get("block_no", "")
        cn = meta.get("col_no", "")
        key = f"{bn}.{cn}" if bn and cn else ""
        pdf = codebook.get(key, {})
        # Prefer value_labels already attached via _attach_codebook_to_layout,
        # then fall back to direct codebook lookup by key.
        value_labels = meta.get("value_labels") or pdf.get("values", [])
        allowed = "; ".join(f"{v['code']}={v['label']}" for v in value_labels)
        # Prefer question_text already resolved in layout_mapping (set by
        # _attach_codebook_to_layout), then fall back to PDF codebook label.
        question = meta.get("question_text") or pdf.get("label", "")
        rows.append(
            {
                "Column Name (Resolved)": meta.get("field_name", var),
                "Original Variable": var,
                "Block No": bn,
                "Block Name": meta.get("block_name", blocks.get(bn, "")),
                "Item No": meta.get("item_no", ""),
                "Level": meta.get("level", ""),
                "Position": meta.get("position", ""),
                "Length": meta.get("length", ""),
                "Data Type": meta.get("data_type", ""),
                "Question / Description": question,
                "Allowed Values": allowed,
            }
        )
    df = pd.DataFrame(rows)
    df.to_excel(output_path, index=False)


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────

session_data = {}


# ─────────────────────────────────────────────────────────────────────────────
# Routes – upload
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/home")
def home():
    """Public landing / marketing homepage."""
    return render_template("home.html")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload/csv", methods=["POST"])
def upload_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename or not allowed_file(f.filename, ALLOWED_CSV):
        return jsonify({"error": "Invalid file type. Please upload a CSV file."}), 400
    filename = secure_filename(f.filename)
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    f.save(path)
    try:
        info = parse_csv(path)
        session_data["csv_path"] = path
        session_data["csv_info"] = info
        session_data.pop("repository", None)
        session_data.pop("layout_mapping", None)
        return jsonify({"success": True, "filename": filename, "data": info})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/upload/excel", methods=["POST"])
def upload_excel():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename or not allowed_file(f.filename, ALLOWED_EXCEL):
        return jsonify(
            {"error": "Invalid file type. Please upload an Excel (.xlsx/.xls) file."}
        ), 400
    filename = secure_filename(f.filename)
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    f.save(path)
    try:
        csv_info = session_data.get("csv_info", {})
        levels = csv_info.get("levels", [])
        info = parse_excel_layout(path, levels)
        session_data["excel_path"] = path
        session_data["excel_info"] = info
        session_data["layout_mapping"] = info.get("layout_mapping", {})
        return jsonify(
            {
                "success": True,
                "filename": filename,
                "data": {
                    "sheets": info["sheets"],
                    "fields_found": len(info.get("layout_mapping", {})),
                },
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/upload/pdf", methods=["POST"])
def upload_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename or not allowed_file(f.filename, ALLOWED_PDF):
        return jsonify({"error": "Invalid file type. Please upload a PDF file."}), 400
    filename = secure_filename(f.filename)
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    f.save(path)
    try:
        blocks, questions, codebook = parse_pdf_full(path)
        session_data["pdf_path"] = path
        session_data["pdf_blocks"] = blocks
        session_data["pdf_questions"] = questions
        session_data["pdf_codebook"] = codebook
        # legacy compat
        legacy_pdf_codes = {}
        for bn in blocks:
            try:
                bn_int = int(bn)
            except (ValueError, TypeError):
                continue
            legacy_pdf_codes[bn_int] = {
                "columns": {
                    k.split(".")[1]: codebook[k]
                    for k in codebook
                    if k.startswith(bn + ".")
                }
            }
        session_data["pdf_codes"] = legacy_pdf_codes
        return jsonify(
            {
                "success": True,
                "filename": filename,
                "data": {
                    "blocks_found": sorted(
                        blocks.keys(), key=lambda x: int(x) if x.isdigit() else 0
                    ),
                    "codebook_entries": len(codebook),
                },
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Routes – metadata repository (existing behaviour)
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/process", methods=["POST"])
def process():
    csv_info = session_data.get("csv_info")
    excel_info = session_data.get("excel_info")
    pdf_codes = session_data.get("pdf_codes", {})

    if not csv_info:
        return jsonify({"error": "Please upload the CSV dataset first."}), 400
    if not excel_info:
        return jsonify(
            {"error": "Please upload the Data Layout Excel file first."}
        ), 400

    try:
        levels = csv_info.get("levels", [])
        csv_columns = csv_info.get("columns", [])
        excel_raw = excel_info.get("raw", {})

        column_map = find_column_metadata(excel_raw, csv_columns, levels)

        # Enrich column_map with per-variable level from the full layout_mapping
        # (already parsed during Excel upload) so the preview table shows correct levels.
        full_layout = session_data.get("layout_mapping", {})
        for col, meta in column_map.items():
            lm_entry = (
                full_layout.get(col)
                or full_layout.get(col.upper())
                or full_layout.get(col.lower())
            )
            if lm_entry and lm_entry.get("level"):
                meta["level"] = lm_entry["level"]

        if not pdf_codes:
            all_blocks = set()
            for meta in column_map.values():
                b = meta.get("block", "")
                if str(b).strip().isdigit():
                    all_blocks.add(int(str(b).strip()))
            pdf_codes = {b: {"columns": {}} for b in all_blocks}

        repository = build_metadata_repository(
            csv_info, column_map, pdf_codes, levels, excel_raw
        )

        session_data["repository"] = repository
        session_data["column_map"] = column_map

        stats = {
            "total_columns": len(repository),
            "mapped_columns": sum(1 for r in repository if r["full_name"]),
            "blocks_identified": len(set(r["block"] for r in repository if r["block"])),
            "columns_with_codes": sum(1 for r in repository if r["value_labels"]),
            "levels": levels,
        }
        return jsonify({"success": True, "stats": stats, "repository": repository})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/search", methods=["GET"])
def search():
    q = request.args.get("q", "").lower().strip()
    block = request.args.get("block", "").strip()
    has_codes = request.args.get("has_codes", "").strip()
    results = session_data.get("repository", [])

    if q:
        results = [
            r
            for r in results
            if q in r["column_name"].lower()
            or q in (r["full_name"] or "").lower()
            or q in str(r["block"]).lower()
            or q in (r["question_text"] or "").lower()
        ]
    if block:
        results = [r for r in results if str(r["block"]).strip() == block]
    if has_codes == "1":
        results = [r for r in results if r["value_labels"]]

    return jsonify({"results": results, "total": len(results)})


# ─────────────────────────────────────────────────────────────────────────────
# Routes – full pipeline (generate)
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/generate", methods=["POST"])
def generate():
    csv_path = session_data.get("csv_path")
    excel_path = session_data.get("excel_path")
    pdf_path = session_data.get("pdf_path")

    if not csv_path:
        return jsonify({"error": "Please upload the CSV dataset first."}), 400
    if not excel_path:
        return jsonify(
            {"error": "Please upload the Data Layout Excel file first."}
        ), 400

    try:
        # ── Re-parse with v2 parsers ──────────────────────────────────────────
        layout_mapping, _ = parse_excel_layout_v2(excel_path)

        blocks, questions, codebook = {}, {}, {}
        if pdf_path:
            blocks, questions, codebook = parse_pdf_full(pdf_path)

        # Attach codebook value labels to layout_mapping entries
        _attach_codebook_to_layout(layout_mapping, codebook)

        session_data["layout_mapping"] = layout_mapping
        session_data["pdf_blocks"] = blocks
        session_data["pdf_codebook"] = codebook

        # ── 1. metadata_repository.json ───────────────────────────────────────
        meta_repo = build_full_metadata_repo(layout_mapping, codebook, blocks)
        meta_repo_path = os.path.join(OUTPUT_FOLDER, "metadata_repository.json")
        with open(meta_repo_path, "w", encoding="utf-8") as fh:
            json.dump(meta_repo, fh, indent=2, ensure_ascii=False)

        # ── 2. Resolve dataset (builds col_rename internally) ────────────────
        resolved_df = resolve_and_decode_dataset(csv_path, layout_mapping, codebook)

        # Rebuild col_rename for column_mapping.json
        seen_names2 = set()
        col_rename = {}
        for col in pd.read_csv(csv_path, nrows=1, encoding="latin-1").columns:
            meta = (
                layout_mapping.get(col)
                or layout_mapping.get(col.upper())
                or layout_mapping.get(col.lower())
            )
            if meta:
                full = meta.get("field_name", "").strip() or col
                res = _unique_resolved_name(
                    full, meta.get("block_name", ""), seen_names2
                )
            else:
                res = _unique_resolved_name(col, "", seen_names2)
            col_rename[col] = res

        # ── 3. column_mapping.json ────────────────────────────────────────────
        col_map_path = os.path.join(OUTPUT_FOLDER, "column_mapping.json")
        with open(col_map_path, "w", encoding="utf-8") as fh:
            json.dump(
                build_column_mapping(col_rename), fh, indent=2, ensure_ascii=False
            )

        # ── 4. resolved_codebook.json ─────────────────────────────────────────
        cb_path = os.path.join(OUTPUT_FOLDER, "resolved_codebook.json")
        with open(cb_path, "w", encoding="utf-8") as fh:
            json.dump(
                build_resolved_codebook(layout_mapping),
                fh,
                indent=2,
                ensure_ascii=False,
            )

        # ── 5. survey_data_dictionary.xlsx ────────────────────────────────────
        dict_path = os.path.join(OUTPUT_FOLDER, "survey_data_dictionary.xlsx")
        generate_data_dictionary(layout_mapping, codebook, blocks, dict_path)

        # ── 6. consolidated_resolved_dataset.csv ──────────────────────────────
        csv_out = os.path.join(OUTPUT_FOLDER, "consolidated_resolved_dataset.csv")
        resolved_df.to_csv(csv_out, index=False, encoding="utf-8-sig")

        # Invalidate any stale Step-4 preprocessed file so Step 5 always reads
        # from fresh data after a new Step-3 run.
        for _stale in ("preprocessed_dataset.csv", "processed_selected_dataset.csv"):
            _stale_path = os.path.join(OUTPUT_FOLDER, _stale)
            if os.path.exists(_stale_path):
                try:
                    os.remove(_stale_path)
                except Exception:
                    pass
        session_data.pop("preproc_done", None)
        session_data.pop("tb_processed_path", None)
        session_data.pop("tb_preproc_steps", None)

        # ── 7. consolidated_resolved_dataset.parquet ──────────────────────────
        parquet_out = os.path.join(
            OUTPUT_FOLDER, "consolidated_resolved_dataset.parquet"
        )
        try:
            resolved_df.to_parquet(parquet_out, index=False)
            parquet_ok = True
        except Exception:
            parquet_ok = False

        # ── Summary stats ─────────────────────────────────────────────────────
        csv_info = session_data.get("csv_info", {})
        levels = csv_info.get("levels", [])
        stats = {
            "rows": len(resolved_df),
            "columns_original": len(csv_info.get("columns", [])),
            "columns_resolved": len(resolved_df.columns),
            "variables_mapped": sum(
                1 for v in layout_mapping.values() if v.get("field_name")
            ),
            "variables_coded": sum(
                1 for v in layout_mapping.values() if v.get("value_labels")
            ),
            "blocks": len(blocks),
            "codebook_entries": len(codebook),
            "levels": levels,
            "parquet_available": parquet_ok,
        }

        outputs = {
            "metadata_repository": "metadata_repository.json",
            "column_mapping": "column_mapping.json",
            "resolved_codebook": "resolved_codebook.json",
            "data_dictionary": "survey_data_dictionary.xlsx",
            "consolidated_csv": "consolidated_resolved_dataset.csv",
            "consolidated_parquet": "consolidated_resolved_dataset.parquet"
            if parquet_ok
            else None,
        }

        session_data["generate_stats"] = stats
        session_data["generate_outputs"] = outputs

        return jsonify({"success": True, "stats": stats, "outputs": outputs})

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/download/<filename>")
def download_file(filename):
    safe = secure_filename(filename)
    path = os.path.join(OUTPUT_FOLDER, safe)
    if not os.path.exists(path):
        return jsonify({"error": "File not found. Please generate outputs first."}), 404
    return send_file(path, as_attachment=True, download_name=safe)


# ─────────────────────────────────────────────────────────────────────────────
# Routes – status / reset
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/status", methods=["GET"])
def status():
    csv_info = session_data.get("csv_info", {})
    return jsonify(
        {
            "csv_uploaded": "csv_info" in session_data,
            "excel_uploaded": "excel_info" in session_data,
            "pdf_uploaded": "pdf_codes" in session_data
            or "pdf_codebook" in session_data,
            "processed": "repository" in session_data,
            "generated": "generate_stats" in session_data,
            "csv_filename": os.path.basename(session_data.get("csv_path", ""))
            if "csv_path" in session_data
            else None,
            "excel_filename": os.path.basename(session_data.get("excel_path", ""))
            if "excel_path" in session_data
            else None,
            "pdf_filename": os.path.basename(session_data.get("pdf_path", ""))
            if "pdf_path" in session_data
            else None,
            "levels": csv_info.get("levels", []),
            "repository_size": len(session_data.get("repository", [])),
            "generate_stats": session_data.get("generate_stats"),
            "generate_outputs": session_data.get("generate_outputs"),
        }
    )


@app.route("/reset", methods=["POST"])
def reset():
    session_data.clear()
    # Clear output files (top-level)
    for fn in os.listdir(OUTPUT_FOLDER):
        fp = os.path.join(OUTPUT_FOLDER, fn)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
            except Exception:
                pass
    # Clear generated tables sub-folder
    if os.path.isdir(GENERATED_TABLES_FOLDER):
        for fn in os.listdir(GENERATED_TABLES_FOLDER):
            try:
                os.remove(os.path.join(GENERATED_TABLES_FOLDER, fn))
            except Exception:
                pass
    return jsonify({"success": True})


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Data Preprocessing routes
# ─────────────────────────────────────────────────────────────────────────────

MAX_FLAGGED_ROWS = 500  # cap rows returned per variable to keep payload sane


def _restore_int_dtype(series, orig_dtype):
    """Round and cast a float series back to its original integer dtype.

    KNNImputer, clip(), and fill operations all produce float64 even when the
    source column is int64.  Round first (Int64 handles NaN), then cast to the
    original dtype only when no NaN remain; otherwise keep as nullable Int64.
    """
    if not pd.api.types.is_integer_dtype(orig_dtype):
        return series
    rounded = series.round().astype("Int64")   # nullable – survives any NaN
    if rounded.isna().any():
        return rounded                          # can't use non-nullable int with NaN
    return rounded.astype(orig_dtype)


def _to_python(val):
    """Convert numpy/pandas scalars to JSON-serializable Python natives."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    if isinstance(val, (np.bool_,)):
        return bool(val)
    return val


def _get_numeric_cols(df):
    weight_col = _detect_weight_column(df)
    numeric_cols = [
        c for c in df.columns
        if pd.api.types.is_numeric_dtype(df[c])
        and c != weight_col
        and not c.lower().endswith("_code")
    ]
    continuous_cols = [c for c in numeric_cols if df[c].nunique() > 25]
    return numeric_cols, continuous_cols


def _detect_outlier_mask(df_col, method):
    """Return (outlier_mask, lo, hi) for a series using the given method."""
    col_data = df_col.dropna()
    if len(col_data) < 10:
        return pd.Series(False, index=df_col.index), None, None
    lo = hi = None
    mask = pd.Series(False, index=df_col.index)
    if method == "iqr":
        Q1, Q3 = col_data.quantile(0.25), col_data.quantile(0.75)
        IQR = Q3 - Q1
        lo, hi = Q1 - 1.5 * IQR, Q3 + 1.5 * IQR
        mask = (df_col < lo) | (df_col > hi)
    elif method == "z_score":
        mean_v, std_v = col_data.mean(), col_data.std()
        if std_v > 0:
            z = (df_col - mean_v) / std_v
            mask = z.abs() > 3
            lo, hi = mean_v - 3 * std_v, mean_v + 3 * std_v
    elif method == "modified_z_score":
        median_v = col_data.median()
        mad = float(np.median(np.abs(col_data - median_v)))
        if mad > 0:
            mzs = 0.6745 * (df_col - median_v) / mad
            mask = mzs.abs() > 3.5
            lo, hi = median_v - 3.5 * mad / 0.6745, median_v + 3.5 * mad / 0.6745
    elif method == "percentile":
        lo, hi = col_data.quantile(0.01), col_data.quantile(0.99)
        mask = (df_col < lo) | (df_col > hi)
    return mask, lo, hi


@app.route("/preprocess/detect", methods=["POST"])
def preprocess_detect():
    """Detect missing values and outliers, return flagged rows (no modifications)."""
    body = request.get_json() or {}
    outlier_method = body.get("outlier_method", "iqr")

    csv_path = os.path.join(OUTPUT_FOLDER, "consolidated_resolved_dataset.csv")
    if not os.path.exists(csv_path):
        return jsonify(
            {"error": "Consolidated dataset not found. Run Step 3 (Generate) first."}
        ), 400

    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
        numeric_cols, continuous_cols = _get_numeric_cols(df)

        all_cols = list(df.columns)
        missing_vars = []
        outlier_vars = []

        # Cache for pagination — stores full index lists per variable
        detect_cache = {
            "all_columns": all_cols,
            "csv_path": csv_path,
            "missing": {},
            "outlier": {},
        }

        # ── Missing value detection ───────────────────────────────────────────
        for col in numeric_cols:
            miss_idx = df.index[df[col].isna()].tolist()
            if not miss_idx:
                continue
            detect_cache["missing"][col] = miss_idx
            sample_idx = miss_idx[:MAX_FLAGGED_ROWS]
            rows = []
            for i in sample_idx:
                row_data = {c: _to_python(df.at[i, c]) for c in all_cols}
                row_data["__row_index__"] = int(i)
                rows.append(row_data)
            missing_vars.append({
                "column": col,
                "count": len(miss_idx),
                "total_rows": len(df),
                "rows": rows,
                "all_columns": all_cols,
            })

        # ── Outlier detection ─────────────────────────────────────────────────
        if outlier_method != "none":
            for col in continuous_cols:
                mask, lo, hi = _detect_outlier_mask(df[col], outlier_method)
                out_idx = df.index[mask].tolist()
                if not out_idx:
                    continue
                detect_cache["outlier"][col] = out_idx
                sample_idx = out_idx[:MAX_FLAGGED_ROWS]
                rows = []
                for i in sample_idx:
                    row_data = {c: _to_python(df.at[i, c]) for c in all_cols}
                    row_data["__row_index__"] = int(i)
                    rows.append(row_data)
                outlier_vars.append({
                    "column": col,
                    "count": len(out_idx),
                    "total_rows": len(df),
                    "lo": round(float(lo), 4) if lo is not None else None,
                    "hi": round(float(hi), 4) if hi is not None else None,
                    "rows": rows,
                    "all_columns": all_cols,
                })

        session_data["detect_cache"] = detect_cache

        return jsonify({
            "success": True,
            "missing_vars": missing_vars,
            "outlier_vars": outlier_vars,
            "total_rows": len(df),
            "all_columns": all_cols,
        })

    except Exception as e:
        app.logger.error("Detection failed: %s", e)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/preprocess/detect/rows", methods=["POST"])
@login_required
def preprocess_detect_rows():
    """Return a paginated page of flagged rows for a specific variable (500 rows/page)."""
    body = request.get_json() or {}
    kind   = body.get("kind")    # "missing" or "outlier"
    col    = body.get("col")
    page   = max(1, int(body.get("page", 1)))

    cache = session_data.get("detect_cache")
    if not cache:
        return jsonify({"error": "No detection results found. Run Detect first."}), 400

    idx_list = cache.get(kind, {}).get(col)
    if idx_list is None:
        return jsonify({"error": f"No cached data for {kind}/{col}"}), 400

    all_cols = cache.get("all_columns", [])
    csv_path = cache.get("csv_path", "")

    if not os.path.exists(csv_path):
        return jsonify({"error": "Source CSV no longer available."}), 400

    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)

        start = (page - 1) * MAX_FLAGGED_ROWS
        end   = start + MAX_FLAGGED_ROWS
        page_idx = idx_list[start:end]

        rows = []
        for i in page_idx:
            row_data = {c: _to_python(df.at[i, c]) for c in all_cols}
            row_data["__row_index__"] = int(i)
            rows.append(row_data)

        total_pages = max(1, (len(idx_list) + MAX_FLAGGED_ROWS - 1) // MAX_FLAGGED_ROWS)
        return jsonify({
            "success": True,
            "rows": rows,
            "all_columns": all_cols,
            "page": page,
            "total_pages": total_pages,
            "total_count": len(idx_list),
        })
    except Exception as e:
        app.logger.error("detect/rows failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/preprocess/run", methods=["POST"])
def preprocess_run():
    body = request.get_json() or {}
    missing_method = body.get("missing_method", "median")  # mean|median|mode|knn|none
    outlier_method = body.get("outlier_method", "iqr")     # iqr|z_score|modified_z_score|percentile|none
    outlier_action = body.get("outlier_action", "cap")     # cap|remove|flag
    # skipped_rows: list of __row_index__ values to exclude from ALL preprocessing
    skipped_rows = set(body.get("skipped_rows", []))
    # edited_cells: { str(row_index): { col: new_value } }
    edited_cells = body.get("edited_cells", {})

    csv_path = os.path.join(OUTPUT_FOLDER, "consolidated_resolved_dataset.csv")
    if not os.path.exists(csv_path):
        return jsonify(
            {"error": "Consolidated dataset not found. Run Step 3 (Generate) first."}
        ), 400

    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
        numeric_cols, continuous_cols = _get_numeric_cols(df)

        report = {
            "rows_before": len(df),
            "rows_after": len(df),
            "cols_numeric": len(numeric_cols),
            "cols_continuous": len(continuous_cols),
            "missing_imputed": 0,
            "outliers_detected": 0,
            "outliers_handled": 0,
            "rows_removed": 0,
            "rows_skipped": len(skipped_rows),
            "cells_edited": 0,
            "columns_with_missing": [],
            "columns_with_outliers": [],
        }

        df_out = df.copy()

        # ── Apply user edits first (before imputation) ────────────────────────
        edited_count = 0
        for row_idx_str, col_vals in edited_cells.items():
            try:
                row_idx = int(row_idx_str)
            except ValueError:
                continue
            if row_idx not in df_out.index:
                continue
            for col, val in col_vals.items():
                if col in df_out.columns:
                    try:
                        df_out.at[row_idx, col] = float(val) if val not in (None, "", "null") else np.nan
                    except (ValueError, TypeError):
                        df_out.at[row_idx, col] = val
                    edited_count += 1
        report["cells_edited"] = edited_count

        # Build mask of rows eligible for preprocessing (not skipped)
        eligible_mask = ~df_out.index.isin(skipped_rows)

        # ── Missing value imputation ──────────────────────────────────────────
        # Record original dtypes so we can restore integer columns after float-producing imputation
        orig_dtypes = {c: df_out[c].dtype for c in numeric_cols}

        if missing_method != "none":
            if missing_method == "knn":
                from sklearn.impute import KNNImputer
                # Apply KNN only on eligible rows, only on cols that actually have missing
                cols_with_missing = [c for c in numeric_cols if df_out.loc[eligible_mask, c].isna().any()]
                if cols_with_missing:
                    eligible_idx = df_out.index[eligible_mask]
                    imputer = KNNImputer(n_neighbors=5)
                    sub = df_out.loc[eligible_idx, cols_with_missing].astype(float).copy()
                    # For large datasets KNNImputer.fit_transform is O(n²) and extremely slow.
                    # Fit on a random subsample (≤10 000 rows) then transform the full eligible set.
                    KNN_FIT_LIMIT = 10_000
                    if len(eligible_idx) > KNN_FIT_LIMIT:
                        rng = np.random.default_rng(42)
                        sample_pos = rng.choice(len(eligible_idx), size=KNN_FIT_LIMIT, replace=False)
                        fit_idx = eligible_idx[sample_pos]
                        imputer.fit(sub.loc[fit_idx])
                        imputed = imputer.transform(sub)
                    else:
                        imputed = imputer.fit_transform(sub)
                    df_imputed = pd.DataFrame(imputed, index=eligible_idx, columns=cols_with_missing)
                    for col in cols_with_missing:
                        n_miss = int(df_out.loc[eligible_idx, col].isna().sum())
                        if n_miss == 0:
                            continue
                        filled = _restore_int_dtype(df_imputed[col], orig_dtypes[col])
                        df_out.loc[eligible_idx, col] = filled
                        report["missing_imputed"] += n_miss
                        report["columns_with_missing"].append({
                            "column": col,
                            "imputed": n_miss,
                            "fill_value": "KNN(k=5)",
                            "method": "knn",
                        })
            else:
                for col in numeric_cols:
                    eligible_series = df_out.loc[eligible_mask, col]
                    n_miss = int(eligible_series.isna().sum())
                    if n_miss == 0:
                        continue
                    if missing_method == "mean":
                        fill = eligible_series.mean()
                    elif missing_method == "mode":
                        m = eligible_series.mode()
                        fill = m.iloc[0] if len(m) else eligible_series.median()
                    else:
                        fill = eligible_series.median()
                    filled = df_out.loc[eligible_mask, col].fillna(fill)
                    df_out.loc[eligible_mask, col] = _restore_int_dtype(filled, orig_dtypes[col])
                    report["missing_imputed"] += n_miss
                    report["columns_with_missing"].append({
                        "column": col,
                        "imputed": n_miss,
                        "fill_value": round(float(fill), 4),
                        "method": missing_method,
                    })
            app.logger.info(
                "Preprocessing: imputed %d missing values across %d columns (%s)",
                report["missing_imputed"],
                len(report["columns_with_missing"]),
                missing_method,
            )

        # ── Outlier detection & handling ──────────────────────────────────────
        rows_to_remove = set()

        if outlier_method != "none":
            for col in continuous_cols:
                # Detect on full column (eligible rows only for mask)
                mask_full, lo, hi = _detect_outlier_mask(df_out[col], outlier_method)
                # Only flag eligible rows
                outlier_mask = mask_full & eligible_mask
                n_out = int(outlier_mask.sum())
                if n_out == 0:
                    continue

                report["outliers_detected"] += n_out
                report["columns_with_outliers"].append({
                    "column": col,
                    "count": n_out,
                    "pct": round(n_out / len(df_out) * 100, 2),
                    "lo": round(float(lo), 4) if lo is not None else None,
                    "hi": round(float(hi), 4) if hi is not None else None,
                })

                if outlier_action == "cap" and lo is not None and hi is not None:
                    clipped = df_out.loc[outlier_mask, col].clip(lower=lo, upper=hi)
                    df_out.loc[outlier_mask, col] = _restore_int_dtype(clipped, orig_dtypes[col])
                    report["outliers_handled"] += n_out
                elif outlier_action == "remove":
                    rows_to_remove.update(df_out.index[outlier_mask].tolist())

            if outlier_action == "remove" and rows_to_remove:
                df_out = df_out.drop(index=list(rows_to_remove)).reset_index(drop=True)
                report["outliers_handled"] = len(rows_to_remove)

            app.logger.info(
                "Preprocessing: detected %d outliers via %s, action=%s",
                report["outliers_detected"],
                outlier_method,
                outlier_action,
            )

        report["rows_after"] = len(df_out)
        report["rows_removed"] = report["rows_before"] - report["rows_after"]

        # ── Save preprocessed dataset ─────────────────────────────────────────
        preproc_path = os.path.join(OUTPUT_FOLDER, "preprocessed_dataset.csv")
        df_out.to_csv(preproc_path, index=False, encoding="utf-8-sig")

        session_data["tb_processed_path"] = preproc_path
        session_data["tb_preproc_steps"] = [
            f"missing_imputation_{missing_method}"
            if missing_method != "none"
            else "missing_imputation_skipped",
            f"outlier_{outlier_method}_{outlier_action}"
            if outlier_method != "none"
            else "outlier_detection_skipped",
        ]
        session_data["preproc_done"] = True

        app.logger.info(
            "Preprocessing complete: %d → %d rows, saved to %s",
            report["rows_before"],
            report["rows_after"],
            preproc_path,
        )
        return jsonify({"success": True, "report": report})

    except Exception as e:
        app.logger.error("Preprocessing failed: %s", e)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/download/preprocessed")
@login_required
def download_preprocessed():
    """Download the Step-4 preprocessed dataset CSV."""
    preproc_path = os.path.join(OUTPUT_FOLDER, "preprocessed_dataset.csv")
    if not os.path.exists(preproc_path):
        return jsonify({"error": "Preprocessed dataset not found. Run preprocessing first."}), 404
    return send_file(
        preproc_path,
        as_attachment=True,
        download_name="preprocessed_dataset.csv",
        mimetype="text/csv",
    )


# ─────────────────────────────────────────────────────────────────────────────
# SURVEY TABLE BUILDER v2.0 — Metadata-Driven MOSPI Engine
# ─────────────────────────────────────────────────────────────────────────────

GENERATED_TABLES_FOLDER = os.path.join(OUTPUT_FOLDER, "generated_tables")
os.makedirs(GENERATED_TABLES_FOLDER, exist_ok=True)

FORMULA_MAP = {
    "Estimated Total": "Σ(W × X)",
    "Weighted Percentage": "Σ(W × I[X=k]) / Σ(W) × 100",
    "Weighted Mean": "Σ(W × X) / Σ(W)",
    "Weighted Ratio": "Σ(W × Num) / Σ(W × Den)",
    "Weighted Frequency": "Σ(W)",
    "Frequency Count": "Count of observations",
    "MOE_95": "1.96 × sqrt( Σ(w_i² × (x_i − μ̂)²) / (Σw_i)² )",
}

# Estimation types that operate on a real numerical variable (not just weights/counts)
NUMERICAL_ETYPE = frozenset(["Estimated Total", "Weighted Mean", "Weighted Ratio"])


def _detect_weight_column(df):
    """Detect the survey design weight column.
    Priority: exact column named 'mult' first, then pattern search."""
    for col in df.columns:
        if col.strip().lower() == "mult":
            try:
                test = pd.to_numeric(df[col], errors="coerce")
                if test.notna().sum() > 0:
                    return col
            except Exception:
                pass
    weight_patterns = ["mult", "multiplier", "design weight", "wt", "weight", "factor"]
    for pat in weight_patterns:
        for col in df.columns:
            if pat in col.lower().strip():
                try:
                    test = pd.to_numeric(df[col], errors="coerce")
                    if test.notna().sum() > 0:
                        return col
                except Exception:
                    pass
    return None


def _auto_classify_role(col, category, is_weight):
    """Auto-classify a variable role: weight / dimension / measure."""
    if is_weight:
        return "weight"
    if category in ("Categorical (Numeric)", "Categorical (Text)"):
        return "dimension"
    return "measure"


def _get_column_metadata(df):
    """Generate metadata for all columns, including auto-role classification."""
    weight_col = _detect_weight_column(df)
    meta = []
    for col in df.columns:
        dtype = str(df[col].dtype)
        n_unique = int(df[col].nunique())
        n_miss = int(df[col].isna().sum())
        pct_miss = round(n_miss / len(df) * 100, 1) if len(df) > 0 else 0

        if pd.api.types.is_numeric_dtype(df[col]):
            category = "Categorical (Numeric)" if n_unique <= 25 else "Continuous"
        else:
            category = "Categorical (Text)"

        sample_vals = [str(v) for v in df[col].dropna().unique()[:8].tolist()]
        is_weight = col == weight_col
        auto_role = _auto_classify_role(col, category, is_weight)

        meta.append(
            {
                "column": col,
                "dtype": dtype,
                "n_unique": n_unique,
                "n_missing": n_miss,
                "pct_missing": pct_miss,
                "category": category,
                "sample_values": sample_vals,
                "is_weight": is_weight,
                "auto_role": auto_role,
            }
        )
    return meta, weight_col


def _preprocess_selected(df, selected_cols, weight_col=None):
    """Cleaning, missing imputation, outlier capping on selected columns."""
    df_work = df.copy()
    for col in selected_cols:
        if col not in df_work.columns:
            continue
        if df_work[col].dtype == object:
            df_work[col] = df_work[col].astype(str).str.strip()
            df_work[col] = df_work[col].replace(
                {"": pd.NA, "nan": pd.NA, "None": pd.NA}
            )
        if df_work[col].isna().sum() > 0:
            if pd.api.types.is_numeric_dtype(df_work[col]):
                n_unique = df_work[col].nunique()
                if n_unique <= 25:
                    mode_s = df_work[col].mode()
                    fill = mode_s.iloc[0] if len(mode_s) else 0
                else:
                    fill = df_work[col].median()
                df_work[col] = df_work[col].fillna(fill)
            else:
                mode_s = df_work[col].mode()
                fill = mode_s.iloc[0] if len(mode_s) else "Unknown"
                df_work[col] = df_work[col].fillna(fill)
        if pd.api.types.is_numeric_dtype(df_work[col]) and col != weight_col:
            n_unique = df_work[col].nunique()
            if n_unique > 25:
                Q1 = df_work[col].quantile(0.25)
                Q3 = df_work[col].quantile(0.75)
                IQR = Q3 - Q1
                df_work[col] = df_work[col].clip(
                    lower=Q1 - 1.5 * IQR, upper=Q3 + 1.5 * IQR
                )
    if weight_col and weight_col in df_work.columns:
        df_work[weight_col] = pd.to_numeric(
            df_work[weight_col], errors="coerce"
        ).fillna(1)
        df_work[weight_col] = df_work[weight_col].clip(lower=0)
    return df_work


def _compute_weight_diagnostics(df, weight_col):
    """Distribution checks, extreme-weight detection, normalization validation."""
    if weight_col not in df.columns:
        return {"error": f"Column '{weight_col}' not found."}
    w = pd.to_numeric(df[weight_col], errors="coerce")
    n_miss = int(w.isna().sum())
    w_valid = w.dropna()
    n_neg = int((w_valid < 0).sum())
    n_zero = int((w_valid == 0).sum())
    w_pos = w_valid[w_valid > 0]
    if len(w_pos) == 0:
        return {"error": "No positive weight values found."}
    q1 = float(w_pos.quantile(0.25))
    q3 = float(w_pos.quantile(0.75))
    iqr = q3 - q1
    ext_thr = q3 + 3.0 * iqr
    n_ext = int((w_pos > ext_thr).sum())
    mean_val = float(w_pos.mean())
    cv_pct = round(float(w_pos.std() / mean_val * 100), 2) if mean_val else 0.0
    wsum = float(w_pos.sum())

    alerts = []
    if n_neg > 0:
        alerts.append(
            {
                "level": "error",
                "msg": f"{n_neg} negative weight(s) detected — must be corrected before estimation.",
            }
        )
    if n_zero > 0:
        alerts.append(
            {
                "level": "warn",
                "msg": f"{n_zero} zero weight(s) — those observations contribute nothing to estimates.",
            }
        )
    if n_ext > 0:
        pct = round(n_ext / len(w_pos) * 100, 1)
        alerts.append(
            {
                "level": "warn",
                "msg": f"{n_ext} extreme weight(s) ({pct}%) above 3×IQR fence — consider Winsorization.",
            }
        )
    if cv_pct > 200:
        alerts.append(
            {
                "level": "warn",
                "msg": f"High weight CV ({cv_pct}%) — extreme heterogeneity may inflate estimate variance.",
            }
        )
    if abs(mean_val - 1.0) <= 0.05:
        alerts.append(
            {"level": "info", "msg": "Weights appear normalized (mean ≈ 1.0)."}
        )
    elif abs(wsum - len(df)) <= len(df) * 0.05:
        alerts.append(
            {
                "level": "info",
                "msg": "Weights sum ≈ sample size — population-relative scaling.",
            }
        )
    else:
        alerts.append(
            {
                "level": "info",
                "msg": f"Weights sum to {wsum:,.0f} — design weights representing population totals.",
            }
        )
    if not any(a["level"] in ("error", "warn") for a in alerts):
        alerts.append(
            {"level": "ok", "msg": "No anomalies detected in weight distribution."}
        )

    return {
        "n_valid": len(w_pos),
        "n_missing": n_miss,
        "n_zero": n_zero,
        "n_negative": n_neg,
        "sum": round(wsum, 2),
        "mean": round(mean_val, 4),
        "median": round(float(w_pos.median()), 4),
        "min": round(float(w_pos.min()), 4),
        "max": round(float(w_pos.max()), 4),
        "std": round(float(w_pos.std()), 4),
        "cv_pct": cv_pct,
        "extreme_count": n_ext,
        "extreme_pct": round(n_ext / len(w_pos) * 100, 2),
        "extreme_threshold": round(ext_thr, 4),
        "normalization": {
            "mean_near_1": abs(mean_val - 1.0) <= 0.05,
            "sum_near_n": abs(wsum - len(df)) <= len(df) * 0.05,
        },
        "percentiles": {
            "p1": round(float(w_pos.quantile(0.01)), 4),
            "p5": round(float(w_pos.quantile(0.05)), 4),
            "p25": round(q1, 4),
            "p50": round(float(w_pos.median()), 4),
            "p75": round(q3, 4),
            "p95": round(float(w_pos.quantile(0.95)), 4),
            "p99": round(float(w_pos.quantile(0.99)), 4),
        },
        "alerts": alerts,
    }


def _apply_table_filters(df, filters):
    """Apply structured filter conditions from a table definition."""
    if not filters:
        return df
    mask = pd.Series([True] * len(df), index=df.index)
    for f in filters:
        var = f.get("variable", "")
        op = f.get("operator", "==")
        val = f.get("value", "")
        if var not in df.columns:
            continue
        try:
            num_s = pd.to_numeric(pd.Series([val]), errors="coerce").iloc[0]
            if pd.notna(num_s):
                col_n = pd.to_numeric(df[var], errors="coerce")
                if op == "==":
                    mask &= col_n == num_s
                elif op == "!=":
                    mask &= col_n != num_s
                elif op == ">":
                    mask &= col_n > num_s
                elif op == ">=":
                    mask &= col_n >= num_s
                elif op == "<":
                    mask &= col_n < num_s
                elif op == "<=":
                    mask &= col_n <= num_s
            else:
                sv = str(val)
                if op == "==":
                    mask &= df[var].astype(str) == sv
                elif op == "!=":
                    mask &= df[var].astype(str) != sv
        except Exception:
            pass
    return df[mask].reset_index(drop=True)


def _calc_measure(sub, sw, mc):
    """Compute one weighted measure for a (sub-)group."""
    var = mc["variable"]
    etype = mc.get("estimation_type", "Estimated Total")
    ind_cat = mc.get("indicator_category")
    ratio_den = mc.get("ratio_denominator")

    if etype == "Estimated Total":
        vals = pd.to_numeric(sub[var], errors="coerce").fillna(0)
        return float((sw * vals).sum())

    elif etype == "Weighted Percentage":
        if ind_cat is not None and str(ind_cat).strip() != "":
            xi = (sub[var].astype(str) == str(ind_cat)).astype(float)
        else:
            xi = pd.to_numeric(sub[var], errors="coerce").fillna(0)
        denom = float(sw.sum())
        return float((sw * xi).sum()) / denom * 100 if denom else 0.0

    elif etype == "Weighted Mean":
        xi = pd.to_numeric(sub[var], errors="coerce").fillna(0)
        denom = float(sw.sum())
        return float((sw * xi).sum()) / denom if denom else 0.0

    elif etype == "Weighted Ratio":
        if not ratio_den or ratio_den not in sub.columns:
            return 0.0
        nv = pd.to_numeric(sub[var], errors="coerce").fillna(0)
        dv = pd.to_numeric(sub[ratio_den], errors="coerce").fillna(0)
        den = float((sw * dv).sum())
        return float((sw * nv).sum()) / den if den else 0.0

    elif etype == "Weighted Frequency":
        return float(sw.sum())

    elif etype == "Frequency Count":
        return float(len(sub))

    return 0.0


def _compute_stat_score(df, all_vars):
    """Average absolute pairwise Spearman correlation across all variable pairs."""
    sub = df[all_vars].copy()
    for col in all_vars:
        if not pd.api.types.is_numeric_dtype(sub[col]):
            sub[col] = pd.Categorical(sub[col]).codes.astype(float)
        else:
            sub[col] = pd.to_numeric(sub[col], errors="coerce")
    sub = sub.dropna()
    if len(sub) < 10:
        return 0.5
    if len(all_vars) == 2:
        try:
            corr, _ = spearmanr(sub[all_vars[0]], sub[all_vars[1]])
            return float(abs(corr)) if not np.isnan(corr) else 0.5
        except Exception:
            return 0.5
    try:
        mat = spearmanr(sub).statistic
        if np.ndim(mat) == 0:
            return float(abs(mat))
        scores = [
            abs(mat[i][j])
            for i in range(len(all_vars))
            for j in range(i + 1, len(all_vars))
            if not np.isnan(mat[i][j])
        ]
        return float(np.mean(scores)) if scores else 0.5
    except Exception:
        return 0.5


def _semantic_check_gemini(var_meta, table_title, is_frequency_table=False):
    """Call Gemini to assess if the variables make analytical sense together.
    Returns (verdict, explanation) where verdict is 'strong'|'moderate'|'weak'."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        app.logger.info("Gemini semantic check skipped: GEMINI_API_KEY not set")
        return None, None
    try:
        from google import genai as google_genai

        client = google_genai.Client(api_key=api_key)
        app.logger.info(
            "Gemini semantic check: calling gemini-2.5-flash for table '%s'",
            table_title,
        )

        var_lines = "\n".join(
            f"- '{v['name']}' ({v['dtype']}, {v['role']})" for v in var_meta
        )

        if is_frequency_table:
            prompt = (
                "You are a statistical survey analysis expert for government reports.\n\n"
                "This is a categorical distribution table showing the estimated value "
                "(sum of survey weights) for each category of one variable:\n"
                f"{var_lines}\n\n"
                f'Table title: "{table_title}"\n\n'
                "Assess whether presenting the estimated (weighted) count of this variable "
                "makes analytical sense for a national survey report.\n\n"
                "Respond with EXACTLY two lines:\n"
                "Line 1: One word only — 'strong', 'moderate', or 'weak'\n"
                "Line 2: One plain-English sentence explaining whether this estimated "
                "value distribution is useful for reporting and why."
            )
        else:
            prompt = (
                "You are a statistical survey analysis expert for government reports.\n\n"
                "These variables are being cross-tabulated together in a survey table:\n"
                f"{var_lines}\n\n"
                f'Table title: "{table_title}"\n\n'
                "Assess whether these variables are semantically related and make "
                "analytical sense to cross-tabulate for a national survey report. "
                "Focus on whether the dimension(s) and measure(s) have a meaningful "
                "real-world relationship.\n\n"
                "Respond with EXACTLY two lines:\n"
                "Line 1: One word only — 'strong', 'moderate', or 'weak'\n"
                "Line 2: One plain-English sentence explaining the relationship between "
                "the specific variables OR what is analytically inconsistent. "
                "Name the variables explicitly."
            )

        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        lines = resp.text.strip().split("\n", 1)
        verdict = lines[0].strip().lower()
        if verdict not in ("strong", "moderate", "weak"):
            verdict = "moderate"
        explanation = lines[1].strip() if len(lines) > 1 else ""
        app.logger.info(
            "Gemini semantic check: verdict='%s' for table '%s'", verdict, table_title
        )
        return verdict, explanation
    except Exception as exc:
        app.logger.warning("Gemini semantic check failed: %s", exc)
        return None, None


def _check_table_relations(df, table_def, weight_col):
    """Compute statistical + semantic relation quality for a table's variables.
    Returns dict with verdict, stat_score, semantic_verdict, explanation."""
    dim_defs = table_def.get("dimensions", [])
    meas_defs = table_def.get("measures", [])

    FREQ_TYPES = ("Frequency Count", "Weighted Frequency")

    dim_vars = [d["variable"] for d in dim_defs]

    # Classify as frequency table only when ALL measures are Frequency/Weighted types —
    # never based on whether the variable happens to equal the weight column.
    real_meas_defs = [
        m for m in meas_defs if m.get("estimation_type") not in FREQ_TYPES
    ]
    is_frequency_table = len(real_meas_defs) == 0

    # Stat correlation: exclude the weight column (can't correlate with itself) and _code cols
    stat_vars = list(
        dict.fromkeys(
            v
            for v in (dim_vars + [m["variable"] for m in real_meas_defs])
            if v in df.columns and not v.lower().endswith("_code") and v != weight_col
        )
    )

    stat_score = _compute_stat_score(df, stat_vars) if len(stat_vars) >= 2 else None

    def _col_dtype(col_name):
        return "categorical" if df[col_name].nunique() <= 25 else "continuous"

    # Build var_meta for Gemini — always include all dimensions; for cross-tab tables
    # include all real measure variables regardless of whether they equal the weight col.
    var_meta = []
    seen = set()
    for d in dim_defs:
        v = d["variable"]
        if v in df.columns and v not in seen:
            seen.add(v)
            var_meta.append(
                {
                    "name": d.get("label") or v,
                    "dtype": _col_dtype(v),
                    "role": "dimension (row grouping variable)",
                }
            )
    if not is_frequency_table:
        for m in real_meas_defs:
            v = m["variable"]
            if v in df.columns and v not in seen:
                seen.add(v)
                var_meta.append(
                    {
                        "name": m.get("label") or v,
                        "dtype": _col_dtype(v),
                        "role": f"measure ({m.get('estimation_type', 'Estimated Total')})",
                    }
                )

    sem_verdict, sem_explanation = _semantic_check_gemini(
        var_meta, table_def.get("title", ""), is_frequency_table=is_frequency_table
    )

    sem_num = {"strong": 1.0, "moderate": 0.5, "weak": 0.0}.get(sem_verdict)
    if stat_score is not None and sem_num is not None:
        combined = 0.40 * stat_score + 0.60 * sem_num
    elif stat_score is not None:
        combined = stat_score
    elif sem_num is not None:
        combined = sem_num
    else:
        return None

    if combined >= 0.60:
        verdict = "strong"
    elif combined >= 0.35:
        verdict = "moderate"
    else:
        verdict = "weak"

    explanation = (
        sem_explanation
        if sem_explanation
        else (
            f"Statistical correlation score: {stat_score:.2f}."
            if stat_score is not None
            else None
        )
    )

    return {
        "verdict": verdict,
        "stat_score": round(stat_score, 3) if stat_score is not None else None,
        "semantic_verdict": sem_verdict,
        "explanation": explanation,
        "is_frequency_table": is_frequency_table,
    }


def _calc_weighted_mean_moe(sub_df, sub_w, measure_col, z=1.96):
    """95 % margin of error for the weighted mean of a single measure.

    Uses the linearisation (sandwich) variance estimator:
        μ̂   = Σ(w_i × x_i) / Σ(w_i)
        V̂(μ̂) = Σ(w_i² × (x_i − μ̂)²) / (Σw_i)²
        SE   = sqrt(V̂(μ̂))
        MOE  = z × SE

    Only rows where both the measure and the weight are valid (non-NaN, w > 0)
    are used.  Returns None if there are no valid rows or total weight is 0."""
    if measure_col not in sub_df.columns:
        return None
    xi = pd.to_numeric(sub_df[measure_col], errors="coerce")
    wi = pd.to_numeric(sub_w, errors="coerce").fillna(0)
    valid = xi.notna() & (wi > 0)
    xi = xi[valid]
    wi = wi[valid]
    if len(xi) == 0:
        return None
    total_w = float(wi.sum())
    if total_w == 0:
        return None
    mu_hat = float((wi * xi).sum()) / total_w
    var_hat = float(((wi ** 2) * (xi - mu_hat) ** 2).sum()) / (total_w ** 2)
    return z * math.sqrt(var_hat)


def _compute_multilevel_table(df, weight_col, table_def):
    """
    Multi-level, multi-measure MOSPI estimation engine.
    Supports: multi-level row dimensions, multiple measures per table,
    categorical indicator variables, filters, subtotals.
    """
    filters = table_def.get("filters", [])
    dim_defs = table_def.get("dimensions", [])
    measures = table_def.get("measures", [])
    dim_vars = [d["variable"] for d in dim_defs]

    if filters:
        df = _apply_table_filters(df, filters)
    if len(df) == 0:
        return pd.DataFrame(), "No rows remain after applying filters."

    w = pd.to_numeric(df[weight_col], errors="coerce").fillna(0)

    for dv in dim_vars:
        if dv not in df.columns:
            return pd.DataFrame(), f"Dimension variable '{dv}' not in dataset."
    for mc in measures:
        if mc["variable"] not in df.columns:
            return (
                pd.DataFrame(),
                f"Measure variable '{mc['variable']}' not in dataset.",
            )
        if mc.get("estimation_type") == "Weighted Ratio":
            rd = mc.get("ratio_denominator")
            if rd and rd not in df.columns:
                return pd.DataFrame(), f"Ratio denominator '{rd}' not in dataset."

    col_names = [mc.get("label") or mc["variable"] for mc in measures]

    # Identify "Weighted Mean" measures — each gets its own _MOE_95 column
    wm_measures = [
        (mc, mc.get("label") or mc["variable"])
        for mc in measures
        if mc.get("estimation_type") == "Weighted Mean"
    ]

    def _row_for(sub_df, key_dict):
        sub_w = w[sub_df.index]
        row = dict(key_dict)
        for mc, cn in zip(measures, col_names):
            row[cn] = round(_calc_measure(sub_df, sub_w, mc), 4)
        for mc, cn in wm_measures:
            moe = _calc_weighted_mean_moe(sub_df, sub_w, mc["variable"])
            row[cn + "_MOE_95"] = round(moe, 4) if moe is not None else ""
        return row

    rows = []

    if not dim_vars:
        rows.append(_row_for(df, {"(All)": "Grand Total"}))
    elif len(dim_vars) == 1:
        dv = dim_vars[0]
        for key, grp in df.groupby(dv, sort=True):
            rows.append(_row_for(grp, {dv: key}))
        rows.append(_row_for(df, {dv: "Total"}))
    else:
        l1 = dim_vars[0]
        for l1_key, l1_grp in df.groupby(l1, sort=True):
            for grp_keys, grp in l1_grp.groupby(dim_vars, sort=True):
                if not isinstance(grp_keys, tuple):
                    grp_keys = (grp_keys,)
                rows.append(_row_for(grp, dict(zip(dim_vars, grp_keys))))
            sub_row = _row_for(l1_grp, {l1: l1_key})
            for dv in dim_vars[1:]:
                sub_row[dv] = "(Subtotal)"
            sub_row["_row_type"] = "subtotal"
            rows.append(sub_row)
        total_row = _row_for(df, {dim_vars[0]: "Total"})
        for dv in dim_vars[1:]:
            total_row[dv] = ""
        total_row["_row_type"] = "total"
        rows.append(total_row)

    result = pd.DataFrame(rows)
    return result, None


# ─────────────────────────────────────────────────────────────────────────────
# SURVEY TABLE BUILDER v2.0 — routes
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/table-builder/columns", methods=["GET"])
def table_builder_columns():
    # Prefer Step 4 preprocessed dataset; fall back to Step 3 consolidated file
    preproc_path = os.path.join(OUTPUT_FOLDER, "preprocessed_dataset.csv")
    original_path = os.path.join(OUTPUT_FOLDER, "consolidated_resolved_dataset.csv")

    # Use preprocessed file only if it is NEWER than the consolidated file.
    # If Step 3 was re-run after Step 4 the consolidated file will be newer,
    # meaning Step 4 hasn't been re-run yet — fall back to the fresh Step-3 data.
    preproc_is_fresh = (
        os.path.exists(preproc_path)
        and os.path.exists(original_path)
        and os.path.getmtime(preproc_path) >= os.path.getmtime(original_path)
    )
    if preproc_is_fresh:
        csv_path = preproc_path
        data_source = "preprocessed (Step 4)"
    elif os.path.exists(original_path):
        csv_path = original_path
        data_source = "original (Step 3)"
    else:
        return jsonify(
            {"error": "No dataset found. Run the full pipeline (Step 3) first."}
        ), 400

    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
        weight_col = _detect_weight_column(df)
        visible_cols = [
            c for c in df.columns if not c.lower().endswith("_code") and c != weight_col
        ]
        df_visible = df[visible_cols]
        meta, _ = _get_column_metadata(df_visible)
        # tb_csv_path tracks which file variables were loaded from
        session_data["tb_csv_path"] = csv_path
        session_data["tb_all_cols"] = visible_cols
        session_data["tb_weight_col_auto"] = weight_col
        # Ensure tb_processed_path always points to the best available cleaned file
        if "tb_processed_path" not in session_data or not os.path.exists(
            session_data.get("tb_processed_path", "")
        ):
            if os.path.exists(preproc_path):
                session_data["tb_processed_path"] = preproc_path
        app.logger.info(
            "Table builder: loaded columns from %s (%s)", csv_path, data_source
        )
        return jsonify(
            {
                "success": True,
                "columns": meta,
                "weight_column": weight_col,
                "total_rows": len(df),
                "total_cols": len(visible_cols),
                "data_source": data_source,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/table-builder/preprocess", methods=["POST"])
def table_builder_preprocess():
    body = request.get_json() or {}
    selected = body.get("selected_variables", [])
    if not selected:
        return jsonify({"error": "No variables selected."}), 400

    # Priority: fresh Step-4 preprocessed file → Step-3 consolidated file
    preproc_path = os.path.join(OUTPUT_FOLDER, "preprocessed_dataset.csv")
    original_path = os.path.join(OUTPUT_FOLDER, "consolidated_resolved_dataset.csv")
    preproc_is_fresh = (
        os.path.exists(preproc_path)
        and os.path.exists(original_path)
        and os.path.getmtime(preproc_path) >= os.path.getmtime(original_path)
    )
    if preproc_is_fresh:
        csv_path = preproc_path
    else:
        csv_path = session_data.get("tb_csv_path", original_path)

    if not os.path.exists(csv_path):
        return jsonify(
            {"error": "Source CSV not found. Run Step 3 (and optionally Step 4) first."}
        ), 400

    try:
        df_full = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
        available = [c for c in selected if c in df_full.columns]
        if not available:
            return jsonify(
                {"error": "None of the selected variables found in the dataset."}
            ), 400

        df_sel = df_full[available].copy()
        weight_col = _detect_weight_column(df_sel)
        miss_before = int(sum(df_sel[c].isna().sum() for c in available))
        df_proc = _preprocess_selected(df_sel, available, weight_col)
        miss_after = int(sum(df_proc[c].isna().sum() for c in available))

        proc_path = os.path.join(OUTPUT_FOLDER, "processed_selected_dataset.csv")
        df_proc.to_csv(proc_path, index=False, encoding="utf-8-sig")

        session_data["tb_processed_path"] = proc_path
        session_data["tb_selected_vars"] = available
        session_data["tb_weight_col"] = weight_col
        session_data["tb_preproc_steps"] = [
            "whitespace_cleaning",
            "missing_imputation",
            "outlier_capping_iqr1_5x",
        ]

        app.logger.info(
            "Table builder preprocess: applied on top of %s → %s", csv_path, proc_path
        )
        return jsonify(
            {
                "success": True,
                "stats": {
                    "rows": len(df_proc),
                    "selected_columns": len(available),
                    "weight_column": weight_col,
                    "missing_before": miss_before,
                    "missing_after": miss_after,
                    "missing_treated": miss_before - miss_after,
                    "preprocessing_steps": session_data["tb_preproc_steps"],
                },
            }
        )
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/table-builder/weight-diagnostics", methods=["POST"])
def table_builder_weight_diagnostics():
    body = request.get_json() or {}
    weight_col = body.get("weight_column") or session_data.get("tb_weight_col")
    data_src = body.get("data_source", "processed")

    original_path = os.path.join(OUTPUT_FOLDER, "consolidated_resolved_dataset.csv")
    preproc_path = os.path.join(OUTPUT_FOLDER, "preprocessed_dataset.csv")

    if data_src == "original":
        csv_path = session_data.get("tb_csv_path", original_path)
    else:
        candidate = session_data.get("tb_processed_path", "")
        if candidate and os.path.exists(candidate):
            csv_path = candidate
        elif os.path.exists(preproc_path):
            csv_path = preproc_path
        else:
            csv_path = original_path

    if not csv_path or not os.path.exists(csv_path):
        return jsonify({"error": "Dataset not found. Run preprocessing first."}), 400

    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
        if not weight_col:
            weight_col = _detect_weight_column(df)
        if not weight_col:
            return jsonify({"error": "No weight column specified or detected."}), 400

        diag = _compute_weight_diagnostics(df, weight_col)
        return jsonify(
            {"success": True, "diagnostics": diag, "weight_column": weight_col}
        )
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/table-builder/generate-tables", methods=["POST"])
def table_builder_generate_tables():
    body = request.get_json() or {}

    weight_col = body.get("weight_variable") or session_data.get("tb_weight_col")
    weight_col_label = body.get("weight_variable_label", "")
    data_src = body.get("data_source", "processed")
    table_defs = body.get("tables", [])

    if not table_defs:
        return jsonify({"error": "No table definitions provided."}), 400

    original_path = os.path.join(OUTPUT_FOLDER, "consolidated_resolved_dataset.csv")
    preproc_path = os.path.join(OUTPUT_FOLDER, "preprocessed_dataset.csv")

    if data_src == "original":
        csv_path = original_path
    else:
        # Always use the FULL dataset for table generation.
        # processed_selected_dataset.csv contains only selected columns and may
        # be missing the weight column or dimension variables — never use it here.
        preproc_is_fresh = (
            os.path.exists(preproc_path)
            and os.path.exists(original_path)
            and os.path.getmtime(preproc_path) >= os.path.getmtime(original_path)
        )
        csv_path = preproc_path if preproc_is_fresh else original_path

    if not csv_path or not os.path.exists(csv_path):
        return jsonify(
            {"error": "Dataset not found. Run Step 3 (and optionally Step 4) first."}
        ), 400

    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)

        if not weight_col or weight_col not in df.columns:
            weight_col = _detect_weight_column(df)
        if not weight_col:
            return jsonify({"error": "No weight column detected."}), 400

        os.makedirs(GENERATED_TABLES_FOLDER, exist_ok=True)

        generated = []

        for tdef in table_defs:
            tid = tdef.get("table_id", f"t{len(generated) + 1:03d}")
            title = tdef.get("title") or tid
            universe = tdef.get("universe", "")
            notes = tdef.get("notes", "")
            measures = tdef.get("measures", [])

            if not measures:
                # No measures specified — default to Estimated Value = Σ(mult) for
                # each category, which is the survey-weighted count (not raw row count).
                tdef = dict(tdef)
                tdef["measures"] = [
                    {
                        "variable": weight_col,
                        "label": "Estimated Value",
                        "estimation_type": "Weighted Frequency",
                    }
                ]
                measures = tdef["measures"]
            else:
                # Replace any raw Frequency Count with weighted Estimated Value
                updated_measures = []
                for mc in measures:
                    if mc.get("estimation_type") == "Frequency Count":
                        mc = dict(mc)
                        mc["estimation_type"] = "Weighted Frequency"
                        mc["variable"] = weight_col
                        lbl = mc.get("label", "")
                        if not lbl or lbl.lower() in ("frequency", "frequency count", "count", "freq"):
                            lbl = "Estimated Value"
                        mc["label"] = lbl
                    updated_measures.append(mc)
                tdef = dict(tdef)
                tdef["measures"] = updated_measures
                measures = updated_measures

            result_df, err = _compute_multilevel_table(df, weight_col, tdef)

            if err:
                generated.append({"table_id": tid, "title": title, "error": err})
                continue

            # Strip helper columns from CSV but keep row_type for preview decoration
            row_types = []
            if "_row_type" in result_df.columns:
                row_types = result_df["_row_type"].fillna("detail").tolist()
                csv_df = result_df.drop(columns=["_row_type"])
            else:
                row_types = ["detail"] * len(result_df)
                csv_df = result_df

            safe_name = (
                re.sub(r"[^\w\-]", "_", title.strip().lower())[:60] + f"_{tid}.csv"
            )
            out_path = os.path.join(GENERATED_TABLES_FOLDER, safe_name)
            csv_df.to_csv(out_path, index=False, encoding="utf-8-sig")

            preview_records = csv_df.head(10).fillna("").to_dict(orient="records")

            methodology = {
                "estimation_method": "Horvitz-Thompson weighted estimation",
                "weight_variable": weight_col,
                "weight_variable_label": weight_col_label or weight_col,
                "data_source": data_src,
                "preprocessing_steps": session_data.get("tb_preproc_steps", []),
                "measures": [
                    {
                        "variable": mc["variable"],
                        "label": mc.get("label") or mc["variable"],
                        "estimation_type": mc.get("estimation_type", "Estimated Total"),
                        "formula": FORMULA_MAP.get(
                            mc.get("estimation_type", "Estimated Total"), ""
                        ),
                        "indicator_category": mc.get("indicator_category"),
                        "ratio_denominator": mc.get("ratio_denominator"),
                        **(
                            {
                                "moe_95_column": (mc.get("label") or mc["variable"]) + "_MOE_95",
                                "moe_95_formula": FORMULA_MAP["MOE_95"],
                                "moe_95_description": (
                                    "95% margin of error for the weighted mean. "
                                    "Uses the linearisation variance estimator: "
                                    "V̂(μ̂) = Σ(w_i² × (x_i − μ̂)²) / (Σw_i)², "
                                    "SE = sqrt(V̂(μ̂)), MOE = 1.96 × SE."
                                ),
                            }
                            if mc.get("estimation_type") == "Weighted Mean"
                            else {}
                        ),
                    }
                    for mc in measures
                ],
            }

            relation_check = _check_table_relations(df, tdef, weight_col)

            generated.append(
                {
                    "table_id": tid,
                    "title": title,
                    "universe": universe,
                    "notes": notes,
                    "methodology": methodology,
                    "dimensions": tdef.get("dimensions", []),
                    "filters": tdef.get("filters", []),
                    "rows": len(csv_df),
                    "columns": list(csv_df.columns),
                    "preview": preview_records,
                    "row_types": row_types[:10],
                    "filename": safe_name,
                    "relation_check": relation_check,
                }
            )

        # Persist full table definition JSON
        definition_payload = {
            "schema_version": "2.0",
            "generated_at": pd.Timestamp.now().isoformat(),
            "session": {
                "weight_variable": weight_col,
                "weight_variable_label": weight_col_label or weight_col,
                "data_source": data_src,
                "preprocessing_steps": session_data.get("tb_preproc_steps", []),
            },
            "tables": table_defs,
        }
        def_path = os.path.join(OUTPUT_FOLDER, "table_definition.json")
        with open(def_path, "w", encoding="utf-8") as fh:
            json.dump(definition_payload, fh, indent=2, ensure_ascii=False, default=str)

        # ── Merge with previously generated tables ────────────────────────────
        # Build an ordered dict keyed by table_id so we can update in-place
        # without losing tables that weren't part of this generation run.
        existing = {t["table_id"]: t for t in session_data.get("tb_generated", [])}
        for t in generated:
            existing[t["table_id"]] = t  # add new or overwrite re-generated
        all_generated = list(
            existing.values()
        )  # preserves insertion order (Python 3.7+)

        survey_tables_payload = {
            "schema_version": "2.0",
            "generated_at": pd.Timestamp.now().isoformat(),
            "generated": all_generated,
        }
        survey_path = os.path.join(OUTPUT_FOLDER, "survey_tables.json")
        with open(survey_path, "w", encoding="utf-8") as fh:
            json.dump(
                survey_tables_payload, fh, indent=2, ensure_ascii=False, default=str
            )

        session_data["tb_generated"] = all_generated
        session_data["tb_weight_col"] = weight_col
        # Return the FULL accumulated list so the frontend renders everything
        return jsonify({"success": True, "tables": all_generated})

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/table-builder/status", methods=["GET"])
def table_builder_status():
    return jsonify(
        {
            "processed": "tb_processed_path" in session_data,
            "weight_col": session_data.get("tb_weight_col"),
            "selected_vars": session_data.get("tb_selected_vars", []),
            "generated": session_data.get("tb_generated", []),
            "preproc_steps": session_data.get("tb_preproc_steps", []),
        }
    )


@app.route("/download/generated_tables/<filename>")
def download_generated_table(filename):
    safe = secure_filename(filename)
    path = os.path.join(GENERATED_TABLES_FOLDER, safe)
    if not os.path.exists(path):
        return jsonify({"error": "File not found."}), 404
    return send_file(path, as_attachment=True, download_name=safe)


@app.route("/download/survey_tables.json")
def download_survey_tables_json():
    path = os.path.join(OUTPUT_FOLDER, "survey_tables.json")
    if not os.path.exists(path):
        return jsonify({"error": "File not found."}), 404
    return send_file(path, as_attachment=True, download_name="survey_tables.json")


@app.route("/download/table_definition.json")
def download_table_definition_json():
    path = os.path.join(OUTPUT_FOLDER, "table_definition.json")
    if not os.path.exists(path):
        return jsonify({"error": "File not found."}), 404
    return send_file(path, as_attachment=True, download_name="table_definition.json")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — REPORT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

REPORTS_FOLDER = os.path.join(OUTPUT_FOLDER, "reports")
os.makedirs(REPORTS_FOLDER, exist_ok=True)

# ── AI Response Cache ─────────────────────────────────────────────────────────
# Caches Gemini and Groq responses to disk keyed by a SHA-256 hash of the
# prompt (+ system message for Groq). Avoids redundant API calls when the same
# table data and query are submitted more than once.
AI_CACHE_DIR = os.path.join(os.path.dirname(__file__), "ai_cache")
AI_CACHE_TTL = int(os.environ.get("AI_CACHE_TTL_SECONDS", str(24 * 3600)))  # default 24 h
os.makedirs(AI_CACHE_DIR, exist_ok=True)


def _ai_cache_key(provider: str, *parts: str) -> str:
    """Return a hex digest that uniquely identifies a (provider, prompt) pair."""
    raw = f"{provider}\n" + "\n__SEP__\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ai_cache_get(key: str):
    """Return cached text or None (returns None if expired or missing)."""
    path = os.path.join(AI_CACHE_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.load(fh)
        if AI_CACHE_TTL > 0 and (time.time() - entry["ts"]) > AI_CACHE_TTL:
            os.remove(path)
            return None
        return entry["value"]
    except Exception:
        return None


def _ai_cache_set(key: str, value: str) -> None:
    """Persist a text response to the cache."""
    path = os.path.join(AI_CACHE_DIR, f"{key}.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "value": value}, fh)
    except Exception as exc:
        app.logger.warning("AI cache write failed: %s", exc)


def _auto_select_charts(table):
    """Rule-based chart selection with data-type validation.

    Returns a LIST of spec dicts (0, 1, or 2 charts per table).
    Each spec: { type, x, y, group, is_pct, label }.

    Validation checks:
    - Measure column must contain real numeric values (not all empty/zero).
    - Dimension column must have ≥2 distinct non-empty categories.
    - Chart type is chosen to suit the cardinality and data nature.

    Multiple charts per table (up to 2) are returned when the data supports
    more than one complementary view (e.g. donut + bar for small categoricals,
    grouped_bar + stacked_bar for two-dimension percentage tables).
    """
    columns = table.get("columns", [])
    dimensions = table.get("dimensions", [])
    measures = table.get("measures", [])

    if not columns:
        return []

    # Use detail rows only (skip subtotals / grand totals)
    all_rows = table.get("preview", [])
    row_types = table.get("row_types", [])
    if row_types and len(row_types) == len(all_rows):
        detail_rows = [r for r, rt in zip(all_rows, row_types) if rt == "detail"]
    else:
        detail_rows = all_rows
    if not detail_rows:
        detail_rows = all_rows

    if len(detail_rows) < 2:
        return []

    # Resolve dimension column labels (must exist in columns)
    dim_labels = [d.get("label", d.get("variable", "")) for d in dimensions]
    dim_labels = [l for l in dim_labels if l and l in columns]

    # Resolve primary measure column
    meas_label = measures[0].get("label", "") if measures else ""
    if not meas_label or meas_label not in columns:
        meas_label = next((c for c in columns if c not in dim_labels), "")

    if not meas_label or not dim_labels:
        return []

    # ── Validation: measure must have actual numeric data ────────────────────
    def _try_num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    y_nums = [_try_num(r.get(meas_label)) for r in detail_rows]
    y_nums = [v for v in y_nums if v is not None]
    if len(y_nums) < 2:
        return []  # not a numeric measure column
    if len({v for v in y_nums if v != 0}) < 1:
        return []  # all-zero — nothing to visualise

    is_pct = any(
        kw in meas_label.lower() for kw in ["%", "pct", "percent", "proportion", "rate"]
    )

    specs = []

    # ── Single dimension ─────────────────────────────────────────────────────
    if len(dim_labels) == 1:
        x_col = dim_labels[0]
        x_vals = {
            str(r.get(x_col, ""))
            for r in detail_rows
            if r.get(x_col) not in (None, "", "None")
        }
        n_cats = len(x_vals)

        if n_cats < 2:
            return []

        base = {"x": x_col, "y": meas_label, "group": None, "is_pct": is_pct}

        if n_cats <= 6:
            # Small categorical: donut shows proportions, bar shows absolute values
            specs.append({**base, "type": "donut", "label": "Proportional breakdown"})
            specs.append({**base, "type": "bar", "label": "Category comparison"})
        elif is_pct and n_cats <= 8:
            # Percentage with moderate categories: both views useful
            specs.append({**base, "type": "donut", "label": "Proportional breakdown"})
            specs.append({**base, "type": "bar", "label": "Category comparison"})
        elif n_cats <= 25:
            specs.append({**base, "type": "bar", "label": "Category comparison"})
        else:
            # Many categories: ranked horizontal bar is most readable
            specs.append(
                {**base, "type": "horizontal_bar", "label": "Category ranking"}
            )

        return specs

    # ── Two or more dimensions ───────────────────────────────────────────────
    x_col = dim_labels[0]
    group_col = dim_labels[1]
    n_x = len({str(r.get(x_col, "")) for r in detail_rows if r.get(x_col)})
    n_g = len({str(r.get(group_col, "")) for r in detail_rows if r.get(group_col)})

    if n_x < 2:
        return []

    base2 = {"x": x_col, "y": meas_label, "is_pct": is_pct}

    if n_x > 25:
        # Too many x-axis categories — collapse to simple ranked bar
        specs.append(
            {
                **base2,
                "type": "horizontal_bar",
                "group": None,
                "label": "Category ranking",
            }
        )
    elif n_g <= 6:
        # Two-dimension data: show both grouped and stacked perspectives
        if is_pct:
            specs.append(
                {
                    **base2,
                    "type": "stacked_bar",
                    "group": group_col,
                    "label": "Proportional breakdown by group",
                }
            )
            specs.append(
                {
                    **base2,
                    "type": "grouped_bar",
                    "group": group_col,
                    "label": "Side-by-side comparison",
                }
            )
        else:
            specs.append(
                {
                    **base2,
                    "type": "grouped_bar",
                    "group": group_col,
                    "label": "Side-by-side comparison",
                }
            )
            specs.append(
                {
                    **base2,
                    "type": "stacked_bar",
                    "group": group_col,
                    "label": "Cumulative composition",
                }
            )
    else:
        # Too many groups: collapse grouping dimension
        specs.append(
            {**base2, "type": "bar", "group": None, "label": "Category comparison"}
        )

    return specs


def _make_plotly_chart(table, spec):
    """Build an interactive Plotly chart HTML div for an NSS survey table.

    Returns an HTML string (a <div> with embedded Plotly JSON) or None.
    Plotly.js must be loaded separately in the page <head>.
    """
    try:
        all_rows = table.get("preview", [])
        row_types = table.get("row_types", [])
        if row_types and len(row_types) == len(all_rows):
            detail_rows = [r for r, rt in zip(all_rows, row_types) if rt == "detail"]
        else:
            detail_rows = all_rows
        if not detail_rows:
            detail_rows = all_rows

        chart_type = spec.get("type", "bar")
        x_col = spec.get("x")
        y_col = spec.get("y")
        group_col = spec.get("group")
        is_pct = spec.get("is_pct", False)
        title = table.get("title", "")

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        # Government-report colour palette (inspired by reference)
        COLORS = [
            "#1e3a8a",
            "#0284c7",
            "#0d9488",
            "#16a34a",
            "#b45309",
            "#dc2626",
            "#7c3aed",
            "#0891b2",
            "#d97706",
            "#6366f1",
        ]

        y_axis_title = y_col or ""
        if is_pct and "%" not in y_axis_title:
            y_axis_title += " (%)"

        base_layout = dict(
            title=dict(text=title, font=dict(size=13, color="#1f2937")),
            paper_bgcolor="#ffffff",
            plot_bgcolor="#f9fafb",
            font=dict(
                family="-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif",
                size=11,
                color="#374151",
            ),
            # NOTE: 'showlegend', 'margin', and 'legend' are intentionally omitted
            # here; each chart type passes them explicitly to avoid "multiple values"
            # keyword argument errors from update_layout(**base_layout, showlegend=...).
        )
        _default_margin = dict(l=60, r=30, t=70, b=80)

        # ── Single-series charts ─────────────────────────────────────────────
        if chart_type in ("bar", "horizontal_bar", "pie", "donut"):
            xs = [str(r.get(x_col, "")) for r in detail_rows]
            ys = [_num(r.get(y_col)) for r in detail_rows]

            # Keep at most 30 items; for bar/h-bar sort by value descending
            MAX = 30
            if len(xs) > MAX:
                pairs = sorted(zip(ys, xs), reverse=True)[:MAX]
                ys, xs = zip(*pairs)
                xs, ys = list(xs), list(ys)

            if not xs:
                return None

            if chart_type == "bar":
                fig = go.Figure(
                    go.Bar(
                        x=xs,
                        y=ys,
                        marker=dict(
                            color=[COLORS[i % len(COLORS)] for i in range(len(xs))],
                            line=dict(color="white", width=0.5),
                        ),
                        text=[f"{v:.1f}" for v in ys],
                        textposition="outside",
                        textfont=dict(size=9),
                    )
                )
                fig.update_layout(
                    **base_layout,
                    showlegend=False,
                    margin=_default_margin,
                    xaxis=dict(
                        title=x_col, tickangle=-35, gridcolor="#e5e7eb", zeroline=False
                    ),
                    yaxis=dict(
                        title=y_axis_title, gridcolor="#e5e7eb", zerolinecolor="#d1d5db"
                    ),
                    bargap=0.25,
                )

            elif chart_type == "horizontal_bar":
                pairs = sorted(zip(ys, xs))  # ascending so largest is at top
                ys_s, xs_s = (list(z) for z in zip(*pairs)) if pairs else ([], [])
                fig = go.Figure(
                    go.Bar(
                        x=ys_s,
                        y=xs_s,
                        orientation="h",
                        marker=dict(
                            color=[COLORS[i % len(COLORS)] for i in range(len(xs_s))],
                            line=dict(color="white", width=0.5),
                        ),
                        text=[f"{v:.1f}" for v in ys_s],
                        textposition="outside",
                        textfont=dict(size=9),
                    )
                )
                fig.update_layout(
                    **base_layout,
                    showlegend=False,
                    margin=_default_margin,
                    xaxis=dict(title=y_axis_title, gridcolor="#e5e7eb"),
                    yaxis=dict(title=x_col, gridcolor="#e5e7eb", automargin=True),
                    bargap=0.2,
                    height=max(320, len(xs_s) * 26 + 120),
                )

            else:  # pie / donut
                hole = 0.42 if chart_type == "donut" else 0
                fig = go.Figure(
                    go.Pie(
                        labels=xs,
                        values=ys,
                        hole=hole,
                        marker=dict(
                            colors=COLORS[: len(xs)],
                            line=dict(color="white", width=1.5),
                        ),
                        textinfo="label+percent",
                        textfont=dict(size=10),
                        insidetextorientation="radial",
                        hovertemplate="%{label}: %{value:.1f} (%{percent})<extra></extra>",
                    )
                )
                fig.update_layout(
                    **base_layout,
                    showlegend=True,
                    margin=_default_margin,
                    legend=dict(orientation="v", x=1.01, y=0.5, font=dict(size=10)),
                )

        # ── Multi-series charts (grouped / stacked bar) ──────────────────────
        elif chart_type in ("grouped_bar", "stacked_bar"):
            if not group_col:
                return None

            x_vals = sorted({str(r.get(x_col, "")) for r in detail_rows})
            groups = sorted({str(r.get(group_col, "")) for r in detail_rows})

            if not x_vals or len(groups) < 2:
                return None

            # Build (x_val, group) → y lookup
            lookup = {}
            for r in detail_rows:
                lookup[(str(r.get(x_col, "")), str(r.get(group_col, "")))] = _num(
                    r.get(y_col)
                )

            traces = []
            for i, grp in enumerate(groups[:8]):
                y_vals = [lookup.get((xv, grp), 0) for xv in x_vals]
                traces.append(
                    go.Bar(
                        name=grp,
                        x=x_vals,
                        y=y_vals,
                        marker=dict(color=COLORS[i % len(COLORS)]),
                        text=[f"{v:.1f}" for v in y_vals],
                        textposition="inside"
                        if chart_type == "stacked_bar"
                        else "outside",
                        textfont=dict(size=8),
                    )
                )

            fig = go.Figure(traces)
            fig.update_layout(
                **base_layout,
                showlegend=True,
                barmode="group" if chart_type == "grouped_bar" else "stack",
                xaxis=dict(title=x_col, tickangle=-35, gridcolor="#e5e7eb", automargin=True),
                yaxis=dict(title=y_axis_title, gridcolor="#e5e7eb", automargin=True),
                bargap=0.15,
                bargroupgap=0.06,
                margin=dict(l=70, r=30, t=70, b=160),
                legend=dict(
                    orientation="h",
                    y=-0.38,
                    x=0.5,
                    xanchor="center",
                    yanchor="top",
                    font=dict(size=10),
                    tracegroupgap=4,
                ),
            )

        else:
            return None

        return fig.to_html(
            include_plotlyjs=False,
            full_html=False,
            config={"responsive": True},
        )

    except Exception as exc:
        app.logger.warning("Plotly chart failed (%s): %s", spec.get("type"), exc)
        return None


def _make_graph(table_data, table_title, graph_spec):
    """Render a single matplotlib chart from Gemini's graph spec.
    Returns base64 PNG string or None on failure."""
    try:
        chart_type = graph_spec.get("type", "bar").lower()
        x_col = graph_spec.get("x")
        y_col = graph_spec.get("y")
        y2_col = graph_spec.get("y2")  # for grouped/stacked
        group_col = graph_spec.get("group_col")  # for grouped/stacked
        label = graph_spec.get("label", table_title)
        x_label = graph_spec.get("x_label", x_col or "")
        y_label = graph_spec.get("y_label", y_col or "")

        # Filter to detail rows only; fall back to all rows
        rows = [r for r in table_data if r.get("_row_type", "detail") == "detail"]
        if not rows:
            rows = table_data

        COLORS = [
            "#2563eb",
            "#7c3aed",
            "#0d9488",
            "#ea580c",
            "#16a34a",
            "#dc2626",
            "#d97706",
            "#6366f1",
            "#db2777",
            "#0891b2",
        ]

        def _tonum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        def _to_png(fig):
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            return base64.b64encode(buf.read()).decode("utf-8")

        def _style_ax(ax, title, xlabel="", ylabel=""):
            ax.set_title(title, fontsize=11, fontweight="bold", pad=10)
            ax.set_xlabel(xlabel, fontsize=9)
            ax.set_ylabel(ylabel, fontsize=9)
            ax.tick_params(axis="x", rotation=30, labelsize=8)
            ax.tick_params(axis="y", labelsize=8)
            ax.grid(axis="y", linestyle="--", alpha=0.4, zorder=0)
            for spine in ["top", "right"]:
                ax.spines[spine].set_visible(False)
            ax.set_facecolor("#f9fafb")

        # ── Build xs / ys from full rows ─────────────────────────────────────
        if x_col:
            valid_rows = [
                r
                for r in rows
                if r.get(x_col) is not None and str(r.get(x_col, "")).strip() != ""
            ]
        else:
            valid_rows = rows

        xs = [str(r.get(x_col, "")) for r in valid_rows] if x_col else []
        ys = [_tonum(r.get(y_col)) for r in valid_rows] if y_col else []
        ys2 = [_tonum(r.get(y2_col)) for r in valid_rows] if y2_col else []

        # Truncate to 40 bars maximum (too many bars are unreadable)
        MAX_BARS = 40
        if len(xs) > MAX_BARS:
            xs = xs[:MAX_BARS]
            ys = ys[:MAX_BARS]
            ys2 = ys2[:MAX_BARS]

        if not xs and chart_type not in ("histogram", "scatter"):
            return None

        fig, ax = plt.subplots(figsize=(9, 5))
        fig.patch.set_facecolor("#ffffff")

        # ── Bar ──────────────────────────────────────────────────────────────
        if chart_type == "bar":
            if not ys:
                return None
            bar_colors = [COLORS[i % len(COLORS)] for i in range(len(xs))]
            bars = ax.bar(
                xs,
                ys,
                color=bar_colors,
                edgecolor="white",
                linewidth=0.5,
                width=0.6,
                zorder=3,
            )
            ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=7)
            _style_ax(ax, label, x_label, y_label)

        # ── Horizontal bar ───────────────────────────────────────────────────
        elif chart_type == "horizontal_bar":
            if not ys:
                return None
            bar_colors = [COLORS[i % len(COLORS)] for i in range(len(xs))]
            bars = ax.barh(
                xs,
                ys,
                color=bar_colors,
                edgecolor="white",
                linewidth=0.5,
                height=0.6,
                zorder=3,
            )
            ax.set_title(label, fontsize=11, fontweight="bold", pad=10)
            ax.set_xlabel(y_label or y_col or "", fontsize=9)
            ax.set_ylabel(x_label or x_col or "", fontsize=9)
            ax.tick_params(axis="y", labelsize=8)
            ax.tick_params(axis="x", labelsize=8)
            ax.grid(axis="x", linestyle="--", alpha=0.4, zorder=0)
            for spine in ["top", "right"]:
                ax.spines[spine].set_visible(False)
            ax.set_facecolor("#f9fafb")

        # ── Grouped bar ──────────────────────────────────────────────────────
        elif chart_type == "grouped_bar":
            if not ys or not ys2:
                return None
            import numpy as np

            x_pos = np.arange(len(xs))
            w = 0.35
            b1 = ax.bar(
                x_pos - w / 2,
                ys,
                w,
                label=y_col,
                color=COLORS[0],
                edgecolor="white",
                zorder=3,
            )
            b2 = ax.bar(
                x_pos + w / 2,
                ys2,
                w,
                label=y2_col,
                color=COLORS[1],
                edgecolor="white",
                zorder=3,
            )
            ax.bar_label(b1, fmt="%.1f", padding=2, fontsize=7)
            ax.bar_label(b2, fmt="%.1f", padding=2, fontsize=7)
            ax.set_xticks(x_pos)
            ax.set_xticklabels(xs)
            ax.legend(fontsize=8)
            _style_ax(ax, label, x_label, y_label)

        # ── Stacked bar ──────────────────────────────────────────────────────
        elif chart_type == "stacked_bar":
            if not ys or not ys2:
                return None
            b1 = ax.bar(
                xs, ys, 0.6, label=y_col, color=COLORS[0], edgecolor="white", zorder=3
            )
            b2 = ax.bar(
                xs,
                ys2,
                0.6,
                label=y2_col,
                color=COLORS[1],
                edgecolor="white",
                bottom=ys,
                zorder=3,
            )
            ax.legend(fontsize=8)
            _style_ax(ax, label, x_label, y_label)

        # ── Pie ──────────────────────────────────────────────────────────────
        elif chart_type == "pie":
            if not ys:
                return None
            explode = [0.04] * len(xs)
            wedges, texts, autotexts = ax.pie(
                ys,
                labels=xs,
                colors=[COLORS[i % len(COLORS)] for i in range(len(xs))],
                autopct="%1.1f%%",
                startangle=140,
                explode=explode,
                textprops={"fontsize": 8},
                shadow=False,
            )
            for at in autotexts:
                at.set_fontsize(7)
            ax.set_title(label, fontsize=11, fontweight="bold", pad=14)

        # ── Donut ────────────────────────────────────────────────────────────
        elif chart_type == "donut":
            if not ys:
                return None
            wedges, texts, autotexts = ax.pie(
                ys,
                labels=xs,
                colors=[COLORS[i % len(COLORS)] for i in range(len(xs))],
                autopct="%1.1f%%",
                startangle=140,
                pctdistance=0.80,
                textprops={"fontsize": 8},
                wedgeprops=dict(width=0.5),
            )
            for at in autotexts:
                at.set_fontsize(7)
            ax.set_title(label, fontsize=11, fontweight="bold", pad=14)

        # ── Line ─────────────────────────────────────────────────────────────
        elif chart_type == "line":
            if not ys:
                return None
            ax.plot(
                xs,
                ys,
                marker="o",
                color=COLORS[0],
                linewidth=2,
                markersize=5,
                label=y_col or "",
            )
            if ys2:
                ax.plot(
                    xs,
                    ys2,
                    marker="s",
                    color=COLORS[1],
                    linewidth=2,
                    markersize=5,
                    linestyle="--",
                    label=y2_col or "",
                )
                ax.legend(fontsize=8)
            ax.fill_between(range(len(xs)), ys, alpha=0.08, color=COLORS[0])
            _style_ax(ax, label, x_label, y_label)

        # ── Scatter ──────────────────────────────────────────────────────────
        elif chart_type == "scatter":
            if not ys:
                return None
            import numpy as np

            sc = ax.scatter(
                xs if not all(isinstance(x, str) for x in xs) else range(len(xs)),
                ys,
                c=COLORS[0],
                alpha=0.7,
                edgecolors="white",
                linewidths=0.5,
                s=60,
                zorder=3,
            )
            _style_ax(ax, label, x_label, y_label)

        # ── Histogram ────────────────────────────────────────────────────────
        elif chart_type == "histogram":
            if y_col:
                vals = [
                    _tonum(r.get(y_col)) for r in valid_rows if r.get(y_col) is not None
                ]
            elif ys:
                vals = ys
            else:
                return None
            n, bins, patches = ax.hist(
                vals,
                bins=min(30, max(10, len(vals) // 10)),
                color=COLORS[0],
                edgecolor="white",
                alpha=0.85,
                zorder=3,
            )
            _style_ax(ax, label, x_label or (y_col or ""), "Frequency")

        # ── Heatmap ──────────────────────────────────────────────────────────
        elif chart_type == "heatmap":
            import numpy as np

            if not ys:
                return None
            n_cols = min(8, max(1, int(np.sqrt(len(ys)))))
            n_rows = int(np.ceil(len(ys) / n_cols))
            mat = np.array(
                ys[: n_rows * n_cols] + [0] * (n_rows * n_cols - len(ys))
            ).reshape(n_rows, n_cols)
            plt.close(fig)
            fig, ax = plt.subplots(figsize=(max(6, n_cols * 1.2), max(4, n_rows * 0.9)))
            im = ax.imshow(mat, cmap="YlOrRd", aspect="auto")
            plt.colorbar(im, ax=ax)
            ax.set_title(label, fontsize=11, fontweight="bold", pad=10)
            for i in range(n_rows):
                for j in range(n_cols):
                    idx = i * n_cols + j
                    if idx < len(ys):
                        ax.text(
                            j, i, f"{ys[idx]:.1f}", ha="center", va="center", fontsize=7
                        )
            ax.set_facecolor("#f9fafb")

        # ── Default → bar ────────────────────────────────────────────────────
        else:
            if not ys:
                return None
            bar_colors = [COLORS[i % len(COLORS)] for i in range(len(xs))]
            bars = ax.bar(
                xs,
                ys,
                color=bar_colors,
                edgecolor="white",
                linewidth=0.5,
                width=0.6,
                zorder=3,
            )
            ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=7)
            _style_ax(ax, label, x_label, y_label)

        fig.tight_layout()
        return _to_png(fig)

    except Exception as exc:
        app.logger.warning(
            "Graph generation failed (%s): %s", graph_spec.get("type"), exc
        )
        plt.close("all")
        return None


def _call_gemini(prompt):
    """Gemini call with disk-based token cache. Returns cached text on HIT."""
    cache_key = _ai_cache_key("gemini", prompt)
    cached = _ai_cache_get(cache_key)
    if cached is not None:
        app.logger.info("AI cache HIT (Gemini) key=%s", cache_key[:12])
        return cached

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set.")
    from google import genai as google_genai

    client = google_genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    result = resp.text.strip()
    _ai_cache_set(cache_key, result)
    app.logger.info("AI cache MISS (Gemini) — stored key=%s", cache_key[:12])
    return result


def _call_groq(
    prompt, system="You are a senior statistical analyst for government survey reports."
):
    """Groq API call with disk-based token cache. Returns cached text on HIT."""
    cache_key = _ai_cache_key("groq", system, prompt)
    cached = _ai_cache_get(cache_key)
    if cached is not None:
        app.logger.info("AI cache HIT (Groq) key=%s", cache_key[:12])
        return cached

    from groq import Groq

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY secret not set.")
    client = Groq(api_key=api_key)
    completion = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=4096,
    )
    result = completion.choices[0].message.content.strip()
    _ai_cache_set(cache_key, result)
    app.logger.info("AI cache MISS (Groq) — stored key=%s", cache_key[:12])
    return result


def _build_report_html(
    tables_payload, query, gemini_sections, trend_html="", table_charts=None, meta=None
):
    """Assemble the final editable HTML report in official statistical format."""
    now = pd.Timestamp.now().strftime("%d %B %Y")
    now_long = pd.Timestamp.now().strftime("%B %d, %Y")
    now_full = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    if table_charts is None:
        table_charts = {}
    if meta is None:
        meta = {}

    csv_name = meta.get("csv_filename", "Survey Dataset")
    n_tables = len(tables_payload)
    n_with_charts = len(table_charts)
    total_rows = sum(t.get("rows", len(t.get("preview", []))) for t in tables_payload)
    total_cols = len({c for t in tables_payload for c in t.get("columns", [])})
    has_query = bool(query and query.strip())

    analysis_text = gemini_sections.get("analysis", "")
    insights_text = gemini_sections.get("insights", "")
    recs_text = gemini_sections.get("recommendations", "")
    qa_text = gemini_sections.get("query_answer", "")

    # ── Table HTML ───────────────────────────────────────────────────────────
    def _table_html(t):
        cols = t.get("columns", [])
        rows = t.get("preview", [])
        rts = t.get("row_types", [])
        thead = "".join(
            f'<th style="background:#1e3a8a;color:#fff;padding:8px 10px;'
            f"font-size:0.82rem;font-weight:600;border:1px solid #cbd5e1;"
            f'white-space:nowrap">{col}</th>'
            for col in cols
        )
        tbody = ""
        for i, row in enumerate(rows):
            rt = rts[i] if i < len(rts) else "detail"
            style = (
                ' style="font-weight:700;background:#eff6ff;border-top:2px solid #bfdbfe"'
                if rt == "total"
                else ' style="font-style:italic;background:#f5f3ff;color:#6d28d9"'
                if rt == "subtotal"
                else ""
            )
            cells = "".join(
                f'<td style="padding:7px 10px;border:1px solid #e2e8f0;'
                f'font-size:0.83rem;vertical-align:top">{row.get(c, "")}</td>'
                for c in cols
            )
            zebra = ' style="background:#f8fafc"' if (i % 2 == 1 and not style) else ""
            tbody += f"<tr{style or zebra}>{cells}</tr>"
        m = t.get("methodology", {})
        dims = " → ".join(
            d.get("label", d.get("variable", "")) for d in t.get("dimensions", [])
        )
        note = (
            f'<p style="font-size:11px;color:#9ca3af;margin-top:4px">'
            f"Showing {len(rows)} of {t.get('rows', len(rows))} rows.</p>"
            if len(rows) < t.get("rows", len(rows))
            else ""
        )
        return (
            f'<p style="font-size:11px;color:#64748b;margin-bottom:6px">'
            f"Dimensions: {dims} &nbsp;|&nbsp; "
            f'Weight: <code style="background:#f1f5f9;padding:1px 5px;'
            f'border-radius:3px;font-size:11px">{m.get("weight_variable", "—")}</code>'
            f" &nbsp;|&nbsp; Method: {m.get('estimation_method', '—')}</p>"
            f'<div style="overflow-x:auto;border:1px solid #cbd5e1">'
            f'<table style="width:100%;border-collapse:collapse;table-layout:auto">'
            f"<thead><tr>{thead}</tr></thead>"
            f"<tbody>{tbody}</tbody>"
            f"</table></div>{note}"
        )

    # ── Figure card HTML (chart) ─────────────────────────────────────────────
    fig_idx = [0]

    def _figure_card(ch, t_title, section):
        fig_idx[0] += 1
        chart_type_label = ch["type"].replace("_", " ").title()
        return (
            f'<div class="figure-card" style="border:1px solid #cbd5e1;padding:18px;'
            f'margin:20px 0;background:#f8fafc;page-break-inside:avoid">'
            f'<div style="font-size:0.82rem;font-weight:700;color:#1e3a8a;'
            f'margin-bottom:10px;text-transform:uppercase;letter-spacing:0.05em">'
            f"Figure {section}.{fig_idx[0]}: {ch['label']} — {t_title}</div>"
            f'<div style="background:#fff;border:1px solid #e2e8f0;padding:6px">'
            f"{ch['div']}</div>"
            f'<div class="figure-caption" contenteditable="true" '
            f'style="background:#fff;border-left:3px solid #1e3a8a;padding:10px 14px;'
            f'font-size:0.84rem;margin-top:10px;color:#334155;line-height:1.5">'
            f"<strong>Chart type:</strong> {chart_type_label}. "
            f"This figure visualises the distribution of values from "
            f"<em>{t_title}</em>. Edit this caption to add your analytical inference."
            f"</div></div>"
        )

    # ── Section 10: Tables only (charts moved to §12 gallery) ───────────────
    tables_with_charts_html = ""
    for t in tables_payload:
        tables_with_charts_html += (
            f'<div style="margin-bottom:40px">'
            f'<h4 contenteditable="true" style="font-size:1rem;font-weight:600;'
            f'color:#0f172a;margin-bottom:8px">{t.get("title", "Table")}</h4>'
            f"{_table_html(t)}"
            f"</div>"
        )

    # ── Section 12: Charts gallery (all charts together) ────────────────────
    fig_idx[0] = 0
    charts_gallery_html = ""
    for t in tables_payload:
        charts = table_charts.get(t["title"], [])
        for ch in charts:
            charts_gallery_html += _figure_card(ch, t["title"], 12)

    # ── TOC rows ─────────────────────────────────────────────────────────────
    toc_sections = [
        ("1", "Cover Page"),
        ("2", "Table of Contents"),
        ("3", "Executive Summary"),
        ("4", "Introduction"),
        ("5", "Data Source and Coverage"),
        ("6", "Concepts, Definitions, and Variable Roles"),
        ("7", "Methodology"),
        ("8", "Data Quality Assessment"),
        ("9", "Dataset Structure and Variable Inventory"),
        ("10", "Descriptive Statistical Analysis"),
        ("11", "Analytical Findings"),
        ("12", "Charts and Figures"),
        ("13", "Key Findings"),
        ("14", "Recommendations"),
        ("15", "Limitations"),
        ("16", "Reproducibility and Audit Notes"),
        ("17", "Annexures"),
    ]
    toc_rows = "".join(
        f"<tr>"
        f'<td style="width:10%;font-weight:600;color:#1e3a8a;padding:10px 14px;'
        f'border-bottom:1px dashed #cbd5e1">&sect;&nbsp;{n}</td>'
        f'<td style="width:80%;padding:10px 14px;border-bottom:1px dashed #cbd5e1">{title}</td>'
        f'<td style="width:10%;text-align:right;color:#64748b;padding:10px 14px;'
        f'border-bottom:1px dashed #cbd5e1">p.&nbsp;{n}</td>'
        f"</tr>"
        for n, title in toc_sections
    )

    # ── Variable inventory rows (§9) ─────────────────────────────────────────
    seen_cols = {}
    for t in tables_payload:
        dims_set = {
            d.get("label", d.get("variable", "")) for d in t.get("dimensions", [])
        }
        for col in t.get("columns", []):
            if col not in seen_cols:
                seen_cols[col] = "Dimension" if col in dims_set else "Measure"
    inv_rows = "".join(
        f"<tr>"
        f'<td style="padding:7px 10px;border-bottom:1px solid #f1f5f9;font-size:0.83rem">'
        f"<strong>{col}</strong></td>"
        f'<td style="padding:7px 10px;border-bottom:1px solid #f1f5f9;font-size:0.83rem">'
        f'<span style="background:{"#1e3a8a" if kind == "Dimension" else "#0d9488"};'
        f'color:#fff;padding:2px 8px;border-radius:3px;font-size:0.75rem">{kind}</span>'
        f"</td>"
        f"</tr>"
        for col, kind in seen_cols.items()
    )

    # ── Rec cards ─────────────────────────────────────────────────────────────
    default_recs = [
        (
            "Establish Systematic Metadata Documentation",
            "Create a formal variable schema including descriptions, weights, and "
            "expected ranges before downstream processing.",
        ),
        (
            "Configure Sampling Weight Verification",
            "Validate sampling weights for each observation stratum to ensure "
            "weighted estimates reflect the survey design.",
        ),
        (
            "Cross-tabulate Key Dimensions",
            "Compare findings across state, sector (rural/urban), and demographic "
            "dimensions to identify heterogeneity in the survey results.",
        ),
    ]

    if recs_text:
        recs_html = (
            f'<div contenteditable="true" style="outline:none">{recs_text}</div>'
        )
    else:
        recs_html = "".join(
            f'<div class="rec-card" style="border:1px solid #cbd5e1;'
            f"border-left:4px solid #1e3a8a;padding:15px 20px;margin-bottom:15px;"
            f'background:#f8fafc" contenteditable="true">'
            f'<div style="font-weight:600;color:#0f172a;font-size:1rem;'
            f'margin-bottom:5px">Action Recommendation {i + 1}: {title}</div>'
            f'<div style="font-size:0.88rem;color:#475569">{body}</div>'
            f"</div>"
            for i, (title, body) in enumerate(default_recs)
        )

    # ── Optional sections ────────────────────────────────────────────────────
    query_section_html = ""
    if has_query and qa_text:
        query_section_html = (
            f'<div class="section-card" style="margin-bottom:40px;padding-top:16px;'
            f'border-top:1px solid #e2e8f0">'
            f'<h2 class="section-title" style="font-size:1.5rem;margin-bottom:14px;'
            f'color:#0f172a">Query Response</h2>'
            f'<div class="section-content">'
            f'<div style="background:#fdf4ff;border:1px solid #e9d5ff;'
            f'border-radius:6px;padding:16px 20px;margin-bottom:12px">'
            f'<div style="font-weight:600;color:#6d28d9;margin-bottom:6px">Q: {query}</div>'
            f'<div contenteditable="true">{qa_text}</div>'
            f"</div></div></div>"
        )

    trend_section_html = ""
    if trend_html:
        # Strip the old wrapper and re-wrap in the new style
        inner = re.sub(
            r'<div[^>]*class="section"[^>]*>.*?<h2>.*?</h2>',
            "",
            trend_html,
            flags=re.DOTALL,
        )
        inner = re.sub(r"</div>\s*$", "", inner.strip())
        trend_section_html = (
            f'<div style="background:#fefce8;border:1px solid #fde68a;'
            f'border-radius:6px;padding:16px 20px;margin-bottom:16px" contenteditable="true">'
            f"{inner or trend_html}"
            f"</div>"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Full HTML document
    # ══════════════════════════════════════════════════════════════════════════
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8"/>\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>\n'
        f"<title>DataVizAI Statistical Analytical Report — {now}</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com"/>\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>\n'
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700'
        '&family=Playfair+Display:ital,wght@0,600;0,700;1,400&display=swap" rel="stylesheet"/>\n'
        '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"/>\n'
        '<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"/>\n'
        '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>\n'
        "<style>\n"
        ":root{--primary:#0f172a;--primary-accent:#1e3a8a;--secondary:#475569;"
        "--accent:#b45309;--bg-light:#f8fafc;--bg-white:#ffffff;"
        "--border:#cbd5e1;--text-dark:#0f172a;--text-muted:#64748b}\n"
        "body{font-family:'Inter',system-ui,-apple-system,sans-serif;"
        "background:#f8fafc;color:#0f172a;line-height:1.6}\n"
        "h1,h2,h3,h4,h5,h6{font-family:'Playfair Display',Georgia,serif;"
        "color:#0f172a;font-weight:700}\n"
        ".navbar{background:#0f172a;border-bottom:2px solid #1e3a8a;"
        "padding:12px 0;position:sticky;top:0;z-index:1000}\n"
        ".navbar-content{max-width:1200px;margin:0 auto;padding:0 20px;"
        "display:flex;justify-content:space-between;align-items:center}\n"
        ".logo-text{color:#fff;font-size:1.15rem;font-weight:600;"
        "font-family:'Playfair Display',serif}\n"
        ".nav-links a{color:#cbd5e1;text-decoration:none;font-weight:500;"
        "font-size:0.88rem;margin-left:20px;transition:color 0.2s}\n"
        ".nav-links a:hover{color:#fff}\n"
        ".report-container{max-width:1000px;margin:28px auto;"
        "background:#fff;border:1px solid #cbd5e1;padding:32px 44px;"
        "box-shadow:0 4px 6px -1px rgba(0,0,0,.05)}\n"
        ".cover-banner{background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);"
        "color:#fff;padding:28px 32px 22px;margin-bottom:0;text-align:left}\n"
        ".cover-banner-label{font-size:0.72rem;font-weight:600;text-transform:uppercase;"
        "letter-spacing:.12em;color:#C8962A;margin-bottom:8px}\n"
        ".cover-title{font-size:2rem;margin-bottom:6px;color:#fff;"
        "letter-spacing:-.02em;font-family:'Playfair Display',serif}\n"
        ".cover-subtitle{font-size:1rem;color:#cbd5e1;font-style:italic;margin-bottom:0}\n"
        ".cover-meta-grid{display:grid;grid-template-columns:1fr 1fr;gap:0;margin:0;"
        "border:1px solid #cbd5e1;border-top:none}\n"
        ".cover-meta-cell{padding:10px 16px;border-bottom:1px solid #cbd5e1;"
        "border-right:1px solid #cbd5e1;font-size:0.9rem}\n"
        ".cover-meta-cell:nth-child(even){border-right:none}\n"
        ".cover-meta-cell-label{font-size:0.72rem;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.08em;color:#C8962A;display:block;margin-bottom:3px}\n"
        ".cover-meta-cell-value{font-size:0.92rem;color:#0f172a;font-weight:500}\n"
        ".section-card{margin-bottom:40px;padding-top:16px;border-top:1px solid #e2e8f0}\n"
        ".section-card:first-of-type{border-top:none}\n"
        ".report-body{counter-reset:section}\n"
        ".section-title{font-size:1.5rem;margin-bottom:14px;counter-increment:section;"
        "display:flex;align-items:center}\n"
        ".section-title::before{content:'Section 'counter(section)': ';"
        "color:#1e3a8a;margin-right:8px;font-family:'Playfair Display',serif}\n"
        ".section-content{font-size:0.97rem;color:#1e293b}\n"
        ".section-content p{margin-bottom:14px}\n"
        ".indicator-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));"
        "gap:14px;margin:22px 0}\n"
        ".indicator-box{border:1px solid #cbd5e1;background:#f8fafc;padding:18px;text-align:center}\n"
        ".indicator-val{font-size:1.7rem;font-weight:700;color:#1e3a8a;"
        "font-family:'Playfair Display',serif}\n"
        ".indicator-lbl{font-size:0.82rem;color:#475569;margin-top:4px;"
        "text-transform:uppercase;letter-spacing:.05em}\n"
        ".positioning-notice{background:#fffbeb;border-left:4px solid #b45309;"
        "padding:12px 16px;font-size:0.8rem;color:#78350f;margin:16px 0 0}\n"
        ".floating-actions{position:fixed;bottom:25px;right:25px;z-index:1000;"
        "display:flex;flex-direction:column;gap:10px}\n"
        ".btn-action{width:48px;height:48px;border-radius:50%;background:#1e3a8a;"
        "color:#fff;display:flex;align-items:center;justify-content:center;"
        "border:none;box-shadow:0 4px 10px rgba(0,0,0,.15);"
        "transition:transform .2s,background-color .2s;cursor:pointer}\n"
        ".btn-action:hover{transform:scale(1.06);background:#0f172a}\n"
        "[contenteditable='true']:focus{outline:2px dashed #2563eb;"
        "outline-offset:2px;border-radius:2px}\n"
        "@media print{"
        ".navbar,.floating-actions{display:none!important}"
        "body{background:#fff!important}"
        ".report-container{border:none!important;box-shadow:none!important;"
        "padding:0!important;margin:0!important;max-width:100%!important}"
        ".section-card{page-break-before:always;break-before:page}"
        ".section-card:first-of-type{page-break-before:avoid;break-before:avoid}"
        ".figure-card{page-break-inside:avoid;break-inside:avoid}"
        "[contenteditable]{outline:none}"
        "}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        # ── Navbar ──────────────────────────────────────────────────────────
        '<nav class="navbar">\n'
        '  <div class="navbar-content">\n'
        '    <div class="logo-text">National Statistical Analyst Platform</div>\n'
        '    <div class="nav-links">\n'
        '      <a href="#cover"><i class="fas fa-home me-1"></i>Report</a>\n'
        '      <a href="javascript:window.print()">'
        '<i class="fas fa-print me-1"></i>Print / Save PDF</a>\n'
        "    </div>\n"
        "  </div>\n"
        "</nav>\n"
        # ── Floating actions ─────────────────────────────────────────────────
        '<div class="floating-actions">\n'
        '  <button class="btn-action" onclick="window.print()" title="Print or Save PDF">'
        '<i class="fas fa-print"></i></button>\n'
        '  <button class="btn-action" onclick="window.scrollTo({top:0,behavior:\'smooth\'})" '
        'title="Back to top"><i class="fas fa-chevron-up"></i></button>\n'
        "</div>\n"
        # ── Main container ───────────────────────────────────────────────────
        '<div class="report-container" id="cover">\n'
        '<div class="report-body">\n'
        # §1 Cover page
        '<div class="cover-page" style="text-align:center;padding-bottom:16px;'
        'margin-bottom:28px;border-bottom:2px solid #1e3a8a">\n'
        '<div class="cover-banner">\n'
        '<div class="cover-banner-label">Statistical Analytical Report &nbsp;·&nbsp; DataVizAI</div>\n'
        f'<h1 class="cover-title">DATAVIZAI SURVEY ANALYSIS REPORT</h1>\n'
        '<p class="cover-subtitle">Prepared in Official Statistical Publication Format</p>\n'
        "</div>\n"
        '<div class="cover-meta-grid">\n'
        f'<div class="cover-meta-cell"><span class="cover-meta-cell-label">Dataset</span>'
        f'<span class="cover-meta-cell-value">{csv_name}</span></div>\n'
        f'<div class="cover-meta-cell"><span class="cover-meta-cell-label">Reporting Date</span>'
        f'<span class="cover-meta-cell-value">{now_long}</span></div>\n'
        f'<div class="cover-meta-cell"><span class="cover-meta-cell-label">Tables Analysed</span>'
        f'<span class="cover-meta-cell-value">{n_tables} tables &nbsp;({n_with_charts} with charts)</span></div>\n'
        f'<div class="cover-meta-cell"><span class="cover-meta-cell-label">Approx. Observations</span>'
        f'<span class="cover-meta-cell-value">{total_rows:,} rows &times; {total_cols} columns</span></div>\n'
        '<div class="cover-meta-cell"><span class="cover-meta-cell-label">Report Status</span>'
        '<span class="cover-meta-cell-value">Official-Style Statistical Publication</span></div>\n'
        '<div class="cover-meta-cell"><span class="cover-meta-cell-label">Chart Engine</span>'
        '<span class="cover-meta-cell-value">Plotly.js v2.35.2 (Interactive)</span></div>\n'
        "</div>\n"
        '<div class="positioning-notice">\n'
        "<strong>Positioning Notice:</strong> This document is styled as an official statistical "
        "report in standard publication format. It does not claim to be issued, approved, "
        "certified, or endorsed by MoSPI, NSO, or any government ministry or public authority. "
        "Calculations assume equal selection probabilities across all observations.\n"
        "</div>\n"
        "</div>\n"
        # §2 Table of Contents
        '<div class="section-card">\n'
        '<h2 class="section-title">Table of Contents</h2>\n'
        '<div class="section-content">\n'
        '<table style="width:100%;border-collapse:collapse">'
        f"{toc_rows}"
        "</table>\n"
        "</div></div>\n"
        # §3 Executive Summary
        '<div class="section-card">\n'
        '<h2 class="section-title">Executive Summary</h2>\n'
        '<div class="section-content">\n'
        + (
            f'<div contenteditable="true">{insights_text}</div>'
            if insights_text
            else '<div contenteditable="true">'
            f"<p>This report presents a statistical analysis of <strong>{n_tables}</strong> "
            f"cross-tabulation table(s) derived from the survey dataset "
            f"<strong>{csv_name}</strong>. "
            f"A total of {total_rows:,} observations across {total_cols} variables were "
            f"examined. Interactive charts have been generated for "
            f"{n_with_charts} table(s) to support visual interpretation of the data.</p>"
            "</div>"
        )
        + f'<div class="indicator-grid">\n'
        f'<div class="indicator-box"><div class="indicator-val">{n_tables}</div>'
        f'<div class="indicator-lbl">Tables</div></div>\n'
        f'<div class="indicator-box"><div class="indicator-val">{n_with_charts}</div>'
        f'<div class="indicator-lbl">Charts Generated</div></div>\n'
        f'<div class="indicator-box"><div class="indicator-val">{total_rows:,}</div>'
        f'<div class="indicator-lbl">Total Rows</div></div>\n'
        f'<div class="indicator-box"><div class="indicator-val">{total_cols}</div>'
        f'<div class="indicator-lbl">Unique Columns</div></div>\n'
        "</div>\n"
        "</div></div>\n"
        # §4 Introduction
        '<div class="section-card">\n'
        '<h2 class="section-title">Introduction</h2>\n'
        '<div class="section-content" contenteditable="true">\n'
        f"<p>This report documents a thorough statistical characterisation of "
        f"NSS survey data from the dataset <strong>{csv_name}</strong>. "
        f"The analysis covers cross-tabulation tables built from microdata, "
        f"examining distributional patterns across dimensions such as state, "
        f"sector (rural/urban), social group, and other relevant categorical variables.</p>\n"
        f"<p>The report layout conforms to publication standards for official statistics, "
        f"structuring content logically from an initial data overview through to detailed "
        f"findings, visual analysis, and recommendations. "
        f"All text sections in this report are directly editable — click any paragraph "
        f"to modify it before printing or saving as PDF.</p>\n"
        "</div></div>\n"
        # §5 Data Source and Coverage
        '<div class="section-card">\n'
        '<h2 class="section-title">Data Source and Coverage</h2>\n'
        '<div class="section-content">\n'
        "<p>The source data was processed through the DataVizAI pipeline. "
        "Details of the input data shape, coverage markers, and observation constraints "
        "are summarised below:</p>\n"
        '<div style="overflow-x:auto;border:1px solid #cbd5e1">'
        '<table class="table table-bordered" style="margin-bottom:0;font-size:0.88rem">'
        "<thead><tr>"
        '<th style="background:#1e3a8a;color:#fff;padding:8px 12px">Coverage Property</th>'
        '<th style="background:#1e3a8a;color:#fff;padding:8px 12px">Observed Value</th>'
        '<th style="background:#1e3a8a;color:#fff;padding:8px 12px">Description</th>'
        "</tr></thead><tbody>"
        f"<tr><td>Source Dataset</td><td><code>{csv_name}</code></td>"
        f"<td>Uploaded microdata file</td></tr>"
        f"<tr><td>Tables Analysed</td><td>{n_tables}</td>"
        f"<td>Cross-tabulation tables in this report</td></tr>"
        f"<tr><td>Approx. Rows Summarised</td><td>{total_rows:,}</td>"
        f"<td>Total observation rows across all tables</td></tr>"
        f"<tr><td>Unique Variables</td><td>{total_cols}</td>"
        f"<td>Distinct columns across all tables</td></tr>"
        f"<tr><td>Charts Produced</td><td>{n_with_charts}</td>"
        f"<td>Tables with at least one interactive chart</td></tr>"
        f"<tr><td>Report Generated</td><td>{now_full}</td>"
        f"<td>Analysis timestamp</td></tr>"
        "</tbody></table></div>\n"
        "</div></div>\n"
        # §6 Concepts, Definitions, Variable Roles
        '<div class="section-card">\n'
        '<h2 class="section-title">Concepts, Definitions, and Variable Roles</h2>\n'
        '<div class="section-content" contenteditable="true">\n'
        "<p>To support proper statistical treatment, data attributes are assigned operational "
        "roles based on their mathematical behaviour:</p>\n"
        '<ul style="margin:12px 0 16px 20px">\n'
        '<li style="margin-bottom:8px"><strong>Numeric Measure:</strong> Continuous or discrete '
        "quantitative variables showing magnitudes (e.g. MPCE, count, estimated persons). "
        "Subject to means, totals, and proportions.</li>\n"
        '<li style="margin-bottom:8px"><strong>Categorical Dimension:</strong> Categorical '
        "factors defining attributes or cohort memberships (e.g. State, Sector, Social Group). "
        "Subject to unique value breakdowns and cross-tabulations.</li>\n"
        '<li style="margin-bottom:8px"><strong>Identifiers / Codes:</strong> Structural keys '
        "(block codes, record sequences) excluded from statistical summaries to prevent "
        "over-interpretation.</li>\n"
        "</ul>\n"
        '<p class="text-danger small"><strong>Notice on Missing Metadata:</strong> '
        "Official metadata logs, sampling weight files, and response rate forms may be "
        "absent from the uploaded package. Calculations may assume equal weight "
        "probability where weights are unavailable.</p>\n"
        "</div></div>\n"
        # §7 Methodology
        '<div class="section-card">\n'
        '<h2 class="section-title">Methodology</h2>\n'
        '<div class="section-content" contenteditable="true">\n'
        "<p>The statistical pipeline follows a structured processing path:</p>\n"
        '<ol style="margin:10px 0 16px 20px">\n'
        '<li style="margin-bottom:6px"><strong>Data Ingestion:</strong> Safe reading of source '
        "CSV microdata into a pandas DataFrame with encoding detection.</li>\n"
        '<li style="margin-bottom:6px"><strong>Layout Mapping:</strong> Excel data-layout file '
        "parsed to map raw column codes (e.g. <code>b11c4</code>) to full field names and "
        "block numbers.</li>\n"
        '<li style="margin-bottom:6px"><strong>Codebook Extraction:</strong> PDF schedule '
        'parsed to extract value labels from "CODES FOR BLOCK N" sections.</li>\n'
        '<li style="margin-bottom:6px"><strong>Table Construction:</strong> Cross-tabulations '
        "built with user-specified dimensions, measures, and estimation method "
        "(weighted/unweighted count or proportion).</li>\n"
        '<li style="margin-bottom:6px"><strong>Chart Generation:</strong> Rule-based chart '
        "type selection validates data types and cardinality before rendering interactive "
        "Plotly charts — no AI call required for visualisation.</li>\n"
        '<li style="margin-bottom:6px"><strong>AI Text Analysis:</strong> Where API keys are '
        "configured, Groq LLaMA or Gemini generates analytical paragraphs, key insights, and "
        "recommendations grounded strictly in the table data.</li>\n"
        "</ol>\n"
        "</div></div>\n"
        # §8 Data Quality Assessment
        '<div class="section-card">\n'
        '<h2 class="section-title">Data Quality Assessment</h2>\n'
        '<div class="section-content" contenteditable="true">\n'
        "<p>The tables in this report were validated through the following quality checks "
        "prior to rendering:</p>\n"
        '<ul style="margin:10px 0 16px 20px">\n'
        '<li style="margin-bottom:6px"><strong>Numeric Measure Validation:</strong> '
        "Chart generation is skipped for any measure column that contains fewer than "
        "two distinct non-zero numeric values.</li>\n"
        '<li style="margin-bottom:6px"><strong>Category Count Validation:</strong> '
        "Dimension columns must have at least two distinct non-empty categories before "
        "a chart is produced.</li>\n"
        '<li style="margin-bottom:6px"><strong>Row Type Awareness:</strong> '
        "Subtotal and grand-total rows are excluded from chart calculations to prevent "
        "double-counting.</li>\n"
        "</ul>\n"
        "</div></div>\n"
        # §9 Dataset Structure and Variable Inventory
        '<div class="section-card">\n'
        '<h2 class="section-title">Dataset Structure and Variable Inventory</h2>\n'
        '<div class="section-content">\n'
        "<p>The variable inventory lists all columns present across the report tables, "
        "classified by their functional role:</p>\n"
        '<div style="overflow-x:auto;border:1px solid #cbd5e1">'
        '<table style="width:100%;border-collapse:collapse;font-size:0.85rem">'
        "<thead><tr>"
        '<th style="background:#1e3a8a;color:#fff;padding:8px 12px">Variable Name</th>'
        '<th style="background:#1e3a8a;color:#fff;padding:8px 12px">Functional Type</th>'
        "</tr></thead>"
        f"<tbody>{inv_rows}</tbody>"
        "</table></div>\n"
        "</div></div>\n"
        # §10 Descriptive Statistical Analysis (tables + inline charts)
        '<div class="section-card">\n'
        '<h2 class="section-title">Descriptive Statistical Analysis</h2>\n'
        '<div class="section-content">\n'
        "<p>The following cross-tabulation tables were generated from the survey microdata. "
        "Interactive charts for these tables are presented in the Charts and Figures section.</p>\n"
        f"{tables_with_charts_html}\n"
        "</div></div>\n"
        # §11 Analytical Findings
        '<div class="section-card">\n'
        '<h2 class="section-title">Analytical Findings</h2>\n'
        '<div class="section-content">\n'
        + (
            f'<div contenteditable="true">{analysis_text}</div>'
            if analysis_text
            else '<div contenteditable="true">'
            "<p>Analytical findings will appear here when an AI key (Groq or Gemini) "
            "is configured. The analysis is grounded strictly in the data from the tables above — "
            "no external facts or generic statements are added.</p>"
            "</div>"
        )
        + (
            f'\n<div style="margin-top:16px"><h4 style="font-size:1rem;font-weight:600;'
            f'margin-bottom:10px">Trend Summary</h4>{trend_section_html}</div>'
            if trend_section_html
            else ""
        )
        + "\n</div></div>\n"
        # §12 Charts and Figures
        + '<div class="section-card">\n'
        '<h2 class="section-title">Charts and Figures</h2>\n'
        '<div class="section-content">\n'
        + (
            f"<p>The following figures provide visual summaries of the {n_with_charts} "
            f"table(s) for which chart generation succeeded. Up to 2 complementary chart "
            f"types are shown per table. All chart captions are editable.</p>\n"
            f"{charts_gallery_html}\n"
            if charts_gallery_html
            else '<p style="color:#64748b;font-style:italic">No charts were generated for this '
            "report. This may occur when table measures are non-numeric, all values are "
            "zero, or dimension columns have fewer than 2 distinct categories.</p>\n"
        )
        + "</div></div>\n"
        # §13 Key Findings
        + '<div class="section-card">\n'
        '<h2 class="section-title">Key Findings</h2>\n'
        '<div class="section-content">\n'
        + (
            f'<div contenteditable="true">{insights_text}</div>'
            if insights_text
            else '<div contenteditable="true">'
            f'<ul style="margin:10px 0 16px 20px">'
            f'<li style="margin-bottom:8px">The report covers {n_tables} cross-tabulation '
            f"table(s) from the dataset <strong>{csv_name}</strong>.</li>"
            f'<li style="margin-bottom:8px">A total of approximately {total_rows:,} observation '
            f"rows were summarised across {total_cols} unique variables.</li>"
            f'<li style="margin-bottom:8px">Interactive charts were generated for '
            f"{n_with_charts} of {n_tables} tables, subject to data quality validation.</li>"
            f"</ul>"
            "</div>"
        )
        + "\n</div></div>\n"
        # §14 Recommendations
        + '<div class="section-card">\n'
        '<h2 class="section-title">Recommendations</h2>\n'
        '<div class="section-content">\n'
        f"{recs_html}\n"
        "</div></div>\n"
        # §15 Limitations
         + '<div class="section-card">\n'
        '<h2 class="section-title">Limitations</h2>\n'
        '<div class="section-content" contenteditable="true">\n'
        "<p>This report operates under several critical analytical constraints:</p>\n"
        '<ul style="margin:10px 0 16px 20px">\n'
        '<li style="margin-bottom:8px"><strong>Equal Selection Probability:</strong> '
        "Due to a lack of stratification weights in some tables, calculations may assume "
        "equal representation, which can skew generalisations.</li>\n"
        '<li style="margin-bottom:8px"><strong>Preview Row Limit:</strong> '
        "Chart generation uses the preview rows loaded into the browser session. "
        "Very large tables may render charts from a subset of the full data.</li>\n"
        '<li style="margin-bottom:8px"><strong>Observational Limits:</strong> '
        "Distribution patterns identify co-movements but do not establish causal "
        "relationships between variables.</li>\n"
        '<li style="margin-bottom:8px"><strong>PDF Codebook Accuracy:</strong> '
        "Value label extraction from PDF schedules depends on text-layer quality; "
        "scanned PDFs may yield incomplete codebooks.</li>\n"
        "</ul>\n"
        "</div></div>\n"
        # §16 Reproducibility and Audit Notes
         + '<div class="section-card">\n'
        '<h2 class="section-title">Reproducibility and Audit Notes</h2>\n'
        '<div class="section-content">\n'
        '<table class="table table-bordered" style="font-size:0.88rem">'
        "<thead><tr>"
        '<th style="background:#1e3a8a;color:#fff;padding:8px 12px">Parameter</th>'
        '<th style="background:#1e3a8a;color:#fff;padding:8px 12px">Audit Record</th>'
        "</tr></thead><tbody>"
        f"<tr><td>Analysis Date</td><td>{now_full}</td></tr>"
        f"<tr><td>Source Dataset</td><td><code>{csv_name}</code></td></tr>"
        f"<tr><td>Tables in Report</td><td>{n_tables}</td></tr>"
        "<tr><td>Chart Engine</td><td>Plotly.js v2.35.2 (rule-based, no AI)</td></tr>"
        "<tr><td>Text Analysis Engine</td><td>Groq LLaMA / Gemini (when API key configured)</td></tr>"
        "<tr><td>Transparency Statement</td>"
        "<td>All table and chart data derive directly from uploaded survey files. "
        "No external data sources were used.</td></tr>"
        "</tbody></table>\n"
        "</div></div>\n"
        # §17 Annexures
         + '<div class="section-card">\n'
        '<h2 class="section-title">Annexures</h2>\n'
        '<div class="section-content">\n'
        + (
            f'<h4 style="font-size:1rem;font-weight:600;margin-bottom:10px">Query Response</h4>'
            f'<div style="background:#fdf4ff;border:1px solid #e9d5ff;border-radius:6px;'
            f'padding:16px 20px;margin-bottom:20px">'
            f'<div style="font-weight:600;color:#6d28d9;margin-bottom:6px">Q: {query}</div>'
            f'<div contenteditable="true">{qa_text}</div>'
            f"</div>"
            if has_query and qa_text
            else ""
        )
        + '<h4 style="font-size:1rem;font-weight:600;margin-bottom:10px">Estimation Formulae</h4>\n'
        '<div style="border:1px solid #cbd5e1;padding:16px;margin-bottom:14px;background:#f8fafc">\n'
        '<div style="font-weight:600;font-size:0.95rem;color:#1e3a8a">Weighted Mean</div>\n'
        '<div style="font-family:Courier New,monospace;background:#fff;padding:8px 12px;'
        'text-align:center;border:1px solid #e2e8f0;margin:8px 0;font-size:0.95rem">'
        "&#x1D465;&#772; = &Sigma;(w&#x1D45C; &times; x&#x1D45C;) / &Sigma;w&#x1D45C;</div>\n"
        '<div style="font-size:0.83rem;color:#475569">Weighted arithmetic mean across '
        "sampled observations with survey weights w&#x1D45C;.</div>\n"
        "</div>\n"
        '<div style="border:1px solid #cbd5e1;padding:16px;margin-bottom:14px;background:#f8fafc">\n'
        '<div style="font-weight:600;font-size:0.95rem;color:#1e3a8a">Estimated Population Total</div>\n'
        '<div style="font-family:Courier New,monospace;background:#fff;padding:8px 12px;'
        'text-align:center;border:1px solid #e2e8f0;margin:8px 0;font-size:0.95rem">'
        "T&#x0302; = M &times; m&#x207B;&#xB9; &times; &Sigma;(w&#x1D45C; &times; y&#x1D45C;)</div>\n"
        '<div style="font-size:0.83rem;color:#475569">Multiplier-expanded estimated total '
        "for domain of interest.</div>\n"
        "</div>\n"
        "</div></div>\n"
        # Close report-body and container
        + "</div>\n</div>\n"
        # Footer
        + '<div style="max-width:1000px;margin:0 auto;padding:16px 44px 32px;'
        "font-size:11px;color:#9ca3af;text-align:center;"
        'border-top:1px solid #e5e7eb;margin-top:0">\n'
        "Generated by DataVizAI &nbsp;·&nbsp; "
        f"Survey Intelligence Platform · {now} &nbsp;·&nbsp; "
        "<em>Click any section heading or paragraph to edit before printing</em>\n"
        "</div>\n"
        "</body>\n"
        "</html>"
    )


@app.route("/report/generate", methods=["POST"])
def report_generate():
    body = request.get_json() or {}
    tables_from_client = body.get("tables", [])  # full snapshots sent by basket
    table_ids = body.get("table_ids", [])  # legacy fallback
    query = body.get("query", "").strip()

    if tables_from_client:
        # Use the exact snapshots the frontend basket had — this preserves
        # older versions of a table that were overwritten in session_data
        # by a later re-generation of the same slot.
        tables = [t for t in tables_from_client if t and not t.get("error")]
    else:
        # Legacy path: look up from session by table_id
        all_tables = session_data.get("tb_generated", [])
        if not all_tables:
            return jsonify({"error": "No tables generated yet. Run Step 5 first."}), 400
        if table_ids:
            tables = [
                t
                for t in all_tables
                if t.get("table_id") in table_ids and not t.get("error")
            ]
        else:
            tables = [t for t in all_tables if not t.get("error")]

    if not tables:
        return jsonify(
            {
                "error": "No valid tables in the report basket. Add at least one table and try again."
            }
        ), 400

    # Charts are generated programmatically via Plotly — no AI key needed.
    # AI keys are only used for the text analysis sections; those are skipped
    # gracefully when no key is configured.
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key_env = os.environ.get("GROQ_API_KEY", "")

    try:
        # ── 0. Load full CSV data for each table (richer data for charts) ───
        def _load_full_table_data(t):
            """Load the full generated table CSV, fall back to preview rows."""
            fname = t.get("filename", "")
            if fname:
                fpath = os.path.join(GENERATED_TABLES_FOLDER, fname)
                try:
                    df_t = pd.read_csv(fpath, encoding="utf-8-sig", low_memory=False)
                    return df_t.fillna("").to_dict(orient="records")
                except Exception:
                    pass
            return t.get("preview", [])

        # Pre-load full data (used for both charts and AI text)
        full_table_data = {t["title"]: _load_full_table_data(t) for t in tables}

        # ── 1. Rule-based chart selection with validation + Plotly rendering ──
        # _auto_select_charts returns 0-2 validated specs per table.
        # Results are stored per-table title for the report builder.
        table_charts = {}  # { table_title: [{"type", "label", "div"}, ...] }
        for t in tables:
            t_full = dict(t)
            t_full["preview"] = full_table_data.get(t["title"], t.get("preview", []))
            specs = _auto_select_charts(t_full)
            chart_list = []
            for spec in specs:
                chart_div = _make_plotly_chart(t_full, spec)
                if chart_div:
                    chart_list.append(
                        {
                            "type": spec["type"],
                            "label": spec.get("label", t["title"]),
                            "div": chart_div,
                        }
                    )
                    app.logger.info(
                        "Chart '%s' generated for '%s'", spec["type"], t["title"]
                    )
                else:
                    app.logger.warning(
                        "Plotly chart returned None for '%s' (type=%s)",
                        t["title"],
                        spec.get("type"),
                    )
            if chart_list:
                table_charts[t["title"]] = chart_list

        # ── 2. Build rich table summary for AI text generation ──────────────
        def _table_summary(t):
            all_rows = full_table_data.get(t["title"], t.get("preview", []))
            m = t.get("methodology", {})
            return (
                f"Table: '{t['title']}'\n"
                f"  Columns: {t.get('columns', [])}\n"
                f"  Dimensions: {[d.get('variable') for d in t.get('dimensions', [])]}\n"
                f"  Measures: {[mc.get('label') for mc in m.get('measures', [])]}\n"
                f"  Weight method: {m.get('estimation_method', '')}\n"
                f"  Row count: {t.get('rows', len(all_rows))}\n"
                f"  Data (up to 20 rows): {json.dumps(all_rows[:20], default=str)}"
            )

        table_summaries = "\n\n".join(_table_summary(t) for t in tables)
        query_section = f"\n\nUser query: {query}" if query else ""

        # ── 3. Use Groq for detailed analysis, insights, recommendations ────
        text_sections = {
            "analysis": "",
            "query_answer": "",
            "insights": "",
            "recommendations": "",
        }

        def _extract_section(text, name):
            pattern = rf"{name}:\s*(.*?)(?=\n[A-Z_]+:|$)"
            m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            return m.group(1).strip() if m else ""

        if groq_key_env:
            try:
                analysis_prompt = (
                    "You are a senior statistical analyst writing a formal government "
                    "survey report based on survey data.\n\n"
                    "IMPORTANT RULES:\n"
                    "- Base ALL analysis strictly on the data provided. Do NOT add external facts or generic statements.\n"
                    "- Cite specific numbers from the tables.\n"
                    "- Do not mention any data, percentages or figures that are not in the provided tables.\n\n"
                    "Tables provided:\n\n"
                    + table_summaries
                    + query_section
                    + "\n\nWrite the following sections in clean HTML (use <p>, <ul>, <li>, <strong> tags):\n\n"
                    "ANALYSIS:\n"
                    "2-3 paragraphs of objective analysis of the data patterns visible in the tables. "
                    "Mention specific numbers exactly as they appear in the data.\n\n"
                    "QUERY_ANSWER:\n"
                    + (
                        f"Answer this specific user query using only the provided data: {query}\n\n"
                        if query
                        else "Leave blank.\n\n"
                    )
                    + "INSIGHTS:\n"
                    "3-5 bullet-point key insights as <ul><li>...</li></ul>. "
                    "Every insight must cite a specific number from the tables.\n\n"
                    "RECOMMENDATIONS:\n"
                    "3-4 bullet-point policy or analytical recommendations as <ul><li>...</li></ul>.\n\n"
                    "Format EXACTLY as:\n"
                    "ANALYSIS:\n<html>\n\nQUERY_ANSWER:\n<html>\n\nINSIGHTS:\n<html>\n\nRECOMMENDATIONS:\n<html>"
                )
                analysis_text = _call_groq(analysis_prompt)
                text_sections = {
                    "analysis": _extract_section(analysis_text, "ANALYSIS"),
                    "query_answer": _extract_section(analysis_text, "QUERY_ANSWER"),
                    "insights": _extract_section(analysis_text, "INSIGHTS"),
                    "recommendations": _extract_section(
                        analysis_text, "RECOMMENDATIONS"
                    ),
                }
                app.logger.info("Groq text generation complete")
            except Exception as exc:
                app.logger.warning(
                    "Groq text generation failed, falling back to Gemini: %s", exc
                )
                # Fall through to Gemini fallback below

        # Gemini fallback for text if Groq unavailable/failed
        if not text_sections.get("analysis") and gemini_key:
            try:
                analysis_prompt = (
                    "You are a senior statistical analyst writing a formal government "
                    "survey report based on survey data.\n\n"
                    "IMPORTANT: Base ALL analysis strictly on the data provided. Cite specific numbers.\n\n"
                    "Tables:\n\n"
                    + table_summaries
                    + query_section
                    + "\n\nWrite these HTML sections:\n"
                    "ANALYSIS:\n2-3 paragraphs citing specific numbers from the tables.\n\n"
                    "QUERY_ANSWER:\n"
                    + (f"Answer: {query}\n\n" if query else "Leave blank.\n\n")
                    + "INSIGHTS:\n3-5 bullet points as <ul><li>...</li></ul> with specific numbers.\n\n"
                    "RECOMMENDATIONS:\n3-4 recommendations as <ul><li>...</li></ul>.\n\n"
                    "Format EXACTLY as:\nANALYSIS:\n<html>\n\nQUERY_ANSWER:\n<html>\n\nINSIGHTS:\n<html>\n\nRECOMMENDATIONS:\n<html>"
                )
                analysis_text = _call_gemini(analysis_prompt)
                text_sections = {
                    "analysis": _extract_section(analysis_text, "ANALYSIS"),
                    "query_answer": _extract_section(analysis_text, "QUERY_ANSWER"),
                    "insights": _extract_section(analysis_text, "INSIGHTS"),
                    "recommendations": _extract_section(
                        analysis_text, "RECOMMENDATIONS"
                    ),
                }
            except Exception as exc:
                app.logger.warning("Gemini text fallback failed: %s", exc)

        # ── 4. Ask Gemini for conclusive trend inferences (brief) ───────────
        trend_html = ""
        if gemini_key:
            try:
                trend_prompt = (
                    "You are a data analyst summarising key trends from survey tables.\n\n"
                    "Based ONLY on the data below, identify 2-3 conclusive trends or patterns "
                    "that stand out across the tables. Be concise and factual.\n\n"
                    + table_summaries
                    + "\n\nRespond with 2-3 short bullet points in HTML: <ul><li>...</li></ul>. "
                    "Each point must reference a specific number or comparison from the data. "
                    "Do NOT add any information not present in the tables."
                )
                trend_text = _call_gemini(trend_prompt)
                # Ensure it's wrapped in a list
                if "<ul>" not in trend_text:
                    trend_text = f"<ul><li>{trend_text}</li></ul>"
                trend_html = (
                    '<div class="section"><h2>Trend Summary</h2>'
                    f'<div class="insight-box" contenteditable="true">{trend_text}</div></div>'
                )
            except Exception as exc:
                app.logger.warning("Gemini trend summary failed: %s", exc)

        # ── 5. Assemble and save report ─────────────────────────────────────
        meta = {
            "csv_filename": os.path.basename(session_data.get("csv_path", ""))
            or "Survey Dataset",
            "n_tables": len(tables),
        }
        report_html = _build_report_html(
            tables,
            query,
            text_sections,
            trend_html,
            table_charts=table_charts,
            meta=meta,
        )

        report_filename = f"report_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.html"
        report_path = os.path.join(REPORTS_FOLDER, report_filename)
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(report_html)

        session_data["last_report_filename"] = report_filename

        return jsonify(
            {
                "success": True,
                "html": report_html,
                "filename": report_filename,
            }
        )

    except Exception as exc:
        return jsonify({"error": str(exc), "trace": traceback.format_exc()}), 500


@app.route("/report/download/<filename>")
def report_download(filename):
    safe = secure_filename(filename)
    path = os.path.join(REPORTS_FOLDER, safe)
    if not os.path.exists(path):
        return jsonify({"error": "Report file not found."}), 404
    return send_file(path, as_attachment=True, download_name=safe, mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
