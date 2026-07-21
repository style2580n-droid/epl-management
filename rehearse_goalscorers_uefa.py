# -*- coding: utf-8 -*-
"""
[임시/리허설 전용] UEFA 예선 경기로 득점자 수집 배관을 실전 검증한다.
2026-07-21 작성 — 8월 리그 개막을 기다리지 않고, 지금 진행 중인 챔스/
유로파/컨퍼런스 예선(실시간 종료 경기)으로 "경기 종료 → incidents 조회
→ 득점자 파싱"이 실제로 도는지 확인하기 위한 것.

⚠️ 이 스크립트는 앱에 아무것도 반영하지 않는다. reports/*.js 도, 리더보드도
건드리지 않는다. 오직 로그로 "득점자가 실제로 뽑히는가"만 출력한다.
검증이 끝나면 football_pipeline.yml에서 이 스크립트 실행 줄만 지우면 된다.

기존 collect_goalscorers.py는 전혀 수정하지 않는다 — 그 파일에서 검증된
헬퍼(_goals_from_items 상당의 파싱)를 여기서 자체 구현해 독립적으로 돈다.
BSD가 예선 경기의 팀을 우리 7개 리그 매핑에 못 맞춰도 상관없다: 팀 한글명
없이 원문 그대로 찍어서, 득점자 데이터 유무만 본다.

검증 포인트(로그로 확인):
  1) BSD가 예선 대회를 리그로 잡는가 → 발견된 대회명/국가/id 출력
  2) 그 대회에 status=finished 경기가 있는가 → 종료경기 수
  3) events/{id}/incidents/ 에서 득점자가 실제로 나오는가 → 득점자 샘플
"""
import time

from api_clients import BSDClient

PAGE_LIMIT = 200
LOG = '[rehearse_uefa]'


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


# BSD가 예선 대회를 뭐라고 부르는지 확실치 않으므로 넓게 매칭한다.
# (UEFA 대항전은 country가 없거나 'europe'/'international'로 올 가능성이 높다.)
def _is_uefa_qualifier(name, country):
    n = (name or '').lower()
    hit_comp = any(k in n for k in (
        'champions league', 'europa league', 'conference league',
        'uefa', 'qualifying', 'qualifier', 'qual.'))
    # 국내리그의 "Championship"이 'champions'에 안 걸리도록 방어
    if 'championship' in n:
        return False
    return hit_comp


def _find_uefa_leagues(client):
    """BSD leagues 목록에서 UEFA 예선/대항전으로 보이는 대회를 전부 찾는다."""
    found = []  # (id, season_id, name, country)
    offset = 0
    while True:
        data = _unwrap(client.leagues(limit=PAGE_LIMIT, offset=offset))
        if not data:
            break
        results = data.get('results', [])
        for lg in results:
            name = lg.get('name') or ''
            country = lg.get('country') or ''
            if _is_uefa_qualifier(name, country):
                season = lg.get('current_season') or {}
                found.append((lg.get('id'), season.get('id'), name, country))
                print(f'{LOG} 대회 발견: "{name}" (country={country!r}, '
                      f'id={lg.get("id")}, season_id={season.get("id")})',
                      flush=True)
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results:
            break
    if not found:
        print(f'{LOG} ⚠️ UEFA 예선/대항전으로 매칭된 대회가 없음 — BSD가 이들을 '
              f'리그로 노출하지 않거나 다른 이름을 쓰는 것. leagues 응답의 '
              f'대회명을 직접 확인해야 함.', flush=True)
    return found


def _fetch_events(client, league_id):
    """해당 대회의 이벤트를 리그 필터로 받는다 (collect_fixtures_multileague의
    확정 파라미터 'league_id'를 우선 시도, 실패 시 'league')."""
    for key in ('league_id', 'league'):
        rows = []
        offset = 0
        while True:
            try:
                data = _unwrap(client.events(**{key: league_id,
                                                'limit': PAGE_LIMIT,
                                                'offset': offset}))
            except TypeError:
                data = _unwrap(client.events(**{key: league_id}))
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
        if rows:
            return rows, key
    return [], None


def _rows_of(resp):
    if isinstance(resp, tuple):
        resp = resp[0]
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ('results', 'incidents', 'events', 'goals', 'timeline',
                  'data', 'items'):
            if isinstance(resp.get(k), list):
                return resp[k]
    return None


def _name_of(val):
    if isinstance(val, dict):
        return val.get('name') or val.get('player_name') or val.get('full_name')
    if isinstance(val, str):
        return val or None
    return None


def _goals_from_incidents(rows):
    """collect_goalscorers._goals_from_items와 동일 기준(실전 검증됨)."""
    goals = []
    for e in rows or []:
        if not isinstance(e, dict):
            continue
        et = (e.get('type') or e.get('event_type')
              or e.get('incident_type') or '').lower()
        if 'goal' not in et or 'own' in et:
            continue
        scorer = _name_of(e.get('player') or e.get('scorer'))
        if not scorer:
            continue
        goals.append({
            'scorer': scorer,
            'assist': _name_of(e.get('assist') or e.get('assist_player')),
            'minute': e.get('minute'),
        })
    return goals


def main():
    client = BSDClient()
    if not client.enabled:
        print(f'{LOG} BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    leagues = _find_uefa_leagues(client)
    if not leagues:
        return

    grand_finished = grand_probed = grand_with_goals = 0
    confirmed_tpl = 'events/{eid}/incidents/'

    for league_id, _season_id, name, _country in leagues:
        rows, param = _fetch_events(client, league_id)
        finished = [e for e in rows
                    if (e.get('status') or '').lower() == 'finished']
        print(f'{LOG} "{name}": events{{{param}}} → 전체 {len(rows)}건, '
              f'종료 {len(finished)}건', flush=True)
        if not finished:
            continue

        # 종료 경기 중 최대 8건만 프로브 (리허설이라 레이트리밋 절약)
        n_goals_here = 0
        for ev in finished[:8]:
            eid = ev.get('id')
            if eid is None:
                continue
            grand_probed += 1
            hs, as_ = ev.get('home_score'), ev.get('away_score')
            home = ev.get('home_team') or ev.get('home_team_id')
            away = ev.get('away_team') or ev.get('away_team_id')
            try:
                rows_inc = _rows_of(client.get(confirmed_tpl.format(eid=eid)))
            except Exception as exc:
                print(f'{LOG}   [diag] eid={eid} incidents 예외 '
                      f'{type(exc).__name__}', flush=True)
                rows_inc = None
            time.sleep(0.25)
            if rows_inc is None:
                print(f'{LOG}   [diag] eid={eid} incidents 리스트 없음 '
                      f'(score {hs}-{as_})', flush=True)
                continue
            goals = _goals_from_incidents(rows_inc)
            grand_with_goals += 1 if goals else 0
            n_goals_here += len(goals)
            sample = ', '.join(
                f'{g["scorer"]}' + (f"({g['minute']}')" if g.get('minute') else '')
                for g in goals[:5])
            print(f'{LOG}   {home} {hs}-{as_} {away} → incidents '
                  f'{len(rows_inc)}건, 골 파싱 {len(goals)}건'
                  + (f' [{sample}]' if goals else ''), flush=True)
        grand_finished += len(finished)
        print(f'{LOG} "{name}" 소계: 프로브한 경기에서 골 {n_goals_here}건',
              flush=True)

    print(f'{LOG} ===== 리허설 결과 =====', flush=True)
    print(f'{LOG} 대회 {len(leagues)}개, 종료경기 {grand_finished}건, '
          f'프로브 {grand_probed}건, 골 확보된 경기 {grand_with_goals}건',
          flush=True)
    if grand_probed and grand_with_goals:
        print(f'{LOG} ✅ 실시간 종료 경기에서 득점자 추출 성공 — 8월 리그 '
              f'개막 시 득점왕 배관이 실전에서 작동함이 검증됨.', flush=True)
    elif grand_probed and not grand_with_goals:
        print(f'{LOG} ⚠️ 종료경기는 프로브했으나 득점자 0건 — 예선 경기의 '
              f'incidents 스키마가 리그와 다르거나, 이 대회엔 incidents가 아직 '
              f'안 채워진 것. 위 [diag] 확인 필요.', flush=True)
    else:
        print(f'{LOG} ℹ️ 프로브할 종료경기가 없었음 — 예선 일정 사이 공백일 수 '
              f'있음(다음 라운드 경기일에 재실행).', flush=True)


if __name__ == '__main__':
    main()
