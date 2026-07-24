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
_item_diag_done = False

# 2026-07-20 실측 확정: 득점자 엔드포인트는 events/{eid}/incidents/ 다.
# 근거(#68~ 실행 로그): 후보 중 유일하게 404가 아니었고(빈 경기는 빈 리스트
# 응답), 실제로 EPL 종료경기 722건 중 251건에서 득점자 추출에 성공했다.
# 리그 단위 topscorers 류는 전부 404 → BSD에 없음 확정.
# 이전 프로브 설계의 허점: 첫 실패 경기 1건으로만 판정해서, 그 경기가
# BSD에 기록이 없는 옛 경기면(160551처럼 빈 리스트) 경로를 못 찾은 걸로
# 오판했다 → 이제 확정 경로를 항상 쓰고, 프로브는 확정 경로가 404를
# 내기 시작할 때(경로 소실)만 비상용으로 돌린다.
_CONFIRMED_TPL = 'events/{eid}/incidents/'
_SUB = {'checked': False, 'tpl': _CONFIRMED_TPL}
_EVENT_SUB_CANDIDATES = [
    'events/{eid}/incidents/',
    'events/{eid}/events/',
    'events/{eid}/goals/',
    'events/{eid}/timeline/',
    'events/{eid}/summary/',
    'events/{eid}/statistics/',
    'events/{eid}/lineups/',
]
_LEAGUE_SUB_CANDIDATES = [
    'leagues/{lid}/topscorers/',
    'leagues/{lid}/top_scorers/',
    'leagues/{lid}/scorers/',
]


def _rows_of(resp):
    """프로브 응답에서 리스트를 뽑는다. {'results': [...]}, 다른 키 밑,
    또는 바로 리스트인 경우 전부 처리."""
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


def _goals_from_items(items):
    """이벤트 항목 리스트에서 골만 뽑는다 (_extract_goals 후보 2와 동일 기준)."""
    global _item_diag_done
    goals = []
    for e in items or []:
        if not isinstance(e, dict):
            continue
        et = (e.get('type') or e.get('event_type') or e.get('incident_type')
              or '').lower()
        if 'goal' not in et or 'own' in et:
            continue
        # 2026-07-24: MLS/엘리테세리엔 골 기록에서 team 필드가 항상 None으로
        # 나온 원인 진단용 — 이 함수가 실제로 쓰이는 경로(events/{eid}/incidents/,
        # event_detail엔 애초에 events/incidents/timeline 필드가 없음이 실측
        # 확정돼 있어 후보 2는 그쪽에서 실행 안 됨)라 여기에 넣는다.
        if not _item_diag_done:
            _item_diag_done = True
            print(f'[collect_goalscorers] [diag] 골 이벤트(incidents/) 원문 '
                  f'아이템 sample_keys={sorted(e.keys())} 전체값={e}', flush=True)
        scorer = _name_of(e.get('player') or e.get('scorer'))
        if not scorer:
            continue
        goals.append({
            'scorer': scorer,
            'assist': _name_of(e.get('assist') or e.get('assist_player')),
            'team': e.get('team') or e.get('team_name'),
            # 2026-07-24 실측 확정(events/{eid}/incidents/): team/team_name
            # 필드는 이 응답에 아예 없고, 대신 is_home(bool)으로 홈/원정 중
            # 어느 쪽 골인지 알려준다. 그동안 team이 항상 None이었던 원인.
            'is_home': e.get('is_home'),
            'minute': e.get('minute'),
        })
    return goals


def _probe_sub_endpoints(client, eid, league_id):
    """종료경기 1건으로 하위 경로 후보를 전부 찔러보고, 골이 실제로 나오는
    이벤트 하위 경로를 찾으면 템플릿을 반환한다. 결과는 성공/실패 불문
    전부 [diag]로 남긴다."""
    found_tpl = None
    for tpl in _EVENT_SUB_CANDIDATES:
        path = tpl.format(eid=eid)
        try:
            rows = _rows_of(client.get(path))
        except Exception as exc:
            print(f'[collect_goalscorers] [diag] probe {path} → 예외 '
                  f'{type(exc).__name__}', flush=True)
            time.sleep(0.25)
            continue
        time.sleep(0.25)
        if rows is None:
            print(f'[collect_goalscorers] [diag] probe {path} → 리스트 없음',
                  flush=True)
            continue
        sample = sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else rows[:1]
        goals = _goals_from_items(rows)
        print(f'[collect_goalscorers] [diag] probe {path} → {len(rows)}건, '
              f'골 파싱 {len(goals)}건, sample={sample}', flush=True)
        if goals and found_tpl is None:
            found_tpl = tpl
    # 리그 단위 득점왕 엔드포인트는 채택은 안 하고(출력 스키마가 경기 단위라)
    # 존재 여부만 기록 — 있으면 다음 세션에서 통째 전환을 검토할 근거가 된다.
    for tpl in _LEAGUE_SUB_CANDIDATES:
        path = tpl.format(lid=league_id)
        try:
            rows = _rows_of(client.get(path))
        except Exception as exc:
            print(f'[collect_goalscorers] [diag] probe {path} → 예외 '
                  f'{type(exc).__name__}', flush=True)
            time.sleep(0.25)
            continue
        time.sleep(0.25)
        if rows is None:
            print(f'[collect_goalscorers] [diag] probe {path} → 리스트 없음',
                  flush=True)
        else:
            sample = sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else rows[:1]
            print(f'[collect_goalscorers] [diag] probe {path} → {len(rows)}건, '
                  f'sample={sample}', flush=True)
    if found_tpl:
        print(f'[collect_goalscorers] 득점 하위 엔드포인트 확정: {found_tpl}',
              flush=True)
    else:
        print('[collect_goalscorers] ⚠️ 하위 경로 후보 전부에서 골 데이터 못 '
              '찾음 — 위 probe [diag]들을 보고 다음 후보를 정할 것', flush=True)
    return found_tpl


def _goals_via_sub(client, eid, tpl):
    """확정 하위 경로에서 골을 뽑는다. 반환: (goals, endpoint_ok).
    endpoint_ok=False는 경로 자체가 죽은 것(404/예외), True인데 goals가
    비면 그 경기에 BSD 기록이 없는 것(옛 경기 등)."""
    try:
        rows = _rows_of(client.get(tpl.format(eid=eid)))
    except Exception:
        rows = None
    time.sleep(0.2)
    if rows is None:
        return [], False
    return _goals_from_items(rows), True


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
                'is_home': g.get('is_home'),  # 2026-07-24: 일관성 유지
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
                'is_home': e.get('is_home'),  # 2026-07-24: 위 함수와 일관성 유지
                'minute': e.get('minute'),
            })
        if goals:
            return goals

    return goals


def _score_total(ev):
    """이벤트 행의 총 득점. 필드 없으면 None (0-0 확정 판정 불가)."""
    hs, as_ = ev.get('home_score'), ev.get('away_score')
    if isinstance(hs, int) and isinstance(as_, int):
        return hs + as_
    return None


def _collect_league(client, league_key, league_id, team_ids, fetch_events_fn,
                    prev_by_eid=None):
    rows = fetch_events_fn(client, league_id)
    prev_by_eid = prev_by_eid or {}
    matches = []
    n_finished, n_with_goals, n_reused = 0, 0, 0
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

        # 2026-07-19 증분 수집: 골 확보 경기, 0-0 확정 경기, incidents까지
        # 확인했는데 BSD에 기록이 없던 경기(checked)는 재호출하지 않는다.
        prev_m = prev_by_eid.get(eid)
        if prev_m and (prev_m.get('goals') or prev_m.get('nil')
                       or prev_m.get('checked')):
            matches.append(prev_m)
            n_reused += 1
            if prev_m.get('goals'):
                n_with_goals += 1
            continue

        total = _score_total(ev)
        goals = []
        checked = False
        if total == 0:
            pass  # 0-0 — 득점자 조회 자체가 불필요
        else:
            detail, ok = client.event_detail(eid)
            time.sleep(0.2)
            if ok and detail:
                goals = _extract_goals(detail)
            if not goals:
                # 2026-07-20 실측 확정 경로(incidents/)를 항상 사용
                goals, ep_ok = _goals_via_sub(client, eid, _SUB['tpl'])
                if ep_ok and not goals:
                    checked = True  # 경로 정상, 이 경기 기록이 BSD에 없음
                elif not ep_ok and not _SUB['checked']:
                    # 확정 경로가 죽었을 때만 비상 프로브 1회
                    _SUB['checked'] = True
                    found = _probe_sub_endpoints(client, eid, league_id)
                    if found:
                        _SUB['tpl'] = found
                        goals, ep_ok = _goals_via_sub(client, eid, _SUB['tpl'])
                        if ep_ok and not goals:
                            checked = True
        date_kst, _ = _kst_date_time(ev.get('event_date'))
        m = {
            'home': home_kr, 'away': away_kr, 'date': date_kst,
            'eid': eid, 'goals': goals,
        }
        if total == 0:
            m['nil'] = True
        if checked:
            m['checked'] = True
        matches.append(m)
        if goals:
            n_with_goals += 1
    print(f'[collect_goalscorers] {league_key}: 종료경기 {n_finished}건 중 '
          f'득점정보 확보 {n_with_goals}건 (기존 재사용 {n_reused}건)', flush=True)
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
        print('[collect_goalscorers] BSD_API_KEY 미등록 → 스킵', flush=True)
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
    total_with_goals = sum(1 for v in out.values() for m in v if m.get('goals'))
    print(f'[collect_goalscorers] 완료 — 종료경기 {total_matches}건 중 '
          f'득점정보 확보 {total_with_goals}건 → {OUT_PATH} 저장', flush=True)
    if total_matches and not total_with_goals:
        if _SUB['checked'] and not _SUB['tpl']:
            print('[collect_goalscorers] ⚠️ event_detail에도, 프로브한 하위 '
                  '경로 후보 어디에도 득점자 데이터가 없음 — 위 probe [diag] '
                  '응답들을 보고 다음 후보를 정하거나 다른 소스(예: '
                  'live_events 축적)를 검토할 것.', flush=True)
        else:
            print('[collect_goalscorers] ⚠️ 종료경기는 있는데 득점정보를 하나도 '
                  '못 뽑았음 — [diag] 로그를 보고 수정 필요.', flush=True)


if __name__ == '__main__':
    main()
