import os
import json
import traceback
from datetime import datetime, timezone, timedelta

import requests
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
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

_GC = None
_SHEET = None
_WS_CACHE = {}
_HEADER_CACHE = {}

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

PICK_RESULT_PAIRS = [
    ("近10讓分推薦", "近10讓分結果"),
    ("近10大小_淨值推薦", "近10大小_淨值結果"),
    ("近10大小_相加推薦", "近10大小_相加結果"),
    ("主客讓分推薦", "主客讓分結果"),
    ("主客大小_淨值推薦", "主客大小_淨值結果"),
    ("主客大小_相加推薦", "主客大小_相加結果"),
    ("EDGE讓分推薦", "EDGE讓分結果"),
    ("EDGE大小推薦", "EDGE大小結果"),
]

RESULT_KEYS = [result_key for _, result_key in PICK_RESULT_PAIRS]

LEAGUE_CONFIG = {
    "NBA": {
        "sport": "basketball",
        "league": "nba",
    },
    "MLB": {
        "sport": "baseball",
        "league": "mlb",
    },
}


def safe_str(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def clean_str(value):
    return safe_str(value).strip()


def now_taipei_iso():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat(timespec="seconds")


def normalize_score_text(value):
    s = clean_str(value)
    if not s:
        return ""
    if not s.startswith("'"):
        s = "'" + s
    return s


def to_float(value):
    s = clean_str(value)
    if not s:
        return None
    s = "".join(ch for ch in s if ch in "0123456789.-+")
    if not s or s in {"+", "-", ".", "+.", "-."}:
        return None
    try:
        return float(s)
    except Exception:
        return None


def parse_final_score(score_text):
    s = clean_str(score_text)
    if not s:
        return None
    if s.startswith("'"):
        s = s[1:]
    s = s.replace("：", ":").replace(" ", "")
    if ":" not in s:
        return None
    left, right = s.split(":", 1)
    try:
        return int(left), int(right)
    except Exception:
        return None


def is_invalid_placeholder_score(score_pair):
    if not score_pair:
        return True
    away_score, home_score = score_pair
    if away_score < 0 or home_score < 0:
        return True
    if away_score == 0 and home_score == 0:
        return True
    return False


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
    return get_spreadsheet().worksheets()


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


def worksheet_name_from_query():
    league = clean_str(request.args.get("league")).upper()
    season = clean_str(request.args.get("season"))

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
    if len(rows) <= 1:
        return []

    width = len(header)
    out = []
    for row in rows[1:]:
        padded = list(row) + [""] * max(0, width - len(row))
        padded = padded[:width]
        out.append({header[i]: padded[i] for i in range(width)})
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


def judge_pick_result(row, pick_key):
    pick = clean_str(row.get(pick_key))
    if not pick or pick == "PASS":
        return ""

    final_score = parse_final_score(row.get("最終比分"))
    if not final_score:
        return ""

    away_score, home_score = final_score

    if is_invalid_placeholder_score(final_score):
        return ""

    away_team = clean_str(row.get("客隊"))
    home_team = clean_str(row.get("主隊"))

    total = away_score + home_score
    total_line = to_float(row.get("大小分盤"))
    spread_line = to_float(row.get("讓分盤"))

    if pick == "大分":
        if total_line is None:
            return ""
        if total > total_line:
            return "WIN"
        if total < total_line:
            return "LOSE"
        return "PUSH"

    if pick == "小分":
        if total_line is None:
            return ""
        if total < total_line:
            return "WIN"
        if total > total_line:
            return "LOSE"
        return "PUSH"

    is_give = pick.endswith("讓分")
    is_take = pick.endswith("受讓")
    if not (is_give or is_take):
        return ""

    if spread_line is None:
        return ""

    team_name = pick.replace("讓分", "").replace("受讓", "").strip()

    if team_name == away_team:
        diff = away_score - home_score
    elif team_name == home_team:
        diff = home_score - away_score
    else:
        return ""

    adj = diff - spread_line if is_give else diff + spread_line

    if adj > 0:
        return "WIN"
    if adj < 0:
        return "LOSE"
    return "PUSH"


def build_stats_rows(rows):
    return rows


def normalize_date_to_yyyymmdd(value):
    s = clean_str(value)
    if not s:
        return ""
    return s.replace("-", "").replace("/", "")[:8]


def espn_scoreboard_url(league_name, yyyymmdd):
    conf = LEAGUE_CONFIG.get(league_name.upper())
    if not conf:
        raise RuntimeError(f"Unsupported league: {league_name}")
    return f"https://site.api.espn.com/apis/site/v2/sports/{conf['sport']}/{conf['league']}/scoreboard?dates={yyyymmdd}"


def fetch_json(url):
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def extract_market_from_event(event):
    comp = ((event or {}).get("competitions") or [{}])[0]
    odds_list = []
    if isinstance(comp.get("odds"), list):
        odds_list.extend(comp["odds"])
    if isinstance(event.get("odds"), list):
        odds_list.extend(event["odds"])

    total = None
    left_spread = None
    right_spread = None

    competitors = (comp.get("competitors") or [])[:]
    competitors.sort(key=lambda x: 0 if x.get("homeAway") == "away" else 1)

    away_comp = competitors[0] if len(competitors) > 0 else {}
    home_comp = competitors[1] if len(competitors) > 1 else {}

    def pick_num(*values):
        for v in values:
            n = to_float(v)
            if n is not None:
                return n
        return None

    def team_side_spread(odds, competitor):
        side = competitor.get("homeAway")
        side_odds = None
        if side == "away":
            side_odds = odds.get("awayTeamOdds") or {}
        elif side == "home":
            side_odds = odds.get("homeTeamOdds") or {}
        return pick_num(
            side_odds.get("spread"),
            side_odds.get("pointSpread"),
            side_odds.get("line"),
            (competitor.get("odds") or {}).get("spread"),
            (competitor.get("odds") or {}).get("pointSpread"),
        )

    for odds in odds_list:
        if total is None:
            total = pick_num(
                odds.get("overUnder"),
                odds.get("overunder"),
                odds.get("total"),
                odds.get("totalPoints"),
                odds.get("overUnderLine"),
            )

        if left_spread is None:
            left_spread = team_side_spread(odds, away_comp)

        if right_spread is None:
            right_spread = team_side_spread(odds, home_comp)

        if total is not None and left_spread is not None and right_spread is not None:
            break

    if left_spread is None and right_spread is not None:
        left_spread = -right_spread
    if right_spread is None and left_spread is not None:
        right_spread = -left_spread

    favorite_abs = None
    if left_spread is not None and right_spread is not None:
        if left_spread < right_spread:
            favorite_abs = abs(left_spread)
        elif right_spread < left_spread:
            favorite_abs = abs(right_spread)
        elif left_spread < 0:
            favorite_abs = abs(left_spread)

    spread_line = favorite_abs

    return {
        "spread_line": spread_line,
        "total_line": total,
    }


def extract_final_score_from_event(event):
    comp = ((event or {}).get("competitions") or [{}])[0]
    competitors = (comp.get("competitors") or [])[:]
    competitors.sort(key=lambda x: 0 if x.get("homeAway") == "away" else 1)

    if len(competitors) < 2:
        return ""

    away_score = competitors[0].get("score")
    home_score = competitors[1].get("score")

    try:
        away_score = int(str(away_score))
        home_score = int(str(home_score))
    except Exception:
        return ""

    status = (((event or {}).get("status") or {}).get("type") or {})
    completed = bool(status.get("completed"))

    if not completed and away_score == 0 and home_score == 0:
        return ""

    return f"{away_score}:{home_score}"


def find_event_by_game_id(league_name, date_yyyymmdd, game_id):
    url = espn_scoreboard_url(league_name, date_yyyymmdd)
    data = fetch_json(url)
    events = data.get("events") or []
    for event in events:
        if clean_str(event.get("id")) == clean_str(game_id):
            return event
    return None


def refresh_row_from_event(row):
    league = clean_str(row.get("聯盟")).upper()
    game_id = clean_str(row.get("比賽ID"))
    date_key = normalize_date_to_yyyymmdd(row.get("比賽日期"))

    if not league or not game_id or not date_key:
        return row, False, "missing_key"

    event = find_event_by_game_id(league, date_key, game_id)
    if not event:
        return row, False, "event_not_found"

    next_row = dict(row)
    changed = False

    final_score = extract_final_score_from_event(event)
    market = extract_market_from_event(event)

    if final_score:
        normalized = normalize_score_text(final_score)
        if clean_str(next_row.get("最終比分")) != clean_str(normalized):
            next_row["最終比分"] = normalized
            changed = True

    spread_line = market.get("spread_line")
    if spread_line is not None:
        spread_text = str(spread_line).rstrip("0").rstrip(".") if isinstance(spread_line, float) else str(spread_line)
        if clean_str(next_row.get("讓分盤")) != clean_str(spread_text):
            next_row["讓分盤"] = spread_text
            changed = True

    total_line = market.get("total_line")
    if total_line is not None:
        total_text = str(total_line).rstrip("0").rstrip(".") if isinstance(total_line, float) else str(total_line)
        if clean_str(next_row.get("大小分盤")) != clean_str(total_text):
            next_row["大小分盤"] = total_text
            changed = True

    if changed:
        next_row["更新時間"] = now_taipei_iso()

    return next_row, changed, "ok"


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
            return jsonify({"ok": True, "rows": [], "worksheet": ws.title, "count": 0}), 200

        rows = get_all_records_safe(ws, header)
        rows = list(reversed(rows))[:parse_limit()]

        return jsonify({
            "ok": True,
            "rows": rows,
            "worksheet": ws.title,
            "count": len(rows),
        }), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__}), 500


@app.route("/stats", methods=["GET"])
def stats():
    try:
        ws_name = worksheet_name_from_query()
        ws = get_worksheet_by_name(ws_name)
        header = get_header(ws, force_refresh=True)

        if not header:
            return jsonify({"ok": True, "rows": [], "worksheet": ws.title, "count": 0}), 200

        rows = get_all_records_safe(ws, header)
        rows = list(reversed(rows))[:parse_limit()]

        return jsonify({
            "ok": True,
            "rows": build_stats_rows(rows),
            "worksheet": ws.title,
            "count": len(rows),
        }), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__}), 500


@app.route("/save_result", methods=["OPTIONS", "POST"])
def save_result():
    if request.method == "OPTIONS":
        return ("", 204)

    try:
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

        game_id = clean_str(alias_value(data, "比賽ID"))
        if not game_id:
            return jsonify({"ok": False, "error": "Missing 比賽ID"}), 400

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
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__}), 500


@app.route("/repair_results", methods=["GET", "POST"])
def repair_results():
    try:
        payload = request.get_json(silent=True) if request.is_json else {}
        payload = payload or {}

        league = clean_str(request.args.get("league") or payload.get("league")).upper()
        season = clean_str(request.args.get("season") or payload.get("season"))

        ws_name = f"{league}_{season}" if league and season else worksheet_name_from_query()
        ws = get_worksheet_by_name(ws_name)
        header = get_header(ws, force_refresh=True)

        if not header:
            return jsonify({
                "ok": True,
                "worksheet": ws.title,
                "repaired": 0,
                "checked": 0,
                "message": "empty header",
            }), 200

        rows = get_all_records_safe(ws, header)
        end_col = col_to_a1(len(header))

        repaired = 0
        checked = 0
        details = []

        for idx, row in enumerate(rows, start=2):
            checked += 1
            next_row = dict(row)
            final_score = parse_final_score(next_row.get("最終比分"))

            if not is_invalid_placeholder_score(final_score):
                continue

            changed = False
            for result_key in RESULT_KEYS:
                if clean_str(next_row.get(result_key)):
                    next_row[result_key] = ""
                    changed = True

            if changed:
                next_row["更新時間"] = now_taipei_iso()
                write_row = build_row_from_header(next_row, header)
                ws.update(f"A{idx}:{end_col}{idx}", [write_row])
                repaired += 1
                details.append({
                    "row": idx,
                    "game_id": clean_str(next_row.get("比賽ID")),
                })

        return jsonify({
            "ok": True,
            "worksheet": ws.title,
            "checked": checked,
            "repaired": repaired,
            "details": details[:100],
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__}), 500


@app.route("/refresh_events", methods=["GET", "POST"])
def refresh_events():
    try:
        payload = request.get_json(silent=True) if request.is_json else {}
        payload = payload or {}

        league = clean_str(request.args.get("league") or payload.get("league")).upper()
        season = clean_str(request.args.get("season") or payload.get("season"))

        ws_name = f"{league}_{season}" if league and season else worksheet_name_from_query()
        ws = get_worksheet_by_name(ws_name)
        header = get_header(ws, force_refresh=True)

        if not header:
            return jsonify({
                "ok": True,
                "worksheet": ws.title,
                "checked": 0,
                "updated": 0,
                "message": "empty header",
            }), 200

        rows = get_all_records_safe(ws, header)
        end_col = col_to_a1(len(header))

        checked = 0
        updated = 0
        details = []

        for idx, row in enumerate(rows, start=2):
            checked += 1
            next_row, changed, reason = refresh_row_from_event(row)

            if changed:
                write_row = build_row_from_header(next_row, header)
                ws.update(f"A{idx}:{end_col}{idx}", [write_row])
                updated += 1
                details.append({
                    "row": idx,
                    "game_id": clean_str(next_row.get("比賽ID")),
                    "reason": reason,
                    "final_score": clean_str(next_row.get("最終比分")),
                    "spread_line": clean_str(next_row.get("讓分盤")),
                    "total_line": clean_str(next_row.get("大小分盤")),
                })

        return jsonify({
            "ok": True,
            "worksheet": ws.title,
            "checked": checked,
            "updated": updated,
            "details": details[:100],
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__}), 500


@app.route("/backfill_results", methods=["GET", "POST"])
def backfill_results():
    try:
        payload = request.get_json(silent=True) if request.is_json else {}
        payload = payload or {}

        league = clean_str(request.args.get("league") or payload.get("league")).upper()
        season = clean_str(request.args.get("season") or payload.get("season"))

        ws_name = f"{league}_{season}" if league and season else worksheet_name_from_query()
        ws = get_worksheet_by_name(ws_name)
        header = get_header(ws, force_refresh=True)

        if not header:
            return jsonify({
                "ok": True,
                "worksheet": ws.title,
                "updated": 0,
                "checked": 0,
                "message": "empty header",
            }), 200

        rows = get_all_records_safe(ws, header)
        end_col = col_to_a1(len(header))

        updated = 0
        checked = 0
        details = []

        for idx, row in enumerate(rows, start=2):
            checked += 1
            changed = False
            next_row = dict(row)

            final_score = parse_final_score(next_row.get("最終比分"))
            if is_invalid_placeholder_score(final_score):
                continue

            for pick_key, result_key in PICK_RESULT_PAIRS:
                pick = clean_str(next_row.get(pick_key))
                result = clean_str(next_row.get(result_key))

                if not pick or pick == "PASS" or result:
                    continue

                judged = judge_pick_result(next_row, pick_key)
                if judged:
                    next_row[result_key] = judged
                    changed = True

            if changed:
                next_row["更新時間"] = now_taipei_iso()
                write_row = build_row_from_header(next_row, header)
                ws.update(f"A{idx}:{end_col}{idx}", [write_row])
                updated += 1
                details.append({
                    "row": idx,
                    "game_id": clean_str(next_row.get("比賽ID")),
                })

        return jsonify({
            "ok": True,
            "worksheet": ws.title,
            "checked": checked,
            "updated": updated,
            "details": details[:100],
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
