# -*- coding: utf-8 -*-
"""
collect_football_data_co_uk.py
2026-07-31 신규 (2순위 크롤링 대상 3곳 중 하나).

football-data.co.uk의 정적 CSV(`/mmz4281/{season}/{div}.csv`)를 받아서
종료경기의 스코어/배당(종가 평균)/팀단위 슈팅·유효슈팅·코너를
data/master/footballdata_co_uk.json에 저장한다.

## Understat과 다른 점 (중요)
Understat은 "선수별" 슈팅을 줘서 metrics.json에 직접 병합했는데, 이 사이트는
**팀 단위** 합계만 준다(어느 선수가 몇 개 쏘았는지는 없음). 그래서 이건
2-1(선수 개인기여도%) 필드를 못 채운다 — 대신 팀 단위 xG류 분석/배당
백테스트/모델 학습용 보조 데이터로 쓴다. 목적이 다르다는 걸 명확히 해둔다.

## 왜 Understat보다 훨씬 안정적인가
Understat은 페이지에 JS로 임베드된 데이터를 정규식으로 뽑아야 해서 사이트
구조 바뀌면 바로 깨졌다(이번 세션에 두 번 겪음). football-data.co.uk는
애초에 "다운로드해서 쓰라"고 만든 정적 CSV 파일이라 JS 렌더링도 없고,
수년째 컬럼 구조가 거의 안 바뀐 걸로 널리 알려져 있다 — 그래도 이 프로젝트
원칙대로 실행 로그로 컬럼 존재 여부를 검증하고, 없으면 조용히 건너뛴다.

## 지원 리그
EPL/챔피언십/라리가/분데스리가/세리에A/리그1/에레디비시 7개(Understat과
거의 동일한 한계 — MLS/엘리테세리엔은 이 사이트도 커버 안 함).

## 시즌 코드
이 프로젝트 관례(시작연도="2026")를 그대로 두 자리씩 이어붙인 형식으로
변환한다(예: 2025년 8월 시작 시즌 → "2526"). 경기 날짜별로 올바른 시즌을
계산해서 필요한 시즌 파일만 받는다(Understat에서 시즌 계산 잘못했다가
겪은 실수를 반영해 처음부터 날짜 기반으로 설계).

## 안전 실패
CSV 파싱은 컬럼 "이름"으로 찾는다(고정 위치 가정 안 함 — 시즌/리그마다
컬럼 순서가 달라질 수 있어서). 필요한 컬럼이 없으면 그 값만 None 처리하고
계속 진행. 전체 실패해도 파이프라인 안 죽음(yml에서 || true).
"""
import csv
import io
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

from app_export import TEAM_NAME_MAP
from app_export_multileague import LEAGUE_TEAM_MAPS

DB_PATH = 'data/football.db'
OUT_PATH = 'data/master/footballdata_co_uk.json'
BASE = 'https://www.football-data.co.uk/mmz4281'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
REQUEST_DELAY_SEC = 1.0

# football-data.co.uk의 division 코드(공개적으로 널리 알려진, 수년째 안정적인 코드).
# MLS/엘리테세리엔은 이 사이트가 아예 안 다뤄서 대상에서 제외.
FD_DIV_CODES = {
    'epl': 'E0', 'championship': 'E1', 'laliga': 'SP1', 'bundesliga': 'D1',
    'seriea': 'I1', 'ligue1': 'F1', 'eredivisie': 'N1',
}

# 배당 컬럼 후보(우선순위 순 — Avg*가 여러 북메이커 평균이라 1순위,
# 없으면 Bet365가 가장 오래·꾸준히 존재해서 2순위, 그다음 Pinnacle).
ODDS_COL_CANDIDATES = [('AvgH', 'AvgD', 'AvgA'), ('B365H', 'B365D', 'B365A'),
                        ('PSH', 'PSD', 'PSA')]


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


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def fd_season_code(date_str):
    """이 프로젝트의 '시작연도' 관례를 football-data.co.uk의 두 자리씩
    이어붙인 시즌코드로 변환(예: 2025년 8월 시작 시즌 → '2526')."""
    year, month = int(date_str[:4]), int(date_str[5:7])
    start_year = year if month >= 8 else year - 1
    end_year = start_year + 1
    return f'{str(start_year)[-2:]}{str(end_year)[-2:]}'


def _parse_fd_date(raw):
    """DD/MM/YYYY 또는 DD/MM/YY 둘 다 처리(사이트가 연도 표기를 시즌별로
    바꿔온 이력이 있다고 알려져 있어 방어적으로)."""
    if not raw:
        return None
    for fmt in ('%d/%m/%Y', '%d/%m/%y'):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    f = _to_float(v)
    return int(f) if f is not None else None


def fetch_division_csv(session, div_code, season_code):
    url = f'{BASE}/{season_code}/{div_code}.csv'
    try:
        r = session.get(url, headers={'User-Agent': UA}, timeout=15)
        if r.status_code != 200:
            print(f'[collect_football_data_co_uk] {url} 응답 {r.status_code}', flush=True)
            return None
        # 이 사이트 CSV는 종종 latin-1로 인코딩돼있다고 알려져 있음(팀명에
        # 특수문자 있을 때 utf-8로 깨지는 경우가 보고된 바 있어 방어적으로 처리).
        text = r.content.decode('utf-8', errors='replace')
        if '\ufffd' in text[:2000]:  # utf-8 디코드 깨짐 흔적이면 latin-1로 재시도
            text = r.content.decode('latin-1', errors='replace')
        return text
    except Exception as e:
        print(f'[collect_football_data_co_uk] {url} 요청 실패: {e}', flush=True)
        return None


def parse_division_csv(csv_text, league_key):
    """컬럼 '이름' 기준으로 파싱(고정 위치 가정 안 함). 못 찾는 컬럼은 None."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return [], {'no_header': True}
    fieldnames = set(reader.fieldnames)
    odds_cols = None
    for h, d, a in ODDS_COL_CANDIDATES:
        if h in fieldnames and d in fieldnames and a in fieldnames:
            odds_cols = (h, d, a)
            break

    out = []
    diag = {'rows': 0, 'team_unmatched': 0, 'date_unparsed': 0, 'odds_col_used': odds_cols}
    for row in reader:
        diag['rows'] += 1
        date_kst = _parse_fd_date(row.get('Date'))
        if not date_kst:
            diag['date_unparsed'] += 1
            continue
        home_raw, away_raw = row.get('HomeTeam'), row.get('AwayTeam')
        h_info = TEAM_INDEX.get(_norm(home_raw))
        a_info = TEAM_INDEX.get(_norm(away_raw))
        if not h_info or not a_info or h_info[0] != league_key or a_info[0] != league_key:
            diag['team_unmatched'] += 1
            continue
        hg, ag = _to_int(row.get('FTHG')), _to_int(row.get('FTAG'))
        if hg is None or ag is None:
            continue  # 아직 안 끝난 경기(드묾, 이 사이트는 보통 종료경기 위주)
        entry = {
            'date': date_kst, 'home': h_info[1], 'away': a_info[1],
            'homeGoals': hg, 'awayGoals': ag,
            'shots_home': _to_int(row.get('HS')), 'shots_away': _to_int(row.get('AS')),
            'sot_home': _to_int(row.get('HST')), 'sot_away': _to_int(row.get('AST')),
            'corners_home': _to_int(row.get('HC')), 'corners_away': _to_int(row.get('AC')),
        }
        if odds_cols:
            oh, od, oa = (_to_float(row.get(c)) for c in odds_cols)
            if oh and od and oa:
                entry['odds'] = {'home': oh, 'draw': od, 'away': oa,
                                  'source': f'football-data.co.uk({odds_cols[0][:-1]})'}
        out.append(entry)
    return out, diag


def _finished_match_dates_by_league():
    """DB에서 종료경기의 (league_key -> 이 리그에 실제로 존재하는 시즌코드 집합)을
    뽑는다 — 리그당 매번 여러 시즌 CSV를 다 받지 않고, 우리 DB에 실제로 있는
    시즌만 받기 위함(불필요한 요청 최소화)."""
    if not os.path.exists(DB_PATH):
        return {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT home, away, date FROM matches "
            "WHERE status='FINISHED' AND home_goals IS NOT NULL AND date IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        h_info = TEAM_INDEX.get(_norm(r['home']))
        a_info = TEAM_INDEX.get(_norm(r['away']))
        if not h_info or not a_info or h_info[0] != a_info[0]:
            continue
        lk = h_info[0]
        if lk not in FD_DIV_CODES:
            continue
        season = fd_season_code(r['date'])
        out.setdefault(lk, set()).add(season)
    return out


def main():
    if requests is None:
        print('[collect_football_data_co_uk] requests 라이브러리 없음 → 스킵', flush=True)
        return

    needed = _finished_match_dates_by_league()
    if not needed:
        print('[collect_football_data_co_uk] DB에 매칭되는 종료경기 없음(db.py '
              '미실행이거나 아직 데이터 없음) → 스킵', flush=True)
        return
    print(f'[collect_football_data_co_uk] 필요한 (리그,시즌) 조합: '
          f'{sum(len(v) for v in needed.values())}건', flush=True)

    session = requests.Session()
    out = _load_json(OUT_PATH, {})
    total_matched = 0
    for lk, seasons in needed.items():
        div_code = FD_DIV_CODES[lk]
        league_matches = {e['date'] + e['home'] + e['away']: e for e in out.get(lk, [])}
        for season_code in sorted(seasons):
            csv_text = fetch_division_csv(session, div_code, season_code)
            time.sleep(REQUEST_DELAY_SEC)
            if not csv_text:
                continue
            entries, diag = parse_division_csv(csv_text, lk)
            print(f'[collect_football_data_co_uk] {div_code}/{season_code}: '
                  f'{diag.get("rows", 0)}행 중 매칭 {len(entries)}건 '
                  f'(팀불일치 {diag.get("team_unmatched", 0)}, '
                  f'날짜파싱실패 {diag.get("date_unparsed", 0)}, '
                  f'배당컬럼={diag.get("odds_col_used")})', flush=True)
            for e in entries:
                league_matches[e['date'] + e['home'] + e['away']] = e
        out[lk] = list(league_matches.values())
        total_matched += len(out[lk])

    _atomic_write(OUT_PATH, out)
    print(f'[collect_football_data_co_uk] 완료: {len(out)}개 리그, '
          f'누적 {total_matched}경기 → {OUT_PATH} 저장', flush=True)


if __name__ == '__main__':
    main()
