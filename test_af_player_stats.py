# -*- coding: utf-8 -*-
"""rule-3 테스트: 실제 API 호출 없이(네트워크 비활성 환경) 핵심 로직만 검증.
실측 확인된 API-FOOTBALL 표준 스키마(EPL_index.html fetchApiFootballPlayerStats
로 이미 확인된 필드 + 공개 문서 기준 확장 필드)를 흉내낸 mock으로 검증한다.
"""
import json
import os
import sqlite3
import sys
import tempfile

import collect_api_football_player_stats as m

FAILS = []


def check(label, cond):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {label}')
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------- 1) 팀명 매칭 (한글/영문 둘 다)
check('한글 입력 EPL 매칭', m._resolve_match_teams(
    {'home': '맨체스터 시티', 'away': '아스날'}) == ('epl', '맨체스터 시티', '아스날'))
check('영문 입력 라리가 매칭', m._resolve_match_teams(
    {'home': 'Real Madrid CF', 'away': 'FC Barcelona'}) == ('laliga', '레알 마드리드', '바르셀로나'))
check('리그 다른 두 팀은 매칭 실패(안전 거부)', m._resolve_match_teams(
    {'home': '맨체스터 시티', 'away': 'FC Barcelona'}) is None)
check('둘 다 모르는 팀은 None', m._resolve_match_teams(
    {'home': 'FC Zenit', 'away': 'FC Something'}) is None)

# ---------------------------------------------------------- 2) AF 날짜 변환(UTC->KST)
check('UTC 자정 근처가 KST로는 다음날', m._af_date_to_kst('2026-08-15T16:00:00+00:00') == '2026-08-16')
check('일반 시각 KST 변환', m._af_date_to_kst('2026-08-15T10:00:00+00:00') == '2026-08-15')
check('빈 문자열은 None', m._af_date_to_kst('') is None)

# ---------------------------------------------------------- 3) AF fixture 매칭
fixtures = [
    {'id': 111, 'date_kst': '2026-08-16', 'status': 'FT',
     'home': 'Manchester City FC', 'away': 'Arsenal FC'},
    {'id': 222, 'date_kst': '2026-08-16', 'status': 'NS',
     'home': 'Real Madrid CF', 'away': 'FC Barcelona'},  # 다른 리그, 같은 날짜(오매칭 방지 검증용)
]
fx = m._find_af_fixture(fixtures, '맨체스터 시티', '아스날', '2026-08-16', 'epl')
check('올바른 리그 필터로 정확히 매칭', fx is not None and fx['id'] == 111)
fx_wrong_league = m._find_af_fixture(fixtures, '맨체스터 시티', '아스날', '2026-08-16', 'laliga')
check('리그 불일치면 매칭 안 됨', fx_wrong_league is None)
fx_wrong_date = m._find_af_fixture(fixtures, '맨체스터 시티', '아스날', '2026-08-17', 'epl')
check('날짜 다르면 매칭 안 됨(같은 시즌 두 번째 맞대결 오매칭 방지)', fx_wrong_date is None)

# ---------------------------------------------------------- 4) /fixtures/players 응답 파싱
# EPL_index.html fetchApiFootballPlayerStats로 확인된 필드(shots/tackles/passes.key/
# games.rating/minutes) + 표준 문서 기준 확장 필드(tackles.interceptions/dribbles/duels)
mock_af_response = {
    "response": [
        {
            "team": {"name": "Manchester City FC"},
            "players": [
                {
                    "player": {"name": "Kevin De Bruyne"},
                    "statistics": [{
                        "games": {"minutes": 90, "rating": "8.2"},
                        "shots": {"total": 3, "on": 2},
                        "goals": {"total": 1, "assists": 1},
                        "passes": {"total": 65, "key": 4, "accuracy": "88"},
                        "tackles": {"total": 2, "blocks": 0, "interceptions": 1},
                        "duels": {"total": 10, "won": 6},
                        "dribbles": {"attempts": 3, "success": 2, "past": None},
                    }]
                },
                {
                    # 통계 자체가 없는 선수(교체 미출전 등) — 크래시 없이 스킵돼야 함
                    "player": {"name": "Bench Warmer"},
                    "statistics": []
                },
            ],
        },
        {
            "team": {"name": "Arsenal FC"},
            "players": [
                {
                    "player": {"name": "Bukayo Saka"},
                    "statistics": [{
                        "games": {"minutes": 90, "rating": "7.1"},
                        "shots": {"total": 1, "on": 1},
                        "goals": {"total": 0, "assists": 0},
                        "passes": {"total": 40, "key": 1},
                        "tackles": {"total": 1, "interceptions": 0},
                        "duels": {"total": 8, "won": 3},
                        "dribbles": {"attempts": 5, "success": 3},
                    }]
                },
            ],
        },
    ]
}
parsed = m.parse_players_response(mock_af_response)
check('파싱된 선수 수 = 2 (통계 없는 선수는 스킵)', len(parsed) == 2)
kdb = next(p for p in parsed if p['name'] == 'Kevin De Bruyne')
check('De Bruyne shots=3', kdb['shots'] == 3)
check('De Bruyne sot=2', kdb['sot'] == 2)
check('De Bruyne interceptions=1', kdb['interceptions'] == 1)
check('De Bruyne keyPasses=4', kdb['keyPasses'] == 4)
check('De Bruyne dribbleAttempts=3', kdb['dribbleAttempts'] == 3)
check('De Bruyne rating=8.2(문자열->float 변환)', kdb['rating'] == 8.2)

check('빈 응답이면 None', m.parse_players_response({"response": []}) is None)
check('response 없으면 None', m.parse_players_response({}) is None)

# ---------------------------------------------------------- 5) metrics 파일 병합
tmpdir = tempfile.mkdtemp()
metrics_path = os.path.join(tmpdir, 'testmatch_metrics.json')
# 기존 BSD 기반 metrics: 이니셜.성 형식 키, 골/어시스트만 정확하고 나머지는 0(알려진 한계)
existing = {
    "players": {
        "K. De Bruyne": {"goals": 1, "assists": 1, "xG": 0.6, "xA": 0.4,
                          "shots": 0, "progressive_passes": 3, "SCA": 2},
        "B. Saka": {"goals": 0, "assists": 0, "xG": 0.1, "xA": 0.0, "shots": 0},
    }
}
with open(metrics_path, 'w', encoding='utf-8') as f:
    json.dump(existing, f, ensure_ascii=False)

n_merged, n_new = m.merge_into_metrics(metrics_path, parsed)
check('2명 병합됨', n_merged == 2)
check('신규 키 0명(둘 다 이니셜.성으로 기존 키에 매칭됨)', n_new == 0)

with open(metrics_path, encoding='utf-8') as f:
    after = json.load(f)
kdb_stats = after['players']['K. De Bruyne']
check('병합 후 K. De Bruyne shots=3(0에서 갱신)', kdb_stats['shots'] == 3)
check('병합 후 K. De Bruyne tackles_won=2', kdb_stats['tackles_won'] == 2)
check('병합 후 K. De Bruyne interceptions=1', kdb_stats['interceptions'] == 1)
check('병합 후 K. De Bruyne key_passes=4', kdb_stats['key_passes'] == 4)
check('병합 후 K. De Bruyne dribbles=3/dribbles_won=2', kdb_stats['dribbles'] == 3 and kdb_stats['dribbles_won'] == 2)
check('병합 후 K. De Bruyne af_rating=8.2', kdb_stats['af_rating'] == 8.2)
check('기존 BSD 필드(xG 등)는 보존됨', kdb_stats['xG'] == 0.6 and kdb_stats['progressive_passes'] == 3)
check('_af_enriched 마커 존재', kdb_stats.get('_af_enriched') is True)

# ---------------------------------------------------------- 6) 동명이인/신규선수 케이스
existing2 = {
    "players": {
        "M. Smith": {"goals": 0, "assists": 0},  # 성 'Smith' 후보 1명뿐이면 이니셜만 봐도 매칭
        "J. Smith": {"goals": 0, "assists": 0},  # 성이 같은 동명이인 2명 -> 이니셜로 구분
    }
}
metrics_path2 = os.path.join(tmpdir, 'testmatch2_metrics.json')
with open(metrics_path2, 'w', encoding='utf-8') as f:
    json.dump(existing2, f, ensure_ascii=False)
parsed2 = [
    {'name': 'John Smith', 'shots': 5, 'sot': 2, 'tackles': None, 'interceptions': None,
     'keyPasses': None, 'dribbleAttempts': None, 'dribbleSuccess': None,
     'duelsTotal': None, 'duelsWon': None, 'rating': None, 'minutes': None},
    {'name': 'Unknown Player', 'shots': 1, 'sot': 0, 'tackles': None, 'interceptions': None,
     'keyPasses': None, 'dribbleAttempts': None, 'dribbleSuccess': None,
     'duelsTotal': None, 'duelsWon': None, 'rating': None, 'minutes': None},
]
n_merged2, n_new2 = m.merge_into_metrics(metrics_path2, parsed2)
with open(metrics_path2, encoding='utf-8') as f:
    after2 = json.load(f)
check('John Smith -> J. Smith로 이니셜 매칭', after2['players']['J. Smith']['shots'] == 5)
check('M. Smith는 안 건드려짐(shots 필드 없음)', 'shots' not in after2['players']['M. Smith'])
check('Unknown Player는 신규 키로 추가', 'Unknown Player' in after2['players'] and n_new2 == 1)
check('신규 병합 카운트 2명', n_merged2 == 2)

# ---------------------------------------------------------- 7) matches 테이블 조회(DB 스키마 재현)
db_path = os.path.join(tmpdir, 'football.db')
conn = sqlite3.connect(db_path)
conn.execute(m.__dict__.get('_SCHEMA_UNUSED', '') or '''
CREATE TABLE matches (
    match_id TEXT PRIMARY KEY, league_id TEXT, home TEXT, away TEXT,
    date TEXT, status TEXT, home_goals INTEGER, away_goals INTEGER
)''')
conn.execute("INSERT INTO matches VALUES ('m1','x','맨체스터 시티','아스날','2026-08-16','FINISHED',2,1)")
conn.execute("INSERT INTO matches VALUES ('m2','x','LA Galaxy','Inter Miami','2026-08-16','FINISHED',1,1)")  # MLS(미지원) - 걸러져야 함
conn.execute("INSERT INTO matches VALUES ('m3','x','Real Madrid CF','FC Barcelona','2026-08-16','SCHEDULED',None,None)".replace('None', 'NULL'))
conn.commit()
conn.close()

_orig_db_path = m.DB_PATH
m.DB_PATH = db_path
rows = m._finished_matches()
check('종료경기만 2건 조회(SCHEDULED 제외)', len(rows) == 2)
resolved_list = [m._resolve_match_teams(r) for r in rows]
resolved_list = [r for r in resolved_list if r]
check('리그판별 자체는 MLS도 인식(LEAGUE_TEAM_MAPS에 mls도 있음)', len(resolved_list) == 2)
af_supported = [r for r in resolved_list if r[0] in dict(m.AF_LEAGUE_IDS, epl=1)]
check('AF_LEAGUE_IDS(+epl) 필터 적용하면 MLS 걸러지고 EPL 1건만 남음(main()의 실제 필터 로직)',
      len(af_supported) == 1 and af_supported[0] == ('epl', '맨체스터 시티', '아스날'))
m.DB_PATH = _orig_db_path

print()
if FAILS:
    print(f'{len(FAILS)}건 실패:', FAILS)
    sys.exit(1)
else:
    print('전부 통과')
