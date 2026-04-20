#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

TAIPEI = timezone(timedelta(hours=8))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "historical-backfill-with-playsport/3.0"})


CONFIG = {
    "NBA": {
        "key": "nba",
        "sport": "basketball",
        "league": "nba",
        "lookback_days": 40,
        "total_edge": 8.0,
        "spread_edge": 3.5,
        "high_total": 235.0,
        "season_start": date(2020, 12, 1),
    },
    "MLB": {
        "key": "mlb",
        "sport": "baseball",
        "league": "mlb",
        "lookback_days": 25,
        "total_edge": 1.2,
        "spread_edge": 0.8,
        "high_total": 11.5,
        "season_start": date(2020, 7, 20),
    },
}

TEAM_NAME_ZH = {
    "nba": {
        "Atlanta Hawks": "亞特蘭大老鷹",
        "Boston Celtics": "波士頓塞爾提克",
        "Brooklyn Nets": "布魯克林籃網",
        "Charlotte Hornets": "夏洛特黃蜂",
        "Chicago Bulls": "芝加哥公牛",
        "Cleveland Cavaliers": "克里夫蘭騎士",
        "Dallas Mavericks": "達拉斯獨行俠",
        "Denver Nuggets": "丹佛金塊",
        "Detroit Pistons": "底特律活塞",
        "Golden State Warriors": "金州勇士",
        "Houston Rockets": "休士頓火箭",
        "Indiana Pacers": "印第安納溜馬",
        "LA Clippers": "洛杉磯快艇",
        "Los Angeles Clippers": "洛杉磯快艇",
        "Los Angeles Lakers": "洛杉磯湖人",
        "LA Lakers": "洛杉磯湖人",
        "Memphis Grizzlies": "曼菲斯灰熊",
        "Miami Heat": "邁阿密熱火",
        "Milwaukee Bucks": "密爾瓦基公鹿",
        "Minnesota Timberwolves": "明尼蘇達灰狼",
        "New Orleans Pelicans": "紐奧良鵜鶘",
        "New York Knicks": "紐約尼克",
        "Oklahoma City Thunder": "奧克拉荷馬雷霆",
        "Orlando Magic": "奧蘭多魔術",
        "Philadelphia 76ers": "費城76人",
        "Phoenix Suns": "鳳凰城太陽",
        "Portland Trail Blazers": "波特蘭拓荒者",
        "Sacramento Kings": "沙加緬度國王",
        "San Antonio Spurs": "聖安東尼奧馬刺",
        "Toronto Raptors": "多倫多暴龍",
        "Utah Jazz": "猶他爵士",
        "Washington Wizards": "華盛頓巫師",
    },
    "mlb": {
        "Arizona Diamondbacks": "亞利桑那響尾蛇",
        "Athletics": "運動家",
        "Oakland Athletics": "奧克蘭運動家",
        "Atlanta Braves": "亞特蘭大勇士",
        "Baltimore Orioles": "巴爾的摩金鶯",
        "Boston Red Sox": "波士頓紅襪",
        "Chicago Cubs": "芝加哥小熊",
        "Chicago White Sox": "芝加哥白襪",
        "Cincinnati Reds": "辛辛那提紅人",
        "Cleveland Guardians": "克里夫蘭守護者",
        "Cleveland Indians": "克里夫蘭守護者",
        "Colorado Rockies": "科羅拉多落磯",
        "Detroit Tigers": "底特律老虎",
        "Houston Astros": "休士頓太空人",
        "Kansas City Royals": "堪薩斯市皇家",
        "Los Angeles Angels": "洛杉磯天使",
        "Los Angeles Dodgers": "洛杉磯道奇",
        "Miami Marlins": "邁阿密馬林魚",
        "Milwaukee Brewers": "密爾瓦基釀酒人",
        "Minnesota Twins": "明尼蘇達雙城",
        "New York Mets": "紐約大都會",
        "New York Yankees": "紐約洋基",
        "Philadelphia Phillies": "費城費城人",
        "Pittsburgh Pirates": "匹茲堡海盜",
        "San Diego Padres": "聖地牙哥教士",
        "San Francisco Giants": "舊金山巨人",
        "Seattle Mariners": "西雅圖水手",
        "St. Louis Cardinals": "聖路易紅雀",
        "Tampa Bay Rays": "坦帕灣光芒",
        "Texas Rangers": "德州遊騎兵",
        "Toronto Blue Jays": "多倫多藍鳥",
        "Washington Nationals": "華盛頓國民",
    },
}

PLAYSPORT_ALIAS = {
    "NBA": {
        "勇士": "金州勇士",
        "籃網": "布魯克林籃網",
        "快艇": "洛杉磯快艇",
        "湖人": "洛杉磯湖人",
        "公鹿": "密爾瓦基公鹿",
        "塞爾提克": "波士頓塞爾提克",
        "熱火": "邁阿密熱火",
        "尼克": "紐約尼克",
        "太陽": "鳳凰城太陽",
        "金塊": "丹佛金塊",
        "灰狼": "明尼蘇達灰狼",
        "灰熊": "曼菲斯灰熊",
        "馬刺": "聖安東尼奧馬刺",
        "國王": "沙加緬度國王",
        "老鷹": "亞特蘭大老鷹",
        "黃蜂": "夏洛特黃蜂",
        "公牛": "芝加哥公牛",
        "騎士": "克里夫蘭騎士",
        "活塞": "底特律活塞",
        "溜馬": "印第安納溜馬",
        "巫師": "華盛頓巫師",
        "魔術": "奧蘭多魔術",
        "76人": "費城76人",
        "暴龍": "多倫多暴龍",
        "爵士": "猶他爵士",
        "雷霆": "奧克拉荷馬雷霆",
        "獨行俠": "達拉斯獨行俠",
        "火箭": "休士頓火箭",
        "拓荒者": "波特蘭拓荒者",
        "鵜鶘": "紐奧良鵜鶘",
    },
    "MLB": {
        "響尾蛇": "亞利桑那響尾蛇",
        "金鶯": "巴爾的摩金鶯",
        "紅襪": "波士頓紅襪",
        "小熊": "芝加哥小熊",
        "白襪": "芝加哥白襪",
        "紅人": "辛辛那提紅人",
        "守護者": "克里夫蘭守護者",
        "印地安人": "克里夫蘭守護者",
        "Indians": "克里夫蘭守護者",
        "落磯": "科羅拉多落磯",
        "老虎": "底特律老虎",
        "太空人": "休士頓太空人",
        "皇家": "堪薩斯市皇家",
        "天使": "洛杉磯天使",
        "道奇": "洛杉磯道奇",
        "馬林魚": "邁阿密馬林魚",
        "釀酒人": "密爾瓦基釀酒人",
        "雙城": "明尼蘇達雙城",
        "大都會": "紐約大都會",
        "洋基": "紐約洋基",
        "費城人": "費城費城人",
        "海盜": "匹茲堡海盜",
        "教士": "聖地牙哥教士",
        "巨人": "舊金山巨人",
        "水手": "西雅圖水手",
        "紅雀": "聖路易紅雀",
        "光芒": "坦帕灣光芒",
        "遊騎兵": "德州遊騎兵",
        "藍鳥": "多倫多藍鳥",
        "國民": "華盛頓國民",
        "勇士": "亞特蘭大勇士",
        "運動家": "奧克蘭運動家",
    },
}


@dataclass
class GameStat:
    pf: float
    pa: float
    date: str
    game_ms: int
    game_id: str
    home_away: str


def clean_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    return str(v).strip()


def safe_num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = clean_text(v).replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def avg(nums: List[float]) -> float:
    return sum(nums) / len(nums) if nums else 0.0


def round1(v: Optional[float]) -> str:
    if v is None:
        return ""
    return f"{round(float(v), 1):.1f}"


def weighted_value(values: List[Optional[float]]) -> Optional[float]:
    weights = [0.5, 0.3, 0.2]
    total = 0.0
    weight_sum = 0.0
    for i, v in enumerate(values):
        if v is None:
            continue
        total += float(v) * weights[i]
        weight_sum += weights[i]
    return total / weight_sum if weight_sum else None


def normalize_team_name(league: str, name: str) -> str:
    name = clean_text(name)
    if not name:
        return ""
    return PLAYSPORT_ALIAS.get(league, {}).get(name, name)


def format_recent_games(games: List[GameStat], count: int = 10) -> str:
    parts: List[str] = []
    for g in games[:count]:
        dt = datetime.fromisoformat(g.date.replace("Z", "+00:00")).astimezone(TAIPEI)
        date_text = dt.strftime("%Y-%m-%d")
        side = "主" if g.home_away == "home" else "客"
        parts.append(f"{date_text} {side} {int(g.pf)}-{int(g.pa)}")
    return " | ".join(parts)


class PlaysportLookup:
    def __init__(self, mlb_csv: Optional[str], nba_csv: Optional[str]) -> None:
        self.lookup: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

        if mlb_csv and os.path.exists(mlb_csv):
            self._load_csv(mlb_csv, "MLB")
        elif mlb_csv:
            print(f"SKIP missing MLB csv: {mlb_csv}", flush=True)

        if nba_csv and os.path.exists(nba_csv):
            self._load_csv(nba_csv, "NBA")
        elif nba_csv:
            print(f"SKIP missing NBA csv: {nba_csv}", flush=True)

    def _load_csv(self, path: str, league: str) -> None:
        df = pd.read_csv(path)
        for _, row in df.iterrows():
            game_date = clean_text(row.get("scrape_date"))
            away_team = normalize_team_name(league, row.get("away_team"))
            home_team = normalize_team_name(league, row.get("home_team"))
            if not game_date or not away_team or not home_team:
                continue

            final_spread = safe_num(row.get("final_spread"))
            final_total = safe_num(row.get("final_total"))
            final_favorite_side = clean_text(row.get("final_favorite_side")).lower()

            away_spread = None
            home_spread = None
            if final_spread is not None and final_favorite_side == "away":
                away_spread = -final_spread
                home_spread = final_spread
            elif final_spread is not None and final_favorite_side == "home":
                away_spread = final_spread
                home_spread = -final_spread

            self.lookup[(league, game_date, away_team, home_team)] = {
                "spread": final_spread,
                "total": final_total,
                "favorite_side": final_favorite_side,
                "away_spread": away_spread,
                "home_spread": home_spread,
            }

    def get(self, league: str, game_date: str, away_team: str, home_team: str) -> Optional[Dict[str, Any]]:
        return self.lookup.get((league, game_date, away_team, home_team))


class BackfillRunner:
    def __init__(
        self,
        api_base: str,
        playsport_lookup: PlaysportLookup,
        dry_run: bool = False,
        sleep_seconds: float = 1.2,
        verbose: bool = True,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.playsport_lookup = playsport_lookup
        self.dry_run = dry_run
        self.sleep_seconds = sleep_seconds
        self.verbose = verbose
        self.scoreboard_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.team_games_cache: Dict[Tuple[str, str, str], List[GameStat]] = {}
        self.saved = 0
        self.failed = 0
        self.scanned = 0

    def log(self, *parts: Any) -> None:
        if self.verbose:
            print(*parts, flush=True)

    def taipei_now_iso(self) -> str:
        return datetime.now(TAIPEI).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    def fetch_json(self, url: str) -> Dict[str, Any]:
        resp = SESSION.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def fetch_existing_rows(self, league_name: str, season: str, limit: int = 5000) -> Dict[str, Dict[str, Any]]:
        try:
            resp = SESSION.get(
                f"{self.api_base}/results",
                params={"league": league_name, "season": season, "limit": limit},
                timeout=30,
            )
            if not resp.ok:
                return {}
            data = resp.json()
            rows = data.get("rows") or []
            return {str(r.get("比賽ID")): r for r in rows if r.get("比賽ID")}
        except Exception:
            return {}

    def get_scoreboard(self, league_name: str, yyyymmdd: str) -> List[Dict[str, Any]]:
        conf = CONFIG[league_name]
        key = (league_name, yyyymmdd)
        if key in self.scoreboard_cache:
            return self.scoreboard_cache[key]

        direct_url = (
            f"https://site.api.espn.com/apis/site/v2/sports/"
            f"{conf['sport']}/{conf['league']}/scoreboard?dates={yyyymmdd}"
        )
        urls = [
            f"{self.api_base}/proxy/scoreboard?league={conf['league']}&sport={conf['sport']}&dates={yyyymmdd}",
            f"{self.api_base}/scoreboard?league={conf['league']}&sport={conf['sport']}&dates={yyyymmdd}",
            direct_url,
        ]
        last_err: Optional[Exception] = None
        for url in urls:
            try:
                data = self.fetch_json(url)
                events = data.get("events") or []
                self.scoreboard_cache[key] = events
                return events
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"scoreboard fetch failed {league_name} {yyyymmdd}: {last_err}")

    def team_name_zh(self, conf_key: str, team: Dict[str, Any]) -> str:
        en = team.get("displayName") or team.get("shortDisplayName") or team.get("name") or "-"
        return TEAM_NAME_ZH.get(conf_key, {}).get(en, en)

    def to_taipei_date_key(self, iso: str) -> str:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TAIPEI)
        return dt.strftime("%Y-%m-%d")

    def get_season_year(self, league_name: str, iso: str) -> str:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TAIPEI)
        if league_name == "NBA":
            return str(dt.year + 1 if dt.month >= 9 else dt.year)
        return str(dt.year)

    def get_season_type_value(self, event: Dict[str, Any]) -> str:
        candidates = [
            event.get("season", {}).get("type"),
            ((event.get("competitions") or [{}])[0].get("type") or {}).get("id"),
            ((event.get("competitions") or [{}])[0].get("type") or {}).get("type"),
            ((event.get("competitions") or [{}])[0].get("type") or {}).get("abbreviation"),
        ]
        for v in candidates:
            if v is None:
                continue
            if isinstance(v, (int, float)):
                return str(int(v))
            if isinstance(v, str):
                return v.lower()
        return ""

    def is_official_game(self, league_name: str, event: Dict[str, Any]) -> bool:
        t = self.get_season_type_value(event)
        if not t:
            return False
        if t in {"2", "3", "4"}:
            return True
        official_terms = ("regular", "post", "playoff", "play-in", "playin", "final")
        reject_terms = ("pre", "preseason", "spring", "exhibition")
        if any(term in t for term in reject_terms):
            return False
        if any(term in t for term in official_terms):
            return True
        return False

    def get_completed_game_for_team(
        self,
        league_name: str,
        event: Dict[str, Any],
        team_id: str,
        cutoff_ms: int,
    ) -> Optional[GameStat]:
        if not self.is_official_game(league_name, event):
            return None
        if not ((event.get("status") or {}).get("type") or {}).get("completed"):
            return None

        game_ms = int(datetime.fromisoformat(event["date"].replace("Z", "+00:00")).timestamp() * 1000)
        if game_ms >= cutoff_ms:
            return None

        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        mine = next((c for c in competitors if str((c.get("team") or {}).get("id")) == str(team_id)), None)
        opp = next((c for c in competitors if str((c.get("team") or {}).get("id")) != str(team_id)), None)
        if not mine or not opp:
            return None

        try:
            pf = float(mine.get("score"))
            pa = float(opp.get("score"))
        except Exception:
            return None

        return GameStat(
            pf=pf,
            pa=pa,
            date=event["date"],
            game_ms=game_ms,
            game_id=str(event.get("id")),
            home_away=mine.get("homeAway") or "",
        )

    def get_team_recent_games(self, league_name: str, team_id: str, cutoff_iso: str) -> List[GameStat]:
        cache_key = (league_name, team_id, cutoff_iso)
        if cache_key in self.team_games_cache:
            return self.team_games_cache[cache_key]

        conf = CONFIG[league_name]
        cutoff_dt = datetime.fromisoformat(cutoff_iso.replace("Z", "+00:00"))
        cutoff_ms = int(cutoff_dt.timestamp() * 1000)

        out: List[GameStat] = []
        seen: set[str] = set()

        for i in range(conf["lookback_days"] + 1):
            d = (cutoff_dt - timedelta(days=i)).date()
            yyyymmdd = d.strftime("%Y%m%d")
            try:
                events = self.get_scoreboard(league_name, yyyymmdd)
            except Exception:
                continue

            for ev in events:
                gs = self.get_completed_game_for_team(league_name, ev, team_id, cutoff_ms)
                if gs and gs.game_id not in seen:
                    seen.add(gs.game_id)
                    out.append(gs)

            if len(out) >= 30:
                break

        out.sort(key=lambda g: g.game_ms, reverse=True)
        result = out[:30]
        self.team_games_cache[cache_key] = result
        return result

    def is_doubleheader_event(self, league_name: str, event: Dict[str, Any]) -> bool:
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            return False

        event_date_key = self.to_taipei_date_key(event["date"])
        yyyymmdd = event_date_key.replace("-", "")
        try:
            same_day_events = self.get_scoreboard(league_name, yyyymmdd)
        except Exception:
            return False

        official_same_day = [ev for ev in same_day_events if self.is_official_game(league_name, ev)]
        if len(official_same_day) <= 1:
            return False

        team_ids = {str((c.get("team") or {}).get("id") or "") for c in competitors}
        team_ids.discard("")
        if not team_ids:
            return False

        counts = {tid: 0 for tid in team_ids}
        for ev in official_same_day:
            ev_date_key = self.to_taipei_date_key(ev.get("date") or "")
            if ev_date_key != event_date_key:
                continue
            ev_comp = (ev.get("competitions") or [{}])[0]
            for c in ev_comp.get("competitors") or []:
                tid = str((c.get("team") or {}).get("id") or "")
                if tid in counts:
                    counts[tid] += 1
        return any(v >= 2 for v in counts.values())

    def get_window_averages(self, games: List[GameStat], count: int, side: Optional[str] = None) -> Optional[Dict[str, float]]:
        filtered = games if not side else [g for g in games if g.home_away == side]
        sl = filtered[:count]
        if not sl:
            return None
        return {"pf": avg([g.pf for g in sl]), "pa": avg([g.pa for g in sl])}

    def to_final_score_text(self, event: Dict[str, Any]) -> str:
        competitors = sorted(
            ((event.get("competitions") or [{}])[0].get("competitors") or []),
            key=lambda c: 0 if c.get("homeAway") == "away" else 1,
        )
        if len(competitors) < 2:
            return ""
        try:
            left_score = int(float(competitors[0].get("score")))
            right_score = int(float(competitors[1].get("score")))
            return f"{left_score}:{right_score}"
        except Exception:
            return ""

    def build_rows_for_event(self, league_name: str, event: Dict[str, Any], existing_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not self.is_official_game(league_name, event):
            return None

        comp = (event.get("competitions") or [{}])[0]
        competitors = sorted(comp.get("competitors") or [], key=lambda c: 0 if c.get("homeAway") == "away" else 1)
        if len(competitors) < 2:
            return None

        conf = CONFIG[league_name]
        conf_key = conf["key"]
        rows = []
        is_doubleheader = self.is_doubleheader_event(league_name, event)

        for c in competitors:
            team_id = str((c.get("team") or {}).get("id") or "")
            stats = self.get_team_recent_games(league_name, team_id, event["date"])
            side = c.get("homeAway")
            side_stats = [g for g in stats if g.home_away == side]

            all10 = self.get_window_averages(stats, 10)
            side10 = self.get_window_averages(side_stats, 10)
            all5 = self.get_window_averages(stats, 5)
            all3 = self.get_window_averages(stats, 3)

            prev_game = stats[0] if stats else None
            rest_days = None
            if prev_game:
                try:
                    rest_days = int(
                        (
                            datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                            - datetime.fromisoformat(prev_game.date.replace("Z", "+00:00"))
                        ).days
                    )
                except Exception:
                    rest_days = None

            rows.append({
                "teamId": team_id,
                "name": self.team_name_zh(conf_key, c.get("team") or {}),
                "side": side,
                "allCount": len(stats),
                "sideCount": len(side_stats),
                "hasTrueAll10": len(stats) >= 10,
                "hasTrueSide10": len(side_stats) >= 10,
                "net10_all": (all10["pf"] - all10["pa"]) if all10 else None,
                "net10_side": (side10["pf"] - side10["pa"]) if side10 else None,
                "net10": (all10["pf"] - all10["pa"]) if all10 else None,
                "net5": (all5["pf"] - all5["pa"]) if all5 else None,
                "net3": (all3["pf"] - all3["pa"]) if all3 else None,
                "sum10": (all10["pf"] + all10["pa"]) if all10 else None,
                "sum5": (all5["pf"] + all5["pa"]) if all5 else None,
                "sum3": (all3["pf"] + all3["pa"]) if all3 else None,
                "pa10": all10["pa"] if all10 else None,
                "pa5": all5["pa"] if all5 else None,
                "pa3": all3["pa"] if all3 else None,
                "restDays": rest_days,
                "restLabel": "未知" if rest_days is None else ("B2B" if rest_days <= 1 else f"休{rest_days-1}天"),
                "all10Avg": all10 or {"pf": 0.0, "pa": 0.0},
                "side10Avg": side10 or {"pf": 0.0, "pa": 0.0},
                "recent10Text": format_recent_games(stats, 10),
            })

        left = rows[0]   # away
        right = rows[1]  # home

        date_key = self.to_taipei_date_key(event["date"])
        away_team = normalize_team_name(league_name, left["name"])
        home_team = normalize_team_name(league_name, right["name"])

        playsport_market = self.playsport_lookup.get(league_name, date_key, away_team, home_team)
        market = {
            "provider": "playsport_universal" if playsport_market else "",
            "total": playsport_market["total"] if playsport_market else None,
            "spread": playsport_market["spread"] if playsport_market else None,
            "favoriteSide": (
                "left" if playsport_market and playsport_market["favorite_side"] == "away"
                else "right" if playsport_market and playsport_market["favorite_side"] == "home"
                else None
            ),
            "leftSpread": playsport_market["away_spread"] if playsport_market else None,
            "rightSpread": playsport_market["home_spread"] if playsport_market else None,
        }

        avg_all_left = left["all10Avg"]
        avg_all_right = right["all10Avg"]

        all_count_enough = bool(left["hasTrueAll10"] and right["hasTrueAll10"])

        final_score_text = self.to_final_score_text(event).replace(":", "-")

        left_pf = avg_all_left["pf"]
        left_pa = avg_all_left["pa"]
        right_pf = avg_all_right["pf"]
        right_pa = avg_all_right["pa"]

        left_net10 = left_pf - left_pa
        right_net10 = right_pf - right_pa
        two_team_total_avg = (left_pf + left_pa + right_pf + right_pa) / 2.0

        if left_pf < left_pa and right_pf < right_pa:
            recent_profile = "雙負分"
        elif left_pf > left_pa and right_pf > right_pa:
            recent_profile = "雙正分"
        else:
            recent_profile = "一正一負"

        net10_diff = left_net10 - right_net10
        net10_diff_abs = abs(net10_diff)
        net10_diff_text = round1(net10_diff_abs)
        if net10_diff_text.endswith(".0"):
            net10_diff_text = net10_diff_text[:-2]

        if net10_diff > 0:
            diff_label = f"客淨值差{net10_diff_text}"
        elif net10_diff < 0:
            diff_label = f"主淨值差{net10_diff_text}"
        else:
            diff_label = "平手"

        spread_text = round1(market["spread"]) if market["spread"] is not None else ""
        if market["favoriteSide"] == "left" and spread_text:
            spread_side_label = f"客讓{spread_text}"
        elif market["favoriteSide"] == "right" and spread_text:
            spread_side_label = f"主讓{spread_text}"
        else:
            spread_side_label = ""

        payload = {
            "比賽ID": str(event.get("id") or ""),
            "聯盟": league_name,
            "賽季": self.get_season_year(league_name, event["date"]),
            "比賽日期": date_key,
            "對戰": f"{left['name']} vs {right['name']}",
            "主隊": right["name"],
            "客隊": left["name"],
            "最終比分": final_score_text,
            "更新時間": self.taipei_now_iso(),
            "讓分盤": existing_payload["讓分盤"] if existing_payload and "讓分盤" in existing_payload else (round1(market["spread"]) if market["spread"] is not None else ""),
            "主客讓分盤": existing_payload["主客讓分盤"] if existing_payload and "主客讓分盤" in existing_payload else spread_side_label,
            "大小分盤": existing_payload["大小分盤"] if existing_payload and "大小分盤" in existing_payload else (round1(market["total"]) if market["total"] is not None else ""),
            "客隊近10場平均得分": round1(left_pf),
            "客隊近10場平均失分": round1(left_pa),
            "客隊近10場平均淨值": round1(left_net10),
            "主隊近10場平均得分": round1(right_pf),
            "主隊近10場平均失分": round1(right_pa),
            "主隊近10場平均淨值": round1(right_net10),
            "客隊近10得分-失分": round1(left_net10),
            "主隊近10得分-失分": round1(right_net10),
            "兩隊總和/2": round1(two_team_total_avg),
            "近10型態": recent_profile,
            "客隊近10場戰績": left["recent10Text"],
            "主隊近10場戰績": right["recent10Text"],
            "淨值差": diff_label,
        }

        should_write = all_count_enough and (not is_doubleheader)
        return payload if should_write else None

    def save_payload(self, payload: Dict[str, Any]) -> bool:
        if self.dry_run:
            self.log("DRY", json.dumps(payload, ensure_ascii=False))
            return True
        try:
            resp = SESSION.post(f"{self.api_base}/save_result", json=payload, timeout=30)
            self.log("SAVE_RESULT", resp.status_code, resp.text[:200])
            resp.raise_for_status()
            data = resp.json()
            return bool(data.get("ok"))
        except Exception as exc:
            self.log("SAVE FAILED", payload.get("比賽ID"), exc)
            return False

    def iterate_dates(self, start_date: date, end_date: date) -> Iterable[date]:
        cur = start_date
        while cur <= end_date:
            yield cur
            cur += timedelta(days=1)

    def run_league(self, league_name: str, start_date: date, end_date: date) -> None:
        self.log(f"=== {league_name} {start_date} -> {end_date} ===")

        existing_rows_cache: Dict[str, Dict[str, Any]] = {}
        for d in self.iterate_dates(start_date, end_date):
            yyyymmdd = d.strftime("%Y%m%d")
            try:
                events = self.get_scoreboard(league_name, yyyymmdd)
            except Exception as exc:
                self.log("SKIP DAY", league_name, yyyymmdd, exc)
                continue

            official_events = [ev for ev in events if self.is_official_game(league_name, ev)]
            if not official_events:
                continue

            for ev in official_events:
                self.scanned += 1
                try:
                    season = self.get_season_year(league_name, ev["date"])
                    if season not in existing_rows_cache:
                        existing_rows_cache[season] = self.fetch_existing_rows(league_name, season, 5000)

                    existing_payload = existing_rows_cache[season].get(str(ev.get("id") or ""))
                    payload = self.build_rows_for_event(league_name, ev, existing_payload)
                    if not payload:
                        continue

                    ok = self.save_payload(payload)
                    if ok:
                        self.saved += 1
                    else:
                        self.failed += 1
                except Exception as exc:
                    self.failed += 1
                    self.log("EVENT FAILED", league_name, yyyymmdd, ev.get("id"), exc)

                if self.sleep_seconds > 0:
                    time.sleep(self.sleep_seconds)

    def run(self, leagues: List[str], start_date: Optional[date], end_date: Optional[date]) -> None:
        today = datetime.now(TAIPEI).date()
        for league_name in leagues:
            conf = CONFIG[league_name]
            league_start = max(start_date, conf["season_start"]) if start_date else conf["season_start"]
            league_end = end_date or today
            if league_end < league_start:
                continue
            self.run_league(league_name, league_start, league_end)

        self.log(f"DONE scanned={self.scanned} saved={self.saved} failed={self.failed}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Historical backfill with playsport universal market")
    parser.add_argument("--api-base", default="https://nba-mlb-cloud.onrender.com")
    parser.add_argument("--league", choices=["NBA", "MLB", "ALL"], default="ALL")
    parser.add_argument("--start-date", help="YYYY-MM-DD")
    parser.add_argument("--end-date", help="YYYY-MM-DD")
    parser.add_argument("--mlb-csv", default="playsport_mlb_2020_now.csv")
    parser.add_argument("--nba-csv", default="playsport_nba_2020_now.csv")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=1.2)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def parse_date_arg(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d").date()


def main() -> int:
    args = parse_args()

    lookup = PlaysportLookup(
        mlb_csv=args.mlb_csv if args.league in {"MLB", "ALL"} else None,
        nba_csv=args.nba_csv if args.league in {"NBA", "ALL"} else None,
    )

    leagues = [args.league] if args.league in {"NBA", "MLB"} else ["NBA", "MLB"]

    runner = BackfillRunner(
        api_base=args.api_base,
        playsport_lookup=lookup,
        dry_run=args.dry_run,
        sleep_seconds=args.sleep,
        verbose=not args.quiet,
    )
    runner.run(leagues, parse_date_arg(args.start_date), parse_date_arg(args.end_date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
