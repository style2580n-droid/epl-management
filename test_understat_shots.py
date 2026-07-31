# -*- coding: utf-8 -*-
"""rule-3 테스트: 실제 네트워크 없이 파싱/매칭/병합 로직만 검증.
Understat 실제 페이지 구조를 흉내낸 mock HTML로 검증한다(공식 문서가 없어서
공개적으로 알려진 임베드 패턴을 재현 — 실제 사이트와 다를 위험은 여전히
있고, 그건 이 테스트로는 못 잡는다. 첫 실전 실행 로그로 최종 검증 필요)."""
import json
import os
import sqlite3
import sys
import tempfile

import collect_understat_shots as m

FAILS = []


def check(label, cond):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {label}')
    if not cond:
        FAILS.append(label)


def js_escape_utf8(obj):
    """실제 Understat이 하는 것과 반대 방향(인코딩)을 흉내내서 테스트용 mock을
    만든다: JSON 문자열의 UTF-8 바이트를 \\xHH로 이스케이프."""
    raw_json = json.dumps(obj, ensure_ascii=False)
    utf8_bytes = raw_json.encode('utf-8')
    escaped = ''.join(f'\\x{b:02x}' for b in utf8_bytes)
    return escaped


# ---------------------------------------------------------- 1) JS 임베드 데이터 디코딩 (한글/악센트 포함)
mock_dates_data = [
    {"id": "111", "isResult": True, "h": {"id": "1", "title": "Manchester City"},
     "a": {"id": "2", "title": "Arsenal"}, "goals": {"h": "2", "a": "1"},
     "datetime": "2026-08-16 16:00:00"},
    {"id": "222", "isResult": False, "h": {"id": "3", "title": "Chelsea"},
     "a": {"id": "4", "title": "Tottenham"}, "goals": None,
     "datetime": "2026-08-20 16:00:00"},
    {"id": "333", "isResult": True, "h": {"id": "5", "title": "Raphaël FC"},  # 악센트 포함(디코딩 검증용)
     "a": {"id": "6", "title": "테스트"},  # 한글도 섞어서(2바이트 이상 멀티바이트 검증)
     "goals": {"h": "1", "a": "0"}, "datetime": "2026-08-16 12:00:00"},
]
escaped = js_escape_utf8(mock_dates_data)
mock_html = f"<script>var datesData = JSON.parse('{escaped}');</script>"
decoded = m._extract_js_json(mock_html, 'datesData')
check('디코딩 성공(None 아님)', decoded is not None)
check('디코딩된 리스트 길이 3', decoded is not None and len(decoded) == 3)
check('영문 팀명 정확히 복원', decoded is not None and decoded[0]['h']['title'] == 'Manchester City')
check('악센트 문자(Raphaël) 정확히 복원', decoded is not None and decoded[2]['h']['title'] == 'Raphaël FC')
check('한글 정확히 복원', decoded is not None and decoded[2]['a']['title'] == '테스트')

check('변수명 다르면 None', m._extract_js_json(mock_html, 'shotsData') is None)
check('빈 HTML이면 None', m._extract_js_json('', 'datesData') is None)

# ---------------------------------------------------------- 2) 매치 매칭 (팀명+날짜, 리그 필터)
found, swapped = m.find_understat_match(mock_dates_data, '맨체스터 시티', '아스날', '2026-08-16', 'epl')
check('올바른 매치 찾음', found is not None and found['id'] == '111')
check('스왑 안 됨', swapped is False)

found2, _ = m.find_understat_match(mock_dates_data, '맨체스터 시티', '아스날', '2026-08-16', 'laliga')
check('리그 불일치면 매칭 안 됨', found2 is None)

found3, _ = m.find_understat_match(mock_dates_data, '맨체스터 시티', '아스날', '2026-08-17', 'epl')
check('날짜 다르면 매칭 안 됨', found3 is None)

found4, _ = m.find_understat_match(mock_dates_data, '첼시', '토트넘', '2026-08-20', 'epl')
check('isResult=False(미종료 경기)는 매칭 안 됨', found4 is None)

# ---------------------------------------------------------- 3) 슈팅 데이터 파싱 (sot 판정 로직)
mock_shots = {
    "h": [
        {"player": "Erling Haaland", "result": "Goal"},
        {"player": "Erling Haaland", "result": "SavedShot"},
        {"player": "Erling Haaland", "result": "MissedShots"},   # sot 아님
        {"player": "Erling Haaland", "result": "BlockedShot"},   # sot 아님
        {"player": "Kevin De Bruyne", "result": "ShotOnPost"},   # sot 아님(관례상 제외)
    ],
    "a": [
        {"player": "Bukayo Saka", "result": "Goal"},
    ],
}
parsed = m.parse_shots_to_player_stats(mock_shots)
check('Haaland 슈팅 4개', parsed['Erling Haaland']['shots'] == 4)
check('Haaland sot 2개(Goal+SavedShot만)', parsed['Erling Haaland']['sot'] == 2)
check('De Bruyne 슈팅 1개, sot 0개(ShotOnPost 제외)', parsed['Kevin De Bruyne']['shots'] == 1 and parsed['Kevin De Bruyne']['sot'] == 0)
check('Saka 슈팅 1개, sot 1개', parsed['Bukayo Saka']['shots'] == 1 and parsed['Bukayo Saka']['sot'] == 1)

# ---------------------------------------------------------- 4) metrics 병합 (이니셜.성 매칭 + 신규선수 스킵)
tmpdir = tempfile.mkdtemp()
metrics_path = os.path.join(tmpdir, 'test_metrics.json')
existing = {
    "players": {
        "E. Haaland": {"goals": 2, "assists": 0, "shots": 0},
        "K. De Bruyne": {"goals": 0, "assists": 1, "shots": 0},
    }
}
with open(metrics_path, 'w', encoding='utf-8') as f:
    json.dump(existing, f, ensure_ascii=False)

shots_by_player = {
    "Erling Haaland": {"shots": 4, "sot": 2},
    "Kevin De Bruyne": {"shots": 1, "sot": 0},
    "Bukayo Saka": {"shots": 1, "sot": 1},  # 이 metrics 파일엔 없는 선수 -> 스킵돼야 함
}
n_merged = m.merge_shots_into_metrics(metrics_path, shots_by_player)
check('2명만 병합(Saka는 기존 metrics에 없어서 스킵)', n_merged == 2)

with open(metrics_path, encoding='utf-8') as f:
    after = json.load(f)
check('Haaland shots=4로 갱신', after['players']['E. Haaland']['shots'] == 4)
check('Haaland sot=2', after['players']['E. Haaland']['sot'] == 2)
check('De Bruyne shots=1', after['players']['K. De Bruyne']['shots'] == 1)
check('Saka는 새 키로 안 생김(보수적 매칭 원칙)', 'Bukayo Saka' not in after['players'])
check('기존 goals/assists 필드 보존', after['players']['E. Haaland']['goals'] == 2)
check('_understat_enriched 마커 존재', after['players']['E. Haaland'].get('_understat_enriched') is True)

# ---------------------------------------------------------- 5) 리그 필터링 (Understat 미지원 리그 자동 제외)
db_path = os.path.join(tmpdir, 'football.db')
conn = sqlite3.connect(db_path)
conn.execute('''CREATE TABLE matches (
    match_id TEXT PRIMARY KEY, home TEXT, away TEXT, date TEXT, status TEXT,
    home_goals INTEGER, away_goals INTEGER)''')
conn.execute("INSERT INTO matches VALUES ('m1','맨체스터 시티','아스날','2026-08-16','FINISHED',2,1)")
conn.execute("INSERT INTO matches VALUES ('m2','LA Galaxy','Inter Miami','2026-08-16','FINISHED',1,1)")  # MLS - Understat 미지원
conn.commit()
conn.close()

_orig = m.DB_PATH
m.DB_PATH = db_path
rows = m._finished_matches()
resolved = [m._resolve_match_teams(r) for r in rows]
resolved = [r for r in resolved if r]
understat_supported = [r for r in resolved if r[0] in m.UNDERSTAT_LEAGUES]
check('전체 리그판별 2건(EPL+MLS 둘 다 인식됨)', len(resolved) == 2)
check('Understat 지원 필터 적용하면 EPL 1건만 남음(MLS 자동 제외)',
      len(understat_supported) == 1 and understat_supported[0][0] == 'epl')
m.DB_PATH = _orig

print()
if FAILS:
    print(f'{len(FAILS)}건 실패:', FAILS)
    sys.exit(1)
else:
    print('전부 통과')
