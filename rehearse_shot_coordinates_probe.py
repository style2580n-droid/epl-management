# -*- coding: utf-8 -*-
"""
rehearse_shot_coordinates_probe.py

목적: events/{eid}/incidents/ 응답에 슈팅(shot) 단위 좌표(x, y)나 그에 준하는
위치정보(zone 등)가 있는지 실측 확인한다. compute_advanced_stats.py(고급지표 직접
계산 엔진)가 이 데이터에 의존하는데, collect_xg_bsd.py/collect_xg_fbref.py는 둘 다
"경기당 합계 xG"만 주지 슈팅별 데이터를 안 줘서 — 이 확인이 먼저 필요함.

기존 rehearse_goal_team_probe.py / rehearse_mls_norway_xg_probe.py와 같은 패턴
(BSD_API_KEY, sports.bzzoiro.com) 그대로 사용.

실행: python3 rehearse_shot_coordinates_probe.py
      (data/football.db가 있는 파이프라인 서버/로컬 환경에서 실행 — 최근 종료경기
       하나를 자동으로 찾아서 그 경기의 incidents를 통째로 출력함)
"""
import json
import os
import sqlite3

import requests

API_TOKEN = os.getenv('BSD_API_KEY', '')
BASE_URL = 'https://sports.bzzoiro.com/api'
DB_PATH = 'data/football.db'


def get_headers():
    return {'Authorization': f'Token {API_TOKEN}'}


def find_recent_finished_event_id():
    """DB에 이미 있는 종료경기 중 하나의 BSD event id를 찾는다.
    matches 테이블에 event_id 비슷한 컬럼이 있다고 가정 — 없으면 컬럼 목록을
    출력해서 실제 이름을 알 수 있게 함(추측 대신 확인)."""
    if not os.path.exists(DB_PATH):
        print(f'[probe] {DB_PATH} 없음 — 이 환경엔 DB가 없는 게 정상(로컬 테스트용).')
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute('PRAGMA table_info(matches)')]
    print(f'[probe] matches 테이블 컬럼: {cols}')
    id_col = None
    for cand in ('event_id', 'bsd_event_id', 'external_id', 'id'):
        if cand in cols:
            id_col = cand
            break
    if not id_col:
        print('[probe] event id로 쓸만한 컬럼을 못 찾음 — 위 컬럼 목록 보고 수동으로 지정 필요')
        conn.close()
        return None
    row = conn.execute(
        f"SELECT {id_col} as eid FROM matches WHERE status='FINISHED' "
        f"AND home_goals IS NOT NULL ORDER BY date DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        print('[probe] 종료경기를 DB에서 못 찾음')
        return None
    print(f'[probe] 테스트용 경기 event_id={row["eid"]} 선택됨 (컬럼: {id_col})')
    return row['eid']


def probe_incidents(eid):
    url = f'{BASE_URL}/events/{eid}/incidents/'
    print(f'[probe] 요청: {url}')
    r = requests.get(url, headers=get_headers(), timeout=30)
    print(f'[probe] HTTP {r.status_code}')
    if r.status_code != 200:
        print(f'[probe] 응답 본문(에러 확인용): {r.text[:500]}')
        return
    data = r.json()
    items = data if isinstance(data, list) else data.get('results', data.get('incidents', []))
    print(f'[probe] 이벤트(인시던트) 개수: {len(items)}')

    # 슈팅으로 보이는 이벤트 타입만 골라서 전체 필드를 그대로 출력(추측 없이 원문 그대로)
    shot_like = [it for it in items if any(
        k in json.dumps(it, ensure_ascii=False).lower()
        for k in ('shot', 'goal', 'save', 'miss', 'block'))]
    print(f'[probe] 슈팅으로 보이는 이벤트 {len(shot_like)}건 (타입 키워드 매칭 기준)')
    print('[probe] 처음 3건 원문 그대로 출력:')
    for it in shot_like[:3]:
        print(json.dumps(it, ensure_ascii=False, indent=2))
        print('---')

    if not shot_like:
        print('[probe] 슈팅 이벤트를 못 찾음 — 전체 이벤트 중 처음 3건 대신 출력:')
        for it in items[:3]:
            print(json.dumps(it, ensure_ascii=False, indent=2))
            print('---')

    # x/y 비슷한 키가 있는지 전체 아이템 통틀어 스캔
    coord_keys_found = set()
    for it in items:
        for k in it.keys():
            if k.lower() in ('x', 'y', 'pos_x', 'pos_y', 'location', 'coordinates',
                              'zone', 'field_zone', 'x_coord', 'y_coord'):
                coord_keys_found.add(k)
    print(f'[probe] 좌표/위치 관련으로 보이는 키 발견: {coord_keys_found or "없음"}')


def main():
    eid = find_recent_finished_event_id()
    if eid is None:
        print('[probe] event_id를 못 구해서 중단. DB 컬럼 확인 후 eid를 직접 넣어서 재실행 가능:')
        print('        probe_incidents(12345)  # 이런 식으로 직접 호출')
        return
    probe_incidents(eid)


if __name__ == '__main__':
    main()
