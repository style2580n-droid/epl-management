# -*- coding: utf-8 -*-
"""
collect_bsd_player_stats.py
2026-07-31 신규 (2-1 최종 대안 — fbrapi.com이 봇탐지로 완전히 막혀서 전환).

BSD 공식 OpenAPI 스펙(football-schema.json, 2026-07-31 사용자가 BSD 공식
사이트에서 직접 받은 것 — 추측 아님)에서 확인된 `/api/v2/events/{id}/
player-stats/` 엔드포인트로 선수별 슈팅/유효슈팅/태클(승리)/인터셉트/
키패스/드리블 6개 필드를 data/metrics/{match_id}_metrics.json에 채운다.

## 왜 이게 지금까지 중 제일 신뢰도 높은가
- Understat: 페이지 구조 추측 → SPA로 개편된 걸로 확인, 완전 실패.
- fbrapi.com: 공식 문서는 진짜였지만, 실제 호출은 봇탐지 JS챌린지
  페이지로 리다이렉트당함 — requests로는 절대 못 뚫음(자바스크립트 실행
  불가), 완전 포기.
- 이번(BSD): 이미 이 파이프라인 전체가 쓰고 있는 **같은 소스**다. 그리고
  결정적으로, **data/football.db의 matches.match_id가 BSD 이벤트 ID
  그대로**라서(db.py의 load_events()가 파일명=BSD id를 그대로 씀 —
  기존 코드로 이미 확인된 사실) 팀명 매칭도 날짜 매칭도 시즌 계산도
  전혀 필요 없다. 이번 세션 내내 다른 소스들을 괴롭혔던 "팀명이 안 맞음"
  류 실패 자체가 구조적으로 발생할 수 없다.

## 필드 매핑 (BSD player-stats 응답 -> 우리 metrics.json 필드, 2026-08-01
## 실측 확인 — 스펙엔 예시 응답이 없어서 처음엔 아래와 다르게 잘못 가정했었음)
- shots         <- total_shots
- sot           <- shots_on_target
- tackles_won   <- won_tackle (total_tackle은 "시도" — 우리 필드명(승리)과
                   더 정확히 일치하는 won_tackle을 씀)
- interceptions <- interception
- key_passes    <- key_pass
- dribbles      <- total_contest (처음엔 dribble_attempted로 잘못 가정했었음)
- dribbles_won  <- won_contest (처음엔 dribble_won으로 잘못 가정했었음)
- tackles(별칭) <- tackles_won과 동일값. app_export.py는 'tackles_won'을,
                   player_profiler.py/season_aggregator.py/transfer_impact.py
                   (이번 세션 이전부터 있던 파일들, 실측 확인)는 'tackles'를
                   읽어서 코드베이스 안에 두 관례가 공존 — 둘 다 채움.

## 선수 이름 매칭 (2026-08-01 실측 확인 — 처음 가정과 다름)
player-stats 응답 레코드엔 'player' 중첩객체가 없고 player_id(정수)만 있다
(처음엔 player.short_name이 바로 있을 거라고 잘못 가정했었음). 그래서
/api/v2/players/{id}/ 로 이름(name/short_name)을 별도 조회해서
data/master/bsd_player_names_cache.json에 캐시해뒀다가 재사용한다 —
매치마다 최대 40명씩 매번 새로 조회하면 너무 느려서, 한 번 조회한 선수는
다음 실행부터 캐시로 즉시 해결된다(캐시가 쌓일수록 실행이 빨라짐).
metrics.json 기존 키(BSD incidents 기반 "이니셜.성" 형식)와 매칭할 때는
다른 수집기들과 동일하게 short_name 우선 + 이니셜.성 폴백을 쓴다.

## 인증
파이프라인이 이미 쓰는 BSD_API_KEY/BSD_API_KEY2를 그대로 재사용
(Authorization: Token {키}, EPL_index.html의 RUNTIME_CONFIG.bsdToken과
동일한 인증 방식 — 이미 검증된 패턴).

## 안전 실패
요청/파싱 전부 try/except. 실패해도 나머지 파이프라인 안 죽음(yml에서
|| true). 실행당 매치 수(25건)/신규 선수이름 조회 수(300명) 둘 다 상한을
둬서 파이프라인 시간을 보호한다.
"""
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

from app_export import TEAM_NAME_MAP
from app_export_multileague import LEAGUE_TEAM_MAPS

DB_PATH = 'data/football.db'
METRICS_DIR = 'data/metrics'
STATE_PATH = 'data/master/bsd_player_stats_state.json'
PLAYER_NAME_CACHE_PATH = 'data/master/bsd_player_names_cache.json'
BSD_BASE = 'https://sports.bzzoiro.com'
# 2026-08-01 추가: 응답 파싱 로직이 바뀔 때마다(예: 이번에 player_stats
# 키/total_contest 필드명을 뒤늦게 알아낸 것처럼) 이 번호를 올린다. 예전
# 버전에서 파싱 버그 때문에 잘못 'no_data'로 마킹해둔 243건이 실측 확인
# 됐는데, 그게 그대로 남아있으면 파싱을 고쳐도 "이미 처리됨"으로 취급돼
# 재시도가 영원히 안 된다 — 버전이 바뀌면 done_matches를 자동으로 비워서
# 이런 상태 오염이 다음에 또 생겨도 자동 복구되게 한다.
STATE_VERSION = 2
REQUEST_DELAY = 0.4  # collect_fixtures.py가 쓰던 BSD 요청 간 딜레이(0.3초)와 비슷한 수준
# 2026-08-01 수정: player-stats 응답에 선수 "이름"이 없고 player_id(정수)만
# 있는 게 실측 확인돼서, /api/v2/players/{id}/ 로 이름을 별도 조회해야 한다
# (캐시해서 재사용 — 한 번 조회한 선수는 다음 실행부터 재조회 안 함). 이름
# 조회가 추가되면서 매치당 API 호출이 최대 40배까지 늘 수 있어 실행당
# 상한을 크게 낮춘다.
MAX_NEW_MATCHES_PER_RUN = 25
MAX_NEW_PLAYER_NAMES_PER_RUN = 300  # 실행당 신규 선수이름 조회 상한(시간 보호)


# ============================================================ 팀명 매칭
# 2-1 스크립트들이 전부 쓰던 것과 동일 — 여기서는 "어느 리그 소속인지"
# 판별용으로만 쓴다(대상 경기 선정, player-stats 조회 자체엔 불필요).
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


def _build_team_index():
    index = {}
    for kr, aliases in TEAM_NAME_MAP.items():
        for a in list(aliases) + [kr]:
            index[_norm(a)] = ('epl', kr)
    for lk, team_map in LEAGUE_TEAM_MAPS.items():
        for kr, aliases in team_map.items():
            for a in list(aliases) + [kr]:
                index.setdefault(_norm(a), (lk, kr))
    return index


TEAM_INDEX = _build_team_index()


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


# ============================================================ BSD 호출
def _get_keys():
    keys = []
    for name in ('BSD_API_KEY', 'BSD_API_KEY2'):
        v = os.environ.get(name)
        if v and v not in keys:
            keys.append(v)
    return keys


# 2026-07-31 추가: 150건 시도해서 전부 실패했는데 HTTP 에러 로그가 하나도
# 없었다 — 404를 "정상(그 경기엔 데이터 없음)"으로 조용히 처리하던 게
# 원인 진단을 가렸다. 상태코드별 집계 + 첫 응답 원문 샘플을 남겨서 진짜
# 원인(전부 404인지/인증실패인지/응답구조가 다른지)을 다음 로그로 확정한다.
_status_counts = {}
_sample_logged = False
_sample_row_logged = False


def fetch_player_name(session, keys, player_id):
    """/api/v2/players/{id}/ 로 name/short_name을 받는다(공식 스펙
    PlayerDetailV2Schema로 필드명 확인됨 — 추측 아님)."""
    url = f'{BSD_BASE}/api/v2/players/{player_id}/'
    for key in keys:
        try:
            r = session.get(url, headers={'Authorization': f'Token {key}'}, timeout=15)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except ValueError:
            continue
        name = data.get('name')
        short_name = data.get('short_name')
        if name or short_name:
            return {'name': name, 'short_name': short_name or None}
        return None
    return None


def fetch_player_stats(session, keys, event_id):
    """/api/v2/events/{id}/player-stats/ 호출. 페이지네이션 있으면 따라감
    (스펙상 PaginatedPlayerStatList — count/next/previous/results)."""
    global _sample_logged
    url = f'{BSD_BASE}/api/v2/events/{event_id}/player-stats/'
    params = {'limit': 50}
    all_results = []
    for key in keys:
        try:
            r = session.get(url, params=params,
                             headers={'Authorization': f'Token {key}'}, timeout=15)
        except Exception as e:
            print(f'[collect_bsd_player_stats] event={event_id} 요청 실패: {e}', flush=True)
            continue
        _status_counts[r.status_code] = _status_counts.get(r.status_code, 0) + 1
        if not _sample_logged:
            print(f'[collect_bsd_player_stats] [diag] event={event_id} 첫 응답 '
                  f'HTTP {r.status_code} · 본문 앞 300자: {r.text[:300]!r}', flush=True)
            _sample_logged = True
        if r.status_code == 404:
            return None  # 이 경기는 BSD에 player-stats 자체가 없음(정상적인 경우)
        if r.status_code != 200:
            print(f'[collect_bsd_player_stats] event={event_id} HTTP {r.status_code}: '
                  f'{r.text[:200]!r}', flush=True)
            continue
        try:
            data = r.json()
        except ValueError as e:
            print(f'[collect_bsd_player_stats] event={event_id} JSON 아님: {e}', flush=True)
            continue
        # 2026-08-01 수정: 실제 응답이 {"event_id":..., "count":N,
        # "player_stats":[...]} 형태인 게 실측 확인됨(스펙엔 "No response
        # body"라 예시가 없어서 페이지네이션 래퍼로 잘못 가정했었음 —
        # 그래서 매번 성공 응답을 받고도 파싱을 못 해 전부 no_data로
        # 새나가고 있었다). player_stats를 최우선으로 확인하고, 혹시
        # 다른 형태로 올 경우도 대비해 결과/리스트 형태도 폴백으로 둔다.
        if isinstance(data, dict) and 'player_stats' in data:
            all_results.extend(data.get('player_stats') or [])
        elif isinstance(data, dict) and 'results' in data:
            all_results.extend(data.get('results') or [])
            next_url = data.get('next')
            while next_url:
                try:
                    r2 = session.get(next_url, headers={'Authorization': f'Token {key}'}, timeout=15)
                    if r2.status_code != 200:
                        break
                    d2 = r2.json()
                    all_results.extend(d2.get('results') or [])
                    next_url = d2.get('next')
                except Exception:
                    break
        elif isinstance(data, list):
            all_results.extend(data)
        return all_results
    return None  # 모든 키 실패


# ============================================================ 파싱 + 병합
_INITIAL_RE = re.compile(r'^([A-Za-zÀ-ÿ])\.\s*(.+)$')


def _resolve_metrics_key(candidate_names, players, last_name_index):
    """short_name('M. Salah')과 name('Mohamed Salah') 둘 다 시도 — short_name이
    metrics.json 기존 키 형식과 그대로 같을 가능성이 높아 1순위."""
    for name in candidate_names:
        if not name:
            continue
        if name in players:
            return name
    # 이니셜.성 폴백(다른 수집기들과 동일 원칙)
    for name in candidate_names:
        if not name:
            continue
        parts = name.strip().split()
        if not parts:
            continue
        last = parts[-1]
        cands = last_name_index.get(last, [])
        if len(cands) == 1:
            return cands[0]
        for c in cands:
            m = _INITIAL_RE.match(c.strip())
            if m and m.group(1).lower() == parts[0][:1].lower() and \
                    m.group(2).strip().split()[-1] == last:
                return c
    return None


def _num(v):
    """혹시 몰라 문자열/정수 둘 다 방어적으로 처리(BSD 응답 필드가 실제로는
    전부 정수로 왔지만, 다른 필드에서도 이런 타입 이슈가 재발할 수 있어 유지)."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def parse_player_stats(results, player_names):
    """results, player_names({str(player_id): {'name','short_name'}}) ->
    ([((name_short, name_full), rec), ...], missing_player_ids(set)).
    missing_player_ids는 이번에 이름 캐시에 없어서 못 채운 선수id들 —
    호출부가 이걸로 다음에 조회할 대상을 정한다."""
    global _sample_row_logged
    out = []
    missing = set()
    for row in (results or []):
        if not _sample_row_logged:
            # 2026-08-01 추가: 최상위 응답 구조(results vs player_stats)가
            # 스펙 예시 없이 틀렸던 전례가 있어서, 개별 선수 레코드 구조도
            # 한 번 그대로 로그에 남긴다 — 필드명이 실제로 맞는지 다음 로그로
            # 바로 확정하기 위함. (2026-08-01 실측 확인: 이 레코드엔 'player'
            # 중첩객체가 없고 'player_id'(정수)만 있음 — 이름은 /api/v2/
            # players/{id}/ 로 별도 조회 필요. 드리블 필드명도 예상했던
            # dribble_attempted/dribble_won이 아니라 total_contest/
            # won_contest였음.)
            print(f'[collect_bsd_player_stats] [diag] 선수 레코드 원본 샘플: {row}',
                  flush=True)
            _sample_row_logged = True
        pid = row.get('player_id')
        if pid is None:
            continue
        info = player_names.get(str(pid))
        if not info:
            missing.add(pid)
            continue
        name_full = info.get('name')
        name_short = info.get('short_name')
        if not name_full and not name_short:
            continue
        rec = {}
        for field, src_key in (
            ('shots', 'total_shots'), ('sot', 'shots_on_target'),
            ('tackles_won', 'won_tackle'), ('interceptions', 'interception'),
            ('key_passes', 'key_pass'),
            ('dribbles', 'total_contest'), ('dribbles_won', 'won_contest'),
        ):
            val = _num(row.get(src_key))
            if val is not None:
                rec[field] = val
        # 2026-08-01 추가: app_export.py는 'tackles_won'을 읽는데,
        # player_profiler.py/season_aggregator.py/transfer_impact.py(이번
        # 세션 이전부터 있던 파일, 실측 확인)는 'tackles'를 읽는다 — 코드베이스
        # 안에 서로 다른 필드명 관례가 이미 공존하고 있어서, 두 이름 다 같은
        # 값으로 저장해야 양쪽 소비처가 다 정상 작동한다.
        if 'tackles_won' in rec:
            rec['tackles'] = rec['tackles_won']
        if rec:
            out.append(((name_short, name_full), rec))
    return out, missing


def merge_into_metrics(metrics_path, parsed):
    data = _load_json(metrics_path, {})
    players = data.get('players')
    if not isinstance(players, dict):
        return 0

    last_name_index = defaultdict(list)
    for full_name in players:
        parts = full_name.strip().split()
        if parts:
            last_name_index[parts[-1]].append(full_name)

    n_merged = 0
    for (name_short, name_full), rec in parsed:
        key = _resolve_metrics_key([name_short, name_full], players, last_name_index)
        if key is None:
            continue
        stats = players.setdefault(key, {})
        stats.update(rec)
        stats['_bsd_playerstats_enriched'] = True
        n_merged += 1

    if n_merged:
        data['players'] = players
        _atomic_write(metrics_path, data)
    return n_merged


# ============================================================ 대상 경기 선정
def _finished_matches():
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # 2026-07-31 수정: match_id는 "홈팀_원정팀_숫자" 합성id라 BSD의 진짜
        # 숫자 이벤트 id가 아님(실측 확인된 사실 — 이전 버전이 이걸 event_id로
        # 그대로 썼다가 전부 404가 났었음). db.py가 h2h 역조회로 채워준
        # bsd_event_id 컬럼(진짜 BSD id)이 있는 경기만 대상으로 삼는다.
        rows = conn.execute(
            "SELECT match_id, home, away, bsd_event_id FROM matches "
            "WHERE status = 'FINISHED' AND home_goals IS NOT NULL "
            "AND away_goals IS NOT NULL AND bsd_event_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return rows


def _resolve_league(row):
    """대상 경기 카운트/로깅용(우리가 추적하는 리그 소속인지 확인) — player-stats
    조회 자체엔 안 씀(match_id로 바로 조회하니까)."""
    home_info = TEAM_INDEX.get(_norm(row['home']))
    away_info = TEAM_INDEX.get(_norm(row['away']))
    if not home_info or not away_info or home_info[0] != away_info[0]:
        return None
    return home_info[0]


# ============================================================ 메인
def main():
    if requests is None:
        print('[collect_bsd_player_stats] requests 라이브러리 없음 → 스킵', flush=True)
        return
    if not os.path.exists(DB_PATH):
        print('[collect_bsd_player_stats] data/football.db 없음(db.py 미실행?) → 스킵', flush=True)
        return

    keys = _get_keys()
    if not keys:
        print('[collect_bsd_player_stats] BSD_API_KEY/KEY2 미등록 → 스킵', flush=True)
        return

    state = _load_json(STATE_PATH, {})
    if state.get('version') != STATE_VERSION:
        print(f'[collect_bsd_player_stats] state 버전 변경 감지'
              f'({state.get("version")} → {STATE_VERSION}) — done_matches 초기화 '
              f'(예전 파싱버그 시절 잘못된 no_data 마킹 제거)', flush=True)
        state = {'version': STATE_VERSION}
    done = state.setdefault('done_matches', {})
    player_names = _load_json(PLAYER_NAME_CACHE_PATH, {})  # {str(player_id): {name, short_name}}
    session = requests.Session()

    rows = _finished_matches()
    print(f'[collect_bsd_player_stats] DB 종료경기 {len(rows)}건 조회', flush=True)

    candidates = []
    n_league_matched = n_metrics_found = 0
    for row in rows:
        lk = _resolve_league(row)
        if lk:
            n_league_matched += 1
        mid = row['match_id']  # 합성id(홈팀_원정팀_숫자) — 파일경로/state 키용
        real_event_id = row['bsd_event_id']  # 진짜 BSD 숫자id — 조회용
        metrics_path = os.path.join(METRICS_DIR, f'{mid}_metrics.json')
        if not os.path.exists(metrics_path):
            continue
        n_metrics_found += 1
        if done.get(mid, {}).get('status') in ('ok', 'no_data'):
            continue
        candidates.append((mid, real_event_id, metrics_path))
    print(f'[collect_bsd_player_stats] 우리 추적리그 소속 {n_league_matched}건(참고용, '
          f'조회는 전체 대상), metrics 파일 존재 {n_metrics_found}건, '
          f'처리 대상(미완료) {len(candidates)}건, 선수이름 캐시 {len(player_names)}명 보유',
          flush=True)

    # 2026-08-01 추가: 1단계 — 실행당 상한만큼 매치별 player-stats를 먼저
    # 전부 받아둔다(캐시에 없는 선수 이름이 있어도 여기선 스킵 안 함).
    match_results = []  # [(mid, metrics_path, results_or_None), ...]
    n_fetch_no_data = 0
    for mid, real_event_id, metrics_path in candidates[:MAX_NEW_MATCHES_PER_RUN]:
        results = fetch_player_stats(session, keys, real_event_id)
        time.sleep(REQUEST_DELAY)
        if results is None:
            done[mid] = {'status': 'no_data', 'at': datetime.now(timezone.utc).isoformat()}
            n_fetch_no_data += 1
            continue
        match_results.append((mid, metrics_path, results))
    if len(candidates) > MAX_NEW_MATCHES_PER_RUN:
        print(f'[collect_bsd_player_stats] 실행당 상한({MAX_NEW_MATCHES_PER_RUN}건) '
              f'적용 → 나머지는 다음 실행에서 이어감', flush=True)

    # 2026-08-01 추가: 2단계 — 이번에 받은 응답들에 등장하는 고유 player_id 중
    # 캐시에 없는 것만 모아서 이름을 일괄 조회한다(같은 선수가 여러 경기에
    # 나와도 한 번만 조회 — 캐시가 쌓일수록 이후 실행은 점점 빨라짐).
    all_player_ids = set()
    for _, _, results in match_results:
        for row in (results or []):
            pid = row.get('player_id')
            if pid is not None:
                all_player_ids.add(pid)
    missing_ids = [pid for pid in all_player_ids if str(pid) not in player_names]
    print(f'[collect_bsd_player_stats] 이번 응답에 등장한 고유 선수 {len(all_player_ids)}명, '
          f'이름 캐시에 없는 선수 {len(missing_ids)}명', flush=True)

    n_name_fetched = n_name_failed = 0
    for pid in missing_ids[:MAX_NEW_PLAYER_NAMES_PER_RUN]:
        info = fetch_player_name(session, keys, pid)
        time.sleep(REQUEST_DELAY)
        if info:
            player_names[str(pid)] = info
            n_name_fetched += 1
        else:
            n_name_failed += 1
    if len(missing_ids) > MAX_NEW_PLAYER_NAMES_PER_RUN:
        print(f'[collect_bsd_player_stats] 선수이름 조회도 실행당 상한'
              f'({MAX_NEW_PLAYER_NAMES_PER_RUN}명) 적용 → 나머지는 다음 실행', flush=True)
    _atomic_write(PLAYER_NAME_CACHE_PATH, player_names)
    print(f'[collect_bsd_player_stats] 선수이름 신규조회 {n_name_fetched}명 성공, '
          f'{n_name_failed}명 실패 → 캐시 총 {len(player_names)}명', flush=True)

    # 2026-08-01 추가: 3단계 — 이제 이름이 채워진 만큼 파싱+병합.
    n_ok = n_no_data = n_no_merge = 0
    n_merged_players = 0
    for mid, metrics_path, results in match_results:
        parsed, missing = parse_player_stats(results, player_names)
        if not parsed:
            # 이번 실행에서 이름을 못 채운 선수만 있었다면 완료(ok)로 확정
            # 짓지 않고 다음 실행에서 재시도하도록 status를 남기지 않는다
            # (done에 아무것도 안 남기면 다음 실행의 candidates에 다시 포함됨).
            if missing:
                n_no_data += 1
                continue
            done[mid] = {'status': 'no_data', 'at': datetime.now(timezone.utc).isoformat()}
            n_no_data += 1
            continue
        n_merged = merge_into_metrics(metrics_path, parsed)
        n_merged_players += n_merged
        if n_merged and not missing:
            # missing이 남아있으면(일부 선수만 이름 확보) 아직 완료가 아니므로
            # ok로 확정 안 짓고 다음 실행에서 나머지 선수까지 마저 채우게 둔다.
            done[mid] = {'status': 'ok', 'at': datetime.now(timezone.utc).isoformat(), 'players': n_merged}
            n_ok += 1
        elif n_merged:
            n_ok += 1  # 부분 병합 — done엔 안 남겨서 다음 실행에 재시도
        else:
            done[mid] = {'status': 'no_data', 'at': datetime.now(timezone.utc).isoformat()}
            n_no_merge += 1

    _atomic_write(STATE_PATH, state)
    print(f'[collect_bsd_player_stats] 완료: 신규병합 {n_ok}경기({n_merged_players}명), '
          f'응답없음/404 {n_fetch_no_data + n_no_data}건, 선수매칭 실패 {n_no_merge}건, '
          f'키별 사용 {len(keys)}개, 상태코드별 집계: {_status_counts}', flush=True)


if __name__ == '__main__':
    main()
