# -*- coding: utf-8 -*-
"""
BSD(Bzzoiro Sports Data)의 팀별 경기 목록(/teams/{id}/matches/)으로
  1) EPL 20개 구단의 향후 경기 일정을 한국시간(KST, UTC+9)으로 변환해
     data/master/schedule.json 에 저장
  2) 두 팀이 맞붙은 과거 경기(완료된 경기)를 모아 상대전적(H2H)을
     data/master/h2h.json 에 저장

리그/팀 ID를 하드코딩하지 않는다 — collect_coaches.py와 동일하게
실제 API 응답을 검증해서 확정한다 (숫자 추측 금지가 이번 프로젝트의 교훈).

실행: BSD_API_KEY 환경변수 필요. 없으면 조용히 스킵(예외 없음).
"""
import json
import os
from datetime import datetime, timedelta, timezone

from api_clients import BSDClient
from app_export import to_kr

SCHEDULE_OUT = 'data/master/schedule.json'
H2H_OUT = 'data/master/h2h.json'
PAGE_LIMIT = 200
KST = timezone(timedelta(hours=9))

# 과거 3년 ~ 향후 400일 범위로 조회 (과거전적 + 다음 시즌 일정까지 커버)
DATE_FROM = (datetime.now(timezone.utc) - timedelta(days=365 * 3)).strftime('%Y-%m-%d')
DATE_TO = (datetime.now(timezone.utc) + timedelta(days=400)).strftime('%Y-%m-%d')


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _find_pl_league_id(client):
    """리그 목록에서 country=England / name에 'Premier League'가 들어간
    항목을 직접 찾아 (league_id, current_season_id) 튜플로 반환한다.
    collect_coaches._find_pl_league_id와 동일한 방식."""
    offset = 0
    while True:
        data = _unwrap(client.leagues(limit=PAGE_LIMIT, offset=offset))
        if not data:
            return None, None
        results = data.get('results', [])
        for lg in results:
            name = (lg.get('name') or '')
            country = (lg.get('country') or '')
            if 'premier league' in name.lower() and country.lower() == 'england':
                season = lg.get('current_season') or {}
                return lg.get('id'), season.get('id')
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results:
            return None, None


def _find_pl_teams(client, league_id, season_id=None):
    """/api/v2/teams/ 필터 파라미터 후보를 시도해 실제로 우리 20개 구단과
    매칭되는 조합만 채택 (추측이 아니라 결과 검증).
    collect_coaches._find_pl_teams와 동일한 후보 목록을 사용해, 감독이
    찾아지는 필터 조합이면 일정도 반드시 찾아지도록 일관성을 맞춘다."""
    candidates = [
        {'league': league_id},
        {'league_id': league_id},
        {'competition': league_id},
        {'league': league_id, 'season': season_id} if season_id else None,
        {'country': 'England'},
    ]
    for params in candidates:
        if not params:
            continue
        data = _unwrap(client.teams(**params))
        if not data:
            continue
        results = data.get('results', [])
        matched = [(t['id'], to_kr(t.get('name') or t.get('short_name')))
                   for t in results if to_kr(t.get('name') or t.get('short_name'))]
        if len(matched) >= 10:
            return dict(matched)
    return {}


_TEAM_PARAM_NAME = None  # 'team' 또는 'team_id' — 첫 성공 시 확정해 재사용(추측 금지)


def _fetch_all_team_matches(client, team_id):
    """한 팀의 경기 목록을 offset/count로 끝까지 순회해 전부 모은다.
    ⚠️ 2026-07-12 실측 확인: BSD 공식 엔드포인트 목록(llms.txt, MCP
    server-card)에 '/teams/{id}/matches/'는 아예 없음(20개 팀 전부
    HTTP 404). 실제로는 /events/를 팀 파라미터로 필터링하는 구조다.
    문서에 그 파라미터명이 명시돼 있지 않으므로, collect_coaches의
    teams 필터 탐색과 동일하게 후보(team/team_id)를 실제로 호출해
    이 팀의 경기가 맞게 나오는지(home/away에 team_id 포함) 검증한
    뒤 확정한다. 한 번 확정되면 이후 팀들은 그 파라미터를 재사용."""
    global _TEAM_PARAM_NAME
    candidates = [_TEAM_PARAM_NAME] if _TEAM_PARAM_NAME else ['team', 'team_id']

    for param_name in candidates:
        all_rows = []
        offset = 0
        while True:
            params = {param_name: team_id, 'date_from': DATE_FROM,
                       'date_to': DATE_TO, 'limit': PAGE_LIMIT, 'offset': offset}
            data = _unwrap(client.events(**params))
            if not data:
                all_rows = []
                break
            rows = data.get('results', [])
            if not rows:
                break
            all_rows.extend(rows)
            total = data.get('count')
            offset += PAGE_LIMIT
            if total is None or offset >= total or len(rows) < PAGE_LIMIT:
                break
        # 검증: 실제로 이 팀이 home/away 어느 쪽으로든 들어있는 경기가
        # 맞는지 확인 — 필터가 안 먹혀서 전체 리그 경기가 다 온 경우를 방지.
        if all_rows and any(
                ev.get('home_team_id') == team_id or ev.get('away_team_id') == team_id
                for ev in all_rows):
            if _TEAM_PARAM_NAME is None:
                _TEAM_PARAM_NAME = param_name
                print(f'[collect_fixtures] events 팀 필터 파라미터 확정: "{param_name}"')
            return all_rows
    return []


def _kst_date_time(iso_str):
    """ISO8601(UTC) 문자열을 KST 'YYYY-MM-DD', 'HH:MM'으로 변환."""
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


def main():
    client = BSDClient()
    if not client.enabled:
        print('[collect_fixtures] BSD_API_KEY 미등록 → 스킵')
        return

    league_id, season_id = _find_pl_league_id(client)
    if not league_id:
        print('[collect_fixtures] Premier League 리그를 못 찾음 → 중단')
        return
    print(f'[collect_fixtures] league_id={league_id}, season_id={season_id}')

    team_id_to_kr = _find_pl_teams(client, league_id, season_id)
    if not team_id_to_kr:
        print('[collect_fixtures] EPL 팀 목록을 못 찾음 → 중단')
        return
    print(f'[collect_fixtures] 팀 {len(team_id_to_kr)}개 매칭')

    events_by_id = {}
    for team_id in team_id_to_kr:
        rows = _fetch_all_team_matches(client, team_id)
        for ev in rows:
            eid = ev.get('id')
            if eid is not None:
                events_by_id[eid] = ev

    print(f'[collect_fixtures] 고유 경기 {len(events_by_id)}건 수집')

    schedule = []
    h2h_raw = {}  # "팀A|||팀B"(정렬됨) -> [record, ...]

    for ev in events_by_id.values():
        home_kr = team_id_to_kr.get(ev.get('home_team_id')) or to_kr(ev.get('home_team'))
        away_kr = team_id_to_kr.get(ev.get('away_team_id')) or to_kr(ev.get('away_team'))
        if not (home_kr and away_kr):
            continue
        status = (ev.get('status') or '').lower()
        date_kst, time_kst = _kst_date_time(ev.get('event_date'))
        if not date_kst:
            continue

        if status == 'finished':
            hs, as_ = ev.get('home_score'), ev.get('away_score')
            if hs is None or as_ is None:
                continue
            key = '|||'.join(sorted([home_kr, away_kr]))
            h2h_raw.setdefault(key, []).append({
                'date': date_kst, 'home': home_kr, 'away': away_kr,
                'homeGoals': hs, 'awayGoals': as_,
            })
        elif status in ('upcoming', 'live'):
            # BSD 공식 문서(status 값: upcoming/live/finished/cancelled/
            # postponed) 실측 확인 결과, 기존 코드가 찾던 'notstarted'/
            # 'scheduled'는 애초에 존재하지 않는 값이었다(2026-07-12 확인).
            schedule.append({
                'date': date_kst, 'time': time_kst or '00:00',
                'home': home_kr, 'away': away_kr,
            })

    schedule.sort(key=lambda m: (m['date'], m['time']))
    for key in h2h_raw:
        h2h_raw[key].sort(key=lambda m: m['date'], reverse=True)
        h2h_raw[key] = h2h_raw[key][:10]  # 최근 10경기만 보관

    print(f'[collect_fixtures] 향후 일정 {len(schedule)}건, '
          f'상대전적 매치업 {len(h2h_raw)}쌍')

    os.makedirs(os.path.dirname(SCHEDULE_OUT), exist_ok=True)
    with open(SCHEDULE_OUT, 'w', encoding='utf-8') as f:
        json.dump(schedule, f, ensure_ascii=False, indent=1)
    with open(H2H_OUT, 'w', encoding='utf-8') as f:
        json.dump(h2h_raw, f, ensure_ascii=False, indent=1)
    print('[collect_fixtures] 완료')


if __name__ == '__main__':
    main()
