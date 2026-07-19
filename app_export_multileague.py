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
import time
import unicodedata
from datetime import datetime, timezone

import requests

DB_PATH = 'data/football.db'
OUT_PATH = 'reports/app_data_multileague.js'
ELO_PATH = 'data/master/club_elo.json'
BSD_SCHEDULE_PATH = 'data/master/schedule_multileague.json'
BSD_SQUADS_PATH = 'data/master/squads_multileague.json'
XG_PATH = 'data/master/xg_multileague.json'
SQUADS_PATH = 'data/master/previous_squads.json'
INJURIES_PATH = 'data/master/injuries_af.json'
# app_export.py(EPL)와 완전히 같은 파일을 공유한다 — 선수가 EPL/6개 리그를
# 넘나드는 경우(이적 등)도 있고, 캐시를 나눠 갖는 것보다 하나로 합쳐야
# 중복 번역 API 호출을 줄일 수 있다.
NAME_CACHE_PATH = 'data/master/name_translations.json'
# collect_transfers_bsd.py가 EPL+6개 리그 전부를 스냅샷 diff로 감지해서
# 쌓아두는 파일. team_kr은 수집 시점에 이미 to_kr_league로 변환된 한글
# 팀명이라 여기서 다시 매핑할 필요 없음 (2026-07-16 확인 — 지금까지는
# 이 파일이 수집만 되고 앱에 노출이 안 되고 있었음).
TRANSFERS_PATH = 'data/master/transfers_bsd.json'


def _load_name_cache():
    if os.path.exists(NAME_CACHE_PATH):
        try:
            with open(NAME_CACHE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_name_cache(cache):
    os.makedirs(os.path.dirname(NAME_CACHE_PATH), exist_ok=True)
    with open(NAME_CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


_MISTRANSLATION_MARKERS = (
    # 서술문/명령문/의문문 어미 — 구글 번역이 이름을 문장으로 직역했을 때
    # 나타나는 대표 패턴들 (2026-07-18 실전 실행에서 실측된 것들 위주)
    '세요', '십시오', '습니다', '합니다', '됩니다', '해요', '하세요',
    '주세요', '것입니다', '것이다', '였다', '이었다', '했다', '한다',
    '떠나', '구출', '살펴보', '찾아보', '찾으세요', '찾아서',
    '인가요', '인가?', '까요', '까요?', '나요', '나요?', '습니까',
    'ㅂ니까', '입니까', '몇 명', '몇개', '몇 개', '얼마나', '무엇',
    '누구', '어디', '어떻게',
)

# 조사(을/를/은/는) 뒤에 다른 단어가 더 붙어 있으면 문장 구조라는 뜻 —
# 사람 이름 음역엔 이런 문법 구조가 나올 이유가 없다(2026-07-18 추가).
# 단어 목록을 계속 늘리는 것보다 이 구조적 규칙 하나가 앞으로 새로
# 나타날 오역 패턴까지 훨씬 넓게 잡아준다. 한 음절짜리 조사(이/가/에/로
# 등)는 진짜 이름 끝음절과 우연히 겹칠 위험이 있어서 제외하고, 겹칠
# 위험이 낮은 을/를/은/는 4개만 쓴다.
_STRUCTURAL_PARTICLES = ('을', '를', '은', '는', '의')
# 형용사 관형형 오역 패턴(예: 'Honest Ahanor' -> '정직한 아하노르') 대응.
# 2026-07-18 3차 추가 — 조사만으로는 못 잡는 [형용사]+[명사] 구조까지 커버.
_ATTRIBUTIVE_SUFFIXES = ('한',)


def _looks_like_mistranslation(translated):
    """구글 번역이 사람 이름을 진짜 문장/명령문/의문문으로 직역해버린
    경우를 걸러낸다 (2026-07-18 발견 — 예: 'Kevin De Bruyne'가 '드
    브루인을 찾아보세요'로, 'Sapoco Ndiaye'류 이름이 '~는 몇 명인가요?'로
    번역되는 등). 이름 일부가 흔한 영어 단어와 겹치면 구글 번역이
    고유명사가 아니라 문장으로 처리해버리는 게 원인.
    두 단계로 검사한다: 1) 흔한 어미/의문형 패턴이 문자열 어디든 있는지
    (기존 방식), 2) 조사(을/를/은/는)로 끝나는 단어 뒤에 다른 단어가 더
    있는지(문장 구조 신호, 2026-07-18 추가 — 이게 미래의 새로운 오역
    패턴까지 넓게 잡아주는 핵심). 걸리면 원문 영문 이름을 그대로 쓴다
    (틀린 한글 번역보다 안 틀린 영문 원문이 낫다는 원칙)."""
    if not translated:
        return False
    if any(marker in translated for marker in _MISTRANSLATION_MARKERS):
        return True
    tokens = translated.split()
    for tok in tokens[:-1]:  # 마지막 토큰 뒤엔 더 이어지는 말이 없으니 검사 제외
        for suf in _STRUCTURAL_PARTICLES + _ATTRIBUTIVE_SUFFIXES:
            if tok.endswith(suf) and len(tok) > len(suf):
                return True
    return False


def _translate_name(name, cache):
    """선수/감독명 -> 한글 (2026-07-17 추가). app_export.py의 EPL용
    _translate_name과 같은 캐시 파일을 공유하되, 소스 언어를 'en'으로
    고정하지 않고 'auto'로 둔다 — 6개 리그는 독일어/스페인어/이탈리아어/
    프랑스어/네덜란드어 표기가 섞여 있어서(예: 'Müller', 'Müller'를 영어로
    잘못 취급하면 발음이 틀어질 수 있음), 구글 번역이 원어를 스스로
    감지하게 하는 쪽이 더 정확하다. 실패하면 원문 그대로 반환(안전 폴백,
    EPL 쪽과 동일한 방침).

    2026-07-18 추가: 캐시에 있어도 오역으로 의심되면(_looks_like_mistranslation)
    재번역을 시도한다 — 이전 실행에서 잘못 캐시된 값이 계속 재사용되는 걸
    막기 위한 자가 치유 로직."""
    if not name:
        return name
    if name in cache and not _looks_like_mistranslation(cache[name]):
        return cache[name]
    try:
        resp = requests.get(
            'https://translate.googleapis.com/translate_a/single',
            params={'client': 'gtx', 'sl': 'auto', 'tl': 'ko', 'dt': 't',
                    'q': name},
            timeout=5)
        data = resp.json()
        ko = ''.join(seg[0] for seg in data[0]) if data and data[0] else name
        ko = ko.strip() or name
    except Exception:
        ko = name  # 실패 시 원문 그대로 (예외 없이 넘어감 — EPL 쪽과 동일 원칙)
    else:
        time.sleep(0.05)  # 신규 항목만 지연, 캐시 히트는 즉시 반환됨
    if _looks_like_mistranslation(ko):
        ko = name  # 오역으로 보이면 원문 영문 이름으로 되돌림
    cache[name] = ko
    return ko
# 2026-07-16 확인: 앱의 STATIC_LOGOS(로컬 파일)가 이미 118팀 전부 채워져 있어서
# 이 필드가 지금 당장 급한 건 아니다. 그래도 다음 시즌 승격/강등으로 로고가 또
# 비게 될 때를 위한 자동 백업 소스로 collect_logos_multileague.py를 붙여둔다
# (앱은 로컬 우선 → 이 데이터는 로컬에 없는 팀에 대해서만 실제로 쓰인다).
LOGOS_PATH = 'data/master/logos_multileague.json'
# EPL과 완전히 같은 파일 — train_ml_ensemble.py가 EPL+6개 리그를 리그 구분 없이
# 통합 학습해서 한 세트의 계수만 만든다(2026-07-16 착수). 없으면 null로 내려가고
# 앱이 안전 폴백한다.
ML_ENSEMBLE_PATH = 'data/master/ml_ensemble.json'

# ============================================================ 포지션 버킷 (2026-07-16 추가)
# BSD position 필드의 실제 표기(축약형/전체 단어 등)를 아직 실전 검증 못 했으므로,
# EPL 앱(app_export.py POS_BUCKET)과 같은 정확 매칭 + 느슨한 substring 매칭을
# 같이 써서 최대한 커버한다. 그래도 못 맞추면 EPL과 동일하게 'mf'로 폴백
# (분류 실패가 예측에 큰 영향 안 주는 안전한 기본값).
_POS_EXACT = {'goalkeeper': 'gk', 'gk': 'gk', 'g': 'gk',
              'defender': 'df', 'df': 'df', 'd': 'df',
              'midfielder': 'mf', 'mf': 'mf', 'm': 'mf',
              'forward': 'fw', 'fw': 'fw', 'f': 'fw', 'attacker': 'fw'}
_POS_SUBSTR = (('keeper', 'gk'), ('back', 'df'), ('wing-back', 'df'),
               ('mid', 'mf'), ('striker', 'fw'), ('wing', 'fw'), ('forward', 'fw'))


def _bucket_position(position):
    if not position:
        return 'mf'
    p = str(position).strip().lower()
    if p in _POS_EXACT:
        return _POS_EXACT[p]
    for kw, bucket in _POS_SUBSTR:
        if kw in p:
            return bucket
    return 'mf'

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
                        'Deportivo de La Coruña', 'RC Deportivo',
                        # 2026-07-18 파이프라인 로그 실측: BSD는 갈리시아어
                        # 표기 "A Coruña"를 쓴다 (La 아님)
                        'Deportivo de A Coruña', 'Deportivo A Coruña'],
        '엘체': ['Elche CF', 'Elche'],
        '에스파뇰': ['RCD Espanyol de Barcelona', 'RCD Espanyol', 'Espanyol'],
        '헤타페': ['Getafe CF', 'Getafe'],
        '레반테': ['Levante UD', 'Levante'],
        '말라가': ['Málaga CF', 'Malaga CF', 'Malaga'],
        '오사수나': ['CA Osasuna', 'Osasuna'],
        '라싱 산탄데르': ['Real Racing Club de Santander', 'Racing de Santander',
                     'Racing Santander', 'Racing Club de Santander',
                     # 2026-07-18 파이프라인 로그 실측: BSD 표기
                     'Real Racing Club'],
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
        '스타드 브레스투아29': ['Stade Brestois 29', 'Brest',
                        # 2026-07-18 파이프라인 로그 실측: BSD는 29 없이 표기
                        'Stade Brestois'],
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
        # 2026-07-19 파이프라인 로그 실측: BSD는 'Wolverhampton' 단독 표기
        # (팀 목록 + 이벤트 diag 양쪽에서 확인, team_id=11)
        '울버햄튼 원더러스': ['Wolverhampton', 'Wolverhampton Wanderers FC', 'Wolves',
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


def build_squads(name_cache):
    """SQUADS(요청사항 1번, 최우선).
    1순위: data/master/squads_multileague.json (collect_fixtures_multileague.py
           가 BSD /players/?team= 로 만든 것 — football-data.org가 아직
           26/27 스쿼드 등록을 안 채운 경우의 보조 소스, 2026-07-13 실측:
           football-data.org 쪽 squad 필드는 존재하지만 비어있음(squad길이=0)
           확인, 시즌 등록 시점 문제로 추정).
    2순위: data/master/previous_squads.json (football-data.org, collectors.py의
           TransferDetector가 이적 감지용으로 이미 수집해둔 스쿼드).
    리그별로 독립적으로 판단한다 — schedule과 동일한 우선순위 패턴.

    2026-07-16 추가: BSD 소스는 이제 팀당 {'coach':.., 'players':[{name,position}]}
    구조로 온다(collect_fixtures_multileague.py 갱신). 구버전 파일(평문
    이름 리스트만 있던 시절)이 남아있어도 깨지지 않게 두 구조 다 처리한다.
    출력 형식: {kr팀명: {'coach':str, 'gk':[...], 'df':[...], 'mf':[...],
    'fw':[...], 'all':[...전체], 'hasPositions':bool}}
    — 'hasPositions'가 False면(구버전/football-data 폴백처럼 포지션 정보가
    전혀 없는 경우) 프론트가 GK/DF/MF/FW로 나누지 않고 'all'만 평평하게
    보여주도록 신호를 준다.

    2026-07-17 추가: 선수명/감독명을 한글로 번역한다(app_export.py의 EPL
    앱과 동일한 캐시 파일 공유 — _translate_name 참고). 정렬은 번역된
    한글 기준으로 하는 게 UI에서 자연스러워서 영문 정렬에서 한글 정렬로
    바꿨다."""
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
        fd_squads[lk].setdefault(kr, []).append(_translate_name(player_name, name_cache))
    for lk in fd_squads:
        for kr in fd_squads[lk]:
            fd_squads[lk][kr].sort()

    squads = {}
    n_teams_with_position = n_teams_with_coach = 0
    for lk in LEAGUE_TEAM_MAPS:
        bsd_sq = bsd_squads.get(lk) or {}
        squads[lk] = {}
        if bsd_sq:
            for kr, entry in bsd_sq.items():
                coach_en = ''
                raw_players = []
                if isinstance(entry, dict):
                    coach_en = entry.get('coach') or ''
                    raw_players = entry.get('players') or []
                elif isinstance(entry, list):
                    # 구버전 호환: 이름 문자열 리스트만 있던 시절 데이터
                    raw_players = [{'name': n, 'position': None} for n in entry if n]
                coach = _translate_name(coach_en, name_cache) if coach_en else ''

                bucket = {'gk': [], 'df': [], 'mf': [], 'fw': []}
                all_names = []
                has_any_position = False
                for p in raw_players:
                    name_en = p.get('name') if isinstance(p, dict) else p
                    pos = p.get('position') if isinstance(p, dict) else None
                    if not name_en:
                        continue
                    name_ko = _translate_name(name_en, name_cache)
                    all_names.append(name_ko)
                    if pos:
                        has_any_position = True
                    bucket[_bucket_position(pos)].append(name_ko)
                for k in bucket:
                    bucket[k].sort()
                if coach:
                    n_teams_with_coach += 1
                if has_any_position:
                    n_teams_with_position += 1
                squads[lk][kr] = {
                    'coach': coach,
                    'gk': bucket['gk'], 'df': bucket['df'],
                    'mf': bucket['mf'], 'fw': bucket['fw'],
                    'all': sorted(all_names),
                    'hasPositions': has_any_position,
                }
        else:
            for kr, names in fd_squads[lk].items():
                squads[lk][kr] = {
                    'coach': '', 'gk': [], 'df': [], 'mf': [], 'fw': [],
                    'all': names, 'hasPositions': False,
                }
    print(f'[app_export_multileague] 스쿼드 포지션 확보 {n_teams_with_position}팀, '
          f'감독 확보 {n_teams_with_coach}팀', flush=True)
    return squads, unmatched_teams


GOALSCORERS_PATH = 'data/master/goalscorers.json'


def build_leaderboard(name_cache):
    """리그별 득점왕/도움왕 (2026-07-18 신규 — EPL 앱의 build_leaderboard와
    동일한 원칙). collect_goalscorers.py가 실제 26-27 시즌 종료 경기만
    대상으로 모은 goalscorers.json을 쓴다.
    ⚠️ 8월 개막 전이라 지금은 리그별로 빈 값이 나오는 게 정상이다 —
    틀린 데이터를 보여주는 것보다 빈 화면이 낫다는 원칙으로, season_players.json
    같은 대체 소스는 일부러 안 쓴다(EPL 쪽도 마찬가지 이유로 뺐음)."""
    all_matches = _load_json(GOALSCORERS_PATH, {})
    out = {}
    for lk in LEAGUE_TEAM_MAPS:
        matches = all_matches.get(lk, [])
        scorers, assists = {}, {}
        n_unresolved_team = 0
        for m in matches:
            home_kr, away_kr = m.get('home'), m.get('away')
            for g in m.get('goals', []):
                scorer_en = g.get('scorer')
                if not scorer_en:
                    continue
                scorer_ko = _translate_name(scorer_en, name_cache)
                team_raw = g.get('team')
                hit = to_kr_league(team_raw) if team_raw else None
                team_kr = hit[1] if (hit and hit[0] == lk) else None
                if team_kr not in (home_kr, away_kr):
                    team_kr = None
                    n_unresolved_team += 1
                key = f'{scorer_ko}|{team_kr or "미상"}'
                scorers[key] = scorers.get(key, 0) + 1

                assist_en = g.get('assist')
                if assist_en:
                    assist_ko = _translate_name(assist_en, name_cache)
                    akey = f'{assist_ko}|{team_kr or "미상"}'
                    assists[akey] = assists.get(akey, 0) + 1
        out[lk] = {'scorers': scorers, 'assists': assists}
        if scorers or n_unresolved_team:
            print(f'[app_export_multileague] {lk} 득점왕/도움왕: '
                  f'득점 {sum(scorers.values())}건, 도움 {sum(assists.values())}건'
                  + (f', 팀 매칭 실패 {n_unresolved_team}건' if n_unresolved_team else ''),
                  flush=True)
    return out


def build_h2h():
    """리그별 팀간 과거 맞대결 — EPL 앱(app_export.py build_h2h)과 완전히
    동일한 계산 방식(matches 테이블, "팀A|||팀B" 가나다순 키, 최신 10경기)을
    6개 리그로 확장한 것. 컵대회 등 다른 대회 매치업은 build_all()과 동일하게
    home_league != away_league 케이스를 걸러서 제외한다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM matches WHERE status='FINISHED' "
        "AND home_goals IS NOT NULL ORDER BY date"
    ).fetchall()
    conn.close()

    h2h_by_league = {lk: {} for lk in LEAGUE_TEAM_MAPS}
    for r in rows:
        home_hit = to_kr_league(r['home'])
        away_hit = to_kr_league(r['away'])
        if not (home_hit and away_hit):
            continue
        home_league, home_kr = home_hit
        away_league, away_kr = away_hit
        if home_league != away_league:
            continue
        key = '|||'.join(sorted([home_kr, away_kr]))
        h2h_by_league[home_league].setdefault(key, []).append({
            'home': home_kr, 'away': away_kr,
            'homeGoals': r['home_goals'], 'awayGoals': r['away_goals'],
            'date': r['date'],
        })

    out = {lk: {} for lk in LEAGUE_TEAM_MAPS}
    for lk, h2h in h2h_by_league.items():
        for key, games in h2h.items():
            games_sorted = sorted(games, key=lambda g: g['date'] or '', reverse=True)
            out[lk][key] = games_sorted[:10]
    return out


def build_transfers(name_cache):
    """리그별 이적(영입/이탈) — collect_transfers_bsd.py가 이미 EPL+6개 리그를
    전부 감지해서 transfers_bsd.json에 쌓아두고 있었는데, 지금까지 이 앱
    출력에 노출이 안 되고 있었다 (multi_league_index.html에 이미 UI 훅까지
    준비돼 있었음, 2026-07-16 발견).
    2026-07-17 추가: player_name을 한글로 번역한다 — 처음엔 SQUADS와의
    일관성을 이유로 영문 그대로 뒀는데, SQUADS 쪽을 번역하기로 하면서
    이쪽도 맞춰야 진짜 일관성이 유지된다."""
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

        # 2026-07-19: 같은 리그·같은 팀 → 같은 팀 "이적"은 가짜다.
        # collect_transfers_bsd가 BSD의 중복 team_id(예: 레알 소시에다드
        # 48/924)를 둘 다 돌면서 같은 선수를 다른 ID 밑에서 재발견해 이적으로
        # 오인한 것 (07-19 실행에서 28건 발생). 근본 수정은
        # collect_transfers_bsd 쪽 대표 ID 적용이지만, 이미 누적 json에 들어간
        # 기존 레코드도 걸러야 하므로 표시단에서도 차단한다.
        if (r.get('from_team') and r.get('from_team') == r.get('to_team')
                and from_league == to_league):
            continue

        player_name_en = r.get('player_name')
        if not player_name_en:
            continue
        player_name = _translate_name(player_name_en, name_cache)
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
              transfers_by_league, h2h_by_league, ml_ensemble, leaderboard_by_league):
    logos_by_league = _load_json(LOGOS_PATH, {})
    lines = ['// 자동 생성 파일 — app_export_multileague.py, 수정하지 말고 파이프라인을 고치세요',
             f'// 생성 시각: {datetime.now(timezone.utc).isoformat()}',
             '']
    for league_key in LEAGUE_TEAM_MAPS:
        block = {
            'schedule': schedules.get(league_key, []),
            'logos': logos_by_league.get(league_key, {}),
            'elo': elo_by_league.get(league_key, {}),
            'squads': squads.get(league_key, {}),
            'teamXg': xg_by_league.get(league_key, {}),
            'injuries': injuries_by_league.get(league_key, {}),
            'fatigue': {},  # ⚠️ 월드컵 여독 — 결승(7/19) 이후 별도 아티팩트로 병합 예정
            'transfers': transfers_by_league.get(league_key, {}),
            'h2h': h2h_by_league.get(league_key, {}),
            'leaderboard': leaderboard_by_league.get(league_key, {'scorers': {}, 'assists': {}}),
        }
        var_name = f'PIPELINE_DATA_{league_key.upper()}'
        lines.append(f'window.{var_name} = ' + _js(block) + ';')
    # 리그별 블록과 별개로 하나만 — EPL 앱(app_export.py)과 동일한 통합 모델을
    # 그대로 재사용(리그마다 따로 학습 안 함, train_ml_ensemble.py 상단 설명 참고).
    lines.append('window.PIPELINE_ML_ENSEMBLE = ' + _js(ml_ensemble) + ';')
    return '\n'.join(lines)


def main():
    os.makedirs('reports', exist_ok=True)
    name_cache = _load_name_cache()  # app_export.py(EPL)와 공유하는 캐시
    schedules, elo_by_league, unmatched = build_all()
    squads, unmatched_squad_teams = build_squads(name_cache)
    xg_by_league = _load_json(XG_PATH, {})
    injuries_by_league, unmatched_injury_teams = build_injuries()
    transfers_by_league = build_transfers(name_cache)
    h2h_by_league = build_h2h()
    leaderboard_by_league = build_leaderboard(name_cache)
    ml_ensemble = _load_json(ML_ENSEMBLE_PATH, None)
    js = render_js(schedules, elo_by_league, squads, xg_by_league, injuries_by_league,
                    transfers_by_league, h2h_by_league, ml_ensemble, leaderboard_by_league)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write(js)
    _save_name_cache(name_cache)

    total_sched = sum(len(v) for v in schedules.values())
    total_elo = sum(len(v) for v in elo_by_league.values())
    total_squad_teams = sum(len(v) for v in squads.values())
    total_squad_players = sum(len(t['all']) for v in squads.values() for t in v.values())
    total_xg_teams = sum(len(xg_by_league.get(lk, {})) for lk in LEAGUE_TEAM_MAPS)
    total_injury_teams = sum(len(v) for v in injuries_by_league.values())
    total_transfers = sum(len(t['in']) + len(t['out'])
                           for v in transfers_by_league.values() for t in v.values())
    total_h2h_pairs = sum(len(v) for v in h2h_by_league.values())
    total_goals = sum(sum(lb.get('scorers', {}).values()) for lb in leaderboard_by_league.values())
    print(f'[app_export_multileague] {OUT_PATH} 생성 완료, '
          f'일정 {total_sched}건, ELO {total_elo}팀, '
          f'스쿼드 {total_squad_teams}팀/{total_squad_players}명, '
          f'xG {total_xg_teams}팀, 부상 {total_injury_teams}팀, '
          f'이적 {total_transfers}건, H2H {total_h2h_pairs}개 조합, '
          f'득점기록 {total_goals}골, '
          f'이름 캐시 {len(name_cache)}건', flush=True)
    for lk in LEAGUE_TEAM_MAPS:
        squad_players = sum(len(t['all']) for t in squads[lk].values())
        lk_transfers = sum(len(t['in']) + len(t['out']) for t in transfers_by_league[lk].values())
        lk_goals = sum(leaderboard_by_league.get(lk, {}).get('scorers', {}).values())
        print(f'  {lk}: 일정 {len(schedules[lk])}건, ELO {len(elo_by_league[lk])}팀, '
              f'스쿼드 {len(squads[lk])}팀/{squad_players}명, '
              f'xG {len(xg_by_league.get(lk, {}))}팀, '
              f'부상 {len(injuries_by_league[lk])}팀, 이적 {lk_transfers}건, '
              f'H2H {len(h2h_by_league[lk])}개 조합, 득점기록 {lk_goals}골',
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
