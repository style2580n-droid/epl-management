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
_MAX_PAGES = 5  # 안전 상한: 팀당 최대 1000건(200*5). 필터가 안 먹혀 전체
                 # 리그 데이터가 오는 경우 무한정 페이지 넘기는 것을 방지.
                 # ⚠️ 24분 넘게 걸린 원인 진단을 위해 상한을 크게 낮춤 —
                 # 실제로 팀당 경기가 1000건을 넘길 일은 없으므로 안전.


def _fetch_all_team_matches(client, team_id):
    """한 팀의 경기 목록을 offset/count로 끝까지 순회해 전부 모은다.
    ⚠️ 2026-07-12 실측 확인: BSD 공식 엔드포인트 목록(llms.txt, MCP
    server-card)에 '/teams/{id}/matches/'는 아예 없음(20개 팀 전부
    HTTP 404). 실제로는 /events/를 팀 파라미터로 필터링하는 구조다.
    문서에 그 파라미터명이 명시돼 있지 않으므로, collect_coaches의
    teams 필터 탐색과 동일하게 후보(team/team_id)를 실제로 호출해
    이 팀의 경기가 맞게 나오는지(home/away에 team_id 포함) 검증한다.
    ⚠️ 검증은 반드시 '첫 페이지만 받은 시점'에 한다 — 필터가 안 먹히면
    3년+400일치 전체 리그 경기가 오는데, 그걸 끝까지 다 모은 뒤에야
    검증하면 페이지네이션이 사실상 멈추지 않는다. 한 번 확정되면
    이후 팀들은 그 파라미터를 재사용하고 검증도 건너뛴다."""
    global _TEAM_PARAM_NAME
    candidates = [_TEAM_PARAM_NAME] if _TEAM_PARAM_NAME else ['team', 'team_id']

    for param_name in candidates:
        print(f'[collect_fixtures]   team_id={team_id}: "{param_name}" 시도 중...',
              flush=True)
        first = _unwrap(client.events(**{
            param_name: team_id, 'date_from': DATE_FROM, 'date_to': DATE_TO,
            'limit': PAGE_LIMIT, 'offset': 0}))
        if not first:
            print(f'[collect_fixtures]   team_id={team_id}: "{param_name}" 응답 없음',
                  flush=True)
            continue
        first_rows = first.get('results', [])
        print(f'[collect_fixtures]   team_id={team_id}: "{param_name}" '
              f'1페이지 {len(first_rows)}건, count={first.get("count")}', flush=True)
        if not first_rows:
            continue
        if not any(ev.get('home_team_id') == team_id or ev.get('away_team_id') == team_id
                   for ev in first_rows):
            print(f'[collect_fixtures]   team_id={team_id}: "{param_name}" '
                  f'검증 실패(이 팀 경기 아님) → 다음 후보', flush=True)
            continue

        if _TEAM_PARAM_NAME is None:
            _TEAM_PARAM_NAME = param_name
            print(f'[collect_fixtures] events 팀 필터 파라미터 확정: "{param_name}"',
                  flush=True)

        all_rows = list(first_rows)
        total = first.get('count')
        offset = PAGE_LIMIT
        pages = 1
        while (total is None or offset < total) and len(first_rows) >= PAGE_LIMIT \
                and pages < _MAX_PAGES:
            data = _unwrap(client.events(**{
                param_name: team_id, 'date_from': DATE_FROM, 'date_to': DATE_TO,
                'limit': PAGE_LIMIT, 'offset': offset}))
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
        if pages >= _MAX_PAGES:
            print(f'[collect_fixtures] 경고: team_id={team_id} 페이지 상한'
                  f'({_MAX_PAGES}) 도달 — 데이터 일부만 수집됐을 수 있음', flush=True)
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
        print('[collect_fixtures] BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    league_id, season_id = _find_pl_league_id(client)
    if not league_id:
        print('[collect_fixtures] Premier League 리그를 못 찾음 → 중단', flush=True)
        return
    print(f'[collect_fixtures] league_id={league_id}, season_id={season_id}', flush=True)

    team_id_to_kr = _find_pl_teams(client, league_id, season_id)
    if not team_id_to_kr:
        print('[collect_fixtures] EPL 팀 목록을 못 찾음 → 중단', flush=True)
        return
    print(f'[collect_fixtures] 팀 {len(team_id_to_kr)}개 매칭', flush=True)

    events_by_id = {}
    for i, team_id in enumerate(team_id_to_kr, 1):
        print(f'[collect_fixtures] ({i}/{len(team_id_to_kr)}) '
              f'{team_id_to_kr[team_id]} 조회 중...', flush=True)
        rows = _fetch_all_team_matches(client, team_id)
        for ev in rows:
            eid = ev.get('id')
            if eid is not None:
                events_by_id[eid] = ev

    print(f'[collect_fixtures] 고유 경기 {len(events_by_id)}건 수집', flush=True)

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
          f'상대전적 매치업 {len(h2h_raw)}쌍', flush=True)

    os.makedirs(os.path.dirname(SCHEDULE_OUT), exist_ok=True)
    with open(SCHEDULE_OUT, 'w', encoding='utf-8') as f:
        json.dump(schedule, f, ensure_ascii=False, indent=1)
    with open(H2H_OUT, 'w', encoding='utf-8') as f:
        json.dump(h2h_raw, f, ensure_ascii=False, indent=1)
    print('[collect_fixtures] 완료', flush=True)


if __name__ == '__main__':
    main()
