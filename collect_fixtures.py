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
    offset = 0
    while True:
        data = _unwrap(client.leagues(limit=PAGE_LIMIT, offset=offset))
        if not data:
            return None
        results = data.get('results', [])
        for lg in results:
            name = (lg.get('name') or '')
            country = (lg.get('country') or '')
            if 'premier league' in name.lower() and country.lower() == 'england':
                return lg.get('id')
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results:
            return None


def _find_pl_teams(client, league_id):
    """/api/v2/teams/ 필터 파라미터 후보를 시도해 실제로 우리 20개 구단과
    매칭되는 조합만 채택 (추측이 아니라 결과 검증)."""
    candidates = [
        {'league_id': league_id},
        {'league': league_id},
        {'competition': league_id},
    ]
    for params in candidates:
        data = _unwrap(client.teams(**params))
        if not data:
            continue
        results = data.get('results', [])
        matched = [(t['id'], to_kr(t.get('name') or t.get('short_name')))
                   for t in results if to_kr(t.get('name') or t.get('short_name'))]
        if len(matched) >= 10:
            return dict(matched)
    return {}


def _fetch_all_team_matches(client, team_id):
    """한 팀의 경기 목록을 offset/count로 끝까지 순회해 전부 모은다.
    BSD가 이 엔드포인트도 leagues/managers처럼 페이지 단위로 끊어 주면
    단발 호출은 뒷부분을 조용히 잘라버리므로(에러 없이 불완전 저장)
    _find_pl_league_id / _fetch_all_managers와 동일한 패턴으로 방어한다.
    응답에 페이지네이션 메타(count)가 없으면 첫 페이지가 전부라고 보고 종료."""
    all_rows = []
    offset = 0
    while True:
        data = _unwrap(client.team_matches(
            team_id, date_from=DATE_FROM, date_to=DATE_TO,
            limit=PAGE_LIMIT, offset=offset))
        if not data:
            break
        rows = data.get('events') if isinstance(data, dict) else None
        if rows is None and isinstance(data, dict):
            rows = data.get('results', [])
        if not rows:
            break
        all_rows.extend(rows)
        # count(전체 건수)가 있으면 그걸로 종료 판정, 없으면 한 페이지에 다
        # 담겼다고 보고(=페이지네이션 미지원) 루프 종료.
        total = data.get('count')
        offset += PAGE_LIMIT
        if total is None or offset >= total or len(rows) < PAGE_LIMIT:
            break
    return all_rows


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

    league_id = _find_pl_league_id(client)
    if not league_id:
        print('[collect_fixtures] Premier League 리그를 못 찾음 → 중단')
        return
    print(f'[collect_fixtures] league_id={league_id}')

    team_id_to_kr = _find_pl_teams(client, league_id)
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
        elif status in ('notstarted', 'scheduled', ''):
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
