# -*- coding: utf-8 -*-
"""
MLS·엘리테세리엔 xG 데이터 존재 여부 확인용 프로브 (2026-07-24 착수).

목적: collect_xg_bsd.py와 완전히 동일한 엔드포인트/필드
(`events/?league=&season=&status=finished`, `home_xg_live`/`away_xg_live`)를
MLS(league_id=18)·엘리테세리엔(league_id=54)에도 그대로 써봐서 실제로 xG
값이 오는지 실측 확인한다. collect_xg_bsd.py 자체는 안 건드림(LEAGUES
딕셔너리가 6개 리그 전용이라 그대로 두고, 이 프로브만 완전히 별도 파일로
분리 — 라인업 때 rehearse_lineups_probe.py와 같은 철학).

⚠️ 중요: collect_xg_bsd.py는 팀명 매칭에 app_export_multileague.to_kr_league()
를 쓰는데, 그건 6개 리그 전용 LEAGUE_TEAM_MAPS만 안다. MLS/엘리테세리엔
팀명은 거기 없어서, 그 함수를 그대로 썼으면 xG가 실제로 있어도 전부
매칭 실패로 버려져 "있는지 없는지" 자체를 알 수 없다(원본 스크립트 코드
읽고 확인한 함정 — 추측 아님). 그래서 이 프로브는 대신
mls_norway_team_maps.py의 매핑(2026-07-24 확정 46/46 매칭)으로 검증한다.

TARGET_YEARS는 collect_xg_bsd.py와 동일하게 [2024, 2025] — 완전히 끝난
과거 시즌만 본다(현재 진행 중인 시즌 데이터는 안 씀, 원본과 같은 방침).
MLS·엘리테세리엔은 캘린더 연도제라 6개 리그(24-25/25-26 시즌)와 달리
2024/2025가 각각 완결된 한 시즌씩과 바로 대응된다.

실행: BSD_API_KEY 필요. 없으면 조용히 스킵. 프로덕션 파일 저장 안 함
(진단 로그만).
"""
import os
import re
import time
import unicodedata

import requests

from mls_norway_team_maps import MLS_NORWAY_TEAM_MAPS, LEAGUE_IDS

API_TOKEN = os.getenv('BSD_API_KEY', '')
BASE_URL = 'https://sports.bzzoiro.com/api'
TARGET_YEARS = [2024, 2025]  # collect_xg_bsd.py와 동일
LOG = '[rehearse_mls_norway_xg]'


def _headers():
    return {'Authorization': f'Token {API_TOKEN}'}


def _ascii_fold(s):
    s = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in s if not unicodedata.combining(c))


def _norm(name):
    """collect_mls_norway_teams.py/app_export_multileague._norm과 동일."""
    if not name:
        return ''
    n = _ascii_fold(name)
    n = re.sub(r'\b(FC|AFC|CF)\b', '', n, flags=re.I)
    return re.sub(r'[^a-z가-힣0-9]', '', n.lower())


def _build_lookup(league_key):
    lookup = {}
    for kr, aliases in MLS_NORWAY_TEAM_MAPS[league_key].items():
        for a in aliases + [kr]:
            lookup[_norm(a)] = kr
    return lookup


def get_season_ids(league_id):
    """collect_xg_bsd.py의 get_season_ids()와 동일 엔드포인트."""
    url = f'{BASE_URL}/v2/leagues/{league_id}/seasons/'
    try:
        r = requests.get(url, headers=_headers(), timeout=30)
        if r.status_code != 200:
            print(f'{LOG} league={league_id} seasons HTTP {r.status_code}', flush=True)
            return []
        seasons = r.json().get('seasons', [])
        years = sorted({s.get('year') for s in seasons if s.get('year') is not None})
        print(f'{LOG} league={league_id} seasons 응답 {len(seasons)}건, '
              f'존재하는 연도들: {years}', flush=True)
        return [s['id'] for s in seasons if s.get('year') in TARGET_YEARS]
    except Exception as e:
        print(f'{LOG} seasons 조회 실패 league={league_id}: {e}', flush=True)
        return []


def probe_league(league_key):
    ids = LEAGUE_IDS[league_key]
    lid = ids['league_id']
    lookup = _build_lookup(league_key)
    sids = get_season_ids(lid)
    if not sids:
        print(f'{LOG} {league_key}: TARGET_YEARS({TARGET_YEARS}) 시즌을 못 찾음 '
              f'→ xG 확인 불가(연도 표기 방식이 다를 수 있음, 위 로그의 '
              f'"존재하는 연도들" 참조)', flush=True)
        return

    total_events, with_xg = 0, 0
    matched_teams = set()
    for sid in sids:
        url = (f'{BASE_URL}/events/?league={lid}&season={sid}'
               f'&status=finished&limit=400')
        try:
            r = requests.get(url, headers=_headers(), timeout=30)
        except Exception as e:
            print(f'{LOG} {league_key} season={sid} 요청 실패: {e}', flush=True)
            continue
        time.sleep(0.2)
        if r.status_code != 200:
            print(f'{LOG} {league_key} season={sid} HTTP {r.status_code}', flush=True)
            continue
        events = r.json().get('results', [])
        total_events += len(events)
        n_this_season = 0
        for ev in events:
            h_xg, a_xg = ev.get('home_xg_live'), ev.get('away_xg_live')
            if h_xg is not None and a_xg is not None:
                with_xg += 1
                n_this_season += 1
                for raw in (ev.get('home_team'), ev.get('away_team')):
                    kr = lookup.get(_norm(raw))
                    if kr:
                        matched_teams.add(kr)
        print(f'{LOG} {league_key} season={sid}: 이벤트 {len(events)}건 중 '
              f'xG 있는 경기 {n_this_season}건', flush=True)

    n_expected = len(MLS_NORWAY_TEAM_MAPS[league_key])
    verdict = 'BSD가 xG 줌' if with_xg else 'xG 없음(전부 None)'
    print(f'{LOG} {league_key} 종합: 종료경기 {total_events}건 중 xG 있는 경기 '
          f'{with_xg}건, xG 나온 경기에서 팀명 매칭 {len(matched_teams)}/'
          f'{n_expected}개 → {verdict}', flush=True)


def main():
    if not API_TOKEN:
        print(f'{LOG} BSD_API_KEY 미등록 → 스킵', flush=True)
        return
    for league_key in MLS_NORWAY_TEAM_MAPS:
        probe_league(league_key)


if __name__ == '__main__':
    main()
