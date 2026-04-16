#!/usr/bin/env python3
"""
NBA + MLB historical batch backfill (2020 -> now)

What it does
- Walks day by day through ESPN scoreboard for NBA and MLB
- Excludes preseason / spring / exhibition games
- Rebuilds the same recommendation fields used by the current web app
- Computes result fields when final scores and market lines exist
- Writes rows to the existing cloud API /save_result endpoint

Why this script exists
- The page-based flow is good for today/tomorrow, but not for full historical rebuilds.
- This script is independent of whether a game still appears on the webpage.

Free-data limitation
- It can only backfill seasons/dates that ESPN's public scoreboard endpoints still return.
- If a season/date is unavailable from the free endpoint, the script logs it and continues.

Usage examples
  python historical_backfill_nba_mlb_2020_now.py --api-base https://nba-mlb-cloud.onrender.com
  python historical_backfill_nba_mlb_2020_now.py --league NBA --start-date 2024-10-01 --end-date 2025-04-15
  python historical_backfill_nba_mlb_2020_now.py --dry-run --league MLB --start-date 2020-07-20 --end-date 2020-08-10

Notes
- This preserves the existing API contract by POSTing the same payload shape to /save_result.
- It does not require editing the current frontend.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

TAIPEI = timezone(timedelta(hours=8))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "history-backfill/1.0"})

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
    "nba": {'Atlanta Hawks':'亞特蘭大老鷹','Boston Celtics':'波士頓塞爾提克','Brooklyn Nets':'布魯克林籃網','Charlotte Hornets':'夏洛特黃蜂','Chicago Bulls':'芝加哥公牛','Cleveland Cavaliers':'克里夫蘭騎士','Dallas Mavericks':'達拉斯獨行俠','Denver Nuggets':'丹佛金塊','Detroit Pistons':'底特律活塞','Golden State Warriors':'金州勇士','Houston Rockets':'休士頓火箭','Indiana Pacers':'印第安納溜馬','LA Clippers':'洛杉磯快艇','Los Angeles Clippers':'洛杉磯快艇','Los Angeles Lakers':'洛杉磯湖人','LA Lakers':'洛杉磯湖人','Memphis Grizzlies':'曼菲斯灰熊','Miami Heat':'邁阿密熱火','Milwaukee Bucks':'密爾瓦基公鹿','Minnesota Timberwolves':'明尼蘇達灰狼','New Orleans Pelicans':'紐奧良鵜鶘','New York Knicks':'紐約尼克','Oklahoma City Thunder':'奧克拉荷馬雷霆','Orlando Magic':'奧蘭多魔術','Philadelphia 76ers':'費城76人','Phoenix Suns':'鳳凰城太陽','Portland Trail Blazers':'波特蘭拓荒者','Sacramento Kings':'沙加緬度國王','San Antonio Spurs':'聖安東尼奧馬刺','Toronto Raptors':'多倫多暴龍','Utah Jazz':'猶他爵士','Washington Wizards':'華盛頓巫師'},
    "mlb": {'Arizona Diamondbacks':'亞利桑那響尾蛇','Athletics':'運動家','Oakland Athletics':'奧克蘭運動家','Atlanta Braves':'亞特蘭大勇士','Baltimore Orioles':'巴爾的摩金鶯','Boston Red Sox':'波士頓紅襪','Chicago Cubs':'芝加哥小熊','Chicago White Sox':'芝加哥白襪','Cincinnati Reds':'辛辛那提紅人','Cleveland Guardians':'克里夫蘭守護者','Colorado Rockies':'科羅拉多落磯','Detroit Tigers':'底特律老虎','Houston Astros':'休士頓太空人','Kansas City Royals':'堪薩斯市皇家','Los Angeles Angels':'洛杉磯天使','Los Angeles Dodgers':'洛杉磯道奇','Miami Marlins':'邁阿密馬林魚','Milwaukee Brewers':'密爾瓦基釀酒人','Minnesota Twins':'明尼蘇達雙城','New York Mets':'紐約大都會','New York Yankees':'紐約洋基','Philadelphia Phillies':'費城費城人','Pittsburgh Pirates':'匹茲堡海盜','San Diego Padres':'聖地牙哥教士','San Francisco Giants':'舊金山巨人','Seattle Mariners':'西雅圖水手','St. Louis Cardinals':'聖路易紅雀','Tampa Bay Rays':'坦帕灣光芒','Texas Rangers':'德州遊騎兵','Toronto Blue Jays':'多倫多藍鳥','Washington Nationals':'華盛頓國民'}
}


@dataclass
class GameStat:
    pf: float
    pa: float
    date: str
    game_ms: int
    game_id: str
    home_away: str


class BackfillRunner:
    def __init__(
        self,
        api_base: str,
        dry_run: bool = False,
        sleep_seconds: float = 0.0,
        verbose: bool = True,
    ) -> None:
        self.api_base = api_base.rstrip("/")
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

    def fetch_json(self, url: str) -> Dict[str, Any]:
        resp = SESSION.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

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
                continue
        raise RuntimeError(f"scoreboard fetch failed {league_name} {yyyymmdd}: {last_err}")

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
            return True
        if t in {"2", "3", "4"}:
            return True
        official_terms = ("regular", "post", "playoff", "play-in", "playin", "final")
        reject_terms = ("pre", "spring", "exhibition")
        if any(term in t for term in official_terms):
            return True
        if any(term in t for term in reject_terms):
            return False
        return True

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
        return GameStat(pf=pf, pa=pa, date=event["date"], game_ms=game_ms, game_id=str(event.get("id")), home_away=mine.get("homeAway") or "")

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
            if len(out) >= 12:
                break
        out.sort(key=lambda g: g.game_ms, reverse=True)
        result = out[:10]
        self.team_games_cache[cache_key] = result
        return result

    @staticmethod
    def avg(nums: List[float]) -> float:
        return sum(nums) / len(nums) if nums else 0.0

    def get_window_averages(self, games: List[GameStat], count: int, side: Optional[str] = None) -> Optional[Dict[str, float]]:
        filtered = games if not side else [g for g in games if g.home_away == side]
        sl = filtered[:count]
        if not sl:
            return None
        return {"pf": self.avg([g.pf for g in sl]), "pa": self.avg([g.pa for g in sl])}

    @staticmethod
    def to_num(v: Any) -> Optional[float]:
        if v in (None, ""):
            return None
        if isinstance(v, (int, float)):
            return float(v) if math.isfinite(float(v)) else None
        s = "".join(ch for ch in str(v) if ch in "+-.0123456789")
        if s in {"", "+", "-"}:
            return None
        try:
            n = float(s)
            return n if math.isfinite(n) else None
        except Exception:
            return None

    def get_team_aliases(self, team: Dict[str, Any]) -> List[str]:
        raw = [
            team.get("displayName"), team.get("shortDisplayName"), team.get("name"),
            team.get("abbreviation"), team.get("location"), team.get("nickname"),
        ]
        out: set[str] = set()
        for v in raw:
            if not v:
                continue
            s = str(v).strip().lower()
            if not s:
                continue
            out.add(s)
            out.update(p for p in s.split() if p)
        return list(out)

    def text_has_team_alias(self, text: str, team: Dict[str, Any]) -> bool:
        src = (text or "").lower()
        return any(alias in src for alias in self.get_team_aliases(team))

    def get_team_side_spread(self, odds: Dict[str, Any], competitor: Dict[str, Any]) -> Optional[float]:
        side = competitor.get("homeAway")
        side_odds = odds.get("awayTeamOdds") if side == "away" else (odds.get("homeTeamOdds") if side == "home" else None)
        candidates = [
            (side_odds or {}).get("spread"), (side_odds or {}).get("pointSpread"), (side_odds or {}).get("line"),
            (competitor.get("odds") or {}).get("spread"), (competitor.get("odds") or {}).get("pointSpread"),
            ((competitor.get("linescores") or [{}])[0]).get("spread"),
        ]
        for v in candidates:
            n = self.to_num(v)
            if n is not None:
                return n
        return None

    def extract_market(self, event: Dict[str, Any], left_comp: Dict[str, Any], right_comp: Dict[str, Any]) -> Dict[str, Any]:
        comp = (event.get("competitions") or [{}])[0]
        odds_list = []
        if isinstance(comp.get("odds"), list):
            odds_list.extend(comp["odds"])
        if isinstance(event.get("odds"), list):
            odds_list.extend(event["odds"])
        market = {
            "provider": "",
            "total": None,
            "spread": None,
            "favoriteSide": None,
            "leftSpread": None,
            "rightSpread": None,
            "details": "",
        }
        for odds in odds_list:
            provider = ((odds.get("provider") or {}).get("name") or (odds.get("provider") or {}).get("id") or odds.get("provider") or "")
            details = odds.get("details") or odds.get("displayValue") or ""
            totals = [
                odds.get("overUnder"), odds.get("overunder"), odds.get("total"), odds.get("totalPoints"),
                odds.get("overUnderLine"), odds.get("underOver"),
            ]
            total = next((self.to_num(v) for v in totals if self.to_num(v) is not None), None)
            left_spread = self.get_team_side_spread(odds, left_comp)
            right_spread = self.get_team_side_spread(odds, right_comp)
            if left_spread is None and right_spread is None:
                raw_spread = next((self.to_num(v) for v in [odds.get("spread"), odds.get("pointSpread"), odds.get("line")] if self.to_num(v) is not None), None)
                if raw_spread is not None:
                    if self.text_has_team_alias(details, left_comp.get("team") or {}):
                        left_spread = raw_spread if raw_spread < 0 else -abs(raw_spread)
                        right_spread = -left_spread
                    elif self.text_has_team_alias(details, right_comp.get("team") or {}):
                        right_spread = raw_spread if raw_spread < 0 else -abs(raw_spread)
                        left_spread = -right_spread
            if market["total"] is None and total is not None:
                market["total"] = total
            if market["leftSpread"] is None and left_spread is not None:
                market["leftSpread"] = left_spread
            if market["rightSpread"] is None and right_spread is not None:
                market["rightSpread"] = right_spread
            if not market["provider"] and provider:
                market["provider"] = provider
            if not market["details"] and details:
                market["details"] = details
        if market["leftSpread"] is not None and market["rightSpread"] is None:
            market["rightSpread"] = -market["leftSpread"]
        if market["rightSpread"] is not None and market["leftSpread"] is None:
            market["leftSpread"] = -market["rightSpread"]
        ls = market["leftSpread"]
        rs = market["rightSpread"]
        if ls is not None and rs is not None:
            if ls < rs:
                market["favoriteSide"] = "left"
                market["spread"] = abs(ls)
            elif rs < ls:
                market["favoriteSide"] = "right"
                market["spread"] = abs(rs)
            elif ls < 0:
                market["favoriteSide"] = "left"
                market["spread"] = abs(ls)
        return market

    @staticmethod
    def get_weighted_value(values: List[Optional[float]]) -> Optional[float]:
        weights = [0.5, 0.3, 0.2]
        total = 0.0
        weight_sum = 0.0
        for i, v in enumerate(values):
            if v is None:
                continue
            total += float(v) * weights[i]
            weight_sum += weights[i]
        return total / weight_sum if weight_sum else None

    def get_recent_state(self, row: Dict[str, Any]) -> Dict[str, Any]:
        net10 = self.to_num(row.get("net10"))
        net5 = self.to_num(row.get("net5"))
        net3 = self.to_num(row.get("net3"))
        return {
            "net10": net10,
            "net5": net5,
            "net3": net3,
            "positive10": net10 is not None and net10 > 0,
            "positive5": net5 is not None and net5 > 0,
            "positive3": net3 is not None and net3 > 0,
            "overheatingUp": net3 is not None and net10 is not None and (net3 - net10) >= 6,
            "overheatingDown": net3 is not None and net10 is not None and (net10 - net3) >= 6,
        }

    def build_bet_suggestion(self, league_name: str, left: Dict[str, Any], right: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
        conf = CONFIG[league_name]
        left_state = self.get_recent_state(left)
        right_state = self.get_recent_state(right)
        left_sum_w = self.get_weighted_value([self.to_num(left.get("sum10")), self.to_num(left.get("sum5")), self.to_num(left.get("sum3"))])
        right_sum_w = self.get_weighted_value([self.to_num(right.get("sum10")), self.to_num(right.get("sum5")), self.to_num(right.get("sum3"))])
        left_net_w = self.get_weighted_value([self.to_num(left.get("net10")), self.to_num(left.get("net5")), self.to_num(left.get("net3"))])
        right_net_w = self.get_weighted_value([self.to_num(right.get("net10")), self.to_num(right.get("net5")), self.to_num(right.get("net3"))])
        extra_reason = ""
        if league_name == "NBA":
            def rest_adjust(days: Optional[int]) -> float:
                if days is None:
                    return 0.0
                if days <= 1:
                    return -1.5
                if days >= 3:
                    return 1.0
                return 0.0
            if left_net_w is not None:
                left_net_w += rest_adjust(left.get("restDays"))
            if right_net_w is not None:
                right_net_w += rest_adjust(right.get("restDays"))
            extra_reason = f"{left.get('name','左隊')} {left.get('restLabel','未知')} / {right.get('name','右隊')} {right.get('restLabel','未知')}"
        if league_name == "MLB":
            left_pa = self.get_weighted_value([left.get("pa10"), left.get("pa5"), left.get("pa3")])
            right_pa = self.get_weighted_value([right.get("pa10"), right.get("pa5"), right.get("pa3")])
            if left_pa is not None and right_pa is not None:
                pitching_bias = ((left_pa + right_pa) / 2 - 4.2) * 0.6
                if left_sum_w is not None:
                    left_sum_w += pitching_bias
                if right_sum_w is not None:
                    right_sum_w += pitching_bias
                extra_reason = "MLB 已納入失分代理投手影響"
        predicted_total = ((left_sum_w + right_sum_w) / 2) if (left_sum_w is not None and right_sum_w is not None) else None
        predicted_margin = (left_net_w - right_net_w) if (left_net_w is not None and right_net_w is not None) else None
        total_gap = (predicted_total - market["total"]) if (predicted_total is not None and market["total"] is not None) else None
        tempo_gap = abs(left_sum_w - right_sum_w) if (left_sum_w is not None and right_sum_w is not None) else None
        profile = "混合盤"
        if left_state["positive10"] and right_state["positive10"]:
            profile = "雙正分"
        elif (not left_state["positive10"]) and (not right_state["positive10"]):
            profile = "雙負分"
        else:
            profile = "一強一弱"
        spread_gap = None
        spread_suggestion = "PASS"
        if predicted_margin is not None and market["spread"] is not None and market["favoriteSide"]:
            if market["favoriteSide"] == "left":
                spread_gap = predicted_margin - market["spread"]
                if spread_gap >= conf["spread_edge"]:
                    spread_suggestion = f"{left['name']} 讓分"
                elif spread_gap <= -conf["spread_edge"]:
                    spread_suggestion = f"{right['name']} 受讓"
            else:
                predicted_right_margin = -predicted_margin
                spread_gap = predicted_right_margin - market["spread"]
                if spread_gap >= conf["spread_edge"]:
                    spread_suggestion = f"{right['name']} 讓分"
                elif spread_gap <= -conf["spread_edge"]:
                    spread_suggestion = f"{left['name']} 受讓"
        total_suggestion = "PASS"
        if total_gap is not None:
            if total_gap >= conf["total_edge"]:
                total_suggestion = "大分"
            elif total_gap <= -conf["total_edge"]:
                total_suggestion = "小分"
        overheat_up = left_state["overheatingUp"] or right_state["overheatingUp"]
        overheat_down = left_state["overheatingDown"] or right_state["overheatingDown"]
        high_total = market["total"] is not None and market["total"] >= conf["high_total"]
        final_suggestion = "PASS"
        reason = "未達下注門檻"
        if profile == "雙正分":
            if total_suggestion == "大分" and not overheat_up and (tempo_gap is None or tempo_gap <= (12 if league_name == "NBA" else 2.2)) and not high_total:
                final_suggestion = "大分"
                reason = "雙正分，主打大小分；edge 達標且未過熱"
            elif spread_suggestion != "PASS" and abs(spread_gap or 0) >= conf["spread_edge"] + 1:
                final_suggestion = spread_suggestion
                reason = "雙正分但讓分 edge 更大"
            elif total_suggestion == "小分":
                final_suggestion = "小分"
                reason = "雙正分但盤口開過高，偏反打"
        elif profile == "雙負分":
            if total_suggestion == "小分" and not overheat_down:
                final_suggestion = "小分"
                reason = "雙負分，主打小分"
            elif spread_suggestion != "PASS" and abs(spread_gap or 0) >= conf["spread_edge"] + 1:
                final_suggestion = spread_suggestion
                reason = "雙負分但讓分 edge 較大"
        else:
            if spread_suggestion != "PASS" and not overheat_up and not overheat_down:
                final_suggestion = spread_suggestion
                reason = "一強一弱，主打讓分"
            elif total_suggestion != "PASS" and (tempo_gap is None or tempo_gap <= (10 if league_name == "NBA" else 1.8)):
                final_suggestion = total_suggestion
                reason = "一強一弱但大小分 edge 較明顯"
        display_edge = None
        edge_text = "edge：無"
        if final_suggestion in {"大分", "小分"}:
            display_edge = total_gap
            edge_text = f"大小 edge：{total_gap:+.1f}" if total_gap is not None else "大小 edge：無"
        elif "讓分" in final_suggestion or "受讓" in final_suggestion:
            display_edge = spread_gap
            edge_text = f"讓分 edge：{spread_gap:+.1f}" if spread_gap is not None else "讓分 edge：無"
        if extra_reason and final_suggestion != "PASS":
            reason += f"｜{extra_reason}"
        return {
            "finalSuggestion": final_suggestion,
            "displayEdge": display_edge,
            "reason": reason,
        }

    @staticmethod
    def round1(n: Optional[float]) -> str:
        if n is None:
            return ""
        return f"{round(float(n), 1):.1f}"

    def model_spread_pick(self, left_avg: Dict[str, float], right_avg: Dict[str, float], market: Dict[str, Any], left_name: str, right_name: str) -> str:
        left_net = left_avg["pf"] - left_avg["pa"]
        right_net = right_avg["pf"] - right_avg["pa"]
        if left_net == right_net:
            return "PASS"
        if market["favoriteSide"] == "left":
            return f"{left_name} 讓分" if left_net > right_net else f"{right_name} 受讓"
        if market["favoriteSide"] == "right":
            return f"{right_name} 讓分" if right_net > left_net else f"{left_name} 受讓"
        return f"{left_name} 讓分" if left_net > right_net else f"{right_name} 讓分"

    def model_total_by_net(self, left_avg: Dict[str, float], right_avg: Dict[str, float]) -> str:
        left_net = left_avg["pf"] - left_avg["pa"]
        right_net = right_avg["pf"] - right_avg["pa"]
        high_score_team = "left" if left_avg["pf"] >= right_avg["pf"] else "right"
        high_net_team = "left" if left_net >= right_net else "right"
        return "小分" if high_score_team == high_net_team else "大分"

    def model_total_by_sum(self, left_avg: Dict[str, float], right_avg: Dict[str, float]) -> str:
        left_sum = left_avg["pf"] + left_avg["pa"]
        right_sum = right_avg["pf"] + right_avg["pa"]
        high_score_team = "left" if left_avg["pf"] >= right_avg["pf"] else "right"
        high_sum = left_sum if high_score_team == "left" else right_sum
        low_sum = right_sum if high_score_team == "left" else left_sum
        return "小分" if high_sum > low_sum else "大分"

    def judge_pick_result(self, event: Dict[str, Any], market: Dict[str, Any], pick: str, left_name: str, right_name: str) -> str:
        comp = (event.get("competitions") or [{}])[0]
        competitors = sorted(comp.get("competitors") or [], key=lambda c: 0 if c.get("homeAway") == "away" else 1)
        if len(competitors) < 2 or not ((event.get("status") or {}).get("type") or {}).get("completed") or not pick or pick == "PASS":
            return ""
        try:
            left_score = float(competitors[0].get("score"))
            right_score = float(competitors[1].get("score"))
        except Exception:
            return ""
        diff = left_score - right_score
        total = left_score + right_score
        if pick == "大分":
            if market["total"] is None:
                return ""
            return "WIN" if total > market["total"] else ("LOSE" if total < market["total"] else "PUSH")
        if pick == "小分":
            if market["total"] is None:
                return ""
            return "WIN" if total < market["total"] else ("LOSE" if total > market["total"] else "PUSH")
        if pick in {f"{left_name} 讓分", f"{left_name} 受讓"} and market["leftSpread"] is not None:
            adj = diff + market["leftSpread"]
            return "WIN" if adj > 0 else ("LOSE" if adj < 0 else "PUSH")
        if pick in {f"{right_name} 讓分", f"{right_name} 受讓"} and market["rightSpread"] is not None:
            adj = (-diff) + market["rightSpread"]
            return "WIN" if adj > 0 else ("LOSE" if adj < 0 else "PUSH")
        return ""

    def build_rows_for_event(self, league_name: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.is_official_game(league_name, event):
            return None
        comp = (event.get("competitions") or [{}])[0]
        competitors = sorted(comp.get("competitors") or [], key=lambda c: 0 if c.get("homeAway") == "away" else 1)
        if len(competitors) < 2:
            return None
        conf_key = CONFIG[league_name]["key"]
        rows = []
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
                later = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                earlier = datetime.fromisoformat(prev_game.date.replace("Z", "+00:00"))
                rest_days = math.floor((later - earlier).total_seconds() / 86400)
            rows.append({
                "teamId": team_id,
                "name": self.team_name_zh(conf_key, c.get("team") or {}),
                "side": "主隊" if side == "home" else ("客隊" if side == "away" else ""),
                "rawGames": stats,
                "allCount": len(stats),
                "sideCount": len(side_stats),
                "net10_all": (all10["pf"] - all10["pa"]) if all10 else None,
                "net10_side": (side10["pf"] - side10["pa"]) if side10 else None,
                "sum10": round((side10["pf"] + side10["pa"]), 1) if side10 else None,
                "sum5": round((all5["pf"] + all5["pa"]), 1) if all5 else None,
                "sum3": round((all3["pf"] + all3["pa"]), 1) if all3 else None,
                "net10": f"{(side10['pf']-side10['pa']):+.1f}" if side10 else "-",
                "net5": f"{(all5['pf']-all5['pa']):+.1f}" if all5 else "-",
                "net3": f"{(all3['pf']-all3['pa']):+.1f}" if all3 else "-",
                "pa10": all10["pa"] if all10 else None,
                "pa5": all5["pa"] if all5 else None,
                "pa3": all3["pa"] if all3 else None,
                "restDays": rest_days,
                "restLabel": ("未知" if rest_days is None else ("B2B" if rest_days <= 1 else ("休1天" if rest_days == 2 else ("休2天" if rest_days == 3 else f"休{rest_days-1}天")))),
                "all10Avg": all10,
                "side10Avg": side10,
            })
        left = rows[0]
        right = rows[1]
        market = self.extract_market(event, competitors[0], competitors[1])
        suggestion = self.build_bet_suggestion(league_name, left, right, market)
        avg_all_left = left["all10Avg"] or {"pf": 0.0, "pa": 0.0}
        avg_all_right = right["all10Avg"] or {"pf": 0.0, "pa": 0.0}
        avg_side_left = left["side10Avg"] or {"pf": 0.0, "pa": 0.0}
        avg_side_right = right["side10Avg"] or {"pf": 0.0, "pa": 0.0}
        all_count_enough = left["allCount"] >= 10 and right["allCount"] >= 10
        side_count_enough = left["sideCount"] >= 10 and right["sideCount"] >= 10
        all_eligible = all_count_enough and (left["net10_all"] or 0) > 0 and (right["net10_all"] or 0) > 0
        side_eligible = side_count_enough and (left["net10_side"] or 0) > 0 and (right["net10_side"] or 0) > 0
        min_edge = math.inf
        final_suggestion = suggestion["finalSuggestion"]
        if league_name == "NBA":
            if final_suggestion in {"大分", "小分"}:
                min_edge = 10.0
            elif "讓分" in final_suggestion or "受讓" in final_suggestion:
                min_edge = 6.0
        else:
            if final_suggestion in {"大分", "小分"} or "讓分" in final_suggestion or "受讓" in final_suggestion:
                min_edge = 3.0
        edge_rank = abs(float(suggestion["displayEdge"] or 0))
        show_strong = final_suggestion != "PASS" and edge_rank >= min_edge
        date_key = self.to_taipei_date_key(event["date"])
        game_id = str(event.get("id") or f"{conf_key}_{date_key}_{left['name']}_{right['name']}")
        model_picks = {
            '近10讓分推薦': self.model_spread_pick(avg_all_left, avg_all_right, market, left['name'], right['name']) if all_eligible else 'PASS',
            '近10大小_淨值推薦': self.model_total_by_net(avg_all_left, avg_all_right) if all_eligible else 'PASS',
            '近10大小_相加推薦': self.model_total_by_sum(avg_all_left, avg_all_right) if all_eligible else 'PASS',
            '主客讓分推薦': self.model_spread_pick(avg_side_left, avg_side_right, market, left['name'], right['name']) if side_eligible else 'PASS',
            '主客大小_淨值推薦': self.model_total_by_net(avg_side_left, avg_side_right) if side_eligible else 'PASS',
            '主客大小_相加推薦': self.model_total_by_sum(avg_side_left, avg_side_right) if side_eligible else 'PASS',
            'EDGE讓分推薦': final_suggestion if (show_strong and ("讓分" in final_suggestion or "受讓" in final_suggestion)) else 'PASS',
            'EDGE大小推薦': final_suggestion if (show_strong and final_suggestion in {'大分', '小分'}) else 'PASS',
        }
        model_results = {
            '近10讓分結果': self.judge_pick_result(event, market, model_picks['近10讓分推薦'], left['name'], right['name']),
            '近10大小_淨值結果': self.judge_pick_result(event, market, model_picks['近10大小_淨值推薦'], left['name'], right['name']),
            '近10大小_相加結果': self.judge_pick_result(event, market, model_picks['近10大小_相加推薦'], left['name'], right['name']),
            '主客讓分結果': self.judge_pick_result(event, market, model_picks['主客讓分推薦'], left['name'], right['name']),
            '主客大小_淨值結果': self.judge_pick_result(event, market, model_picks['主客大小_淨值推薦'], left['name'], right['name']),
            '主客大小_相加結果': self.judge_pick_result(event, market, model_picks['主客大小_相加推薦'], left['name'], right['name']),
            'EDGE讓分結果': self.judge_pick_result(event, market, model_picks['EDGE讓分推薦'], left['name'], right['name']),
            'EDGE大小結果': self.judge_pick_result(event, market, model_picks['EDGE大小推薦'], left['name'], right['name']),
        }
        payload = {
            '比賽ID': game_id,
            '聯盟': league_name,
            '賽季': self.get_season_year(league_name, event['date']),
            '比賽日期': date_key,
            '對戰': f"{right['name']} vs {left['name']}",
            '主隊': right['name'],
            '客隊': left['name'],
            '最終比分': self.to_final_score_text(event),
            '判定時間': self.taipei_now_iso(),
            '更新時間': self.taipei_now_iso(),
            '讓分盤': self.round1(market['spread']) if market['spread'] is not None else '',
            '大小分盤': self.round1(market['total']) if market['total'] is not None else '',
            'EDGE讓分值': self.round1(abs(float(suggestion['displayEdge']))) if model_picks['EDGE讓分推薦'] != 'PASS' and suggestion['displayEdge'] is not None else '',
            'EDGE大小值': self.round1(abs(float(suggestion['displayEdge']))) if model_picks['EDGE大小推薦'] != 'PASS' and suggestion['displayEdge'] is not None else '',
            **model_picks,
            **model_results,
        }
        return payload

    def to_final_score_text(self, event: Dict[str, Any]) -> str:
        competitors = sorted(((event.get("competitions") or [{}])[0].get("competitors") or []), key=lambda c: 0 if c.get("homeAway") == "away" else 1)
        if len(competitors) < 2:
            return ""
        try:
            left_score = int(float(competitors[0].get("score")))
            right_score = int(float(competitors[1].get("score")))
            return f"{left_score}:{right_score}"
        except Exception:
            return ""

    def save_payload(self, payload: Dict[str, Any]) -> bool:
        if self.dry_run:
            self.log("DRY", payload["聯盟"], payload["比賽日期"], payload["比賽ID"], payload["對戰"], payload.get("EDGE讓分推薦"), payload.get("EDGE大小推薦"))
            return True
        url = f"{self.api_base}/save_result"
        try:
            resp = SESSION.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(str(data))
            return True
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
            self.log(f"{league_name} {yyyymmdd} events={len(official_events)}")
            for ev in official_events:
                self.scanned += 1
                try:
                    payload = self.build_rows_for_event(league_name, ev)
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
            league_start = start_date or conf["season_start"]
            league_end = end_date or today
            if league_end < league_start:
                continue
            self.run_league(league_name, league_start, league_end)
        self.log(f"DONE scanned={self.scanned} saved={self.saved} failed={self.failed}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NBA + MLB 2020->now historical backfill")
    parser.add_argument("--api-base", default=os.getenv("API_BASE", "https://nba-mlb-cloud.onrender.com"))
    parser.add_argument("--league", choices=["NBA", "MLB", "ALL"], default="ALL")
    parser.add_argument("--start-date", help="YYYY-MM-DD")
    parser.add_argument("--end-date", help="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to sleep between saves")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def parse_date_arg(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d").date()


def main() -> int:
    args = parse_args()
    leagues = [args.league] if args.league in {"NBA", "MLB"} else ["NBA", "MLB"]
    runner = BackfillRunner(api_base=args.api_base, dry_run=args.dry_run, sleep_seconds=args.sleep, verbose=not args.quiet)
    runner.run(leagues, parse_date_arg(args.start_date), parse_date_arg(args.end_date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
