# -*- coding: utf-8 -*-
"""
파이프라인 산출물 → EPL_index.html 앱 데이터 변환기

입력: data/football.db, data/metrics/*.json, data/master/club_elo.json
출력: reports/app_data.js
      (ELO / ADVANCED_STATS / SQUADS / STATIC_LEADERBOARD / _liveResults
       를 앱이 바로 붙여넣을 수 있는 JS const 블록으로 생성)

값이 없는 지표는 가이드 문서 원칙대로 0 또는 생략 → 예측에 영향 없음(하위호환).
"""
import glob
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict

import requests

NAME_CACHE_PATH = 'data/master/name_translations.json'


def _load_name_cache():
    if os.path.exists(NAME_CACHE_PATH):
        try:
            with open(NAME_CACHE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_name_cache(cache):
    os.makedirs(os.path.dirname(NAME_CACHE_PATH), exist_ok=True)
    with open(NAME_CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def _ascii_fold(s):
    """악센트 제거 + 소문자화 (매칭용, 표시용 아님)."""
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _fuzzy_cache_match(name, cache):
    """데이터 소스가 선수의 긴 법적 본명을 보낼 때 대비:
    캐시에 있는 '일반적으로 쓰는 이름'의 각 단어가 전체 이름 문자열
    안에 부분 문자열로 다 포함되면 그 캐시 항목을 재사용한다.
    예: 'Rúben Santos Gato Alves Dias' -> 캐시의 'Rúben Dias' 매칭.
    가장 긴(=가장 구체적인) 캐시 키를 우선한다."""
    name_folded = _ascii_fold(name)
    best_key, best_ko = None, None
    for key, ko in cache.items():
        if not key or key == name:
            continue
        parts = [p for p in re.split(r"[\s'\"]+", key) if p]
        if not parts:
            continue
        parts_folded = [_ascii_fold(p) for p in parts]
        if all(p in name_folded for p in parts_folded):
            if best_key is None or len(key) > len(best_key):
                best_key, best_ko = key, ko
    return best_ko


def _translate_name(name, cache):
    """영문 선수명 -> 한글. 캐시에 정확히 있으면 재사용, 없으면 캐시 내
    짧은 이름과의 부분일치(퍼지 매칭)를 시도, 그래도 없으면 구글 번역
    무료 엔드포인트로 조회(키 불필요, 실패하면 영문 그대로 반환)."""
    if not name:
        return name
    if name in cache:
        return cache[name]
    fuzzy = _fuzzy_cache_match(name, cache)
    if fuzzy:
        cache[name] = fuzzy
        return fuzzy
    try:
        resp = requests.get(
            'https://translate.googleapis.com/translate_a/single',
            params={'client': 'gtx', 'sl': 'en', 'tl': 'ko', 'dt': 't',
                    'q': name},
            timeout=5)
        data = resp.json()
        ko = ''.join(seg[0] for seg in data[0]) if data and data[0] else name
        ko = ko.strip() or name
    except Exception:
        ko = name  # 실패 시 영문 이름 그대로 사용 (예외 없이 넘어감)
    else:
        time.sleep(0.05)  # API 과다 호출 방지 (신규 항목만 지연, 캐시 히트는 즉시)
    cache[name] = ko
    return ko

DB_PATH = 'data/football.db'
METRICS_DIR = 'data/metrics'
OUT_PATH = 'reports/app_data.js'

# ============================================================ 팀명 매핑
# 앱(EPL_index.html)이 쓰는 한글 20개 구단명 ↔ 파이프라인 소스가 쓰는 영문명
# (football-data.org / openfootball / ClubElo 표기 편차를 모두 커버)
TEAM_NAME_MAP = {
    '맨체스터 시티': ['Manchester City FC', 'Man City', 'Manchester City'],
    '아스날': ['Arsenal FC', 'Arsenal'],
    '리버풀': ['Liverpool FC', 'Liverpool'],
    '첼시': ['Chelsea FC', 'Chelsea'],
    '맨체스터 유나이티드': ['Manchester United FC', 'Man United', 'Man Utd',
                    'Manchester United'],
    '토트넘': ['Tottenham Hotspur FC', 'Tottenham', 'Spurs'],
    '뉴캐슬': ['Newcastle United FC', 'Newcastle'],
    '아스톤 빌라': ['Aston Villa FC', 'Aston Villa'],
    '브라이튼': ['Brighton & Hove Albion FC', 'Brighton'],
    '크리스탈 팰리스': ['Crystal Palace FC', 'Crystal Palace'],
    '풀럼': ['Fulham FC', 'Fulham'],
    '본머스': ['AFC Bournemouth', 'Bournemouth'],
    '브렌트포드': ['Brentford FC', 'Brentford'],
    '에버튼': ['Everton FC', 'Everton'],
    '노팅엄 포레스트': ['Nottingham Forest FC', 'Nottingham Forest',
                 "Nott'm Forest"],
    '리즈 유나이티드': ['Leeds United FC', 'Leeds United', 'Leeds'],
    '선덜랜드': ['Sunderland AFC', 'Sunderland'],
    '코번트리 시티': ['Coventry City FC', 'Coventry City', 'Coventry'],
    '헐 시티': ['Hull City AFC', 'Hull City', 'Hull'],
    '입스위치 타운': ['Ipswich Town FC', 'Ipswich Town', 'Ipswich'],
}


def _norm(name):
    """비교용 정규화: 대소문자/공백/흔한 접미사 제거."""
    if not name:
        return ''
    n = re.sub(r'\b(FC|AFC|CF)\b', '', name, flags=re.I)
    return re.sub(r'[^a-z가-힣0-9]', '', n.lower())


_LOOKUP = {}
for kr, aliases in TEAM_NAME_MAP.items():
    for a in aliases + [kr]:
        _LOOKUP[_norm(a)] = kr


def to_kr(name):
    """영문/한글 어떤 표기가 와도 앱 표준 한글 팀명으로 변환. 매칭 실패 시 None."""
    return _LOOKUP.get(_norm(name))


# ============================================================ 로드 유틸
def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _js(v):
    """파이썬 값을 JS 리터럴 문자열로."""
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(v, ensure_ascii=False)


# ============================================================ 1) ELO / ADVANCED_STATS
def build_team_blocks():
    elo_rankings = _load_json('data/master/club_elo.json', {}).get('rankings', [])
    elo_by_team = {}
    for r in elo_rankings:
        kr = to_kr(r.get('club'))
        if kr:
            elo_by_team[kr] = r.get('elo')

    season_teams = _load_json(f'{METRICS_DIR}/season_teams.json', {})
    team_pm = {}  # 한글팀명 -> per_match dict
    for name, obj in season_teams.items():
        kr = to_kr(name)
        if kr:
            team_pm[kr] = obj.get('per_match', {})

    default_elo = dict(base=1500, xgElo=1500, atk=1500, def_=1500, form=1500,
                        market=1500, player=1500, xG=1.4, xGA=1.4, npXg=1.3,
                        ppda=12, fieldTilt=50, setPxg=0.30,
                        formArr=[0, 0, 0, 0, 0], fatigue=0, injuries=0)

    default_adv = dict(psxg=1.35, psxgAllowed=1.35, bigChances=2.0,
                        bigChancesAllowed=2.0, shotQuality=0.105,
                        spCornerXg=0.12, spFreekickXg=0.05, spPenaltyXg=0.06,
                        spCornerXgA=0.12, spFreekickXgA=0.05, spPenaltyXgA=0.06,
                        aerialWinPct=50.0, headerGoals=0.15,
                        headerGoalsAllowed=0.15, gkPsxgSaved=0.0,
                        cleanSheetPct=30.0, savePct=70.0, homeXg=1.4,
                        homeXga=1.3, awayXg=1.2, awayXga=1.5, euRotation=0)

    elo_out, adv_out = {}, {}
    for kr in TEAM_NAME_MAP:
        base_elo = elo_by_team.get(kr)
        e = dict(default_elo)
        if base_elo:
            e['base'] = round(base_elo)
            e['xgElo'] = round(base_elo)
        pm = team_pm.get(kr, {})
        if pm.get('xG') is not None:
            e['xG'] = pm['xG']
        if pm.get('xGA') is not None:
            e['xGA'] = pm['xGA']
        if pm.get('npxG') is not None:
            e['npXg'] = pm['npxG']
        if pm.get('PPDA') is not None:
            e['ppda'] = pm['PPDA']
        e['form'] = e['base']
        elo_out[kr] = {('def' if k == 'def_' else k): v for k, v in e.items()}

        a = dict(default_adv)
        if pm.get('PSxG') is not None:
            a['psxg'] = pm['PSxG']
        if pm.get('PSxG_faced') is not None:
            a['psxgAllowed'] = pm['PSxG_faced']
        if pm.get('big_chances_created') is not None:
            a['bigChances'] = pm['big_chances_created']
        adv_out[kr] = a

    return elo_out, adv_out


# ============================================================ 2) RECENT_FORM / _liveResults
def build_matches():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM matches WHERE status='FINISHED' "
        "AND home_goals IS NOT NULL ORDER BY date"
    ).fetchall()
    conn.close()

    recent_form = defaultdict(list)
    live_results = {}
    for r in rows:
        home_kr, away_kr = to_kr(r['home']), to_kr(r['away'])
        if not (home_kr and away_kr):
            continue
        hg, ag = r['home_goals'], r['away_goals']
        recent_form[home_kr].append({
            'opp': away_kr, 'r': 'W' if hg > ag else 'D' if hg == ag else 'L',
            'gf': hg, 'ga': ag, 'date': r['date']})
        recent_form[away_kr].append({
            'opp': home_kr, 'r': 'W' if ag > hg else 'D' if hg == ag else 'L',
            'gf': ag, 'ga': hg, 'date': r['date']})
        live_results[f'{home_kr}_{away_kr}'] = {
            'home': hg, 'away': ag, 'date': r['date'], 'source': 'pipeline',
            'fetched': 0}

    for kr in recent_form:
        recent_form[kr] = recent_form[kr][-10:]  # 최근 10경기만 보관
    return dict(recent_form), live_results


# ============================================================ 3) SQUADS (선수/경기별 기록)
POS_BUCKET = {'Goalkeeper': 'gk', 'GK': 'gk',
              'Defender': 'df', 'DF': 'df',
              'Midfielder': 'mf', 'MF': 'mf',
              'Forward': 'fw', 'FW': 'fw', 'Attacker': 'fw'}


def _game_record(stats):
    shots = stats.get('shots', 0) or 0
    return {
        'goals': int(stats.get('goals', 0) or 0),
        'assists': int(stats.get('assists', 0) or 0),
        'xg': round(stats.get('xG', 0) or 0, 3),
        'xa': round(stats.get('xA', 0) or 0, 3),
        'shots': int(shots),
        'sot': 0,
        'progPass': int(stats.get('progressive_passes', 0) or 0),
        'progCarry': int(stats.get('progressive_carries', 0) or 0),
        'sca': int(stats.get('SCA', 0) or 0),
        'gca': int(stats.get('GCA', 0) or 0),
        'psxgGA': 0,
        'bigChances': int(stats.get('big_chances_created', 0) or 0),
        'keyPasses': int(stats.get('key_passes', 0) or 0),
        'crossComp': 0,
        'tacklesWon': int(stats.get('tackles_won', 0) or 0),
        'interceptions': 0,
        'clr': 0,
        'recoveries': int((stats.get('pressure_regains', 0) or 0)
                          + (stats.get('counterpress_recoveries', 0) or 0)),
        'aerialWinPct': 0,
        'takeOns': int(stats.get('dribbles', 0) or 0),
        'takeOnPct': round((stats.get('dribbles_won', 0) or 0)
                           / stats['dribbles'] * 100, 1)
                    if stats.get('dribbles') else 0,
        'pressSucc': int(stats.get('counterpress_recoveries', 0) or 0),
        'pressSuccPct': 0,
    }


def build_squads(name_cache):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    players_rows = conn.execute(
        'SELECT p.name, p.position, t.name AS team '
        'FROM players p LEFT JOIN teams t ON p.team_id = t.team_id'
    ).fetchall()
    team_coach_rows = conn.execute(
        'SELECT name, coach FROM teams WHERE coach IS NOT NULL AND coach != ""'
    ).fetchall()
    conn.close()

    # 팀 한글명 -> 감독명
    coach_by_team = {}
    for row in team_coach_rows:
        kr = to_kr(row['name'])
        if kr and row['coach']:
            coach_by_team[kr] = row['coach']

    # 선수명 -> (팀 한글명, 포지션버킷)
    player_team = {}
    for p in players_rows:
        kr = to_kr(p['team']) if p['team'] else None
        if kr:
            player_team[p['name']] = (kr, POS_BUCKET.get(p['position'], 'mf'))

    # 경기별 metrics 파일을 순회하며 선수별 game 배열 축적
    games_by_player = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(METRICS_DIR, '*_metrics.json'))):
        base = os.path.basename(path)
        if base in ('player_profiles.json', 'season_players.json',
                    'season_teams.json', 'transfer_impact.json'):
            continue
        data = _load_json(path, {})
        for name, stats in data.get('players', {}).items():
            games_by_player[name].append(_game_record(stats))

    squads = {}
    for kr in TEAM_NAME_MAP:
        squads[kr] = {'coach': coach_by_team.get(kr, ''), 'formation': '4-3-3', 'league': 'PL',
                      'gk': [], 'df': [], 'mf': [], 'fw': [],
                      'xi': [], 'injured': [], 'keyOut': [],
                      'ppda': 12.0, 'fieldTilt': 50, 'setPieceXg': 0.30}

    for name, (kr, bucket) in player_team.items():
        games = games_by_player.get(name, [])
        squads[kr][bucket].append({
            'name': _translate_name(name, name_cache),
            'nameEn': name,
            'pos': {'gk': 'GK', 'df': 'DF', 'mf': 'MF', 'fw': 'FW'}[bucket],
            'games': games,
        })

    return squads


# ============================================================ 4) STATIC_LEADERBOARD
def build_leaderboard(name_cache):
    season_players = _load_json(f'{METRICS_DIR}/season_players.json', {})
    scorers, assists = {}, {}
    for name, obj in season_players.items():
        team_kr = to_kr(obj.get('team')) if obj.get('team') else None
        if not team_kr:
            continue
        ko_name = _translate_name(name, name_cache)
        key = f'{ko_name}|{team_kr}'
        totals = obj.get('totals', {})
        if totals.get('goals'):
            scorers[key] = int(totals['goals'])
        if totals.get('assists'):
            assists[key] = int(totals['assists'])
    return {'scorers': scorers, 'assists': assists, 'gk': {}, 'cards': {},
            'ownGoals': {}}


# ============================================================ JS 렌더링
def render_js(name_cache):
    elo, adv = build_team_blocks()
    recent_form, live_results = build_matches()
    squads = build_squads(name_cache)
    leaderboard = build_leaderboard(name_cache)

    lines = ['// 자동 생성 파일 — app_export.py, 수정하지 말고 파이프라인을 고치세요',
             f'// 생성 시각: {__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}',
             '']
    lines.append('const PIPELINE_ELO = ' + _js(elo) + ';')
    lines.append('const PIPELINE_ADVANCED_STATS = ' + _js(adv) + ';')
    lines.append('const PIPELINE_RECENT_FORM = ' + _js(recent_form) + ';')
    lines.append('const PIPELINE_SQUADS = ' + _js(squads) + ';')
    lines.append('const PIPELINE_STATIC_LEADERBOARD = ' + _js(leaderboard) + ';')
    lines.append('const PIPELINE_LIVE_RESULTS = ' + _js(live_results) + ';')
    lines.append('')
    lines.append('// 앱에 반영하려면: 위 6개 PIPELINE_* 객체 내용을 앱 파일의 '
                 'ELO/ADVANCED_STATS/RECENT_FORM/SQUADS/STATIC_LEADERBOARD/'
                 '_liveResults 각각에 Object.assign으로 병합하거나, '
                 '해당 const 선언을 통째로 교체하세요.')
    return '\n'.join(lines)


def main():
    os.makedirs('reports', exist_ok=True)
    name_cache = _load_name_cache()
    js = render_js(name_cache)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write(js)
    _save_name_cache(name_cache)
    print(f'[app_export] {OUT_PATH} 생성 완료 ({len(js)} bytes), '
          f'이름 캐시 {len(name_cache)}건')


if __name__ == '__main__':
    main()
