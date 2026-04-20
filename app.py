from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gspread
import pandas as pd
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
    "version": "V5-clean+results-api+nba-rule-picks",
    "default_mode": DEFAULT_MODE if DEFAULT_MODE in FILE_MAP else "regular",
    "weekly_target": int(os.getenv("WEEKLY_TARGET", "10")),
    "weekly_range_min": int(os.getenv("WEEKLY_RANGE_MIN", "8")),
    "weekly_range_max": int(os.getenv("WEEKLY_RANGE_MAX", "12")),
    "risk_controls": {
        "extreme_total_guard": False,
        "rest_diff_guard": False,
        "volatility_guard": False,
        "line_move_guard": False,
    },
    "results_sheet_enabled": bool(GOOGLE_SHEET_ID),
    "results_worksheet": RESULTS_WORKSHEET,
}

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))


# ----------------------------
# basic utils
# ----------------------------

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


def _rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    return df.fillna("").to_dict(orient="records")


def _clean_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    return str(v)


def _clean_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    return str(v).strip()


def _safe_num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _clean_text(v).replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _round1(v: Optional[float]) -> str:
    if v is None:
        return ""
    return f"{round(float(v), 1):.1f}"


def _parse_away_home_score(score_text: str) -> Tuple[Optional[float], Optional[float]]:
    """
    你的回填腳本最終比分格式是 away-home，例如 102-99
    """
    s = _clean_text(score_text).replace(":", "-")
    if not s or "-" not in s:
        return None, None
    parts = s.split("-")
    if len(parts) != 2:
        return None, None
    try:
        away = float(parts[0].strip())
        home = float(parts[1].strip())
        return away, home
    except Exception:
        return None, None


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
        ws = sh.add_worksheet(title=RESULTS_WORKSHEET, rows=1000, cols=100)
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


# ----------------------------
# NBA recommendation rules
# ----------------------------

def _nba_net_info(row: Dict[str, Any]) -> Dict[str, Any]:
    away_net = _safe_num(row.get("客隊近10場平均淨值"))
    home_net = _safe_num(row.get("主隊近10場平均淨值"))
    if away_net is None or home_net is None:
        return {"high_side": "", "diff": None}

    diff = abs(home_net - away_net)
    if home_net > away_net:
        return {"high_side": "home", "diff": diff}
    if away_net > home_net:
        return {"high_side": "away", "diff": diff}
    return {"high_side": "", "diff": 0.0}


def _nba_total_avg(row: Dict[str, Any]) -> Optional[float]:
    away_pf = _safe_num(row.get("客隊近10場平均得分"))
    away_pa = _safe_num(row.get("客隊近10場平均失分"))
    home_pf = _safe_num(row.get("主隊近10場平均得分"))
    home_pa = _safe_num(row.get("主隊近10場平均失分"))

    if None in (away_pf, away_pa, home_pf, home_pa):
        return None

    return (away_pf + away_pa + home_pf + home_pa) / 2.0


def _market_side_from_text(spread_side_text: str) -> Tuple[str, Optional[float]]:
    """
    回傳 favorite_side(home/away/"") 和 spread 數字
    例：
      主讓4.5 -> ("home", 4.5)
      客讓5.5 -> ("away", 5.5)
    """
    s = _clean_text(spread_side_text)
    spread_num = _safe_num(re.sub(r"[^\d.\-]", "", s))

    if s.startswith("主讓"):
        return "home", spread_num
    if s.startswith("客讓"):
        return "away", spread_num
    return "", spread_num


def _team_pick_text(team_side: str, home_team: str, away_team: str, spread_side_text: str) -> str:
    favorite_side, _ = _market_side_from_text(spread_side_text)

    if team_side == "home":
        team_name = home_team
        if favorite_side == "home":
            return f"{team_name} 讓分"
        return f"{team_name} 受讓"

    if team_side == "away":
        team_name = away_team
        if favorite_side == "away":
            return f"{team_name} 讓分"
        return f"{team_name} 受讓"

    return ""


def _judge_spread_result(row: Dict[str, Any], pick: str) -> str:
    score_text = _clean_text(row.get("最終比分"))
    away_score, home_score = _parse_away_home_score(score_text)
    if away_score is None or home_score is None:
        return ""

    spread_side_text = _clean_text(row.get("主客讓分盤"))
    favorite_side, spread = _market_side_from_text(spread_side_text)
    if spread is None:
        return ""

    home_team = _clean_text(row.get("主隊"))
    away_team = _clean_text(row.get("客隊"))

    if pick == f"{home_team} 讓分":
        value = (home_score - spread) - away_score
    elif pick == f"{home_team} 受讓":
        value = (home_score + spread) - away_score
    elif pick == f"{away_team} 讓分":
        value = (away_score - spread) - home_score
    elif pick == f"{away_team} 受讓":
        value = (away_score + spread) - home_score
    else:
        return ""

    if value > 0:
        return "WIN"
    if value < 0:
        return "LOSE"
    return "PUSH"


def _judge_total_result(row: Dict[str, Any], pick: str) -> str:
    score_text = _clean_text(row.get("最終比分"))
    away_score, home_score = _parse_away_home_score(score_text)
    if away_score is None or home_score is None:
        return ""

    total_line = _safe_num(row.get("大小分盤"))
    if total_line is None:
        return ""

    total_score = away_score + home_score
    if pick == "大分":
        if total_score > total_line:
            return "WIN"
        if total_score < total_line:
            return "LOSE"
        return "PUSH"

    if pick == "小分":
        if total_score < total_line:
            return "WIN"
        if total_score > total_line:
            return "LOSE"
        return "PUSH"

    return ""


def _nba_spread_pick(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    home_team = _clean_text(row.get("主隊"))
    away_team = _clean_text(row.get("客隊"))
    spread_side_text = _clean_text(row.get("主客讓分盤"))
    spread_line = _clean_text(row.get("讓分盤"))

    if not home_team or not away_team or not spread_side_text:
        return None

    info = _nba_net_info(row)
    high_side = info["high_side"]
    diff = info["diff"]

    if diff is None or diff <= 0:
        return None

    target_side = ""
    reason = ""

    # 你最後定下來的規則
    # 高淨值在主隊：0.1–10+ 打主隊
    # 高淨值在客隊：1–5 打客隊；>5–10+ 打主隊
    if high_side == "home":
        target_side = "home"
        reason = f"主高淨值｜淨值差{_round1(diff)}"
    elif high_side == "away":
        if 1 <= diff <= 5:
            target_side = "away"
            reason = f"客高淨值｜淨值差{_round1(diff)}｜1-5打客"
        elif diff > 5:
            target_side = "home"
            reason = f"客高淨值｜淨值差{_round1(diff)}｜>5打主"

    if not target_side:
        return None

    pick = _team_pick_text(target_side, home_team, away_team, spread_side_text)
    if not pick:
        return None

    result = _judge_spread_result(row, pick)

    return {
        "league": "NBA",
        "date": _clean_text(row.get("比賽日期")),
        "pick": pick,
        "result": result,
        "market": f"讓分｜{spread_side_text}",
        "edge_abs": _round1(diff),
        "season": _clean_text(row.get("賽季")),
        "game_id": _clean_text(row.get("比賽ID")),
        "reason": reason,
    }


def _nba_total_pick(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    total_avg = _nba_total_avg(row)
    total_line = _safe_num(row.get("大小分盤"))
    if total_avg is None:
        return None

    # 你定下來的區間
    # 205–210：大分
    # 210–215：大分
    # 215–220：小分
    # 220–225：大分
    # 225–230：大分
    # 230–235：大分
    # 235–240：小分
    # 240–245：大分
    # 245–250：小分
    # 分界值算下一區：lower inclusive, upper exclusive
    pick = ""
    if 205 <= total_avg < 210:
        pick = "大分"
    elif 210 <= total_avg < 215:
        pick = "大分"
    elif 215 <= total_avg < 220:
        pick = "小分"
    elif 220 <= total_avg < 225:
        pick = "大分"
    elif 225 <= total_avg < 230:
        pick = "大分"
    elif 230 <= total_avg < 235:
        pick = "大分"
    elif 235 <= total_avg < 240:
        pick = "小分"
    elif 240 <= total_avg < 245:
        pick = "大分"
    elif 245 <= total_avg < 250:
        pick = "小分"

    if not pick:
        return None

    result = _judge_total_result(row, pick)
    edge_abs = None
    if total_line is not None:
        edge_abs = abs(total_avg - total_line)

    return {
        "league": "NBA",
        "date": _clean_text(row.get("比賽日期")),
        "pick": pick,
        "result": result,
        "market": f"大小｜{_clean_text(row.get('大小分盤'))}",
        "edge_abs": _round1(edge_abs),
        "season": _clean_text(row.get("賽季")),
        "game_id": _clean_text(row.get("比賽ID")),
        "reason": f"兩隊總和/2={_round1(total_avg)}",
    }


def _sort_pick_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def _key(r: Dict[str, Any]):
        date_text = _clean_text(r.get("date"))
        try:
            dt = datetime.strptime(date_text, "%Y-%m-%d")
        except Exception:
            dt = datetime(1900, 1, 1)
        # 未結算優先、日期新到舊
        unresolved_first = 0 if not _clean_text(r.get("result")) else 1
        return (unresolved_first, -int(dt.timestamp()), _clean_text(r.get("game_id")))

    return sorted(rows, key=_key)


def _build_nba_picks_from_results(df: pd.DataFrame, limit: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    if "聯盟" in df.columns:
        df = df[df["聯盟"].astype(str).str.upper() == "NBA"]

    if df.empty:
        return pd.DataFrame()

    rows = df.fillna("").to_dict(orient="records")
    out: List[Dict[str, Any]] = []

    for row in rows:
        spread_pick = _nba_spread_pick(row)
        if spread_pick:
            out.append(spread_pick)

        total_pick = _nba_total_pick(row)
        if total_pick:
            out.append(total_pick)

    out = _sort_pick_rows(out)
    if limit > 0:
        out = out[:limit]

    return pd.DataFrame(out)


# ----------------------------
# routes
# ----------------------------

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
    league = request.args.get("league", "").strip().upper()
    limit = int(request.args.get("limit", "100"))

    # NBA 直接讀 results sheet 動態算推薦
    if league == "NBA" and GOOGLE_SHEET_ID:
        try:
            ws = _open_results_ws()
            df = _worksheet_as_df(ws)
            pick_df = _build_nba_picks_from_results(df, limit)
            return jsonify({"ok": True, "mode": mode, "rows": _rows(pick_df)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # 其他聯盟或沒設 sheet 時，維持原本 CSV 流程
    df = _read_csv("picks", mode)
    if league and not df.empty and "league" in df.columns:
        df = df[df["league"].astype(str).str.upper() == league]
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
