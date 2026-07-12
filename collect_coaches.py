# -*- coding: utf-8 -*-
"""
BSD(Bzzoiro Sports Data)의 팀/감독 엔드포인트로 EPL 20개 구단의 '현재'
감독을 조회해 data/master/coaches.json에 저장한다.

API-Football은 무료 플랜이 2022~2024 시즌까지만 허용해 2026-27 시즌
데이터에 접근이 안 됐다(429/plan 오류로 확인됨). BSD는 시즌 제한이 없고
이미 BSD_API_KEY가 파이프라인에 등록되어 있어 이걸로 대체한다.

동작:
  1) GET /api/v2/teams/?league_id=17 로 EPL 20개 구단의 BSD team_id 확보
     (17 = BSD의 Premier League league_id)
  2) GET /api/v2/managers/ 를 페이지네이션하며 전체 감독 목록 확보
  3) 각 감독의 current_team_id가 1)의 팀 id와 일치하면 그 팀의 현재 감독으로 채택

실행: BSD_API_KEY 환경변수 필요. 없으면 조용히 스킵(예외 없음).
"""
import json
import os

from api_clients import BSDClient
from app_export import to_kr

OUT_PATH = 'data/master/coaches.json'
PL_LEAGUE_ID = 17          # BSD 리그 ID: Premier League
PAGE_LIMIT = 200           # BSD 페이지당 최대 개수


def _unwrap(resp):
    """BaseClient.get()이 (data, changed) 튜플이나 data 단독,
    또는 실패 시 None을 반환하는 모든 경우를 방어적으로 처리."""
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _fetch_all_managers(client):
    """/api/v2/managers/ 를 offset 페이지네이션하며 전부 수집."""
    all_results = []
    offset = 0
    while True:
        data = _unwrap(client.managers(limit=PAGE_LIMIT, offset=offset))
        if not data:
            print(f'[collect_coaches] managers 조회 실패 (offset={offset})')
            break
        errors = data.get('errors')
        if errors:
            print(f'[collect_coaches] managers API 오류 → {errors}')
            break
        results = data.get('results', [])
        all_results.extend(results)
        total = data.get('count', len(all_results))
        offset += PAGE_LIMIT
        if offset >= total or not results:
            break
    print(f'[collect_coaches] managers 총 {len(all_results)}명 수집')
    return all_results


def main():
    client = BSDClient()
    if not client.enabled:
        print('[collect_coaches] BSD_API_KEY 미등록 → 스킵')
        return

    teams_data = _unwrap(client.teams(league_id=PL_LEAGUE_ID))
    if not teams_data:
        print('[collect_coaches] BSD 팀 목록 조회 실패 (응답 없음)')
        return
    errors = teams_data.get('errors')
    if errors:
        print(f'[collect_coaches] BSD 팀 목록 API 오류 → {errors}')
        return

    team_rows = teams_data.get('results', [])
    if not team_rows:
        print(f'[collect_coaches] BSD 팀 목록이 비어있음 '
              f'(league_id={PL_LEAGUE_ID} 확인 필요할 수 있음)')
        return
    print(f'[collect_coaches] BSD에서 팀 {len(team_rows)}개 조회 성공')

    # BSD team_id -> 앱 표준 한글 팀명
    team_id_to_kr = {}
    for t in team_rows:
        kr = to_kr(t.get('name') or t.get('short_name'))
        if kr and t.get('id'):
            team_id_to_kr[t['id']] = kr
    print(f'[collect_coaches] 한글 팀명 매칭: {len(team_id_to_kr)}/{len(team_rows)}')

    managers = _fetch_all_managers(client)

    coaches = {}
    for m in managers:
        team_id = m.get('current_team_id')
        kr = team_id_to_kr.get(team_id)
        if kr and m.get('name'):
            coaches[kr] = m['name']

    for kr in team_id_to_kr.values():
        if kr not in coaches:
            print(f'[collect_coaches] {kr}: 매칭되는 감독 없음')
        else:
            print(f'[collect_coaches] {kr}: {coaches[kr]}')

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(coaches, f, ensure_ascii=False, indent=1)
    print(f'[collect_coaches] 완료: {len(coaches)}개 팀 저장')


if __name__ == '__main__':
    main()
