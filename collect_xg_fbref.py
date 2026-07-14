# -*- coding: utf-8 -*-
"""
fbref.com에서 6개 리그(라리가/분데스리가/세리에A/리그앙/에레디비시/챔피언십)
24-25, 25-26 시즌 팀별 xG(득점 기대값)/xGA(실점 기대값)를 스크래핑한다.

⚠️ 2026-07-14 경위: BSD는 완료된 경기의 xG를 제공하지 않는 것으로 확정됨
(collect_fixtures_multileague.py의 진단 로그로 확인 — 목록/상세 응답 둘 다
stats=None). understat은 5대 리그만 커버(에레디비시/챔피언십 없음)하고
robots.txt가 자동화 접근을 명시적으로 금지해서 제외. StatsBomb Open Data는
전체 시즌이 아니라 일부 쇼케이스 경기만 공개돼 있어 제외. fbref가 6개
리그를 다 커버하는 유일한 무료 소스라 이걸 쓴다.

⚠️ fbref는 봇 탐지가 있다(제 리서치 도구에서도 막힌 바 있음). 그래서:
- 브라우저처럼 보이는 User-Agent 사용
- 요청 사이 텀을 넉넉히 둠(리그당 2개 시즌 = 12번의 페이지 요청, 전부)
- 실패해도 파이프라인 전체가 죽지 않도록 개별 리그/시즌 단위로 예외 처리
- 첫 응답에서 테이블을 못 찾으면 원인 파악용 진단 로그를 남김(추측 금지 원칙)

출력: data/master/xg_multileague.json (collect_fixtures_multileague.py가
쓰던 것과 같은 경로 — 그쪽은 이제 이 파일을 안 건드림)

실행: requests, beautifulsoup4 필요 (requirements.txt에 없으면 추가 필요).
"""
import json
import os
import re
import time

import requests
from bs4 import BeautifulSoup, Comment

from app_export_multileague import to_kr_league

OUT_PATH = 'data/master/xg_multileague.json'

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'),
    'Accept-Language': 'en-US,en;q=0.9',
}
REQUEST_DELAY = 4  # fbref 권장: 요청 사이 몇 초 텀 (봇 탐지 회피)

# fbref competition ID + URL slug (2026-07-14 실측 확인, 검색으로 검증됨)
LEAGUES = {
    'laliga':       (12, 'La-Liga'),
    'bundesliga':   (20, 'Bundesliga'),
    'seriea':       (11, 'Serie-A'),
    'ligue1':       (13, 'Ligue-1'),
    'eredivisie':   (23, 'Eredivisie'),
    'championship': (10, 'Championship'),
}
SEASONS = ['2024-2025', '2025-2026']


def _fetch_html(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f'[collect_xg_fbref] 요청 실패: {url} → {e}', flush=True)
        return None
    if r.status_code != 200:
        print(f'[collect_xg_fbref] HTTP {r.status_code}: {url}', flush=True)
        return None
    return r.text


def _find_table(soup, id_prefix):
    """fbref는 일부 테이블을 HTML 주석 안에 숨겨둔다 — 직접 찾고, 없으면
    주석 안까지 뒤진다."""
    table = soup.find('table', id=lambda x: x and x.startswith(id_prefix))
    if table:
        return table
    for comment in soup.find_all(string=lambda x: isinstance(x, Comment)):
        if id_prefix in comment:
            csoup = BeautifulSoup(comment, 'html.parser')
            table = csoup.find('table', id=lambda x: x and x.startswith(id_prefix))
            if table:
                return table
    return None


def _parse_squad_xg_table(table):
    """<table id="stats_squads_standard_for"|"...against"> 에서
    {팀명: xG값} 딕셔너리를 뽑는다. fbref는 헤더가 2단(카테고리+지표)이라
    'xG'라는 지표명이 정확히 일치하는 열을 찾는다."""
    thead = table.find('thead')
    header_rows = thead.find_all('tr')
    header_cells = header_rows[-1].find_all(['th', 'td'])  # 실제 지표명은 마지막 헤더행
    xg_col_idx = None
    for i, cell in enumerate(header_cells):
        if cell.get_text(strip=True) == 'xG':
            xg_col_idx = i
            break
    if xg_col_idx is None:
        return None

    result = {}
    tbody = table.find('tbody')
    for row in tbody.find_all('tr'):
        cells = row.find_all(['th', 'td'])
        if not cells:
            continue
        team_cell = cells[0]
        team_name = team_cell.get_text(strip=True)
        if not team_name or xg_col_idx >= len(cells):
            continue
        xg_text = cells[xg_col_idx].get_text(strip=True)
        try:
            result[team_name] = float(xg_text)
        except ValueError:
            continue
    return result


def _fetch_league_season_xg(league_key, comp_id, slug, season):
    url = f'https://fbref.com/en/comps/{comp_id}/{season}/{season}-{slug}-Stats'
    html = _fetch_html(url)
    time.sleep(REQUEST_DELAY)
    if not html:
        return None
    soup = BeautifulSoup(html, 'html.parser')

    for_table = _find_table(soup, 'stats_squads_standard_for')
    against_table = _find_table(soup, 'stats_squads_standard_against')
    if not for_table or not against_table:
        # ⚠️ 진단: 테이블을 못 찾은 게 봇 차단 때문인지 구조가 다른 건지
        # 확인할 수 있게 페이지 제목/테이블 id 목록을 남긴다.
        title = soup.find('title')
        all_table_ids = [t.get('id') for t in soup.find_all('table') if t.get('id')]
        print(f'[collect_xg_fbref] [diag] {league_key} {season} 테이블 못 찾음. '
              f'페이지제목={title.get_text() if title else None}, '
              f'테이블id목록={all_table_ids[:20]}', flush=True)
        return None

    xg_for = _parse_squad_xg_table(for_table)
    xg_against = _parse_squad_xg_table(against_table)
    if not xg_for or not xg_against:
        print(f'[collect_xg_fbref] [diag] {league_key} {season} xG 열을 '
              f'테이블에서 못 찾음', flush=True)
        return None

    return xg_for, xg_against


def main():
    result = {}
    all_unmatched = set()
    for league_key, (comp_id, slug) in LEAGUES.items():
        # 시즌별 xG를 모아서 평균낸다 (24-25 + 25-26 합산 평균)
        agg_for = {}   # fbref 영문 팀명 -> [xG값들]
        agg_against = {}

        for season in SEASONS:
            try:
                out = _fetch_league_season_xg(league_key, comp_id, slug, season)
            except Exception as e:
                print(f'[collect_xg_fbref] {league_key} {season} 처리 중 예외: {e}',
                      flush=True)
                continue
            if not out:
                continue
            xg_for, xg_against = out
            for team, val in xg_for.items():
                agg_for.setdefault(team, []).append(val)
            for team, val in xg_against.items():
                agg_against.setdefault(team, []).append(val)
            print(f'[collect_xg_fbref] {league_key} {season}: '
                  f'{len(xg_for)}팀 xG 수집', flush=True)

        # fbref 영문 팀명 -> 한글 팀명 매핑 (app_export_multileague의
        # LEAGUE_TEAM_MAPS 별칭으로 매칭. fbref는 줄임말 표기를 쓰기도 해서
        # 매칭 실패한 이름은 진단 로그로 남긴다 — 필요하면 별칭 추가)
        league_result = {}
        for team in agg_for:
            fs = agg_for.get(team)
            ags = agg_against.get(team)
            if not (fs and ags):
                continue
            hit = to_kr_league(team)
            if not hit or hit[0] != league_key:
                all_unmatched.add(f'{league_key}:{team}')
                continue
            _, kr = hit
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
