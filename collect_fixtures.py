# -*- coding: utf-8 -*-
"""
BSD(Bzzoiro Sports Data)의 /events/ (리그 단위, date_from~date_to 필터)로
  1) EPL 20개 구단의 향후 경기 일정을 한국시간(KST, UTC+9)으로 변환해
     data/master/schedule.json 에 저장
  2) 두 팀이 맞붙은 과거 경기(완료된 경기)를 모아 상대전적(H2H)을
     data/master/h2h.json 에 저장

리그/팀 ID를 하드코딩하지 않는다 — collect_coaches.py와 동일하게
실제 API 응답을 검증해서 확정한다 (숫자 추측 금지가 이번 프로젝트의 교훈).

⚠️ 2026-07-12 시행착오 기록 (다음에 또 겪지 않도록):
  1차: '/teams/{id}/matches/' 호출 → 20개 팀 전부 HTTP 404 (엔드포인트
       자체가 존재하지 않았음).
  2차: '/events/?team=...' 호출 → 응답은 오는데 팀을 바꿔도 count가
       34631로 항상 똑같음 (필터가 무시되고 전체 스포츠 데이터가 그대로
       옴). BSD 공식 문서(llms.txt)에도 애초에 team 필터는 없었음.
  최종: 문서에 명시된 'league' 필터로 리그 전체를 한 번에 받고, 우리가
        이미 아는 20개 EPL team_id로 파이썬에서 직접 걸러내는 방식으로
        확정.

실행: BSD_API_KEY 환경변수 필요. 없으면 조용히 스킵(예외 없음).
"""
import json
import os
import time
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


_LEAGUE_PARAM_NAME = None  # 'league' 또는 'league_id' — 첫 성공 시 확정
_MAX_PAGES = 100  # 안전 상한: 리그 단위 전체 페이지네이션이라 넉넉히 잡아도
                   # 200*100=2만 건이면 3년+400일치 EPL 경기는 충분히 커버.


def _fetch_all_league_events(client, league_id, team_ids):
    """리그 전체 경기를 date_from~date_to 범위로 한 번에 받아온다.
    ⚠️ 2026-07-12 실측으로 밝혀진 사실 두 가지:
    1) '/teams/{id}/matches/'는 애초에 존재하지 않는 엔드포인트였다
       (20개 팀 전부 HTTP 404).
    2) 그 다음 시도했던 /events/?team=... 도 실은 안 먹혔다 — 팀을
       바꿔가며 호출해도 count가 34631로 완전히 똑같이 나왔는데, 이는
       필터가 무시되고 전체 스포츠/전체 리그 데이터가 그대로 반환됐다는
       뜻이다(20번 다른 팀을 넣었는데 매번 똑같은 count가 나올 수는 없음).
    BSD 공식 문서(llms.txt: "GET /api/events/?date_from=&date_to=&league=
    &status=&tz=")에는 애초부터 team 필터가 없고 league 필터만 있다.
    그래서 팀별 반복 대신 리그 단위로 딱 한 번만 받고, 우리가 이미 알고
    있는 20개 EPL team_id로 파이썬 쪽에서 직접 걸러낸다 — 존재하지 않는
    파라미터를 추측하는 대신 문서에 명시된 파라미터만 쓰는 방식."""
    global _LEAGUE_PARAM_NAME
    candidates = [_LEAGUE_PARAM_NAME] if _LEAGUE_PARAM_NAME else ['league', 'league_id']

    for param_name in candidates:
        print(f'[collect_fixtures] events "{param_name}"={league_id} 시도 중...',
              flush=True)
        first = _unwrap(client.events(**{
            param_name: league_id, 'date_from': DATE_FROM, 'date_to': DATE_TO,
            'limit': PAGE_LIMIT, 'offset': 0}))
        time.sleep(0.3)
        if not first:
            print(f'[collect_fixtures] "{param_name}" 응답 없음', flush=True)
            continue
        first_rows = first.get('results', [])
        total = first.get('count')
        print(f'[collect_fixtures] "{param_name}" 1페이지 {len(first_rows)}건, '
              f'count={total}', flush=True)
        if not first_rows:
            continue
        # 검증: 리그 필터가 실제로 먹혔다면, 1페이지 안에 우리가 아는 20개
        # EPL team_id가 상당수(과반) 등장해야 한다. 필터 무시되고 전체
        # 스포츠 데이터가 왔다면 EPL 팀 비중이 훨씬 낮을 것.
        hits = sum(1 for ev in first_rows
                   if ev.get('home_team_id') in team_ids
                   or ev.get('away_team_id') in team_ids)
        print(f'[collect_fixtures] "{param_name}" 1페이지 중 EPL 팀 매칭 '
              f'{hits}/{len(first_rows)}건', flush=True)
        if hits < len(first_rows) * 0.3:
            print(f'[collect_fixtures] "{param_name}" 검증 실패(EPL 비중 낮음) '
                  f'→ 다음 후보', flush=True)
            continue

        if _LEAGUE_PARAM_NAME is None:
            _LEAGUE_PARAM_NAME = param_name
            print(f'[collect_fixtures] events 리그 필터 파라미터 확정: '
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
            if pages % 10 == 0:
                print(f'[collect_fixtures] ...{pages}페이지, 누적 '
                      f'{len(all_rows)}건', flush=True)
            if len(rows) < PAGE_LIMIT:
                break
        if pages >= _MAX_PAGES:
            print(f'[collect_fixtures] 경고: 페이지 상한({_MAX_PAGES}) 도달 '
                  f'— 데이터 일부만 수집됐을 수 있음', flush=True)
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
    rows = _fetch_all_league_events(client, league_id, set(team_id_to_kr.keys()))
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
        elif status.lower() != 'finished':
            # ⚠️ 2026-07-13: 원래 ('upcoming','live')만 허용했더니 EPL만
            # 향후 일정 0건이 나왔는데, 같은 날 만든 6개 리그용 스크립트
            # (collect_fixtures_multileague.py)는 "finished만 제외" 방식으로
            # 라리가 306건/분데스리가 306건 등 정상 수집에 성공했다. BSD가
            # 실제로 쓰는 상태값이 문서(upcoming/live)와 다를 수 있다는
            # 뜻이라, 검증된 더 느슨한 방식으로 통일한다.
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
