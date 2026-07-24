# -*- coding: utf-8 -*-
"""
골 이벤트(incidents/) 원문 필드 확인용 프로브 (2026-07-24).

목적: collect_goalscorers.py는 증분 캐싱 때문에 이미 처리된 경기를 건너뛰어서,
team 필드 진단 로그(_goals_from_items 안에 심어둔 것)가 찍히기까지 새 경기가
쌓일 때까지 기다려야 한다. 이 프로브는 캐시를 무시하고 confirmed 경로
(events/{eid}/incidents/)를 MLS 종료경기 1건에 직접 호출해서 원문 구조를
바로 확인한다. 프로덕션 파일은 아무것도 안 건드림(순수 진단).
"""
import time

from api_clients import BSDClient
from collect_fixtures_multileague import _find_leagues, _fetch_league_events

LOG = '[rehearse_goal_team]'
_CONFIRMED_TPL = 'events/{eid}/incidents/'  # collect_goalscorers.py와 동일


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def main():
    client = BSDClient()
    if not client.enabled:
        print(f'{LOG} BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    leagues = _find_leagues(client)
    mls = leagues.get('mls')
    if not mls:
        print(f'{LOG} mls 리그를 못 찾음 → 중단', flush=True)
        return
    league_id, season_id, _ = mls

    rows = _fetch_league_events(client, league_id)
    finished = [ev for ev in rows if (ev.get('status') or '').lower() == 'finished']
    print(f'{LOG} mls 종료경기 {len(finished)}건 중 앞에서부터 최대 3건 직접 조회',
          flush=True)

    checked = 0
    for ev in finished:
        if checked >= 3:
            break
        eid = ev.get('id')
        if eid is None:
            continue
        try:
            resp = client.get(_CONFIRMED_TPL.format(eid=eid))
        except Exception as exc:
            print(f'{LOG} eid={eid} 요청 실패: {exc}', flush=True)
            continue
        time.sleep(0.2)
        data = _unwrap(resp)
        rows2 = None
        if isinstance(data, list):
            rows2 = data
        elif isinstance(data, dict):
            for k in ('results', 'incidents', 'events', 'goals', 'timeline', 'data', 'items'):
                if isinstance(data.get(k), list):
                    rows2 = data[k]
                    break
        if rows2 is None:
            print(f'{LOG} eid={eid} → 리스트를 못 찾음, 응답 타입={type(data)}',
                  flush=True)
            checked += 1
            continue
        print(f'{LOG} eid={eid} → 이벤트 {len(rows2)}건', flush=True)
        for item in rows2:
            if not isinstance(item, dict):
                continue
            et = (item.get('type') or item.get('event_type')
                  or item.get('incident_type') or '').lower()
            if 'goal' not in et:
                continue
            print(f'{LOG} eid={eid} 골 아이템 sample_keys='
                  f'{sorted(item.keys())} 전체값={item}', flush=True)
        checked += 1


if __name__ == '__main__':
    main()
