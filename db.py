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
    away_goals  INTEGER
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
                'INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?)',
                (mid, code, m.get('home'), m.get('away'), m.get('date'),
                 'FINISHED' if score and score[0] is not None else 'SCHEDULED',
                 score[0] if score else None, score[1] if score else None))
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
    data = _load_json(path, {})
    for rec in data.values():
        conn.execute(
            'INSERT OR REPLACE INTO lineups '
            '(fixture_id, league_id, team, formation, coach, starters, updated_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (str(rec.get('fixture_id')), rec.get('league'), rec.get('team'),
             rec.get('formation'), rec.get('coach'),
             json.dumps(rec.get('starters', []), ensure_ascii=False),
             rec.get('collected_at')))
    return len(data)


def load_events(conn, events_dir='data/events'):
    import glob
    n_match = n_ev = 0
    for path in glob.glob(os.path.join(events_dir, '*.json')):
        payload = _load_json(path, {})
        if not isinstance(payload, dict) or 'events' not in payload:
            continue
        mid = os.path.splitext(os.path.basename(path))[0]
        home, away = payload.get('home'), payload.get('away')
        evs = payload['events']
        goals = {home: 0, away: 0}
        for e in evs:
            if e.get('type') == 'Shot' and e.get('outcome') == 'Goal':
                goals[e.get('team')] = goals.get(e.get('team'), 0) + 1
        conn.execute('INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?)',
                     (mid, payload.get('league'), home, away,
                      payload.get('date'), 'FINISHED',
                      goals.get(home, 0), goals.get(away, 0)))
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
