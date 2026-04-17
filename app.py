import os
import json
import traceback
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials


app = Flask(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()

# 這張當作目前表頭來源 / 預設分頁
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "results").strip()

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

_GC = None
_SHEET = None
_WS_CACHE = {}
_HEADER_CACHE = {}


def safe_str(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


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


def get_worksheet_by_name(title):
    global _WS_CACHE

    key = str(title or "").strip()
    if not key:
        raise RuntimeError("Worksheet title is empty")

    if key in _WS_CACHE:
        return _WS_CACHE[key]

    sh = get_spreadsheet()

    try:
        ws = sh.worksheet(key)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=key, rows=2000, cols=120)

    _WS_CACHE[key] = ws
    return ws


def read_header(ws):
    first_row = ws.row_values(1)
    return [str(h).strip() for h in first_row if str(h).strip()]


def get_header(ws):
    global _HEADER_CACHE

    key = ws.title
    if key in _HEADER_CACHE:
        return _HEADER_CACHE[key]

    header = read_header(ws)
    _HEADER_CACHE[key] = header
    return header


def set_header(ws, header):
    global _HEADER_CACHE

    cleaned = [str(h).strip() for h in (header or []) if str(h).strip()]
    if not cleaned:
        return

    ws.update("1:1", [cleaned])
    _HEADER_CACHE[ws.title] = cleaned


def get_template_header():
    template_ws = get_worksheet_by_name(WORKSHEET_NAME)
    header = get_header(template_ws)
    if not header:
        raise RuntimeError(
            f"{WORKSHEET_NAME} 第1列沒有表頭，請先把目前使用中的表頭放到 {WORKSHEET_NAME} 第1列"
        )
    return header


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


def normalize_score_text(value):
    s = safe_str(value).strip()
    if not s:
        return ""
    if not s.startswith("'"):
        s = "'" + s
    return s


def build_row_from_existing_header(data, header):
    row = []

    for col in header:
        value = alias_value(data, col)

        if col == "最終比分":
            row.append(normalize_score_text(value))
        else:
            row.append(safe_str(value))

    return row


def get_target_worksheet_name(data):
    season = safe_str(alias_value(data, "賽季")).strip()
    return season if season else WORKSHEET_NAME


def ensure_target_worksheet_with_header(data):
    target_title = get_target_worksheet_name(data)
    ws = get_worksheet_by_name(target_title)
    header = get_header(ws)

    if header:
        return ws, header

    template_header = get_template_header()
    set_header(ws, template_header)
    return ws, template_header


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
        sh = get_spreadsheet()
        template_ws = get_worksheet_by_name(WORKSHEET_NAME)
        template_header = get_header(template_ws)

        return jsonify({
            "ok": True,
            "spreadsheet_id": SPREADSHEET_ID,
            "template_worksheet": template_ws.title,
            "template_header_count": len(template_header),
            "worksheets": [ws.title for ws in sh.worksheets()],
        }), 200
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 500


@app.route("/save_result", methods=["POST"])
def save_result():
    try:
        data = request.get_json(force=True, silent=True) or {}

        if not isinstance(data, dict):
            return jsonify({
                "ok": False,
                "error": "Invalid JSON payload",
            }), 400

        ws, header = ensure_target_worksheet_with_header(data)
        row = build_row_from_existing_header(data, header)
        ws.append_row(row, value_input_option="RAW")

        return jsonify({
            "ok": True,
            "message": "saved",
            "worksheet": ws.title,
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
