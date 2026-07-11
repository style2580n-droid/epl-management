# -*- coding: utf-8 -*-
"""
정규화 계층 (Normalizer) — League/Club/Player Mapper

문제: football-data(선수 ID 44), FPL(ID 233), BSD(ID 'p_9182')가 전부
같은 선수를 다른 ID로 부름 → 소스 간 조인 불가.

해법: entity_map 테이블에 (source, source_id) → canonical_id 매핑을 축적.
  · 1차: 소스 ID가 이미 등록됐으면 그대로 (confidence 1.0)
  · 2차: 정규화된 이름 완전 일치 (confidence 0.9)
  · 3차: 토큰 기반 유사도 매칭 — 'B. Saka' ≈ 'Bukayo Saka' (confidence 0.8)
  · 실패: 새 canonical_id 발급

팀 이름 별칭도 처리: 'Man City' = 'Manchester City FC' 등.
"""
import re
import sqlite3
import unicodedata

import db as dbmod

# 흔한 접미어/불용어 (팀명 정규화)
TEAM_STOPWORDS = {'fc', 'cf', 'afc', 'club', 'de', 'futbol', 'calcio', 'ssc',
                  'ac', 'as', 'rc', 'sc', 'bk', '1899', '1909', '04', '05'}
TEAM_ALIASES = {
    'man city': 'manchester city', 'man united': 'manchester united',
    'man utd': 'manchester united', 'spurs': 'tottenham hotspur',
    'wolves': 'wolverhampton wanderers', 'barca': 'barcelona',
    'atleti': 'atletico madrid', 'inter': 'internazionale',
    'psg': 'paris saint germain', 'bayern': 'bayern munchen',
}


SPECIAL_LATIN = str.maketrans({
    'ø': 'o', 'Ø': 'o', 'đ': 'd', 'Đ': 'd', 'ð': 'd', 'Ð': 'd',
    'þ': 'th', 'Þ': 'th', 'ł': 'l', 'Ł': 'l', 'ß': 'ss',
    'æ': 'ae', 'Æ': 'ae', 'œ': 'oe', 'Œ': 'oe', 'ı': 'i',
})


def normalize_name(name):
    """악센트 제거 → 소문자 → 특수문자 제거 → 공백 정리.
    NFKD로 분해되지 않는 특수 라틴 문자(Ø, Ł, ß 등)는 변환표로 처리."""
    if not name:
        return ''
    s = str(name).translate(SPECIAL_LATIN)
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9\s]", ' ', s).lower()
    return re.sub(r'\s+', ' ', s).strip()


def normalize_team(name):
    s = normalize_name(name)
    s = TEAM_ALIASES.get(s, s)
    tokens = [t for t in s.split() if t not in TEAM_STOPWORDS]
    return ' '.join(tokens) or s


def _name_tokens(name):
    return set(normalize_name(name).split())


def token_similarity(a, b):
    """이니셜 축약('B. Saka')을 고려한 토큰 유사도 0~1."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    la, lb = normalize_name(a).split(), normalize_name(b).split()
    if not la or not lb or la[-1] != lb[-1]:
        return 0.0
    matched = 0
    for x in la[:-1]:
        for y in lb[:-1]:
            if x == y or (len(x) == 1 and y.startswith(x)) \
                    or (len(y) == 1 and x.startswith(y)):
                matched += 1
                break
    total = max(len(la), len(lb)) - 1
    return 1.0 if total == 0 else 0.6 + 0.4 * (matched / total)


class Normalizer:
    def __init__(self, conn=None):
        self.conn = conn or dbmod.connect()
        self._own = conn is None

    def close(self):
        if self._own:
            self.conn.close()

    # ---------------------------------------------------------------- 조회
    def _lookup(self, source, source_id, etype):
        row = self.conn.execute(
            'SELECT canonical_id FROM entity_map '
            'WHERE source=? AND source_id=? AND entity_type=?',
            (source, str(source_id), etype)).fetchone()
        return row[0] if row else None

    def _register(self, source, source_id, etype, canonical, conf):
        self.conn.execute(
            'INSERT OR REPLACE INTO entity_map VALUES (?,?,?,?,?)',
            (source, str(source_id), etype, canonical, conf))

    def _canonicals(self, etype):
        """표준ID → 대표 이름 사전 (이름 일치 탐색용)."""
        rows = self.conn.execute(
            "SELECT DISTINCT canonical_id FROM entity_map WHERE entity_type=?",
            (etype,)).fetchall()
        return [r[0] for r in rows]

    # ---------------------------------------------------------------- 팀
    def canonical_team(self, source, source_id, name):
        found = self._lookup(source, source_id, 'team')
        if found:
            return found
        norm = normalize_team(name)
        canonical = f'team:{norm.replace(" ", "_")}'
        # 동일 정규화 팀이 이미 있으면 그 ID 재사용
        existing = self.conn.execute(
            'SELECT canonical_id FROM entity_map '
            "WHERE entity_type='team' AND canonical_id=?",
            (canonical,)).fetchone()
        conf = 0.9 if existing else 1.0
        self._register(source, source_id, 'team', canonical, conf)
        return canonical

    # ---------------------------------------------------------------- 선수
    def canonical_player(self, source, source_id, name):
        found = self._lookup(source, source_id, 'player')
        if found:
            return found
        norm = normalize_name(name)
        exact = f'player:{norm.replace(" ", "_")}'
        # 2차: 완전 일치
        existing = self.conn.execute(
            "SELECT canonical_id FROM entity_map "
            "WHERE entity_type='player' AND canonical_id=?",
            (exact,)).fetchone()
        if existing:
            self._register(source, source_id, 'player', exact, 0.9)
            return exact
        # 3차: 유사도 매칭 (이니셜 축약 대응)
        best, best_sim = None, 0.0
        for cand in self._canonicals('player'):
            cand_name = cand.split(':', 1)[1].replace('_', ' ')
            sim = token_similarity(name, cand_name)
            if sim > best_sim:
                best, best_sim = cand, sim
        if best and best_sim >= 0.8:
            self._register(source, source_id, 'player', best, round(best_sim, 2))
            return best
        # 신규 발급
        self._register(source, source_id, 'player', exact, 1.0)
        return exact

    # ------------------------------------------------------------ 일괄 구축
    def build_from_db(self):
        """DB에 적재된 소스 데이터로 매핑 테이블 일괄 구축."""
        n = 0
        with self.conn:
            for tid, name in self.conn.execute(
                    'SELECT team_id, name FROM teams WHERE name IS NOT NULL'):
                self.canonical_team('football-data', tid, name)
                n += 1
            for pid, name in self.conn.execute(
                    'SELECT player_id, name FROM players WHERE name IS NOT NULL'):
                src, sid = pid.split(':', 1) if ':' in pid else ('unknown', pid)
                self.canonical_player(src, sid, name)
                n += 1
            for (pid, pname) in self.conn.execute(
                    'SELECT DISTINCT player_id, player_name FROM transfers'):
                if pname:
                    self.canonical_player('football-data', pid, pname)
                    n += 1
        print(f'[normalizer] 엔티티 {n}건 매핑 완료')
        return n

    def link_events(self):
        """② 업그레이드: events.canonical_id 채우기.
        이벤트의 선수 이름을 entity_map 표준 ID에 연결해
        'B. Saka'의 이벤트와 FPL 'Bukayo Saka' 스탯이 DB에서 조인되게 함."""
        names = [r[0] for r in self.conn.execute(
            'SELECT DISTINCT player FROM events '
            'WHERE player IS NOT NULL AND canonical_id IS NULL')]
        linked = 0
        with self.conn:
            for name in names:
                cid = self.canonical_player('event', f'name:{normalize_name(name)}',
                                            name)
                self.conn.execute(
                    'UPDATE events SET canonical_id=? '
                    'WHERE player=? AND canonical_id IS NULL', (cid, name))
                linked += 1
        print(f'[normalizer] 이벤트 선수 {linked}명 표준 ID 연결')
        return linked


if __name__ == '__main__':
    nz = Normalizer()
    nz.build_from_db()
    nz.link_events()
    nz.close()
