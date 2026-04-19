import os
from pathlib import Path
import pandas as pd
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv('OUTPUT_DIR', BASE_DIR / 'outputs'))
DEFAULT_MODE = os.getenv('DEFAULT_MODE', 'regular').strip().lower() or 'regular'
PORT = int(os.getenv('PORT', '10000'))

FILE_MAP = {
    'regular': {
        'summary': 'summary_regular.csv',
        'weekly': 'weekly_regular.csv',
        'picks': 'picks_regular.csv',
    },
    'all': {
        'summary': 'summary_regular_plus_playoffs.csv',
        'weekly': 'weekly_regular_plus_playoffs.csv',
        'picks': 'picks_regular_plus_playoffs.csv',
    },
}

CONFIG = {
    'version': 'V5-clean',
    'default_mode': DEFAULT_MODE if DEFAULT_MODE in FILE_MAP else 'regular',
    'weekly_target': int(os.getenv('WEEKLY_TARGET', '10')),
    'weekly_range_min': int(os.getenv('WEEKLY_RANGE_MIN', '8')),
    'weekly_range_max': int(os.getenv('WEEKLY_RANGE_MAX', '12')),
    'risk_controls': {
        'extreme_total_guard': True,
        'rest_diff_guard': True,
        'volatility_guard': True,
        'line_move_guard': True,
    },
}

app = Flask(__name__, template_folder=str(BASE_DIR / 'templates'))


def _mode() -> str:
    mode = request.args.get('mode', CONFIG['default_mode']).strip().lower()
    return mode if mode in FILE_MAP else CONFIG['default_mode']


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
    return df.fillna('').to_dict(orient='records')


@app.route('/')
def index():
    return render_template('index.html', config=CONFIG)


@app.route('/api')
def api_root():
    files = {m: {k: str((OUTPUT_DIR / v).exists()) for k, v in mp.items()} for m, mp in FILE_MAP.items()}
    return jsonify({'ok': True, 'version': CONFIG['version'], 'output_dir': str(OUTPUT_DIR), 'files': files})


@app.route('/health')
def health():
    files = {m: {k: (OUTPUT_DIR / v).exists() for k, v in mp.items()} for m, mp in FILE_MAP.items()}
    return jsonify({'ok': True, 'version': CONFIG['version'], 'output_dir': str(OUTPUT_DIR), 'files': files})


@app.route('/config')
def config():
    return jsonify({'ok': True, **CONFIG})


@app.route('/summary')
def summary():
    mode = _mode()
    df = _read_csv('summary', mode)
    return jsonify({'ok': True, 'mode': mode, 'rows': _rows(df)})


@app.route('/weekly')
def weekly():
    mode = _mode()
    df = _read_csv('weekly', mode)
    limit = int(request.args.get('limit', '200'))
    if limit > 0 and not df.empty:
        df = df.head(limit)
    return jsonify({'ok': True, 'mode': mode, 'rows': _rows(df)})


@app.route('/picks')
def picks():
    mode = _mode()
    df = _read_csv('picks', mode)
    league = request.args.get('league', '').strip().upper()
    if league and not df.empty and 'league' in df.columns:
        df = df[df['league'].astype(str).str.upper() == league]
    limit = int(request.args.get('limit', '100'))
    if limit > 0 and not df.empty:
        df = df.head(limit)
    return jsonify({'ok': True, 'mode': mode, 'rows': _rows(df)})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=True)
