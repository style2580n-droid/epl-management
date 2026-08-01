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

## 2026-08-02 추가 수정: 친선전 상대팀 범위 문제 (배포 후 실측 발견)
위 방식으로 첫 배포한 실행(2026-08-01 22:19~22:21 KST)에서 로그 확인
결과, 정규 8개리그는 정상(180팀)이었지만 친선전 상대팀이 2705명이나
잡히는 문제가 있었다 — collect_fixtures.py의 _fetch_friendlies_events를
그대로 갖다 썼더니 과거 3년치까지 전세계 Club Friendlies 리그 전체를
훑어버려서, 우리 8개리그+EPL과 무관한 나라의 친선전까지 전부 딸려왔다
(관련성 필터가 없었던 게 원인). 수정: (1) 조회 범위를 앞으로 60일로
직접 좁히고, (2) EPL+8개리그 전체 BSD team_id 집합을 먼저 만들어서
그중 최소 한쪽이 낀 경기만 관련 있는 걸로 보고 '모르는 쪽'만 기록하도록
고쳤다(_fetch_upcoming_friendlies + known_ids 필터).
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from api_clients import build_registry
from collect_fixtures import (_find_friendlies_league_id, _find_pl_league_id,
                               _find_pl_teams, _to_kr_any_league)
import collect_fixtures_multileague as cfm

# 2026-08-02 수정: 로고 용도로는 '앞으로 표시될 예정 경기'만 있으면
# 충분한데, collect_fixtures.py의 _fetch_friendlies_events는 과거 3년치
# 까지 전세계 Club Friendlies 리그 전체를 페이지네이션한다(실측 확인:
# 2026-08-01 22:19~22:21 실행에서 상대팀 2705명이 나왔는데, 그중 우리
# 8개리그+EPL과 실제로 관련 있는 친선전은 극소수이고 나머지는 전혀 무관한
# 전세계 하위리그 클럽끼리의 과거 친선전이었다 — 관련성 필터가 아예
# 없었던 게 원인). 앞으로 60일로 직접 좁혀서 조회하고, 우리 팀이 한쪽에
# 낀 경기만 걸러낸다.
_FRIENDLY_DATE_FROM = datetime.now(timezone.utc).strftime('%Y-%m-%d')
_FRIENDLY_DATE_TO = (datetime.now(timezone.utc)
                      + timedelta(days=60)).strftime('%Y-%m-%d')

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


def _fetch_upcoming_friendlies(bsd, league_id):
    """앞으로 60일 범위로만 좁혀서 Club Friendlies 이벤트를 받는다
    (collect_fixtures._fetch_friendlies_events는 3년치 과거까지 훑어서
    로고 용도엔 과함 — 위 모듈 상단 주석 참고)."""
    rows = []
    offset = 0
    while offset < 2000:  # 안전 상한(60일 범위면 이 정도로 충분)
        resp = bsd.events(league=league_id, date_from=_FRIENDLY_DATE_FROM,
                           date_to=_FRIENDLY_DATE_TO, limit=200, offset=offset)
        data = resp[0] if isinstance(resp, tuple) else resp
        if not data:
            break
        results = data.get('results', [])
        rows.extend(results)
        total = data.get('count', len(results))
        offset += 200
        if offset >= total or not results:
            break
    return rows


def _friendly_team_ids(bsd, known_ids):
    """친선전(Club Friendlies) 이벤트 원문에서 상대팀명(한글 변환 또는
    실패 시 원문) -> BSD team_id 맵을 만든다. schedule.json을 거치지 않고
    원본 이벤트에서 바로 뽑아야 team_id가 안 사라진다(이전 설계의 한계
    였음). known_ids(EPL+8개리그 전체 team_id 집합)에 최소 한쪽이 껴있는
    경기만 관련 있는 경기로 보고, 그 경기의 양쪽 팀을 전부 기록한다(한쪽만
    known이어도 관련 경기로 인정하지만, 기록 자체는 known 여부 무관하게
    양쪽 다 — 이유는 아래 루프 안 주석 참고). 양쪽 다 우리 팀과 무관한
    전세계 하위리그 친선전은 전부 스킵(2026-08-01 실행에서 이 필터 없이
    2705명이 잡혔던 문제 수정)."""
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
        rows = _fetch_upcoming_friendlies(bsd, league_id)
    except Exception as e:
        print(f'[collect_logos_multileague] 친선전 이벤트 조회 실패(무시): {e}',
              flush=True)
        return result
    n_total, n_relevant = len(rows or []), 0
    for ev in rows or []:
        home_tid, away_tid = ev.get('home_team_id'), ev.get('away_team_id')
        home_known = home_tid in known_ids
        away_known = away_tid in known_ids
        if not (home_known or away_known):
            continue  # 우리 8개리그+EPL과 무관한 친선전 — 스킵
        n_relevant += 1
        # 2026-08-02 수정: 예전엔 "양쪽 다 known이면 스킵"이었는데, 이건
        # EPL 앱 실측으로 확인된 버그였다 — EPL 앱(app_export.py)은
        # '_friendly' 키 하나만 읽고 8개리그 각자의 버킷은 아예 안 본다.
        # 그래서 레알 마드리드(라리가, known) vs 피오렌티나(세리에A,
        # known) 같은 "서로 다른 두 트래킹 리그끼리의" 친선전에서, 두 팀
        # 다 known이라는 이유로 스킵해버리면 EPL 페이지에서 이 경기를 볼
        # 때 양쪽 다 로고가 없다(사용자 스크린샷으로 실측 확인). known
        # 인지 아닌지와 무관하게, 관련 있는 경기라면 양쪽 팀을 전부
        # 기록한다 — 8개리그 앱은 자기 리그 버킷을 뒤에 스프레드해서
        # 우선시키니(app_export_multileague.py) 값이 겹쳐도 무해하고,
        # EPL 앱은 이 키가 있어야만 뜨므로 반드시 필요하다.
        for tid, raw in ((home_tid, ev.get('home_team')),
                         (away_tid, ev.get('away_team'))):
            if tid is None or not raw:
                continue
            kr = _to_kr_any_league(raw) or raw
            result.setdefault(kr, tid)
    print(f'[collect_logos_multileague] Club Friendlies 앞으로 60일 '
          f'{n_total}건 중 우리 팀 관련 {n_relevant}건, 로고 대상 팀 '
          f'{len(result)}명(known 포함 — EPL 앱 크로스리그 표시용)', flush=True)
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
    # 관련성 판정을 위해 EPL 20개 팀의 BSD team_id도 필요(위 1번은
    # 8개리그만 다뤘음 — EPL은 로컬 하드코딩 로고를 쓰니 로고 자체는
    # 필요 없지만, "EPL팀 vs 하위리그팀" 친선전을 관련 있다고 인식하려면
    # EPL team_id 집합이 있어야 한다).
    known_ids = set()
    for primary in cfm.PRIMARY_TEAM_IDS.values():
        known_ids.update(primary.values())
    try:
        epl_league_id, epl_season_id = _find_pl_league_id(bsd)
        if epl_league_id:
            epl_team_ids = _find_pl_teams(bsd, epl_league_id, epl_season_id)
            known_ids.update(epl_team_ids.keys())
    except Exception as e:
        print(f'[collect_logos_multileague] EPL team_id 조회 실패(무시, '
              f'친선전 관련성 판정 일부만 됨): {e}', flush=True)

    # 2026-08-02 추가 수정: '_friendly'를 기존 값에 누적 병합만 하면, 관련성
    # 필터 도입 전에 잘못 쌓인 2705건짜리 오염 데이터가 새 버전을 올려도
    # 영원히 안 지워지고 누적 카운트에 계속 남는다(실측 확인 — 필터 버그를
    # 고친 버전을 올려도 '누적'에 옛날 무관 친선팀들이 그대로 남아있었음).
    # 8개리그 로고(위 1번)와 똑같이, 이번 실행에서 새로 계산한 값으로
    # 완전히 덮어쓴다 — 조회 자체가 이제 가볍기 때문에(60일 제한) 매번
    # 새로 통째로 만들어도 문제없다.
    friendly_ids = _friendly_team_ids(bsd, known_ids)
    out['_friendly'] = {kr: IMG_TPL.format(tid=tid)
                         for kr, tid in friendly_ids.items()}
    n_friendly_new = len(friendly_ids)
    print(f'[collect_logos_multileague] 친선전 상대팀 {len(friendly_ids)}명 '
          f'(이전 실행 데이터 포함 전부 새로 계산)', flush=True)

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
