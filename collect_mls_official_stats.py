# -*- coding: utf-8 -*-
"""
MLS 공식(Opta 기반) 도움/득점 순위 수집 — 2026-07-26 착수, 2026-07-29 재작성.

배경: collect_goalscorers.py가 쓰는 BSD event incidents 스키마는 골당
assist 필드 1명만 준다 — MLS 고유의 "세컨더리 어시스트"(도움으로 이어진
패스의 직전 패스도 도움 인정) 규정이 반영이 안 돼서, MLS 도움왕만 실제
mlssoccer.com 공식 집계보다 적게 나온다(손흥민 실사례로 확인: 공식 10개인데
파이프라인 집계 6개). 다른 5개 리그는 이 문제가 없다(세컨더리 어시스트
규정 자체가 MLS 전용이라).

⚠️ 2026-07-26 첫 시도(stats-api.mlssoccer.com, competition_opta_id=98) 전부
404 — 완전히 틀린 호스트/ID 체계였다. 사용자가 모바일에서 mlssoccer.com
페이지 소스를 직접 열어서(view-page-source.com 경유) 찾아낸 실제 값으로
2026-07-29 재작성:
  - ID 체계가 "Opta"가 아니라 "Sportec"이었다: competitionSportecId
    (예: "MLS-COM-000001", 숫자 아님).
  - 실제 API 서버 후보 3개(페이지의 JS 설정 객체에서 직접 확인):
      forgeDAPI(v2)   = https://dapi.mlssoccer.com/v2
      forgeDAPIv1     = https://dapi.mlssoccer.com/v1
      d3SportsAPI     = https://sportapi.mlssoccer.com/api  (Deltatre로 추정 —
        "d3Sports"·소스에 deltatre.digital 흔적 있었음. 유력 후보.)
  - mls-leader-card 컴포넌트가 실제로 쓰던 파라미터: competitionSportecId,
    season(="2026", 문자열), statViewType(="shots_assists" 등 snake_case).
  - statsAPIToken이 빈 문자열("")로 찍혀 있었음 — 인증 토큰이 필요한 요청이면
    이 시도들도 401/403 날 수 있다. 이번에도 실패하면 그게 다음 단서.

⚠️ 이번에도 정확한 경로(path)까지는 확인 못 했다 — 호스트 3개 × 경로 후보
여러 개를 조합해서 시도한다. 여전히 [diag] 로그로 검증하는 방식(추측 금지
원칙 — collect_goalscorers.py와 동일).

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
COMPETITION_ID = 'MLS-COM-000001'  # 2026-07-29 확인: 정규시즌으로 추정(파일 하단 참고)
SEASON = '2026'

_BASES = [
    'https://sportapi.mlssoccer.com/api',   # d3SportsAPI — 가장 유력한 후보
    'https://dapi.mlssoccer.com/v1',        # forgeDAPIv1
    'https://dapi.mlssoccer.com/v2',        # forgeDAPI
]
_PATHS = [
    'stats/leaders/players',
    'leaders/players',
    'competitions/{comp}/seasons/{season}/leaders/players',
    'stats/leaders',
]

def _build_queries():
    out = []
    for base in _BASES:
        for path in _PATHS:
            p = path.format(comp=COMPETITION_ID, season=SEASON)
            out.append((f'{base}/{p}', {
                'competitionSportecId': COMPETITION_ID,
                'season': SEASON,
                'statViewType': 'shots_assists',
            }))
    return out

_CANDIDATE_QUERIES = _build_queries()


def _get(url_base, params, timeout=10):
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    url = f'{url_base}?{qs}'
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
    diag = {'tried': [], 'season_assumed': SEASON, 'competition_id_assumed': COMPETITION_ID}
    rows = None
    used_query = None

    for url_base, params in _CANDIDATE_QUERIES:
        status, body, url = _get(url_base, params, timeout=6)
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
