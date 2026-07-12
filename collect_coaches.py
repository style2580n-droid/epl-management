# -*- coding: utf-8 -*-
"""
BSD(Bzzoiro Sports Data)의 리그 순위표 + 감독 목록으로 EPL 20개 구단의
'현재' 감독을 조회해 data/master/coaches.json에 저장한다.

리그 ID를 하드코딩하지 않는다. /api/v2/teams/의 필터가 불명확했고
league_id=17을 순위표에 넘겼더니 사우디 프로리그가 나온 것도 실측으로
확인됐다 (숫자 추측은 신뢰할 수 없다는 게 반복 확인됨). 대신 매 실행마다
/api/v2/leagues/ 전체 목록에서 name에 'Premier League'가 들어가고
country가 'England'인 항목을 직접 찾아 그 id를 쓴다.

실행: BSD_API_KEY 환경변수 필요. 없으면 조용히 스킵(예외 없음).
"""
import json
import os

from api_clients import BSDClient
from app_export import to_kr

OUT_PATH = 'data/master/coaches.json'
PAGE_LIMIT = 200


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _find_pl_league_id(client):
    """리그 목록에서 country=England / name에 'Premier League'가 들어간
    항목을 직접 찾는다. ID를 추측하지 않고 실데이터로 확정."""
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
                print(f'[collect_coaches] 리그 발견: {lg}')
                return lg.get('id')
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results:
            return None


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

    league_id = _find_pl_league_id(client)
    if not league_id:
        print('[collect_coaches] "Premier League"/England 리그를 리그 목록에서 '
              '못 찾음 → 중단')
        return
    print(f'[collect_coaches] 실제 확인된 Premier League league_id = {league_id}')

    standings_data = _unwrap(client.standings(league_id))
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
