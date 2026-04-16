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
    if not SPREADSHEET_ID:
        raise RuntimeError("Missing SPREADSHEET_ID")

    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=30)
        ensure_header(ws)

    return ws


def ensure_header(ws):
    values = ws.get_all_values()
    if values:
        return

    header = [
        "game_id",
        "league",
        "sport",
        "game_date",
        "commence_time",
        "home_team",
        "away_team",
        "pick",
        "result",
        "status",
        "odds",
        "bookmaker",
        "market",
        "model_name",
        "notes",
        "raw_payload",
        "saved_at_utc",
    ]
    ws.append_row(header, value_input_option="USER_ENTERED")


def build_row_from_payload(data):
    row = [
        safe_str(pick_first(data, "game_id", "event_id", "id")),
        safe_str(pick_first(data, "league")),
        safe_str(pick_first(data, "sport")),
        safe_str(pick_first(data, "game_date", "date")),
        safe_str(pick_first(data, "commence_time", "start_time")),
        safe_str(pick_first(data, "home_team", "home")),
        safe_str(pick_first(data, "away_team", "away")),
        safe_str(pick_first(data, "pick", "prediction", "recommended_pick")),
        safe_str(pick_first(data, "result", "outcome")),
        safe_str(pick_first(data, "status")),
        safe_str(pick_first(data, "odds", "price")),
        safe_str(pick_first(data, "bookmaker", "book")),
        safe_str(pick_first(data, "market")),
        safe_str(pick_first(data, "model_name")),
        safe_str(pick_first(data, "notes", "remark")),
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


# 你原本如果有 scoreboard 或 proxy 路由，可保留原版
# 下面只是避免 history script 打到不存在時完全沒訊息

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
