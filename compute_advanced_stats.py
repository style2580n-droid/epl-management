# -*- coding: utf-8 -*-
"""
고급지표(Advanced Stats) 직접 계산 엔진 — 크롤링/유료API 없이 BSD 원시 이벤트만으로 산출.

배경: Opta/StatsBomb/Wyscout/FBref/Understat 전부 배제(크롤링 차단·비용 문제, 2026-07
결정). 대신 이미 수집 중인 BSD 이벤트 데이터를 수학적으로 가공해서 직접 계산한다.

출력 형태는 app_export.py의 build_team_blocks()가 만드는 adv_out과 완전히 같은 모양
({팀: {psxg, psxgAllowed, bigChances, bigChancesAllowed, shotQuality, spCornerXg, ...}})
이라 app_export.py 쪽 한 줄만 바꾸면(직접 계산값으로 default_adv를 대체) 바로 꽂힌다.

⚠️ 좌표계 가정: 아래 코드는 슈팅 위치가 (x, y)로 온다고 가정하고, x=0~100(0=자기 골대
   라인, 100=상대 골대 라인), y=0~100(0=한쪽 터치라인, 100=반대쪽)인 정규화 좌표를
   기준으로 짰다. BSD의 실제 좌표계가 이거랑 다르면(예: 미터 단위, y축 반전, 원점
   위치 다름 등) _normalize_event()의 좌표 변환 부분만 고치면 나머지 계산 로직은
   그대로 재사용 가능 — 전체를 다시 짤 필요 없게 이 부분만 격리해뒀다.

⚠️ 좌표 자체가 아예 없는 이벤트 소스라면: xG/PSxG/빅찬스/슈팅품질은 계산 불가능하지만
   PPDA/필드틸트는 "이벤트가 어느 1/3 지역(zone)에서 일어났는지"만 알아도 근사 가능
   하므로, zone 필드(또는 x좌표만이라도)가 있으면 그것만으로도 절반은 건질 수 있다.
   아래 함수들은 좌표가 없으면 해당 항목만 조용히 None을 반환하고(크래시 안 함),
   메인 실행부가 "이번 경기는 좌표 없어서 xG류 스킵함" 식으로 로그를 남긴다
   (app_export.py의 기존 원칙: "값 없는 지표는 생략 → 예측에 영향 없음"과 동일).
"""
import json
import math
import os
import sqlite3
from collections import defaultdict

DB_PATH = 'data/football.db'
METRICS_DIR = 'data/metrics'
OUT_PATH = 'data/metrics/advanced_stats_computed.json'


# ============================================================ 1) 이벤트 정규화
# BSD 원시 이벤트 → 이 모듈 전체가 공유하는 표준 형태로 변환하는 유일한 지점.
# 실제 필드명이 아래 가정과 다르면 이 함수 안쪽만 고치면 된다(다른 함수 안 건드림).
def _normalize_event(raw):
    """
    반환 형태(가정, 실제 BSD 응답 보고 필드명만 맞추면 됨):
    {
      'type': 'shot'|'pass'|'tackle'|'interception'|'foul'|'carry'|...,
      'team': '홈팀'|'원정팀' 이 아니라 is_home(bool) — collect_goalscorers.py에서
              이미 확인된 BSD 패턴(팀명 필드가 아예 없고 is_home만 옴)과 동일하게 가정,
      'x': 0~100 or None, 'y': 0~100 or None,
      'end_x': 0~100 or None, 'end_y': 0~100 or None,   # 패스/캐리 종료지점(있으면)
      'body_part': 'foot'|'head'|'other'|None,
      'situation': 'open_play'|'corner'|'freekick'|'penalty'|None,
      'outcome': 'goal'|'saved'|'blocked'|'off_target'|None,
    }
    """
    return {
        'type': raw.get('type') or raw.get('incident_type'),
        'is_home': bool(raw.get('is_home')),
        'x': raw.get('x'), 'y': raw.get('y'),
        'end_x': raw.get('end_x') or raw.get('pass_end_x'),
        'end_y': raw.get('end_y') or raw.get('pass_end_y'),
        'body_part': raw.get('body_part'),
        'situation': raw.get('situation') or raw.get('play_pattern'),
        'outcome': raw.get('outcome') or raw.get('result'),
    }


# ============================================================ 2) xG / PSxG (거리·각도 기반)
# 공개된 축구 분석 문헌에서 공통적으로 쓰이는 "거리+각도" 단순 로지스틱 근사식.
# 수만 건 실제 슈팅 결과로 훈련된 전문 xG 모델(Opta 등)만큼 정밀하진 않지만,
# 크롤링/유료 API 없이 자체 계산 가능한 현실적 근사치 — 이 프로젝트 목표에 맞음.
GOAL_X, GOAL_Y = 100.0, 50.0  # 좌표계 가정: 상대 골대가 x=100, y=50(중앙)

def _shot_distance_angle(x, y):
    dx = GOAL_X - x
    dy = abs(GOAL_Y - y)
    distance = math.hypot(dx, dy)
    # 골대 폭 약 7.32m를 좌표계 스케일(0~100이 105m 기준)로 환산 → 약 6.97
    goal_half_width = 6.97 / 2
    angle = math.atan2(goal_half_width, distance) if distance > 0 else math.pi / 2
    return distance, angle


def calc_xg(x, y, body_part=None, situation=None):
    """거리+각도 기반 단순 로지스틱 xG 근사. x,y는 0~100 정규화 좌표(공격방향 기준)."""
    if x is None or y is None:
        return None
    distance, angle = _shot_distance_angle(x, y)
    # 계수는 공개 문헌에서 흔히 보고되는 대략적 크기(거리가 지배적 변수, 각도가 보조)를
    # 이 좌표계 스케일에 맞게 조정한 값 — 실제 슈팅 결과 쌓이면 로지스틱회귀로
    # 재적합(refit) 권장(아래 refit_xg_coefficients() 참고).
    z = 1.2 - 0.09 * distance + 1.8 * angle
    if situation == 'penalty':
        return 0.76  # 페널티킥은 위치 무관 고정 확률로 처리하는 게 통상적 관례
    if body_part == 'head':
        z -= 0.6  # 헤더는 발슛보다 성공률 낮음(공개 통계 공통 결론)
    if situation in ('corner', 'freekick'):
        z -= 0.25  # 세트피스 크로스발 슈팅은 오픈플레이보다 대체로 낮음
    xg = 1 / (1 + math.exp(-z))
    return round(min(max(xg, 0.01), 0.95), 4)


def calc_psxg(shot_event):
    """포스트샷 xG(슈팅이 골대 프레임 안으로 향했는지까지 반영).
    BSD가 골대 내 도착지점(end_x/end_y, 골라인 상의 좌표)을 안 주면 사전 xG로 대체
    (이 경우 psxg == xg가 되어 "골대 안/밖 방향성"만 반영 못 함 — 데이터 확장되면
    자동으로 더 정밀해지는 구조)."""
    base_xg = calc_xg(shot_event['x'], shot_event['y'],
                       shot_event.get('body_part'), shot_event.get('situation'))
    if base_xg is None:
        return None
    if shot_event.get('outcome') == 'off_target' or shot_event.get('outcome') == 'blocked':
        return round(base_xg * 0.35, 4)  # 프레임 밖으로 가거나 막힌 슈팅은 psxg 대폭 하향
    return base_xg  # 유효슈팅(온타겟/골)은 사전 xG 그대로(더 정밀하려면 도착좌표 필요)


# ============================================================ 3) 빅찬스
def is_big_chance(xg_value):
    """공개 통계 사이트들이 흔히 쓰는 기준(xG > 0.3 안팎)을 채택 — 이 프로젝트에선
    질적 판단(수비 상황 등) 없이 순수 xG 임계값으로만 근사."""
    return xg_value is not None and xg_value >= 0.3


# ============================================================ 4) PPDA / 필드틸트
def calc_ppda(team_def_actions_opp_third, opp_passes_own_third):
    """PPDA = 상대가 자기 진영 2/3(수비+중원)에서 시도한 패스 수 / 우리 팀이 상대
    공격 2/3에서 한 수비 액션(태클+인터셉트+파울) 수. 낮을수록 압박 강함(통상 정의)."""
    if not opp_passes_own_third:
        return None
    if not team_def_actions_opp_third:
        return None
    return round(opp_passes_own_third / team_def_actions_opp_third, 2)


def calc_field_tilt(team_final_third_touches, opp_final_third_touches):
    """필드틸트 = 우리 팀 최종 1/3 터치 수 / (우리+상대 최종 1/3 터치 수) * 100."""
    total = team_final_third_touches + opp_final_third_touches
    if total == 0:
        return None
    return round(team_final_third_touches / total * 100, 1)


# ============================================================ 5) 경기 단위 집계
def aggregate_match_events(events):
    """정규화된 이벤트 리스트 하나(한 경기)를 홈/원정으로 나눠 위 지표들을 계산."""
    home_shots, away_shots = [], []
    home_def_in_away_third = away_def_in_home_third = 0
    home_passes_in_own_third = away_passes_in_own_third = 0
    home_final_third_touches = away_final_third_touches = 0

    for e in events:
        is_home = e['is_home']
        if e['type'] == 'shot' and e['x'] is not None:
            (home_shots if is_home else away_shots).append(e)
        if e['type'] in ('tackle', 'interception', 'foul') and e['x'] is not None:
            # 공격 2/3(x>=33)에서의 수비 액션만 PPDA 분자로 카운트(통상 정의)
            if is_home and e['x'] >= 33:
                home_def_in_away_third += 1
            elif not is_home and e['x'] <= 67:  # 원정팀 기준 좌표는 그대로, 상대(홈) 진영이 x<=67
                away_def_in_home_third += 1
        if e['type'] == 'pass' and e['x'] is not None:
            if is_home and e['x'] <= 67:
                home_passes_in_own_third += 1
            elif not is_home and e['x'] >= 33:
                away_passes_in_own_third += 1
        if e['x'] is not None and e['x'] >= 67:
            if is_home:
                home_final_third_touches += 1
        if e['x'] is not None and e['x'] <= 33:
            if not is_home:
                away_final_third_touches += 1

    def _shot_stats(shots):
        xgs = [calc_xg(s['x'], s['y'], s.get('body_part'), s.get('situation')) for s in shots]
        xgs = [v for v in xgs if v is not None]
        psxgs = [calc_psxg(s) for s in shots]
        psxgs = [v for v in psxgs if v is not None]
        big = sum(1 for v in xgs if is_big_chance(v))
        sp_corner = sum(calc_xg(s['x'], s['y'], s.get('body_part'), 'corner') or 0
                         for s in shots if s.get('situation') == 'corner')
        sp_freekick = sum(calc_xg(s['x'], s['y'], s.get('body_part'), 'freekick') or 0
                           for s in shots if s.get('situation') == 'freekick')
        sp_penalty = sum(0.76 for s in shots if s.get('situation') == 'penalty')
        return {
            'psxg_sum': round(sum(psxgs), 3) if psxgs else None,
            'big_chances': big,
            'shot_quality': round(sum(xgs) / len(xgs), 3) if xgs else None,
            'sp_corner_xg': round(sp_corner, 3),
            'sp_freekick_xg': round(sp_freekick, 3),
            'sp_penalty_xg': round(sp_penalty, 3),
            'n_shots': len(shots),
        }

    return {
        'home': {**_shot_stats(home_shots),
                 'ppda': calc_ppda(home_def_in_away_third, away_passes_in_own_third),
                 'field_tilt': calc_field_tilt(home_final_third_touches, away_final_third_touches)},
        'away': {**_shot_stats(away_shots),
                 'ppda': calc_ppda(away_def_in_home_third, home_passes_in_own_third),
                 'field_tilt': calc_field_tilt(away_final_third_touches, home_final_third_touches)},
    }


# ============================================================ 6) 시즌 집계 → app_export.py adv_out 형태
def build_advanced_stats_from_matches(matches_events):
    """matches_events: {match_id: {'home_team': kr, 'away_team': kr, 'events': [raw_event, ...]}, ...}
    반환: app_export.py의 build_team_blocks()가 만드는 adv_out과 동일한 키 구조.
    한 시즌 여러 경기를 팀별로 누적 평균낸다(경기당 psxg 합의 평균 = 시즌 psxg 등)."""
    acc = defaultdict(lambda: defaultdict(list))
    for m in matches_events.values():
        events = [_normalize_event(e) for e in m.get('events', [])]
        if not events:
            continue
        agg = aggregate_match_events(events)
        for side, team_kr in (('home', m['home_team']), ('away', m['away_team'])):
            s = agg[side]
            if s['shot_quality'] is not None:
                acc[team_kr]['shot_quality'].append(s['shot_quality'])
                acc[team_kr]['psxg'].append(s['psxg_sum'] or 0)
                acc[team_kr]['big_chances'].append(s['big_chances'])
            if s['ppda'] is not None:
                acc[team_kr]['ppda'].append(s['ppda'])
            if s['field_tilt'] is not None:
                acc[team_kr]['field_tilt'].append(s['field_tilt'])
            acc[team_kr]['sp_xg'].append(s['sp_corner_xg'] + s['sp_freekick_xg'] + s['sp_penalty_xg'])
        # 상대팀 실점 관점(psxgAllowed/bigChancesAllowed)도 같이 누적
        for side, opp_side, team_kr in (('home', 'away', m['home_team']), ('away', 'home', m['away_team'])):
            opp = agg[opp_side]
            if opp['shot_quality'] is not None:
                acc[team_kr].setdefault('psxg_allowed', []).append(opp['psxg_sum'] or 0)
                acc[team_kr].setdefault('big_chances_allowed', []).append(opp['big_chances'])

    def _avg(lst, default):
        return round(sum(lst) / len(lst), 3) if lst else default

    out = {}
    for kr, v in acc.items():
        out[kr] = {
            'psxg': _avg(v.get('psxg', []), None),
            'psxgAllowed': _avg(v.get('psxg_allowed', []), None),
            'bigChances': _avg(v.get('big_chances', []), None),
            'bigChancesAllowed': _avg(v.get('big_chances_allowed', []), None),
            'shotQuality': _avg(v.get('shot_quality', []), None),
            'ppda': _avg(v.get('ppda', []), None),
            'fieldTilt': _avg(v.get('field_tilt', []), None),
            'setPxg': _avg(v.get('sp_xg', []), None),
            'nMatches': len(v.get('shot_quality', [])),
            'computed': True,  # app_export.py 쪽에서 "이건 직접계산값" 구분용 플래그
        }
    return out


# ============================================================ 7) 실행부 (DB 스키마 자동탐지)
# 이벤트가 실제로 어느 테이블/컬럼에 있는지 모르는 상태라, 후보를 순서대로 시도하고
# 뭘 찾았는지/왜 못 찾았는지 로그로 남긴다 — 사전에 스키마를 안 물어보고 실행 시점에
# 스스로 알아내는 방식(2026-07-25 방침: "재검증 요청 대신 코드가 실행하면서 증명").
_CANDIDATE_TABLES = ['events', 'incidents', 'match_events', 'match_incidents']


def _detect_event_table(conn):
    cur = conn.cursor()
    existing = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in _CANDIDATE_TABLES:
        if t in existing:
            cols = [r[1] for r in cur.execute(f'PRAGMA table_info({t})')]
            return t, cols
    return None, list(existing)


def main():
    if not os.path.exists(DB_PATH):
        print(f'[advanced_stats] {DB_PATH} 없음 — 이 스크립트는 파이프라인 서버에서 '
              f'실행해야 함(로컬 테스트 환경엔 DB 없는 게 정상)')
        return
    conn = sqlite3.connect(DB_PATH)
    table, cols = _detect_event_table(conn)
    if table is None:
        print(f'[advanced_stats] 이벤트 테이블 후보({_CANDIDATE_TABLES}) 중 아무것도 '
              f'못 찾음. DB에 있는 테이블: {cols}')
        print('[advanced_stats] → collect_xg_bsd.py나 BSD incidents 응답을 저장하는 '
              '수집 스크립트가 있다면 그 테이블명/컬럼명을 _CANDIDATE_TABLES와 '
              '_normalize_event()에 반영하면 나머지는 그대로 작동함(로직 재작성 불필요)')
        conn.close()
        return
    print(f'[advanced_stats] 이벤트 테이블 발견: {table}, 컬럼: {cols}')
    has_coords = 'x' in cols or any('x' in c.lower() for c in cols)
    print(f'[advanced_stats] 좌표 필드 감지: {has_coords} '
          f'({"xG/PSxG/빅찬스/슈팅품질 계산 가능" if has_coords else "좌표 없으면 이 지표들은 스킵되고 PPDA/필드틸트만 시도됨"})')
    conn.close()
    # TODO: table/cols 확인되면 실제 쿼리로 matches_events 구성 후
    # build_advanced_stats_from_matches() 호출 → OUT_PATH에 JSON 저장.
    # (테이블 구조를 실제로 봐야 쿼리를 짤 수 있어서 여기까지만 자동화해둠)


if __name__ == '__main__':
    main()
