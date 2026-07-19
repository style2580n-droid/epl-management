# -*- coding: utf-8 -*-
"""
득점자/도움 기록 수집 (2026-07-18 착수 — 8월 26-27 시즌 개막 대비).

⚠️ 8월 개막 전이라 지금은 FINISHED 경기가 사실상 없다 — 이 스크립트가
"수집 0건"으로 나오는 건 지금 시점엔 버그가 아니라 정상이다. 개막 후
첫 실제 종료 경기가 생기면 로그의 [diag] 줄을 꼭 확인할 것.

배경: data/metrics/*.json(impact_engine.py 산출물)은 StatsBomb 공개데이터
(북유럽 리그·국가대표 친선전 등 이번 시즌과 무관한 경기)로 채워지고 있어서
EPL/6개 리그의 진짜 득점왕/도움왕 집계에 못 쓴다는 걸 2026-07-18에 확인
했다. impact_engine의 55개+ 지표짜리 정밀 이벤트 엔진(좌표 단위 추적 필요)
대신, "이 경기에서 누가 넣었고 누가 도왔나"만 뽑는 훨씬 가벼운 전용
수집기로 간다.

소스: BSD event_detail() — collect_fixtures.py/collect_fixtures_multileague.py
가 이미 확보해둔 리그 검색·팀 매칭 로직을 그대로 재사용해서 종료 경기
목록을 얻고, 경기마다 상세 조회를 한 번씩 추가로 한다.
⚠️ 이 엔드포인트가 득점자 정보를 실제로 어떤 필드명으로 주는지는 이
시점(개막 전) 검증이 원천적으로 불가능하다 — 종료 경기 자체가 없어서
호출할 대상이 없기 때문. 그래서 흔한 필드명 후보를 최대한 넓게 방어적으로
시도하고, 첫 성공 응답의 실제 키 목록을 [diag] 로그로 남긴다. 그 로그
보고 _extract_goals()를 실제 스키마에 맞게 고쳐야 할 가능성이 높다
(추측 금지 원칙 — 실행 결과로 검증).

출력: data/master/goalscorers.json
  { "epl": [ {home, away, date, eid, goals:[{scorer, assist, team, minute}]}, ... ],
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

OUT_PATH = 'data/master/goalscorers.json'
_diag_done = False


def _name_of(val):
    """BSD가 선수를 문자열('Kevin De Bruyne')로 줄지, 객체({'name':...})로
    줄지 확실치 않아 둘 다 처리."""
    if val is None:
        return None
    if isinstance(val, dict):
        return val.get('name') or val.get('player_name') or val.get('full_name')
    if isinstance(val, str):
        return val or None
    return None


def _extract_goals(detail):
    """event_detail() 응답에서 득점 이벤트 리스트를 뽑는다.
    실전 스키마 미검증 상태라 흔한 후보를 순서대로 다 시도한다."""
    global _diag_done
    if isinstance(detail, dict) and not _diag_done:
        _diag_done = True
        print(f'[collect_goalscorers] [diag] event_detail sample_keys='
              f'{sorted(detail.keys())}', flush=True)

    if not isinstance(detail, dict):
        return []

    goals = []
    # 후보 1: 'goals' 필드가 바로 리스트로 있는 경우 (football-data.org류 스키마)
    raw_goals = detail.get('goals')
    if isinstance(raw_goals, list) and raw_goals:
        for g in raw_goals:
            if not isinstance(g, dict):
                continue
            scorer = _name_of(g.get('scorer') or g.get('player'))
            if not scorer:
                continue
            goals.append({
                'scorer': scorer,
                'assist': _name_of(g.get('assist')),
                'team': g.get('team') or g.get('team_name'),
                'minute': g.get('minute'),
            })
        if goals:
            return goals

    # 후보 2: 'events'/'incidents' 안에 여러 이벤트 타입이 섞여있고, 그중
    # type이 'goal'류인 것만 골라야 하는 경우 (collectors.py EventCollector가
    # 쓰던 것과 같은 스타일 — 실제로 이 형태일 가능성이 더 높아 보인다).
    for key in ('events', 'incidents', 'timeline'):
        items = detail.get(key)
        if not isinstance(items, list) or not items:
            continue
        for e in items:
            if not isinstance(e, dict):
                continue
            et = (e.get('type') or e.get('event_type') or e.get('incident_type') or '').lower()
            if 'goal' not in et:
                continue
            if 'own' in et:  # 자책골은 scorer/assist 개념이 다르니 일단 제외
                continue
            scorer = _name_of(e.get('player') or e.get('scorer'))
            if not scorer:
                continue
            goals.append({
                'scorer': scorer,
                'assist': _name_of(e.get('assist') or e.get('assist_player')),
                'team': e.get('team') or e.get('team_name'),
                'minute': e.get('minute'),
            })
        if goals:
            return goals

    return goals


def _collect_league(client, league_key, league_id, team_ids, fetch_events_fn):
    rows = fetch_events_fn(client, league_id)
    matches = []
    n_finished, n_with_goals = 0, 0
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
        detail, ok = client.event_detail(eid)
        time.sleep(0.2)
        if not (ok and detail):
            continue
        goals = _extract_goals(detail)
        date_kst, _ = _kst_date_time(ev.get('event_date'))
        matches.append({
            'home': home_kr, 'away': away_kr, 'date': date_kst,
            'eid': eid, 'goals': goals,
        })
        if goals:
            n_with_goals += 1
    print(f'[collect_goalscorers] {league_key}: 종료경기 {n_finished}건 중 '
          f'득점정보 확보 {n_with_goals}건', flush=True)
    return matches


def main():
    client = BSDClient()
    if not client.enabled:
        print('[collect_goalscorers] BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    out = {}

    # ---- EPL ----
    league_id, season_id = _find_pl_league_id(client)
    if league_id:
        team_ids = _find_pl_teams(client, league_id, season_id)
        if team_ids:
            out['epl'] = _collect_league(
                client, 'epl', league_id, team_ids,
                lambda c, lid: _fetch_all_league_events(c, lid, set(team_ids.keys())))
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
            lambda c, lid: _fetch_league_events(c, lid))

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    total_matches = sum(len(v) for v in out.values())
    total_with_goals = sum(1 for v in out.values() for m in v if m.get('goals'))
    print(f'[collect_goalscorers] 완료 — 종료경기 {total_matches}건 중 '
          f'득점정보 확보 {total_with_goals}건 → {OUT_PATH} 저장', flush=True)
    if total_matches and not total_with_goals:
        print('[collect_goalscorers] ⚠️ 종료경기는 있는데 득점정보를 하나도 '
              '못 뽑았음 — event_detail() 실제 응답 구조가 _extract_goals()의 '
              '추측과 다른 것으로 보임. 위 [diag] 로그의 sample_keys를 보고 '
              '수정 필요.', flush=True)


if __name__ == '__main__':
    main()
