# -*- coding: utf-8 -*-
"""
collect_understat_shots.py
2026-07-31 신규 (2-1 보조/2순위 — API-FOOTBALL 계정정지 대응).

Understat 웹페이지에 임베드된 슈팅 데이터를 파싱해서 선수별 "슈팅수(shots)/
유효슈팅수(sot)" 2개 필드만 data/metrics/{match_id}_metrics.json에 채운다.
나머지 4개 필드(태클/키패스/드리블/인터셉트)는 Understat에 애초에 없어서
여전히 공백 — 이건 API-FOOTBALL 없이는 못 채운다(확정, 대체 소스 없음).

## 왜 이렇게까지 하나
API-FOOTBALL 계정 정지가 언제 풀릴지 몰라서(1순위, 계속 대기), 그 사이라도
"슈팅/유효슈팅" 2개 필드만이라도 채워두자는 2순위 임시 대응. 계정 정지가
풀리면 이 스크립트는 끄는 게 맞다(공식 API가 훨씬 안정적이고 풍부함 —
크롤링은 항상 이보다 아래 등급 대안).

## ⚠️ 이 스크립트의 근본적 한계 (크롤링이라 API와 다름)
1. **공식 API 아님** — Understat이 공개 문서로 제공하는 게 아니라, 페이지에
   임베드된 JS 변수(`shotsData`/`datesData`)를 정규식으로 뽑아 파싱한다.
   사이트가 마크업/변수명을 바꾸면 아무 경고 없이 조용히 깨진다.
2. **이용약관 회색지대** — 크롤링 자체가 완전한 정당성이 보장된 방식이 아니다.
   요청 빈도를 낮게 유지하고(매치 페이지 사이 1.5초 딜레이), 실행당 신규
   매치 처리 건수를 제한(30건)해서 부담을 최소화한다.
3. **지원 리그 한계** — Understat은 EPL/라리가/분데스리가/세리에A/리그1
   5개 리그만 커버한다. 에레디비시/챔피언십/MLS/엘리테세리엔은 Understat
   자체에 데이터가 없어 이 스크립트로 영영 못 채운다(사용자 확인 요청 기반
   판단 — 대체 소스 없으면 그 4개 리그는 계속 공백).
4. **시즌 파라미터(2026)** — 이 코드베이스 전체가 26/27시즌을 "2026"으로
   표기하는 관례를 그대로 따랐다. Understat도 같은 방식(시작연도)일 것으로
   가정 — 첫 실행 로그의 "리그당 수신 매치 수"로 검증할 것.

## 안전 실패
파싱/네트워크 각 단계 전부 try/except. 실패해도 나머지 파이프라인 안 죽음
(yml에서 || true). 무슨 일이 있어도 이 스크립트 하나가 파이프라인 전체를
막으면 안 된다 — 크롤링은 API보다 훨씬 깨지기 쉬우므로 특히 중요.
"""
import glob
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    requests = None

from app_export import TEAM_NAME_MAP
from app_export_multileague import LEAGUE_TEAM_MAPS

DB_PATH = 'data/football.db'
METRICS_DIR = 'data/metrics'
STATE_PATH = 'data/master/understat_shots_state.json'
UNDERSTAT_BASE = 'https://understat.com'
SEASON = '2026'  # 이 코드베이스 전체 관례(26/27시즌=2026)와 동일 — 검증 필요(위 docstring 참고)
REQUEST_DELAY_SEC = 1.5  # 매치 페이지 요청 사이 딜레이(크롤링 매너)
MAX_NEW_MATCHES_PER_RUN = 30  # 한 실행당 신규 매치 처리 상한(부담 최소화 + 파이프라인 시간 보호
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

# Understat이 실제로 커버하는 리그만(확인된 5개 — 나머지 4개 리그는 대상 자체에서 제외)
UNDERSTAT_LEAGUES = {
    'epl': 'EPL', 'laliga': 'La_liga', 'bundesliga': 'Bundesliga',
    'seriea': 'Serie_A', 'ligue1': 'Ligue_1',
}

# understat 결과 코드 중 "유효슈팅"(골키퍼 선방 대상이 된 슈팅)으로 칠 것들.
# BlockedShot(수비수 차단)/MissedShots(완전히 빗나감)/ShotOnPost(골대 맞음,
# 통상 유효슈팅으로 안 침)는 제외 — 표준 축구 스탯 관례.
SOT_RESULTS = {'Goal', 'SavedShot'}


# ============================================================ 팀명 매칭 (collect_api_football_player_stats.py와 동일 원칙)
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


# ============================================================ 파일 유틸 (기존 스크립트들과 동일 패턴)
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


# ============================================================ Understat 페이지 파싱
def _extract_js_json(html, var_name):
    """Understat 페이지에 `var X = JSON.parse('...')` 형태로 임베드된 데이터를
    뽑아 실제 파이썬 객체로 복원한다. 그 문자열은 JS 문자열 리터럴 이스케이프
    (\\xHH 등)로 UTF-8 바이트를 인코딩해둔 것이라, unicode_escape로 디코드한
    뒤 latin1로 재인코드 → utf-8로 재디코드하는 왕복이 필요하다(Understat
    스크래핑에서 널리 쓰이는 알려진 패턴 — 공식 문서는 없음)."""
    m = re.search(var_name + r"\s*=\s*JSON\.parse\('(.*?)'\)", html)
    if not m:
        # 2026-07-31 추가: 실전 첫 실행에서 5개 리그 전부 0건이 나왔는데 에러
        # 로그가 하나도 안 찍혀서 원인을 못 봤다 — 정규식이 아예 안 맞는
        # 경우를 조용히 넘기던 게 원인이었다. 이제 HTML 길이, 변수명 자체가
        # 존재하는지, 존재한다면 그 주변 200자를 로그로 남겨서 다음 실행
        # 로그로 실제 원인(변수명이 다른지/JSON.parse 패턴이 다른지/페이지
        # 자체가 아예 다른 내용인지)을 바로 알 수 있게 한다.
        idx = html.find(var_name)
        if idx == -1:
            print(f'[collect_understat_shots] [diag] {var_name} 정규식 매치 실패 — '
                  f'변수명 자체가 HTML(길이 {len(html)}자)에 없음. 페이지 구조가 '
                  f'바뀌었거나 차단 페이지를 받았을 가능성', flush=True)
        else:
            snippet = html[idx:idx+200].replace('\n', ' ')
            print(f'[collect_understat_shots] [diag] {var_name} 변수명은 있는데 '
                  f'JSON.parse(\'...\') 정규식은 안 맞음 — 주변 200자: {snippet}',
                  flush=True)
        return None
    raw = m.group(1)
    try:
        decoded = raw.encode('utf-8').decode('unicode_escape').encode('latin1').decode('utf-8')
        return json.loads(decoded)
    except (UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as e:
        print(f'[collect_understat_shots] [diag] {var_name} 디코드 실패: {e} '
              f'(사이트 구조가 바뀌었을 가능성 — 이 함수부터 재검토할 것)', flush=True)
        return None


def _fetch(session, url):
    try:
        r = session.get(url, headers={'User-Agent': UA}, timeout=15)
        if r.status_code != 200:
            print(f'[collect_understat_shots] {url} 응답 {r.status_code}', flush=True)
            return None
        return r.text
    except Exception as e:
        print(f'[collect_understat_shots] {url} 요청 실패: {e}', flush=True)
        return None


def fetch_league_matches(session, understat_league):
    """리그 페이지에서 datesData(매치 목록: understat matchId, 팀명, 날짜, 종료여부)를 가져온다."""
    url = f'{UNDERSTAT_BASE}/league/{understat_league}/{SEASON}'
    html = _fetch(session, url)
    if not html:
        return []
    print(f'[collect_understat_shots] [diag] {understat_league} 응답 HTML {len(html)}자',
          flush=True)
    data = _extract_js_json(html, 'datesData')
    if not isinstance(data, list):
        return []
    return data


def fetch_match_shots(session, understat_match_id):
    """매치 페이지에서 shotsData({'h': [...], 'a': [...]})를 가져온다."""
    url = f'{UNDERSTAT_BASE}/match/{understat_match_id}'
    html = _fetch(session, url)
    if not html:
        return None
    data = _extract_js_json(html, 'shotsData')
    if not isinstance(data, dict):
        return None
    return data


# ============================================================ 매치 매칭
def _understat_date_str(entry):
    dt = entry.get('datetime') or ''
    return dt[:10] if dt else None


def find_understat_match(understat_matches, kr_home, kr_away, date_str, league_key):
    date_str = (date_str or '')[:10]
    for m in understat_matches:
        if not m.get('isResult'):
            continue  # 아직 안 끝난 경기는 슈팅데이터 없음
        if _understat_date_str(m) != date_str:
            continue
        h_title = (m.get('h') or {}).get('title', '')
        a_title = (m.get('a') or {}).get('title', '')
        h_info = TEAM_INDEX.get(_norm(h_title))
        a_info = TEAM_INDEX.get(_norm(a_title))
        if not h_info or not a_info:
            continue
        if h_info[0] != league_key or a_info[0] != league_key:
            continue
        if h_info[1] == kr_home and a_info[1] == kr_away:
            return m, False
        if h_info[1] == kr_away and a_info[1] == kr_home:
            return m, True  # swapped
    return None, False


# ============================================================ 선수 매칭 + 병합 (AF 스크립트와 동일 원칙)
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


def merge_shots_into_metrics(metrics_path, shots_by_player):
    """shots_by_player: {player_name: {'shots': n, 'sot': n}}"""
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
    for name, stats_new in shots_by_player.items():
        key = _resolve_metrics_key(name, players, last_name_index)
        if key is None:
            continue  # Understat 신규 선수는 새 키로 안 만듦(BSD가 아예 모르는 선수면 그냥 스킵 — AF와 달리 확신도가 낮은 크롤링 매칭이라 보수적으로)
        stats = players.setdefault(key, {})
        stats['shots'] = stats_new['shots']
        stats['sot'] = stats_new['sot']
        stats['_understat_enriched'] = True
        n_merged += 1

    if n_merged:
        data['players'] = players
        _atomic_write(metrics_path, data)
    return n_merged


def parse_shots_to_player_stats(shots_data):
    """{'h': [...], 'a': [...]} -> {player_name: {'shots': n, 'sot': n}}"""
    out = defaultdict(lambda: {'shots': 0, 'sot': 0})
    for side in ('h', 'a'):
        for shot in (shots_data.get(side) or []):
            name = shot.get('player')
            if not name:
                continue
            result = shot.get('result')
            out[name]['shots'] += 1
            if result in SOT_RESULTS:
                out[name]['sot'] += 1
    return dict(out)


# ============================================================ 대상 경기 선정 (AF 스크립트와 동일)
def _finished_matches():
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT match_id, home, away, date FROM matches "
            "WHERE status = 'FINISHED' AND home_goals IS NOT NULL AND away_goals IS NOT NULL"
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
        print('[collect_understat_shots] requests 라이브러리 없음 → 스킵', flush=True)
        return
    if not os.path.exists(DB_PATH):
        print('[collect_understat_shots] data/football.db 없음(db.py 미실행?) → 스킵', flush=True)
        return

    state = _load_json(STATE_PATH, {})
    done = state.setdefault('done_matches', {})
    session = requests.Session()

    rows = _finished_matches()
    print(f'[collect_understat_shots] DB 종료경기 {len(rows)}건 조회', flush=True)

    candidates = []
    n_league_matched = n_metrics_found = 0
    for row in rows:
        resolved = _resolve_match_teams(row)
        if not resolved:
            continue
        lk, kr_home, kr_away = resolved
        if lk not in UNDERSTAT_LEAGUES:
            continue  # 에레디비시/챔피언십/MLS/엘리테세리엔 — Understat 미지원, 대상에서 제외
        n_league_matched += 1
        mid = row['match_id']
        metrics_path = os.path.join(METRICS_DIR, f'{mid}_metrics.json')
        if not os.path.exists(metrics_path):
            continue
        n_metrics_found += 1
        if done.get(mid, {}).get('status') == 'ok':
            continue
        candidates.append((mid, lk, kr_home, kr_away, row['date'], metrics_path))
    print(f'[collect_understat_shots] Understat 지원리그+팀 매칭 {n_league_matched}건, '
          f'metrics 파일 존재 {n_metrics_found}건, 처리 대상(미완료) {len(candidates)}건', flush=True)

    # 리그 페이지는 리그당 1번만 조회(캐시 없이 매번 새로 — 시즌 통째로 받아오는
    # 가벼운 호출이라 매치 페이지처럼 딜레이/제한을 안 둔다)
    league_matches_cache = {}
    for lk, understat_league in UNDERSTAT_LEAGUES.items():
        matches = fetch_league_matches(session, understat_league)
        league_matches_cache[lk] = matches
        print(f'[collect_understat_shots] {understat_league} 리그페이지: '
              f'{len(matches)}경기 수신', flush=True)
        time.sleep(REQUEST_DELAY_SEC)

    n_ok = n_no_match = n_no_shots = 0
    n_merged_players = 0
    for mid, lk, kr_home, kr_away, date_str, metrics_path in candidates:
        if n_ok + n_no_match + n_no_shots >= MAX_NEW_MATCHES_PER_RUN:
            print(f'[collect_understat_shots] 실행당 상한({MAX_NEW_MATCHES_PER_RUN}건) 도달 '
                  f'→ 중단(다음 실행에서 이어감)', flush=True)
            break
        understat_matches = league_matches_cache.get(lk, [])
        m, swapped = find_understat_match(understat_matches, kr_home, kr_away, date_str, lk)
        if not m:
            done[mid] = {'status': 'no_match', 'at': datetime.now(timezone.utc).isoformat()}
            n_no_match += 1
            continue
        understat_id = m.get('id')
        shots_data = fetch_match_shots(session, understat_id)
        time.sleep(REQUEST_DELAY_SEC)
        if not shots_data:
            done[mid] = {'status': 'no_shots_data', 'at': datetime.now(timezone.utc).isoformat()}
            n_no_shots += 1
            continue
        shots_by_player = parse_shots_to_player_stats(shots_data)
        n_merged = merge_shots_into_metrics(metrics_path, shots_by_player)
        n_merged_players += n_merged
        done[mid] = {'status': 'ok', 'at': datetime.now(timezone.utc).isoformat(), 'players': n_merged}
        n_ok += 1

    _atomic_write(STATE_PATH, state)
    print(f'[collect_understat_shots] 완료: 신규병합 {n_ok}경기({n_merged_players}명), '
          f'매치못찾음 {n_no_match}건, 슈팅데이터없음 {n_no_shots}건', flush=True)


if __name__ == '__main__':
    main()
