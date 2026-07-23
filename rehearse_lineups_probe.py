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
    """응답 구조를 사람이 읽을 요약으로."""
    if isinstance(obj, dict):
        keys = sorted(obj.keys())
        return f'dict(keys={keys[:15]})'
    if isinstance(obj, list):
        n = len(obj)
        if n and isinstance(obj[0], dict):
            return f'list[{n}] first_keys={sorted(obj[0].keys())[:15]}'
        return f'list[{n}] {obj[:3]}'
    return repr(obj)[:80]


def _walk_structure(obj, path='', depth=0, out=None, max_depth=4):
    """2026-07-23: 응답 구조를 재귀적으로 훑어 '선수 리스트로 보이는 것'을 전부
    찾는다. 1차 프로브에서 events/{id}/lineups/가 dict(keys=[beta, event_id,
    lineup_status, lineups, unavailable_players, updated_at])를 반환했는데,
    고정된 body['home']['players'] 경로만 찾다가 놓쳤다 → 구조를 가정하지 말고
    전수 탐색으로 바꾼다(추측 금지 원칙)."""
    if out is None:
        out = []
    if depth > max_depth:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_structure(v, f'{path}.{k}' if path else k, depth + 1, out, max_depth)
    elif isinstance(obj, list) and obj:
        first = obj[0]
        if isinstance(first, dict):
            keys = set(first.keys())
            # 선수 항목으로 보이는지: 이름/포지션/등번호/출전 관련 키가 있으면
            player_ish = keys & {'player', 'player_id', 'player_name', 'name',
                                 'position', 'pos', 'shirt_number', 'jersey_number',
                                 'number', 'minutes', 'minutes_played', 'is_starter',
                                 'starting', 'substitute'}
            if player_ish:
                out.append((path, len(obj), sorted(keys)[:14], obj[:2]))
            else:
                _walk_structure(first, f'{path}[0]', depth + 1, out, max_depth)
    return out


def _fmt_player(p):
    """선수 항목 dict에서 이름/포지션/출전분 후보를 뽑아 한 줄로."""
    if not isinstance(p, dict):
        return str(p)[:40]
    nm = p.get('player') or p.get('player_name') or p.get('name')
    if isinstance(nm, dict):
        nm = nm.get('name') or nm.get('short_name') or nm.get('id')
    pos = p.get('position') or p.get('pos') or p.get('specific_position')
    mins = p.get('minutes') or p.get('minutes_played')
    start = p.get('is_starter')
    if start is None:
        start = p.get('starting')
    bits = [str(nm)]
    if pos:
        bits.append(f'pos={pos}')
    if mins is not None:
        bits.append(f'{mins}분')
    if start is not None:
        bits.append('선발' if start else '교체')
    return ' '.join(bits)


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
            except Exception:
                continue
            time.sleep(0.2)
            if raw is None:
                continue
            if isinstance(raw, (dict, list)) and not raw:
                continue
            ok_any = True
            print(f'{LOG} [diag] {path} → {_describe(raw)}', flush=True)
            # dict면 최상위 키별 요약도 남긴다(구조 파악용)
            if isinstance(raw, dict):
                for k, v in list(raw.items())[:10]:
                    print(f'{LOG}     .{k} = {_describe(v)}', flush=True)
            # 2026-07-23: 구조를 가정하지 않고 전수 탐색으로 선수 리스트를 찾는다
            found = _walk_structure(raw)
            if found:
                for (fpath, n, keys, sample) in found[:4]:
                    print(f'{LOG}   ✔ 선수 리스트 발견 경로 "{fpath}" '
                          f'{n}명, keys={keys}', flush=True)
                    print(f'{LOG}     샘플: '
                          f'{[_fmt_player(x) for x in sample]}', flush=True)
                lineup_parsed += 1
                if endpoint_found is None:
                    # 대표 경로는 실제 출전 라인업 쪽을 우선(결장자 목록보다).
                    lineup_paths = [f for f in found
                                    if not any(w in f[0].lower() for w in
                                               ('unavailable', 'injur', 'missing', 'out'))]
                    endpoint_found = (tpl, (lineup_paths or found)[0][0])
            else:
                print(f'{LOG}   (선수 리스트로 보이는 항목 없음 — 구조 위 참조)',
                      flush=True)
            break  # 이 후보는 한 경기에서 확인됐으면 충분
        if not ok_any:
            print(f'{LOG} [diag] {tpl} → 모든 샘플 경기에서 데이터 없음', flush=True)

    # --- 결론 ---
    print(f'{LOG} ===== 라인업 실측 결과 =====', flush=True)
    if endpoint_found and lineup_parsed:
        tpl, inner = endpoint_found
        print(f'{LOG} ✅ BSD 라인업 확보 가능: {tpl} → 내부 경로 "{inner}"에서 '
              f'선수 리스트 파싱 성공({lineup_parsed}개 경로). '
              f'다음 단계: 이 경로로 collect_lineups.py 작성 → baseline '
              f'defending per90 갱신에 연결.', flush=True)
    elif detail_has_lineup:
        print(f'{LOG} ✅ event_detail 자체에 라인업 필드 있음(위 diag 참조).',
              flush=True)
    else:
        print(f'{LOG} ❌ BSD 라인업 미확인. 위 [diag] 구조 로그를 보고 판단 필요. '
              f'대안: SportScore(RAPIDAPI_KEY 필요) 등.', flush=True)


if __name__ == '__main__':
    main()
