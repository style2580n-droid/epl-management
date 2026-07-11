# -*- coding: utf-8 -*-
"""
카테고리별 수집기 (새 틀 2장 '카테고리별 상세 수집 항목 및 API 매핑')

  2.1 LeagueCollector   : 리그명/국가/시즌/라운드/순위
  2.2 TeamCollector     : 구단명/ID/경기장/로고/메타
  2.3 PlayerCollector   : 성명/나이/국적/포지션/출전/폼 (FPL)
  2.4 TransferDetector  : 스쿼드 diff 기반 이적 감지 (이전 팀→현재 팀)
  EventCollector        : BSD 라이브/상세 이벤트 → 지표 엔진 입력용 정규화

각 수집기는 data/master/ 아래에 카테고리별 JSON을 적재한다.
"""
import os
import json
from datetime import datetime, timezone

from api_clients import build_registry

DATA_DIR = 'data/master'
EVENTS_DIR = 'data/events'
LEAGUES = ['PL', 'PD', 'BL1', 'SA', 'FL1']  # football-data.org 공식 코드
# API-Football 리그 ID 매핑 (부상/라인업 피드용)
AF_LEAGUE_IDS = {'PL': 39, 'PD': 140, 'BL1': 78, 'SA': 135, 'FL1': 61}


def _season_year():
    """유럽 시즌 기준 연도 (8월 개막: 7월 이후면 당해, 이전이면 전년)."""
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def _save(path, obj):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=4, ensure_ascii=False)


def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _now():
    return datetime.now(timezone.utc).isoformat()


# ================================================================ 2.1 리그
class LeagueCollector:
    """일정·순위: 1순위 football-data.org → 폴백 OpenFootball (문서 5장)."""

    def __init__(self, registry):
        self.fd = registry.get('football-data')
        self.of = registry.get('openfootball')

    def run(self, leagues=LEAGUES):
        if not self.fd:
            print('[league] football-data 비활성 → OpenFootball 폴백')
            return self._run_openfootball(leagues)
        out = _load(f'{DATA_DIR}/leagues.json', {})
        for code in leagues:
            data, updated = self.fd.standings(code)
            if not (updated and data):
                continue
            comp = data.get('competition', {})
            season = data.get('season', {})
            table = []
            for standing in data.get('standings', []):
                if standing.get('type') != 'TOTAL':
                    continue
                for row in standing.get('table', []):
                    table.append({
                        'position': row.get('position'),
                        'team_id': row.get('team', {}).get('id'),
                        'team': row.get('team', {}).get('name'),
                        'played': row.get('playedGames'),
                        'points': row.get('points'),
                        'gd': row.get('goalDifference'),
                    })
            out[code] = {
                'name': comp.get('name'),
                'country': data.get('area', {}).get('name'),
                'season': {'start': season.get('startDate'),
                           'end': season.get('endDate'),
                           'matchday': season.get('currentMatchday')},
                'standings': table,
                'collected_at': _now(),
            }
            print(f'[league] {code} 순위표 {len(table)}팀 수집')
        _save(f'{DATA_DIR}/leagues.json', out)

    def _run_openfootball(self, leagues):
        """OpenFootball raw JSON에서 일정/결과 수집 (CC0, 키 불필요)."""
        if not self.of:
            return
        out = _load(f'{DATA_DIR}/fixtures_openfootball.json', {})
        for code in leagues:
            data, updated = self.of.season(code)
            if not (updated and data):
                continue
            out[code] = {
                'name': data.get('name'),
                'matches': [{'round': m.get('round'), 'date': m.get('date'),
                             'home': m.get('team1'), 'away': m.get('team2'),
                             'score': (m.get('score') or {}).get('ft')}
                            for m in data.get('matches', [])],
                'collected_at': _now(),
            }
            print(f"[league/of] {code} 경기 {len(out[code]['matches'])}건")
        _save(f'{DATA_DIR}/fixtures_openfootball.json', out)


# ============================================================ 팀 전력지수
class EloCollector:
    """ClubElo — 유럽 클럽 Elo 랭킹. 무인증 (문서 3장)."""

    def __init__(self, registry):
        self.elo = registry.get('clubelo')

    def run(self, top_n=100):
        if not self.elo:
            return
        rows, ok = self.elo.rankings_today()
        if not (ok and rows):
            print('[elo] 수집 실패')
            return
        ranked = sorted(rows, key=lambda r: float(r.get('Elo', 0)), reverse=True)
        out = [{'rank': i + 1, 'club': r.get('Club'), 'country': r.get('Country'),
                'elo': round(float(r.get('Elo', 0)), 1)}
               for i, r in enumerate(ranked[:top_n])]
        _save(f'{DATA_DIR}/club_elo.json',
              {'collected_at': _now(), 'rankings': out})
        print(f'[elo] 상위 {len(out)}클럽 Elo 수집')


# ============================================================ 팀 경기통계
class StatsCollector:
    """팀 경기통계+xG: 1순위 Highlightly (문서 5장 '팀 경기통계' 행)."""

    def __init__(self, registry):
        self.hl = registry.get('highlightly')

    def run(self, date_str=None):
        if not self.hl:
            print('[stats] Highlightly 비활성 → 건너뜀')
            return
        from datetime import date
        date_str = date_str or date.today().isoformat()
        data, ok = self.hl.matches(date_str)
        if not (ok and data):
            return
        out = _load(f'{DATA_DIR}/match_stats.json', {})
        for m in (data.get('data') or data.get('matches') or [])[:20]:
            mid = m.get('id')
            stats, ok2 = self.hl.match_statistics(mid)
            if ok2 and stats:
                out[str(mid)] = {'match': m, 'statistics': stats,
                                 'collected_at': _now()}
        _save(f'{DATA_DIR}/match_stats.json', out)
        print(f'[stats] Highlightly 경기통계 {len(out)}경기 누적')


# ============================================================ 라이브 스코어
class LiveCollector:
    """라이브 스코어: 1순위 API-Football live (5대 리그 필터),
    예비 apifootball.com (무료는 챔피언십/리그앙만 — 보조로 강등)."""

    def __init__(self, registry):
        self.af = registry.get('api-football')
        self.afc = registry.get('apifootball-com')

    def run(self):
        if self.af:
            data, ok = self.af.live_fixtures()
            if ok and data:
                wanted = set(AF_LEAGUE_IDS.values())
                matches = [{
                    'fixture_id': f.get('fixture', {}).get('id'),
                    'league': f.get('league', {}).get('name'),
                    'home': f.get('teams', {}).get('home', {}).get('name'),
                    'away': f.get('teams', {}).get('away', {}).get('name'),
                    'score': f.get('goals'),
                    'minute': f.get('fixture', {}).get('status', {}).get('elapsed'),
                } for f in data.get('response', [])
                    if f.get('league', {}).get('id') in wanted]
                _save(f'{DATA_DIR}/live_scores.json',
                      {'collected_at': _now(), 'source': 'api-football',
                       'matches': matches})
                print(f'[live] API-Football 라이브 {len(matches)}경기 수집')
                return
        if self.afc:
            data, ok = self.afc.live_events()
            if ok and isinstance(data, list):
                _save(f'{DATA_DIR}/live_scores.json',
                      {'collected_at': _now(), 'source': 'apifootball-com',
                       'matches': data})
                print(f'[live] apifootball.com(예비) {len(data)}경기 수집')
                return
        print('[live] 활성 라이브 소스 없음 → 건너뜀')


# ================================================================ 2.2 구단
class TeamCollector:
    def __init__(self, registry):
        self.fd = registry.get('football-data')
        self.tsdb = registry.get('thesportsdb')

    def run(self, leagues=LEAGUES):
        if not self.fd:
            print('[team] football-data 비활성 → 건너뜀')
            return
        out = _load(f'{DATA_DIR}/teams.json', {})
        for code in leagues:
            data, updated = self.fd.competition_teams(code)
            if not (updated and data):
                continue
            for t in data.get('teams', []):
                tid = str(t.get('id'))
                rec = {
                    'team_id': t.get('id'),
                    'name': t.get('name'),
                    'short_name': t.get('shortName'),
                    'country': t.get('area', {}).get('name'),
                    'venue': t.get('venue'),
                    'founded': t.get('founded'),
                    'crest': t.get('crest'),
                    'coach': (t.get('coach') or {}).get('name'),
                    'league': code,
                    'collected_at': _now(),
                }
                # TheSportsDB로 경기장 수용 인원/이미지 보강 (새 틀 2.2)
                if self.tsdb:
                    extra, ok = self.tsdb.search_team(t.get('name', ''))
                    if ok and extra and extra.get('teams'):
                        e = extra['teams'][0]
                        rec['stadium'] = e.get('strStadium')
                        rec['stadium_capacity'] = e.get('intStadiumCapacity')
                        rec['badge'] = e.get('strBadge')
                out[tid] = rec
            print(f'[team] {code} 구단 메타 수집 완료')
        _save(f'{DATA_DIR}/teams.json', out)


# ================================================================ 2.3 선수
class PlayerCollector:
    """FPL 부트스트랩으로 PL 선수 상세(폼/부상/출전/시장가치 근사) 수집."""
    POSITIONS = {1: 'GK', 2: 'DF', 3: 'MF', 4: 'FW'}

    def __init__(self, registry):
        self.fpl = registry.get('fpl')

    def run(self):
        if not self.fpl:
            print('[player] FPL 비활성 → 건너뜀')
            return
        data, updated = self.fpl.bootstrap()
        if not (updated and data):
            print('[player] FPL 변경 없음')
            return
        team_names = {t['id']: t['name'] for t in data.get('teams', [])}
        players = {}
        for p in data.get('elements', []):
            players[str(p['id'])] = {
                'name': f"{p.get('first_name', '')} {p.get('second_name', '')}".strip(),
                'team': team_names.get(p.get('team')),
                'position': self.POSITIONS.get(p.get('element_type')),
                'minutes': p.get('minutes'),
                'goals': p.get('goals_scored'),
                'assists': p.get('assists'),
                'form': p.get('form'),
                'status': p.get('status'),          # a=가용, i=부상 등
                'news': p.get('news'),               # 부상 이력 텍스트
                'now_cost': p.get('now_cost'),       # 시장 가치 근사
                'yellow_cards': p.get('yellow_cards'),
                'red_cards': p.get('red_cards'),
                'collected_at': _now(),
            }
        _save(f'{DATA_DIR}/players_pl.json', players)
        print(f'[player] FPL 선수 {len(players)}명 수집')


# ================================================================ 2.4 이적
class TransferDetector:
    """스쿼드 스냅샷 diff — 이전 팀/현재 팀/감지 시각 기록."""

    def __init__(self, registry):
        self.fd = registry.get('football-data')
        self.prev_file = f'{DATA_DIR}/previous_squads.json'
        self.target_file = f'{DATA_DIR}/transfer_targets.json'
        self.previous = _load(self.prev_file, {})
        self.current = {}
        self.new_transfers = []

    @staticmethod
    def _prev_team_id(entry):
        return entry.get('team_id') if isinstance(entry, dict) else entry

    def run(self, leagues=LEAGUES):
        if not self.fd:
            print('[transfer] football-data 비활성 → 건너뜀')
            return []
        for code in leagues:
            data, updated = self.fd.competition_teams(code)
            if not (updated and data):
                print(f'[transfer] {code} 변경 없음/실패 → 건너뜀')
                continue
            for team in data.get('teams', []):
                for player in team.get('squad', []):
                    p_id = str(player['id'])
                    self.current[p_id] = {
                        'team_id': team.get('id'),
                        'team_name': team.get('name', 'Unknown'),
                        'player_name': player.get('name', 'Unknown'),
                        'league': code,
                    }
                    prev = self.previous.get(p_id)
                    prev_id = self._prev_team_id(prev)
                    if prev_id is not None and prev_id != team.get('id'):
                        from_name = prev.get('team_name', str(prev_id)) \
                            if isinstance(prev, dict) else str(prev_id)
                        self.new_transfers.append({
                            'player_id': p_id,
                            'player_name': player.get('name', 'Unknown'),
                            'from_team': from_name,
                            'to_team': team.get('name'),
                            'league': code,
                            'detected_at': _now(),
                        })
                        print(f"[transfer] {player.get('name')}: "
                              f"{from_name} → {team.get('name')}")
        self._save_results()
        return self.new_transfers

    def _save_results(self):
        if self.new_transfers:
            targets = _load(self.target_file, [])
            targets.extend(self.new_transfers)
            _save(self.target_file, targets)
        final = self.previous.copy()
        final.update(self.current)
        _save(self.prev_file, final)
        print(f'[transfer] 스냅샷 {len(final)}명 / 신규 이적 {len(self.new_transfers)}건')


# ============================================================= 이벤트 수집
class EventCollector:
    """
    BSD 라이브 이벤트 → impact_engine 입력 스키마로 정규화.
    (BSD 응답 필드명은 계정 승인 후 실제 스키마 확인 필요 — 매핑 테이블만 수정하면 됨)
    """

    def __init__(self, registry):
        self.bsd = registry.get('bsd')

    def run(self):
        if not self.bsd:
            print('[event] BSD 비활성 → 건너뜀')
            return []
        data, updated = self.bsd.live_events()
        if not (updated and data):
            return []
        collected = []
        for ev in data.get('events', []):
            eid = ev.get('id')
            detail, ok = self.bsd.event_detail(eid)
            if not (ok and detail):
                continue
            home = ev.get('home_team', 'Home')
            away = ev.get('away_team', 'Away')
            events = self._normalize(detail)
            path = f'{EVENTS_DIR}/{home}_{away}_{eid}.json'.replace(' ', '')
            _save(path, {'home': home, 'away': away, 'events': events})
            collected.append(path)
            print(f'[event] {home} vs {away} 이벤트 {len(events)}건 저장')
        return collected

    @staticmethod
    def _normalize(detail):
        """BSD 상세 응답 → 표준 이벤트 리스트. 필드명 매핑 지점."""
        out = []
        for e in detail.get('events', detail.get('incidents', [])):
            out.append({
                'type': e.get('type'),
                'team': e.get('team'),
                'player': e.get('player'),
                'x': e.get('x'), 'y': e.get('y'),
                'end_x': e.get('end_x'), 'end_y': e.get('end_y'),
                'outcome': e.get('outcome'),
                'situation': e.get('situation'),
                'minute': e.get('minute'),
                'second': e.get('second'),
                'xg': e.get('xg'),  # BSD가 자체 xG 제공 시 그대로 활용
            })
        return out


# ============================================================ 부상 (전용 피드)
class InjuryCollector:
    """API-Football 전용 부상 피드 → data/master/injuries_af.json (보고서 3.1)."""

    def __init__(self, registry):
        self.af = registry.get('api-football')

    def run(self, leagues=LEAGUES):
        if not self.af:
            print('[injury] API-Football 비활성 → 건너뜀 (FPL 뉴스만 사용)')
            return
        season = _season_year()
        out = _load(f'{DATA_DIR}/injuries_af.json', {})
        for code in leagues:
            lid = AF_LEAGUE_IDS.get(code)
            if not lid:
                continue
            data, ok = self.af.injuries(lid, season)
            if not (ok and data):
                continue
            for item in data.get('response', []):
                p = item.get('player', {})
                out[str(p.get('id'))] = {
                    'player_name': p.get('name'),
                    'team': item.get('team', {}).get('name'),
                    'type': p.get('type'),          # Missing Fixture 등
                    'reason': p.get('reason'),      # 부상 부위/사유
                    'league': code,
                    'fixture_date': item.get('fixture', {}).get('date'),
                    'collected_at': _now(),
                }
            print(f'[injury] {code} 부상 피드 수집')
        _save(f'{DATA_DIR}/injuries_af.json', out)


# ============================================================ 라인업
class LineupCollector:
    """API-Football 당일 경기 라인업 → data/master/lineups.json (보고서 3.1).
    쿼터 보호: 하루 최대 MAX_FIXTURES 경기만."""
    MAX_FIXTURES = 10

    def __init__(self, registry):
        self.af = registry.get('api-football')

    def run(self, leagues=LEAGUES):
        if not self.af:
            print('[lineup] API-Football 비활성 → 건너뜀')
            return
        season = _season_year()
        today = datetime.now(timezone.utc).date().isoformat()
        out = _load(f'{DATA_DIR}/lineups.json', {})
        fetched = 0
        for code in leagues:
            lid = AF_LEAGUE_IDS.get(code)
            if not lid or fetched >= self.MAX_FIXTURES:
                continue
            fx, ok = self.af.fixtures(lid, season, date=today)
            if not (ok and fx):
                continue
            for f in fx.get('response', []):
                if fetched >= self.MAX_FIXTURES:
                    break
                fid = f.get('fixture', {}).get('id')
                lu, ok2 = self.af.lineups(fid)
                if not (ok2 and lu):
                    continue
                for side in lu.get('response', []):
                    out[f"{fid}:{side.get('team', {}).get('id')}"] = {
                        'fixture_id': fid,
                        'league': code,
                        'team': side.get('team', {}).get('name'),
                        'formation': side.get('formation'),
                        'coach': (side.get('coach') or {}).get('name'),
                        'starters': [
                            {'name': s['player'].get('name'),
                             'position': s['player'].get('pos'),
                             'number': s['player'].get('number')}
                            for s in side.get('startXI', [])],
                        'collected_at': _now(),
                    }
                fetched += 1
            print(f'[lineup] {code} 라인업 수집 (누적 {fetched}경기)')
        _save(f'{DATA_DIR}/lineups.json', out)


# =================================================================== main
def main():
    registry = build_registry()
    category = os.getenv('CATEGORY', 'all')
    league = os.getenv('LEAGUE')
    leagues = [league] if league else LEAGUES

    if category in ('all', 'league'):
        LeagueCollector(registry).run(leagues)
    if category in ('all', 'team'):
        TeamCollector(registry).run(leagues)
    if category in ('all', 'player'):
        PlayerCollector(registry).run()
    if category in ('all', 'transfer'):
        TransferDetector(registry).run(leagues)
    if category in ('all', 'event'):
        EventCollector(registry).run()
    if category in ('all', 'elo'):
        EloCollector(registry).run()
    if category in ('all', 'stats'):
        StatsCollector(registry).run()
    if category in ('all', 'live'):
        LiveCollector(registry).run()
    if category in ('all', 'injury'):
        InjuryCollector(registry).run(leagues)
    if category in ('all', 'lineup'):
        LineupCollector(registry).run(leagues)


if __name__ == '__main__':
    main()
