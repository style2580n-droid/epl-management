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
    항목을 직접 찾는다. ID를 추측하지 않고 실데이터로 확정.
    (league_id, current_season_id) 튜플을 반환."""
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
                print(f'[collect_coaches] 리그 발견: {lg}')
                season = lg.get('current_season') or {}
                return lg.get('id'), season.get('id')
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results:
            return None, None


def _find_pl_teams(client, league_id, season_id=None):
    """/api/v2/teams/의 정확한 필터 파라미터 이름을 문서만으로 확신할 수
    없으므로, 그럴듯한 후보를 전부 시도한 뒤 '결과의 절반 이상이
    to_kr()로 우리 20개 구단에 실제 매칭되는' 조합만 채택한다.
    추측이 아니라 결과를 직접 검증해서 고르는 방식."""
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
        if not results:
            continue
        matched = sum(1 for t in results if to_kr(t.get('name') or t.get('short_name')))
        print(f'[collect_coaches] teams 필터 시도 {params} → '
              f'{len(results)}개 중 {matched}개 매칭')
        if matched >= 10:
            return results, params
    return [], None


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

    league_id, season_id = _find_pl_league_id(client)
    if not league_id:
        print('[collect_coaches] "Premier League"/England 리그를 리그 목록에서 '
              '못 찾음 → 중단')
        return
    print(f'[collect_coaches] 실제 확인된 Premier League league_id = {league_id}, '
          f'season_id = {season_id}')

    rows, used_params = _find_pl_teams(client, league_id, season_id)
    if not rows:
        print('[collect_coaches] 어떤 필터 조합으로도 EPL 팀을 못 찾음 → 중단')
        return
    print(f'[collect_coaches] 채택된 필터: {used_params}, 팀 {len(rows)}개')

    team_id_to_kr = {}
    for r in rows:
        kr = to_kr(r.get('name') or r.get('short_name'))
        if kr and r.get('id'):
            team_id_to_kr[r['id']] = kr
    print(f'[collect_coaches] 한글 팀명 매칭: {len(team_id_to_kr)}/{len(rows)}')

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
