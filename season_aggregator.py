# -*- coding: utf-8 -*-
"""
시즌 누적 집계기 — 경기별 스냅샷 → 시즌 단위 누적 + per 90

  · 선수: 전 지표 합산 + 경기 수 + FPL 출전시간(canonical 이름 매칭) 조인
          → 핵심 지표 per 90 산출 (기관급 표준 기준)
  · 팀  : 전 지표 합산 + 경기당 평균
  · 출전시간 없는 선수는 per-match 값으로 대체하고 basis 필드에 명시 (정직한 표기)

출력: data/metrics/season_players.json / season_teams.json
"""
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from normalizer import normalize_name  # noqa: E402

PER90_METRICS = ('xG', 'npxG', 'xA', 'xT', 'VAEP', 'SCA', 'GCA',
                 'key_passes', 'progressive_passes', 'progressive_carries',
                 'tackles', 'interceptions', 'recoveries', 'pressures')


def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _fpl_minutes(path='data/master/players_pl.json'):
    """정규화된 이름 → FPL 출전시간(분)."""
    return {normalize_name(p.get('name', '')): p.get('minutes') or 0
            for p in _load(path, {}).values()}


def aggregate(metrics_dir='data/metrics'):
    players = defaultdict(lambda: defaultdict(float))
    teams = defaultdict(lambda: defaultdict(float))
    p_matches, t_matches = defaultdict(int), defaultdict(int)
    p_team = {}

    for path in glob.glob(os.path.join(metrics_dir, '*_metrics.json')):
        data = _load(path, {})
        for t, stats in data.get('teams', {}).items():
            t_matches[t] += 1
            for k, v in stats.items():
                if isinstance(v, (int, float)):
                    teams[t][k] += v
        for p, stats in data.get('players', {}).items():
            p_matches[p] += 1
            if stats.get('_team'):
                p_team[p] = stats['_team']
            for k, v in stats.items():
                if isinstance(v, (int, float)):
                    players[p][k] += v
    return players, teams, p_matches, t_matches, p_team


def build(metrics_dir='data/metrics', out_dir='data/metrics'):
    players, teams, p_matches, t_matches, p_team = aggregate(metrics_dir)
    minutes = _fpl_minutes()

    season_players = {}
    for p, totals in players.items():
        n = p_matches[p]
        mins = minutes.get(normalize_name(p), 0)
        per90 = {}
        if mins >= 90:
            basis = f'per90 (FPL {mins}분 기준)'
            for m in PER90_METRICS:
                if m in totals:
                    per90[m] = round(totals[m] / mins * 90, 3)
        else:
            basis = f'per_match (출전시간 미확보, {n}경기 기준)'
            for m in PER90_METRICS:
                if m in totals:
                    per90[m] = round(totals[m] / n, 3)
        season_players[p] = {
            'team': p_team.get(p),
            'matches': n,
            'minutes_fpl': mins or None,
            'totals': {k: round(v, 3) for k, v in totals.items()
                       if not k.startswith('_')},
            'per90': per90,
            'per90_basis': basis,
        }

    season_teams = {}
    for t, totals in teams.items():
        n = t_matches[t]
        season_teams[t] = {
            'matches': n,
            'totals': {k: round(v, 3) for k, v in totals.items()},
            'per_match': {k: round(v / n, 3) for k, v in totals.items()},
        }

    os.makedirs(out_dir, exist_ok=True)
    for name, obj in (('season_players.json', season_players),
                      ('season_teams.json', season_teams)):
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=4, ensure_ascii=False)
    print(f'[season] 선수 {len(season_players)}명 / 팀 {len(season_teams)}팀 '
          f'시즌 누적 집계 완료')
    return season_players, season_teams


if __name__ == '__main__':
    build()
