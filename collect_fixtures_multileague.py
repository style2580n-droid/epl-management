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
from app_export_multileague import LEAGUE_TEAM_MAPS, to_kr_league

OUT_PATH = 'data/master/schedule_multileague.json'
SQUADS_OUT_PATH = 'data/master/squads_multileague.json'
XG_OUT_PATH = 'data/master/xg_multileague.json'
PAGE_LIMIT = 200
_MAX_PAGES = 50
_XG_MAX_PAGES = 20  # 리그당 시즌 하나에 최대 4000경기 — 실제로는 400경기 안팎이라 넉넉함
DATE_FROM = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
DATE_TO = (datetime.now(timezone.utc) + timedelta(days=400)).strftime('%Y-%m-%d')
# 24-25, 25-26 시즌 대략적 날짜 범위 (유럽 시즌은 8월 개막~5/6월 종료 기준)
XG_SEASON_RANGES = [
    ('2024-08-01', '2025-06-30'),  # 24-25 시즌
    ('2025-08-01', '2026-06-30'),  # 25-26 시즌
]

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
}


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


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
def _find_league_teams(client, league_key, league_id, season_id):
    """collect_coaches._find_pl_teams와 동일한 후보 시도+검증 패턴.
    BSD team_id -> 한글팀명 매핑을 만든다 (LEAGUE_TEAM_MAPS로 검증)."""
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
        data = _unwrap(client.teams(**params))
        if not data:
            continue
        results = data.get('results', [])
        matched = {}
        for t in results:
            hit = to_kr_league(t.get('name') or t.get('short_name'))
            if hit and hit[0] == league_key:
                matched[t['id']] = hit[1]
        print(f'[collect_fixtures_multileague]   {league_key} teams{params} → '
              f'{len(results)}개 중 {len(matched)}개 매칭(기대 {n_expected}팀)',
              flush=True)
        if len(matched) >= max(3, n_expected * 0.3):
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
    확인해서, 그걸 쿼리 파라미터 후보에도 추가해 검증한다."""
    global _players_diag_done, _PLAYERS_PARAM_NAME
    candidates = [_PLAYERS_PARAM_NAME] if _PLAYERS_PARAM_NAME else \
        ['current_team_id', 'team_id', 'team']

    for param_name in candidates:
        resp = client.players(**{param_name: team_id, 'limit': 100})
        data = _unwrap(resp)
        time.sleep(0.2)
        if not _players_diag_done:
            _players_diag_done = True
            print(f'[collect_fixtures_multileague] [diag] players('
                  f'{param_name}={team_id}) count={data.get("count") if data else None}',
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
        return [p.get('name') for p in rows if p.get('name')]
    return []


_xg_diag_done = False


def _fetch_team_xg(client, league_id, team_ids):
    """24-25, 25-26 시즌 완료 경기에서 팀별 평균 xG(득점 기대값)/xGA(실점
    기대값)를 계산한다. BSD 문서(docs/v2)에 "목록 응답도 상세 엔드포인트와
    같은 구조라 stats.home.xg.actual이 바로 들어있다"고 나와 있는데, 오늘
    하루 문서와 실제가 다른 경우가 많았으므로 첫 응답에서 그 필드가 실제로
    있는지 진단 로그를 남긴다. 있으면 경기 상세를 따로 부를 필요 없이
    한 번에 처리되고, 없으면 이 함수는 빈 결과를 반환한다(추가 조사 필요)."""
    global _xg_diag_done
    xg_for = {}
    xg_against = {}

    for date_from, date_to in XG_SEASON_RANGES:
        offset = 0
        pages = 0
        while pages < _XG_MAX_PAGES:
            data = _unwrap(client.events(
                league_id=league_id, date_from=date_from, date_to=date_to,
                status='finished', limit=PAGE_LIMIT, offset=offset))
            time.sleep(0.2)
            if not data:
                break
            rows = data.get('results', [])
            if not rows:
                break
            if not _xg_diag_done:
                _xg_diag_done = True
                sample = rows[0]
                print(f'[collect_fixtures_multileague] [diag-xg] 샘플 경기 '
                      f'키목록={sorted(sample.keys())}', flush=True)
                print(f'[collect_fixtures_multileague] [diag-xg] '
                      f'stats={sample.get("stats")}', flush=True)
            for ev in rows:
                hid, aid = ev.get('home_team_id'), ev.get('away_team_id')
                stats = ev.get('stats') or {}
                hxg = ((stats.get('home') or {}).get('xg') or {}).get('actual')
                axg = ((stats.get('away') or {}).get('xg') or {}).get('actual')
                if hxg is None or axg is None:
                    continue
                if hid in team_ids:
                    xg_for.setdefault(hid, []).append(hxg)
                    xg_against.setdefault(hid, []).append(axg)
                if aid in team_ids:
                    xg_for.setdefault(aid, []).append(axg)
                    xg_against.setdefault(aid, []).append(hxg)
            total = data.get('count')
            offset += PAGE_LIMIT
            pages += 1
            if total is None or offset >= total or len(rows) < PAGE_LIMIT:
                break

    result = {}
    for tid, kr in team_ids.items():
        fs = xg_for.get(tid)
        ags = xg_against.get(tid)
        if fs and ags:
            result[kr] = {
                'xG': round(sum(fs) / len(fs), 2),
                'xGA': round(sum(ags) / len(ags), 2),
                'matches': len(fs),
            }
    return result


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
    xg_out = {}
    for league_key, (league_id, season_id, real_name) in leagues.items():
        team_ids = _find_league_teams(client, league_key, league_id, season_id)
        if not team_ids:
            print(f'[collect_fixtures_multileague] {league_key} 팀 매칭 실패 → 스킵',
                  flush=True)
            out[league_key] = []
            squads_out[league_key] = {}
            xg_out[league_key] = {}
            continue

        squads = {}
        for team_id, kr in team_ids.items():
            players = _fetch_team_players(client, team_id)
            if players:
                squads[kr] = players
        squads_out[league_key] = squads
        total_players = sum(len(v) for v in squads.values())
        print(f'[collect_fixtures_multileague] {league_key} 스쿼드: '
              f'{len(squads)}팀/{total_players}명', flush=True)

        xg = _fetch_team_xg(client, league_id, team_ids)
        xg_out[league_key] = xg
        print(f'[collect_fixtures_multileague] {league_key} xG: {len(xg)}팀',
              flush=True)

        rows = _fetch_league_events(client, league_id)
        schedule = []
        for ev in rows:
            home_kr = team_ids.get(ev.get('home_team_id'))
            away_kr = team_ids.get(ev.get('away_team_id'))
            if not (home_kr and away_kr):
                continue
            status = (ev.get('status') or '').lower()
            if status == 'finished':
                continue
            schedule.append({
                'home': home_kr, 'away': away_kr,
                'date': (ev.get('event_date') or '')[:10] or None,
            })
        schedule.sort(key=lambda m: m['date'] or '')
        out[league_key] = schedule
        print(f'[collect_fixtures_multileague] {league_key}: 팀 {len(team_ids)}개, '
              f'경기 {len(rows)}건 중 일정 {len(schedule)}건', flush=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(SQUADS_OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(squads_out, f, ensure_ascii=False, indent=1)
    with open(XG_OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(xg_out, f, ensure_ascii=False, indent=1)
    print('[collect_fixtures_multileague] 완료', flush=True)


if __name__ == '__main__':
    main()
