import os
import json
import traceback
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "results").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

_GC = None
_SHEET = None
_WS_CACHE = {}
_HEADER_CACHE = {}

# 你目前表頭會用到的核心欄位，先固定住，避免不同分頁表頭飄掉
DEFAULT_HEADERS = [
    "比賽ID",
    "聯盟",
    "賽季",
    "比賽日期",
    "對戰",
    "主隊",
    "客隊",
    "讓分盤",
    "大小分盤",
    "最終比分",
    "判定時間",
    "更新時間",
    "近10讓分推薦",
    "近10讓分結果",
    "近10大小_淨值推薦",
    "近10大小_淨值結果",
    "近10大小_相加推薦",
    "近10大小_相加結果",
    "主客讓分推薦",
    "主客讓分結果",
    "主客大小_淨值推薦",
    "主客大小_淨值結果",
    "主客大小_相加推薦",
    "主客大小_相加結果",
    "EDGE讓分推薦",
    "EDGE讓分結果",
    "EDGE大小推薦",
    "EDGE大小結果",
    "EDGE讓分值",
    "EDGE大小值",
]

RESULT_KEYS = [
    "近10讓分結果",
    "近10大小_淨值結果",
    "近10大小_相加結果",
    "主客讓分結果",
    "主客大小_淨值結果",
    "主客大小_相加結果",
    "EDGE讓分結果",
    "EDGE大小結果",
]


def safe_str(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def clean_str(value):
    return safe_str(value).strip()


def normalize_score_text(value):
    s = clean_str(value)
    if not s:
        return ""
    if not s.startswith("'"):
        s = "'" + s
    return s


def get_gspread_client():
    global _GC

    if _GC is not None:
        return _GC

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _GC = gspread.authorize(creds)
    return _GC


def get_spreadsheet():
    global _SHEET

    if _SHEET is not None:
        return _SHEET

    if not SPREADSHEET_ID:
        raise RuntimeError("Missing SPREADSHEET_ID")

    gc = get_gspread_client()
    _SHEET = gc.open_by_key(SPREADSHEET_ID)
    return _SHEET


def list_worksheets():
    sh = get_spreadsheet()
    return sh.worksheets()


def get_worksheet_by_name(title):
    key = clean_str(title)
    if not key:
        raise RuntimeError("Worksheet title is empty")

    if key in _WS_CACHE:
        return _WS_CACHE[key]

    sh = get_spreadsheet()

    try:
        ws = sh.worksheet(key)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=key, rows=5000, cols=160)

    _WS_CACHE[key] = ws
    return ws


def read_header(ws):
    first_row = ws.row_values(1)
    return [clean_str(h) for h in first_row if clean_str(h)]


def get_header(ws, force_refresh=False):
    key = ws.title

    if not force_refresh and key in _HEADER_CACHE:
        return _HEADER_CACHE[key]

    header = read_header(ws)
    _HEADER_CACHE[key] = header
    return header


def set_header(ws, header):
    cleaned = [clean_str(h) for h in (header or []) if clean_str(h)]
    if not cleaned:
        return

    ws.update("1:1", [cleaned])
    _HEADER_CACHE[ws.title] = cleaned


def find_first_non_empty_header(exclude_titles=None):
    exclude_titles = set(exclude_titles or [])

    for ws in list_worksheets():
        if ws.title in exclude_titles:
            continue
        header = get_header(ws, force_refresh=True)
        if header:
            return ws, header

    return None, []


def get_template_header():
    template_ws = get_worksheet_by_name(WORKSHEET_NAME)
    header = get_header(template_ws, force_refresh=True)
    if header:
        return template_ws, header

    fallback_ws, fallback_header = find_first_non_empty_header(exclude_titles={template_ws.title})
    if fallback_header:
        return fallback_ws, fallback_header

    return template_ws, DEFAULT_HEADERS[:]


def alias_value(data, col_name):
    if col_name in data and data.get(col_name) is not None:
        return data.get(col_name)

    alias_map = {
        "比賽ID": ["game_id", "event_id", "id"],
        "聯盟": ["league"],
        "賽季": ["season"],
        "比賽日期": ["game_date", "date"],
        "對戰": ["matchup"],
        "主隊": ["home_team", "home"],
        "客隊": ["away_team", "away"],
        "最終比分": ["final_score", "score"],
        "判定時間": ["judged_at"],
        "更新時間": ["updated_at"],
        "讓分盤": ["spread_line"],
        "大小分盤": ["total_line"],
        "EDGE讓分值": ["edge_spread_value"],
        "EDGE大小值": ["edge_total_value"],
    }

    for key in alias_map.get(col_name, []):
        if key in data and data.get(key) is not None:
            return data.get(key)

    return ""


def get_target_worksheet_name(data):
    league = clean_str(alias_value(data, "聯盟")).upper()
    season = clean_str(alias_value(data, "賽季"))

    if league and season:
        return f"{league}_{season}"
    if season:
        return season
    return WORKSHEET_NAME


def ensure_target_worksheet_with_header(data):
    target_title = get_target_worksheet_name(data)
    ws = get_worksheet_by_name(target_title)
    header = get_header(ws, force_refresh=True)

    if header:
        return ws, header, None

    source_ws, template_header = get_template_header()
    set_header(ws, template_header)
    return ws, template_header, source_ws.title


def build_row_from_header(data, header):
    row = []

    for col in header:
        value = alias_value(data, col)

        if col == "最終比分":
            row.append(normalize_score_text(value))
        else:
            row.append(safe_str(value))

    return row


def col_to_a1(n):
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_all_records_safe(ws, header):
    rows = ws.get_all_values()
    if not rows:
        return []

    if len(rows) <= 1:
        return []

    data_rows = rows[1:]
    width = len(header)
    out = []

    for row in data_rows:
        padded = list(row) + [""] * max(0, width - len(row))
        padded = padded[:width]
        obj = {header[i]: padded[i] for i in range(width)}
        out.append(obj)

    return out


def find_row_by_game_id(ws, header, game_id):
    gid = clean_str(game_id)
    if not gid:
        return None

    try:
        col_idx = header.index("比賽ID") + 1
    except ValueError:
        return None

    values = ws.col_values(col_idx)
    for i in range(2, len(values) + 1):
        if clean_str(values[i - 1]) == gid:
            return i
    return None


def merge_payload_with_existing(existing_row, payload, header):
    merged = {}

    for col in header:
        old_val = existing_row.get(col, "")
        new_val = alias_value(payload, col)

        if new_val is None:
            new_val = ""

        if clean_str(new_val) != "":
            merged[col] = new_val
        else:
            merged[col] = old_val

    return merged


def parse_limit(default_value=5000, max_value=20000):
    raw = request.args.get("limit", str(default_value))
    try:
        n = int(raw)
    except Exception:
        n = default_value
    if n < 1:
        n = 1
    if n > max_value:
        n = max_value
    return n


def worksheet_name_from_query():
    league = clean_str(request.args.get("league")).upper()
    season = clean_str(request.args.get("season"))

    if league and season:
        return f"{league}_{season}"
    if season:
        return season
    return WORKSHEET_NAME


def build_stats_rows(rows):
    return rows


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "ok": True,
        "service": "nba-mlb-cloud",
        "message": "running",
    }), 200


@app.route("/health", methods=["GET"])
def health():
    try:
        template_ws = get_worksheet_by_name(WORKSHEET_NAME)
        template_header = get_header(template_ws, force_refresh=True)

        fallback_ws = None
        fallback_header_count = 0
        if not template_header:
            fallback_ws, fallback_header = find_first_non_empty_header(exclude_titles={template_ws.title})
            fallback_header_count = len(fallback_header)

        return jsonify({
            "ok": True,
            "spreadsheet_id": SPREADSHEET_ID,
            "template_worksheet": template_ws.title,
            "template_header_count": len(template_header),
            "fallback_template_worksheet": fallback_ws.title if fallback_ws else "",
            "fallback_header_count": fallback_header_count,
            "worksheets": [ws.title for ws in list_worksheets()],
        }), 200

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 500


@app.route("/results", methods=["GET"])
def results():
    try:
        ws_name = worksheet_name_from_query()
        ws = get_worksheet_by_name(ws_name)
        header = get_header(ws, force_refresh=True)

        if not header:
            return jsonify({
                "ok": True,
                "rows": [],
                "worksheet": ws.title,
                "count": 0,
            }), 200

        rows = get_all_records_safe(ws, header)
        limit = parse_limit()

        # 最新的擺前面，讓前端回填先處理最近的
        rows = list(reversed(rows))
        rows = rows[:limit]

        return jsonify({
            "ok": True,
            "rows": rows,
            "worksheet": ws.title,
            "count": len(rows),
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 500


@app.route("/stats", methods=["GET"])
def stats():
    try:
        ws_name = worksheet_name_from_query()
        ws = get_worksheet_by_name(ws_name)
        header = get_header(ws, force_refresh=True)

        if not header:
            return jsonify({
                "ok": True,
                "rows": [],
                "worksheet": ws.title,
                "count": 0,
            }), 200

        rows = get_all_records_safe(ws, header)
        limit = parse_limit()
        rows = list(reversed(rows))
        rows = rows[:limit]

        return jsonify({
            "ok": True,
            "rows": build_stats_rows(rows),
            "worksheet": ws.title,
            "count": len(rows),
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 500


@app.route("/save_result", methods=["OPTIONS", "POST"])
def save_result():
    if request.method == "OPTIONS":
        return ("", 204)

    try:
        data = request.get_json(force=True, silent=True) or {}

        if not isinstance(data, dict):
            return jsonify({
                "ok": False,
                "error": "Invalid JSON payload",
            }), 400

        game_id = clean_str(alias_value(data, "比賽ID"))
        if not game_id:
            return jsonify({
                "ok": False,
                "error": "Missing 比賽ID",
            }), 400

        ws, header, header_source = ensure_target_worksheet_with_header(data)

        existing_row_no = find_row_by_game_id(ws, header, game_id)

        if existing_row_no:
            records = get_all_records_safe(ws, header)
            existing_idx = existing_row_no - 2
            existing_row = records[existing_idx] if 0 <= existing_idx < len(records) else {}
            merged = merge_payload_with_existing(existing_row, data, header)
            row = build_row_from_header(merged, header)

            end_col = col_to_a1(len(header))
            ws.update(f"A{existing_row_no}:{end_col}{existing_row_no}", [row])
            action = "updated"
        else:
            row = build_row_from_header(data, header)
            ws.append_row(row, value_input_option="RAW")
            action = "inserted"

        return jsonify({
            "ok": True,
            "message": "saved",
            "action": action,
            "worksheet": ws.title,
            "header_count": len(header),
            "header_source": header_source or ws.title,
            "game_id": game_id,
            "league": clean_str(alias_value(data, "聯盟")),
            "season": clean_str(alias_value(data, "賽季")),
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
