# -*- coding: utf-8 -*-
"""
BSD(Bzzoiro Sports Data)를 6개 리그(라리가/분데스리가/세리에A/리그앙/
에레디비시/챔피언십) 일정의 보조 소스로 쓴다.

배경: football-data.org(collectors.py)가 26/27 시즌 일정을 아직 다 못
채웠을 가능성이 있어(2026-07-13 기준 확인 불가), EPL에서 이미 실측 검증된
패턴(리그 검색→league_id 확정→그 리그 팀 ID 확보→events()로 일정 수집)을
그대로 6개 리그에 재사용한다. EPL의 collect_fixtures.py와 동일한 원칙:
숫자/파라미터 절대 추측하지 않고 실제 응답으로 검증한다.

출력: data/master/schedule_multileague.json
      { "laliga": [{home, away, date}, ...], "bundesliga": [...], ... }
      app_export_multileague.py가 이 파일이 있으면 football-data.org DB보다
      우선 사용하도록 다음 단계에서 연결한다.

실행: BSD_API_KEY 환경변수 필요. 없으면 조용히 스킵(예외 없음).
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from api_clients import BSDClient
from app_export_multileague import LEAGUE_TEAM_MAPS, to_kr_league, _ascii_fold

OUT_PATH = 'data/master/schedule_multileague.json'
SQUADS_OUT_PATH = 'data/master/squads_multileague.json'
PAGE_LIMIT = 200
_MAX_PAGES = 50
KST = timezone(timedelta(hours=9))
DATE_FROM = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
DATE_TO = (datetime.now(timezone.utc) + timedelta(days=400)).strftime('%Y-%m-%d')

# ============================================================ 리그 판별 기준
# EPL의 _find_pl_league_id(name+country 조합 실측 확인)와 동일한 방식.
# 세리에A는 브라질 세리에A와, 분데스리가는 2.Bundesliga와, 챔피언십은
# 다른 나라 대회명과 안 섞이도록 국가+정확한 이름으로 판별한다.
LEAGUE_MATCHERS = {
    'laliga': lambda n, c: c == 'spain' and n in (
        'la liga', 'laliga', 'primera division', 'primera división'),
    'bundesliga': lambda n, c: c == 'germany' and n == 'bundesliga',
    'seriea': lambda n, c: c == 'italy' and n == 'serie a',
    'ligue1': lambda n, c: c == 'france' and 'ligue 1' in n,
    'eredivisie': lambda n, c: c in ('netherlands', 'holland') and 'eredivisie' in n,
    'championship': lambda n, c: c == 'england' and n == 'championship',
    # 2026-07-24 추가: rehearse_mls_norway_probe.py로 실측 확정된 매처
    # (mls: id=18/season_id=158, eliteserien: id=54/season_id=1230).
    # 컵대회(NM Cupen 등)와 안 섞이게 나라+정확한 이름으로 제한.
    'mls': lambda n, c: c in ('usa', 'united states', 'united states of america') and (
        n == 'mls' or 'major league soccer' in n),
    'eliteserien': lambda n, c: c == 'norway' and (
        n == 'eliteserien' or 'eliteserien' in n),
}


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _kst_date_time(iso_str):
    """ISO8601(UTC) 문자열을 KST 'YYYY-MM-DD', 'HH:MM'으로 변환.
    EPL(collect_fixtures.py)에서 이미 검증된 함수를 그대로 재사용 —
    2026-07-15까지 6개 리그 쪽은 [:10]으로 시간을 버리고 날짜만 썼는데,
    킥오프 알림 기능에 시각이 필요해서 EPL과 동일하게 맞춘다."""
    if not iso_str:
        return None, None
    s = iso_str.replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    kst = dt.astimezone(KST)
    return kst.strftime('%Y-%m-%d'), kst.strftime('%H:%M')


# ============================================================ 1) 리그 찾기
def _find_leagues(client):
    found = {}  # league_key -> (league_id, season_id, 실제이름)
    offset = 0
    while True:
        data = _unwrap(client.leagues(limit=PAGE_LIMIT, offset=offset))
        if not data:
            break
        results = data.get('results', [])
        for lg in results:
            name = (lg.get('name') or '').lower()
            country = (lg.get('country') or '').lower()
            for league_key, matcher in LEAGUE_MATCHERS.items():
                if league_key in found:
                    continue
                if matcher(name, country):
                    season = lg.get('current_season') or {}
                    found[league_key] = (lg.get('id'), season.get('id'), lg.get('name'))
                    print(f'[collect_fixtures_multileague] {league_key} 발견: '
                          f'"{lg.get("name")}" (id={lg.get("id")}, '
                          f'season_id={season.get("id")})', flush=True)
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results or len(found) == len(LEAGUE_MATCHERS):
            break
    missing = set(LEAGUE_MATCHERS) - set(found)
    if missing:
        print(f'[collect_fixtures_multileague] 못 찾은 리그: {sorted(missing)}',
              flush=True)
    return found


# ============================================================ 2) 그 리그의 팀 ID 확보
# 2026-07-18: 라리가 22/20·챔피언십 22/24 매칭 갭 원인 분석용 진단 강화.
# 코드 분석으로 좁힌 원인 후보 (실행 로그로 확정해야 함 — 추측 금지 원칙):
#  (A) 중복 ID(라리가 22/20): matched가 BSD id 키라서 같은 클럽에 ID가
#      2개(옛 레코드 중복/여자팀/B팀 등)면 둘 다 세어진다. 게다가 기존
#      main()은 team_ids를 그대로 돌며 squads[한글팀명]에 덮어쓰기 때문에
#      뒤에 온 ID(예: B팀)의 스쿼드가 1군 스쿼드를 **조용히 덮어쓸 수 있는
#      실제 데이터 오염 경로**였다 → PRIMARY_TEAM_IDS로 해결.
#  (B) 조용한 잘림(챔피언십 22/24 후보 #1): 기존엔 client.teams()를 limit
#      없이 1회만 호출 — BSD 기본 페이지 크기에 잘리면 팀이 티 안 나게
#      누락된다 → 페이지네이션 추가.
#  (C) 별칭 부재(챔피언십 22/24 후보 #2): BSD가 "Sheffield Utd",
#      "Wolverhampton"(단독) 같은 표기를 쓰면 현재 별칭 목록으로는 매칭
#      실패한다(_norm은 FC/AFC/CF만 제거) → 미매칭 원문 팀명을 로그로
#      남겨서 다음 실행에서 바로 별칭을 추가할 수 있게 한다.
#  (D) 리그 필터 무시 가능성: /players/의 team= 이 무시됐던 전례처럼
#      /teams/의 league 필터도 무시된다면 전체 팀 DB에서 이름이 우연히
#      겹치는 타국 팀(예: 에콰도르 "Barcelona")이 섞일 수 있다 → 응답
#      총 개수를 로그로 남겨 기대치 대비 과도하면 경고.
PRIMARY_TEAM_IDS = {}  # league_key -> {한글팀명: 대표 team_id} (스쿼드/감독 조회용)
_B_TEAM_RE = None


def _looks_like_b_team(raw_name):
    """원문 팀명이 B팀/유스/여자팀으로 보이는지. 중복 ID가 있을 때만
    대표 ID 선정에 쓰인다(단독 매칭엔 적용 안 함 — 'Willem II'처럼 1군
    이름에 II가 든 팀을 오판하지 않기 위해)."""
    global _B_TEAM_RE
    if _B_TEAM_RE is None:
        import re as _re
        _B_TEAM_RE = _re.compile(
            r'\b(b|ii|iii|u\d{2}|youth|junior|castilla|atletic|femen\w*|'
            r'women|ladies|reserves?)\b', _re.I)
    return bool(_B_TEAM_RE.search(_ascii_fold(raw_name or '')))


def _fetch_all_teams(client, params):
    """teams()를 페이지네이션으로 전부 받는다. BSDClient.teams가
    limit/offset을 안 받는 시그니처면(실측 전 미확정) 기존처럼 1회
    호출로 폴백한다."""
    rows, offset = [], 0
    while True:
        try:
            data = _unwrap(client.teams(limit=PAGE_LIMIT, offset=offset, **params))
        except TypeError:
            # teams()가 limit/offset을 안 받는 시그니처 → 기존 방식 1회 호출
            data = _unwrap(client.teams(**params))
            return (data or {}).get('results', []) if data else rows
        time.sleep(0.2)
        if not data:
            break
        page = data.get('results', [])
        rows.extend(page)
        count = data.get('count')
        offset += PAGE_LIMIT
        if not page or len(page) < PAGE_LIMIT \
                or (count is not None and offset >= count) \
                or offset >= PAGE_LIMIT * 10:
            break
    return rows


def _find_league_teams(client, league_key, league_id, season_id):
    """collect_coaches._find_pl_teams와 동일한 후보 시도+검증 패턴.
    BSD team_id -> 한글팀명 매핑을 만든다 (LEAGUE_TEAM_MAPS로 검증).
    반환 형식 {id: 한글팀명}은 기존과 동일(중복 ID 전부 포함 — 일정 매칭과
    collect_goalscorers.py가 이 형태를 그대로 씀). 스쿼드/감독 조회용
    클럽당 대표 ID는 PRIMARY_TEAM_IDS[league_key]에 따로 담는다."""
    candidates = [
        {'league_id': league_id},
        {'league': league_id},
        {'league_id': league_id, 'season': season_id} if season_id else None,
        {'league': league_id, 'season': season_id} if season_id else None,
    ]
    team_map = LEAGUE_TEAM_MAPS[league_key]
    n_expected = len(team_map)
    for params in candidates:
        if not params:
            continue
        results = _fetch_all_teams(client, params) or []
        if not results:
            continue
        matched = {}       # id -> 한글팀명 (반환용, 중복 ID 유지)
        by_kr = {}         # 한글팀명 -> [(id, 원문명)] (중복 진단용)
        unmatched = []     # 어느 리그에도 매칭 안 된 원문명 (누락팀 후보)
        for t in results:
            raw = t.get('name') or t.get('short_name')
            hit = to_kr_league(raw)
            if hit and hit[0] == league_key:
                matched[t['id']] = hit[1]
                by_kr.setdefault(hit[1], []).append((t['id'], raw))
            elif hit is None and raw:
                unmatched.append(raw)
        print(f'[collect_fixtures_multileague]   {league_key} teams{params} → '
              f'응답 {len(results)}개 중 {len(matched)}개 ID 매칭'
              f'(클럽 {len(by_kr)}개, 기대 {n_expected}팀)', flush=True)
        if len(matched) < max(3, n_expected * 0.3):
            continue

        # --- 진단 (A): 한 클럽에 ID 여러 개 (라리가 22/20 원인 규명용) ---
        primary = {}
        for kr, lst in by_kr.items():
            chosen = lst[0]
            if len(lst) > 1:
                non_b = [x for x in lst if not _looks_like_b_team(x[1])]
                chosen = (non_b or lst)[0]
                dup_desc = ', '.join(f'{i}:"{n}"' for i, n in lst)
                print(f'[collect_fixtures_multileague] [diag] {league_key} '
                      f'"{kr}" 중복 {len(lst)}건 [{dup_desc}] → 대표 '
                      f'id={chosen[0]} "{chosen[1]}"', flush=True)
            primary[kr] = chosen[0]
        PRIMARY_TEAM_IDS[league_key] = primary

        # --- 진단 (B)(C): 기대 팀 중 매칭 실패 (챔피언십 22/24 원인 규명용) ---
        missing = [kr for kr in team_map if kr not in by_kr]
        if missing:
            print(f'[collect_fixtures_multileague] [diag] {league_key} '
                  f'미매칭 기대팀 {len(missing)}개: {missing}', flush=True)
            if len(results) <= n_expected * 3:
                # 2026-07-18: [:20] 잘림 탓에 알파벳 뒷순서(W 등) 표기를 못
                # 봤다(울버햄튼) → 리그 필터 응답은 최대 50개 수준이라 전부 출력
                print(f'[collect_fixtures_multileague] [diag] {league_key} '
                      f'응답에 있었지만 매칭 안 된 원문 팀명(누락팀의 실제 BSD '
                      f'표기 후보): {unmatched}', flush=True)
            else:
                # 진단 (D): 응답이 기대치보다 훨씬 많으면 리그 필터 무시 의심
                print(f'[collect_fixtures_multileague] [diag] {league_key} '
                      f'응답 {len(results)}개 ≫ 기대 {n_expected}팀 — 리그 '
                      f'필터가 무시됐을 가능성(과거 /players/ team= 무시 전례). '
                      f'미매칭 원문 로그는 노이즈라 생략', flush=True)
        return matched
    return {}


# ============================================================ 3) 리그 일정 수집
_LEAGUE_PARAM_NAME = None


def _fetch_league_events(client, league_id):
    global _LEAGUE_PARAM_NAME
    candidates = [_LEAGUE_PARAM_NAME] if _LEAGUE_PARAM_NAME else ['league_id', 'league']

    for param_name in candidates:
        first = _unwrap(client.events(**{
            param_name: league_id, 'date_from': DATE_FROM, 'date_to': DATE_TO,
            'limit': PAGE_LIMIT, 'offset': 0}))
        time.sleep(0.3)
        if not first:
            continue
        first_rows = first.get('results', [])
        total = first.get('count')
        if not first_rows:
            continue
        if _LEAGUE_PARAM_NAME is None:
            _LEAGUE_PARAM_NAME = param_name
            print(f'[collect_fixtures_multileague] events 필터 파라미터 확정: '
                  f'"{param_name}"', flush=True)

        all_rows = list(first_rows)
        offset = PAGE_LIMIT
        pages = 1
        while (total is None or offset < total) and len(first_rows) >= PAGE_LIMIT \
                and pages < _MAX_PAGES:
            data = _unwrap(client.events(**{
                param_name: league_id, 'date_from': DATE_FROM, 'date_to': DATE_TO,
                'limit': PAGE_LIMIT, 'offset': offset}))
            time.sleep(0.3)
            if not data:
                break
            rows = data.get('results', [])
            if not rows:
                break
            all_rows.extend(rows)
            offset += PAGE_LIMIT
            pages += 1
            if len(rows) < PAGE_LIMIT:
                break
        return all_rows
    return []


_players_diag_done = False
_PLAYERS_PARAM_NAME = None  # 'team'/'team_id'/'current_team_id' — 첫 성공 시 확정


def _fetch_team_players(client, team_id):
    """BSD /players/ 로 한 팀의 선수 명단을 받는다.
    ⚠️ 2026-07-13 실측 확인: 문서에 명시된 team= 파라미터가 실제로는
    무시됨(count=66053 전체 선수 DB가 그대로 옴, 요청한 team_id와 무관한
    선수가 응답). 대신 선수 객체 필드명이 'current_team_id'인 것을
    확인해서, 그걸 쿼리 파라미터 후보에도 추가해 검증한다.
    ⚠️ 2026-07-16 추가: position 필드도 함께 뽑는다. BSD 공식 문서가
    position을 필터 파라미터로 명시하고 있어 응답 객체에도 있을 가능성이
    높지만(api_clients.py BSDClient.players 참고), 실전 검증 전까지는
    단정하지 않는다 — 없으면 그냥 None으로 채워서 앱 쪽 포지션 버킷팅이
    안전하게 폴백(기타로 분류)하도록 한다."""
    global _players_diag_done, _PLAYERS_PARAM_NAME
    candidates = [_PLAYERS_PARAM_NAME] if _PLAYERS_PARAM_NAME else \
        ['current_team_id', 'team_id', 'team']

    for param_name in candidates:
        resp = client.players(**{param_name: team_id, 'limit': 100})
        data = _unwrap(resp)
        time.sleep(0.2)
        if not _players_diag_done:
            _players_diag_done = True
            sample = data.get('results', [{}])[0] if data and data.get('results') else {}
            print(f'[collect_fixtures_multileague] [diag] players('
                  f'{param_name}={team_id}) count={data.get("count") if data else None} '
                  f'sample_keys={sorted(sample.keys()) if sample else []}',
                  flush=True)
        if not data:
            continue
        rows = data.get('results', [])
        if not rows:
            continue
        # 검증: 응답의 current_team_id가 실제로 요청한 팀과 일치하는지.
        hits = sum(1 for p in rows if p.get('current_team_id') == team_id)
        if hits < len(rows) * 0.8:
            continue
        if _PLAYERS_PARAM_NAME is None:
            _PLAYERS_PARAM_NAME = param_name
            print(f'[collect_fixtures_multileague] players 팀 필터 파라미터 확정: '
                  f'"{param_name}"', flush=True)
        return [{'name': p.get('name'), 'position': p.get('position')}
                for p in rows if p.get('name')]
    return []


_managers_cache = None  # {current_team_id: manager_dict} — 한 번만 전체 페이지네이션으로 채움


def _fetch_all_managers(client):
    """BSDClient.managers()로 전체 감독 목록을 페이지네이션으로 받아
    current_team_id 기준으로 캐시한다.

    ⚠️ 2026-07-17 수정: 원래 여기서 client.coach(team_id)를 호출했는데,
    coach()는 api_clients.py의 BSDClient가 아니라 APIFootballClient에만
    있는 메서드였다(비활성화된 클라이언트라 항상 AttributeError로 실패,
    스쿼드 수집 자체는 방어 코드 덕에 안 죽었지만 감독 확보 0팀이었음
    — 2026-07-17 실행 로그에서 실측 확인). collect_coaches.py가 EPL에서
    이미 BSDClient.managers()로 "managers 총 2076명 수집"에 성공한 걸
    보고서야 진짜 메서드를 찾았다 — 앞으로는 실제 클래스 소속을 grep으로
    직접 확인하고 쓸 것(문서/기억으로 짐작하지 말 것).
    managers()는 팀 단위 필터 파라미터가 문서화돼 있지 않아서(players()와
    달리), team_id당 개별 호출 대신 전체를 한 번만 받아 메모리에서
    current_team_id로 매칭한다 — API 호출도 아끼고 필터 파라미터 실측
    문제도 같이 피해간다."""
    global _managers_cache
    if _managers_cache is not None:
        return _managers_cache

    all_managers = []
    limit, offset = 200, 0
    while True:
        resp = client.managers(limit=limit, offset=offset)
        data = _unwrap(resp)
        time.sleep(0.2)
        if not data:
            break
        rows = data.get('results', [])
        all_managers.extend(rows)
        count = data.get('count')
        offset += limit
        if not rows or offset > 3000 or (count is not None and offset >= count):
            break

    _managers_cache = {}
    for m in all_managers:
        tid = m.get('current_team_id')
        if tid is not None:
            _managers_cache[tid] = m
    print(f'[collect_fixtures_multileague] managers 전체 {len(all_managers)}명 수집, '
          f'current_team_id 있는 것 {len(_managers_cache)}명 (감독 조회용 캐시)',
          flush=True)
    if all_managers:
        sample = all_managers[0]
        print(f'[collect_fixtures_multileague] [diag] managers sample_keys='
              f'{sorted(sample.keys())}', flush=True)
    return _managers_cache


def _fetch_team_coach(client, team_id):
    managers_by_team = _fetch_all_managers(client)
    m = managers_by_team.get(team_id)
    if not m:
        return ''
    for key in ('name', 'coach_name', 'manager_name', 'full_name'):
        val = m.get(key)
        if val:
            return val
    return ''


# ⚠️ 2026-07-14 확정: BSD는 완료된 경기의 xG를 제공하지 않는다. 목록
# 응답(/events/)에도, 상세 조회(event_detail)에도 stats가 None으로 옴 —
# BSD 자체 피드백 페이지에도 "경기가 끝나면 라이브 스탯이 사라진다"는
# 사용자 제보가 있어 확정됨. xG는 이제 collect_xg_fbref.py(fbref 스크래핑)
# 에서 별도로 수집해 같은 data/master/xg_multileague.json에 저장한다.
# (한때 이 파일에 있던 _fetch_team_xg 함수는 그래서 삭제됨 — 항상 빈
# 결과만 반환하는 죽은 코드였음)


def main():
    client = BSDClient()
    if not client.enabled:
        print('[collect_fixtures_multileague] BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    leagues = _find_leagues(client)
    if not leagues:
        print('[collect_fixtures_multileague] 리그를 하나도 못 찾음 → 중단', flush=True)
        return

    out = {}
    squads_out = {}
    for league_key, (league_id, season_id, real_name) in leagues.items():
        team_ids = _find_league_teams(client, league_key, league_id, season_id)
        if not team_ids:
            print(f'[collect_fixtures_multileague] {league_key} 팀 매칭 실패 → 스킵',
                  flush=True)
            out[league_key] = []
            squads_out[league_key] = {}
            continue

        # 2026-07-18: 기존엔 team_ids(중복 ID 포함)를 그대로 돌아서, 한 클럽에
        # ID가 2개면(라리가 22/20) 스쿼드를 두 번 받고 뒤의 것(B팀일 수 있음)이
        # 1군을 덮어썼다 → 클럽당 대표 ID 1개(PRIMARY_TEAM_IDS)로만 조회.
        primary = PRIMARY_TEAM_IDS.get(league_key) or \
            {kr: tid for tid, kr in team_ids.items()}
        squads = {}
        n_with_position = n_with_coach = 0
        for kr, team_id in primary.items():
            players = _fetch_team_players(client, team_id)
            coach = _fetch_team_coach(client, team_id)
            if players:
                squads[kr] = {'coach': coach, 'players': players}
                if coach:
                    n_with_coach += 1
                if any(p.get('position') for p in players):
                    n_with_position += 1
        squads_out[league_key] = squads
        total_players = sum(len(v['players']) for v in squads.values())
        print(f'[collect_fixtures_multileague] {league_key} 스쿼드: '
              f'{len(squads)}팀/{total_players}명 '
              f'(포지션 확보 {n_with_position}팀, 감독 확보 {n_with_coach}팀)', flush=True)
        # 2026-07-19: 라리가 스쿼드가 19/20으로 나온 원인 규명용 — 선수 0명이라
        # 조용히 빠진 클럽과 그때 쓴 team_id를 로그로 남긴다. 대표 ID가 빈
        # 레코드였다면 중복 diag의 다른 ID로 바꿔볼 근거가 된다.
        empty = {kr: tid for kr, tid in primary.items() if kr not in squads}
        if empty:
            print(f'[collect_fixtures_multileague] [diag] {league_key} 선수 0명으로 '
                  f'스쿼드 누락된 클럽: {empty}', flush=True)

        rows = _fetch_league_events(client, league_id)
        schedule = []
        # 2026-07-18: 이벤트에는 등장하는데 team_ids에 없는 팀 진단.
        # 챔피언십 552건 중 46건(=한 팀의 풀시즌)이 매칭 실패 → 그 팀이
        # /teams 응답에 다른 표기로 있거나 아예 없거나 둘 중 하나인데,
        # 이 로그가 어느 쪽인지 + 이벤트가 팀명을 직접 들고 있는지까지
        # 한 번의 실행으로 확정해준다.
        unknown_ids = {}
        for ev in rows:
            home_kr = team_ids.get(ev.get('home_team_id'))
            away_kr = team_ids.get(ev.get('away_team_id'))
            if not (home_kr and away_kr):
                for side in ('home', 'away'):
                    tid = ev.get(f'{side}_team_id')
                    if tid is not None and tid not in team_ids:
                        info = unknown_ids.setdefault(tid, {'n': 0, 'name': None})
                        info['n'] += 1
                        if info['name'] is None:
                            # 이벤트 행이 팀명을 직접 들고 있으면 그걸 확보
                            # (스키마 미확정이라 흔한 키 후보만 시도)
                            for k in (f'{side}_team', f'{side}_team_name',
                                      f'{side}_name'):
                                nm = ev.get(k)
                                if isinstance(nm, dict):
                                    nm = nm.get('name') or nm.get('short_name')
                                if isinstance(nm, str) and nm:
                                    info['name'] = nm
                                    break
                continue
            status = (ev.get('status') or '').lower()
            if status == 'finished':
                continue
            date_kst, time_kst = _kst_date_time(ev.get('event_date'))
            if not date_kst:
                continue
            schedule.append({
                'home': home_kr, 'away': away_kr,
                'date': date_kst, 'time': time_kst or '00:00',
            })
        schedule.sort(key=lambda m: (m['date'] or '', m['time'] or ''))
        out[league_key] = schedule
        print(f'[collect_fixtures_multileague] {league_key}: 팀 {len(team_ids)}개, '
              f'경기 {len(rows)}건 중 일정 {len(schedule)}건', flush=True)
        if unknown_ids:
            desc = ', '.join(
                f'id={tid}({v["n"]}경기' + (f', 이벤트상 팀명 "{v["name"]}"' if v['name'] else ', 이벤트에 팀명 필드 없음') + ')'
                for tid, v in sorted(unknown_ids.items(), key=lambda x: -x[1]['n'])[:8])
            print(f'[collect_fixtures_multileague] [diag] {league_key} 이벤트에 '
                  f'있지만 팀 매칭에 없는 team_id {len(unknown_ids)}개: {desc}',
                  flush=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(SQUADS_OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(squads_out, f, ensure_ascii=False, indent=1)
    print('[collect_fixtures_multileague] 완료', flush=True)


if __name__ == '__main__':
    main()
