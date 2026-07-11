# -*- coding: utf-8 -*-
"""
스모크 테스트 — 실데이터 최초 가동 검증 도구

목적: 문서 기반으로 작성된 응답 파싱이 실제 API 응답과 맞는지 소스별 1회
호출로 대조. "성공하면서 빈 데이터를 쌓는" 조용한 실패를 사전 차단.

사용:
  키 등록 후  →  python scripts/smoke_test.py
  결과        →  reports/smoke_report.md (소스별 OK/스키마 불일치/키 없음/호출 실패)
  STRICT=1 환경변수 설정 시 불일치가 있으면 종료코드 1 (CI 게이트용)
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from api_clients import build_registry  # noqa: E402


def _has_path(data, path):
    """'a.b[].c' 형태의 키 경로 존재 확인."""
    cur = data
    for part in path.split('.'):
        is_list = part.endswith('[]')
        key = part[:-2] if is_list else part
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return False
            cur = cur[key]
        if is_list:
            if not isinstance(cur, list) or not cur:
                return False
            cur = cur[0]
    return True


# 소스별 프로브: (설명, 호출 람다, 기대 키 경로들 — 파이프라인이 실제 파싱하는 필드)
PROBES = {
    'football-data': ('PL 팀 목록', lambda c: c.competition_teams('PL'),
                      ['teams[].id', 'teams[].name']),
    'openfootball': ('PL 시즌 일정', lambda c: c.season('PL'),
                     ['matches[].team1', 'matches[].team2', 'matches[].date']),
    'statsbomb': ('대회 목록', lambda c: c.competitions(),
                  []),  # 리스트 응답 — 아래 리스트 검사로 대체
    'fpl': ('부트스트랩', lambda c: c.bootstrap(),
            ['elements[].id', 'elements[].web_name', 'teams[].name']),
    'thesportsdb': ('팀 검색', lambda c: c.search_team('Arsenal'),
                    ['teams[].strStadium']),
    'bsd': ('리그 목록', lambda c: c.leagues(), []),
    'highlightly': ('오늘 경기', lambda c: c.matches(
        datetime.now(timezone.utc).date().isoformat()), []),
    'api-football': ('PL 픽스처', lambda c: c.fixtures(39, 2025), ['response']),
    'sportscore': ('오늘 이벤트', lambda c: c.events_by_date(
        datetime.now(timezone.utc).date().isoformat()), []),
}

LIST_OK = {'statsbomb': 'competition_id'}


def probe_source(name, client):
    desc, call, expected = PROBES[name]
    try:
        data, ok = call(client)
    except Exception as e:
        return {'status': 'CALL_FAIL', 'desc': desc, 'detail': str(e)[:200]}
    if not ok or data is None:
        return {'status': 'CALL_FAIL', 'desc': desc,
                'detail': 'ok=False 또는 빈 응답 (키 유효성/쿼터 확인)'}
    # 리스트형 응답 검사
    if name in LIST_OK:
        key = LIST_OK[name]
        if isinstance(data, list) and data and key in data[0]:
            return {'status': 'OK', 'desc': desc,
                    'detail': f'리스트 {len(data)}건, 키 {list(data[0])[:6]}'}
        return {'status': 'SCHEMA_MISMATCH', 'desc': desc,
                'detail': f'기대: [{key}] / 실제 최상위: '
                          f'{list(data)[:6] if isinstance(data, dict) else type(data).__name__}'}
    missing = [p for p in expected if not _has_path(data, p)]
    top = list(data)[:8] if isinstance(data, dict) else f'{type(data).__name__}'
    if missing:
        return {'status': 'SCHEMA_MISMATCH', 'desc': desc,
                'detail': f'누락 경로: {missing} / 실제 최상위: {top}'}
    return {'status': 'OK', 'desc': desc, 'detail': f'최상위: {top}'}


def main():
    registry = build_registry()
    results = {}
    for name in PROBES:
        if name in registry:
            print(f'[smoke] {name} 호출 중...')
            results[name] = probe_source(name, registry[name])
        else:
            results[name] = {'status': 'NO_KEY',
                             'desc': PROBES[name][0], 'detail': '키 미등록'}
    icon = {'OK': '✅', 'SCHEMA_MISMATCH': '🟠', 'CALL_FAIL': '🔴', 'NO_KEY': '⚪'}
    lines = [f'# 🔬 스모크 테스트 리포트 — '
             f'{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}', '',
             '| 소스 | 상태 | 프로브 | 상세 |', '|---|---|---|---|']
    for name, r in results.items():
        lines.append(f"| {name} | {icon[r['status']]} {r['status']} "
                     f"| {r['desc']} | {r['detail']} |")
    mismatches = [n for n, r in results.items()
                  if r['status'] == 'SCHEMA_MISMATCH']
    lines += ['', f"요약: OK {sum(r['status'] == 'OK' for r in results.values())} "
              f"/ 불일치 {len(mismatches)} "
              f"/ 실패 {sum(r['status'] == 'CALL_FAIL' for r in results.values())} "
              f"/ 키없음 {sum(r['status'] == 'NO_KEY' for r in results.values())}"]
    if mismatches:
        lines += ['', f'🟠 스키마 불일치 소스({", ".join(mismatches)})는 '
                  '해당 클라이언트/수집기의 필드 매핑 수정이 필요합니다.']
    os.makedirs('reports', exist_ok=True)
    with open('reports/smoke_report.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print('\n'.join(lines))
    if mismatches and os.getenv('STRICT') == '1':
        sys.exit(1)


if __name__ == '__main__':
    main()
