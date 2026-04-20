from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import gspread
from flask import Flask, jsonify, render_template, request
from google.oauth2.service_account import Credentials

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "outputs"))
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "regular").strip().lower() or "regular"
PORT = int(os.getenv("PORT", "10000"))

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
RESULTS_WORKSHEET = os.getenv("RESULTS_WORKSHEET", "results").strip()

FILE_MAP = {
    "regular": {
        "summary": "summary_regular.csv",
        "weekly": "weekly_regular.csv",
        "picks": "picks_regular.csv",
    },
    "all": {
        "summary": "summary_regular_plus_playoffs.csv",
        "weekly": "weekly_regular_plus_playoffs.csv",
        "picks": "picks_regular_plus_playoffs.csv",
    },
}

CONFIG = {
    "version": "V5-clean+results-api",
    "default_mode": DEFAULT_MODE if DEFAULT_MODE in FILE_MAP else "regular",
    "weekly_target": int(os.getenv("WEEKLY_TARGET", "10")),
    "weekly_range_min": int(os.getenv("WEEKLY_RANGE_MIN", "8")),
    "weekly_range_max": int(os.getenv("WEEKLY_RANGE_MAX", "12")),
    "risk_controls": {
        "extreme_total_guard": True,
        "rest_diff_guard": True,
        "volatility_guard": True,
        "line_move_guard": True,
    },
    "results_sheet_enabled": bool(GOOGLE_SHEET_ID),
    "results_worksheet": RESULTS_WORKSHEET,
}

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))


def _mode() -> str:
    mode = request.args.get("mode", CONFIG["default_mode"]).strip().lower()
    return mode if mode in FILE_MAP else CONFIG["default_mode"]


def _read_csv(kind: str, mode: str) -> pd.DataFrame:
    path = OUTPUT_DIR / FILE_MAP[mode][kind]
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _rows(df: pd.DataFrame):
    if df.empty:
        return []
    return df.fillna("").to_dict(orient="records")


def _clean_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    return str(v)


def _sheet_scopes() -> List[str]:
    return [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]


def _get_google_credentials() -> Credentials:
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    json_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()

    if raw_json:
        info = json.loads(raw_json)
        return Credentials.from_service_account_info(info, scopes=_sheet_scopes())

    if json_path:
        return Credentials.from_service_account_file(json_path, scopes=_sheet_scopes())

    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")


def _get_gspread_client():
    creds = _get_google_credentials()
    return gspread.authorize(creds)


def _open_results_ws():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    gc = _get_gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(RESULTS_WORKSHEET)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=RESULTS_WORKSHEET, rows=1000, cols=50)
    return ws


def _get_headers(ws) -> List[str]:
    headers = ws.row_values(1)
    return [h.strip() for h in headers if str(h).strip()]


def _ensure_headers(ws, payload: Dict[str, Any]) -> List[str]:
    existing_headers = _get_headers(ws)
    incoming_keys = list(payload.keys())

    if not existing_headers:
        ws.update("A1", [incoming_keys])
        return incoming_keys

    missing = [k for k in incoming_keys if k not in existing_headers]
    if missing:
        new_headers = existing_headers + missing
        ws.update("A1", [new_headers])
        return new_headers

    return existing_headers


def _worksheet_as_df(ws) -> pd.DataFrame:
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame()

    headers = values[0]
    rows = values[1:] if len(values) > 1 else []
    if not headers:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame(columns=headers)

    norm_rows = []
    width = len(headers)
    for r in rows:
        rr = list(r[:width]) + [""] * max(0, width - len(r))
        norm_rows.append(rr)

    return pd.DataFrame(norm_rows, columns=headers)


def _find_row_by_game_id(df: pd.DataFrame, game_id: str) -> Optional[int]:
    if df.empty or "比賽ID" not in df.columns:
        return None

    matches = df.index[df["比賽ID"].astype(str) == str(game_id)].tolist()
    if not matches:
        return None

    # worksheet row index = dataframe row index + 2
    return int(matches[0]) + 2


def _row_from_payload(headers: List[str], payload: Dict[str, Any]) -> List[str]:
    return [_clean_cell(payload.get(h, "")) for h in headers]


@app.route("/")
def index():
    return render_template("index.html", config=CONFIG)


@app.route("/api")
def api_root():
    files = {m: {k: str((OUTPUT_DIR / v).exists()) for k, v in mp.items()} for m, mp in FILE_MAP.items()}
    return jsonify({
        "ok": True,
        "version": CONFIG["version"],
        "output_dir": str(OUTPUT_DIR),
        "files": files,
        "results_sheet_enabled": CONFIG["results_sheet_enabled"],
        "results_worksheet": CONFIG["results_worksheet"],
    })


@app.route("/health")
def health():
    files = {m: {k: (OUTPUT_DIR / v).exists() for k, v in mp.items()} for m, mp in FILE_MAP.items()}
    return jsonify({
        "ok": True,
        "version": CONFIG["version"],
        "output_dir": str(OUTPUT_DIR),
        "files": files,
        "results_sheet_enabled": CONFIG["results_sheet_enabled"],
        "results_worksheet": CONFIG["results_worksheet"],
    })


@app.route("/config")
def config():
    return jsonify({"ok": True, **CONFIG})


@app.route("/summary")
def summary():
    mode = _mode()
    df = _read_csv("summary", mode)
    return jsonify({"ok": True, "mode": mode, "rows": _rows(df)})


@app.route("/weekly")
def weekly():
    mode = _mode()
    df = _read_csv("weekly", mode)
    limit = int(request.args.get("limit", "200"))
    if limit > 0 and not df.empty:
        df = df.head(limit)
    return jsonify({"ok": True, "mode": mode, "rows": _rows(df)})


@app.route("/picks")
def picks():
    mode = _mode()
    df = _read_csv("picks", mode)
    league = request.args.get("league", "").strip().upper()
    if league and not df.empty and "league" in df.columns:
        df = df[df["league"].astype(str).str.upper() == league]
    limit = int(request.args.get("limit", "100"))
    if limit > 0 and not df.empty:
        df = df.head(limit)
    return jsonify({"ok": True, "mode": mode, "rows": _rows(df)})


@app.route("/results", methods=["GET"])
def results():
    try:
        ws = _open_results_ws()
        df = _worksheet_as_df(ws)

        league = request.args.get("league", "").strip().upper()
        season = request.args.get("season", "").strip()
        limit = int(request.args.get("limit", "5000"))

        if not df.empty:
            if league and "聯盟" in df.columns:
                df = df[df["聯盟"].astype(str).str.upper() == league]
            if season and "賽季" in df.columns:
                df = df[df["賽季"].astype(str) == season]
            if limit > 0:
                df = df.head(limit)

        return jsonify({
            "ok": True,
            "rows": _rows(df),
            "count": 0 if df.empty else len(df),
            "worksheet": RESULTS_WORKSHEET,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/save_result", methods=["POST"])
def save_result():
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

        game_id = str(payload.get("比賽ID", "")).strip()
        if not game_id:
            return jsonify({"ok": False, "error": "缺少 比賽ID"}), 400

        ws = _open_results_ws()
        headers = _ensure_headers(ws, payload)
        df = _worksheet_as_df(ws)
        target_row = _find_row_by_game_id(df, game_id)
        row_values = _row_from_payload(headers, payload)

        if target_row is None:
            ws.append_row(row_values, value_input_option="USER_ENTERED")
            action = "inserted"
        else:
            last_col = gspread.utils.rowcol_to_a1(1, len(headers)).rstrip("1")
            ws.update(f"A{target_row}:{last_col}{target_row}", [row_values])
            action = "updated"

        return jsonify({
            "ok": True,
            "action": action,
            "比賽ID": game_id,
            "worksheet": RESULTS_WORKSHEET,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
