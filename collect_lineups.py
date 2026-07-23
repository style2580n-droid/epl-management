# -*- coding: utf-8 -*-
"""
라인업(선발 명단) 수집 (2026-07-23 착수).

배경: player_baseline.json의 league_apps는 지금까지 goalscorers.json 기반으로만
갱신됐다(득점/도움에 관여한 선수만 카운트). 무득점 선수(수비수 다수, 백업
공격수 등)는 아무리 많이 뛰어도 source가 영원히 wc2026에 머문다. 라인업(선발
명단)으로 "이 선수가 이 경기에 뛰었다"를 득점 여부와 무관하게 확인해서 이
한계를 해소한다.

⚠️ 정정 (2026-07-23 rehearse_lineups_probe.py 실측, 인수인계 "다음 할 일
0번" 참조): 이 데이터로 defending per90은 갱신할 수 없다. BSD 라인업
응답엔 태클/인터셉트 같은 수비 이벤트 카운트가 없고(포지션·명단 정보뿐),
minutes_played/is_starter류 필드도 없다. 그래서 이 수집기는 "선발 11명 =
확정 출전"만 뽑는다. 벤치(substitutes)는 실제 교체 투입 여부를 이 응답만
으로 구분할 수 없어 과대집계 방지 차원에서 카운트하지 않는다(보수적 선택).
defending per90 갱신은 별도 미해결 항목(수비 스탯 소스 자체가 없음)이다.

실측 확정 경로 (rehearse_lineups_probe.py #실행, 2026-07-23, 재조사 불필요):
  events/{eid}/lineups/
  → dict(keys=['beta','event_id','lineup_status','lineups',
               'unavailable_players','updated_at'])
  → lineups = dict(keys=['home','away'])
  → lineups.home.players = 선발 11명 리스트 (이 수집기가 쓰는 것),
    keys=['ai_score','id','jersey_number','name','position','short_name']
  → lineups.home.substitutes = 벤치 리스트 (동일 키, 이 수집기는 미사용)
  나머지 후보(/lineup/, /formations/, /players/, /squads/)는 전부 404
  확정됨 → 재탐색 금지.

collect_goalscorers.py와 동일한 리그 순회/증분 캐싱 골격을 그대로 재사용
한다(팀 매칭·이벤트 조회는 이미 검증된 로직이라 새로 만들지 않음).

출력: data/master/lineups_bsd.json (db.py의 기존 lineups 테이블/로더와
      경로 충돌 나서 개명함 — 아래 OUT_PATH 옆 주석 참조)
  { "epl": [ {home, away, date, eid,
              home_starters:[영문명,...], away_starters:[영문명,...]}, ... ],
    "laliga": [...], ... 6개 리그 키 ... }
"""
import json
import os
import time

from api_clients import BSDClient
from collect_fixtures import (_find_pl_league_id, _find_pl_teams,
                               _fetch_all_league_events, _kst_date_time)
from collect_fixtures_multileague import (_find_leagues, _find_league_teams,
                                           _fetch_league_events)

# 2026-07-23 수정(파이프라인 크래시): 원래 'data/master/lineups.json'을
# 썼는데, db.py의 load_lineups()가 그 경로를 이미 선점하고 있었다(완전히
# 다른 스키마 — {fixture_id,league,team,formation,coach,starters} per-team
# 레코드용, 우리 건 리그별 리스트라 db.py가 data.values()로 리스트를
# 만나 AttributeError로 파이프라인 전체를 죽였다, 실행 로그로 확인됨).
# db.py 쪽 lineups 테이블은 손대지 않고 이름만 바꿔서 충돌을 없앤다.
OUT_PATH = 'data/master/lineups_bsd.json'

# 2026-07-23 실측 확정 (rehearse_lineups_probe.py, [rehearse_lineups] ✅ 로그).
# 다른 후보(/lineup/, /formations/, /players/, /squads/)는 전부 404 확인됨
# → 재탐색하지 말 것.
_CONFIRMED_TPL = 'events/{eid}/lineups/'
_diag_done = False


def _name_of(val):
    """BSD가 선수를 문자열로 줄지 객체로 줄지 확실치 않아 둘 다 처리
    (collect_goalscorers.py의 _name_of와 동일 방어)."""
    if val is None:
        return None
    if isinstance(val, dict):
        return (val.get('name') or val.get('short_name')
                or val.get('player_name') or val.get('full_name'))
    if isinstance(val, str):
        return val or None
    return None


def _starters_of(side):
    """lineups.{home|away} 하나에서 선발 11명의 영문명만 뽑는다. 실측
    확정된 키(players)만 신뢰한다 — 구조가 바뀌면 [diag]로만 남기고
    추측으로 다른 키를 뒤지지 않는다(그건 rehearse 스크립트의 몫)."""
    if not isinstance(side, dict):
        return []
    rows = side.get('players')
    if not isinstance(rows, list):
        return []
    names = []
    for p in rows:
        nm = _name_of(p)
        if nm:
            names.append(nm)
    return names


def _extract_starters(detail):
    """events/{eid}/lineups/ 응답에서 (home_starters, away_starters) 영문명
    리스트를 뽑는다."""
    global _diag_done
    if isinstance(detail, dict) and not _diag_done:
        _diag_done = True
        print(f'[collect_lineups] [diag] lineups 응답 sample_keys='
              f'{sorted(detail.keys())}', flush=True)
    if not isinstance(detail, dict):
        return [], []
    lineups = detail.get('lineups')
    if not isinstance(lineups, dict):
        return [], []
    return _starters_of(lineups.get('home')), _starters_of(lineups.get('away'))


def _collect_league(client, league_key, league_id, team_ids, fetch_events_fn,
                     prev_by_eid=None):
    rows = fetch_events_fn(client, league_id)
    prev_by_eid = prev_by_eid or {}
    matches = []
    n_finished, n_with_lineup, n_reused = 0, 0, 0
    for ev in rows:
        status = (ev.get('status') or '').lower()
        if status != 'finished':
            continue
        home_kr = team_ids.get(ev.get('home_team_id'))
        away_kr = team_ids.get(ev.get('away_team_id'))
        if not (home_kr and away_kr):
            continue
        n_finished += 1
        eid = ev.get('id')
        if eid is None:
            continue

        # 증분 수집: 선발 명단 확보됐거나, 확인했는데 BSD에 없던 경기
        # (checked)는 재호출하지 않는다 (collect_goalscorers.py와 동일 패턴).
        prev_m = prev_by_eid.get(eid)
        if prev_m and (prev_m.get('home_starters') or prev_m.get('checked')):
            matches.append(prev_m)
            n_reused += 1
            if prev_m.get('home_starters'):
                n_with_lineup += 1
            continue

        try:
            raw = client.get(_CONFIRMED_TPL.format(eid=eid))
        except Exception:
            raw = None
        time.sleep(0.2)
        if isinstance(raw, tuple):
            raw = raw[0]
        home_starters, away_starters = _extract_starters(raw)
        # raw는 왔는데(경로 정상) 선발이 안 뽑히면 이 경기는 확인 완료로
        # 표시(재호출 방지). raw 자체가 없으면(예외/404) 다음 실행에 재시도.
        checked = raw is not None and not (home_starters or away_starters)

        date_kst, _ = _kst_date_time(ev.get('event_date'))
        m = {
            'home': home_kr, 'away': away_kr, 'date': date_kst, 'eid': eid,
            'home_starters': home_starters, 'away_starters': away_starters,
        }
        if checked:
            m['checked'] = True
        matches.append(m)
        if home_starters or away_starters:
            n_with_lineup += 1
    print(f'[collect_lineups] {league_key}: 종료경기 {n_finished}건 중 '
          f'선발명단 확보 {n_with_lineup}건 (기존 재사용 {n_reused}건)', flush=True)
    return matches


def _load_prev():
    """이전 실행 결과를 {리그: {eid: match}} 형태로 로드 (증분 수집 기준)."""
    if not os.path.exists(OUT_PATH):
        return {}
    try:
        with open(OUT_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {lk: {m.get('eid'): m for m in v if isinstance(m, dict)}
            for lk, v in data.items() if isinstance(v, list)}


def main():
    client = BSDClient()
    if not client.enabled:
        print('[collect_lineups] BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    out = {}
    prev_out = _load_prev()

    # ---- EPL ----
    league_id, season_id = _find_pl_league_id(client)
    if league_id:
        team_ids = _find_pl_teams(client, league_id, season_id)
        if team_ids:
            out['epl'] = _collect_league(
                client, 'epl', league_id, team_ids,
                lambda c, lid: _fetch_all_league_events(c, lid, set(team_ids.keys())),
                prev_by_eid=prev_out.get('epl', {}))
        else:
            out['epl'] = []
    else:
        out['epl'] = []

    # ---- 6개 리그 ----
    leagues = _find_leagues(client)
    for league_key, (league_id, season_id, _real_name) in (leagues or {}).items():
        team_ids = _find_league_teams(client, league_key, league_id, season_id)
        if not team_ids:
            out[league_key] = []
            continue
        out[league_key] = _collect_league(
            client, league_key, league_id, team_ids,
            lambda c, lid: _fetch_league_events(c, lid),
            prev_by_eid=prev_out.get(league_key, {}))

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    total_matches = sum(len(v) for v in out.values())
    total_with_lineup = sum(1 for v in out.values() for m in v if m.get('home_starters'))
    print(f'[collect_lineups] 완료 — 종료경기 {total_matches}건 중 '
          f'선발명단 확보 {total_with_lineup}건 → {OUT_PATH} 저장', flush=True)


if __name__ == '__main__':
    main()
