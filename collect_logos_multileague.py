# -*- coding: utf-8 -*-
"""
구단 로고(엠블럼) 수집 — 인수인계 문서에 "아직 안 채워진 것" #2로 남아있던
항목(2026-07-16 착수). app_export_multileague.py의 logos 필드가 지금까지
항상 빈 딕셔너리({})였던 걸 채운다.

소스: TheSportsDB(api_clients.py TheSportsDBClient) — 무료 테스트 키('123')로
바로 쓸 수 있어서 별도 API 키 등록 없이 동작한다. football-data.org의
teams.json에도 'crest' 필드가 있지만 PL/PD/BL1/SA/FL1(5개 대회)만 커버해서
Eredivisie·Championship이 빠진다 — 반면 TheSportsDB 팀 검색은 리그 상관없이
팀명 하나로 찾는 방식이라 6개 리그 전부에 똑같이 적용 가능해서 이쪽을 1차
소스로 쓴다.

⚠️ 실전 미검증 지점: TheSportsDB 응답 객체의 배지 URL 필드명이 문서 버전에
따라 strTeamBadge/strBadge로 갈릴 수 있다(collectors.py TeamCollector는
strBadge를 쓰고 있었음, 2026-07-13 이전 코드). 여기선 후보 필드명을 순서대로
다 시도하고, 첫 응답의 실제 키 목록을 로그로 남겨서 다음 실행 때 바로
확인할 수 있게 해둔다.
"""
import json
import os
import time

from api_clients import build_registry

try:
    from app_export_multileague import LEAGUE_TEAM_MAPS
except ImportError as e:
    LEAGUE_TEAM_MAPS = {}
    print(f'[collect_logos_multileague] ⚠️ LEAGUE_TEAM_MAPS 임포트 실패: {e} '
          f'→ app_export_multileague.py와 같은 폴더에서 실행해야 한다', flush=True)

OUT_PATH = 'data/master/logos_multileague.json'
_BADGE_KEYS = ('strTeamBadge', 'strBadge', 'strTeamLogo', 'strLogo')
_diag_done = False


def _load_existing():
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _extract_badge(team_obj):
    for key in _BADGE_KEYS:
        val = team_obj.get(key)
        if val:
            return val
    return None


def main():
    registry = build_registry()
    tsdb = registry.get('thesportsdb')
    if not tsdb:
        print('[collect_logos_multileague] thesportsdb 클라이언트 비활성 → 스킵',
              flush=True)
        return
    if not LEAGUE_TEAM_MAPS:
        print('[collect_logos_multileague] LEAGUE_TEAM_MAPS 없음 → 중단', flush=True)
        return

    global _diag_done
    out = _load_existing()
    n_new, n_skipped_cached, n_failed = 0, 0, 0
    failed_names = []

    for league_key, teams in LEAGUE_TEAM_MAPS.items():
        out.setdefault(league_key, {})
        for kr, aliases in teams.items():
            if out[league_key].get(kr):
                n_skipped_cached += 1
                continue
            search_name = aliases[0]  # 가장 완전한 공식명(별칭 목록 첫 항목)
            resp, ok = tsdb.search_team(search_name)
            time.sleep(0.3)  # TheSportsDB 무료 키 레이트리밋 여유
            if not _diag_done:
                _diag_done = True
                teams_list = (resp or {}).get('teams') or []
                sample = teams_list[0] if teams_list else {}
                print(f'[collect_logos_multileague] [diag] search_team('
                      f'{search_name!r}) sample_keys='
                      f'{sorted(sample.keys()) if sample else []}', flush=True)
            if not (ok and resp and resp.get('teams')):
                n_failed += 1
                failed_names.append(f'{league_key}:{kr}')
                continue
            badge = _extract_badge(resp['teams'][0])
            if badge:
                out[league_key][kr] = badge
                n_new += 1
            else:
                n_failed += 1
                failed_names.append(f'{league_key}:{kr}')

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    total = sum(len(v) for v in out.values())
    print(f'[collect_logos_multileague] 완료 — 신규 {n_new}건, 캐시 재사용 '
          f'{n_skipped_cached}건, 실패 {n_failed}건, 누적 {total}건 → '
          f'{OUT_PATH} 저장', flush=True)
    if failed_names:
        print(f'[collect_logos_multileague] ⚠️ 매칭 실패 샘플(최대 20개): '
              f'{failed_names[:20]}', flush=True)


if __name__ == '__main__':
    main()
