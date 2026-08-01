# -*- coding: utf-8 -*-
"""
compute_card_suspensions.py
2026-08-01 — 리그별 실제 옐로카드 누적 출장정지 규정을 반영해서 결장
위험을 판정한다(사용자가 9개 리그 규정을 직접 조사해서 제공한 내용
그대로 반영 — 추측 아님).

## 리그별 규정 (2025-26시즌 기준, 사용자 제공)
- EPL/챔피언십: 5장→1경기, 10장→2경기, 15장→3경기 (그 이후 규정 없음)
- 라리가/세리에A: 5→10→14→17→19장마다 정지, 19장 이후로는 매 장마다
- 분데스리가/리그앙: 5의 배수(5,10,15,20...)마다 무조건 1경기
- 에레디비시: 5,7,9번째 및 그 이후 매 카드마다
- 엘리테세리엔: 4번째, 이후 2장마다(4-2-2 방식)
- MLS: 5,8,11,13번째, 그 이후 2장마다

## ⚠️ 알고 있는 한계 (정직하게 명시)
1. **경기 수 제한 무시**: EPL "19경기 이내 5장" 같은 "N경기 이내" 조건은
   구현 안 함 — 시즌 누적 카드 수만 본다(경기 수 윈도우까지 추적하려면
   훨씬 복잡해지고 버그 위험이 커서 의도적으로 단순화함). 대부분의
   경우 결과는 같지만, 정확히 그 경계에서는 실제 규정과 다를 수 있음.
2. **컵대회 카드 미분리**: EPL은 FA컵/리그컵 카드를 별도 집계하는데,
   지금 이 스크립트는 대회 구분 없이 BSD가 주는 모든 경기 카드를 합산함
   — EPL 선수는 실제보다 카드 수가 많게 잡힐 수 있음(컵 경기 참여 시).
3. **시즌 중 리셋 미반영**: 에레디비시(플레이오프 전/시즌 시작), MLS
   (플레이오프, 굿비헤이비어 차감) 같은 시즌 중 리셋 규정은 반영 안 됨
   — 시즌 후반부일수록 실제보다 과대 카운트될 수 있음.
4. **"정지가 이미 소화됐는지" 추적 불가**: 각 경기별로 그 선수가 실제
   출전했는지/결장했는지 데이터가 없어서, "누적 카드 수가 정확히 정지
   기준값과 일치하는 시점"만 결장 위험으로 표시한다. 기준값을 이미
   넘어서(정지를 소화하고) 새 카드가 다음 기준값에 도달하기 전이면
   표시 안 함 — 이건 근사치이지 확정 판정이 아니다.

## API 호출 없음 (이전과 동일)
"""
import glob
import json
import os
import re
import sqlite3
import unicodedata

DB_PATH = 'data/football.db'
METRICS_DIR = 'data/metrics'
OUT_PATH = 'data/master/card_suspensions.json'

# 2026-08-01: 사용자가 조사해서 제공한 리그별 규정을 그대로 반영.
# explicit: 정확히 이 카드수에서 정지 트리거. repeat_step: explicit
# 마지막 값(또는 repeat_start) 이후로 그만큼씩 반복 트리거.
LEAGUE_CARD_RULES = {
    'epl':          {'explicit': [5, 10, 15], 'repeat_step': None},
    'championship': {'explicit': [5, 10, 15], 'repeat_step': None},
    'laliga':       {'explicit': [5, 10, 14, 17, 19], 'repeat_step': 1},
    'seriea':       {'explicit': [5, 10, 14, 17, 19], 'repeat_step': 1},
    'bundesliga':   {'explicit': [], 'repeat_step': 5, 'repeat_start': 5},
    'ligue1':       {'explicit': [], 'repeat_step': 5, 'repeat_start': 5},
    'eredivisie':   {'explicit': [5, 7, 9], 'repeat_step': 1},
    'eliteserien':  {'explicit': [4], 'repeat_step': 2},
    'mls':          {'explicit': [5, 8, 11, 13], 'repeat_step': 2},
    # 위 9개 리그가 이 프로젝트가 추적하는 리그 전체(EPL+8개리그)와
    # 정확히 일치 — 커버리지 빠짐 없음.
}


def is_card_threshold(league_key, n):
    """이 카드 수(n)가 그 리그 규정상 '지금 막 정지 기준에 도달한
    시점'인지. True면 결장 위험으로 표시한다."""
    rule = LEAGUE_CARD_RULES.get(league_key)
    if not rule or n <= 0:
        return False
    explicit = rule['explicit']
    if n in explicit:
        return True
    step = rule.get('repeat_step')
    if step is None:
        return False
    start = explicit[-1] if explicit else rule.get('repeat_start', step)
    if n < start:
        return False
    return (n - start) % step == 0


# ============================================================ 팀명->리그 매칭 (다른 수집기들과 동일 원칙)
def _fold(name):
    n = unicodedata.normalize('NFKD', name)
    n = ''.join(c for c in n if not unicodedata.combining(c))
    return unicodedata.normalize('NFC', n)


def _norm(name):
    if not name:
        return ''
    n = _fold(name)
    n = re.sub(r'\b(FC|AFC|CF)\b', '', n, flags=re.I)
    return re.sub(r'[^a-z가-힣0-9]', '', n.lower())


def _build_team_to_league():
    from app_export import TEAM_NAME_MAP
    from app_export_multileague import LEAGUE_TEAM_MAPS
    index = {}
    for kr in TEAM_NAME_MAP:
        index[kr] = 'epl'
    for lk, team_map in LEAGUE_TEAM_MAPS.items():
        for kr in team_map:
            index.setdefault(kr, lk)
    return index


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _match_dates():
    if not os.path.exists(DB_PATH):
        return {}
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT match_id, date FROM matches WHERE date IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return {mid: date for mid, date in rows}


def compute(metrics_dir=METRICS_DIR, dates=None, team_to_league=None):
    """선수별 결장위험 계산. team_to_league가 None이면 실제 팀맵 임포트
    (테스트 시엔 mock dict 주입 가능)."""
    if dates is None:
        dates = _match_dates()
    if team_to_league is None:
        team_to_league = _build_team_to_league()

    players = {}
    n_files = n_with_cards = 0
    for path in glob.glob(os.path.join(metrics_dir, '*_metrics.json')):
        mid = os.path.splitext(os.path.basename(path))[0].replace('_metrics', '')
        date = dates.get(mid)
        data = _load_json(path, {})
        p_dict = data.get('players')
        if not isinstance(p_dict, dict):
            continue
        n_files += 1
        for name, stats in p_dict.items():
            yc = stats.get('yellow_cards')
            rc = stats.get('red_cards')
            if yc is None and rc is None:
                continue
            n_with_cards += 1
            entry = players.setdefault(name, {'yellow_total': 0, 'matches': [],
                                                'team': stats.get('_team')})
            entry['yellow_total'] += (yc or 0)
            entry['matches'].append((date or '', yc or 0, rc or 0))
            if stats.get('_team'):
                entry['team'] = stats.get('_team')

    out = {}
    for name, info in players.items():
        matches_sorted = sorted(info['matches'], key=lambda t: t[0])
        last_date, _, last_red = matches_sorted[-1] if matches_sorted else ('', 0, 0)
        red_on_last_match = bool(last_red) and bool(last_date)
        yellow_total = info['yellow_total']
        league_key = team_to_league.get(info['team'])
        yellow_threshold_hit = (
            league_key is not None and
            is_card_threshold(league_key, yellow_total)
        )
        if not (red_on_last_match or yellow_threshold_hit):
            continue
        out[name] = {
            'team': info['team'],
            'yellow_total': yellow_total,
            'red_on_last_match': red_on_last_match,
            'yellow_threshold_hit': yellow_threshold_hit,
            'last_match_date': last_date or None,
        }

    print(f'[compute_card_suspensions] metrics 파일 {n_files}개 스캔, '
          f'카드기록 있는 선수-경기 {n_with_cards}건', flush=True)
    print(f'[compute_card_suspensions] 결장 위험 선수 {len(out)}명 '
          f'(레드카드 직전경기 {sum(1 for v in out.values() if v["red_on_last_match"])}명, '
          f'옐로누적 기준도달 {sum(1 for v in out.values() if v["yellow_threshold_hit"])}명)',
          flush=True)
    return out


def main():
    out = compute()
    _atomic_write(OUT_PATH, out)
    print(f'[compute_card_suspensions] {OUT_PATH} 저장 완료', flush=True)


if __name__ == '__main__':
    main()
