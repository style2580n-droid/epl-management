# -*- coding: utf-8 -*-
"""
6개 리그 통합 예측앱(multi_league_index.html)용 데이터 export.

입력: data/football.db의 matches 테이블 — football_pipeline.yml의
      collect-by-league 매트릭스가 이미 PD/BL1/SA/FL1/ELC/DED를 전부
      수집해서 쌓고 있으므로, EPL(collect_fixtures.py)처럼 BSD를 따로
      호출할 필요 없이 여기서 리그별로 걸러내기만 하면 된다.
      data/master/club_elo.json (ClubElo 랭킹, 여러 리그 커버)도 함께 사용.
      data/master/previous_squads.json (collectors.py의 TransferDetector가
      이적 감지용으로 이미 수집해둔 전체 스쿼드 — SQUADS 요청사항은 새로
      수집할 필요 없이 이 파일을 재활용하면 된다, 2026-07-13 확인).
출력: reports/app_data_multileague.js
      리그별로 window.PIPELINE_DATA_{리그대문자} = {schedule, logos, elo, squads}
      형태로 생성한다. multi_league_index.html의 loadPipelineData()가
      기대하는 정확히 그 형식이다 (schedule: [{home,away,date}], ...).

리그 키(laliga/bundesliga/seriea/ligue1/eredivisie/championship)는
multi_league_index.html의 LEAGUE_ORDER와 반드시 일치해야 한다.

⚠️ 팀명 영문 별칭은 이 세션에서 실제 API 호출 없이 축적된 지식으로 채운
것이라, app_export.py의 20개 EPL 팀명(실전 검증된 값)만큼 신뢰도가 높지
않다. 첫 실행 로그의 "매칭 안 된 팀명" 목록을 반드시 확인해서, 실패한
이름이 있으면 LEAGUE_TEAM_MAPS에 별칭을 추가해야 한다 (추측 금지 원칙 —
실행 결과로 검증).
"""
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

DB_PATH = 'data/football.db'
OUT_PATH = 'reports/app_data_multileague.js'
ELO_PATH = 'data/master/club_elo.json'
BSD_SCHEDULE_PATH = 'data/master/schedule_multileague.json'
BSD_SQUADS_PATH = 'data/master/squads_multileague.json'
XG_PATH = 'data/master/xg_multileague.json'
SQUADS_PATH = 'data/master/previous_squads.json'
INJURIES_PATH = 'data/master/injuries_af.json'
# collect_transfers_bsd.py가 EPL+6개 리그 전부를 스냅샷 diff로 감지해서
# 쌓아두는 파일. team_kr은 수집 시점에 이미 to_kr_league로 변환된 한글
# 팀명이라 여기서 다시 매핑할 필요 없음 (2026-07-16 확인 — 지금까지는
# 이 파일이 수집만 되고 앱에 노출이 안 되고 있었음).
TRANSFERS_PATH = 'data/master/transfers_bsd.json'

# ============================================================ 팀명 매핑 (6개 리그, 116팀)
# 인수인계_요약.md v2의 팀 명단과 1:1 대응. 리그 키는 multi_league_index.html의
# LEAGUE_ORDER(laliga/bundesliga/seriea/ligue1/eredivisie/championship)와 동일.
LEAGUE_TEAM_MAPS = {
    'laliga': {
        '알라베스': ['Deportivo Alavés', 'Alavés', 'Alaves'],
        '아틀레틱 빌바오': ['Athletic Club', 'Athletic Bilbao', 'Athletic Club de Bilbao'],
        '아틀레티코 마드리드': ['Club Atlético de Madrid', 'Atlético de Madrid',
                        'Atlético Madrid', 'Atletico Madrid'],
        '바르셀로나': ['FC Barcelona', 'Barcelona'],
        '셀타 비고': ['RC Celta de Vigo', 'Celta Vigo', 'Celta de Vigo', 'RC Celta'],
        '데포르티보 라코루냐': ['RC Deportivo La Coruña', 'Deportivo La Coruña',
                        'Deportivo de La Coruña', 'RC Deportivo'],
        '엘체': ['Elche CF', 'Elche'],
        '에스파뇰': ['RCD Espanyol de Barcelona', 'RCD Espanyol', 'Espanyol'],
        '헤타페': ['Getafe CF', 'Getafe'],
        '레반테': ['Levante UD', 'Levante'],
        '말라가': ['Málaga CF', 'Malaga CF', 'Malaga'],
        '오사수나': ['CA Osasuna', 'Osasuna'],
        '라싱 산탄데르': ['Real Racing Club de Santander', 'Racing de Santander',
                     'Racing Santander', 'Racing Club de Santander'],
        '라요 바예카노': ['Rayo Vallecano de Madrid', 'Rayo Vallecano'],
        '레알 베티스': ['Real Betis Balompié', 'Real Betis'],
        '레알 마드리드': ['Real Madrid CF', 'Real Madrid'],
        '레알 소시에다드': ['Real Sociedad de Fútbol', 'Real Sociedad'],
        '세비야': ['Sevilla FC', 'Sevilla'],
        '발렌시아': ['Valencia CF', 'Valencia'],
        '비야레알': ['Villarreal CF', 'Villarreal'],
    },
    'bundesliga': {
        '바이에른 뮌헨': ['FC Bayern München', 'Bayern Munich', 'Bayern München',
                     'FC Bayern Munich'],
        '보루시아 도르트문트': ['Borussia Dortmund', 'BVB', 'Borussia Dortmund 09'],
        'RB 라이프치히': ['RB Leipzig'],
        '바이어 레버쿠젠': ['Bayer 04 Leverkusen', 'Bayer Leverkusen', 'Bayer 04'],
        '아인트라흐트 프랑크푸르트': ['Eintracht Frankfurt'],
        'VfB 슈투트가르트': ['VfB Stuttgart'],
        'SC 프라이부르크': ['Sport-Club Freiburg', 'SC Freiburg', 'Freiburg'],
        '1.FC 쾰른': ['1. FC Köln', 'FC Köln', 'Cologne', '1.FC Köln'],
        'TSG 호펜하임': ['TSG 1899 Hoffenheim', 'Hoffenheim', 'TSG Hoffenheim'],
        'FC 아우크스부르크': ['FC Augsburg', 'Augsburg'],
        '우니온 베를린': ['1. FC Union Berlin', 'Union Berlin', '1.FC Union Berlin'],
        '베르더 브레멘': ['SV Werder Bremen', 'Werder Bremen'],
        '1.FSV 마인츠05': ['1. FSV Mainz 05', 'Mainz 05', '1.FSV Mainz 05'],
        '보루시아 묀헨글라드바흐': ['Borussia Mönchengladbach', "Borussia M'gladbach",
                          'Monchengladbach'],
        '함부르크SV': ['Hamburger SV', 'HSV', 'Hamburg SV'],
        '샬케04': ['FC Schalke 04', 'Schalke 04', 'Schalke'],
        'SV 엘버스베르크': ['SV 07 Elversberg', 'Elversberg'],
        'SC 파더보른07': ['SC Paderborn 07', 'Paderborn', 'Paderborn 07'],
    },
    'seriea': {
        '아탈란타': ['Atalanta BC', 'Atalanta'],
        '볼로냐': ['Bologna FC 1909', 'Bologna'],
        '칼리아리': ['Cagliari Calcio', 'Cagliari'],
        '코모': ['Como 1907', 'Como'],
        '피오렌티나': ['ACF Fiorentina', 'Fiorentina'],
        '프로시노네': ['Frosinone Calcio', 'Frosinone'],
        '제노아': ['Genoa CFC', 'Genoa'],
        '인테르': ['FC Internazionale Milano', 'Inter Milan', 'Inter'],
        '유벤투스': ['Juventus FC', 'Juventus'],
        '라치오': ['SS Lazio', 'Lazio'],
        '레체': ['US Lecce', 'Lecce'],
        'AC밀란': ['AC Milan', 'Milan'],
        '몬차': ['AC Monza', 'Monza'],
        '나폴리': ['SSC Napoli', 'Napoli'],
        '파르마': ['Parma Calcio 1913', 'Parma'],
        'AS로마': ['AS Roma', 'Roma'],
        '사수올로': ['US Sassuolo Calcio', 'Sassuolo'],
        '토리노': ['Torino FC', 'Torino'],
        '우디네세': ['Udinese Calcio', 'Udinese'],
        '베네치아': ['Venezia FC', 'Venezia'],
    },
    'ligue1': {
        '파리 생제르맹': ['Paris Saint-Germain FC', 'PSG', 'Paris Saint Germain'],
        'AS 모나코': ['AS Monaco FC', 'AS Monaco', 'Monaco'],
        'ESTAC 트루아': ['Espérance Sportive Troyes Aube Champagne', 'ESTAC Troyes',
                     'Troyes'],
        '파리 FC': ['Paris FC'],
        '툴루즈': ['Toulouse FC', 'Toulouse'],
        '올랭피크 리옹': ['Olympique Lyonnais', 'Lyon', 'OL'],
        '르망FC': ['Le Mans FC', 'Le Mans'],
        '스타드 브레스투아29': ['Stade Brestois 29', 'Brest'],
        'OGC 니스': ['OGC Nice', 'Nice'],
        'FC 로리앙': ['FC Lorient', 'Lorient'],
        '올랭피크 마르세유': ['Olympique de Marseille', 'Marseille', 'OM'],
        '라싱 스트라스부르': ['RC Strasbourg Alsace', 'Strasbourg', 'RC Strasbourg'],
        '스타드 렌': ['Stade Rennais FC 1901', 'Stade Rennais FC', 'Rennes'],
        'RC 랑스': ['Racing Club de Lens', 'RC Lens', 'Lens'],
        'AJ 오세르': ['AJ Auxerre', 'Auxerre'],
        '앙제SCO': ['Angers SCO', 'Angers'],
        '릴OSC': ['Lille OSC', 'LOSC Lille', 'Lille'],
        '르아브르AC': ['Le Havre AC', 'Le Havre'],
    },
    'eredivisie': {
        'ADO 덴하흐': ['ADO Den Haag'],
        '아약스': ['AFC Ajax', 'Ajax'],
        'AZ 알크마르': ['AZ', 'AZ Alkmaar'],
        '엑셀시오르 로테르담': ['SBV Excelsior', 'Excelsior'],
        'FC 흐로닝언': ['FC Groningen', 'Groningen'],
        'FC 트벤테': ['FC Twente', 'Twente'],
        'FC 위트레흐트': ['FC Utrecht', 'Utrecht'],
        '페예노르트': ['Feyenoord Rotterdam', 'Feyenoord'],
        '포르투나 시타르드': ['Fortuna Sittard'],
        '호 어헤드 이글스': ['Go Ahead Eagles'],
        'N.E.C. 네이메헌': ['NEC Nijmegen', 'NEC'],
        'PEC 즈볼레': ['PEC Zwolle'],
        'PSV 에인트호번': ['PSV', 'PSV Eindhoven'],
        'SC 캄뷔르': ['SC Cambuur Leeuwarden', 'SC Cambuur', 'Cambuur'],
        'sc 헤렌베인': ['sc Heerenveen', 'SC Heerenveen', 'Heerenveen'],
        '스파르타 로테르담': ['Sparta Rotterdam'],
        '텔스타르': ['SC Telstar', 'Telstar'],
        '빌럼II': ['Willem II Tilburg', 'Willem II'],
    },
    'championship': {
        '울버햄튼 원더러스': ['Wolverhampton Wanderers FC', 'Wolves',
                      'Wolverhampton Wanderers'],
        '블랙번 로버스': ['Blackburn Rovers FC', 'Blackburn Rovers'],
        '볼턴 원더러스': ['Bolton Wanderers FC', 'Bolton Wanderers', 'Bolton'],
        '프레스턴 노스엔드': ['Preston North End FC', 'Preston North End', 'Preston'],
        '브리스톨 시티': ['Bristol City FC', 'Bristol City'],
        '밀월': ['Millwall FC', 'Millwall'],
        '찰턴 애슬레틱': ['Charlton Athletic FC', 'Charlton Athletic'],
        '더비 카운티': ['Derby County FC', 'Derby County'],
        '미들즈브러': ['Middlesbrough FC', 'Middlesbrough'],
        '링컨 시티': ['Lincoln City FC', 'Lincoln City'],
        '노리치 시티': ['Norwich City FC', 'Norwich City'],
        '웨스트브롬위치 알비온': ['West Bromwich Albion FC', 'West Bromwich Albion',
                        'West Brom'],
        '포츠머스': ['Portsmouth FC', 'Portsmouth'],
        'QPR': ['Queens Park Rangers FC', 'Queens Park Rangers', 'QPR'],
        '셰필드 유나이티드': ['Sheffield United FC', 'Sheffield United'],
        '버밍엄 시티': ['Birmingham City FC', 'Birmingham City'],
        '스토크 시티': ['Stoke City FC', 'Stoke City'],
        '스완지 시티': ['Swansea City AFC', 'Swansea City'],
        '번리': ['Burnley FC', 'Burnley'],
        '웨스트햄 유나이티드': ['West Ham United FC', 'West Ham United', 'West Ham'],
        '왓포드': ['Watford FC', 'Watford'],
        '렉섬': ['Wrexham AFC', 'Wrexham'],
        '카디프 시티': ['Cardiff City FC', 'Cardiff City'],
        '사우샘프턴': ['Southampton FC', 'Southampton'],
    },
}


# ============================================================ 매칭 유틸
def _ascii_fold(s):
    """악센트 제거 (Málaga -> Malaga, München -> Munchen 등), 매칭용."""
    s = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in s if not unicodedata.combining(c))


def _norm(name):
    if not name:
        return ''
    n = _ascii_fold(name)
    n = re.sub(r'\b(FC|AFC|CF)\b', '', n, flags=re.I)
    return re.sub(r'[^a-z가-힣0-9]', '', n.lower())


_LOOKUP = {}  # normalized alias -> (league_key, kr_name)
for _league_key, _team_map in LEAGUE_TEAM_MAPS.items():
    for _kr, _aliases in _team_map.items():
        for _a in _aliases + [_kr]:
            _LOOKUP[_norm(_a)] = (_league_key, _kr)


def to_kr_league(name):
    """영문/한글 어떤 표기가 와도 (리그키, 한글팀명) 튜플로. 매칭 실패 시 None."""
    return _LOOKUP.get(_norm(name))


# ============================================================ 로드 유틸
def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _js(v):
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(v, ensure_ascii=False)


# ============================================================ 데이터 빌드
# ⚠️ 2026-07-16: API-Football의 부상 데이터(injuries_af.json)엔 "핵심결장
# vs 로테이션 부상"을 구분하는 필드가 없다 — reason 텍스트로 심각도를
# 추정하는 키워드 방식을 쓴다(장기·수술급 키워드만 keyOut, 나머지는 전부
# injured로 보수적으로 분류 — 과대평가보다 과소평가가 예측 왜곡이 적음).
# 완벽한 분류가 아니므로 로그에 분류 결과를 남겨 검증할 수 있게 한다.
_KEYOUT_KEYWORDS = ('cruciate', 'acl', 'surgery', 'fracture', 'broken',
                     'rupture', 'achilles', 'torn', 'long-term', 'long term')


def _classify_injury(reason):
    r = (reason or '').lower()
    return 'keyOut' if any(kw in r for kw in _KEYOUT_KEYWORDS) else 'injured'


def build_injuries():
    """collectors.py의 InjuryCollector(어제 완성)가 만든 injuries_af.json을
    리그별 {"팀명": {keyOut:[...], injured:[...]}} 형태로 변환한다.
    EPL은 이 6개 리그 앱과 별개(SQUADS 임베드 방식)라 건드리지 않는다."""
    raw = _load_json(INJURIES_PATH, {})
    injuries = {lk: {} for lk in LEAGUE_TEAM_MAPS}
    unmatched_teams = set()
    n_keyout = n_injured = 0

    for _pid, info in raw.items():
        if not isinstance(info, dict):
            continue
        team_name = info.get('team')
        player_name = info.get('player_name')
        if not (team_name and player_name):
            continue
        hit = to_kr_league(team_name)
        if not hit:
            unmatched_teams.add(team_name)
            continue
        lk, kr = hit
        bucket = injuries[lk].setdefault(kr, {'keyOut': [], 'injured': []})
        category = _classify_injury(info.get('reason'))
        bucket[category].append(player_name)
        if category == 'keyOut':
            n_keyout += 1
        else:
            n_injured += 1

    print(f'[app_export_multileague] 부상 분류: keyOut {n_keyout}건, '
          f'injured(로테이션) {n_injured}건', flush=True)
    return injuries, unmatched_teams


def build_squads():
    """SQUADS(요청사항 1번, 최우선).
    1순위: data/master/squads_multileague.json (collect_fixtures_multileague.py
           가 BSD /players/?team= 로 만든 것 — football-data.org가 아직
           26/27 스쿼드 등록을 안 채운 경우의 보조 소스, 2026-07-13 실측:
           football-data.org 쪽 squad 필드는 존재하지만 비어있음(squad길이=0)
           확인, 시즌 등록 시점 문제로 추정).
    2순위: data/master/previous_squads.json (football-data.org, collectors.py의
           TransferDetector가 이적 감지용으로 이미 수집해둔 스쿼드).
    리그별로 독립적으로 판단한다 — schedule과 동일한 우선순위 패턴."""
    bsd_squads = _load_json(BSD_SQUADS_PATH, {})
    raw = _load_json(SQUADS_PATH, {})
    fd_squads = {lk: {} for lk in LEAGUE_TEAM_MAPS}
    unmatched_teams = set()
    for _pid, info in raw.items():
        if not isinstance(info, dict):
            continue
        team_name = info.get('team_name')
        player_name = info.get('player_name')
        if not (team_name and player_name):
            continue
        hit = to_kr_league(team_name)
        if not hit:
            unmatched_teams.add(team_name)
            continue
        lk, kr = hit
        fd_squads[lk].setdefault(kr, []).append(player_name)
    for lk in fd_squads:
        for kr in fd_squads[lk]:
            fd_squads[lk][kr].sort()

    squads = {}
    for lk in LEAGUE_TEAM_MAPS:
        bsd_sq = bsd_squads.get(lk) or {}
        if bsd_sq:
            squads[lk] = bsd_sq
        else:
            squads[lk] = fd_squads[lk]
    return squads, unmatched_teams


def build_transfers():
    """리그별 이적(영입/이탈) — collect_transfers_bsd.py가 이미 EPL+6개 리그를
    전부 감지해서 transfers_bsd.json에 쌓아두고 있었는데, 지금까지 이 앱
    출력에 노출이 안 되고 있었다 (multi_league_index.html에 이미 UI 훅까지
    준비돼 있었음, 2026-07-16 발견). player_name은 BSD 원문(영문) 그대로
    유지 — SQUADS도 번역 없이 영문 그대로라 일관성 유지."""
    records = _load_json(TRANSFERS_PATH, [])
    out = {lk: {kr: {'in': [], 'out': []} for kr in LEAGUE_TEAM_MAPS[lk]}
           for lk in LEAGUE_TEAM_MAPS}
    if not records:
        return out

    records = sorted(records, key=lambda r: r.get('detected_at') or '', reverse=True)
    seen = set()
    for r in records:
        to_league, from_league = r.get('to_league'), r.get('from_league')
        dedup_key = (r.get('player_id'), r.get('from_team'), r.get('to_team'))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        player_name = r.get('player_name')
        if not player_name:
            continue
        to_team_kr, from_team_kr = r.get('to_team'), r.get('from_team')

        if to_league in out and to_team_kr in out[to_league]:
            out[to_league][to_team_kr]['in'].append({
                'player': player_name,
                'from': from_team_kr or (from_league or '미상'),
                'date': r.get('detected_at'),
            })
        if from_league in out and from_team_kr in out[from_league]:
            out[from_league][from_team_kr]['out'].append({
                'player': player_name,
                'to': to_team_kr or (to_league or '미상'),
                'date': r.get('detected_at'),
            })

    for lk in out:
        for kr in out[lk]:
            out[lk][kr]['in'] = out[lk][kr]['in'][:20]
            out[lk][kr]['out'] = out[lk][kr]['out'][:20]
    return out


def build_all():
    """리그별 schedule을 두 소스에서 만든다.
    1순위: data/master/schedule_multileague.json (collect_fixtures_multileague.py
           가 BSD로 만든 것 — football-data.org가 26/27 시즌을 아직 못 채웠을
           가능성에 대비한 보조 소스, EPL과 동일한 실측 검증 패턴 사용).
    2순위: data/football.db의 matches 테이블 (football-data.org, collectors.py).
    리그별로 독립적으로 판단한다 — 어떤 리그는 BSD가 채워주고 다른 리그는
    football-data.org가 채워줄 수 있으므로, 리그 단위로 "BSD 쪽에 그 리그
    데이터가 있으면 그걸 쓰고, 없으면 DB 폴백" 방식이다."""
    bsd_schedules = _load_json(BSD_SCHEDULE_PATH, {})

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM matches ORDER BY date').fetchall()
    conn.close()

    db_schedules = {lk: [] for lk in LEAGUE_TEAM_MAPS}
    unmatched = set()

    for r in rows:
        keys = r.keys()
        status = (r['status'] if 'status' in keys else '') or ''
        home_hit = to_kr_league(r['home'] if 'home' in keys else None)
        away_hit = to_kr_league(r['away'] if 'away' in keys else None)
        if not home_hit:
            unmatched.add(r['home'] if 'home' in keys else '?')
        if not away_hit:
            unmatched.add(r['away'] if 'away' in keys else '?')
        if not (home_hit and away_hit):
            continue
        home_league, home_kr = home_hit
        away_league, away_kr = away_hit
        if home_league != away_league:
            continue  # 컵대회 등 다른 리그 팀끼리 매치업은 스킵
        if status.upper() == 'FINISHED':
            continue
        db_schedules[home_league].append({
            'home': home_kr, 'away': away_kr,
            'date': r['date'] if 'date' in keys else None,
        })

    schedules = {}
    for lk in LEAGUE_TEAM_MAPS:
        bsd_sched = bsd_schedules.get(lk) or []
        if bsd_sched:
            schedules[lk] = bsd_sched
        else:
            schedules[lk] = db_schedules[lk]

    elo_rankings = _load_json(ELO_PATH, {}).get('rankings', [])
    elo_by_league = {lk: {} for lk in LEAGUE_TEAM_MAPS}
    for rk in elo_rankings:
        hit = to_kr_league(rk.get('club'))
        if hit:
            lk, kr = hit
            elo_by_league[lk][kr] = rk.get('elo')

    return schedules, elo_by_league, unmatched


# ============================================================ JS 렌더링
def render_js(schedules, elo_by_league, squads, xg_by_league, injuries_by_league,
              transfers_by_league):
    lines = ['// 자동 생성 파일 — app_export_multileague.py, 수정하지 말고 파이프라인을 고치세요',
             f'// 생성 시각: {datetime.now(timezone.utc).isoformat()}',
             '']
    for league_key in LEAGUE_TEAM_MAPS:
        block = {
            'schedule': schedules.get(league_key, []),
            'logos': {},  # ⚠️ 로고 파일 미확보 (인수인계 문서 "아직 안 채워진 것" #2) — 나중에 채움
            'elo': elo_by_league.get(league_key, {}),
            'squads': squads.get(league_key, {}),
            'teamXg': xg_by_league.get(league_key, {}),
            'injuries': injuries_by_league.get(league_key, {}),
            'fatigue': {},  # ⚠️ 월드컵 여독 — 결승(7/19) 이후 별도 아티팩트로 병합 예정
            'transfers': transfers_by_league.get(league_key, {}),
        }
        var_name = f'PIPELINE_DATA_{league_key.upper()}'
        lines.append(f'window.{var_name} = ' + _js(block) + ';')
    return '\n'.join(lines)


def main():
    os.makedirs('reports', exist_ok=True)
    schedules, elo_by_league, unmatched = build_all()
    squads, unmatched_squad_teams = build_squads()
    xg_by_league = _load_json(XG_PATH, {})
    injuries_by_league, unmatched_injury_teams = build_injuries()
    transfers_by_league = build_transfers()
    js = render_js(schedules, elo_by_league, squads, xg_by_league, injuries_by_league,
                    transfers_by_league)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write(js)

    total_sched = sum(len(v) for v in schedules.values())
    total_elo = sum(len(v) for v in elo_by_league.values())
    total_squad_teams = sum(len(v) for v in squads.values())
    total_squad_players = sum(len(p) for v in squads.values() for p in v.values())
    total_xg_teams = sum(len(xg_by_league.get(lk, {})) for lk in LEAGUE_TEAM_MAPS)
    total_injury_teams = sum(len(v) for v in injuries_by_league.values())
    total_transfers = sum(len(t['in']) + len(t['out'])
                           for v in transfers_by_league.values() for t in v.values())
    print(f'[app_export_multileague] {OUT_PATH} 생성 완료, '
          f'일정 {total_sched}건, ELO {total_elo}팀, '
          f'스쿼드 {total_squad_teams}팀/{total_squad_players}명, '
          f'xG {total_xg_teams}팀, 부상 {total_injury_teams}팀, '
          f'이적 {total_transfers}건', flush=True)
    for lk in LEAGUE_TEAM_MAPS:
        squad_players = sum(len(p) for p in squads[lk].values())
        lk_transfers = sum(len(t['in']) + len(t['out']) for t in transfers_by_league[lk].values())
        print(f'  {lk}: 일정 {len(schedules[lk])}건, ELO {len(elo_by_league[lk])}팀, '
              f'스쿼드 {len(squads[lk])}팀/{squad_players}명, '
              f'xG {len(xg_by_league.get(lk, {}))}팀, '
              f'부상 {len(injuries_by_league[lk])}팀, 이적 {lk_transfers}건',
              flush=True)
    if unmatched:
        sample = sorted(unmatched)[:15]
        print(f'[app_export_multileague] ⚠️ 일정 매칭 안 된 팀명 {len(unmatched)}개 '
              f'(샘플): {sample}', flush=True)
    if unmatched_squad_teams:
        sample2 = sorted(unmatched_squad_teams)[:15]
        print(f'[app_export_multileague] ⚠️ 스쿼드 매칭 안 된 팀명 '
              f'{len(unmatched_squad_teams)}개 (샘플): {sample2}', flush=True)
    if unmatched_injury_teams:
        sample3 = sorted(unmatched_injury_teams)[:15]
        print(f'[app_export_multileague] ⚠️ 부상 매칭 안 된 팀명 '
              f'{len(unmatched_injury_teams)}개 (샘플): {sample3}', flush=True)


if __name__ == '__main__':
    main()
