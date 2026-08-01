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

## 2026-08-01 추가: 프리시즌 친선전 상대팀 로고
정규 8개리그(LEAGUE_TEAM_MAPS)+EPL(TEAM_NAME_MAP) 소속 팀만 순회하던 게
원래 설계였는데, 프리시즌 친선전(collect_fixtures*.py가 만드는
'friendly': true 경기)엔 이 목록에 없는 상대팀(SC 캄뷔르, 라싱
스트라스부르 등)이 자주 나와서 로고가 항상 텍스트 약어로만 표시되고
있었다(실측 확인 — 사용자 스크린샷). data/master/schedule.json +
schedule_multileague.json에서 friendly 경기의 상대팀을 뽑아 같은
TheSportsDB 검색으로 로고를 채우고, 리그 구분 없는 별도 키('_friendly')에
저장한다 — app_export.py/app_export_multileague.py가 이 키를 모든 리그
block에 공통으로 병합해서 EPL·8개리그 앱 둘 다 프리시즌 상대팀 로고를
쓸 수 있게 한다.
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
try:
    from app_export import TEAM_NAME_MAP
except ImportError as e:
    TEAM_NAME_MAP = {}
    print(f'[collect_logos_multileague] ⚠️ TEAM_NAME_MAP(EPL) 임포트 실패: {e}',
          flush=True)

OUT_PATH = 'data/master/logos_multileague.json'
_BADGE_KEYS = ('strTeamBadge', 'strBadge', 'strTeamLogo', 'strLogo')
_diag_done = False
MAX_NEW_FRIENDLY_TEAMS_PER_RUN = 40  # TheSportsDB 레이트리밋 보호(실행당 상한)


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


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _known_team_names():
    """정규 8개리그+EPL 소속으로 이미 알려진 한글 팀명 전체(별칭 검색
    대상에서 제외하기 위함 — 이미 위 루프에서 다뤄지니 중복 조회 방지)."""
    names = set(TEAM_NAME_MAP.keys())
    for team_map in LEAGUE_TEAM_MAPS.values():
        names.update(team_map.keys())
    return names


def _friendly_opponent_names():
    """schedule.json(EPL) + schedule_multileague.json(8개리그)에서 friendly
    경기의 상대팀 이름을 뽑는다. 이미 정규 리그 소속으로 아는 팀은 제외
    (예: EPL팀 vs EPL팀 친선전이면 둘 다 이미 커버되니 스킵)."""
    known = _known_team_names()
    names = set()

    epl_schedule = _load_json('data/master/schedule.json', [])
    if isinstance(epl_schedule, list):
        for g in epl_schedule:
            if not isinstance(g, dict) or not g.get('friendly'):
                continue
            for side in ('home', 'away'):
                n = g.get(side)
                if n and n not in known:
                    names.add(n)

    ml_schedule = _load_json('data/master/schedule_multileague.json', {})
    if isinstance(ml_schedule, dict):
        for games in ml_schedule.values():
            if not isinstance(games, list):
                continue
            for g in games:
                if not isinstance(g, dict) or not g.get('friendly'):
                    continue
                for side in ('home', 'away'):
                    n = g.get(side)
                    if n and n not in known:
                        names.add(n)
    return names


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

    # 2026-08-01 추가: 친선전 상대팀(정규 리그 소속 아님) — 별도 '_friendly'
    # 키에 저장, 리그 구분 없이 모든 앱이 공통으로 참조.
    out.setdefault('_friendly', {})
    friendly_names = _friendly_opponent_names()
    todo = [n for n in friendly_names if not out['_friendly'].get(n)]
    print(f'[collect_logos_multileague] 친선전 상대팀 {len(friendly_names)}명 '
          f'중 로고 미보유 {len(todo)}명', flush=True)
    n_friendly_new = n_friendly_failed = 0
    for kr in todo[:MAX_NEW_FRIENDLY_TEAMS_PER_RUN]:
        resp, ok = tsdb.search_team(kr)
        time.sleep(0.3)
        if not (ok and resp and resp.get('teams')):
            n_friendly_failed += 1
            continue
        badge = _extract_badge(resp['teams'][0])
        if badge:
            out['_friendly'][kr] = badge
            n_friendly_new += 1
        else:
            n_friendly_failed += 1
    if len(todo) > MAX_NEW_FRIENDLY_TEAMS_PER_RUN:
        print(f'[collect_logos_multileague] 친선전 상대팀도 실행당 상한'
              f'({MAX_NEW_FRIENDLY_TEAMS_PER_RUN}명) 적용 → 나머지는 다음 실행',
              flush=True)
    print(f'[collect_logos_multileague] 친선전 상대팀: 신규 {n_friendly_new}건, '
          f'실패 {n_friendly_failed}건(대부분 한글명 그대로라 TheSportsDB 검색이 '
          f'안 될 수 있음 — 실측 확인 필요)', flush=True)

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
