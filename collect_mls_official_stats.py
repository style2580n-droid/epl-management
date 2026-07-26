# -*- coding: utf-8 -*-
"""
MLS 공식(Opta 기반) 도움/득점 순위 수집 — 2026-07-26 착수.

배경: collect_goalscorers.py가 쓰는 BSD event incidents 스키마는 골당
assist 필드 1명만 준다 — MLS 고유의 "세컨더리 어시스트"(도움으로 이어진
패스의 직전 패스도 도움 인정) 규정이 반영이 안 돼서, MLS 도움왕만 실제
mlssoccer.com 공식 집계보다 적게 나온다(손흥민 실사례로 확인: 공식 10개인데
파이프라인 집계 6개). 다른 5개 리그는 이 문제가 없다(세컨더리 어시스트
규정 자체가 MLS 전용이라).

⚠️ 이 스크립트는 이 세션(샌드박스, 외부 네트워크 차단)에서 단 한 번도
실행/검증을 못 해봤다 — mlssoccer.com이 실제로 쓰는 백엔드
(stats-api.mlssoccer.com, Opta 기반)의 존재 자체는 공개된 3rd-party 문서
(GitHub gist: akeaswaran/mls-json-api.md)로 확인했지만, "리그 전체 도움
순위"를 한 번에 주는 엔드포인트가 정확히 어떤 경로/파라미터/응답 필드명인지는
실행 결과로만 검증 가능하다. collect_goalscorers.py와 동일한 원칙
(추측 금지 — [diag] 로그 남기고 실행 결과 보고 다음 사람이 고친다)으로 작성.

⚠️ 실행 전 반드시 확인/조정할 것:
  1. SEASON_OPTA_ID: 지금은 2026(MLS는 달력연도=시즌이라 연도값 그대로일
     가능성이 높다고 추정만 함 — gist 예시가 season_opta_id=2022로 2022년도를
     가리켰던 것과 같은 패턴이라 추정. 확정 아님)로 하드코딩해뒀다. 첫 실행
     로그의 [diag] season 확인 결과를 보고 틀렸으면 고칠 것.
  2. 인증 불필요(공개 API로 보임)라고 가정했다 — 401/403 뜨면 헤더
     (User-Agent 등) 추가가 필요할 수 있다.
  3. 리그 전체 정렬 엔드포인트를 못 찾으면(모든 후보가 개별 선수 조회만
     되면), 대안으로 선수 목록(로스터)을 먼저 받아 선수별로 순회 조회하는
     방식으로 전환해야 하는데, 그러면 호출 수가 팀당 30명 × 30팀 = 900회
     가까이 나올 수 있어 rate limit 확인이 먼저 필요하다 — 이번 스크립트는
     "리그 전체 한 번에" 후보들만 우선 시도하고, 전부 실패하면 그 사실만
     로그로 남기고 종료한다(추측으로 로스터 순회를 자동 실행하지 않음).

출력: data/master/mls_official_stats.json
  { "assists": {"선수명(영문)": 개수, ...}, "goals": {...},
    "_diag": {...} }  ← 다음 세션/사용자 검증용, build_leaderboard에는
  아직 자동 병합 안 함(필드 매핑 확정 전까지는 수동 대조 단계).
"""
import json
import os
import time
import urllib.error
import urllib.request

OUT_PATH = 'data/master/mls_official_stats.json'
BASE = 'https://stats-api.mlssoccer.com/v1'
COMPETITION_OPTA_ID = 98  # gist 문서에 MLS 정규시즌으로 명시됨(competition=98)
SEASON_OPTA_ID = 2026     # ⚠️ 추정치 — 실행 로그로 검증 필요(위 docstring 참고)

# "리그 전체 도움 순위"를 한 번에 줄 가능성이 있는 후보 쿼리들.
# player_opta_id를 빼고 order_by만으로 리그 전체를 정렬해서 받는 시도.
_CANDIDATE_QUERIES = [
    ('players/seasons', {
        'competition_opta_id': COMPETITION_OPTA_ID,
        'season_opta_id': SEASON_OPTA_ID,
        'order_by': '-assists',
        'page_size': 60,
        'include': '*',
    }),
    ('players/seasons', {
        'competition_opta_id': COMPETITION_OPTA_ID,
        'season_opta_id': SEASON_OPTA_ID,
        'order_by': '-statistics.assists',
        'page_size': 60,
        'include': 'player,club,statistics',
    }),
    ('players/statistics/seasons', {
        'competition_opta_id': COMPETITION_OPTA_ID,
        'season_opta_id': SEASON_OPTA_ID,
        'order_by': '-assists',
        'page_size': 60,
    }),
]


def _get(path, params, timeout=10):
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    url = f'{BASE}/{path}?{qs}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; stats-research/1.0)',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return resp.status, body, url
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', errors='replace'), url
    except Exception as exc:  # noqa: BLE001 — 진단 단계라 광범위 캐치 후 로그
        return None, f'{type(exc).__name__}: {exc}', url


def _rows_of(parsed):
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in ('data', 'results', 'items'):
            if isinstance(parsed.get(k), list):
                return parsed[k]
    return None


def _find_assist_field(row):
    """row(dict) 안에서 어시스트로 보이는 필드를 폭넓게 탐색.
    중첩 dict(예: row['statistics']['assists'])도 한 단계 내려가서 확인."""
    candidates = ('assists', 'goal_assists', 'assist')
    for k in candidates:
        if k in row and isinstance(row[k], (int, float)):
            return row[k], k
    for v in row.values():
        if isinstance(v, dict):
            for k in candidates:
                if k in v and isinstance(v[k], (int, float)):
                    return v[k], f'(nested).{k}'
    return None, None


def main():
    diag = {'tried': [], 'season_opta_id_assumed': SEASON_OPTA_ID}
    rows = None
    used_query = None

    for path, params in _CANDIDATE_QUERIES:
        status, body, url = _get(path, params)
        entry = {'url': url, 'status': status}
        if status == 200:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                entry['error'] = 'JSON 파싱 실패'
                entry['body_head'] = body[:300]
                diag['tried'].append(entry)
                continue
            found = _rows_of(parsed)
            entry['row_count'] = len(found) if found is not None else None
            entry['top_level_keys'] = (sorted(parsed.keys())
                                        if isinstance(parsed, dict) else None)
            if found:
                entry['sample_row_keys'] = (sorted(found[0].keys())
                                             if isinstance(found[0], dict) else None)
                rows = found
                used_query = entry
                diag['tried'].append(entry)
                print(f'[collect_mls_official_stats] [diag] 성공 후보: {url}\n'
                      f'  row_count={entry["row_count"]}, '
                      f'sample_row_keys={entry["sample_row_keys"]}', flush=True)
                break
        else:
            entry['body_head'] = body[:300]
        diag['tried'].append(entry)
        print(f'[collect_mls_official_stats] [diag] 후보 실패/미확정: {url} '
              f'→ status={status}', flush=True)
        time.sleep(0.3)

    result = {'assists': {}, 'goals': {}, '_diag': diag}

    if rows is None:
        print('[collect_mls_official_stats] ⚠️ 모든 후보 실패 — 리그 전체 '
              '정렬 엔드포인트를 못 찾음. [diag]의 status/body_head를 보고 '
              '다음 후보(예: 로스터 순회 방식)를 판단할 것. 자동으로 로스터 '
              '순회는 실행하지 않음(호출량 문제로 사전 확인 필요).', flush=True)
    else:
        n_matched = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = None
            for key in ('player_name', 'full_name', 'name'):
                if isinstance(row.get(key), str):
                    name = row[key]
                    break
            if not name:
                player_obj = row.get('player')
                if isinstance(player_obj, dict):
                    name = player_obj.get('full_name') or player_obj.get('name')
            assists, field_used = _find_assist_field(row)
            if name and assists is not None:
                result['assists'][name] = assists
                n_matched += 1
        diag['n_rows'] = len(rows)
        diag['n_matched'] = n_matched
        print(f'[collect_mls_official_stats] 파싱 결과: {len(rows)}행 중 '
              f'{n_matched}명 매칭 성공', flush=True)
        if n_matched == 0:
            print('[collect_mls_official_stats] ⚠️ row는 받았는데 assist/이름 '
                  '필드를 하나도 못 찾음 — sample_row_keys를 보고 '
                  '_find_assist_field()의 candidates를 실제 필드명으로 '
                  '고칠 것.', flush=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f'[collect_mls_official_stats] {OUT_PATH} 저장 완료 — '
          f'다음 세션에서 이 [diag] 로그를 보고 build_leaderboard()의 MLS '
          f'항목에 실제로 병합할지 결정할 것 (아직 자동 병합 안 함).', flush=True)


if __name__ == '__main__':
    main()
