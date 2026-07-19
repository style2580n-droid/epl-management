# -*- coding: utf-8 -*-
"""
BSD 기반 이적 감지 (EPL + 6개 리그 전부).

⚠️ 2026-07-15 경위: 기존 TransferDetector(collectors.py)는 football-data.org의
competition_teams().squad로 diff를 뜨는데, 26-27 시즌 스쿼드가 아직 등록
전이라 squad가 항상 빈 배열로 옴(실측 확인: squad길이=0) — 어제 고친
"같은 API 두 번 호출" 버그와는 별개의, 원본 데이터 자체가 없는 문제.
BSD /players/?team_id= 는 실제 선수 데이터가 있는 것으로 이미 검증됐으므로
(collect_fixtures_multileague.py의 스쿼드 수집과 동일 근거), 이적 감지도
BSD 기준 스냅샷 diff로 전환한다.

방식: 매 실행마다 전체 선수(id, current_team_id)를 스냅샷으로 저장해두고,
다음 실행 때 같은 선수 id의 team_id가 바뀌었으면 이적으로 감지한다
(collectors.py의 previous_squads.json과 같은 스냅샷-diff 패턴).

출력:
  data/master/bsd_player_snapshot.json — {선수id: {name, team_id, team_kr,
    league, last_seen}} — 다음 실행 비교 기준(자동 누적, 페치 실패한 팀의
    이전 기록도 보존해서 일시적 오류로 이력이 끊기지 않게 한다)
  data/master/transfers_bsd.json — 감지된 이적 누적 목록 (append-only)

실행: BSD_API_KEY 환경변수 필요. 없으면 조용히 스킵.
"""
import json
import os
import time
from datetime import datetime, timezone

from api_clients import BSDClient
from app_export import to_kr as epl_to_kr
from app_export_multileague import to_kr_league

SNAPSHOT_PATH = 'data/master/bsd_player_snapshot.json'
TRANSFERS_PATH = 'data/master/transfers_bsd.json'
PAGE_LIMIT = 200

# EPL 포함 7개 리그. collect_coaches.py/collect_fixtures_multileague.py와
# 동일한 이름+국가 조합 실측 확인 패턴 (숫자 추측 금지 원칙).
LEAGUE_MATCHERS = {
    'epl': lambda n, c: c == 'england' and 'premier league' in n,
    'laliga': lambda n, c: c == 'spain' and n in (
        'la liga', 'laliga', 'primera division', 'primera división'),
    'bundesliga': lambda n, c: c == 'germany' and n == 'bundesliga',
    'seriea': lambda n, c: c == 'italy' and n == 'serie a',
    'ligue1': lambda n, c: c == 'france' and 'ligue 1' in n,
    'eredivisie': lambda n, c: c in ('netherlands', 'holland') and 'eredivisie' in n,
    'championship': lambda n, c: c == 'england' and n == 'championship',
}


def _unwrap(resp):
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _find_leagues(client):
    found = {}
    offset = 0
    while True:
        data = _unwrap(client.leagues(limit=PAGE_LIMIT, offset=offset))
        if not data:
            break
        results = data.get('results', [])
        for lg in results:
            name = (lg.get('name') or '').lower()
            country = (lg.get('country') or '').lower()
            for lk, matcher in LEAGUE_MATCHERS.items():
                if lk in found:
                    continue
                if matcher(name, country):
                    found[lk] = lg.get('id')
                    print(f'[collect_transfers_bsd] {lk} 발견: '
                          f'"{lg.get("name")}" (id={lg.get("id")})', flush=True)
        total = data.get('count', len(results))
        offset += PAGE_LIMIT
        if offset >= total or not results or len(found) == len(LEAGUE_MATCHERS):
            break
    missing = set(LEAGUE_MATCHERS) - set(found)
    if missing:
        print(f'[collect_transfers_bsd] 못 찾은 리그: {sorted(missing)}', flush=True)
    return found


def _team_kr(name, league_key):
    """EPL은 app_export.to_kr, 6개 리그는 app_export_multileague.to_kr_league
    — 둘 다 이미 실전 검증된 함수를 그대로 재사용한다."""
    if league_key == 'epl':
        kr = epl_to_kr(name)
        return kr if kr else None
    hit = to_kr_league(name)
    if hit and hit[0] == league_key:
        return hit[1]
    return None


def _looks_like_b_team(raw_name):
    """collect_fixtures_multileague의 동명 헬퍼와 동일 기준. 중복 ID가 있을
    때만 대표 ID 선정에 쓴다."""
    import re
    from app_export_multileague import _ascii_fold
    return bool(re.search(
        r'\b(b|ii|iii|u\d{2}|youth|junior|castilla|atletic|femen\w*|'
        r'women|ladies|reserves?)\b', _ascii_fold(raw_name or ''), re.I))


def _find_league_teams(client, league_key, league_id):
    """2026-07-19: 클럽당 BSD ID를 1개(대표)로 축소. 기존엔 중복 ID(예:
    라리가 레알 소시에다드 48/924)를 둘 다 돌면서 같은 선수를 다른 ID
    밑에서 재발견해 가짜 이적 28건을 만들었다(07-19 실행 실측). 스냅샷
    비교도 team_id 기준이라 대표 ID 고정이 근본 해결책이다."""
    candidates = [{'league_id': league_id}, {'league': league_id}]
    for params in candidates:
        data = _unwrap(client.teams(**params))
        if not data:
            continue
        results = data.get('results', [])
        by_kr = {}
        for t in results:
            name = t.get('name') or t.get('short_name')
            kr = _team_kr(name, league_key)
            if kr:
                by_kr.setdefault(kr, []).append((t['id'], name))
        matched = {}
        for kr, lst in by_kr.items():
            chosen = lst[0]
            if len(lst) > 1:
                non_b = [x for x in lst if not _looks_like_b_team(x[1])]
                chosen = (non_b or lst)[0]
                dup_desc = ', '.join(f'{i}:"{n}"' for i, n in lst)
                print(f'[collect_transfers_bsd] [diag] {league_key} "{kr}" '
                      f'중복 {len(lst)}건 [{dup_desc}] → 대표 id={chosen[0]} 사용',
                      flush=True)
            matched[chosen[0]] = kr
        print(f'[collect_transfers_bsd]   {league_key} teams{params} → '
              f'{len(results)}개 중 클럽 {len(matched)}개 매칭', flush=True)
        if len(matched) >= 3:
            return matched
    return {}


def _fetch_team_players_full(client, team_id):
    """어제 실측으로 확정된 team_id 필터 재사용. 전체 필드(id,
    current_team_id 포함)를 유지해서 이적 감지에 쓴다."""
    data = _unwrap(client.players(team_id=team_id, limit=100))
    time.sleep(0.2)
    if not data:
        return []
    rows = data.get('results', [])
    if len(rows) > 60:
        return []  # 필터 안 먹혀서 전체 DB가 온 경우 방어
    return rows


def main():
    client = BSDClient()
    if not client.enabled:
        print('[collect_transfers_bsd] BSD_API_KEY 미등록 → 스킵', flush=True)
        return

    leagues = _find_leagues(client)
    if not leagues:
        print('[collect_transfers_bsd] 리그를 하나도 못 찾음 → 중단', flush=True)
        return

    prev_snapshot = _load_json(SNAPSHOT_PATH, {})
    fresh_snapshot = {}
    transfers = _load_json(TRANSFERS_PATH, [])
    # 2026-07-19: 이미 누적된 가짜 이적(같은 리그·같은 팀 → 같은 팀) 청소.
    # 07-19 실행에서 레알 소시에다드 28건 등이 들어간 상태라, 근본 수정
    # (대표 ID + 같은 클럽 가드)과 별개로 기존 레코드도 걸러야 한다.
    n_before = len(transfers)
    transfers = [t for t in transfers
                 if not (t.get('from_team')
                         and t.get('from_team') == t.get('to_team')
                         and t.get('from_league') == t.get('to_league'))]
    if len(transfers) != n_before:
        print(f'[collect_transfers_bsd] 누적 목록에서 가짜 이적(같은팀→같은팀) '
              f'{n_before - len(transfers)}건 제거', flush=True)
    today = datetime.now(timezone.utc).date().isoformat()

    for league_key, league_id in leagues.items():
        team_ids = _find_league_teams(client, league_key, league_id)
        if not team_ids:
            print(f'[collect_transfers_bsd] {league_key} 팀 매칭 실패 → 스킵',
                  flush=True)
            continue
        league_player_count = 0
        for team_id, team_kr in team_ids.items():
            players = _fetch_team_players_full(client, team_id)
            for p in players:
                pid = str(p.get('id')) if p.get('id') is not None else None
                if not pid:
                    continue
                fresh_snapshot[pid] = {
                    'name': p.get('name'),
                    'team_id': team_id,
                    'team_kr': team_kr,
                    'league': league_key,
                    'last_seen': today,
                }
                league_player_count += 1
                prev = prev_snapshot.get(pid)
                if prev and prev.get('team_id') != team_id:
                    # 2026-07-19: ID가 달라도 같은 리그·같은 클럽이면 이적이
                    # 아니다 — BSD 중복 레코드이거나 BSD가 클럽 ID를 갈아탄
                    # 경우다. 스냅샷만 새 ID로 갱신하고 넘어간다.
                    if (prev.get('team_kr') == team_kr
                            and prev.get('league') == league_key):
                        continue
                    transfers.append({
                        'player_id': pid,
                        'player_name': p.get('name'),
                        'from_team': prev.get('team_kr'),
                        'from_league': prev.get('league'),
                        'to_team': team_kr,
                        'to_league': league_key,
                        'detected_at': _now(),
                    })
                    print(f'[collect_transfers_bsd] 이적 감지: {p.get("name")} '
                          f'{prev.get("team_kr")} → {team_kr}', flush=True)
        print(f'[collect_transfers_bsd] {league_key}: {len(team_ids)}팀, '
              f'선수 {league_player_count}명', flush=True)

    # 이번 실행에서 못 받아온 팀(네트워크 실패 등)의 이전 기록은 보존한다
    # — 안 그러면 일시적 오류로 그 팀 선수 이력이 통째로 사라져서, 다음
    # 실행 때 비교 기준이 없어져 이적 감지를 놓칠 수 있다.
    merged_snapshot = dict(prev_snapshot)
    merged_snapshot.update(fresh_snapshot)

    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, 'w', encoding='utf-8') as f:
        json.dump(merged_snapshot, f, ensure_ascii=False, indent=1)
    with open(TRANSFERS_PATH, 'w', encoding='utf-8') as f:
        json.dump(transfers, f, ensure_ascii=False, indent=1)
    print(f'[collect_transfers_bsd] 완료 — 스냅샷 {len(merged_snapshot)}명, '
          f'누적 이적 {len(transfers)}건', flush=True)


if __name__ == '__main__':
    main()
