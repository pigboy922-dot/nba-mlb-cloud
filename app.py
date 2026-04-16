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

# 快取，避免每次 /save_result 都重抓 worksheet / header
_WS = None
_HEADER_READY = False


def safe_str(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def pick_first(data, *keys, default=""):
    for key in keys:
        if key in data and data.get(key) is not None:
            return data.get(key)
    return default


def get_gspread_client():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")

    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    except Exception as e:
        raise RuntimeError(f"Invalid GOOGLE_SERVICE_ACCOUNT_JSON: {e}")

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
        _WS = sh.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=40)

    return _WS


def ensure_header(ws):
    global _HEADER_READY

    if _HEADER_READY:
        return

    header = [
        "比賽ID",
        "聯盟",
        "賽季",
        "比賽日期",
        "對戰",
        "主隊",
        "客隊",
        "最終比分",
        "判定時間",
        "更新時間",
        "讓分盤",
        "大小分盤",
        "EDGE讓分值",
        "EDGE大小值",
        "近10讓分推薦",
        "近10大小_淨值推薦",
        "近10大小_相加推薦",
        "主客讓分推薦",
        "主客大小_淨值推薦",
        "主客大小_相加推薦",
        "EDGE讓分推薦",
        "EDGE大小推薦",
        "近10讓分結果",
        "近10大小_淨值結果",
        "近10大小_相加結果",
        "主客讓分結果",
        "主客大小_淨值結果",
        "主客大小_相加結果",
        "EDGE讓分結果",
        "EDGE大小結果",
        "raw_payload",
        "saved_at_utc",
    ]

    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(header, value_input_option="USER_ENTERED")

    _HEADER_READY = True


def build_row_from_payload(data):
    row = [
        safe_str(pick_first(data, "比賽ID", "game_id", "event_id", "id")),
        safe_str(pick_first(data, "聯盟", "league")),
        safe_str(pick_first(data, "賽季", "season")),
        safe_str(pick_first(data, "比賽日期", "game_date", "date")),
        safe_str(pick_first(data, "對戰", "matchup")),
        safe_str(pick_first(data, "主隊", "home_team", "home")),
        safe_str(pick_first(data, "客隊", "away_team", "away")),
        safe_str(pick_first(data, "最終比分", "final_score", "score")),
        safe_str(pick_first(data, "判定時間", "judged_at")),
        safe_str(pick_first(data, "更新時間", "updated_at")),
        safe_str(pick_first(data, "讓分盤", "spread_line")),
        safe_str(pick_first(data, "大小分盤", "total_line")),
        safe_str(pick_first(data, "EDGE讓分值", "edge_spread_value")),
        safe_str(pick_first(data, "EDGE大小值", "edge_total_value")),
        safe_str(pick_first(data, "近10讓分推薦")),
        safe_str(pick_first(data, "近10大小_淨值推薦")),
        safe_str(pick_first(data, "近10大小_相加推薦")),
        safe_str(pick_first(data, "主客讓分推薦")),
        safe_str(pick_first(data, "主客大小_淨值推薦")),
        safe_str(pick_first(data, "主客大小_相加推薦")),
        safe_str(pick_first(data, "EDGE讓分推薦")),
        safe_str(pick_first(data, "EDGE大小推薦")),
        safe_str(pick_first(data, "近10讓分結果")),
        safe_str(pick_first(data, "近10大小_淨值結果")),
        safe_str(pick_first(data, "近10大小_相加結果")),
        safe_str(pick_first(data, "主客讓分結果")),
        safe_str(pick_first(data, "主客大小_淨值結果")),
        safe_str(pick_first(data, "主客大小_相加結果")),
        safe_str(pick_first(data, "EDGE讓分結果")),
        safe_str(pick_first(data, "EDGE大小結果")),
        safe_str(data),
        datetime.utcnow().isoformat(),
    ]
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
        ensure_header(ws)
        return jsonify({
            "ok": True,
            "worksheet": ws.title,
        }), 200
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 500


@app.route("/debug_sheet", methods=["GET"])
def debug_sheet():
    try:
        ws = get_worksheet()
        return jsonify({
            "ok": True,
            "spreadsheet_id": SPREADSHEET_ID,
            "worksheet_name_env": WORKSHEET_NAME,
            "worksheet_title_actual": ws.title,
            "row_count": ws.row_count,
            "col_count": ws.col_count,
        }), 200
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
            "spreadsheet_id": SPREADSHEET_ID,
            "worksheet_name_env": WORKSHEET_NAME,
        }), 500


@app.route("/debug_peek", methods=["GET"])
def debug_peek():
    try:
        ws = get_worksheet()
        values = ws.get("A1:AF10")
        return jsonify({
            "ok": True,
            "worksheet_title_actual": ws.title,
            "preview": values,
        }), 200
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 500


@app.route("/debug_write", methods=["GET"])
def debug_write():
    try:
        ws = get_worksheet()
        ensure_header(ws)

        row = [
            "DEBUG_TEST_ID",
            "DEBUG",
            "2026",
            "2026-04-16",
            "debug vs debug",
            "主隊",
            "客隊",
            "1:0",
            "2026-04-16T00:00:00+08:00",
            "2026-04-16T00:00:00+08:00",
            "",
            "",
            "",
            "",
            "PASS",
            "PASS",
            "PASS",
            "PASS",
            "PASS",
            "PASS",
            "PASS",
            "PASS",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            '{"debug": true}',
            datetime.utcnow().isoformat(),
        ]

        ws.append_row(row, value_input_option="USER_ENTERED")

        return jsonify({
            "ok": True,
            "worksheet": ws.title,
            "spreadsheet_id": SPREADSHEET_ID,
            "message": "debug row written",
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
        print("SAVE_RESULT incoming =", json.dumps(data, ensure_ascii=False))

        if not isinstance(data, dict):
            return jsonify({
                "ok": False,
                "error": "Invalid JSON payload",
            }), 400

        ws = get_worksheet()
        ensure_header(ws)

        row = build_row_from_payload(data)
        print("SAVE_RESULT row =", json.dumps(row, ensure_ascii=False))

        ws.append_row(row, value_input_option="USER_ENTERED")

        return jsonify({
            "ok": True,
            "message": "saved",
        }), 200

    except Exception as e:
        print("SAVE_RESULT ERROR =", str(e))
        traceback.print_exc()

        return jsonify({
            "ok": False,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 500


@app.route("/scoreboard", methods=["GET"])
def scoreboard():
    return jsonify({
        "ok": False,
        "error": "scoreboard route not implemented in this build"
    }), 404


@app.route("/proxy/scoreboard", methods=["GET"])
def proxy_scoreboard():
    return jsonify({
        "ok": False,
        "error": "proxy scoreboard route not implemented in this build"
    }), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
