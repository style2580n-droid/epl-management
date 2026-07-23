# -*- coding: utf-8 -*-
"""
MLS(미국)·노르웨이 엘리테세리엔 확장 검토용 실측 프로브 (2026-07-23 착수).

목적: 두 리그를 정식으로 추가하기 전에, BSD가 이 둘을 (a) 별도 리그
오브젝트로 갖고 있는지 (b) 팀 목록/시즌이 온전한지 (c) 지금(7/23) 실제로
시즌 진행 중인지(=득점왕/라인업 배관을 실시간 리허설할 수 있는지)를 아주
싸게 확인한다. 프로덕션 파일은 아무것도 안 건드림(순수 진단 — 라인업 때
rehearse_lineups_probe.py와 같은 철학).

근거: 오늘 파이프라인 로그에 이미 두 리그로 보이는 이벤트가 대량으로
찍혔다(app_export_multileague.py쪽 metrics 로그, 우리가 만든 리그가 아닌
잡음으로 흘러들어온 것) — LosAngelesFC_RealSaltLake_5150 등 MLS로 보이는
이벤트ID가 5133~5153대에 몰려 있고, RosenborgBK_KristiansundBK_207007 등
노르웨이로 보이는 이벤트ID가 207004~207013대에 몰려 있다. 이건 간접
정황이라 이 프로브로 직접 확인한다(추측 금지).

collect_fixtures_multileague.py의 LEAGUE_MATCHERS/_find_leagues 패턴을
그대로 재사용 — 새로 만드는 건 두 리그의 matcher 함수와 팀/일정 요약
출력뿐이다.

실행: BSD_API_KEY 필요. 없으면 조용히 스킵.
"""
import time
from datetime import datetime, timedelta, timezone

from api_clients import BSDClient

PAGE_LIMIT = 200
DATE_FROM = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
DATE_TO = (datetime.now(timezone.utc) + timedelta(days=60)).strftime('%Y-%m-%d')
LOG = '[rehearse_mls_norway]'

# 2026-07-23: 이름/국가만으로 판별. collect_fixtures_multileague.py의
# LEAGUE_MATCHERS와 같은 방식 — 나라+정확한 이름으로 다른 대회(2부리그,
# 컵대회, 여자리그 등)와 안 섞이게 한다.
CANDIDATE_MATCHERS = {
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


def _find_candidate_leagues(client):
    """CANDIDATE_MATCHERS에 해당하는 리그를 client.leagues() 전수 페이지네이션
    으로 찾는다. collect_fixtures_multileague._find_leagues와 동일 골격."""
    found = {}  # key -> (league_id, season_id, season_name, real_name, country)
    offset = 0
    while True:
        data = _unwrap(client.leagues(limit=PAGE_LIMIT, offset=offset))
        if not data:
            break
        results = data.get('results', [])
        for lg in results:
            name = (lg.get('name') or '').lower()
            country = (lg.get('country') or '').lower()
            for key, matcher in CANDIDATE_MATCHERS.items():
                if key in found:
                    continue
                if matcher(name, country):
                    season = lg.get('current_season') or {}
                    found[key] = {
                        'league_id': lg.get('id'),
                        'season_id': season.get('id'),
                        'season_name': season.get('name'),
                        'start_date': season.get('start_date'),
                        'end_date': season.get('end_date'),
                        'is_current': season.get('is_current'),
                        'real_name': lg.get('name'),
                        'country': lg.get('country'),
                    }
                    print(f'{LOG} ✅ {key} 발견: "{lg.get("name")}" '
                          f'(country={lg.get("country")}, id={lg.get("id")}, '
                          f'season_id={season.get("id")}, '
                          f'season="{season.get("name")}", '
                          f'{season.get("start_date")}~{season.get("end_date")}, '
                          f'is_current={season.get("is_current")})', flush=True)
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results or len(found) == len(CANDIDATE_MATCHERS):
            break
    missing = set(CANDIDATE_MATCHERS) - set(found)
    if missing:
        print(f'{LOG} ❌ 못 찾음: {sorted(missing)} (리그 검색 {offset}건 전수 '
              f'조회 후에도 매칭 실패 — BSD에 해당 리그 자체가 없거나 이름/국가'
              f'표기가 예상과 다름)', flush=True)
    return found


def _probe_teams(client, key, league_id, season_id):
    """팀 목록 실측 — 개수와 원문 표기 샘플만 본다(한글 매칭은 다음 단계).
    league_id/league 두 파라미터 후보를 다 시도(6개 리그 때도 이렇게 해서
    파라미터명이 리그마다 다를 수 있다는 걸 확인했었음 — 추측 안 함)."""
    for param_name in ('league_id', 'league'):
        params = {param_name: league_id}
        if season_id:
            params['season'] = season_id
        data = _unwrap(client.teams(limit=PAGE_LIMIT, offset=0, **params))
        time.sleep(0.2)
        if not data:
            continue
        results = data.get('results', [])
        if not results:
            continue
        names = [t.get('name') or t.get('short_name') for t in results]
        print(f'{LOG} {key} teams({param_name}={league_id}'
              f'{f", season={season_id}" if season_id else ""}) → '
              f'{len(results)}개 (전체 count={data.get("count")}) 샘플: '
              f'{names[:10]}', flush=True)
        return len(results)
    print(f'{LOG} ⚠️ {key} teams 응답 둘 다 빈 값 — league_id/league 파라미터 '
          f'둘 다 실패', flush=True)
    return 0


def _probe_events(client, key, league_id):
    """최근 30일~향후 60일 일정/결과 개수만 본다 — 시즌이 실제로 진행
    중인지(득점왕/라인업 리허설 가능 여부) 확인용."""
    for param_name in ('league_id', 'league'):
        data = _unwrap(client.events(**{
            param_name: league_id, 'date_from': DATE_FROM, 'date_to': DATE_TO,
            'limit': PAGE_LIMIT, 'offset': 0}))
        time.sleep(0.2)
        if not data:
            continue
        rows = data.get('results', [])
        if not rows:
            continue
        finished = sum(1 for r in rows if (r.get('status') or '').lower() == 'finished')
        pending = sum(1 for r in rows if (r.get('status') or '').lower() != 'finished')
        print(f'{LOG} {key} events({param_name}={league_id}, '
              f'{DATE_FROM}~{DATE_TO}) → 이번 페이지 {len(rows)}건 '
              f'(전체 count={data.get("count")}) — 종료 {finished}건, '
              f'예정/기타 {pending}건', flush=True)
        return finished, pending
    print(f'{LOG} ⚠️ {key} events 응답 둘 다 빈 값', flush=True)
    return 0, 0


def main():
    client = BSDClient()
    if not client.enabled:
        print(f'{LOG} BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    found = _find_candidate_leagues(client)
    print(f'{LOG} ===== 결과 요약 =====', flush=True)
    for key, info in found.items():
        n_teams = _probe_teams(client, key, info['league_id'], info['season_id'])
        finished, pending = _probe_events(client, key, info['league_id'])
        print(f'{LOG} {key} 종합: 리그 확인 ✅, 팀 {n_teams}개, '
              f'최근30일~향후60일 종료경기 {finished}건/예정 {pending}건 '
              f'→ {"시즌 진행중으로 보임(리허설 가능)" if finished or pending else "일정 없음(비시즌일 수 있음)"}',
              flush=True)
    if not found:
        print(f'{LOG} 둘 다 못 찾음 — 확장 보류 권장 (BSD에 리그 자체가 없을 '
              f'가능성)', flush=True)


if __name__ == '__main__':
    main()
