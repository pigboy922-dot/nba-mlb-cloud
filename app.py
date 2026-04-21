import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request, g
from flask_cors import CORS

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

APP_TZ = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "results.db"))
ESPN_TIMEOUT = float(os.environ.get("ESPN_TIMEOUT", "15"))
PORT = int(os.environ.get("PORT", "8000"))
DEBUG = os.environ.get("DEBUG", "0") == "1"
MAX_RESULTS_DEFAULT = int(os.environ.get("MAX_RESULTS_DEFAULT", "5000"))

# Google Sheets mirror.
# Support both the new names and a few legacy aliases.
GOOGLE_SHEETS_SPREADSHEET_ID = (
    os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    or os.environ.get("GOOGLE_SHEET_ID", "").strip()
    or os.environ.get("SPREADSHEET_ID", "").strip()
)
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEET_NAME = (
    os.environ.get("GOOGLE_SHEET_NAME", "").strip()
    or os.environ.get("WORKSHEET_NAME", "").strip()
    or os.environ.get("RESULTS_WORKSHEET", "").strip()
    or "results"
)

PREFERRED_COLUMNS = [
    "聯盟", "賽季", "比賽日期", "對戰", "主隊", "客隊", "最終比分",
    "讓分盤", "主客讓分盤", "大小分盤",
    "客隊近10場平均得分", "客隊近10場平均失分", "客隊近10場平均淨值",
    "主隊近10場平均得分", "主隊近10場平均失分", "主隊近10場平均淨值",
    "客隊近10得分-失分", "主隊近10得分-失分",
    "主客節奏差", "主客淨值差", "更新時間",
    "規則1讓分推薦", "規則1讓分結果",
    "規則2讓分推薦", "規則2讓分結果",
    "規則3大小推薦", "規則3大小結果",
    "規則4大小推薦", "規則4大小結果",
    "比賽ID",
]

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
CORS(app)


def utc_now_iso() -> str:
    return datetime.now(APP_TZ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_int_str(value: Any) -> str:
    return normalize_str(value)


def get_db() -> sqlite3.Connection:
    conn = getattr(g, "db", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        g.db = conn
    return conn


@app.teardown_appcontext
def close_db(exc: Optional[BaseException]) -> None:
    conn = getattr(g, "db", None)
    if conn is not None:
        conn.close()
        try:
            delattr(g, "db")
        except Exception:
            pass


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league TEXT NOT NULL,
                season TEXT NOT NULL,
                game_date TEXT NOT NULL,
                game_id TEXT NOT NULL,
                matchup TEXT DEFAULT '',
                home_team TEXT DEFAULT '',
                away_team TEXT DEFAULT '',
                updated_time TEXT DEFAULT '',
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(league, season, game_date, game_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_results_lookup ON results (league, season, game_date DESC, updated_at DESC)"
        )
        conn.commit()


def canonicalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload or {})
    league = normalize_str(data.get("聯盟"))
    season = normalize_int_str(data.get("賽季"))
    game_date = normalize_str(data.get("比賽日期"))
    game_id = normalize_str(data.get("比賽ID"))

    if not league:
        raise ValueError("缺少 聯盟")
    if not season:
        raise ValueError("缺少 賽季")
    if not game_date:
        raise ValueError("缺少 比賽日期")

    if not game_id:
        matchup = normalize_str(data.get("對戰"))
        home = normalize_str(data.get("主隊"))
        away = normalize_str(data.get("客隊"))
        fallback = matchup or (away + "_vs_" + home)
        game_id = f"{league}_{season}_{game_date}_{fallback}"
        data["比賽ID"] = game_id

    if not data.get("更新時間"):
        data["更新時間"] = utc_now_iso()

    return data


def upsert_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = canonicalize_payload(payload)
    db = get_db()
    now = utc_now_iso()

    league = normalize_str(data["聯盟"])
    season = normalize_int_str(data["賽季"])
    game_date = normalize_str(data["比賽日期"])
    game_id = normalize_str(data["比賽ID"])
    matchup = normalize_str(data.get("對戰"))
    home_team = normalize_str(data.get("主隊"))
    away_team = normalize_str(data.get("客隊"))
    updated_time = normalize_str(data.get("更新時間"))
    raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    db.execute(
        """
        INSERT INTO results (
            league, season, game_date, game_id, matchup, home_team, away_team,
            updated_time, raw_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(league, season, game_date, game_id)
        DO UPDATE SET
            matchup=excluded.matchup,
            home_team=excluded.home_team,
            away_team=excluded.away_team,
            updated_time=excluded.updated_time,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        (
            league, season, game_date, game_id, matchup, home_team, away_team,
            updated_time, raw_json, now, now,
        ),
    )
    db.commit()

    mirror_info = maybe_mirror_to_sheet(data)
    return {
        "ok": True,
        "league": league,
        "season": season,
        "game_date": game_date,
        "game_id": game_id,
        "sheet": mirror_info,
    }


def row_to_payload(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        payload = json.loads(row["raw_json"])
    except Exception:
        payload = {}

    payload.setdefault("聯盟", row["league"])
    payload.setdefault("賽季", row["season"])
    payload.setdefault("比賽日期", row["game_date"])
    payload.setdefault("比賽ID", row["game_id"])
    return payload


def query_results(
    league: Optional[str],
    season: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    db = get_db()
    clauses: List[str] = []
    params: List[Any] = []

    if league:
        clauses.append("league = ?")
        params.append(league)
    if season:
        clauses.append("season = ?")
        params.append(season)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT *
        FROM results
        {where_sql}
        ORDER BY game_date DESC, updated_at DESC, id DESC
        LIMIT ?
    """
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    return [row_to_payload(row) for row in rows]


def compute_stats_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return rows


def get_sheet_client():
    if not GOOGLE_SHEETS_SPREADSHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    if gspread is None or Credentials is None:
        return None

    client = getattr(g, "sheet_client", None)
    if client is not None:
        return client

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    g.sheet_client = client
    return client


def get_or_create_worksheet(spreadsheet, title: str):
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        ws = spreadsheet.add_worksheet(
            title=title,
            rows=2000,
            cols=max(26, len(PREFERRED_COLUMNS) + 10),
        )
        ws.append_row(PREFERRED_COLUMNS, value_input_option="USER_ENTERED")
        return ws


def build_sheet_headers(existing_headers: List[str], payload: Dict[str, Any]) -> List[str]:
    headers = [h for h in existing_headers if normalize_str(h)]
    if not headers:
        headers = list(PREFERRED_COLUMNS)

    for key in payload.keys():
        if key not in headers:
            headers.append(key)
    return headers


def col_to_a1(col_num: int) -> str:
    result = ""
    while col_num > 0:
        col_num, rem = divmod(col_num - 1, 26)
        result = chr(65 + rem) + result
    return result


def worksheet_records_index(ws) -> Tuple[List[str], Dict[str, int]]:
    values = ws.get_all_values()
    if not values:
        ws.append_row(PREFERRED_COLUMNS, value_input_option="USER_ENTERED")
        values = ws.get_all_values()

    headers = values[0] if values else list(PREFERRED_COLUMNS)
    index: Dict[str, int] = {}

    try:
        gid_idx = headers.index("比賽ID")
        league_idx = headers.index("聯盟")
        season_idx = headers.index("賽季")
        date_idx = headers.index("比賽日期")
    except ValueError:
        new_headers = build_sheet_headers(headers, {h: "" for h in PREFERRED_COLUMNS})
        end_col = col_to_a1(len(new_headers))
        ws.update(f"A1:{end_col}1", [new_headers])
        values = ws.get_all_values()
        headers = values[0]
        gid_idx = headers.index("比賽ID")
        league_idx = headers.index("聯盟")
        season_idx = headers.index("賽季")
        date_idx = headers.index("比賽日期")

    for row_num, row in enumerate(values[1:], start=2):
        def cell(i: int) -> str:
            return row[i] if i < len(row) else ""

        key = "|".join([
            normalize_str(cell(league_idx)),
            normalize_str(cell(season_idx)),
            normalize_str(cell(date_idx)),
            normalize_str(cell(gid_idx)),
        ])
        if key.strip("|"):
            index[key] = row_num

    return headers, index


def maybe_mirror_to_sheet(payload: Dict[str, Any]) -> Dict[str, Any]:
    client = get_sheet_client()
    if client is None:
        return {
            "enabled": False,
            "reason": "google_sheets_not_configured_or_dependencies_missing",
        }

    try:
        ss = client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        ws = get_or_create_worksheet(ss, GOOGLE_SHEET_NAME)
        headers, row_index = worksheet_records_index(ws)

        new_headers = build_sheet_headers(headers, payload)
        if new_headers != headers:
            end_col = col_to_a1(len(new_headers))
            ws.update(f"A1:{end_col}1", [new_headers])
            headers = new_headers
            _, row_index = worksheet_records_index(ws)

        row_values = [payload.get(h, "") for h in headers]
        key = "|".join([
            normalize_str(payload.get("聯盟")),
            normalize_str(payload.get("賽季")),
            normalize_str(payload.get("比賽日期")),
            normalize_str(payload.get("比賽ID")),
        ])

        if key in row_index:
            row_num = row_index[key]
            end_col = col_to_a1(len(headers))
            ws.update(f"A{row_num}:{end_col}{row_num}", [row_values])
            return {
                "enabled": True,
                "worksheet": GOOGLE_SHEET_NAME,
                "action": "update",
                "row": row_num,
            }

        ws.append_row(row_values, value_input_option="USER_ENTERED")
        return {
            "enabled": True,
            "worksheet": GOOGLE_SHEET_NAME,
            "action": "append",
        }
    except Exception as exc:
        return {
            "enabled": True,
            "error": str(exc),
        }


def proxy_scoreboard(league: str, sport: str, dates: str):
    if not league or not sport or not dates:
        return jsonify({"ok": False, "error": "league, sport, dates are required"}), 400

    url = ESPN_SCOREBOARD_URL.format(sport=sport, league=league)
    resp = requests.get(
        url,
        params={"dates": dates},
        timeout=ESPN_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    return jsonify(resp.json())


@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "service": "nba-mlb-cloud",
        "time": utc_now_iso(),
        "endpoints": [
            "/health",
            "/save_result",
            "/results",
            "/stats",
            "/proxy/scoreboard",
            "/scoreboard",
        ],
        "google_sheet_enabled": bool(GOOGLE_SHEETS_SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON),
        "google_sheet_name": GOOGLE_SHEET_NAME,
    })


@app.get("/health")
def health():
    db = get_db()
    db.execute("SELECT 1")
    return jsonify({"ok": True, "time": utc_now_iso()})


@app.post("/save_result")
def save_result():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400

    try:
        result = upsert_result(payload)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/results")
def results():
    league = normalize_str(request.args.get("league")) or None
    season = normalize_str(request.args.get("season")) or None

    try:
        limit_num = max(1, min(int(request.args.get("limit", MAX_RESULTS_DEFAULT)), 20000))
    except Exception:
        limit_num = MAX_RESULTS_DEFAULT

    rows = query_results(league, season, limit_num)
    return jsonify({"ok": True, "count": len(rows), "rows": rows})


@app.get("/stats")
def stats():
    league = normalize_str(request.args.get("league")) or None
    season = normalize_str(request.args.get("season")) or None

    try:
        limit_num = max(1, min(int(request.args.get("limit", MAX_RESULTS_DEFAULT)), 20000))
    except Exception:
        limit_num = MAX_RESULTS_DEFAULT

    rows = compute_stats_rows(query_results(league, season, limit_num))
    return jsonify({"ok": True, "count": len(rows), "rows": rows})


@app.get("/proxy/scoreboard")
def proxy_scoreboard_route():
    try:
        return proxy_scoreboard(
            normalize_str(request.args.get("league")),
            normalize_str(request.args.get("sport")),
            normalize_str(request.args.get("dates")),
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        return jsonify({"ok": False, "error": f"ESPN HTTP {status}"}), status
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/scoreboard")
def scoreboard_route():
    try:
        return proxy_scoreboard(
            normalize_str(request.args.get("league")),
            normalize_str(request.args.get("sport")),
            normalize_str(request.args.get("dates")),
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        return jsonify({"ok": False, "error": f"ESPN HTTP {status}"}), status
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)
else:
    init_db()
