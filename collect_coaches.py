# -*- coding: utf-8 -*-
"""
BSD(Bzzoiro Sports Data)의 리그 순위표 + 감독 목록으로 EPL 20개 구단의
'현재' 감독을 조회해 data/master/coaches.json에 저장한다.

/api/v2/teams/ 는 필터 파라미터가 불명확해 리그 필터링이 안 먹혔음
(사우디 팀이 섞여 나옴, 실측 확인됨) → 대신 /api/v2/leagues/17/standings/
(리그 순위표)로 EPL 소속 20개 팀의 정확한 team_id를 확보한다.
17 = BSD의 Premier League league_id.

실행: BSD_API_KEY 환경변수 필요. 없으면 조용히 스킵(예외 없음).
"""
import json
import os

from api_clients import BSDClient
from app_export import to_kr

OUT_PATH = 'data/master/coaches.json'
PL_LEAGUE_ID = 17
PAGE_LIMIT = 200


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _fetch_all_managers(client):
    all_results = []
    offset = 0
    while True:
        data = _unwrap(client.managers(limit=PAGE_LIMIT, offset=offset))
        if not data:
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

    standings_data = _unwrap(client.standings(PL_LEAGUE_ID))
    if not standings_data:
        print('[collect_coaches] BSD 순위표 조회 실패 (응답 없음)')
        return
    if standings_data.get('errors'):
        print(f'[collect_coaches] 순위표 API 오류 → {standings_data["errors"]}')
        return

    rows = standings_data.get('standings', [])
    if rows and isinstance(rows, dict):
        # 컵대회처럼 grouped 응답일 경우를 대비한 방어 처리
        rows = [r for grp in rows.values() for r in grp]
    if not rows:
        print(f'[collect_coaches] 순위표가 비어있음: {json.dumps(standings_data, ensure_ascii=False)[:500]}')
        return

    print(f'[collect_coaches] BSD 순위표로 팀 {len(rows)}개 확보')

    team_id_to_kr = {}
    for r in rows:
        kr = to_kr(r.get('team_name'))
        if kr and r.get('team_id'):
            team_id_to_kr[r['team_id']] = kr
    print(f'[collect_coaches] 한글 팀명 매칭: {len(team_id_to_kr)}/{len(rows)}')
    if len(team_id_to_kr) == 0 and rows:
        print(f'[collect_coaches] 매칭 실패 원본 예시: {json.dumps(rows[:3], ensure_ascii=False)}')

    managers = _fetch_all_managers(client)

    coaches = {}
    for m in managers:
        kr = team_id_to_kr.get(m.get('current_team_id'))
        if kr and m.get('name'):
            coaches[kr] = m['name']

    for kr in team_id_to_kr.values():
        print(f'[collect_coaches] {kr}: {coaches.get(kr, "매칭되는 감독 없음")}')

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(coaches, f, ensure_ascii=False, indent=1)
    print(f'[collect_coaches] 완료: {len(coaches)}개 팀 저장')


if __name__ == '__main__':
    main()
