# -*- coding: utf-8 -*-
"""
SQLite 데이터베이스 계층
- 제안 아키텍처의 PostgreSQL 스키마를 GitHub Actions 친화적 SQLite로 구현
  (서버 불필요, data/football.db 파일로 저장소에 커밋 가능)
- 테이블: leagues / teams / players / matches / events / transfers / injuries
- 정규화 지원: entity_map (소스별 ID → 표준 ID 매핑, normalizer.py가 사용)
- 기존 JSON 산출물(data/master/*, data/events/*)을 적재하는 로더 포함
"""
import json
import os
import sqlite3
import unicodedata
import re as _re

# 2026-07-31 추가: 이벤트 payload에 date 필드 자체가 없다는 게 실측 확인돼서
# (진단로그: payload 키가 home/away/events 세 개뿐), h2h.json/h2h_history_
# multileague.json(둘 다 날짜+팀명+스코어를 이미 갖고 있음)에서 역으로 날짜를
# 찾아오기 위해 팀명 별칭 테이블이 필요하다. db.py는 원래 app_export*.py에
# 의존하지 않는 독립 모듈이었는데 이번에 처음 의존이 생긴다 — db.py가
# 파이프라인에서 제일 앞단(다른 모든 스크립트가 이 DB를 읽음)이라 이 임포트가
# 실패하면 절대 안 되므로 반드시 방어적으로(실패해도 빈 dict로 폴백, db.py
# 자체는 절대 안 죽게).
try:
    from app_export import TEAM_NAME_MAP as _EPL_TEAM_MAP
except Exception:
    _EPL_TEAM_MAP = {}
try:
    from app_export_multileague import LEAGUE_TEAM_MAPS as _ML_TEAM_MAPS
except Exception:
    _ML_TEAM_MAPS = {}

DB_PATH = 'data/football.db'

SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
    league_id   TEXT PRIMARY KEY,          -- 표준 코드 (PL/PD/BL1/SA/FL1)
    name        TEXT,
    country     TEXT,
    season      TEXT,
    matchday    INTEGER,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS teams (
    team_id     INTEGER PRIMARY KEY,       -- football-data ID를 표준으로
    league_id   TEXT REFERENCES leagues(league_id),
    name        TEXT,
    short_name  TEXT,
    stadium     TEXT,
    coach       TEXT,
    crest       TEXT,
    elo         REAL,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS players (
    player_id   TEXT PRIMARY KEY,          -- 표준 ID (normalizer 발급)
    team_id     INTEGER REFERENCES teams(team_id),
    name        TEXT,
    position    TEXT,
    minutes     INTEGER,
    goals       INTEGER,
    assists     INTEGER,
    form        TEXT,
    status      TEXT,
    news        TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    match_id    TEXT PRIMARY KEY,
    league_id   TEXT,
    home        TEXT,
    away        TEXT,
    date        TEXT,
    status      TEXT,
    home_goals  INTEGER,
    away_goals  INTEGER,
    bsd_event_id INTEGER  -- 2026-07-31 추가: match_id는 "홈팀_원정팀_숫자"
    -- 합성id라 BSD의 진짜 숫자 이벤트 id가 아님(실측 확인) — player-stats
    -- 등 event_id로 직접 조회해야 하는 소비처를 위해 별도 저장.
);
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    TEXT REFERENCES matches(match_id),
    minute      INTEGER,
    second      INTEGER,
    team        TEXT,
    player      TEXT,
    type        TEXT,
    x REAL, y REAL, end_x REAL, end_y REAL,
    outcome     TEXT,
    canonical_id TEXT,                      -- 표준 선수 ID (normalizer가 채움)
    extra       TEXT                        -- 잔여 필드 JSON
);
CREATE TABLE IF NOT EXISTS transfers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   TEXT,
    player_name TEXT,
    from_team   TEXT,
    to_team     TEXT,
    league_id   TEXT,
    detected_at TEXT,
    UNIQUE(player_id, detected_at)
);
CREATE TABLE IF NOT EXISTS lineups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id  TEXT,
    league_id   TEXT,
    team        TEXT,
    formation   TEXT,
    coach       TEXT,
    starters    TEXT,                       -- JSON 배열
    updated_at  TEXT,
    UNIQUE(fixture_id, team)
);
CREATE TABLE IF NOT EXISTS injuries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name TEXT,
    team        TEXT,
    status      TEXT,
    news        TEXT,
    source      TEXT DEFAULT 'fpl',         -- 'fpl' | 'api-football'
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS entity_map (   -- ① 정규화 계층의 심장
    source      TEXT,                      -- 'football-data' | 'fpl' | 'bsd' ...
    source_id   TEXT,                      -- 해당 소스에서의 ID 또는 이름 키
    entity_type TEXT,                      -- 'player' | 'team' | 'league'
    canonical_id TEXT,                     -- 표준 ID
    confidence  REAL,                      -- 매칭 신뢰도 (1.0=ID일치, 0.8=이름일치)
    PRIMARY KEY (source, source_id, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_events_match ON events(match_id);
CREATE INDEX IF NOT EXISTS idx_events_player ON events(player);
CREATE INDEX IF NOT EXISTS idx_map_canonical ON entity_map(canonical_id);
"""


def connect(db_path=DB_PATH):
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # 구버전 DB 마이그레이션: canonical_id 컬럼 없으면 추가
    cols = [r[1] for r in conn.execute('PRAGMA table_info(events)')]
    if 'canonical_id' not in cols:
        conn.execute('ALTER TABLE events ADD COLUMN canonical_id TEXT')
    match_cols = [r[1] for r in conn.execute('PRAGMA table_info(matches)')]
    if 'bsd_event_id' not in match_cols:
        conn.execute('ALTER TABLE matches ADD COLUMN bsd_event_id INTEGER')
    inj_cols = [r[1] for r in conn.execute('PRAGMA table_info(injuries)')]
    if inj_cols and 'source' not in inj_cols:
        conn.execute("ALTER TABLE injuries ADD COLUMN source TEXT DEFAULT 'fpl'")
    return conn


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


# ================================================================ 로더
def load_leagues(conn, path='data/master/leagues.json'):
    data = _load_json(path, {})
    for code, info in data.items():
        s = info.get('season', {})
        conn.execute(
            'INSERT OR REPLACE INTO leagues VALUES (?,?,?,?,?,?)',
            (code, info.get('name'), info.get('country'),
             f"{s.get('start', '')}~{s.get('end', '')}", s.get('matchday'),
             info.get('collected_at')))
    return len(data)


def load_teams(conn, path='data/master/teams.json',
               elo_path='data/master/club_elo.json'):
    teams = _load_json(path, {})
    elo = {r['club']: r['elo']
           for r in _load_json(elo_path, {}).get('rankings', [])}
    for tid, t in teams.items():
        conn.execute(
            'INSERT OR REPLACE INTO teams VALUES (?,?,?,?,?,?,?,?,?)',
            (int(tid), t.get('league'), t.get('name'), t.get('short_name'),
             t.get('stadium') or t.get('venue'), t.get('coach'),
             t.get('crest'), elo.get(t.get('name')), t.get('collected_at')))
    return len(teams)


def _norm_team(name):
    """팀명 비교용 정규화: 대소문자/공백/FC·AFC 접미사 제거."""
    import re
    if not name:
        return ''
    n = re.sub(r'\b(FC|AFC|CF)\b', '', name, flags=re.I)
    return re.sub(r'[^a-z0-9]', '', n.lower())


# FPL 등 소스별로 쓰는 축약 팀명 -> 정식명 매핑 (정규화 후 비교)
# football-data.org 정식명 기준. 정규화로 안 잡히는 축약형만 여기 추가.
_TEAM_ALIASES = {
    'mancity': 'manchestercity', 'mcity': 'manchestercity',
    'manutd': 'manchesterunited', 'manu': 'manchesterunited',
    'spurs': 'tottenhamhotspur',
    'nottmforest': 'nottinghamforest', 'forest': 'nottinghamforest',
    'wolves': 'wolverhamptonwanderers',
    'brighton': 'brightonhovealbion',
    'newcastle': 'newcastleunited',
    'leeds': 'leedsunited',
    'palace': 'crystalpalace',
    'westham': 'westhamunited',
}


def load_players(conn, path='data/master/players_pl.json'):
    players = _load_json(path, {})
    # teams 테이블에서 정규화 이름 -> team_id 매핑을 먼저 구성 (FPL 팀명과 매칭용)
    team_lookup = {}
    for row in conn.execute('SELECT team_id, name, short_name FROM teams'):
        tid, name, short_name = row
        for candidate in (name, short_name):
            if candidate:
                team_lookup[_norm_team(candidate)] = tid

    def resolve_team_id(raw_name):
        key = _norm_team(raw_name)
        if key in team_lookup:
            return team_lookup[key]
        aliased = _TEAM_ALIASES.get(key)
        if aliased and aliased in team_lookup:
            return team_lookup[aliased]
        return None

    for pid, p in players.items():
        team_id = resolve_team_id(p.get('team'))
        conn.execute(
            'INSERT OR REPLACE INTO players '
            '(player_id, team_id, name, position, minutes, goals, assists, '
            ' form, status, news, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (f'fpl:{pid}', team_id, p.get('name'), p.get('position'),
             p.get('minutes'), p.get('goals'), p.get('assists'), p.get('form'),
             p.get('status'), p.get('news'), p.get('collected_at')))
        # 부상 뉴스가 있으면 injuries에도 반영
        if p.get('news'):
            conn.execute(
                'INSERT INTO injuries (player_name, team, status, news, updated_at) '
                'VALUES (?,?,?,?,?)',
                (p.get('name'), p.get('team'), p.get('status'),
                 p.get('news'), p.get('collected_at')))
    return len(players)


def load_transfers(conn, path='data/master/transfer_targets.json'):
    transfers = _load_json(path, [])
    n = 0
    for t in transfers:
        try:
            conn.execute(
                'INSERT OR IGNORE INTO transfers '
                '(player_id, player_name, from_team, to_team, league_id, detected_at) '
                'VALUES (?,?,?,?,?,?)',
                (t.get('player_id'), t.get('player_name'), t.get('from_team'),
                 t.get('to_team'), t.get('league'), t.get('detected_at')))
            n += 1
        except sqlite3.Error:
            pass
    return n


def load_fixtures_openfootball(conn, path='data/master/fixtures_openfootball.json'):
    """OpenFootball 폴백 일정/결과 → matches 테이블 (보고서 3.1 지적 해소)."""
    data = _load_json(path, {})
    n = 0
    for code, info in data.items():
        for m in info.get('matches', []):
            score = m.get('score') or [None, None]
            mid = f"of:{code}:{m.get('round','')}:{m.get('home','')}_{m.get('away','')}".replace(' ', '')
            conn.execute(
                # 2026-07-31 수정: matches 스키마에 bsd_event_id 컬럼이 추가돼서
                # (총 9개 컬럼) 물음표도 9개로 맞춰야 함 — 이 함수를 안 고쳤으면
                # SQL "컬럼 개수 불일치" 에러로 이 함수 전체가 죽었을 것.
                'INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?,?)',
                (mid, code, m.get('home'), m.get('away'), m.get('date'),
                 'FINISHED' if score and score[0] is not None else 'SCHEDULED',
                 score[0] if score else None, score[1] if score else None,
                 None))  # OpenFootball 소스엔 BSD 이벤트 id가 없음(당연히 None)
            n += 1
    return n


def load_injuries_af(conn, path='data/master/injuries_af.json'):
    """API-Football 전용 부상 피드 → injuries (source='api-football')."""
    data = _load_json(path, {})
    for rec in data.values():
        conn.execute(
            'INSERT INTO injuries (player_name, team, status, news, source, updated_at) '
            'VALUES (?,?,?,?,?,?)',
            (rec.get('player_name'), rec.get('team'), rec.get('type'),
             rec.get('reason'), 'api-football', rec.get('collected_at')))
    return len(data)


def load_lineups(conn, path='data/master/lineups.json'):
    """⚠️ 2026-07-23: 이 파일 경로를 collect_lineups.py가 다른 스키마(리그별
    리스트)로 잠깐 같이 쓰다가 파이프라인 전체를 크래시시킨 적이 있다
    (AttributeError: 'list' object has no attribute 'get' — data.values()가
    dict 레코드가 아니라 리스트를 내놓아서). collect_lineups.py 쪽은
    lineups_bsd.json으로 개명해서 근본 원인은 없앴지만, 이 로더 자체도
    dict가 아닌 값이 오면 죽지 않고 건너뛰도록 방어한다 — 파일 하나의 스키마
    문제가 db.py 전체를(그리고 이 뒤에 실행되는 모든 스텝을) 죽이는 구조는
    이번 일로 위험하다는 게 실측 확인됐다."""
    data = _load_json(path, {})
    if not isinstance(data, dict):
        return 0
    n = 0
    for rec in data.values():
        if not isinstance(rec, dict):
            continue
        conn.execute(
            'INSERT OR REPLACE INTO lineups '
            '(fixture_id, league_id, team, formation, coach, starters, updated_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (str(rec.get('fixture_id')), rec.get('league'), rec.get('team'),
             rec.get('formation'), rec.get('coach'),
             json.dumps(rec.get('starters', []), ensure_ascii=False),
             rec.get('collected_at')))
        n += 1
    return n


def _fold(name):
    n = unicodedata.normalize('NFKD', name)
    n = ''.join(c for c in n if not unicodedata.combining(c))
    return unicodedata.normalize('NFC', n)


def _norm_kr(name):
    """한글 안전 정규화(다른 수집기들과 동일 원칙 — NFKD로 한글이 자모로
    쪼개지는 걸 NFC로 재조합해서 방지)."""
    if not name:
        return ''
    n = _fold(name)
    n = _re.sub(r'\b(FC|AFC|CF)\b', '', n, flags=_re.I)
    return _re.sub(r'[^a-z가-힣0-9]', '', n.lower())


def _build_kr_team_index():
    """영문/한글 별칭 -> 한글 표준명. EPL_TEAM_MAP/LEAGUE_TEAM_MAPS 임포트가
    실패했으면(위에서 방어) 그냥 빈 인덱스 — 날짜 역조회를 못 할 뿐 db.py
    자체는 정상 동작."""
    index = {}
    for kr, aliases in _EPL_TEAM_MAP.items():
        for a in list(aliases) + [kr]:
            index[_norm_kr(a)] = kr
    for team_map in _ML_TEAM_MAPS.values():
        for kr, aliases in team_map.items():
            for a in list(aliases) + [kr]:
                index.setdefault(_norm_kr(a), kr)
    return index


_KR_TEAM_INDEX = _build_kr_team_index()


def _build_h2h_date_lookup(paths=('data/master/h2h.json',
                                   'data/master/h2h_history_multileague.json')):
    """h2h.json류 파일들(이미 날짜+팀명+스코어+[2026-07-31 추가]진짜 BSD id를
    갖고 있음)에서 (한글팀A, 한글팀B, 팀A골, 팀B골) -> (날짜, BSD id) 매핑을
    만든다. 팀명 순서를 안 가리려고 양방향(A,B)과 (B,A) 둘 다 키로 넣는다."""
    lookup = {}
    for path in paths:
        data = _load_json(path, {})
        if not isinstance(data, dict):
            continue
        for games in data.values():
            if not isinstance(games, list):
                continue
            for g in games:
                date = g.get('date')
                home, away = g.get('home'), g.get('away')
                hg, ag = g.get('homeGoals'), g.get('awayGoals')
                bsd_id = g.get('id')
                if not (date and home and away) or hg is None or ag is None:
                    continue
                lookup[(home, away, hg, ag)] = (date, bsd_id)
                lookup[(away, home, ag, hg)] = (date, bsd_id)  # 반대 방향도(안전망)
    return lookup


def _resolve_date_from_h2h(h2h_lookup, home_raw, away_raw, home_goals, away_goals):
    """이벤트 payload의 home/away(한글이든 영문이든)를 한글 표준명으로 바꿔서
    h2h_lookup에서 (날짜, 진짜BSD id)를 찾는다. 실패하면 (None, None)(기존
    동작과 동일 — 절대 크래시 안 남)."""
    try:
        home_kr = _KR_TEAM_INDEX.get(_norm_kr(home_raw))
        away_kr = _KR_TEAM_INDEX.get(_norm_kr(away_raw))
        if not home_kr or not away_kr:
            return None, None
        return h2h_lookup.get((home_kr, away_kr, home_goals, away_goals), (None, None))
    except Exception:
        return None, None


def load_events(conn, events_dir='data/events'):
    import glob
    n_match = n_ev = 0
    n_date_backfilled = 0
    _diag_printed = False
    h2h_lookup = _build_h2h_date_lookup()
    for path in glob.glob(os.path.join(events_dir, '*.json')):
        payload = _load_json(path, {})
        if not isinstance(payload, dict) or 'events' not in payload:
            continue
        if not _diag_printed:
            # 2026-07-31: matches.date가 전부 비어있는 게 확인돼서(Understat/
            # football-data.co.uk 연동 작업 중 실측 확인) 이벤트 payload에
            # 애초에 date 필드가 없다는 게 드러났다(이 진단으로 확정) — 그래서
            # 아래에서 h2h.json 역조회로 백필한다.
            print(f'[db] [diag] events payload 최상위 키 샘플: {sorted(payload.keys())} '
                  f'· date 필드 값: {payload.get("date")!r} · h2h 역조회용 데이터 '
                  f'{len(h2h_lookup)}건 로드됨', flush=True)
            _diag_printed = True
        mid = os.path.splitext(os.path.basename(path))[0]
        home, away = payload.get('home'), payload.get('away')
        evs = payload['events']
        goals = {home: 0, away: 0}
        for e in evs:
            # 2026-07-30 확정된 근본원인 수정: 이 조건이 원래 type=='Shot'&&
            # outcome=='Goal'이었는데, rehearse_goal_team_probe.py로 실측
            # 확정된 BSD incidents/ 실제 필드는 type=='goal'(소문자, outcome
            # 필드 자체가 없음) — 절대 매치가 안 돼서 모든 경기의 골이 0-0
            # 으로 저장되고 있었다(H2H가 전부 0-0으로만 나오던 진짜 원인).
            if e.get('type') == 'goal':
                goals[e.get('team')] = goals.get(e.get('team'), 0) + 1
        date_val = payload.get('date')
        bsd_event_id = None
        if not date_val:
            # 2026-07-31 추가: payload에 date가 아예 없어서(위 진단으로 확정)
            # h2h.json/h2h_history_multileague.json에서 (팀명,스코어)로
            # 역조회한다. 못 찾으면 그냥 None(기존과 동일 — Understat 등
            # 날짜 필요한 소비처는 그 경기만 건너뛸 뿐 크래시 없음). 같은
            # 김에 진짜 BSD event id도 같이 받아온다(match_id 자체는
            # "홈팀_원정팀_숫자" 합성id라 BSD의 진짜 숫자 id가 아님 — 실측
            # 확인된 사실. player-stats 등 event_id 직접조회가 필요한
            # 소비처를 위해 별도 컬럼에 저장).
            date_val, bsd_event_id = _resolve_date_from_h2h(
                h2h_lookup, home, away, goals.get(home, 0), goals.get(away, 0))
            if date_val:
                n_date_backfilled += 1
        conn.execute('INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?,?)',
                     (mid, payload.get('league'), home, away,
                      date_val, 'FINISHED',
                      goals.get(home, 0), goals.get(away, 0), bsd_event_id))
        conn.execute('DELETE FROM events WHERE match_id = ?', (mid,))
        core = ('minute', 'second', 'team', 'player', 'type',
                'x', 'y', 'end_x', 'end_y', 'outcome')
        for e in evs:
            extra = {k: v for k, v in e.items() if k not in core}
            conn.execute(
                'INSERT INTO events (match_id, minute, second, team, player, '
                'type, x, y, end_x, end_y, outcome, extra) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',   # canonical_id는 normalizer가 후처리
                (mid, e.get('minute'), e.get('second'), e.get('team'),
                 e.get('player'), e.get('type'), e.get('x'), e.get('y'),
                 e.get('end_x'), e.get('end_y'), e.get('outcome'),
                 json.dumps(extra, ensure_ascii=False) if extra else None))
            n_ev += 1
        n_match += 1
    print(f'[db] events 적재: {n_match}경기 중 날짜 h2h역조회로 백필 '
          f'{n_date_backfilled}건', flush=True)
    return n_match, n_ev


def build(db_path=DB_PATH):
    """모든 JSON 산출물을 DB로 적재."""
    conn = connect(db_path)
    with conn:
        stats = {
            'leagues': load_leagues(conn),
            'teams': load_teams(conn),
            'players': load_players(conn),
            'transfers': load_transfers(conn),
        }
        stats['matches'], stats['events'] = load_events(conn)
        stats['of_fixtures'] = load_fixtures_openfootball(conn)
        stats['injuries_af'] = load_injuries_af(conn)
        stats['lineups'] = load_lineups(conn)
    conn.close()
    print(f'[db] 적재 완료 → {db_path} | {stats}')
    return stats


if __name__ == '__main__':
    build()
