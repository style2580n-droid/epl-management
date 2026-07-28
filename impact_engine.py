# -*- coding: utf-8 -*-
"""
임팩트 엔진 v2 — 기관급 지표 연산 (새 틀 3장 'Bible' 명세 구현)

이벤트 표준 스키마 (StatsBomb 좌표계: 120x80, 공격 방향 → x 증가):
  type      : Shot|Pass|Carry|Tackle|Interception|Recovery|Block|Clearance|
              Pressure|Duel|Aerial|Save|Claim|Sweeper
  team, player, x, y, end_x, end_y, minute, second
  outcome   : Goal|Saved|Complete|Incomplete|Won|Lost ...
  situation : Open Play|Set Piece|Penalty
  cross, through_ball : bool 태그
  shot_end_y, shot_end_z : 골문 도달 위치 (PSxG)
  freeze_frame : [{'x':..,'y':..}, ...] 패스 순간 상대 수비 위치 (Packing/Line-Breaking)
  possession_lost_at : 카운터프레스 판정용 (초 단위 경기시간)

구현 지표 (총 55+):
  공격  xG, npxG, PSxG(≈xGOT), xA, SCA, GCA, Progressive Pass/Carry/Reception,
        Box Entries, Final Third Entries, Deep Completions, Key Passes,
        Through Balls, Cross Accuracy, Carry Distance
  패스  Pass Completion %, Progressive Pass %, Forward Pass Ratio,
        Vertical Passes, Switches, Passes into Box/Final Third,
        Packing Rate, Line-Breaking Passes
  수비  Defensive Actions, Tackles Won, Interceptions, Recoveries, Blocks,
        Clearances, Pressures, Counterpress Recoveries, Defensive Duel %,
        Aerial Duel %, PPDA, xGA
  점유  Possession %, Field Tilt, Territory %, Build-up Success %
  GK    PSxG, PSxG +/-, Save %, Launch Accuracy, Cross Claims, Sweeper Actions
  AI    xT, VAEP(근사), OBV(근사)

미구현(트래킹 데이터 필요): 피지컬 전체, Defensive Line Height, Compactness,
Width — 새 틀 1.3의 Metrica Sports Open Data 연동 시 확장 예정.
"""
import json
import math
import os
from collections import defaultdict

PITCH_LENGTH, PITCH_WIDTH = 120.0, 80.0
GOAL_X, GOAL_Y = 120.0, 40.0
GOAL_HALF_WIDTH = 3.66
FINAL_THIRD_X = PITCH_LENGTH * 2 / 3          # 80
BOX_X, BOX_Y_MIN, BOX_Y_MAX = 102.0, 18.0, 62.0
DEEP_ZONE_RADIUS = 20.0                        # Deep Completion: 골문 20m 이내
PROGRESSIVE_MIN = 10.0                         # 전진 판정 최소 거리
SWITCH_MIN_LATERAL = 40.0                      # 스위치: 횡방향 40m+
COUNTERPRESS_WINDOW = 5.0                      # 소유권 상실 후 5초 내 회수


def _dist_to_goal(x, y):
    return math.hypot(GOAL_X - x, GOAL_Y - y)


# ================================================================ xG 모델
class XGModel:
    """거리+시야각 로지스틱. StatsBomb Open Data로 fit() 재학습 가능 (새 틀 3.1)."""

    def __init__(self, w0=-0.30, w_dist=-0.095, w_angle=1.30, w_setpiece=-0.25):
        self.w0, self.w_dist = w0, w_dist
        self.w_angle, self.w_setpiece = w_angle, w_setpiece

    @staticmethod
    def _features(x, y):
        dist = _dist_to_goal(x, y)
        a = math.hypot(GOAL_X - x, (GOAL_Y - GOAL_HALF_WIDTH) - y)
        b = math.hypot(GOAL_X - x, (GOAL_Y + GOAL_HALF_WIDTH) - y)
        c = 2 * GOAL_HALF_WIDTH
        cos_v = (a * a + b * b - c * c) / (2 * a * b) if a > 0 and b > 0 else 1.0
        return dist, math.acos(max(-1.0, min(1.0, cos_v)))

    def predict(self, x, y, set_piece=False, penalty=False):
        if penalty:
            return 0.76  # 페널티 고정값 (업계 표준 근사)
        d, ang = self._features(x, y)
        z = self.w0 + self.w_dist * d + self.w_angle * ang \
            + (self.w_setpiece if set_piece else 0.0)
        return 1.0 / (1.0 + math.exp(-z))

    @classmethod
    def load(cls, path='data/models/xg_coefficients.json'):
        """학습된 계수가 있으면 로드, 없으면 기본 근사 계수 사용."""
        import os as _os
        if _os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    c = json.load(f)
                return cls(c['w0'], c['w_dist'], c['w_angle'], c['w_setpiece'])
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        return cls()

    def fit(self, shots, epochs=300, lr=0.05):
        if not shots:
            return self
        data = [(self._features(s['x'], s['y'])[0],
                 self._features(s['x'], s['y'])[1],
                 1.0 if s.get('set_piece') else 0.0,
                 float(s.get('goal', 0))) for s in shots]
        n = len(data)
        for _ in range(epochs):
            g0 = gd = ga = gs = 0.0
            for d, a, sp, y in data:
                p = 1.0 / (1.0 + math.exp(-(self.w0 + self.w_dist * d
                                            + self.w_angle * a
                                            + self.w_setpiece * sp)))
                err = p - y
                g0 += err; gd += err * d; ga += err * a; gs += err * sp
            self.w0 -= lr * g0 / n; self.w_dist -= lr * gd / n
            self.w_angle -= lr * ga / n; self.w_setpiece -= lr * gs / n
        return self


def psxg(shot, xg_model):
    base = xg_model.predict(shot['x'], shot['y'],
                            shot.get('situation') == 'Set Piece',
                            shot.get('situation') == 'Penalty')
    end_y, end_z = shot.get('shot_end_y'), shot.get('shot_end_z')
    if end_y is None:
        return base
    horiz = min(abs(end_y - GOAL_Y) / GOAL_HALF_WIDTH, 1.0)
    vert = min((end_z or 0) / 2.67, 1.0)
    placement = 0.5 + 0.5 * max(horiz, vert)
    return min(base * (0.6 + 0.8 * placement), 0.99)


# ============================================================ xT / VAEP 격자
class XTGrid:
    """12x8 그리드 — 패스 전후 득점 확률 변화량 누적 (새 틀 3.1)."""

    def __init__(self, cols=12, rows=8):
        self.cols, self.rows = cols, rows
        self.grid = [[self._seed(c, r) for c in range(cols)] for r in range(rows)]

    def _seed(self, col, row):
        x_frac = (col + 0.5) / self.cols
        y_off = abs((row + 0.5) / self.rows - 0.5) * 2
        return round((0.002 + 0.28 * x_frac ** 3) * (1 - 0.35 * y_off), 5)

    def value(self, x, y):
        c = min(int(x / PITCH_LENGTH * self.cols), self.cols - 1)
        r = min(int(y / PITCH_WIDTH * self.rows), self.rows - 1)
        return self.grid[r][c]

    def delta(self, x, y, ex, ey):
        return self.value(ex, ey) - self.value(x, y)


def vaep_value(event, xt):
    """
    VAEP 근사 (새 틀 2.2 수식): V(a) = ΔP_score - ΔP_concede
    P_score ≈ 현재 위치의 xT값, P_concede ≈ 반대 진영 미러 위치의 xT값.
    성공 액션은 도착 지점 기준, 실패(턴오버)는 상대 관점 가치로 페널티.
    """
    x, y = event.get('x'), event.get('y')
    ex = event.get('end_x', x)
    ey = event.get('end_y', y)
    if x is None:
        return 0.0
    p_score_before = xt.value(x, y)
    p_concede_before = xt.value(PITCH_LENGTH - x, PITCH_WIDTH - y)
    failed = event.get('outcome') in ('Incomplete', 'Lost', 'Out')
    if failed:
        # 턴오버: 상대가 그 지점에서 공격 시작 → 실점 확률 상승
        p_score_after = 0.0
        p_concede_after = xt.value(PITCH_LENGTH - ex, PITCH_WIDTH - ey) + 0.01
    else:
        p_score_after = xt.value(ex, ey)
        p_concede_after = xt.value(PITCH_LENGTH - ex, PITCH_WIDTH - ey)
    return (p_score_after - p_score_before) - (p_concede_after - p_concede_before)


# ============================================================ 프리즈프레임 지표
def packing_count(event):
    """
    Packing Rate: 패스/캐리로 제친 상대 수 (새 틀 3.1 '좌표 데이터 기반 추정').
    freeze_frame(상대 좌표)가 있을 때: 시작 x와 종료 x 사이에 있던 상대 수.
    """
    ff = event.get('freeze_frame')
    if not ff or 'end_x' not in event:
        return None
    x0, x1 = event['x'], event['end_x']
    if x1 <= x0:
        return 0
    return sum(1 for d in ff if x0 < d.get('x', -1) < x1)


def is_line_breaking(event, min_bypassed=2, y_corridor=15.0):
    """패스 진행 경로(±y_corridor) 안에서 2명 이상 제치면 라인 브레이킹."""
    ff = event.get('freeze_frame')
    if not ff or 'end_x' not in event:
        return None
    x0, y0 = event['x'], event['y']
    x1, y1 = event['end_x'], event['end_y']
    if x1 <= x0:
        return False
    mid_y = (y0 + y1) / 2
    bypassed = sum(1 for d in ff
                   if x0 < d.get('x', -1) < x1
                   and abs(d.get('y', 999) - mid_y) <= y_corridor)
    return bypassed >= min_bypassed


# ============================================================ 판정 헬퍼
def _in_box(x, y):
    return x >= BOX_X and BOX_Y_MIN <= y <= BOX_Y_MAX


def _is_progressive(e):
    if 'end_x' not in e or e['end_x'] is None:
        return False
    return (e['end_x'] - e['x']) >= PROGRESSIVE_MIN or \
        (_in_box(e.get('end_x', 0), e.get('end_y', 40)) and not _in_box(e['x'], e['y']))


def _pass_complete(e):
    return e.get('outcome') in ('Complete', 'Goal', None) and \
        e.get('outcome') != 'Incomplete'


# ============================================================ 점유 시퀀스
ON_BALL_TYPES = ('Pass', 'Carry', 'Shot', 'Dribble')
TURNOVER_TYPES = ('Recovery', 'Interception', 'Tackle')
COUNTER_WINDOW = 15.0  # 역습 판정: 탈취 후 15초 내 파이널서드/슛


def _event_time(e):
    return e.get('minute', 0) * 60 + e.get('second', 0)


def segment_possessions(events, teams):
    """이벤트 로그를 팀별 점유 시퀀스로 분할."""
    possessions = []
    cur = None
    pending_turnover = {}  # team -> 탈취 시각 (직후 점유 시작 시 역습 플래그)
    for e in events:
        t = e.get('team')
        if t not in teams:
            continue
        et = e.get('type')
        if et in TURNOVER_TYPES:
            pending_turnover[t] = _event_time(e)
            continue
        if et not in ON_BALL_TYPES:
            continue
        if cur is None or cur['team'] != t:
            started_by_turnover = False
            to_time = pending_turnover.get(t)
            if to_time is not None and _event_time(e) - to_time <= 5:
                started_by_turnover = True
            pending_turnover.pop(t, None)
            cur = {'team': t, 'start_x': e.get('x'), 'start_t': _event_time(e),
                   'turnover_start': started_by_turnover,
                   'reached_ft_t': None, 'has_shot': False, 'n_events': 0}
            possessions.append(cur)
        cur['n_events'] += 1
        xs = [e.get('x')]
        if e.get('end_x') is not None:
            xs.append(e['end_x'])
        if cur['reached_ft_t'] is None and any(
                v is not None and v >= FINAL_THIRD_X for v in xs):
            cur['reached_ft_t'] = _event_time(e)
        if et == 'Shot':
            cur['has_shot'] = True
    return possessions


def tactical_metrics(possessions, team, opponent):
    """팀 전술 지표 산출 (Buildup/역습/전환)."""
    mine = [p for p in possessions if p['team'] == team]
    theirs = [p for p in possessions if p['team'] == opponent]

    # Build-up: 자기 진영 1/3에서 시작한 점유
    buildups = [p for p in mine
                if p['start_x'] is not None and p['start_x'] < PITCH_LENGTH / 3]
    bu_success = [p for p in buildups if p['reached_ft_t'] is not None]
    opp_buildups = [p for p in theirs
                    if p['start_x'] is not None and p['start_x'] < PITCH_LENGTH / 3]
    opp_bu_fail = [p for p in opp_buildups if p['reached_ft_t'] is None]

    # Buildup Speed: 성공 빌드업의 자진영→파이널서드 평균 소요 초
    speeds = [p['reached_ft_t'] - p['start_t'] for p in bu_success
              if p['reached_ft_t'] is not None and p['reached_ft_t'] >= p['start_t']]

    # Counter Attack: 볼 탈취 직후 시작 + 15초 내 파이널서드 도달/슛
    counters = [p for p in mine if p['turnover_start']
                and ((p['reached_ft_t'] is not None
                      and p['reached_ft_t'] - p['start_t'] <= COUNTER_WINDOW)
                     or p['has_shot'])]
    counter_shots = [p for p in counters if p['has_shot']]

    return {
        'buildup_attempts': len(buildups),
        'buildup_success_pct': round(len(bu_success) / len(buildups) * 100, 1)
            if buildups else None,
        'buildup_disruption_pct': round(len(opp_bu_fail) / len(opp_buildups) * 100, 1)
            if opp_buildups else None,
        'buildup_speed_sec': round(sum(speeds) / len(speeds), 1) if speeds else None,
        'counter_attacks': len(counters),
        'counter_attack_freq_pct': round(
            len(counters) / len([p for p in mine if p['turnover_start']]) * 100, 1)
            if any(p['turnover_start'] for p in mine) else None,
        'transition_efficiency_pct': round(
            len(counter_shots) / len(counters) * 100, 1) if counters else None,
    }



# ============================================================ V4 Proxy Logic
def _convex_hull_area(points):
    """트래킹 없이 이벤트 좌표로 Compactness 추정 — 순수 파이썬 Convex Hull
    (Andrew's monotone chain) + Shoelace 면적. scipy 불필요."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return 0.0
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    area = 0.0
    for i in range(len(hull)):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % len(hull)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


MAX_HUMAN_SPEED = 11.0   # m/s 상한 (이벤트 간 순간이동 왜곡 방지)
SPRINT_SPEED = 7.0       # V4: 초당 7m 초과 = 스프린트


def estimate_physical(events, teams):
    """V4 Proxy: 이벤트 간 거리/시간 역산으로 선수별 피지컬 추정.
    주의 — 온볼 이벤트 순간만 포착되므로 실제 활동량의 하한 추정치."""
    by_player = {}
    for e in events:
        if e.get('team') not in teams:
            continue
        pl, x, y = e.get('player'), e.get('x'), e.get('y')
        if pl is None or x is None or y is None:
            continue
        by_player.setdefault(pl, []).append((_event_time(e), x, y))

    out = {}
    for pl, pts in by_player.items():
        pts.sort()
        top, dist_sum, load, sprints, prev_speed = 0.0, 0.0, 0.0, 0, None
        for (t0, x0, y0), (t1, x1, y1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0:
                continue
            d = math.hypot(x1 - x0, y1 - y0)
            speed = min(d / dt, MAX_HUMAN_SPEED)
            dist_sum += min(d, speed * dt)
            top = max(top, speed)
            if speed > SPRINT_SPEED:
                sprints += 1
            if prev_speed is not None:
                load += abs(speed - prev_speed)   # Player Load(속도 변화량 합)
            prev_speed = speed
        out[pl] = {
            'top_speed_est_kmh': round(top * 3.6, 1),
            'sprints_est': sprints,
            'distance_covered_est': round(dist_sum, 1),
            'player_load_est': round(load, 1),
            'note': '온볼 이벤트 기반 하한 추정치 (V4 Proxy Logic)',
        }
    return out


def compute_match_metrics(events, home, away, xg_model=None, xt=None):
    xg_model = xg_model or XGModel.load()   # 학습 계수 자동 반영
    xt = xt or XTGrid()
    teams = {home: away, away: home}

    # 2026-07-27 추가: players 딕셔너리가 항상 빈 채로 나오는 원인 진단.
    # events가 애초에 비어있는지, 아니면 이벤트는 있는데 e.get('team')이
    # home/away 문자열과 안 맞아서 continue로 다 걸러지는지 확정한다.
    global _diag_done_impact
    try:
        _diag_done_impact
    except NameError:
        _diag_done_impact = False
    if not _diag_done_impact:
        _diag_done_impact = True
        sample_teams = [e.get('team') for e in events[:5]] if events else []
        print(f'[impact_engine] [diag] events 길이: {len(events)}, '
              f'home={home!r}, away={away!r}', flush=True)
        print(f'[impact_engine] [diag] 앞 5개 이벤트의 team 필드값: '
              f'{sample_teams}', flush=True)
        if events:
            print(f'[impact_engine] [diag] 첫 이벤트 전체 키: '
                  f'{sorted(events[0].keys())}', flush=True)

    T = {t: defaultdict(float) for t in teams}       # 팀 지표
    P = defaultdict(lambda: defaultdict(float))       # 선수 지표
    last_key_pass = {}                                 # xA: 슛 직전 키패스 추적
    last_possession_loss = {}                          # 카운터프레스용
    def_x_coords = {}                                  # 수비 라인 높이 (V4 Proxy)
    team_coords = {}                                   # Compactness/Width (V4 Proxy)

    for i, e in enumerate(events):
        t = e.get('team')
        if t not in teams:
            continue
        pl = e.get('player', 'Unknown')
        et = e.get('type')
        if pl != 'Unknown' and '_team' not in P[pl]:
            P[pl]['_team'] = t          # Big Match Performance 귀속용
        x, y = e.get('x'), e.get('y')

        # ---------------- Possession/Territory 근사 (이벤트 점유 비율)
        T[t]['events'] += 1
        if x is not None and y is not None:
            team_coords.setdefault(t, []).append((x, y))     # Compactness/Width
            if _in_box(x, y) and et in ON_BALL_TYPES:
                T[t]['touches_in_box'] += 1                  # V5 2.1-8
                P[pl]['touches_in_box'] += 1
        if x is not None and x >= PITCH_LENGTH / 2:
            T[t]['events_opp_half'] += 1

        # ================================================== Shot
        if et == 'Shot':
            sp = e.get('situation') == 'Set Piece'
            pen = e.get('situation') == 'Penalty'
            xg = e.get('xg') or xg_model.predict(x, y, sp, pen)
            T[t]['xG'] += xg
            P[pl]['xG'] += xg
            P[pl]['shots'] += 1
            if not pen:
                T[t]['npxG'] += xg
            T[t]['shots'] += 1
            T[t]['shot_dist_sum'] += _dist_to_goal(x, y)     # Distance per Shot
            on_target = e.get('outcome') in ('Goal', 'Saved')
            if on_target:
                T[t]['shots_on_target'] += 1
                v = psxg(e, xg_model)
                T[t]['PSxG'] += v
                T[teams[t]]['PSxG_faced'] += v      # 상대 GK 관점
            if e.get('outcome') == 'Goal':
                T[t]['goals'] += 1
                P[pl]['goals'] += 1
            # xA / Key Pass: 직전 같은 팀 패스 성공자에게 귀속
            kp = last_key_pass.get(t)
            if kp is not None:
                P[kp]['key_passes'] += 1
                P[kp]['xA'] += xg
                if xg >= 0.3:                                 # Big Chance 기준
                    P[kp]['big_chances_created'] += 1
                    T[t]['big_chances_created'] += 1
                    # 2026-07-25 추가: PSxG_faced와 동일 패턴으로 상대팀 관점(허용) 미러링.
                    # app_export.py의 ADVANCED_STATS.bigChancesAllowed가 여태 항상
                    # 기본값(2.0)이었던 이유가 이 필드 자체가 없었기 때문 — 추가함.
                    T[teams[t]]['big_chances_allowed'] += 1
                T[t]['xA'] += xg
                if e.get('outcome') == 'Goal':
                    P[kp]['assists'] += 1
            # SCA/GCA: 직전 2개 기여 액션
            found, j = 0, i - 1
            while j >= 0 and found < 2:
                pr = events[j]
                if pr.get('team') == t and pr.get('type') in \
                        ('Pass', 'Carry', 'Dribble', 'Foul Won'):
                    who = pr.get('player', 'Unknown')
                    P[who]['SCA'] += 1
                    if e.get('outcome') == 'Goal':
                        P[who]['GCA'] += 1
                    found += 1
                j -= 1
            last_key_pass[t] = None

        # ================================================== Pass
        elif et == 'Pass':
            T[t]['passes'] += 1
            P[pl]['passes'] += 1
            if x is not None and x <= PITCH_LENGTH * 0.4:
                T[t]['passes_own40'] += 1        # 자진영 40% 빌드업 패스 (PPDA 분자)
            complete = _pass_complete(e)
            # 심리/안정성: 압박(under_pressure) 상황 패스 성공률 (새 틀 V2 2.2)
            if e.get('under_pressure'):
                P[pl]['passes_up'] += 1
                T[t]['passes_up'] += 1                    # Press Resistance(팀)
                if complete:
                    P[pl]['passes_up_completed'] += 1
                    T[t]['passes_up_completed'] += 1
            if complete:
                T[t]['passes_completed'] += 1
                P[pl]['passes_completed'] += 1
                last_key_pass[t] = pl
            else:
                T[t]['possession_losses'] += 1            # 카운터프레스 분모
                last_key_pass[t] = None
                last_possession_loss[t] = e.get('minute', 0) * 60 + e.get('second', 0)
                # 심리/안정성: Error Lead to Shot — 실수 후 3이벤트 내 상대 슛
                for nxt in events[i + 1:i + 4]:
                    if nxt.get('team') == teams[t] and nxt.get('type') == 'Shot':
                        P[pl]['errors_lead_to_shot'] += 1
                        break
            ex, ey = e.get('end_x'), e.get('end_y')
            if ex is not None:
                if ex - x > 0:
                    T[t]['forward_passes'] += 1
                if ex - x >= 15 and abs(ey - y) <= 8:
                    T[t]['vertical_passes'] += 1
                    P[pl]['vertical_passes'] += 1
                if abs(ey - y) >= SWITCH_MIN_LATERAL:
                    T[t]['switches'] += 1
                    P[pl]['switches'] += 1
                if _is_progressive(e):
                    T[t]['progressive_passes'] += 1
                    P[pl]['progressive_passes'] += 1
                    if complete:
                        receiver = e.get('receiver')
                        if receiver:
                            P[receiver]['progressive_receptions'] += 1
                if complete and ex >= FINAL_THIRD_X > x:
                    T[t]['final_third_entries'] += 1
                    T[t]['passes_into_final_third'] += 1
                if complete and _in_box(ex, ey) and not _in_box(x, y):
                    T[t]['box_entries'] += 1
                    T[t]['passes_into_box'] += 1
                    P[pl]['passes_into_box'] += 1
                if complete and _dist_to_goal(ex, ey) <= DEEP_ZONE_RADIUS \
                        and not e.get('cross'):
                    T[t]['deep_completions'] += 1
                    P[pl]['deep_completions'] += 1
                if e.get('cross'):
                    T[t]['crosses'] += 1
                    if complete:
                        T[t]['crosses_completed'] += 1
                if e.get('through_ball'):
                    T[t]['through_balls'] += 1
                    P[pl]['through_balls'] += 1
                    if complete:
                        P[pl]['through_balls_completed'] += 1
                # Long Ball Accuracy: 30야드+ 패스 (V5 2.2-4)
                pass_dist = math.hypot(ex - x, ey - y)
                if pass_dist >= 30:
                    T[t]['long_balls'] += 1
                    if complete:
                        T[t]['long_balls_completed'] += 1
                # Crosses into Penalty Area (V5 2.2-7)
                if e.get('cross') and complete and _in_box(ex, ey):
                    T[t]['crosses_into_box'] += 1
                # Progressive Distance: 전방 이동분 합산 (V5 2.4-11)
                if complete and ex > x:
                    T[t]['progressive_distance'] += ex - x
                    P[pl]['progressive_distance'] += ex - x
                d_xt = xt.delta(x, y, ex, ey)
                if complete and d_xt > 0:
                    T[t]['xT'] += d_xt
                    P[pl]['xT'] += d_xt
                pk = packing_count(e)
                if pk is not None and complete:
                    T[t]['packing'] += pk
                    P[pl]['packing'] += pk
                lb = is_line_breaking(e)
                if lb:
                    T[t]['line_breaking_passes'] += 1
                    P[pl]['line_breaking_passes'] += 1
                # Smart Pass (Wyscout 정의 근사): 수비 조직을 깨는 창의적 전진 패스
                if complete and (ex - x) >= 15 and ex >= FINAL_THIRD_X and \
                        (e.get('through_ball') or lb or _in_box(ex, ey)):
                    T[t]['smart_passes'] += 1
                    P[pl]['smart_passes'] += 1
            v = vaep_value(e, xt)
            T[t]['VAEP'] += v
            P[pl]['VAEP'] += v
            P[pl]['OBV_pass'] += v                      # OBV Passing Value
            # xOVA: 공격 방향 가치 증가분만 누적 (실점 위험 제외한 순수 공격 기여)
            if 'end_x' in e and e['end_x'] is not None:
                off = xt.value(e['end_x'], e['end_y']) - xt.value(x, y)
                if _pass_complete(e) and off > 0:
                    T[t]['xOVA'] += off
                    P[pl]['xOVA'] += off

        # ================================================== Carry
        elif et == 'Carry':
            ex, ey = e.get('end_x'), e.get('end_y')
            if ex is not None:
                dist = math.hypot(ex - x, ey - y)
                T[t]['carry_distance'] += dist
                P[pl]['carry_distance'] += dist
                if ex > x:
                    T[t]['progressive_distance'] += ex - x
                    P[pl]['progressive_distance'] += ex - x
                if _is_progressive(e):
                    T[t]['progressive_carries'] += 1
                    P[pl]['progressive_carries'] += 1
                if ex >= FINAL_THIRD_X > x:
                    T[t]['final_third_entries'] += 1
                if _in_box(ex, ey) and not _in_box(x, y):
                    T[t]['box_entries'] += 1
                d_xt = xt.delta(x, y, ex, ey)
                if d_xt > 0:
                    T[t]['xT'] += d_xt
                    P[pl]['xT'] += d_xt
                pk = packing_count(e)
                if pk is not None:
                    T[t]['packing'] += pk
                    P[pl]['packing'] += pk
            v = vaep_value(e, xt)
            T[t]['VAEP'] += v
            P[pl]['VAEP'] += v

        # ================================================== 수비 액션
        elif et in ('Tackle', 'Interception', 'Recovery', 'Block', 'Clearance',
                    'Pressure'):
            T[t]['defensive_actions'] += 1
            if x is not None:
                def_x_coords.setdefault(t, []).append(x)     # Defensive Line Height
                if x >= PITCH_LENGTH * 0.6 and et in \
                        ('Tackle', 'Interception', 'Pressure', 'Foul'):
                    T[t]['def_actions_opp40'] += 1   # 상대진영 40% 압박 (PPDA 분모)
                if x >= FINAL_THIRD_X:
                    T[t]['def_actions_final_third'] += 1     # V5 2.3-10
                if et in TURNOVER_TYPES:
                    if x >= PITCH_LENGTH / 2:
                        T[t]['ball_wins_opp_half'] += 1      # V5 2.3-13
                    if _dist_to_goal(x, y) <= 40:
                        T[t]['high_turnovers'] += 1          # V5 2.3-15
            key = {'Tackle': 'tackles', 'Interception': 'interceptions',
                   'Recovery': 'recoveries', 'Block': 'blocks',
                   'Clearance': 'clearances', 'Pressure': 'pressures'}[et]
            T[t][key] += 1
            P[pl][key] += 1
            if et == 'Tackle' and e.get('outcome') == 'Won':
                T[t]['tackles_won'] += 1
                P[pl]['tackles_won'] += 1
            # 수비 기여도: Pressure Efficiency — 압박 직후 2이벤트 내 아군 회수
            if et == 'Pressure':
                for nxt in events[i + 1:i + 3]:
                    if nxt.get('team') == t and nxt.get('type') in \
                            ('Recovery', 'Interception', 'Tackle'):
                        P[pl]['pressure_regains'] += 1
                        break
            # 카운터프레스: 소유권 상실 5초 내 회수 (새 틀 수비 지표)
            if et == 'Recovery':
                lost_at = last_possession_loss.get(t)
                now_s = e.get('minute', 0) * 60 + e.get('second', 0)
                if lost_at is not None and 0 <= now_s - lost_at <= COUNTERPRESS_WINDOW:
                    T[t]['counterpress_recoveries'] += 1
                    P[pl]['counterpress_recoveries'] += 1

        # ================================================== 듀얼
        elif et == 'Dribble':
            T[t]['dribbles'] += 1
            P[pl]['dribbles'] += 1
            if e.get('outcome') == 'Won':
                T[t]['dribbles_won'] += 1
                P[pl]['dribbles_won'] += 1
                T[teams[t]]['dribbled_past'] += 1            # 상대팀 돌파허용 (V5 2.3-11)
                beaten = e.get('opponent')
                if beaten:
                    P[beaten]['dribbled_past'] += 1

        elif et in ('Yellow Card', 'Red Card', 'Card'):
            card = e.get('detail', et)
            k = 'red_cards' if 'Red' in card else 'yellow_cards'
            T[t][k] += 1
            P[pl][k] += 1

        elif et == 'Foul':
            T[t]['fouls_committed'] += 1
            P[pl]['fouls_committed'] += 1
        elif et == 'Foul Won':
            T[t]['fouls_suffered'] += 1
            P[pl]['fouls_suffered'] += 1
        elif et == 'Offside':
            T[t]['offsides'] += 1
            P[pl]['offsides'] += 1

        elif et in ('Duel', 'Aerial'):
            k = 'duels' if et == 'Duel' else 'aerials'
            T[t][k] += 1
            if e.get('outcome') == 'Won':
                T[t][f'{k}_won'] += 1
                P[pl][f'{k}_won'] += 1

        # ================================================== GK
        elif et == 'Save':
            T[t]['saves'] += 1
            P[pl]['saves'] += 1
        elif et == 'Claim':
            T[t]['cross_claims'] += 1
            P[pl]['cross_claims'] += 1
        elif et == 'Sweeper':
            T[t]['sweeper_actions'] += 1
            P[pl]['sweeper_actions'] += 1
        elif et == 'Launch':
            T[t]['launches'] += 1
            if _pass_complete(e):
                T[t]['launches_completed'] += 1

    # ================================================== 파생/비율 지표
    possessions = segment_possessions(events, teams)
    result_teams = {}
    for t, opp in teams.items():
        d = dict(T[t])
        # ---- 팀 전술 지표 (새로 추가된 10종)
        d.update(tactical_metrics(possessions, t, opp))
        d['press_resistance_pct'] = round(
            T[t]['passes_up_completed'] / T[t]['passes_up'] * 100, 1) \
            if T[t]['passes_up'] else None
        d['press_intensity'] = round(
            T[t]['pressures'] / T[opp]['passes'] * 100, 1) \
            if T[opp]['passes'] else None          # 상대 패스 100회당 압박 수
        d['counterpress_success_pct'] = round(
            T[t]['counterpress_recoveries'] / T[t]['possession_losses'] * 100, 1) \
            if T[t]['possession_losses'] else None
        total_ev = T[t]['events'] + T[opp]['events']
        d['possession_pct'] = round(T[t]['events'] / total_ev * 100, 1) if total_ev else None
        opp_half = T[t]['events_opp_half'] + T[opp]['events_opp_half']
        d['territory_pct'] = round(T[t]['events_opp_half'] / opp_half * 100, 1) if opp_half else None
        d['pass_completion_pct'] = round(
            T[t]['passes_completed'] / T[t]['passes'] * 100, 1) if T[t]['passes'] else None
        d['progressive_pass_pct'] = round(
            T[t]['progressive_passes'] / T[t]['passes'] * 100, 1) if T[t]['passes'] else None
        d['forward_pass_ratio'] = round(
            T[t]['forward_passes'] / T[t]['passes'], 2) if T[t]['passes'] else None
        d['cross_accuracy_pct'] = round(
            T[t]['crosses_completed'] / T[t]['crosses'] * 100, 1) if T[t]['crosses'] else None
        d['defensive_duel_pct'] = round(
            T[t]['duels_won'] / T[t]['duels'] * 100, 1) if T[t]['duels'] else None
        d['aerial_duel_pct'] = round(
            T[t]['aerials_won'] / T[t]['aerials'] * 100, 1) if T[t]['aerials'] else None
        d['xGA'] = round(T[opp]['xG'], 3)
        # ---- V5 파생 비율 지표
        d['shots_on_target_pct'] = round(
            T[t]['shots_on_target'] / T[t]['shots'] * 100, 1) if T[t]['shots'] else None
        d['distance_per_shot'] = round(
            T[t]['shot_dist_sum'] / T[t]['shots'], 1) if T[t]['shots'] else None
        d['goals_minus_xG'] = round(T[t]['goals'] - T[t]['xG'], 3)
        d['dribble_success_pct'] = round(
            T[t]['dribbles_won'] / T[t]['dribbles'] * 100, 1) if T[t]['dribbles'] else None
        d['long_ball_accuracy_pct'] = round(
            T[t]['long_balls_completed'] / T[t]['long_balls'] * 100, 1) \
            if T[t]['long_balls'] else None
        d['clean_sheet'] = 1 if T[opp]['goals'] == 0 else 0
        # ---- V4 Proxy 팀 전술 지표
        dx = def_x_coords.get(t, [])
        d['defensive_line_height'] = round(sum(dx) / len(dx), 1) if dx else None
        tc = team_coords.get(t, [])
        if tc:
            ys = [p[1] for p in tc]
            d['width_est'] = round(max(ys) - min(ys), 1)
            d['compactness_area_est'] = round(_convex_hull_area(tc), 1)
        else:
            d['width_est'] = d['compactness_area_est'] = None
        # PPDA: 상대의 자진영 40% 빌드업 패스 / 내 상대진영 40% 압박 액션
        # (V5 2.3-1 수식 기준 — 전 구역 근사식에서 구역 기준으로 교정)
        opp_buildup = T[opp]['passes_own40']
        my_press = T[t]['def_actions_opp40']
        d['PPDA'] = round(opp_buildup / my_press, 2) if my_press else None
        # GK 파생
        shots_faced = T[opp]['shots_on_target']
        d['save_pct'] = round(T[t]['saves'] / shots_faced * 100, 1) if shots_faced else None
        d['PSxG_plus_minus'] = round(T[t]['PSxG_faced'] - T[opp]['goals'], 3) \
            if 'PSxG_faced' in T[t] else None
        d['launch_accuracy_pct'] = round(
            T[t]['launches_completed'] / T[t]['launches'] * 100, 1) if T[t]['launches'] else None
        # Field Tilt
        ft_mine = T[t]['passes_into_final_third'] + T[t].get('final_third_entries', 0)
        ft_opp = T[opp]['passes_into_final_third'] + T[opp].get('final_third_entries', 0)
        d['field_tilt_pct'] = round(ft_mine / (ft_mine + ft_opp) * 100, 1) \
            if (ft_mine + ft_opp) else None
        for k in ('xG', 'npxG', 'xA', 'PSxG', 'xT', 'VAEP', 'xOVA',
                  'carry_distance', 'progressive_distance', 'shot_dist_sum'):
            if k in d:
                d[k] = round(d[k], 3)
        d.pop('events', None)
        d.pop('events_opp_half', None)
        result_teams[t] = d

    players = {p: {k: (round(v, 3) if isinstance(v, float) else v)
                   for k, v in stats.items()}
               for p, stats in P.items()}

    physical = estimate_physical(events, teams)
    for pl, ph in physical.items():
        if pl in players:
            players[pl].update(ph)

    return {'teams': result_teams, 'players': players}


def run_from_file(events_path, out_dir='data/metrics'):
    with open(events_path, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    if isinstance(payload, dict) and 'events' in payload:
        events, home, away = payload['events'], payload['home'], payload['away']
    else:
        raise ValueError('이벤트 파일에 home/away/events 필드가 필요합니다')
    result = compute_match_metrics(events, home, away)
    os.makedirs(out_dir, exist_ok=True)
    name = os.path.splitext(os.path.basename(events_path))[0]
    out = os.path.join(out_dir, f'{name}_metrics.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=4, ensure_ascii=False)
    print(f'[metrics] {out} 저장')
    return result


if __name__ == '__main__':
    import sys
    import glob
    paths = sys.argv[1:] or glob.glob('data/events/*.json')
    for p in paths:
        try:
            run_from_file(p)
        except Exception as ex:
            print(f'[metrics] {p} 실패: {ex}')
