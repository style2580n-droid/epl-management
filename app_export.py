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

# 2026-07-23 (0-B #3 수정): update_player_baseline()이 app_export_multileague.py
# 안에서만 호출되는데 yml 순서가 app_export.py(EPL) → app_export_multileague.py라,
# EPL은 항상 한 실행 전(stale) baseline을 읽고 있었다. 여기서도 같은 함수를
# 먼저 호출해서 EPL도 이번 실행의 최신 리그 기록을 반영받게 한다. 이 함수는
# player_baseline.json을 매번 처음부터 다시 계산해서 통째로 덮어쓰므로(증분
# 누적이 아님) 같은 실행 안에서 두 번 호출돼도 안전(멱등)하다.
from app_export_multileague import update_player_baseline

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
    캐시에 있는 '일반적으로 쓰는 이름'의 각 단어가 전체 이름의 단어 목록
    안에 '완전한 단어'로 다 포함되면 그 캐시 항목을 재사용한다.
    예: 'Rúben Santos Gato Alves Dias' -> 캐시의 'Rúben Dias' 매칭.
    단어 단위 매칭이라 'Rodri'가 'Rodrigues' 안에서 잘못 매칭되는 것을 방지.
    가장 긴(=가장 구체적인) 캐시 키를 우선한다."""
    name_tokens = [_ascii_fold(t) for t in re.split(r"[\s'\"-]+", name) if t]
    best_key, best_ko = None, None
    for key, ko in cache.items():
        if not key or key == name:
            continue
        parts = [p for p in re.split(r"[\s'\"-]+", key) if p]
        if not parts:
            continue
        parts_folded = [_ascii_fold(p) for p in parts]
        if all(p in name_tokens for p in parts_folded):
            if best_key is None or len(key) > len(best_key):
                best_key, best_ko = key, ko
    return best_ko


_MISTRANSLATION_MARKERS = (
    # 서술문/명령문/의문문 어미 — 구글 번역이 이름을 문장으로 직역했을 때
    # 나타나는 대표 패턴들 (2026-07-18 실전 실행에서 실측된 것들 위주)
    '세요', '십시오', '습니다', '합니다', '됩니다', '해요', '하세요',
    '주세요', '것입니다', '것이다', '였다', '이었다', '했다', '한다',
    '떠나', '구출', '살펴보', '찾아보', '찾으세요', '찾아서',
    '인가요', '인가?', '까요', '까요?', '나요', '나요?', '습니까',
    'ㅂ니까', '입니까', '몇 명', '몇개', '몇 개', '얼마나', '무엇',
    '누구', '어디', '어떻게',
)

# 조사(을/를/은/는) 뒤에 다른 단어가 더 붙어 있으면 문장 구조라는 뜻 —
# 사람 이름 음역엔 이런 문법 구조가 나올 이유가 없다(2026-07-18 추가).
# 단어 목록을 계속 늘리는 것보다 이 구조적 규칙 하나가 앞으로 새로
# 나타날 오역 패턴까지 훨씬 넓게 잡아준다. 한 음절짜리 조사(이/가/에/로
# 등)는 진짜 이름 끝음절과 우연히 겹칠 위험이 있어서 제외하고, 겹칠
# 위험이 낮은 을/를/은/는 4개만 쓴다.
_STRUCTURAL_PARTICLES = ('을', '를', '은', '는', '의')
# 형용사 관형형 오역 패턴(예: 'Honest Ahanor' -> '정직한 아하노르') 대응.
# 2026-07-18 3차 추가 — 조사만으로는 못 잡는 [형용사]+[명사] 구조까지 커버.
_ATTRIBUTIVE_SUFFIXES = ('한',)


def _looks_like_mistranslation(translated):
    """구글 번역이 사람 이름을 진짜 문장/명령문/의문문으로 직역해버린
    경우를 걸러낸다 (2026-07-18 발견 — 예: 'Kevin De Bruyne'가 '드
    브루인을 찾아보세요'로, 'Sapoco Ndiaye'류 이름이 '~는 몇 명인가요?'로
    번역되는 등). 이름 일부가 흔한 영어 단어와 겹치면 구글 번역이
    고유명사가 아니라 문장으로 처리해버리는 게 원인.
    두 단계로 검사한다: 1) 흔한 어미/의문형 패턴이 문자열 어디든 있는지
    (기존 방식), 2) 조사(을/를/은/는)로 끝나는 단어 뒤에 다른 단어가 더
    있는지(문장 구조 신호, 2026-07-18 추가 — 이게 미래의 새로운 오역
    패턴까지 넓게 잡아주는 핵심). 걸리면 원문 영문 이름을 그대로 쓴다
    (틀린 한글 번역보다 안 틀린 영문 원문이 낫다는 원칙)."""
    if not translated:
        return False
    if any(marker in translated for marker in _MISTRANSLATION_MARKERS):
        return True
    tokens = translated.split()
    for tok in tokens[:-1]:  # 마지막 토큰 뒤엔 더 이어지는 말이 없으니 검사 제외
        for suf in _STRUCTURAL_PARTICLES + _ATTRIBUTIVE_SUFFIXES:
            if tok.endswith(suf) and len(tok) > len(suf):
                return True
    return False


def _translate_name(name, cache):
    """영문 선수명 -> 한글. 캐시에 정확히 있으면 재사용, 없으면 캐시 내
    짧은 이름과의 부분일치(퍼지 매칭)를 시도, 그래도 없으면 구글 번역
    무료 엔드포인트로 조회(키 불필요, 실패하면 영문 그대로 반환).

    2026-07-18 추가: 캐시/퍼지매칭/신규번역 결과 전부 _looks_like_mistranslation
    으로 한 번 더 검증한다 — 오역으로 의심되면 원문으로 재시도하거나
    폴백한다(자가 치유 — 이전에 잘못 캐시된 값도 다음 실행에서 걸러짐)."""
    if not name:
        return name
    if name in cache and not _looks_like_mistranslation(cache[name]):
        return cache[name]
    fuzzy = _fuzzy_cache_match(name, cache)
    if fuzzy and not _looks_like_mistranslation(fuzzy):
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
    if _looks_like_mistranslation(ko):
        ko = name  # 오역으로 보이면 원문 영문 이름으로 되돌림
    cache[name] = ko
    return ko

DB_PATH = 'data/football.db'
METRICS_DIR = 'data/metrics'
OUT_PATH = 'reports/app_data.js'
# collect_transfers_bsd.py가 만드는 BSD 스냅샷 diff 기반 이적 기록.
# football-data.org 기반 구 DB `transfers` 테이블은 26-27 시즌 스쿼드가
# 아직 등록 안 돼(squad길이=0) 항상 0건이라, 여기를 1순위로 쓴다
# (2026-07-16 확인, 인수인계 문서 "다음에 할 것" 대응).
TRANSFERS_BSD_PATH = 'data/master/transfers_bsd.json'
# train_ml_ensemble.py가 EPL+6개 리그 통합으로 학습한 모델(2026-07-16).
# 2026-07-16 결정: EPL 앱의 randomForestWinProb(하드코딩 결정트리 5개, 실제
# 데이터로 학습된 게 아님)를 이걸로 교체한다 — 파일 없으면(학습 표본 부족)
# 프론트가 자동으로 기존 하드코딩 트리로 폴백하므로 안전하게 시도할 수 있다.
ML_ENSEMBLE_PATH = 'data/master/ml_ensemble.json'

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


# ============================================================ 감독 수동 보정
# BSD 데이터가 초고속 감독 교체를 못 따라잡을 때를 위한 override.
# coaches.json / teams / lineups 어떤 소스보다도 우선 적용된다.
# ⚠️ 보정한 팀이 실제로 또 감독을 바꾸면 여기 값이 낡을 수 있으니,
#    BSD가 정확히 채우기 시작하면 해당 항목을 지우는 것이 이상적이다.
# (기준일: 2026-07-12, 시즌 개막 전 여름 감독 대이동 반영)
COACH_OVERRIDES = {
    '리버풀': 'Andoni Iraola',        # 2026-06-05 슬롯 후임으로 부임
    '노팅엄 포레스트': 'Oliver Glasner',  # 2026-07-06 페레이라 후임으로 부임
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
    lineup_coach_rows = conn.execute(
        'SELECT team, coach FROM lineups WHERE coach IS NOT NULL AND coach != "" '
        'ORDER BY updated_at DESC'
    ).fetchall()
    conn.close()

    # 팀 한글명 -> 감독명
    # 우선순위: 0) COACH_OVERRIDES 수동 보정(BSD가 못 따라잡는 초고속 교체 대비)
    #          1) collect_coaches.py가 만든 전용 감독 조회 결과
    #          2) teams 테이블(football-data.org)   3) 최근 라인업의 감독
    coach_by_team = {}
    for kr, name in COACH_OVERRIDES.items():
        if name:
            coach_by_team[kr] = name
    af_coaches = _load_json('data/master/coaches.json', {})
    for kr, name in af_coaches.items():
        if name and kr not in coach_by_team:
            coach_by_team[kr] = name
    for row in team_coach_rows:
        kr = to_kr(row['name'])
        if kr and row['coach'] and kr not in coach_by_team:
            coach_by_team[kr] = row['coach']
    for row in lineup_coach_rows:
        kr = to_kr(row['team'])
        if kr and row['coach'] and kr not in coach_by_team:
            coach_by_team[kr] = row['coach']

    # 선수명 -> (팀 한글명, 포지션버킷)
    player_team = {}
    for p in players_rows:
        kr = to_kr(p['team']) if p['team'] else None
        if kr:
            player_team[p['name']] = (kr, POS_BUCKET.get(p['position'], 'mf'))
    print(f'[app_export] [diag] players_rows {len(players_rows)}건 → '
          f'player_team {len(player_team)}명 (팀 매칭된 선수만) '
          f'샘플: {list(player_team.keys())[:5]}', flush=True)

    # 2026-07-29 확정된 근본원인 수정: BSD incidents/ 이벤트가 주는 선수명은
    # "M. Longstaff"(이니셜.성) 형식인데, player_team(DB)엔 "David Raya
    # Martín" 같은 풀네임으로 저장돼 있어서 문자열이 절대 일치할 수 없었음
    # (실전 로그로 매칭 0/10 확인) — 이게 team_group_games가 계속 0으로
    # 나오던 진짜 원인. 성(마지막 단어)으로 역인덱스를 만들고, 이니셜.성
    # 패턴이면 성이 일치하는 후보 중 이니셜도 맞는 것을 찾는다. 후보가
    # 2명 이상 겹치면(동성이름) 오매칭 위험이 크므로 매칭시키지 않는다.
    last_name_index = defaultdict(list)
    for full_name in player_team:
        parts = full_name.strip().split()
        if parts:
            last_name_index[parts[-1]].append(full_name)
    _initial_re = re.compile(r'^([A-Za-zÀ-ÿ])\.\s*(.+)$')

    def _resolve_player(name):
        """player_team에서 name을 찾는다. 정확매칭 우선, 실패시 'M. Longstaff'
        같은 이니셜.성 형식을 풀네임과 대조한다."""
        info = player_team.get(name)
        if info:
            return info
        m = _initial_re.match(name.strip())
        if not m:
            return None
        initial, last = m.group(1).lower(), m.group(2).strip()
        candidates = last_name_index.get(last, [])
        matched = [c for c in candidates
                   if c.strip().split()[0][:1].lower() == initial]
        if len(matched) == 1:
            return player_team.get(matched[0])
        return None  # 0명 또는 동명이인 2명 이상이면 안전하게 매칭 안 함

    # 경기별 metrics 파일을 순회하며 선수별 game 배열 축적.
    # 2026-07-26 추가: 같은 루프에서 팀→경기(파일명)→선수배열 구조
    # (team_group_games)도 같이 만든다 — EPL_index.html의 TEAM_GROUP_GAMES가
    # 여태 빈 객체로 하드코딩돼 있어서 개인기여도(playerImpactPct)가 죽어있던
    # 문제 대응. gameRawScore()가 필요로 하는 지표(xg/xa/progPass/sca/gca/
    # bigChances/keyPasses/tacklesWon/recoveries/takeOns 등)를 _game_record()가
    # 이미 다 뽑고 있어서 새로 만들 데이터는 없고 재구성만 하면 된다.
    games_by_player = defaultdict(list)
    team_group_games = defaultdict(dict)  # {팀kr: {경기라벨: [선수레코드,...]}}
    _diag_done = False
    _n_files_total, _n_files_with_players = 0, 0
    for path in sorted(glob.glob(os.path.join(METRICS_DIR, '*_metrics.json'))):
        base = os.path.basename(path)
        if base in ('player_profiles.json', 'season_players.json',
                    'season_teams.json', 'transfer_impact.json'):
            continue
        data = _load_json(path, {})
        metrics_names = list(data.get('players', {}).keys())
        _n_files_total += 1
        if metrics_names:
            _n_files_with_players += 1
        # 2026-07-29 수정: 이전 버전은 sorted() 결과의 "첫 파일"만 찍었는데,
        # 알파벳순 정렬이라 숫자/영문으로 시작하는 오래된 파일(예:
        # 1.FCKaiserslautern...)이 항상 먼저 나와서, 이번에 새로 대량
        # 채워진 한글 팀명 파일들(아스톤빌라_맨체스터유나이티드 등, 알파벳
        # 순으로 한참 뒤)은 한 번도 진단 로그에 안 잡혔을 가능성이 있음
        # (실전에서 team_group_games가 계속 0으로 나온 게 이 진단 맹점
        # 때문인지, 진짜 0인지 구분이 안 됐음). 이제 "실제로 players가
        # 채워진 첫 파일"을 찾아서 보여주고, 전체 중 몇 개나 채워졌는지도
        # 집계한다.
        if not _diag_done and metrics_names:
            _diag_done = True
            sample_metrics_names = metrics_names[:5]
            sample_player_team_names = list(player_team.keys())[:5]
            matched = sum(1 for n in metrics_names if _resolve_player(n))
            print(f'[app_export] [diag] 실제로 채워진 첫 metrics 파일: {base}, '
                  f'선수 키 샘플: {sample_metrics_names}', flush=True)
            print(f'[app_export] [diag] player_team(DB) 키 샘플: '
                  f'{sample_player_team_names}', flush=True)
            print(f'[app_export] [diag] 이 경기 {len(metrics_names)}명 중 '
                  f'player_team과 매칭된 수: {matched}', flush=True)
        for name, stats in data.get('players', {}).items():
            rec = _game_record(stats)
            games_by_player[name].append(rec)
            info = _resolve_player(name)
            if not info:
                continue
            kr, bucket = info
            team_group_games[kr].setdefault(base, []).append({
                **rec,
                'name': _translate_name(name, name_cache),
                'pos': {'gk': 'GK', 'df': 'DF', 'mf': 'MF', 'fw': 'FW'}[bucket],
            })

    squads = {}
    for kr in TEAM_NAME_MAP:
        coach_en = coach_by_team.get(kr, '')
        squads[kr] = {'coach': _translate_name(coach_en, name_cache) if coach_en else '',
                      'formation': '4-3-3', 'league': 'PL',
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

    print(f'[app_export] [diag] metrics 파일 총 {_n_files_total}개 중 '
          f'players 채워진 파일 {_n_files_with_players}개', flush=True)
    return squads, dict(team_group_games)


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


# ============================================================ 5) TRANSFERS (영입/이탈)
def _build_transfers_from_bsd(name_cache):
    """1순위 소스: collect_transfers_bsd.py가 BSD 스냅샷 diff로 감지한 이적.
    team_kr은 수집 시점에 이미 to_kr/to_kr_league로 변환된 한글 팀명이라
    여기서 다시 매핑할 필요 없음 — league=='epl' 레코드만 걸러서 쓴다."""
    records = _load_json(TRANSFERS_BSD_PATH, [])
    if not records:
        return None  # BSD 소스가 아직 없으면 None을 반환해 DB 폴백으로 넘김
    # detected_at 최신순 정렬
    records = sorted(records, key=lambda r: r.get('detected_at') or '', reverse=True)

    out = {kr: {'in': [], 'out': []} for kr in TEAM_NAME_MAP}
    seen = set()  # (player_id, from_team, to_team) 중복 제거
    for r in records:
        to_league, from_league = r.get('to_league'), r.get('from_league')
        if to_league != 'epl' and from_league != 'epl':
            continue
        dedup_key = (r.get('player_id'), r.get('from_team'), r.get('to_team'))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        player_en = r.get('player_name')
        if not player_en:
            continue
        player_ko = _translate_name(player_en, name_cache)
        to_team_kr, from_team_kr = r.get('to_team'), r.get('from_team')

        if to_league == 'epl' and to_team_kr in out:
            out[to_team_kr]['in'].append({
                'player': player_ko,
                'from': from_team_kr or ('미상' if not from_league else f'{from_league} 소속팀'),
                'date': r.get('detected_at'),
            })
        if from_league == 'epl' and from_team_kr in out:
            out[from_team_kr]['out'].append({
                'player': player_ko,
                'to': to_team_kr or ('미상' if not to_league else f'{to_league} 소속팀'),
                'date': r.get('detected_at'),
            })

    for kr in out:
        out[kr]['in'] = out[kr]['in'][:20]
        out[kr]['out'] = out[kr]['out'][:20]
    return out


def _build_transfers_from_db(name_cache):
    """2순위(폴백) 소스: football-data.org 기반 구 TransferDetector 결과.
    26-27 시즌 스쿼드 등록 전에는 항상 비어있지만, BSD 소스가 없을 때
    (예: BSD_API_KEY 미설정) 안전망으로 유지한다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT player_name, from_team, to_team, detected_at '
        'FROM transfers ORDER BY detected_at DESC'
    ).fetchall()
    conn.close()

    out = {kr: {'in': [], 'out': []} for kr in TEAM_NAME_MAP}
    for r in rows:
        if not r['player_name']:
            continue
        player_ko = _translate_name(r['player_name'], name_cache)
        to_team_kr = to_kr(r['to_team']) if r['to_team'] else None
        from_team_kr = to_kr(r['from_team']) if r['from_team'] else None

        if to_team_kr and to_team_kr in out:
            out[to_team_kr]['in'].append({
                'player': player_ko,
                'from': r['from_team'] or '미상',
                'date': r['detected_at'],
            })
        if from_team_kr and from_team_kr in out:
            out[from_team_kr]['out'].append({
                'player': player_ko,
                'to': r['to_team'] or '미상',
                'date': r['detected_at'],
            })

    for kr in out:
        out[kr]['in'] = out[kr]['in'][:20]
        out[kr]['out'] = out[kr]['out'][:20]
    return out


def build_transfers(name_cache):
    bsd_out = _build_transfers_from_bsd(name_cache)
    if bsd_out is not None:
        total = sum(len(v['in']) + len(v['out']) for v in bsd_out.values())
        print(f'[app_export] 이적: BSD 소스 사용, {total}건', flush=True)
        return bsd_out
    print('[app_export] 이적: BSD 소스 없음 → DB 폴백(football-data.org, '
          '시즌 전이라 0건일 가능성 높음)', flush=True)
    return _build_transfers_from_db(name_cache)


# ============================================================ 6) SCHEDULE / H2H
# collect_fixtures.py가 만들어둔 파일을 그대로 읽어 전달한다(자체 계산 없음).
def build_schedule():
    return _load_json('data/master/schedule.json', [])


def build_h2h():
    """두 팀의 과거 맞대결 기록을 matches 테이블(이미 매일 수집 중인 데이터)에서
    직접 계산한다. 프론트엔드(EPL_index.html getH2H)가 기대하는 키 형식
    "팀A|||팀B"(가나다순 정렬)에 맞춰 최신순으로 최대 10경기씩 담는다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM matches WHERE status='FINISHED' "
        "AND home_goals IS NOT NULL ORDER BY date"
    ).fetchall()
    conn.close()

    h2h = {}
    for r in rows:
        home_kr, away_kr = to_kr(r['home']), to_kr(r['away'])
        if not (home_kr and away_kr):
            continue
        key = '|||'.join(sorted([home_kr, away_kr]))
        h2h.setdefault(key, []).append({
            'home': home_kr, 'away': away_kr,
            'homeGoals': r['home_goals'], 'awayGoals': r['away_goals'],
            'date': r['date'],
        })

    # 2026-07-30 확정: collect_fixtures.py가 DATE_FROM=오늘-3년으로 이미
    # 3년치 종료경기를 통째로 훑으면서 h2h_raw를 계산해 data/master/h2h.json
    # 으로 저장까지 해두고 있었는데, 여기(build_h2h)는 그동안 이 파일을 전혀
    # 안 읽고 matches 테이블(EventCollector가 최근 것만 채움)만 썼음 — 그래서
    # "예전시즌 상대전적"이 계속 빠져 있었음. 새 API 호출 없이 이미 있는
    # 파일을 병합만 하면 된다.
    h2h_3yr = _load_json('data/master/h2h.json', {})
    n_3yr_merged = 0
    for key, games_3yr in h2h_3yr.items():
        h2h.setdefault(key, []).extend(games_3yr)
        n_3yr_merged += len(games_3yr)
    if n_3yr_merged:
        print(f'[app_export] 3년치 상대전적(h2h.json) {n_3yr_merged}건 병합',
              flush=True)

    out = {}
    for key, games in h2h.items():
        # 2026-07-30: DB(matches)와 h2h.json 양쪽에서 같은 경기가 중복으로
        # 들어올 수 있어(둘 다 소스가 겹치는 최근 구간) date+home+away로
        # 중복 제거 후 최신순 10경기.
        seen = set()
        dedup = []
        for g in sorted(games, key=lambda g: g['date'] or '', reverse=True):
            sig = (g['date'], g['home'], g['away'])
            if sig in seen:
                continue
            seen.add(sig)
            dedup.append(g)
        out[key] = dedup[:10]
    return out


# ============================================================ JS 렌더링
def render_js(name_cache):
    elo, adv = build_team_blocks()
    recent_form, live_results = build_matches()
    squads, team_group_games = build_squads(name_cache)
    leaderboard = build_leaderboard(name_cache)
    transfers = build_transfers(name_cache)
    schedule = build_schedule()
    h2h = build_h2h()

    lines = ['// 자동 생성 파일 — app_export.py, 수정하지 말고 파이프라인을 고치세요',
             f'// 생성 시각: {__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}',
             '']
    lines.append('const PIPELINE_ELO = ' + _js(elo) + ';')
    lines.append('const PIPELINE_ADVANCED_STATS = ' + _js(adv) + ';')
    lines.append('const PIPELINE_RECENT_FORM = ' + _js(recent_form) + ';')
    lines.append('const PIPELINE_SQUADS = ' + _js(squads) + ';')
    # 2026-07-26 추가: EPL_index.html의 TEAM_GROUP_GAMES(개인기여도 소스,
    # 여태 하드코딩 빈 객체라 죽어있던 기능) 살리기용.
    lines.append('const PIPELINE_TEAM_GROUP_GAMES = ' + _js(team_group_games) + ';')
    _n_games = sum(len(v) for v in team_group_games.values())
    print(f'[app_export] team_group_games: {len(team_group_games)}팀, '
          f'경기기록 {_n_games}건 → PIPELINE_TEAM_GROUP_GAMES 전달', flush=True)
    lines.append('const PIPELINE_STATIC_LEADERBOARD = ' + _js(leaderboard) + ';')
    lines.append('const PIPELINE_LIVE_RESULTS = ' + _js(live_results) + ';')
    lines.append('const PIPELINE_TRANSFERS = ' + _js(transfers) + ';')
    lines.append('const PIPELINE_SCHEDULE = ' + _js(schedule) + ';')
    lines.append('const PIPELINE_H2H = ' + _js(h2h) + ';')
    ml_ensemble = _load_json(ML_ENSEMBLE_PATH, None)
    lines.append('const PIPELINE_ML_ENSEMBLE = ' + _js(ml_ensemble) + ';')
    # 2026-07-22 (A단계): 선수 능력치 기준선. EPL 앱도 club_en으로 팀 매칭하고
    # source==="league"(리그 실기록으로 갱신된)만 예측에 쓰므로 여기서 그 조건만
    # 추려 넘긴다. 개막 전엔 사실상 빈 {}라 파일이 안 커지고, 리그 경기가 쌓여
    # league_apps>=5로 전환된 선수만 자동으로 실린다.
    # 2026-07-23 (0-B #3): 읽기 직전에 먼저 갱신 — app_export_multileague.py가
    # 나중에 실행돼서 생기던 "EPL은 한 실행 지연된 baseline" 문제 해결.
    update_player_baseline(name_cache)
    _baseline_all = (_load_json('data/master/player_baseline.json', {}) or {}).get('players', {})
    player_baseline = {
        k: v for k, v in _baseline_all.items()
        if isinstance(v, dict) and v.get('source') == 'league'
        and v.get('club_en') and isinstance(v.get('per90'), dict)
    }
    lines.append('const PIPELINE_PLAYER_BASELINE = ' + _js(player_baseline) + ';')
    print(f'[app_export] 선수 기준선(A단계): source=league {len(player_baseline)}명 전달'
          + ('' if player_baseline else ' (개막 전이라 정상적으로 0명)'))
    lines.append('')
    lines.append('// 앱에 반영하려면: 위 PIPELINE_* 객체 내용을 앱 파일의 '
                 'ELO/ADVANCED_STATS/RECENT_FORM/SQUADS/STATIC_LEADERBOARD/'
                 '_liveResults/TRANSFERS/SCHEDULE/H2H/ML_ENSEMBLE/'
                 'TEAM_GROUP_GAMES 각각에 '
                 'Object.assign으로 병합하거나, 해당 const 선언을 통째로 '
                 '교체하세요. PIPELINE_ML_ENSEMBLE은 train_ml_ensemble.py가 '
                 '표본 부족으로 스킵했으면 null일 수 있음(앱에서 null 체크 후 '
                 '기존 randomForestWinProb로 자동 폴백). PIPELINE_PLAYER_BASELINE'
                 '(A단계)은 앱의 PLAYER_BASELINE에 Object.assign — 개막 전엔 빈 '
                 '{}이고 playerBaselineLambdaAdjust가 표본 부족 시 무효과.')
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
