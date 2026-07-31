# -*- coding: utf-8 -*-
"""
collect_api_football_player_stats.py
2026-07-30 신규 (인수인계 2-1 착수).

API-FOOTBALL(v3.football.api-sports.io)의 /fixtures/players로 종료경기의
세부 선수스탯(슈팅/유효슈팅/태클/인터셉트/키패스/드리블/듀얼/평점)을 받아
data/metrics/{match_id}_metrics.json에 병합한다.

## 왜 필요한가
BSD incidents/는 골/어시스트 외 세부지표를 안 준다. gameRawScore()가 쓰는
15개 지표 중 13개가 그래서 항상 0으로 들어가고, 개인기여도%(playerImpactPct)가
골/어시스트 있는 선수만 차등되고 나머지는 최소 하한값으로 균등하게 나온다
(원인 확정, 코드버그 아님 — 2026-07-30 인수인계 2-1 참고). API-FOOTBALL
/fixtures/players가 이 13개 중 상당수(슈팅/유효슈팅/태클/인터셉트/키패스/
드리블)를 실제로 준다는 건 EPL_index.html의 fetchApiFootballPlayerStats
(지금은 월드컵 앱 전용, league=1 — EPL 실연동 아님, 스키마 참고용으로만
씀)에서 코드로 이미 확인했다.

## 지원 리그
epl + 6개 리그(laliga/bundesliga/seriea/ligue1/eredivisie/championship).
MLS/eliteserien은 파이썬 쪽에 API-FOOTBALL 리그ID가 없어(기존 라인업
기능과 동일한 이유) 의도적 미지원 — multi_league_index.html의
AF_LEAGUE_IDS와 동일 기준.

## ⚠️ 이 스크립트 특유의 미검증 가정 (다음 실행 로그로 반드시 확인할 것)
1) data/metrics/{match_id}_metrics.json의 파일명이 data/events/{match_id}.json
   과 동일한 match_id를 쓴다는 가정. impact_engine.py가 이 1:1 변환을
   한다고 가정했을 뿐 impact_engine.py 소스 자체는 이번 세션에 못 봄.
   틀렸다면 "metrics 파일 존재" 카운트가 0에 가깝게 나올 것 — 그러면
   이 가정부터 재검토.
2) matches 테이블의 home/away 값이 한글 팀명인지 영문 팀명인지 확실치
   않음 → 방어적으로 TEAM_INDEX가 한글 키/영문 별칭 양쪽 다 정규화해서
   담고 있어서 어느 쪽이 와도 매칭되게 만들었다(안전).
3) matches.league_id 값의 실제 포맷(리그키 문자열인지 다른 코드인지)도
   불확실해서 아예 안 쓴다 — home/away 팀명만으로 리그를 역산한다(양쪽
   팀이 같은 리그의 TEAM_INDEX에 같이 걸려야 확정).
4) EPL은 6개 리그와 달리 API-FOOTBALL 리그ID가 이 코드베이스에서 실측된
   적이 없어서, 매 실행 시 /leagues 검색으로 스스로 찾아 state에 캐싱한다
   (collect_fixtures.py의 _find_pl_league_id와 동일 원칙 — 숫자 추측 금지).

## 쿼터 보호
무료 티어 100회/일 "키당". API_FOOTBALL_KEY1/2 두 키를 각각 별도로
추적(DAILY_CALL_BUDGET, 키당 안전마진 적용)해서 합산 쿼터를 최대한 쓴다.
쿼터 소진 시 그 실행은 중단하고 처리 상태를 data/master/
af_player_stats_state.json에 영구 저장 — 파이프라인이 하루 4번(6시간
간격) 돌고 data/를 매번 커밋하므로, 실행 간 이어서 진행된다. EPL(722경기)
+8개리그(수천 경기)를 한 번에 다 못 채우는 게 정상이며 며칠~몇 주에
걸쳐 점진적으로 채워지도록 의도한 설계.

## 안전 실패
이 스크립트가 실패해도(스키마 미확정 등) 나머지 파이프라인에 영향 없게
설계(모든 외부 호출 try/except, DB/파일 없으면 조용히 스킵). yml에서도
`|| true`로 감싸서 실행(신규/미검증 스크립트 공통 관례, collect_mls_
official_stats.py와 동일).

## 파이프라인 내 위치
db.py(→ matches 테이블) 뒤, app_export.py/app_export_multileague.py
(→ team_group_games가 data/metrics를 읽음) 앞에 와야 한다.
"""
import glob
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:  # pragma: no cover - 이 환경엔 항상 있을 것으로 추정되나 방어
    requests = None

import unicodedata

from app_export import TEAM_NAME_MAP
from app_export_multileague import LEAGUE_TEAM_MAPS

DB_PATH = 'data/football.db'
METRICS_DIR = 'data/metrics'
STATE_PATH = 'data/master/af_player_stats_state.json'
AF_BASE = 'https://v3.football.api-sports.io'
KST = timezone(timedelta(hours=9))

SEASON = 2026  # multi_league_index.html loadApiFootballFixtures와 동일 값 재사용(일관성 유지)
DAILY_CALL_BUDGET = int(os.environ.get('AF_PLAYER_STATS_DAILY_BUDGET', '90'))  # 키당, 100의 안전마진
FIXTURES_CACHE_TTL_HOURS = 20  # 리그당 시즌 전체 목록 — 하루 한 번이면 충분(파이프라인 6시간 간격)
RETRY_NO_FIXTURE_AFTER_HOURS = 24  # AF에 늦게 등록되는 경우 대비 재시도 간격

AF_LEAGUE_IDS = {  # multi_league_index.html AF_LEAGUE_IDS와 동일 출처(이미 실측 확인됨)
    'laliga': 140, 'bundesliga': 78, 'seriea': 135, 'ligue1': 61,
    'eredivisie': 88, 'championship': 180,
}


# ============================================================ 팀명 매칭
# ⚠️ 2026-07-30 발견(이번 스크립트 작성 중): app_export_multileague.py의
# _ascii_fold()를 한글 입력에 그대로 쓰면, NFKD가 완성형 한글 음절을
# 자모(초성/중성/종성, U+1100대)로 분해해버려서 뒤이은 정규식
# [^a-z가-힣0-9]가 그 자모들을 전부 걸러내 버린다 — 결과적으로 한글
# 팀명이 항상 빈 문자열로 정규화되는 잠재 버그(실측 확인:
# _norm('맨체스터 시티') == ''). 기존 코드(app_export_multileague.py의
# _LOOKUP/to_kr_league)는 항상 "영문 입력 → 한글 변환" 방향으로만
# 쓰여왔어서(BSD/AF가 주는 이름은 영문) 이 버그가 지금까지 안 드러난
# 것뿐 — 한글 입력이 들어오는 경로가 생기면(이 스크립트처럼 matches
# 테이블의 home/away가 한글일 가능성을 열어둬야 하는 경우) 바로 걸린다.
# 그래서 여기서는 그 함수를 재사용하지 않고, NFKD 분해 후 결합기호만
# 제거하고 다시 NFC로 재조합(자모 → 완성형 음절 복원)하는 안전판을 쓴다.
def _fold(name):
    n = unicodedata.normalize('NFKD', name)
    n = ''.join(c for c in n if not unicodedata.combining(c))
    return unicodedata.normalize('NFC', n)


def _norm(name):
    """비교용 정규화: 악센트 제거(한글 안전) + FC/AFC/CF 접미사 제거 + 소문자 + 영숫자/한글만."""
    if not name:
        return ''
    n = _fold(name)
    n = re.sub(r'\b(FC|AFC|CF)\b', '', n, flags=re.I)
    return re.sub(r'[^a-z가-힣0-9]', '', n.lower())


def _build_team_index():
    """정규화명 -> (league_key, kr_name). 한글 키/영문 별칭 둘 다 담아서
    matches 테이블의 home/away가 한글이든 영문이든 매칭되게 한다."""
    index = {}
    dupes = 0
    for kr, aliases in TEAM_NAME_MAP.items():
        for a in list(aliases) + [kr]:
            index[_norm(a)] = ('epl', kr)
    for lk, team_map in LEAGUE_TEAM_MAPS.items():
        for kr, aliases in team_map.items():
            for a in list(aliases) + [kr]:
                key = _norm(a)
                if key in index and index[key] != (lk, kr):
                    dupes += 1
                    continue  # 리그 간 이름 충돌 — 먼저 등록된 쪽 유지, 진단 카운트만
                index[key] = (lk, kr)
    if dupes:
        print(f'[collect_api_football_player_stats] [diag] 팀명 정규화 충돌 '
              f'{dupes}건(먼저 등록된 리그 우선, 무시함)', flush=True)
    return index


TEAM_INDEX = _build_team_index()
_EN_ALIAS = {}  # (league_key, kr_name) -> 대표 영문명(AF 조회/매칭용)
for _kr, _aliases in TEAM_NAME_MAP.items():
    if _aliases:
        _EN_ALIAS[('epl', _kr)] = _aliases[0]
for _lk, _team_map in LEAGUE_TEAM_MAPS.items():
    for _kr, _aliases in _team_map.items():
        if _aliases:
            _EN_ALIAS[(_lk, _kr)] = _aliases[0]


# ============================================================ 파일 유틸
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


def _load_state():
    return _load_json(STATE_PATH, {})


def _save_state(state):
    _atomic_write(STATE_PATH, state)


def _ensure_key_budget(keys, state):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if state.get('date') != today:
        state['date'] = today
        state['calls_used'] = {}
    for i in range(len(keys)):
        state['calls_used'].setdefault(str(i), 0)
    return state


def _budget_remaining(keys, state):
    return sum(max(0, DAILY_CALL_BUDGET - state['calls_used'].get(str(i), 0))
               for i in range(len(keys)))


# ============================================================ API-FOOTBALL 호출
def _af_get(session, keys, state, path, params):
    for i, key in enumerate(keys):
        idx = str(i)
        if state['calls_used'].get(idx, 0) >= DAILY_CALL_BUDGET:
            continue
        try:
            r = session.get(AF_BASE + path, params=params,
                             headers={'x-apisports-key': key}, timeout=15)
            state['calls_used'][idx] = state['calls_used'].get(idx, 0) + 1
            if r.status_code == 200:
                body = r.json()
                # 2026-07-31 추가: API-FOOTBALL은 HTTP 200을 주면서도 바디의
                # 'errors' 필드로 진짜 실패 사유(잘못된 키, 플랜 제한, 일일
                # 쿼터 초과 등)를 알리는 경우가 많다 — 상태코드만 보고 넘어가면
                # "성공했는데 결과가 0건"으로 오인하게 된다(실제로 이번 세션
                # 첫 실행에서 EPL 리그검색 0건, 6개리그 fixtures 조회 3건 모두
                # 0건이 나왔는데 에러 로그가 하나도 없었던 게 이 문제로 의심됨).
                # errors가 dict/list든 비어있지 않으면 실패로 간주하고 원문을
                # 그대로 로그에 남긴다.
                errors = body.get('errors') if isinstance(body, dict) else None
                if errors:
                    print(f'[collect_api_football_player_stats] {path} '
                          f'HTTP 200이지만 본문에 errors 있음(키{i+1}): '
                          f'{errors} | results={body.get("results")}',
                          flush=True)
                    continue  # 다음 키로 폴백(쿼터/플랜 문제면 다른 키는 될 수도)
                return body
            if r.status_code in (401, 403, 429):
                print(f'[collect_api_football_player_stats] 키{i+1} 응답 '
                      f'{r.status_code} → 다음 키 시도', flush=True)
                continue
            print(f'[collect_api_football_player_stats] {path} 응답 '
                  f'{r.status_code}', flush=True)
            return None
        except Exception as e:
            print(f'[collect_api_football_player_stats] {path} 요청 실패: {e}',
                  flush=True)
            continue
    return None  # 모든 키 쿼터 소진 또는 실패



def _discover_epl_league_id(session, keys, state):
    data = _af_get(session, keys, state, '/leagues',
                    {'name': 'Premier League', 'country': 'England'})
    if not data:
        return None
    for item in (data.get('response') or []):
        lg = item.get('league') or {}
        country = (item.get('country') or {}).get('name') or ''
        if (lg.get('name') or '').strip().lower() == 'premier league' \
                and country.strip().lower() == 'england':
            lid = lg.get('id')
            print(f'[collect_api_football_player_stats] EPL AF league_id '
                  f'확인: {lid}', flush=True)
            return lid
    print('[collect_api_football_player_stats] EPL AF 리그 검색 결과에서 '
          '정확히 일치하는 항목을 못 찾음', flush=True)
    return None


def _af_date_to_kst(iso_str):
    if not iso_str:
        return None
    s = iso_str.replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).strftime('%Y-%m-%d')


def _af_fixtures_for_league(session, keys, state, league_key, af_league_id):
    cache = state.setdefault('af_fixtures_cache', {})
    entry = cache.get(league_key)
    now = time.time()
    if entry and (now - entry.get('fetched_at', 0)) < FIXTURES_CACHE_TTL_HOURS * 3600:
        return entry['fixtures']
    data = _af_get(session, keys, state, '/fixtures',
                    {'league': af_league_id, 'season': SEASON})
    if data is None:
        return entry['fixtures'] if entry else []
    fixtures = []
    for f in (data.get('response') or []):
        fx = f.get('fixture') or {}
        fid = fx.get('id')
        if fid is None:
            continue
        date_kst = _af_date_to_kst(fx.get('date'))
        status = (fx.get('status') or {}).get('short')
        teams = f.get('teams') or {}
        home = (teams.get('home') or {}).get('name', '')
        away = (teams.get('away') or {}).get('name', '')
        fixtures.append({'id': fid, 'date_kst': date_kst, 'status': status,
                          'home': home, 'away': away})
    cache[league_key] = {'fetched_at': now, 'fixtures': fixtures}
    print(f'[collect_api_football_player_stats] {league_key} AF fixtures '
          f'{len(fixtures)}건 갱신', flush=True)
    return fixtures


def _find_af_fixture(fixtures, kr_home, kr_away, date_str, league_key):
    date_str = (date_str or '')[:10]
    for f in fixtures:
        if f['date_kst'] != date_str:
            continue
        h_info = TEAM_INDEX.get(_norm(f['home']))
        a_info = TEAM_INDEX.get(_norm(f['away']))
        if not h_info or not a_info:
            continue
        if h_info[0] != league_key or a_info[0] != league_key:
            continue
        if h_info[1] == kr_home and a_info[1] == kr_away:
            return f
        if h_info[1] == kr_away and a_info[1] == kr_home:
            return f  # 홈/원정 뒤바뀐 경우(드묾) — 아래 파서는 팀명으로 다시 매칭하니 안전
    return None


# ============================================================ 응답 파싱
def _num(v):
    try:
        n = float(v)
        return n
    except (TypeError, ValueError):
        return None


def parse_players_response(data):
    """API-FOOTBALL /fixtures/players 표준 응답을 팀 무관 평평한 리스트로.
    표준 스키마 기준(EPL_index.html fetchApiFootballPlayerStats로 일부
    확인, 나머지 필드는 공개 문서 기준 — 실측 전까지는 .get() 체인으로
    안전하게, 없으면 None으로 빠지게 처리)."""
    teams = data.get('response') if isinstance(data, dict) else None
    if not isinstance(teams, list) or not teams:
        return None
    out = []
    for team_block in teams:
        for p in (team_block.get('players') or []):
            player = p.get('player') or {}
            name = player.get('name')
            if not name:
                continue
            st_list = p.get('statistics') or []
            if not st_list:
                continue  # 출전기록 자체가 없는 선수(명단만 있고 미출전 등) — 빈 레코드로 신규키 오염 방지
            st = st_list[0] or {}
            goals = st.get('goals') or {}
            shots = st.get('shots') or {}
            tackles = st.get('tackles') or {}
            passes = st.get('passes') or {}
            dribbles = st.get('dribbles') or {}
            duels = st.get('duels') or {}
            games = st.get('games') or {}
            out.append({
                'name': name,
                'goals': _num(goals.get('total')),
                'assists': _num(goals.get('assists')),
                'shots': _num(shots.get('total')),
                'sot': _num(shots.get('on')),
                'tackles': _num(tackles.get('total')),
                'interceptions': _num(tackles.get('interceptions')),
                'keyPasses': _num(passes.get('key')),
                'dribbleAttempts': _num(dribbles.get('attempts')),
                'dribbleSuccess': _num(dribbles.get('success')),
                'duelsTotal': _num(duels.get('total')),
                'duelsWon': _num(duels.get('won')),
                'rating': _num(games.get('rating')),
                'minutes': _num(games.get('minutes')),
            })
    return out or None


# ============================================================ metrics 병합
_INITIAL_RE = re.compile(r'^([A-Za-zÀ-ÿ])\.\s*(.+)$')


def _resolve_metrics_key(af_name, players, last_name_index):
    """AF의 풀네임(예: 'Kevin De Bruyne')을 기존 metrics.json의 BSD식
    키(예: 'K. De Bruyne')와 매칭한다. app_export*.py의 _resolve_player와
    동일한 이니셜.성 대조 원칙(동명이인 2명 이상이면 매칭 포기)."""
    if af_name in players:
        return af_name
    af_parts = af_name.strip().split()
    if not af_parts:
        return None
    af_last = af_parts[-1]
    candidates = last_name_index.get(af_last, [])
    if len(candidates) == 1:
        return candidates[0]
    for c in candidates:
        m = _INITIAL_RE.match(c.strip())
        if m and m.group(1).lower() == af_parts[0][:1].lower() \
                and m.group(2).strip().split()[-1] == af_last:
            return c
    return None


def merge_into_metrics(metrics_path, parsed):
    """parsed(AF 선수기록 리스트)를 기존 metrics.json의 players 딕셔너리에
    병합. AF가 실제로 값을 준 필드만 덮어쓰고(None이면 기존 BSD값 유지),
    매칭 안 된 선수는 새 키로 추가(값 유실보다 낫다 — 로그로 남김)."""
    data = _load_json(metrics_path, {})
    players = data.get('players')
    if not isinstance(players, dict):
        return 0, 0

    last_name_index = defaultdict(list)
    for full_name in players:
        parts = full_name.strip().split()
        if parts:
            last_name_index[parts[-1]].append(full_name)

    field_map = {
        'shots': 'shots', 'sot': 'sot', 'tackles': 'tackles_won',
        'interceptions': 'interceptions', 'keyPasses': 'key_passes',
        'dribbleAttempts': 'dribbles', 'dribbleSuccess': 'dribbles_won',
        'duelsTotal': 'af_duels_total', 'duelsWon': 'af_duels_won',
        'rating': 'af_rating', 'minutes': 'af_minutes',
    }

    n_merged = n_new = 0
    for rec in parsed:
        key = _resolve_metrics_key(rec['name'], players, last_name_index)
        is_new = key is None
        if is_new:
            key = rec['name']
        stats = players.setdefault(key, {})
        for src_field, dst_field in field_map.items():
            v = rec.get(src_field)
            if v is not None:
                stats[dst_field] = v
        stats['_af_enriched'] = True
        n_merged += 1
        if is_new:
            n_new += 1

    if n_merged:
        data['players'] = players
        _atomic_write(metrics_path, data)
    return n_merged, n_new


# ============================================================ 대상 경기 선정
def _finished_matches():
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT match_id, home, away, date FROM matches "
            "WHERE status = 'FINISHED' "
            "AND home_goals IS NOT NULL AND away_goals IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return rows


def _resolve_match_teams(row):
    """home/away 팀명(한글이든 영문이든)을 리그키+한글팀명으로 역산.
    matches.league_id는 포맷이 불확실해서 안 쓰고, 양쪽 팀이 같은 리그의
    TEAM_INDEX에 같이 걸릴 때만 확정(안전 우선)."""
    home_info = TEAM_INDEX.get(_norm(row['home']))
    away_info = TEAM_INDEX.get(_norm(row['away']))
    if not home_info or not away_info:
        return None
    if home_info[0] != away_info[0]:
        return None
    return home_info[0], home_info[1], away_info[1]


# ============================================================ 메인
def main():
    # 2026-07-31 수정: 계정정지 풀리면 API_FOOTBALL_KEY 하나로 통합할 예정이라
    # 미리 대응 — KEY1/KEY2도 계속 인식하니(하위호환) 나중에 시크릿을 어떤
    # 조합으로 등록해도(단일 키만 / KEY1·2만 / 셋 다) 코드 수정 없이 그대로
    # 작동한다. 중복값은 자동 제거.
    keys = []
    for _name in ('API_FOOTBALL_KEY', 'API_FOOTBALL_KEY1', 'API_FOOTBALL_KEY2'):
        _v = os.environ.get(_name)
        if _v and _v not in keys:
            keys.append(_v)
    keys = [k for k in keys if k]
    if not keys:
        print('[collect_api_football_player_stats] API_FOOTBALL_KEY1/2 '
              '미등록 → 스킵', flush=True)
        return
    if requests is None:
        print('[collect_api_football_player_stats] requests 라이브러리 '
              '없음 → 스킵', flush=True)
        return
    if not os.path.exists(DB_PATH):
        print('[collect_api_football_player_stats] data/football.db 없음'
              '(db.py가 이 스크립트보다 먼저 실행돼야 함) → 스킵', flush=True)
        return

    state = _load_state()
    _ensure_key_budget(keys, state)
    session = requests.Session()

    af_league_ids = dict(AF_LEAGUE_IDS)
    epl_id = state.get('epl_af_league_id')
    if not epl_id:
        epl_id = _discover_epl_league_id(session, keys, state)
        if epl_id:
            state['epl_af_league_id'] = epl_id
    if epl_id:
        af_league_ids['epl'] = epl_id
    else:
        print('[collect_api_football_player_stats] EPL AF 리그ID 미확인 '
              '→ 이번 실행은 6개리그만 처리', flush=True)

    rows = _finished_matches()
    print(f'[collect_api_football_player_stats] DB 종료경기 {len(rows)}건 '
          f'조회', flush=True)

    done = state.setdefault('done_matches', {})
    now_utc = datetime.now(timezone.utc)
    candidates = []
    n_league_matched = n_metrics_found = 0
    for row in rows:
        resolved = _resolve_match_teams(row)
        if not resolved:
            continue
        lk, kr_home, kr_away = resolved
        if lk not in af_league_ids:
            continue
        n_league_matched += 1
        mid = row['match_id']
        metrics_path = os.path.join(METRICS_DIR, f'{mid}_metrics.json')
        if not os.path.exists(metrics_path):
            continue
        n_metrics_found += 1
        prior = done.get(mid)
        if prior:
            if prior.get('status') == 'ok':
                continue
            if prior.get('status') == 'no_af_fixture':
                try:
                    last_try = datetime.fromisoformat(prior.get('at', ''))
                    if (now_utc - last_try) < timedelta(hours=RETRY_NO_FIXTURE_AFTER_HOURS):
                        continue
                except ValueError:
                    pass
        candidates.append((mid, lk, kr_home, kr_away, row['date'], metrics_path))
    print(f'[collect_api_football_player_stats] 리그+팀 매칭 {n_league_matched}건, '
          f'metrics 파일 존재 {n_metrics_found}건, 처리 대상(미완료) '
          f'{len(candidates)}건', flush=True)

    n_ok = n_no_fixture = n_no_af_data = n_budget_stop = 0
    n_merged_players = n_new_players = 0
    for mid, lk, kr_home, kr_away, date_str, metrics_path in candidates:
        if _budget_remaining(keys, state) <= 0:
            n_budget_stop = len(candidates) - (n_ok + n_no_fixture + n_no_af_data)
            print(f'[collect_api_football_player_stats] 일일 쿼터 소진 → 중단 '
                  f'(남은 {n_budget_stop}건은 다음 실행에서 이어감)', flush=True)
            break
        af_league_id = af_league_ids[lk]
        fixtures = _af_fixtures_for_league(session, keys, state, lk, af_league_id)
        fx = _find_af_fixture(fixtures, kr_home, kr_away, date_str, lk)
        if not fx:
            done[mid] = {'status': 'no_af_fixture', 'at': now_utc.isoformat()}
            n_no_fixture += 1
            continue
        data = _af_get(session, keys, state, '/fixtures/players', {'fixture': fx['id']})
        if data is None:
            continue  # 쿼터 초과/일시 실패 — done 표시 안 하고 다음 실행에서 재시도
        parsed = parse_players_response(data)
        if not parsed:
            done[mid] = {'status': 'af_no_data', 'at': now_utc.isoformat()}
            n_no_af_data += 1
            continue
        n_merged, n_new = merge_into_metrics(metrics_path, parsed)
        n_merged_players += n_merged
        n_new_players += n_new
        done[mid] = {'status': 'ok', 'at': now_utc.isoformat(), 'players': n_merged}
        n_ok += 1

    _save_state(state)
    print(f'[collect_api_football_player_stats] 완료: 신규병합 {n_ok}경기'
          f'({n_merged_players}명, 그중 신규키 {n_new_players}명), '
          f'AF기록없음 {n_no_af_data}경기, AF fixture못찾음 {n_no_fixture}경기, '
          f'쿼터소진중단 {n_budget_stop}건, 키별 사용량 '
          f'{state.get("calls_used")}', flush=True)


if __name__ == '__main__':
    main()
