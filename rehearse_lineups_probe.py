# -*- coding: utf-8 -*-
"""
[임시/리허설 전용] BSD가 경기 라인업(선발 XI/포지션/출전시간)을 주는지 실측 확정.
2026-07-22 작성.

배경: A단계의 유일한 미완 부분은 선수 baseline의 defending(수비) per90이 월드컵
값에 고정돼 있다는 것. 이걸 시즌 데이터로 갱신하려면 "누가 그 경기에 뛰었나"
= 라인업 데이터가 필요하다(득점자 데이터는 골 넣은 선수만 커버).

득점왕 때 events/{id}/incidents/를 실측으로 찾아낸 것과 동일한 방식으로,
추측 없이 BSD가 라인업을 주는 경로가 있는지 로그로 확정한다. 진행 중인 타 리그
(UEFA 예선 등 실시간 종료 경기)로 프로브하므로 8월 개막을 기다릴 필요 없다.

⚠️ 앱에 아무것도 반영하지 않는다. 순수 실측용. 검증 후 yml에서 실행 줄만 지운다.

알려진 사전 정보(재조사 불필요):
- BSDClient에는 라인업 전용 메서드가 없다(api_clients.py 실측).
- 득점왕 리허설(#74)에서 events/{id}/lineups/ 프로브가 "리스트 없음"이었으나,
  그때는 BSD에 기록이 없는 옛 경기 1건이라 오판 가능성이 있었다(incidents도
  같은 함정이 있었음). 그래서 이번엔 '실제로 방금 끝난' 경기 여러 건으로 확인한다.
- SportScore 클라이언트에 lineups()가 있으나 RAPIDAPI_KEY 필요 → 지금 비활성.
  이 스크립트는 BSD만 확인한다(SportScore는 키가 있어야 별도 검증).

검증 포인트(로그):
  1) event_detail 응답에 라인업/포메이션 관련 필드가 있는가 (sample_keys 전수)
  2) events/{id}/lineups/ 등 하위 경로 후보가 데이터를 주는가
  3) 준다면 선발 선수명/포지션이 실제로 파싱되는가 (샘플 출력)
"""
import time

from api_clients import BSDClient

PAGE_LIMIT = 200
LOG = '[rehearse_lineups]'


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _is_uefa_or_active(name, country):
    """진행 중일 가능성이 높은 대항전/리그를 넓게 잡는다(종료경기 확보용)."""
    n = (name or '').lower()
    if 'championship' in n:  # 잉글랜드 국내리그 오탐 방지
        return False
    return any(k in n for k in (
        'champions league', 'europa league', 'conference league', 'uefa',
        'qualifying', 'qualifier', 'nations league', 'world cup'))


def _find_active_leagues(client):
    found = []
    offset = 0
    while True:
        data = _unwrap(client.leagues(limit=PAGE_LIMIT, offset=offset))
        if not data:
            break
        results = data.get('results', [])
        for lg in results:
            if _is_uefa_or_active(lg.get('name'), lg.get('country')):
                season = lg.get('current_season') or {}
                found.append((lg.get('id'), lg.get('name')))
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results:
            break
    return found


def _finished_events(client, league_id, cap=6):
    for key in ('league_id', 'league'):
        try:
            data = _unwrap(client.events(**{key: league_id, 'limit': PAGE_LIMIT}))
        except TypeError:
            data = _unwrap(client.events(**{key: league_id}))
        if not data:
            continue
        rows = data.get('results', [])
        fin = [e for e in rows if (e.get('status') or '').lower() == 'finished']
        if fin:
            return fin[:cap]
    return []


def _rows_of(resp):
    if isinstance(resp, tuple):
        resp = resp[0]
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ('results', 'lineups', 'lineup', 'players', 'starting',
                  'formations', 'data', 'items', 'home', 'away'):
            if isinstance(resp.get(k), list):
                return resp[k]
        # {'home': {...}, 'away': {...}} 형태도 흔함 → dict면 그대로 반환
        return resp
    return None


# event_detail 응답에서 라인업으로 의심되는 키(전수 조사 대상)
_LINEUP_HINT_KEYS = (
    'lineup', 'lineups', 'formation', 'formations', 'starting', 'starters',
    'home_lineup', 'away_lineup', 'home_formation', 'away_formation',
    'players', 'squad', 'home_players', 'away_players', 'bench',
)

_SUB_CANDIDATES = [
    'events/{eid}/lineups/',
    'events/{eid}/lineup/',
    'events/{eid}/formations/',
    'events/{eid}/players/',
    'events/{eid}/squads/',
]


def _describe(obj, depth=0):
    """응답 구조를 사람이 읽을 요약으로. 선수명/포지션 후보를 뽑아본다."""
    if isinstance(obj, dict):
        keys = sorted(obj.keys())
        return f'dict(keys={keys[:15]})'
    if isinstance(obj, list):
        n = len(obj)
        if n and isinstance(obj[0], dict):
            return f'list[{n}] first_keys={sorted(obj[0].keys())[:15]}'
        return f'list[{n}] {obj[:3]}'
    return repr(obj)[:80]


def main():
    client = BSDClient()
    if not client.enabled:
        print(f'{LOG} BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    leagues = _find_active_leagues(client)
    if not leagues:
        print(f'{LOG} 진행 중 대항전 대회 못 찾음 → 스킵', flush=True)
        return
    print(f'{LOG} 진행 중 대회 {len(leagues)}개 발견', flush=True)

    # 실제 종료경기 수집 (여러 대회에서 최대 8건)
    samples = []
    for league_id, name in leagues:
        for ev in _finished_events(client, league_id, cap=3):
            samples.append((name, ev))
            if len(samples) >= 8:
                break
        if len(samples) >= 8:
            break
    if not samples:
        print(f'{LOG} 프로브할 종료경기 없음(예선 라운드 공백일 수 있음) → '
              f'다른 경기일에 재실행', flush=True)
        return
    print(f'{LOG} 프로브 대상 종료경기 {len(samples)}건 확보', flush=True)

    # --- (1) event_detail에 라인업 필드가 있는가 ---
    detail_has_lineup = False
    for name, ev in samples[:3]:
        eid = ev.get('id')
        if eid is None:
            continue
        det = _unwrap(client.event_detail(eid))
        time.sleep(0.25)
        if not isinstance(det, dict):
            continue
        keys = sorted(det.keys())
        hits = [k for k in keys if any(h in k.lower() for h in
                ('lineup', 'formation', 'starting', 'squad'))]
        print(f'{LOG} [diag] event_detail eid={eid} sample_keys={keys}', flush=True)
        if hits:
            detail_has_lineup = True
            for h in hits:
                print(f'{LOG}   → 라인업 후보 필드 "{h}": {_describe(det[h])}',
                      flush=True)

    # --- (2)(3) 하위 경로 후보 프로브 ---
    endpoint_found = None
    lineup_parsed = 0
    for tpl in _SUB_CANDIDATES:
        ok_any = False
        for name, ev in samples:
            eid = ev.get('id')
            if eid is None:
                continue
            path = tpl.format(eid=eid)
            try:
                raw = _unwrap(client.get(path))
            except Exception as exc:
                continue
            time.sleep(0.2)
            if raw is None:
                continue
            body = _rows_of(raw)
            # 비었으면 다음 경기로 (옛 경기 함정 회피 — 여러 건 시도)
            if not body or (isinstance(body, list) and not body):
                continue
            ok_any = True
            print(f'{LOG} [diag] {path} → {_describe(body)}', flush=True)
            # 선수명/포지션 뽑아보기
            def _extract_players(b):
                out = []
                seq = b if isinstance(b, list) else (
                    (b.get('home', {}).get('players', []) if isinstance(b, dict) else []) +
                    (b.get('away', {}).get('players', []) if isinstance(b, dict) else []))
                for p in (seq or [])[:6]:
                    if isinstance(p, dict):
                        nm = (p.get('player') or p.get('name') or
                              (p.get('player') or {}).get('name')
                              if isinstance(p.get('player'), dict) else p.get('name'))
                        pos = p.get('position') or p.get('pos')
                        out.append(f'{nm}({pos})' if pos else str(nm))
                return out
            players = _extract_players(body)
            if players:
                lineup_parsed += 1
                print(f'{LOG}   선수 샘플: {players}', flush=True)
            if endpoint_found is None:
                endpoint_found = tpl
            break  # 이 후보는 한 경기에서 확인됐으면 충분
        if not ok_any:
            print(f'{LOG} [diag] {tpl} → 모든 샘플 경기에서 데이터 없음', flush=True)

    # --- 결론 ---
    print(f'{LOG} ===== 라인업 실측 결과 =====', flush=True)
    if endpoint_found and lineup_parsed:
        print(f'{LOG} ✅ BSD 라인업 확보 가능: {endpoint_found} 에서 선발 선수 '
              f'파싱 성공({lineup_parsed}경기). → defending 갱신용 라인업 수집기 '
              f'작성 가능. 다음 단계: 이 경로로 collect_lineups.py 작성 후 '
              f'baseline defending per90 갱신에 연결.', flush=True)
    elif detail_has_lineup:
        print(f'{LOG} ✅ event_detail 자체에 라인업 필드 있음(위 diag 참조). '
              f'하위 경로 대신 event_detail에서 뽑는 방식으로 수집기 작성.',
              flush=True)
    else:
        print(f'{LOG} ❌ BSD는 라인업을 주지 않음(모든 경로/필드에서 미확인). '
              f'→ A단계 defending 갱신은 BSD로 불가. 대안: SportScore(RAPIDAPI_KEY '
              f'필요) 또는 TheSportsDB/FPL의 라인업. 사용자와 소스 결정 필요.',
              flush=True)


if __name__ == '__main__':
    main()
