import os
import json
import traceback
from datetime import datetime

from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials


app = Flask(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "results").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

_WS = None
_HEADER = None


def safe_str(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def get_gspread_client():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet():
    global _WS

    if _WS is not None:
        return _WS

    if not SPREADSHEET_ID:
        raise RuntimeError("Missing SPREADSHEET_ID")

    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    try:
        _WS = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        _WS = sh.add_worksheet(title=WORKSHEET_NAME, rows=2000, cols=120)

    return _WS


def get_header(ws):
    global _HEADER

    if _HEADER is not None:
        return _HEADER

    first_row = ws.row_values(1)
    _HEADER = [str(h).strip() for h in first_row if str(h).strip()]
    return _HEADER


def normalize_score_text(value):
    s = safe_str(value).strip()
    if not s:
        return ""
    if not s.startswith("'"):
        s = "'" + s
    return s


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


def build_row_from_existing_header(data, header):
    row = []

    for col in header:
        value = alias_value(data, col)

        if col == "最終比分":
            row.append(normalize_score_text(value))
        else:
            row.append(safe_str(value))

    return row


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
        ws = get_worksheet()
        header = get_header(ws)
        return jsonify({
            "ok": True,
            "worksheet": ws.title,
            "header_count": len(header),
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

        ws = get_worksheet()
        header = get_header(ws)

        if not header:
            return jsonify({
                "ok": False,
                "error": "results 第1列沒有表頭，先把 2026 分頁第1列複製到 results 第1列"
            }), 400

        row = build_row_from_existing_header(data, header)
        ws.append_row(row, value_input_option="RAW")

        return jsonify({
            "ok": True,
            "message": "saved",
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
