import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import gspread
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2.service_account import Credentials

app = Flask(__name__)
CORS(app)

TZ_OFFSET = timedelta(hours=8)
SPREADSHEET_ID = os.getenv('GOOGLE_SHEET_ID', '').strip()
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON', '').strip()

HEADERS = [
    '比賽ID', '聯盟', '賽季', '比賽日期', '對戰', '主隊', '客隊', '最終比分',
    '近10讓分推薦', '近10讓分結果',
    '近10大小_淨值推薦', '近10大小_淨值結果',
    '近10大小_相加推薦', '近10大小_相加結果',
    '主客讓分推薦', '主客讓分結果',
    '主客大小_淨值推薦', '主客大小_淨值結果',
    '主客大小_相加推薦', '主客大小_相加結果',
    'EDGE讓分推薦', 'EDGE讓分結果', 'EDGE讓分值',
    'EDGE大小推薦', 'EDGE大小結果', 'EDGE大小值',
    '讓分盤', '大小分盤', '判定時間', '更新時間'
]

RESULT_COLUMNS = [
    '近10讓分結果', '近10大小_淨值結果', '近10大小_相加結果',
    '主客讓分結果', '主客大小_淨值結果', '主客大小_相加結果',
    'EDGE讓分結果', 'EDGE大小結果'
]


def taipei_now() -> datetime:
    return datetime.utcnow() + TZ_OFFSET


def build_credentials() -> Credentials:
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError('缺少 GOOGLE_CREDENTIALS_JSON')
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    return Credentials.from_service_account_info(info, scopes=scopes)


def get_client():
    if not SPREADSHEET_ID:
        raise RuntimeError('缺少 GOOGLE_SHEET_ID')
    gc = gspread.authorize(build_credentials())
    return gc.open_by_key(SPREADSHEET_ID)


def clean_str(value: Any) -> str:
    return '' if value is None else str(value).strip()


def clean_num(value: Any) -> str:
    if value in (None, ''):
        return ''
    try:
        return str(round(float(value), 3))
    except Exception:
        return clean_str(value)


def get_season_year(league: str, game_date: str) -> str:
    try:
        dt = datetime.strptime(game_date, '%Y-%m-%d')
    except Exception:
        return clean_str(game_date)[:4] or str(taipei_now().year)
    if clean_str(league).upper() == 'NBA':
        return str(dt.year + 1 if dt.month >= 9 else dt.year)
    return str(dt.year)


def worksheet_title(league: str, season: str) -> str:
    return f"{clean_str(league).upper()}_{clean_str(season)}"


def get_or_create_sheet(league: str, season: str):
    sh = get_client()
    title = worksheet_title(league, season)
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=3000, cols=max(40, len(HEADERS) + 4))
    ensure_headers(ws)
    return ws


def ensure_headers(ws) -> None:
    current = ws.row_values(1)
    if current[:len(HEADERS)] != HEADERS:
        end_col = gspread.utils.rowcol_to_a1(1, len(HEADERS)).split('1')[0]
        ws.update(f'A1:{end_col}1', [HEADERS])


def payload_to_row(payload: Dict[str, Any]) -> List[str]:
    row = []
    for key in HEADERS:
        if '值' in key or key in ('讓分盤', '大小分盤'):
            row.append(clean_num(payload.get(key)))
        else:
            row.append(clean_str(payload.get(key)))
    return row


def validate_payload(payload: Dict[str, Any]) -> Optional[str]:
    required = ['比賽ID', '聯盟', '比賽日期', '對戰']
    for key in required:
        if clean_str(payload.get(key)) == '':
            return f'缺少欄位: {key}'
    return None


def find_row_by_game_id(ws, game_id: str) -> Optional[int]:
    values = ws.col_values(1)
    for idx, value in enumerate(values[1:], start=2):
        if clean_str(value) == game_id:
            return idx
    return None


def parse_date(text: str) -> Optional[datetime]:
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    return None


def calc_summary(rows: List[Dict[str, Any]], result_col: str, mode: str) -> Dict[str, Any]:
    now = taipei_now()
    current_month = (now.year, now.month)
    weekday = now.weekday()
    week_start = datetime(now.year, now.month, now.day) - timedelta(days=weekday)

    win = lose = push = 0
    for row in rows:
        dt = parse_date(clean_str(row.get('比賽日期')))
        if not dt:
            continue
        if mode == 'week' and dt < week_start:
            continue
        if mode == 'month' and (dt.year, dt.month) != current_month:
            continue

        result = clean_str(row.get(result_col)).upper()
        if result == 'WIN':
            win += 1
        elif result == 'LOSE':
            lose += 1
        elif result == 'PUSH':
            push += 1

    graded = win + lose
    rate = round(win / graded * 100, 1) if graded else 0.0

    return {
        'win': win,
        'lose': lose,
        'push': push,
        'graded': graded,
        'rate': rate
    }


@app.get('/ping')
def ping():
    return jsonify({'ok': True, 'time': taipei_now().strftime('%Y-%m-%d %H:%M:%S')})


# ✅ 已修改這裡（支援 GET + POST）
@app.route('/init_sheet', methods=['GET', 'POST'])
def init_sheet():
    payload = request.get_json(silent=True) or {}
    league = clean_str(payload.get('聯盟') or request.args.get('league') or 'NBA')
    season = clean_str(payload.get('賽季') or request.args.get('season') or str(taipei_now().year))
    try:
        ws = get_or_create_sheet(league, season)
        return jsonify({'ok': True, 'sheet': ws.title, 'headers': HEADERS})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.post('/save_result')
def save_result():
    payload = request.get_json(silent=True) or {}
    err = validate_payload(payload)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    try:
        league = clean_str(payload.get('聯盟'))
        season = clean_str(payload.get('賽季')) or get_season_year(league, clean_str(payload.get('比賽日期')))
        payload['賽季'] = season
        payload.setdefault('更新時間', taipei_now().strftime('%Y-%m-%d %H:%M:%S'))

        ws = get_or_create_sheet(league, season)
        row = payload_to_row(payload)

        row_no = find_row_by_game_id(ws, clean_str(payload.get('比賽ID')))
        end_col = gspread.utils.rowcol_to_a1(1, len(HEADERS)).split('1')[0]

        if row_no:
            ws.update(f'A{row_no}:{end_col}{row_no}', [row])
            action = 'updated'
        else:
            ws.append_row(row, value_input_option='USER_ENTERED')
            action = 'inserted'

        return jsonify({'ok': True, 'action': action})

    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.get('/stats')
def stats():
    league = clean_str(request.args.get('league') or 'NBA')
    season = clean_str(request.args.get('season') or str(taipei_now().year))

    try:
        ws = get_or_create_sheet(league, season)
        rows = ws.get_all_records(expected_headers=HEADERS)

        summary = {
            col: {
                'week': calc_summary(rows, col, 'week'),
                'month': calc_summary(rows, col, 'month'),
                'season': calc_summary(rows, col, 'season'),
            }
            for col in RESULT_COLUMNS
        }

        return jsonify({'ok': True, 'summary': summary})

    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.get('/results')
def results():
    league = clean_str(request.args.get('league') or 'NBA')
    season = clean_str(request.args.get('season') or str(taipei_now().year))

    try:
        ws = get_or_create_sheet(league, season)
        rows = ws.get_all_records(expected_headers=HEADERS)
        rows = list(reversed(rows))[:200]
        return jsonify({'ok': True, 'rows': rows})

    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port)
