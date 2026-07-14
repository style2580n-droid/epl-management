# -*- coding: utf-8 -*-
"""
fbrapi.com(fbref.com 데이터를 정식 REST API로 감싼 프록시 서비스)에서
6개 리그 24-25, 25-26 시즌 팀별 xG(득점 기대값)/xGA(실점 기대값)를 받는다.

⚠️ 2026-07-14 경위:
  1차 시도: BSD 완료 경기 xG → 목록/상세 응답 둘 다 None으로 확정, 불가.
  2차 시도: fbref.com을 직접 HTML 스크래핑 → 봇 탐지로 막힘(리서치 도구
            기준), GitHub Actions에서도 안정성 낮음.
  최종: fbrapi.com이 fbref 데이터를 3초/요청 제한까지 대신 관리해주는
        정식 JSON REST API를 제공 — 이걸 쓴다. /league-standings
        엔드포인트가 팀별 xg/xga를 이미 계산된 필드로 바로 준다
        (HTML 테이블 파싱 불필요, 리그당 API 호출 1번).

출력: data/master/xg_multileague.json
      (collect_fixtures_multileague.py가 쓰던 것과 같은 경로 — 그쪽은
      이제 이 파일을 안 건드림)

실행: requests 필요(이미 기존 파이프라인이 사용 중). API 키는 매 실행마다
      POST /generate_api_key로 새로 발급받아 쓴다(무료, 인증 불필요).
"""
import json
import os
import time

import requests

from app_export_multileague import to_kr_league

OUT_PATH = 'data/master/xg_multileague.json'
BASE_URL = 'https://fbrapi.com'
REQUEST_DELAY = 3.5  # fbref 제한(3초/요청)을 fbrapi가 대행하지만 여유를 둔다

# fbref/fbrapi 공용 리그 ID (2026-07-13 fbref.com URL로 실측 확인된 값과 동일)
LEAGUES = {
    'laliga': 12,
    'bundesliga': 20,
    'seriea': 11,
    'ligue1': 13,
    'eredivisie': 23,
    'championship': 10,
}
SEASONS = ['2024-2025', '2025-2026']


def _generate_api_key():
    try:
        r = requests.post(f'{BASE_URL}/generate_api_key', timeout=30)
    except requests.RequestException as e:
        print(f'[collect_xg_fbref] API 키 발급 실패: {e}', flush=True)
        return None
    if r.status_code != 200:
        print(f'[collect_xg_fbref] API 키 발급 HTTP {r.status_code}', flush=True)
        return None
    return r.json().get('api_key')


def _fetch_standings(api_key, league_id, season_id):
    try:
        r = requests.get(
            f'{BASE_URL}/league-standings',
            params={'league_id': league_id, 'season_id': season_id},
            headers={'X-API-Key': api_key},
            timeout=30)
    except requests.RequestException as e:
        print(f'[collect_xg_fbref] 요청 실패 league_id={league_id} '
              f'season={season_id}: {e}', flush=True)
        return None
    if r.status_code != 200:
        print(f'[collect_xg_fbref] HTTP {r.status_code}: league_id={league_id} '
              f'season={season_id}', flush=True)
        return None
    return r.json()


def main():
    api_key = _generate_api_key()
    if not api_key:
        print('[collect_xg_fbref] API 키 없음 → 중단', flush=True)
        return
    time.sleep(REQUEST_DELAY)

    result = {}
    all_unmatched = set()

    for league_key, league_id in LEAGUES.items():
        agg_xg = {}   # 한글팀명 -> [xG값들]
        agg_xga = {}  # 한글팀명 -> [xGA값들]

        for season_id in SEASONS:
            data = _fetch_standings(api_key, league_id, season_id)
            time.sleep(REQUEST_DELAY)
            if not data:
                continue
            tables = data.get('data', [])
            if not tables:
                print(f'[collect_xg_fbref] {league_key} {season_id}: '
                      f'테이블 없음', flush=True)
                continue
            # 컵 대회처럼 조별 테이블이 여러 개일 수 있어 전부 순회
            n_teams = 0
            for table in tables:
                for row in table.get('standings', []):
                    team_name = row.get('team_name')
                    xg = row.get('xg')
                    xga = row.get('xga')
                    mp = row.get('mp')
                    if not team_name or xg is None or xga is None or not mp:
                        continue
                    # fbref의 xg/xga는 시즌 누적 총합이라, 요청사항 형식
                    # (팀당 "경기당 평균" 값)에 맞게 mp로 나눈다.
                    per_match_xg = float(xg) / mp
                    per_match_xga = float(xga) / mp
                    hit = to_kr_league(team_name)
                    if not hit or hit[0] != league_key:
                        all_unmatched.add(f'{league_key}:{team_name}')
                        continue
                    _, kr = hit
                    agg_xg.setdefault(kr, []).append(per_match_xg)
                    agg_xga.setdefault(kr, []).append(per_match_xga)
                    n_teams += 1
            print(f'[collect_xg_fbref] {league_key} {season_id}: '
                  f'{n_teams}팀 xG 수집', flush=True)

        league_result = {}
        for kr in agg_xg:
            fs = agg_xg[kr]
            ags = agg_xga.get(kr)
            if fs and ags:
                league_result[kr] = {
                    'xG': round(sum(fs) / len(fs), 2),
                    'xGA': round(sum(ags) / len(ags), 2),
                }
        result[league_key] = league_result
        print(f'[collect_xg_fbref] {league_key} 최종: {len(league_result)}팀 매칭',
              flush=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    total = sum(len(v) for v in result.values())
    print(f'[collect_xg_fbref] {OUT_PATH} 생성 완료, 총 {total}팀', flush=True)
    if all_unmatched:
        sample = sorted(all_unmatched)[:20]
        print(f'[collect_xg_fbref] ⚠️ 한글 매칭 안 된 fbref 팀명 '
              f'{len(all_unmatched)}개 (샘플): {sample}', flush=True)


if __name__ == '__main__':
    main()
