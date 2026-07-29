# -*- coding: utf-8 -*-
"""
MLS 공식(Opta 기반) 도움/득점 순위 수집 — 2026-07-26 착수, 2026-07-29 3차 개정.

배경: collect_goalscorers.py가 쓰는 BSD event incidents 스키마는 골당
assist 필드 1명만 준다 — MLS 고유의 "세컨더리 어시스트"(도움으로 이어진
패스의 직전 패스도 도움 인정) 규정이 반영이 안 돼서, MLS 도움왕만 실제
mlssoccer.com 공식 집계보다 적게 나온다(손흥민 실사례로 확인: 공식 10개인데
파이프라인 집계 6개). 다른 5개 리그는 이 문제가 없다(세컨더리 어시스트
규정 자체가 MLS 전용이라).

⚠️ 시행착오 이력(다음 세션이 또 같은 삽질 안 하도록 기록):
  1차(2026-07-26): stats-api.mlssoccer.com, competition_opta_id=98,
    "players/seasons" 등 3개 경로 → 전부 404. 완전히 틀린 시도였음.
  2차(2026-07-29): 사용자가 모바일로 mlssoccer.com 페이지 소스를 직접 열어서
    (view-page-source.com 경유) 실제 API 서버 3개(sportapi/dapi v1/v2)와
    파라미터 이름(competitionSportecId="MLS-COM-000001", season, statViewType)
    을 확인 → 이 조합으로 12개 경로 시도 → 전부 404. 서버는 살아있는데
    ("Distribution API" 배너 응답) 내가 추측한 경로가 다 틀렸던 것.
  3차(2026-07-29, 이번 버전): 웹검색으로 실제 동작 확인된 예시 URL이 있는
    공개 gist(GitHub: akeaswaran/mls-json-api)를 찾음 — sportapi.mlssoccer.com은
    "Sportec" 문자열 ID가 아니라 **예전 숫자 Opta ID**(competition=98)를 쓰는
    별개 시스템이었다. "리그 전체 도움 순위"를 한 번에 주는 엔드포인트를
    찾으려던 게 애초에 잘못된 접근이었을 수 있어서, 전략을 바꿈:
      (a) stats-api.mlssoccer.com/v1/clubs 로 MLS 30개 클럽의 club_opta_id를
          받는다(gist에 있는 문서화된 실제 예시 패턴, season만 2026로 교체).
      (b) sportapi.mlssoccer.com/api/players/byClub/{clubId}?culture=en-us 로
          클럽별 로스터+시즌 스탯을 받는다(마찬가지로 문서화된 패턴).
      (c) 30개 클럽 응답을 모아서 선수별 도움/득점을 직접 집계한다.
    이번에도 실제 필드명(선수 객체 안에 assist가 어떻게 들어있는지)은 실행
    결과로만 검증 가능 — [diag] 로그로 확인할 것.

⚠️ (a)가 실패하면 (b)는 시도하지 않는다(클럽 ID를 못 구하면 로스터를 못
불러오므로). (a)가 성공하고 (b) 첫 번째 클럽 응답의 필드명이 예상과 다르면,
_extract_player_stats()의 candidates만 실제 필드명으로 고치면 된다 —
구조 자체는 재사용 가능할 것으로 예상.

출력: data/master/mls_official_stats.json
  { "assists": {"선수명(영문)": 개수, ...}, "goals": {...},
    "_diag": {...} }  ← build_leaderboard()에 아직 자동 병합 안 함
  (필드 매핑 확정 전까지는 수동 대조 단계).
"""
import json
import os
import time
import urllib.error
import urllib.request

OUT_PATH = 'data/master/mls_official_stats.json'
COMPETITION_OPTA_ID = 98  # gist 문서화 예시 그대로(2026-07-29 기준 여전히 유효한지는 실행 결과로 확인)
SEASON_OPTA_ID = 2026


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; stats-research/1.0)',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', errors='replace')
    except Exception as exc:  # noqa: BLE001 — 진단 단계라 광범위 캐치 후 로그
        return None, f'{type(exc).__name__}: {exc}'


def _rows_of(parsed):
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in ('data', 'results', 'items', 'clubs'):
            if isinstance(parsed.get(k), list):
                return parsed[k]
    return None


def _extract_club_id(club_row):
    for k in ('club_opta_id', 'optaId', 'opta_id', 'id', 'clubId'):
        v = club_row.get(k)
        if v is not None:
            return v
    return None


def _extract_club_name(club_row):
    for k in ('club_name', 'name', 'clubName', 'shortName'):
        v = club_row.get(k)
        if isinstance(v, str):
            return v
    return '?'


def _extract_player_stats(player_row):
    """선수 하나(dict)에서 이름/도움/득점을 뽑는다. 실제 응답 구조를
    모르는 상태라 폭넓게(중첩 1단계까지) 후보 필드명을 탐색한다."""
    name = None
    for k in ('name', 'full_name', 'fullName', 'playerName', 'player_name', 'shortName'):
        v = player_row.get(k)
        if isinstance(v, str) and v:
            name = v
            break
    if not name:
        p = player_row.get('player')
        if isinstance(p, dict):
            name = p.get('name') or p.get('fullName')

    def find_stat(keys):
        for k in keys:
            v = player_row.get(k)
            if isinstance(v, (int, float)):
                return v
        for container_key in ('statistics', 'stats', 'seasonStats'):
            c = player_row.get(container_key)
            if isinstance(c, dict):
                for k in keys:
                    v = c.get(k)
                    if isinstance(v, (int, float)):
                        return v
        return None

    assists = find_stat(('assists', 'goal_assists', 'assist', 'totalAssists'))
    goals = find_stat(('goals', 'goal', 'totalGoals'))
    return name, assists, goals


def main():
    diag = {'competition_opta_id': COMPETITION_OPTA_ID, 'season_opta_id': SEASON_OPTA_ID,
            'clubs_step': None, 'roster_steps': []}
    result = {'assists': {}, 'goals': {}, '_diag': diag}

    # (a) 클럽 목록 확보
    clubs_url = (f'https://stats-api.mlssoccer.com/v1/clubs?'
                 f'competition_opta_id={COMPETITION_OPTA_ID}&'
                 f'season_opta_id={SEASON_OPTA_ID}&order_by=club_name')
    status, body = _get(clubs_url)
    diag['clubs_step'] = {'url': clubs_url, 'status': status}
    if status != 200:
        diag['clubs_step']['body_head'] = body[:300]
        print(f'[collect_mls_official_stats] ⚠️ 클럽 목록 조회 실패(status={status}) — '
              f'클럽 ID 자체를 못 구해서 로스터 단계로 못 감. '
              f'[diag].clubs_step.body_head 확인할 것.', flush=True)
        _save(result)
        return

    try:
        clubs_parsed = json.loads(body)
    except json.JSONDecodeError:
        diag['clubs_step']['error'] = 'JSON 파싱 실패'
        diag['clubs_step']['body_head'] = body[:300]
        print('[collect_mls_official_stats] ⚠️ 클럽 목록 응답이 JSON이 아님 — '
              '십중팔구 API가 아니라 HTML(로그인/에러) 페이지를 받은 것.', flush=True)
        _save(result)
        return

    club_rows = _rows_of(clubs_parsed)
    if not club_rows:
        diag['clubs_step']['top_level_keys'] = (sorted(clubs_parsed.keys())
                                                  if isinstance(clubs_parsed, dict) else None)
        print('[collect_mls_official_stats] ⚠️ 클럽 목록 응답은 200인데 리스트를 못 찾음 — '
              f'top_level_keys={diag["clubs_step"]["top_level_keys"]} 보고 '
              '_rows_of() 후보 키 추가할 것.', flush=True)
        _save(result)
        return

    diag['clubs_step']['row_count'] = len(club_rows)
    diag['clubs_step']['sample_row_keys'] = (sorted(club_rows[0].keys())
                                              if isinstance(club_rows[0], dict) else None)
    print(f'[collect_mls_official_stats] [diag] 클럽 목록 성공: {len(club_rows)}개, '
          f'sample_row_keys={diag["clubs_step"]["sample_row_keys"]}', flush=True)

    clubs = []
    for row in club_rows:
        if not isinstance(row, dict):
            continue
        cid = _extract_club_id(row)
        cname = _extract_club_name(row)
        if cid is not None:
            clubs.append((cid, cname))

    if not clubs:
        print('[collect_mls_official_stats] ⚠️ 클럽은 받았는데 club_opta_id로 보이는 '
              '필드를 하나도 못 찾음 — sample_row_keys 보고 _extract_club_id() '
              '후보 필드명 고칠 것.', flush=True)
        _save(result)
        return

    print(f'[collect_mls_official_stats] 클럽 ID {len(clubs)}개 확보, '
          f'로스터 조회 시작(클럽당 1회, 총 {len(clubs)}회 호출 예정)', flush=True)

    # (b) 클럽별 로스터 조회 → (c) 선수별 스탯 집계
    n_players_seen = 0
    n_players_matched = 0
    for i, (cid, cname) in enumerate(clubs):
        roster_url = f'https://sportapi.mlssoccer.com/api/players/byClub/{cid}?culture=en-us'
        status, body = _get(roster_url)
        step = {'club': cname, 'club_id': cid, 'url': roster_url, 'status': status}
        if status != 200:
            step['body_head'] = body[:200]
            diag['roster_steps'].append(step)
            time.sleep(0.2)
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            step['error'] = 'JSON 파싱 실패'
            step['body_head'] = body[:200]
            diag['roster_steps'].append(step)
            time.sleep(0.2)
            continue
        rows = _rows_of(parsed)
        if rows is None and isinstance(parsed, list):
            rows = parsed
        if not rows:
            step['top_level_keys'] = sorted(parsed.keys()) if isinstance(parsed, dict) else None
            diag['roster_steps'].append(step)
            time.sleep(0.2)
            continue

        step['row_count'] = len(rows)
        if i == 0:
            # 첫 클럽만 상세 로그(전체 로그가 너무 길어지지 않게)
            step['sample_row_keys'] = (sorted(rows[0].keys())
                                        if isinstance(rows[0], dict) else None)
            print(f'[collect_mls_official_stats] [diag] 첫 클럽({cname}) 로스터 성공: '
                  f'{len(rows)}명, sample_row_keys={step["sample_row_keys"]}', flush=True)

        for prow in rows:
            if not isinstance(prow, dict):
                continue
            n_players_seen += 1
            name, assists, goals = _extract_player_stats(prow)
            if name and (assists is not None or goals is not None):
                n_players_matched += 1
                if assists is not None:
                    result['assists'][name] = assists
                if goals is not None:
                    result['goals'][name] = goals

        diag['roster_steps'].append(step)
        time.sleep(0.2)

    diag['n_clubs_total'] = len(clubs)
    diag['n_clubs_ok'] = sum(1 for s in diag['roster_steps'] if s.get('status') == 200)
    diag['n_players_seen'] = n_players_seen
    diag['n_players_matched'] = n_players_matched
    print(f'[collect_mls_official_stats] 완료 — 클럽 {diag["n_clubs_ok"]}/{diag["n_clubs_total"]}개 '
          f'성공, 선수 {n_players_seen}명 중 {n_players_matched}명 스탯 매칭', flush=True)
    if n_players_matched == 0 and n_players_seen > 0:
        print('[collect_mls_official_stats] ⚠️ 선수는 받았는데 assist/goal 필드를 '
              '하나도 못 찾음 — roster_steps[0].sample_row_keys 보고 '
              '_extract_player_stats()의 find_stat() 후보 필드명을 실제 값으로 '
              '고칠 것.', flush=True)

    _save(result)


def _save(result):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f'[collect_mls_official_stats] {OUT_PATH} 저장 완료 — '
          f'다음 세션에서 이 [diag] 로그를 보고 build_leaderboard()의 MLS '
          f'항목에 실제로 병합할지 결정할 것 (아직 자동 병합 안 함).', flush=True)


if __name__ == '__main__':
    main()
