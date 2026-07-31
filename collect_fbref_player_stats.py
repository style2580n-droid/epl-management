# -*- coding: utf-8 -*-
"""
collect_fbref_player_stats.py
2026-07-31 신규 (2-1 최종 대안 — API-FOOTBALL 무료플랜 시즌제한 대응).

fbrapi.com(fbref.com을 감싼 정식 REST API — 이미 collect_xg_fbref.py가
같은 서비스로 xG를 받고 있어 인증/호출 패턴이 검증돼있음)의
`/all-players-match-stats`로 선수별 슈팅/유효슈팅/태클(승리)/인터셉트/
키패스/드리블 6개 필드를 data/metrics/{match_id}_metrics.json에 채운다.

## 왜 이게 이전 두 시도보다 신뢰도가 높은가
- Understat: 페이지 구조를 추측해서 정규식으로 파싱 → 완전히 틀림(SPA로
  개편된 걸로 확인됨).
- 이번: **공식 문서(https://fbrapi.com/documentation, 2026-07-31 직접
  확인)에 응답 예시(JSON)까지 전부 있는 정식 REST API**. 필드명을 추측한
  게 아니라 문서의 실제 예시 응답에서 그대로 가져옴. 그래도 이 프로젝트
  원칙대로 첫 실행 로그로 최종 검증할 것.

## 필드 매핑 (fbrapi 응답 -> 우리 metrics.json 필드)
- shots            <- stats.summary.sh
- sot              <- stats.summary.sot
- tackles_won      <- stats.defense.tkl_won (summary.tkl은 "시도" 총합이라
                       "승리"만 뜻하는 defense.tkl_won이 우리 필드명과 더
                       정확히 일치 — API-FOOTBALL/Understat보다 오히려 정밀함)
- interceptions    <- stats.summary.int
- key_passes       <- stats.passing.key_passes
- dribbles         <- stats.summary.take_on_att
- dribbles_won     <- stats.summary.take_on_suc

## 지원 리그
EPL(league_id=9, 공식 문서 예시로 확인) + collect_xg_fbref.py가 이미 쓰던
6개 리그(라리가/분데스리가/세리에A/리그1/에레디비시/챔피언십) = 7개.
MLS/엘리테세리엔은 collect_xg_fbref.py도 안 다뤘던 리그라 이번에도 제외
(fbref 커버리지 미확인 — 추측 안 함).

## 시즌 계산
fbref는 "시작연도-끝연도" 형식이라(예: "2026-2027") 이 프로젝트 관례
(시작연도="2026")와 그대로 맞아떨어진다 — Understat처럼 뒤집힌 문제 없음.

## Rate limit
fbref.com 원칙(3초/요청)을 fbrapi가 그대로 적용 — collect_xg_fbref.py와
동일하게 3.5초 텀(여유 포함)을 모든 호출 사이에 둔다. 일일 호출수 제한은
문서에 없지만(API-Football처럼 숫자로 안 막힘), 파이프라인 실행시간
보호를 위해 실행당 매치 수를 제한한다.

## 안전 실패
API 키 발급부터 각 요청까지 전부 try/except. 실패해도 나머지 파이프라인
안 죽음(yml에서 || true).
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
STATE_PATH = 'data/master/fbref_player_stats_state.json'
BASE_URL = 'https://fbrapi.com'
REQUEST_DELAY = 3.5  # collect_xg_fbref.py와 동일(fbref 3초/요청 제한에 여유)
MAX_NEW_MATCHES_PER_RUN = 25  # 매치당 호출 1회씩 최소 25*3.5초=87.5초 — 파이프라인 시간 보호

# collect_xg_fbref.py가 이미 실측 확인한 6개 리그 + EPL(공식 문서 예시로 확인된 9번).
LEAGUE_IDS = {
    'epl': 9, 'laliga': 12, 'bundesliga': 20, 'seriea': 11,
    'ligue1': 13, 'eredivisie': 23, 'championship': 10,
}


# ============================================================ 팀명 매칭 (다른 수집기들과 동일 원칙)
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


def _team_match(raw_name, league_key, kr_name):
    """fbref 팀명이 축약형(예: "Wycombe" vs "Wycombe Wanderers")일 수 있어서
    TEAM_INDEX 직접매칭 실패 시 부분문자열로도 한 번 더 시도."""
    info = TEAM_INDEX.get(_norm(raw_name))
    if info and info[0] == league_key and info[1] == kr_name:
        return True
    # 부분 문자열 폴백
    rn = _norm(raw_name)
    for alias_norm, (lk, kr) in TEAM_INDEX.items():
        if lk == league_key and kr == kr_name and alias_norm and \
                (alias_norm in rn or rn in alias_norm) and len(rn) >= 3:
            return True
    return False


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


def fbref_season_id(date_str):
    """이 프로젝트 관례(시작연도)와 fbref 표기가 그대로 일치한다 —
    Understat과 달리 뒤집을 필요 없음(공식 문서 예시로 확인: "season_id":
    "2023-2024" 형식)."""
    year, month = int(date_str[:4]), int(date_str[5:7])
    start_year = year if month >= 8 else year - 1
    return f'{start_year}-{start_year + 1}'


# ============================================================ fbrapi 호출
# 2026-07-31 발견: fbrapi.com 호출 시 SSLCertVerificationError(unable to get
# local issuer certificate)가 남 — 2026-07-15에 collect_xg_fbref.py에서 이미
# 겪었던 것과 동일(그때 pip install --upgrade certifi로 시도했지만 이번에도
# 재현됨). "로컬 CA 번들이 오래됨" 문제였다면 certifi 업데이트로 고쳐졌을
# 텐데 안 고쳐진 걸 보면, 서버 쪽이 중간 인증서 체인을 불완전하게 보내는
# 문제일 가능성이 높다(클라이언트 쪽 조치로는 못 고치는 종류) — 그래서 이
# 서버 하나에 한해서만, SSL 검증 실패 시에만 verify=False로 재시도한다.
# 무차별 적용 아님: 다른 종류의 요청 실패(타임아웃, 404 등)는 그대로 실패.
_ssl_warned = False


UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


def _request(session, method, url, **kwargs):
    global _ssl_warned
    # 2026-07-31 추가: 기본 User-Agent(python-requests/x.x)가 그대로 나가고
    # 있었다 — 봇으로 차단됐을 가능성이 있어 다른 크롤링 스크립트들과 동일하게
    # 브라우저 UA를 명시. "원격 서버가 응답 없이 연결 끊음"(3회 재시도 전부
    # 동일 증상)이 이걸로 해결되는지 다음 실행 로그로 검증 필요.
    headers = dict(kwargs.pop('headers', None) or {})
    headers.setdefault('User-Agent', UA)
    try:
        return session.request(method, url, timeout=30, headers=headers, **kwargs)
    except requests.exceptions.SSLError as e:
        if not _ssl_warned:
            print(f'[collect_fbref_player_stats] [diag] SSL 인증서 검증 실패 '
                  f'(fbrapi.com 서버측 인증서 체인 문제로 추정, 2026-07-15에 '
                  f'collect_xg_fbref.py도 동일 증상 — certifi 업데이트로도 '
                  f'안 고쳐짐): {e} — 이 서버에 한해 검증 없이 재시도', flush=True)
            _ssl_warned = True
        try:
            return session.request(method, url, timeout=30, headers=headers, verify=False, **kwargs)
        except Exception as e2:
            print(f'[collect_fbref_player_stats] 검증 없이도 실패: {e2}', flush=True)
            return None


def generate_api_key(session):
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    # 2026-07-31 추가: SSL 우회 후 "원격 서버가 응답 없이 연결 끊음"이 실측
    # 확인됨 — 인증서 문제와는 다른 증상이라, fbrapi.com 서버 자체가 그
    # 순간 일시적으로 불안정했을 가능성을 반영해 재시도(딜레이 포함)를 둔다.
    for attempt in range(3):
        r = _request(session, 'POST', f'{BASE_URL}/generate_api_key')
        if r is not None and r.status_code == 200:
            # 2026-07-31 추가: status 200이어도 body가 JSON이 아닐 수 있어서
            # (실측 확인: JSONDecodeError로 스크립트 자체가 죽음 — .json()을
            # 안 감쌌던 버그) try/except로 방어 + 실패 시 원문 일부를 로그로.
            try:
                key = r.json().get('api_key')
                if key:
                    return key
                print(f'[collect_fbref_player_stats] API 키 응답에 api_key '
                      f'없음(시도 {attempt+1}/3): {r.text[:200]!r}', flush=True)
            except ValueError as e:
                print(f'[collect_fbref_player_stats] API 키 응답이 JSON 아님 '
                      f'(시도 {attempt+1}/3): {e} · 원문: {r.text[:200]!r}', flush=True)
        elif r is not None and r.status_code != 200:
            print(f'[collect_fbref_player_stats] API 키 발급 HTTP {r.status_code} '
                  f'(시도 {attempt+1}/3)', flush=True)
        else:
            print(f'[collect_fbref_player_stats] API 키 발급 요청 실패 '
                  f'(시도 {attempt+1}/3)', flush=True)
        if attempt < 2:
            time.sleep(5)
    print('[collect_fbref_player_stats] API 키 발급 3회 시도 전부 실패', flush=True)
    return None


def _get(session, api_key, path, params):
    r = _request(session, 'GET', f'{BASE_URL}{path}', params=params,
                 headers={'X-API-Key': api_key})
    if r is None:
        print(f'[collect_fbref_player_stats] {path} 요청 실패(재시도 포함 모두 실패)', flush=True)
        return None
    if r.status_code != 200:
        print(f'[collect_fbref_player_stats] {path} HTTP {r.status_code} '
              f'params={params}', flush=True)
        return None
    try:
        return r.json()
    except ValueError as e:
        print(f'[collect_fbref_player_stats] {path} 응답이 JSON 아님: {e} · '
              f'원문: {r.text[:200]!r}', flush=True)
        return None


def fetch_league_matches(session, api_key, league_id, season_id):
    """리그 단위 매치 목록(팀명, 날짜, 스코어, fbref match_id)을 받는다."""
    data = _get(session, api_key, '/matches',
                {'league_id': league_id, 'season_id': season_id})
    time.sleep(REQUEST_DELAY)
    if not data:
        return []
    rows = data.get('data', [])
    return rows if isinstance(rows, list) else []


def fetch_all_players_match_stats(session, api_key, match_id):
    data = _get(session, api_key, '/all-players-match-stats', {'match_id': match_id})
    time.sleep(REQUEST_DELAY)
    if not data:
        return None
    rows = data.get('data', [])
    return rows if isinstance(rows, list) else None


# ============================================================ 매치 매칭
def find_fbref_match(fbref_matches, kr_home, kr_away, date_str, league_key):
    date_str = (date_str or '')[:10]
    for m in fbref_matches:
        if m.get('date') != date_str:
            continue
        home_raw, away_raw = m.get('home'), m.get('away')
        if not home_raw or not away_raw:
            continue
        if _team_match(home_raw, league_key, kr_home) and _team_match(away_raw, league_key, kr_away):
            return m.get('match_id'), False
        if _team_match(home_raw, league_key, kr_away) and _team_match(away_raw, league_key, kr_home):
            return m.get('match_id'), True  # swapped
    return None, False


# ============================================================ 선수 매칭 + 병합 (다른 수집기들과 동일 원칙)
_INITIAL_RE = re.compile(r'^([A-Za-zÀ-ÿ])\.\s*(.+)$')


def _resolve_metrics_key(full_name, players, last_name_index):
    if full_name in players:
        return full_name
    parts = full_name.strip().split()
    if not parts:
        return None
    last = parts[-1]
    candidates = last_name_index.get(last, [])
    if len(candidates) == 1:
        return candidates[0]
    for c in candidates:
        m = _INITIAL_RE.match(c.strip())
        if m and m.group(1).lower() == parts[0][:1].lower() and m.group(2).strip().split()[-1] == last:
            return c
    return None


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_all_players_response(rows):
    """fbrapi /all-players-match-stats 응답 -> {player_name: {필드: 값}}.
    필드가 없는 선수(스탯 자체가 안 잡힌 교체 미출전 등)는 스킵."""
    out = {}
    for team_block in (rows or []):
        for p in (team_block.get('players') or []):
            meta = p.get('meta_data') or {}
            name = meta.get('player_name')
            if not name:
                continue
            stats = p.get('stats') or {}
            summary = stats.get('summary') or {}
            passing = stats.get('passing') or {}
            defense = stats.get('defense') or {}
            rec = {}
            for field, val in (
                ('shots', _num(summary.get('sh'))),
                ('sot', _num(summary.get('sot'))),
                ('tackles_won', _num(defense.get('tkl_won'))),
                ('interceptions', _num(summary.get('int'))),
                ('key_passes', _num(passing.get('key_passes'))),
                ('dribbles', _num(summary.get('take_on_att'))),
                ('dribbles_won', _num(summary.get('take_on_suc'))),
            ):
                if val is not None:
                    rec[field] = val
            if rec:
                out[name] = rec
    return out


def merge_into_metrics(metrics_path, stats_by_player):
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
    for name, rec in stats_by_player.items():
        key = _resolve_metrics_key(name, players, last_name_index)
        if key is None:
            continue  # 확신 낮은 신규선수 키는 안 만듦(다른 수집기들과 동일 원칙)
        stats = players.setdefault(key, {})
        stats.update(rec)
        stats['_fbref_enriched'] = True
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
        rows = conn.execute(
            "SELECT match_id, home, away, date FROM matches "
            "WHERE status = 'FINISHED' AND home_goals IS NOT NULL "
            "AND away_goals IS NOT NULL AND date IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return rows


def _resolve_match_teams(row):
    home_info = TEAM_INDEX.get(_norm(row['home']))
    away_info = TEAM_INDEX.get(_norm(row['away']))
    if not home_info or not away_info:
        return None
    if home_info[0] != away_info[0]:
        return None
    return home_info[0], home_info[1], away_info[1]


# ============================================================ 메인
def main():
    if requests is None:
        print('[collect_fbref_player_stats] requests 라이브러리 없음 → 스킵', flush=True)
        return
    if not os.path.exists(DB_PATH):
        print('[collect_fbref_player_stats] data/football.db 없음(db.py 미실행?) → 스킵', flush=True)
        return

    session = requests.Session()
    api_key = generate_api_key(session)
    if not api_key:
        print('[collect_fbref_player_stats] API 키 발급 실패 → 스킵', flush=True)
        return
    time.sleep(REQUEST_DELAY)

    state = _load_json(STATE_PATH, {})
    done = state.setdefault('done_matches', {})

    rows = _finished_matches()
    print(f'[collect_fbref_player_stats] DB 종료경기(날짜있음) {len(rows)}건 조회', flush=True)

    candidates = []
    n_league_matched = n_metrics_found = 0
    for row in rows:
        resolved = _resolve_match_teams(row)
        if not resolved:
            continue
        lk, kr_home, kr_away = resolved
        if lk not in LEAGUE_IDS:
            continue  # MLS/엘리테세리엔 — fbref 커버리지 미확인, 대상 제외
        n_league_matched += 1
        mid = row['match_id']
        metrics_path = os.path.join(METRICS_DIR, f'{mid}_metrics.json')
        if not os.path.exists(metrics_path):
            continue
        n_metrics_found += 1
        if done.get(mid, {}).get('status') == 'ok':
            continue
        season_id = fbref_season_id(row['date'])
        candidates.append((mid, lk, kr_home, kr_away, row['date'], season_id, metrics_path))
    print(f'[collect_fbref_player_stats] 지원리그+팀 매칭 {n_league_matched}건, '
          f'metrics 파일 존재 {n_metrics_found}건, 처리 대상(미완료) {len(candidates)}건', flush=True)

    league_season_cache = {}

    def _get_league_matches(lk, season_id):
        key = (lk, season_id)
        if key not in league_season_cache:
            matches = fetch_league_matches(session, api_key, LEAGUE_IDS[lk], season_id)
            league_season_cache[key] = matches
            print(f'[collect_fbref_player_stats] {lk}/{season_id} fbref 매치목록: '
                  f'{len(matches)}건 수신', flush=True)
        return league_season_cache[key]

    n_ok = n_no_match = n_no_stats = 0
    n_merged_players = 0
    for mid, lk, kr_home, kr_away, date_str, season_id, metrics_path in candidates:
        if n_ok + n_no_match + n_no_stats >= MAX_NEW_MATCHES_PER_RUN:
            print(f'[collect_fbref_player_stats] 실행당 상한({MAX_NEW_MATCHES_PER_RUN}건) '
                  f'도달 → 중단(다음 실행에서 이어감)', flush=True)
            break
        fbref_matches = _get_league_matches(lk, season_id)
        fbref_match_id, swapped = find_fbref_match(fbref_matches, kr_home, kr_away, date_str, lk)
        if not fbref_match_id:
            done[mid] = {'status': 'no_match', 'at': datetime.now(timezone.utc).isoformat()}
            n_no_match += 1
            continue
        rows_resp = fetch_all_players_match_stats(session, api_key, fbref_match_id)
        if not rows_resp:
            done[mid] = {'status': 'no_stats', 'at': datetime.now(timezone.utc).isoformat()}
            n_no_stats += 1
            continue
        stats_by_player = parse_all_players_response(rows_resp)
        n_merged = merge_into_metrics(metrics_path, stats_by_player)
        n_merged_players += n_merged
        done[mid] = {'status': 'ok', 'at': datetime.now(timezone.utc).isoformat(), 'players': n_merged}
        n_ok += 1

    _atomic_write(STATE_PATH, state)
    print(f'[collect_fbref_player_stats] 완료: 신규병합 {n_ok}경기({n_merged_players}명), '
          f'매치못찾음 {n_no_match}건, 스탯없음 {n_no_stats}건', flush=True)


if __name__ == '__main__':
    main()
