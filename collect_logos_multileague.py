# -*- coding: utf-8 -*-
"""
구단 로고(엠블럼) 수집 — 인수인계 문서에 "아직 안 채워진 것" #2로 남아있던
항목(2026-07-16 착수). app_export_multileague.py의 logos 필드가 지금까지
항상 빈 딕셔너리({})였던 걸 채운다.

## 2026-08-02 소스 전면 교체: TheSportsDB → BSD 이미지 프록시
⚠️ 배경(실측 확정, 추측 아님): TheSportsDB 공식 문서
(thesportsdb.com/documentation)를 직접 확인한 결과, 무료 테스트 키
'123'의 searchteams.php는 "NOTE: Free tier limited to just 'Arsenal'.
Upgrade for full search." — 즉 임의 팀명 검색이 아예 막혀있고 데모용으로
"Arsenal" 검색 결과 하나만 허용된다(가격 페이지에도 무료 플랜 항목에
팀 검색이 없음, 선수/이벤트 검색만 있음). 2026-08-01 파이프라인 로그에서
실제 시도한 11건 전부 HTTP 404였던 게 이걸로 완전히 설명된다 — 코드
버그가 아니라 API 정책 자체였다.

대안으로 BSD(Bzzoiro Sports Data, 이미 이 파이프라인 전체가 쓰는 메인
소스) 공식 문서(sports.bzzoiro.com/docs/static-data/)를 확인하니 이미지
프록시가 있다:
    GET https://sports.bzzoiro.com/img/team/{id}/
    - {id}는 BSD API 응답에 항상 들어있는 team 객체의 숫자 id 그대로.
    - 인증 불필요, <img src>에 바로 꽂아 쓰는 방식(사전 검증 호출 자체가
      불필요 — 이미지가 없으면 그 요청 하나만 404가 뜨고 페이지 전체엔
      영향 없음, BSD 문서에 명시된 정상 동작).
    - ?bg=transparent 옵션으로 투명배경 PNG도 가능(기본은 흰 배경).
    - 365일 캐시.
팀명 검색 자체가 필요 없어진다 — collect_fixtures_multileague.py가 이미
리그별 팀 목록을 BSD team_id로 확보해두는 로직(_find_leagues/
_find_league_teams, PRIMARY_TEAM_IDS)을 그대로 재사용해서, 검색 없이
바로 URL을 조립한다. 친선전 상대팀도 마찬가지로 collect_fixtures.py의
친선리그 조회 함수를 재사용해서 이벤트 원문의 home_team_id/away_team_id를
직접 얻는다 — schedule.json을 거치면 team_id가 사라져서(이름만 남음)
이전 설계는 애초에 이 정보를 못 썼다.

football-data.org의 teams.json에도 'crest' 필드가 있지만 PL/PD/BL1/SA/
FL1(5개 대회)만 커버해서 Eredivisie·Championship이 빠진다 — BSD는 이미
전체 리그를 커버하는 메인 소스라 이 문제가 없다.
"""
import json
import os
import time

from api_clients import build_registry
from collect_fixtures import (_find_friendlies_league_id,
                               _fetch_friendlies_events, _to_kr_any_league)
import collect_fixtures_multileague as cfm

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
IMG_TPL = 'https://sports.bzzoiro.com/img/team/{tid}/'


def _load_existing():
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _known_team_names():
    """정규 8개리그+EPL 소속으로 이미 알려진 한글 팀명 전체(친선전
    상대팀 목록에서 제외하기 위함 — 이미 위 루프에서 다뤄지니 중복 방지)."""
    names = set(TEAM_NAME_MAP.keys())
    for team_map in LEAGUE_TEAM_MAPS.values():
        names.update(team_map.keys())
    return names


def _friendly_team_ids(bsd):
    """친선전(Club Friendlies) 이벤트 원문에서 상대팀명(한글 변환 또는
    실패 시 원문) -> BSD team_id 맵을 직접 만든다. schedule.json을 거치지
    않고 원본 이벤트에서 바로 뽑아야 team_id가 안 사라진다(이전 설계의
    한계였음 — 위 모듈 docstring 참고). 정규 8개리그+EPL 소속 팀은 이미
    위에서 다뤘으니 제외."""
    known = _known_team_names()
    result = {}
    try:
        league_id, _season_id = _find_friendlies_league_id(bsd)
    except Exception as e:
        print(f'[collect_logos_multileague] 친선리그 조회 실패(무시): {e}',
              flush=True)
        return result
    if not league_id:
        print('[collect_logos_multileague] Club Friendlies 리그를 못 찾음 '
              '— 친선전 상대팀 로고는 이번 실행에서 건너뜀', flush=True)
        return result
    try:
        rows = _fetch_friendlies_events(bsd, league_id)
    except Exception as e:
        print(f'[collect_logos_multileague] 친선전 이벤트 조회 실패(무시): {e}',
              flush=True)
        return result
    for ev in rows or []:
        for side in ('home', 'away'):
            tid = ev.get(f'{side}_team_id')
            raw = ev.get(f'{side}_team')
            if tid is None or not raw:
                continue
            kr = _to_kr_any_league(raw) or raw
            if kr in known:
                continue  # 정규 리그 로고로 이미 커버됨
            result.setdefault(kr, tid)
    return result


def main():
    registry = build_registry()
    bsd = registry.get('bsd')
    if not bsd:
        print('[collect_logos_multileague] bsd 클라이언트 비활성 → 스킵',
              flush=True)
        return
    if not LEAGUE_TEAM_MAPS:
        print('[collect_logos_multileague] LEAGUE_TEAM_MAPS 없음 → 중단', flush=True)
        return

    out = _load_existing()

    # --- 1) 정규 8개리그: 팀 검색 없이 리그 전체 팀목록을 BSD team_id로
    # 바로 받는다(collect_fixtures_multileague.py가 이미 검증해둔 로직
    # 재사용 — 리그당 호출 몇 번뿐이라 매 실행마다 통째로 새로 받아도
    # 부담 없음, 그래서 "캐시 재사용" 개념 자체가 불필요해짐).
    leagues_found = cfm._find_leagues(bsd)
    n_leagues_ok, n_teams_total = 0, 0
    for league_key, (league_id, season_id, _real_name) in leagues_found.items():
        if league_key not in LEAGUE_TEAM_MAPS:
            continue
        cfm._find_league_teams(bsd, league_key, league_id, season_id)
        primary = cfm.PRIMARY_TEAM_IDS.get(league_key) or {}
        if not primary:
            print(f'[collect_logos_multileague] ⚠️ {league_key} 팀 매칭 '
                  f'0건 — 이 리그 로고는 이번 실행에서 못 채움', flush=True)
            continue
        out[league_key] = {kr: IMG_TPL.format(tid=tid)
                            for kr, tid in primary.items()}
        n_leagues_ok += 1
        n_teams_total += len(primary)
        time.sleep(0.2)

    missing_leagues = set(LEAGUE_TEAM_MAPS) - set(leagues_found)
    if missing_leagues:
        print(f'[collect_logos_multileague] ⚠️ BSD에서 못 찾은 리그(로고 '
              f'못 채움): {sorted(missing_leagues)}', flush=True)

    # --- 2) 친선전 상대팀(정규 리그 소속 아님) — '_friendly' 키에 저장,
    # 리그 구분 없이 모든 앱(EPL·8개리그)이 공통으로 참조.
    friendly_ids = _friendly_team_ids(bsd)
    out.setdefault('_friendly', {})
    n_friendly_new = 0
    for kr, tid in friendly_ids.items():
        if out['_friendly'].get(kr):
            continue
        out['_friendly'][kr] = IMG_TPL.format(tid=tid)
        n_friendly_new += 1
    print(f'[collect_logos_multileague] 친선전 상대팀 {len(friendly_ids)}명 '
          f'중 신규 {n_friendly_new}건', flush=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    total = sum(len(v) for v in out.values())
    print(f'[collect_logos_multileague] 완료(BSD 소스) — 리그 {n_leagues_ok}개 '
          f'({n_teams_total}팀), 친선 신규 {n_friendly_new}건, 누적 {total}건 '
          f'→ {OUT_PATH} 저장', flush=True)
    print('[collect_logos_multileague] [diag] 검증 필요: 위 URL 몇 개를 '
          '브라우저에서 직접 열어 실제 로고가 뜨는지 확인할 것 (BSD 문서상 '
          '이미지 없는 팀은 그 요청만 404, 전체 파이프라인엔 영향 없음)',
          flush=True)


if __name__ == '__main__':
    main()
