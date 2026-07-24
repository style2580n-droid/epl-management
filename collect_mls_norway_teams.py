# -*- coding: utf-8 -*-
"""
MLS·엘리테세리엔 팀 ID ↔ 한글명 매칭 검증 (2026-07-24 착수).

목적: mls_norway_team_maps.py의 별칭 초안이 BSD 실제 팀명과 얼마나
매칭되는지 확인한다. 앱 통합은 보류 상태라 이 스크립트는 진단 결과만
data/master/teams_mls_norway.json에 저장 — app_export*.py의 어떤 것도
읽지 않고, 이 파일도 그쪽에서 안 읽는다(완전히 독립적).

정규화 로직은 app_export_multileague.py의 _norm()과 동일하게 맞춰서
(악센트 제거 + FC/AFC/CF 제거) 실전과 같은 조건으로 검증한다.

collect_fixtures_multileague.py의 _fetch_all_teams/_find_league_teams
패턴 재사용(페이지네이션, 후보 파라미터 시도).

실행: BSD_API_KEY 필요. 없으면 조용히 스킵.
"""
import json
import os
import re
import time
import unicodedata

from api_clients import BSDClient
from mls_norway_team_maps import MLS_NORWAY_TEAM_MAPS, LEAGUE_IDS

OUT_PATH = 'data/master/teams_mls_norway.json'
PAGE_LIMIT = 200
LOG = '[collect_mls_norway_teams]'


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _ascii_fold(s):
    s = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in s if not unicodedata.combining(c))


def _norm(name):
    """app_export_multileague._norm과 동일 — 실전과 같은 조건으로 검증."""
    if not name:
        return ''
    n = _ascii_fold(name)
    n = re.sub(r'\b(FC|AFC|CF)\b', '', n, flags=re.I)
    return re.sub(r'[^a-z가-힣0-9]', '', n.lower())


def _build_lookup(team_map):
    lookup = {}
    for kr, aliases in team_map.items():
        for a in aliases + [kr]:
            lookup[_norm(a)] = kr
    return lookup


def _fetch_all_teams(client, params):
    rows, offset = [], 0
    while True:
        try:
            data = _unwrap(client.teams(limit=PAGE_LIMIT, offset=offset, **params))
        except TypeError:
            data = _unwrap(client.teams(**params))
            return (data or {}).get('results', []) if data else rows
        time.sleep(0.2)
        if not data:
            break
        page = data.get('results', [])
        rows.extend(page)
        count = data.get('count')
        offset += PAGE_LIMIT
        if not page or len(page) < PAGE_LIMIT \
                or (count is not None and offset >= count) \
                or offset >= PAGE_LIMIT * 10:
            break
    return rows


def _match_league(client, league_key):
    ids = LEAGUE_IDS[league_key]
    team_map = MLS_NORWAY_TEAM_MAPS[league_key]
    lookup = _build_lookup(team_map)
    n_expected = len(team_map)

    candidates = [
        {'league_id': ids['league_id']},
        {'league': ids['league_id']},
        {'league_id': ids['league_id'], 'season': ids['season_id']},
    ]
    for params in candidates:
        results = _fetch_all_teams(client, params)
        if not results:
            continue
        matched = {}     # id -> kr_name
        by_kr = {}        # kr_name -> [(id, raw), ...] (중복 진단용)
        unmatched = []
        for t in results:
            raw = t.get('name') or t.get('short_name')
            kr = lookup.get(_norm(raw))
            if kr:
                matched[t['id']] = kr
                by_kr.setdefault(kr, []).append((t['id'], raw))
            elif raw:
                unmatched.append(raw)
        print(f'{LOG} {league_key} teams{params} → 응답 {len(results)}개 중 '
              f'{len(matched)}개 ID 매칭(클럽 {len(by_kr)}개, 기대 {n_expected}팀)',
              flush=True)
        if len(matched) < max(3, n_expected * 0.3):
            continue
        missing = [kr for kr in team_map if kr not in by_kr]
        if missing:
            print(f'{LOG} {league_key} 미매칭 기대팀 {len(missing)}개: {missing}',
                  flush=True)
        if unmatched:
            print(f'{LOG} {league_key} 응답에 있었지만 안 걸린 원문 팀명 '
                  f'{len(unmatched)}개: {unmatched}', flush=True)
        dup = {kr: lst for kr, lst in by_kr.items() if len(lst) > 1}
        if dup:
            print(f'{LOG} {league_key} 중복 ID(대표 선정 필요, 이 스크립트는 '
                  f'선정 안 함): {dup}', flush=True)
        return matched, by_kr
    print(f'{LOG} {league_key} 팀 응답 자체를 못 받음(모든 파라미터 후보 실패)',
          flush=True)
    return {}, {}


def main():
    client = BSDClient()
    if not client.enabled:
        print(f'{LOG} BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    out = {}
    for league_key in MLS_NORWAY_TEAM_MAPS:
        matched, by_kr = _match_league(client, league_key)
        out[league_key] = {str(tid): kr for tid, kr in matched.items()}

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    total = sum(len(v) for v in out.values())
    print(f'{LOG} 완료 — 총 {total}개 팀 ID 매칭 → {OUT_PATH} 저장 (진단용, '
          f'앱에서 안 씀)', flush=True)


if __name__ == '__main__':
    main()
