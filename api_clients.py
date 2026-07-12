# -*- coding: utf-8 -*-
"""
다중 API 클라이언트 계층 (새 틀 1.1 무료 API 매트릭스 + 2장 카테고리 매핑)

카테고리 → 주력 API 매핑 (새 틀 2장):
  2.1 리그 데이터   → football-data.org  (+ API-Football 예비)
  2.2 구단 데이터   → TheSportsDB, SportScore
  2.3 선수 데이터   → FPL API, TheSportsDB
  2.4 이적 데이터   → football-data.org 스쿼드 diff (자체 감지)
  이벤트/xG 좌표    → BSD (Bzzoiro)
  xG 모델 학습셋    → StatsBomb Open Data (키 불필요)

각 클라이언트는 IncrementalFetcher를 내장해 조건부 요청을 공유한다.
키가 없는 소스는 등록된 키가 없으면 조용히 비활성화(enabled=False)된다.
"""
import os

from incremental_fetcher import IncrementalFetcher


def _env_keys(base, max_suffix=9):
    """BASE, BASE1, BASE2 ... BASE{max_suffix}까지 자동 탐지해 키 목록 수집
    (중복 제거). 계정을 늘려 BASE3, BASE4 ...를 추가로 등록해도 코드 수정
    없이 자동 인식된다."""
    keys = []
    v = os.getenv(base, '')
    if v and v not in keys:
        keys.append(v)
    for i in range(1, max_suffix + 1):
        v = os.getenv(f'{base}{i}', '')
        if v and v not in keys:
            keys.append(v)
    return keys


class BaseClient:
    name = 'base'
    enabled = True

    def __init__(self, base_url, headers=None):
        self.fetcher = IncrementalFetcher(
            base_url, headers=headers, state_namespace=self.name)

    def get(self, endpoint, params=None):
        return self.fetcher.fetch_with_cache(endpoint, params=params)


# ---------------------------------------------------------------- 2.1 리그
class FootballDataClient(BaseClient):
    """football-data.org — 리그/일정/스쿼드. 분당 10회, GHA IP 차단 없음."""
    name = 'football-data'

    def __init__(self):
        # 다중 계정 지원: KEY / KEY1 / KEY2 / KEY3 ... 라운드로빈 → 실질 분당 한도 x키수
        self.keys = _env_keys('FOOTBALL_DATA_API_KEY')
        self.enabled = bool(self.keys)
        self._i = 0
        super().__init__('https://api.football-data.org/v4',
                         headers={'X-Auth-Token': self.keys[0] if self.keys else ''})

    def get(self, endpoint, params=None):
        if self.keys:
            self.fetcher.headers['X-Auth-Token'] = self.keys[self._i % len(self.keys)]
            self._i += 1
        return super().get(endpoint, params=params)

    def competition_teams(self, league_code):
        return self.get(f'competitions/{league_code}/teams')

    def competition_matches(self, league_code, **params):
        return self.get(f'competitions/{league_code}/matches', params=params)

    def standings(self, league_code):
        return self.get(f'competitions/{league_code}/standings')


# ------------------------------------------------------------ 이벤트/좌표
class BSDClient(BaseClient):
    """BSD (Bzzoiro) — 슛별 xG·좌표·이벤트. Authorization: Token 방식."""
    name = 'bsd'

    def __init__(self):
        token = os.getenv('BSD_API_KEY') or os.getenv('BSD_API_TOKEN', '')
        self.enabled = bool(token)
        super().__init__('https://sports.bzzoiro.com/api/v2',
                         headers={'Authorization': f'Token {token}'})

    def live_events(self):
        return self.get('events/live/')

    def leagues(self, **params):
        return self.get('leagues/', params=params)

    def event_detail(self, event_id):
        return self.get(f'events/{event_id}/')


# ---------------------------------------------------------------- 2.2 구단
class TheSportsDBClient(BaseClient):
    """TheSportsDB — 구단 로고/경기장/선수 프로필. 무료 키 '123' 사용 가능."""
    name = 'thesportsdb'

    def __init__(self):
        key = os.getenv('THESPORTSDB_API_KEY', '123')  # 공식 무료 테스트 키
        super().__init__(f'https://www.thesportsdb.com/api/v1/json/{key}')

    def search_team(self, team_name):
        return self.get('searchteams.php', params={'t': team_name})

    def team_players(self, team_id):
        return self.get('lookup_all_players.php', params={'id': team_id})


class SportScoreClient(BaseClient):
    """SportScore (RapidAPI) — 대량 경기 리스트/라인업. X-RapidAPI-Key 방식."""
    name = 'sportscore'

    def __init__(self):
        key = os.getenv('RAPIDAPI_KEY', '')
        self.enabled = bool(key)
        super().__init__('https://sportscore1.p.rapidapi.com/api/v1', headers={
            'X-RapidAPI-Key': key,
            'X-RapidAPI-Host': 'sportscore1.p.rapidapi.com',
        })

    def events_by_date(self, date_str, sport_id=1):
        return self.get(f'sports/{sport_id}/events/date/{date_str}')

    def lineups(self, event_id):
        return self.get(f'events/{event_id}/lineups')


# ---------------------------------------------------------------- 2.3 선수
class FPLClient(BaseClient):
    """FPL 공식 API — PL 선수 상세(폼/부상/출전). 키 불필요."""
    name = 'fpl'

    def __init__(self):
        super().__init__('https://fantasy.premierleague.com/api')

    def bootstrap(self):
        """전체 선수/팀/포지션 마스터 데이터."""
        return self.get('bootstrap-static/')

    def player_history(self, player_id):
        return self.get(f'element-summary/{player_id}/')


# ------------------------------------------------------------- 학습 데이터
class StatsBombOpenClient(BaseClient):
    """StatsBomb Open Data — GitHub 정적 JSON. xG 모델 학습 핵심. 키 불필요."""
    name = 'statsbomb'

    def __init__(self):
        super().__init__(
            'https://raw.githubusercontent.com/statsbomb/open-data/master/data')

    def competitions(self):
        return self.get('competitions.json')

    def matches(self, competition_id, season_id):
        return self.get(f'matches/{competition_id}/{season_id}.json')

    def events(self, match_id):
        return self.get(f'events/{match_id}.json')


# ================================================================
# 신규 소스 (축구API 총정리 문서 5장 매핑 반영)
# ================================================================
class APIFootballClient(BaseClient):
    """API-Football (100/일) — 라인업 1순위, 선수/이적/부상 백업 (문서 1장).
    일일 쿼터 예산: data/af_quota.json 전역 카운터로 90회 상한 관리
    (부상/라인업/픽스처 수집기가 공유 — 조용한 쿼터 초과 방지)."""
    name = 'api-football'
    DAILY_BUDGET = int(os.getenv('AF_DAILY_BUDGET', '90'))
    QUOTA_FILE = 'data/af_quota.json'

    def __init__(self):
        self.keys = _env_keys('API_FOOTBALL_KEY')
        self.enabled = bool(self.keys)
        super().__init__('https://v3.football.api-sports.io',
                         headers={'x-apisports-key': self.keys[0] if self.keys else ''})

    def live_fixtures(self):
        """라이브 경기 (apifootball.com 대체 — 5대 리그 커버)."""
        return self.get('fixtures', params={'live': 'all'})

    def _quota(self):
        import json as _json
        from datetime import date
        today = date.today().isoformat()
        state = {'date': today, 'count': 0}
        if os.path.exists(self.QUOTA_FILE):
            try:
                with open(self.QUOTA_FILE, 'r', encoding='utf-8') as f:
                    loaded = _json.load(f)
                if loaded.get('date') == today:
                    state = loaded
            except (ValueError, OSError):
                pass
        return state

    def _save_quota(self, state):
        import json as _json
        os.makedirs(os.path.dirname(self.QUOTA_FILE) or '.', exist_ok=True)
        with open(self.QUOTA_FILE, 'w', encoding='utf-8') as f:
            _json.dump(state, f)

    def get(self, endpoint, params=None):
        """키별 예산(DAILY_BUDGET) 분산: 여유 있는 키를 골라 호출.
        2키면 합산 예산 2배 — 전부 소진 시 차단."""
        state = self._quota()
        counts = state.get('counts') or {}
        if 'count' in state:                       # 구버전 상태 마이그레이션
            counts.setdefault('0', state['count'])
        pick = None
        for i in range(len(self.keys)):
            if counts.get(str(i), 0) < self.DAILY_BUDGET:
                pick = i
                break
        if pick is None:
            total = self.DAILY_BUDGET * max(len(self.keys), 1)
            print(f'[api-football] 전 키 합산 예산 {total}회 소진 → 호출 차단')
            return None, False
        counts[str(pick)] = counts.get(str(pick), 0) + 1
        self._save_quota({'date': state['date'], 'counts': counts})
        self.fetcher.headers['x-apisports-key'] = self.keys[pick]
        return super().get(endpoint, params=params)

    def lineups(self, fixture_id):
        return self.get('fixtures/lineups', params={'fixture': fixture_id})

    def injuries(self, league_id, season):
        return self.get('injuries', params={'league': league_id, 'season': season})

    def transfers(self, team_id):
        return self.get('transfers', params={'team': team_id})

    def player_stats(self, player_id, season):
        return self.get('players', params={'id': player_id, 'season': season})

    def fixtures(self, league_id, season, date=None):
        params = {'league': league_id, 'season': season}
        if date:
            params['date'] = date
        return self.get('fixtures', params=params)

    def teams(self, league_id, season):
        """리그 소속 팀 목록(API-Football 자체 team_id 포함)."""
        return self.get('teams', params={'league': league_id, 'season': season})

    def coach(self, team_id):
        """팀의 현재/과거 감독 이력. career 배열 중 end가 없는 항목이 현직."""
        return self.get('coachs', params={'team': team_id})


class ApiFootballComClient(BaseClient):
    """apifootball.com (180/시간, 최대 쿼터) — 라이브 스코어·이벤트 1순위 (문서 5장)."""
    name = 'apifootball-com'

    def __init__(self):
        self.key = os.getenv('APIFOOTBALL_COM_KEY', '')
        self.enabled = bool(self.key)
        super().__init__('https://apiv3.apifootball.com')

    def _q(self, action, **params):
        params.update({'action': action, 'APIkey': self.key})
        return self.get('', params=params)

    def live_events(self):
        return self._q('get_events', match_live='1')

    def events_by_date(self, date_from, date_to):
        return self._q('get_events', **{'from': date_from, 'to': date_to})

    def leagues(self):
        return self._q('get_leagues')


class HighlightlyClient(BaseClient):
    """Highlightly (100/일) — 팀 경기통계+xG 1순위 (문서 5장)."""
    name = 'highlightly'

    def __init__(self):
        key = os.getenv('HIGHLIGHTLY_KEY', '')
        self.enabled = bool(key)
        super().__init__('https://soccer.highlightly.net',
                         headers={'x-rapidapi-key': key})

    def matches(self, date_str, league_id=None):
        params = {'date': date_str}
        if league_id:
            params['leagueId'] = league_id
        return self.get('matches', params=params)

    def match_statistics(self, match_id):
        """팀 단위 통계 — xG/xA 포함 (문서 1장 Highlightly 행)."""
        return self.get(f'statistics/{match_id}')


class ClubEloClient(BaseClient):
    """ClubElo — 유럽 클럽 전력지수. 무인증 공개 API (문서 3장)."""
    name = 'clubelo'

    def __init__(self):
        super().__init__('http://api.clubelo.com')

    def rankings_today(self):
        """당일 전체 클럽 Elo CSV → 파싱해 dict 리스트로 반환."""
        import requests as rq
        from datetime import date
        try:
            r = rq.get(f'{self.fetcher.base_url}/{date.today().isoformat()}',
                       timeout=30)
            if r.status_code != 200:
                return None, False
            lines = r.text.strip().split('\n')
            headers = lines[0].split(',')
            rows = [dict(zip(headers, ln.split(','))) for ln in lines[1:]]
            return rows, True
        except rq.RequestException:
            return None, False


class OpenFootballClient(BaseClient):
    """OpenFootball — GitHub raw JSON. 일정·결과 오픈데이터 대체 (CC0, 차단 위험 0)."""
    name = 'openfootball'
    REPO = {'PL': 'england/2025-26/1-premierleague.json',
            'PD': 'espana/2025-26/1-liga.json',
            'BL1': 'deutschland/2025-26/1-bundesliga.json',
            'SA': 'italy/2025-26/1-seriea.json',
            'FL1': 'france/2025-26/1-ligue1.json'}

    def __init__(self):
        super().__init__('https://openfootball.github.io')

    def season(self, league_code):
        path = self.REPO.get(league_code)
        return self.get(path) if path else (None, False)


class FootballDataCoUkClient(BaseClient):
    """Football-Data.co.uk — 경기결과+기본통계 CSV. 무인증 (문서 3장)."""
    name = 'football-data-couk'
    CODES = {'PL': 'E0', 'PD': 'SP1', 'BL1': 'D1', 'SA': 'I1', 'FL1': 'F1'}

    def __init__(self):
        super().__init__('https://www.football-data.co.uk/mmz4281')

    def season_csv(self, league_code, season='2526'):
        """CSV 원문 반환 (호출측에서 파싱)."""
        import requests as rq
        code = self.CODES.get(league_code)
        if not code:
            return None, False
        try:
            r = rq.get(f'{self.fetcher.base_url}/{season}/{code}.csv', timeout=30)
            return (r.text, True) if r.status_code == 200 else (None, False)
        except rq.RequestException:
            return None, False


# ------------------------------------------------- 신규: PDF 매핑 무료 소스
def build_registry():
    """활성화된 클라이언트만 담은 dict 반환."""
    clients = {}
    for cls in (FootballDataClient, BSDClient, TheSportsDBClient,
                SportScoreClient, FPLClient, StatsBombOpenClient,
                APIFootballClient, ApiFootballComClient, HighlightlyClient,
                OpenFootballClient):
        c = cls()
        if c.enabled:
            clients[c.name] = c
        else:
            print(f'[registry] {c.name}: API 키 미등록 → 비활성화')
    # 무인증 특수 클라이언트 (CSV/무키)
    clients['clubelo'] = ClubEloClient()
    clients['football-data-couk'] = FootballDataCoUkClient()
    return clients


if __name__ == '__main__':
    reg = build_registry()
    print('활성 소스:', list(reg.keys()))
