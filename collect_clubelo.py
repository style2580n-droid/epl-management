# -*- coding: utf-8 -*-
"""
collect_clubelo.py
2026-07-31 신규 (2순위 크롤링 대상 3곳 중 하나).

clubelo.com의 날짜별 스냅샷 CSV(`http://api.clubelo.com/{YYYY-MM-DD}`)로
그 시점 전 세계 클럽의 Elo 레이팅을 한 번에 받아서, 우리가 추적하는 팀만
걸러 data/master/clubelo.json에 저장한다.

## 용도
기존 파이프라인의 ELO는 BSD(`CATEGORY=elo`)에서 온다 — 이건 그걸 대체하는
게 아니라 **보조/대조용**이다. BSD가 배당처럼 이 기능도 없앨 가능성을
대비한 백업, 그리고 두 소스가 크게 어긋나면 로그로 드러나게 하는 교차검증
용도로 만든다(자동으로 어느 한쪽에 덮어쓰지 않음 — 그냥 나란히 저장만).

## 왜 이 사이트는 비교적 신뢰 가능한가
clubelo.com은 애초에 "이 URL 그대로 받아서 쓰라"는 용도로 만들어진
공개 CSV 엔드포인트로, 수년간 같은 컬럼 구조(Rank,Club,Country,Level,
Elo,From,To)로 알려져 있다. 그래도 이 프로젝트 원칙대로 컬럼명으로
찾고, 없으면 조용히 스킵 + 로그로 남긴다.

## 팀명 매칭
Club 컬럼이 영문명이라(예: "Man City", "Real Madrid") 기존 TEAM_INDEX
(TEAM_NAME_MAP + LEAGUE_TEAM_MAPS 별칭)로 매칭한다. MLS/엘리테세리엔
포함 우리가 추적하는 모든 팀이 TEAM_INDEX에 있으니, 이 사이트가 그
리그들을 다루기만 하면(유럽 축구 중심 사이트라 MLS/엘리테세리엔은
없을 가능성이 있음 — 실행 로그로 확인) 자동으로 포함된다. 리그를
따로 안 가려서(Understat/football-data.co.uk와 달리) 커버리지가
넓으면 넓은 대로 다 잡힌다.

## 안전 실패
실패해도 파이프라인 안 죽음(yml에서 || true).
"""
import csv
import io
import json
import os
import re
import unicodedata
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

from app_export import TEAM_NAME_MAP
from app_export_multileague import LEAGUE_TEAM_MAPS

OUT_PATH = 'data/master/clubelo.json'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


# ============================================================ 팀명 매칭 (다른 스크립트들과 동일 원칙)
def _fold(name):
    n = unicodedata.normalize('NFKD', name)
    n = ''.join(c for c in n if not unicodedata.combining(c))
    return unicodedata.normalize('NFC', n)


def _norm(name):
    if not name:
        return ''
    n = _fold(name)
    n = re.sub(r'\b(FC|AFC|CF)\b', '', n, flags=re.I)
    return re.sub(r'[^a-z가-힣0-9]', '', n.lower())


def _build_team_index():
    index = {}
    for kr, aliases in TEAM_NAME_MAP.items():
        for a in list(aliases) + [kr]:
            index[_norm(a)] = ('epl', kr)
    for lk, team_map in LEAGUE_TEAM_MAPS.items():
        for kr, aliases in team_map.items():
            for a in list(aliases) + [kr]:
                index.setdefault(_norm(a), (lk, kr))
    return index


TEAM_INDEX = _build_team_index()


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def fetch_snapshot(session, date_str):
    """실패하면 https -> http 순으로 한 번 더 시도(clubelo.com이 원래
    http만 지원하던 시절 문서가 남아있어서 방어적으로 둘 다 시도)."""
    for scheme in ('https', 'http'):
        url = f'{scheme}://api.clubelo.com/{date_str}'
        try:
            r = session.get(url, headers={'User-Agent': UA}, timeout=15)
            if r.status_code == 200 and r.text.strip():
                return r.text
            print(f'[collect_clubelo] {url} 응답 {r.status_code}', flush=True)
        except Exception as e:
            print(f'[collect_clubelo] {url} 요청 실패: {e}', flush=True)
    return None


def parse_snapshot(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or 'Club' not in reader.fieldnames or 'Elo' not in reader.fieldnames:
        print(f'[collect_clubelo] [diag] 예상 컬럼(Club, Elo) 없음 — 실제 헤더: '
              f'{reader.fieldnames}', flush=True)
        return {}, {'rows': 0, 'matched': 0}

    out = {}  # league_key -> {kr_name: elo}
    n_rows = n_matched = 0
    for row in reader:
        n_rows += 1
        club = row.get('Club')
        info = TEAM_INDEX.get(_norm(club))
        if not info:
            continue
        lk, kr = info
        try:
            elo = float(row.get('Elo'))
        except (TypeError, ValueError):
            continue
        out.setdefault(lk, {})[kr] = elo
        n_matched += 1
    return out, {'rows': n_rows, 'matched': n_matched}


def main():
    if requests is None:
        print('[collect_clubelo] requests 라이브러리 없음 → 스킵', flush=True)
        return

    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    session = requests.Session()
    csv_text = fetch_snapshot(session, date_str)
    if not csv_text:
        print('[collect_clubelo] 스냅샷 조회 실패 → 스킵', flush=True)
        return

    by_league, diag = parse_snapshot(csv_text)
    print(f'[collect_clubelo] {date_str} 스냅샷 {diag["rows"]}개 클럽 중 '
          f'우리 팀 매칭 {diag["matched"]}건', flush=True)
    for lk, teams in sorted(by_league.items()):
        print(f'[collect_clubelo]   {lk}: {len(teams)}팀', flush=True)

    if not by_league:
        print('[collect_clubelo] 매칭된 팀 0개 — 저장 안 함(기존 파일 유지)', flush=True)
        return

    _atomic_write(OUT_PATH, {'date': date_str, 'by_league': by_league})
    print(f'[collect_clubelo] 완료 → {OUT_PATH} 저장', flush=True)


if __name__ == '__main__':
    main()
