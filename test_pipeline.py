# -*- coding: utf-8 -*-
"""파이프라인 회귀 테스트 — push마다 CI에서 실행 (pytest)."""
import json
import time
import os
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import impact_engine as ie
import transfer_impact as ti
from kloppy_adapter import convert_statsbomb
from normalizer import normalize_name, normalize_team, token_similarity


def sec(m, s):
    return {'minute': m, 'second': s}


# ================================================================ xG 모델
def test_xg_monotonicity():
    m = ie.XGModel()
    close, far = m.predict(114, 40), m.predict(85, 40)
    wide = m.predict(114, 70)
    assert 0 < far < close < 1
    assert wide < close


def test_xg_penalty_fixed():
    assert ie.XGModel().predict(109, 40, penalty=True) == 0.76


def test_xg_fit_converges():
    shots = [{'x': 115, 'y': 40, 'goal': 1}] * 20 + \
            [{'x': 80, 'y': 40, 'goal': 0}] * 60
    m = ie.XGModel().fit(shots, epochs=80)
    assert m.predict(115, 40) > m.predict(80, 40)


def test_xg_load_fallback(tmp_path):
    m = ie.XGModel.load(str(tmp_path / 'none.json'))
    assert isinstance(m, ie.XGModel)


def test_xg_load_trained(tmp_path):
    p = tmp_path / 'xg.json'
    p.write_text(json.dumps({'w0': -1.0, 'w_dist': -0.1,
                             'w_angle': 1.0, 'w_setpiece': -0.2}))
    m = ie.XGModel.load(str(p))
    assert m.w0 == -1.0


# ============================================================ 지표 엔진
@pytest.fixture
def match_events():
    return [
        {'type': 'Pass', 'team': 'H', 'player': 'A', 'x': 30, 'y': 40,
         'end_x': 60, 'end_y': 40, 'outcome': 'Complete', **sec(5, 0)},
        {'type': 'Pass', 'team': 'H', 'player': 'B', 'x': 60, 'y': 40,
         'end_x': 85, 'end_y': 42, 'outcome': 'Complete', **sec(5, 6)},
        {'type': 'Shot', 'team': 'H', 'player': 'C', 'x': 110, 'y': 40,
         'outcome': 'Goal', 'shot_end_y': 42, 'shot_end_z': 1.0, **sec(5, 10)},
        {'type': 'Pass', 'team': 'A', 'player': 'X', 'x': 20, 'y': 40,
         'end_x': 40, 'end_y': 40, 'outcome': 'Incomplete', **sec(10, 0)},
        {'type': 'Interception', 'team': 'H', 'player': 'A', 'x': 85, 'y': 40,
         **sec(12, 0)},
    ]


def test_core_metrics(match_events):
    r = ie.compute_match_metrics(match_events, 'H', 'A')
    h = r['teams']['H']
    assert h['goals'] == 1 and h['xG'] > 0
    assert h['high_turnovers'] == 1
    assert r['teams']['A']['xGA'] == h['xG']
    assert h['clean_sheet'] == 1


def test_ppda_zone_based(match_events):
    r = ie.compute_match_metrics(match_events, 'H', 'A')
    # 상대(A) 자진영 40% 패스 1건 / H의 상대진영 40% 수비 1건 → PPDA 1.0
    assert r['teams']['H']['PPDA'] == 1.0


def test_buildup_and_counter():
    evs = [
        {'type': 'Pass', 'team': 'H', 'player': 'A', 'x': 20, 'y': 40,
         'end_x': 50, 'end_y': 40, 'outcome': 'Complete', **sec(1, 0)},
        {'type': 'Pass', 'team': 'H', 'player': 'B', 'x': 50, 'y': 40,
         'end_x': 85, 'end_y': 40, 'outcome': 'Complete', **sec(1, 8)},
    ]
    r = ie.compute_match_metrics(evs, 'H', 'A')
    assert r['teams']['H']['buildup_success_pct'] == 100.0


def test_physical_proxy_caps():
    evs = [
        {'type': 'Pass', 'team': 'H', 'player': 'A', 'x': 0, 'y': 0,
         'end_x': 10, 'end_y': 0, 'outcome': 'Complete', **sec(0, 0)},
        {'type': 'Pass', 'team': 'H', 'player': 'A', 'x': 120, 'y': 80,
         'end_x': 110, 'end_y': 80, 'outcome': 'Complete', **sec(0, 1)},
    ]
    r = ie.compute_match_metrics(evs, 'H', 'A')
    # 1초에 144유닛 이동 → 11m/s 상한 적용 = 39.6km/h
    assert r['players']['A']['top_speed_est_kmh'] <= 39.6


# ============================================================ 정규화
def test_name_normalization():
    assert normalize_name('Ødegaard, Martin!') == 'odegaard martin'
    assert normalize_team('Manchester City FC') == 'manchester city'
    assert normalize_team('Man City') == 'manchester city'


def test_token_similarity_initials():
    assert token_similarity('B. Saka', 'Bukayo Saka') >= 0.8
    assert token_similarity('B. Saka', 'Bukayo Vieira') == 0.0


def test_normalizer_db(tmp_path):
    import db as dbmod
    from normalizer import Normalizer
    conn = dbmod.connect(str(tmp_path / 't.db'))
    nz = Normalizer(conn)
    c1 = nz.canonical_player('fpl', '1', 'Bukayo Saka')
    c2 = nz.canonical_player('bsd', 'p9', 'B. Saka')
    assert c1 == c2
    conn.commit()
    conn.close()


# ============================================================ DB
def test_db_events_canonical(tmp_path, monkeypatch, match_events):
    import db as dbmod
    from normalizer import Normalizer
    monkeypatch.chdir(tmp_path)
    os.makedirs('data/events')
    os.makedirs('data/master')
    json.dump({'home': 'H', 'away': 'A', 'events': match_events},
              open('data/events/H_A_1.json', 'w'))
    dbmod.build('data/t.db')
    conn = dbmod.connect('data/t.db')
    Normalizer(conn).link_events()
    conn.commit()
    row = conn.execute(
        "SELECT canonical_id FROM events WHERE player='A' LIMIT 1").fetchone()
    assert row[0] and row[0].startswith('player:')
    conn.close()


# ============================================================ 시각화/어댑터
def test_viz_valid_svg(tmp_path, match_events):
    import viz_engine as viz
    svg = viz.shot_map(match_events, 'H')
    ET.fromstring(svg)
    assert 'ff5252' in svg  # 골 마커


def test_kloppy_fallback():
    raw = [{'type': {'name': 'Shot'}, 'team': {'name': 'H'},
            'player': {'name': 'C'}, 'location': [110, 40],
            'minute': 5, 'second': 0,
            'shot': {'statsbomb_xg': 0.3, 'outcome': {'name': 'Goal'},
                     'end_location': [120, 41, 1.0],
                     'type': {'name': 'Open Play'}}}]
    events, engine = convert_statsbomb(raw)
    assert events[0]['xg'] == 0.3 and events[0]['outcome'] == 'Goal'


# ============================================================ 이적 임팩트
def test_transfer_impact_math():
    s = ti.transfer_impact_score({'xG': 0.5}, {'xG': 0.8}, ('xG',))
    assert s['metrics']['xG']['change_pct'] == 60.0


def test_consistency_rating():
    assert ti.consistency_rating([1.0, 1.0, 1.0]) == 100.0
    assert ti.consistency_rating([5.0]) is None


# ============================================================ 수집·통합 경로 (보고서 3.3)
def test_registry_keyless_sources(monkeypatch):
    """키 없이도 무인증 소스는 항상 활성이어야 함."""
    for k in ('FOOTBALL_DATA_API_KEY', 'BSD_API_KEY', 'API_FOOTBALL_KEY',
              'HIGHLIGHTLY_KEY', 'APIFOOTBALL_COM_KEY', 'RAPIDAPI_KEY'):
        monkeypatch.delenv(k, raising=False)
    from api_clients import build_registry
    reg = build_registry()
    for name in ('openfootball', 'statsbomb', 'clubelo', 'fpl',
                 'football-data-couk', 'thesportsdb'):
        assert name in reg, f'{name} 무인증 소스가 비활성됨'


def test_openfootball_fallback_to_db(tmp_path, monkeypatch):
    """보고서 3.1: 폴백 일정이 DB matches까지 도달하는지 (수집→적재 관통)."""
    monkeypatch.chdir(tmp_path)
    os.makedirs('data/master')
    import collectors as C
    import db as dbmod

    class MockOF:
        def season(self, code):
            return {'name': 'Premier League 2025/26', 'matches': [
                {'round': 'Matchday 1', 'date': '2025-08-16',
                 'team1': 'Arsenal', 'team2': 'Chelsea', 'score': {'ft': [2, 1]}},
                {'round': 'Matchday 2', 'date': '2025-08-23',
                 'team1': 'Chelsea', 'team2': 'Spurs', 'score': None},
            ]}, True

    lc = C.LeagueCollector({'openfootball': MockOF()})
    lc.fd = None
    lc.of = MockOF()
    lc.run(['PL'])
    assert os.path.exists('data/master/fixtures_openfootball.json')
    dbmod.build('data/t.db')
    conn = dbmod.connect('data/t.db')
    rows = conn.execute("SELECT status, home_goals FROM matches "
                        "WHERE league_id='PL' ORDER BY date").fetchall()
    conn.close()
    assert rows[0] == ('FINISHED', 2) and rows[1][0] == 'SCHEDULED'


def test_injury_collector_and_loader(tmp_path, monkeypatch):
    """보고서 3.1: API-Football 부상 피드 수집→DB 적재 관통."""
    monkeypatch.chdir(tmp_path)
    os.makedirs('data/master')
    import collectors as C
    import db as dbmod

    class MockAF:
        def injuries(self, league_id, season):
            return {'response': [{
                'player': {'id': 7, 'name': 'Gabriel Jesus',
                           'type': 'Missing Fixture', 'reason': 'Knee Injury'},
                'team': {'name': 'Arsenal'},
                'fixture': {'date': '2026-07-12'}}]}, True

    C.InjuryCollector({'api-football': MockAF()}).run(['PL'])
    dbmod.build('data/t.db')
    conn = dbmod.connect('data/t.db')
    row = conn.execute("SELECT player_name, news, source FROM injuries "
                       "WHERE source='api-football'").fetchone()
    conn.close()
    assert row == ('Gabriel Jesus', 'Knee Injury', 'api-football')


def test_merge_includes_all_collector_outputs(tmp_path, monkeypatch):
    """보고서 3.2: club_elo/match_stats/live_scores/fixtures/부상/라인업 병합."""
    monkeypatch.chdir(tmp_path)
    import merge_artifacts as ma
    files = {
        'master/club_elo.json': {'rankings': [{'club': 'Arsenal', 'elo': 1985}]},
        'master/match_stats.json': {'m1': {'statistics': {}}},
        'master/live_scores.json': {'matches': [{'id': 1}]},
        'master/fixtures_openfootball.json': {'PL': {'matches': []}},
        'master/injuries_af.json': {'7': {'player_name': 'X'}},
        'master/lineups.json': {'f1:t1': {'formation': '4-3-3'}},
    }
    os.makedirs('art/data-PL/master')
    for rel, obj in files.items():
        with open(f'art/data-PL/{rel}', 'w') as f:
            json.dump(obj, f)
    ma.merge('art', 'out')
    ma.merge('art', 'out')   # 멱등성
    for rel in files:
        assert os.path.exists(f'out/{rel}'), f'{rel} 병합 누락'
        with open(f'out/{rel}') as f:
            assert json.load(f), f'{rel} 내용 유실'


def test_fetcher_304_and_state(tmp_path, monkeypatch):
    """조건부 요청: 200에서 ETag 저장 → 다음 요청에 If-None-Match 전송 → 304 처리."""
    monkeypatch.chdir(tmp_path)
    from incremental_fetcher import IncrementalFetcher

    calls = []

    class Resp:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.headers = headers or {}
        def json(self):
            return {'ok': True}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(headers.copy())
        if 'If-None-Match' in headers:
            return Resp(304)
        return Resp(200, {'ETag': 'abc123'})

    import incremental_fetcher as inc
    monkeypatch.setattr(inc.requests, 'get', fake_get)
    f = IncrementalFetcher('https://x.test', state_file='data/state.json')
    data, updated = f.fetch_with_cache('ep')
    assert updated and data == {'ok': True}
    f2 = IncrementalFetcher('https://x.test', state_file='data/state.json')
    data2, updated2 = f2.fetch_with_cache('ep')
    assert not updated2 and data2 is None
    assert calls[1].get('If-None-Match') == 'abc123'


# ============================================================ 최종 업그레이드 5종
def test_af_quota_budget(tmp_path, monkeypatch):
    """API-Football 일일 예산: 상한 도달 시 호출 차단."""
    monkeypatch.chdir(tmp_path)
    os.environ['API_FOOTBALL_KEY'] = 'test'
    import importlib
    import api_clients
    importlib.reload(api_clients)
    c = api_clients.APIFootballClient()
    c.DAILY_BUDGET = 2
    calls = []
    monkeypatch.setattr(api_clients.BaseClient, 'get',
                        lambda self, ep, params=None: (calls.append(ep) or ({'response': []}, True)))
    assert c.get('injuries')[1] is True
    assert c.get('injuries')[1] is True
    data, ok = c.get('injuries')          # 3번째 → 차단
    assert ok is False and len(calls) == 2
    os.environ.pop('API_FOOTBALL_KEY', None)
    importlib.reload(api_clients)


def test_season_per90(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('data/metrics'); os.makedirs('data/master')
    json.dump({'teams': {'H': {'xG': 2.0}}, 'players': {
        'Bukayo Saka': {'_team': 'H', 'xG': 0.5, 'SCA': 3, 'goals': 1}}},
        open('data/metrics/m1_metrics.json', 'w'))
    json.dump({'teams': {'H': {'xG': 1.0}}, 'players': {
        'Bukayo Saka': {'_team': 'H', 'xG': 0.4, 'SCA': 2}}},
        open('data/metrics/m2_metrics.json', 'w'))
    json.dump({'1': {'name': 'Bukayo Saka', 'minutes': 180}},
              open('data/master/players_pl.json', 'w'))
    import season_aggregator as sa
    players, teams = sa.build()
    p = players['Bukayo Saka']
    assert p['matches'] == 2 and p['totals']['xG'] == 0.9
    assert p['per90']['xG'] == 0.45          # 0.9 / 180분 * 90
    assert 'per90 (FPL' in p['per90_basis']
    assert teams['H']['per_match']['xG'] == 1.5


def test_season_per_match_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('data/metrics')
    json.dump({'teams': {}, 'players': {'Unknown Kid': {'xG': 0.6, '_team': 'H'}}},
              open('data/metrics/m1_metrics.json', 'w'))
    import season_aggregator as sa
    players, _ = sa.build()
    assert 'per_match' in players['Unknown Kid']['per90_basis']


def test_big_match_performance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('data/metrics'); os.makedirs('data/master')
    json.dump({'rankings': [{'club': 'Real Madrid', 'elo': 2000}]},
              open('data/master/club_elo.json', 'w'))
    json.dump({'teams': {'Arsenal': {}, 'Real Madrid': {}}, 'players': {
        'Saka': {'_team': 'Arsenal', 'VAEP': 0.8}}},
        open('data/metrics/big_metrics.json', 'w'))
    json.dump({'teams': {'Arsenal': {}, 'Burnley': {}}, 'players': {
        'Saka': {'_team': 'Arsenal', 'VAEP': 0.4}}},
        open('data/metrics/small_metrics.json', 'w'))
    import transfer_impact as ti2
    bmp = ti2.big_match_performance(top_n=1)
    assert bmp['Saka'] == 2.0                 # 빅매치 0.8 / 일반 0.4


def test_retention_archives_old(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('data/events'); os.makedirs('data/metrics')
    old_f = 'data/events/old_match.json'
    new_f = 'data/events/new_match.json'
    keep_f = 'data/metrics/season_players.json'
    for f in (old_f, new_f, keep_f):
        json.dump({}, open(f, 'w'))
    old_time = time.time() - 60 * 86400
    os.utime(old_f, (old_time, old_time))
    os.utime(keep_f, (old_time, old_time))    # 보존 접두어 → 아카이브 제외
    import retention
    moved = retention.archive_old_files(retention_days=30)
    assert moved == 1
    assert not os.path.exists(old_f) and os.path.exists(new_f)
    assert os.path.exists(keep_f)
    import zipfile, glob as g
    z = zipfile.ZipFile(g.glob('data/archive/*.zip')[0])
    assert 'events/old_match.json' in z.namelist()


def test_smoke_path_checker():
    from smoke_test import _has_path
    data = {'teams': [{'id': 1, 'squad': [{'id': 9}]}]}
    assert _has_path(data, 'teams[].id')
    assert _has_path(data, 'teams[].squad[].id')
    assert not _has_path(data, 'teams[].missing')


# ============================================================ 2키 로테이션
def test_fd_key_rotation(monkeypatch):
    monkeypatch.delenv('FOOTBALL_DATA_API_KEY', raising=False)
    os.environ['FOOTBALL_DATA_API_KEY1'] = 'k1'
    os.environ['FOOTBALL_DATA_API_KEY2'] = 'k2'
    import importlib
    import api_clients
    importlib.reload(api_clients)
    c = api_clients.FootballDataClient()
    used = []
    monkeypatch.setattr(
        c.fetcher, 'fetch_with_cache',
        lambda ep, params=None: (used.append(c.fetcher.headers['X-Auth-Token'])
                                 or ({}, True)))
    for _ in range(4):
        c.get('x')
    assert used == ['k1', 'k2', 'k1', 'k2']
    for k in ('FOOTBALL_DATA_API_KEY1', 'FOOTBALL_DATA_API_KEY2'):
        os.environ.pop(k, None)
    importlib.reload(api_clients)


def test_af_dual_key_budget(tmp_path, monkeypatch):
    """2키 × 예산1 = 합산 2회 후 차단, 키가 순서대로 소진되는지."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('API_FOOTBALL_KEY', raising=False)
    os.environ['API_FOOTBALL_KEY1'] = 'a1'
    os.environ['API_FOOTBALL_KEY2'] = 'a2'
    import importlib
    import api_clients
    importlib.reload(api_clients)
    c = api_clients.APIFootballClient()
    c.DAILY_BUDGET = 1
    used = []
    monkeypatch.setattr(
        api_clients.BaseClient, 'get',
        lambda self, ep, params=None: (
            used.append(self.fetcher.headers['x-apisports-key']) or ({}, True)))
    assert c.get('e')[1] and c.get('e')[1]
    assert c.get('e')[1] is False            # 합산 소진
    assert used == ['a1', 'a2']
    for k in ('API_FOOTBALL_KEY1', 'API_FOOTBALL_KEY2'):
        os.environ.pop(k, None)
    importlib.reload(api_clients)


def test_live_collector_api_football(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs('data/master')
    import collectors as C

    class MockAF:
        def live_fixtures(self):
            return {'response': [
                {'fixture': {'id': 1, 'status': {'elapsed': 62}},
                 'league': {'id': 39, 'name': 'Premier League'},
                 'teams': {'home': {'name': 'Arsenal'}, 'away': {'name': 'Chelsea'}},
                 'goals': {'home': 2, 'away': 0}},
                {'fixture': {'id': 2, 'status': {'elapsed': 30}},
                 'league': {'id': 999, 'name': 'Other League'},   # 필터 대상
                 'teams': {'home': {'name': 'X'}, 'away': {'name': 'Y'}},
                 'goals': {'home': 0, 'away': 0}},
            ]}, True

    C.LiveCollector({'api-football': MockAF()}).run()
    data = json.load(open('data/master/live_scores.json'))
    assert data['source'] == 'api-football'
    assert len(data['matches']) == 1 and data['matches'][0]['home'] == 'Arsenal'
