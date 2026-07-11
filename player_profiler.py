# -*- coding: utf-8 -*-
"""
선수 개인 능력치 프로파일러 (V2 Bible 2.2 'Advanced Player Metrics')

5개 능력치 카테고리를 경기 지표에서 산출:
  ① 슈팅 정밀도  : xG per Shot(기회 질), Shot Quality, Finishing Skill(G - xG)
  ② 패스 창의성  : xT from Passes, Key Passes per Match, Through Ball Success %
  ③ 수비 기여도  : True Interceptions, Pressure Efficiency(압박→탈취율),
                   PPDA Contribution(팀 수비 액션 지분)
  ④ 피지컬/활동량: Carry Distance, Carry 빈도 기반 Work Rate 추정
                   (V2 명세: 'Carry 이벤트의 속도와 빈도로 폭발력/커버리지 추정')
  ⑤ 심리/안정성  : Pass Accuracy under Pressure(압박 시 성공률 변화),
                   Error Lead to Shot

각 항목을 0~100 스케일 점수로 정규화해 레이더차트/리포트에 바로 쓸 수 있게 출력.
트래킹 데이터가 없으므로 per 90 대신 per Match 기준 사용 (Metrica 연동 시 확장).
"""
import glob
import json
import os
from collections import defaultdict


def _scale(value, lo, hi):
    """lo→0점, hi→100점 선형 스케일 (범위 밖은 절사)."""
    if value is None:
        return None
    if hi == lo:
        return 50.0
    return round(max(0.0, min(100.0, (value - lo) / (hi - lo) * 100)), 1)


def aggregate_players(metrics_dir='data/metrics'):
    """경기별 metrics JSON의 players 블록을 선수 단위로 합산 + 경기 수 집계."""
    agg = defaultdict(lambda: defaultdict(float))
    matches = defaultdict(int)
    team_def_actions = defaultdict(float)   # PPDA Contribution 분모용

    for path in glob.glob(os.path.join(metrics_dir, '*_metrics.json')):
        if path.endswith('player_profiles.json'):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        total_def = sum(t.get('defensive_actions', 0)
                        for t in data.get('teams', {}).values())
        for p, stats in data.get('players', {}).items():
            matches[p] += 1
            for k, v in stats.items():
                if isinstance(v, (int, float)):
                    agg[p][k] += v
            team_def_actions[p] += total_def / 2  # 선수 소속팀 근사
    return agg, matches, team_def_actions


def build_profile(p, s, n_matches, team_def):
    """한 선수의 5개 카테고리 프로파일 생성."""
    n = max(n_matches, 1)

    # ---------------- ① 슈팅 정밀도
    shots = s.get('shots', 0)
    xg_per_shot = s.get('xG', 0) / shots if shots else None
    finishing = s.get('goals', 0) - s.get('xG', 0) if shots else None
    shooting = {
        'shots': int(shots),
        'xG_per_shot': round(xg_per_shot, 3) if xg_per_shot is not None else None,
        'finishing_skill': round(finishing, 3) if finishing is not None else None,
        'score': _scale(xg_per_shot, 0.03, 0.30) if shots else None,
    }

    # ---------------- ② 패스 창의성
    tb = s.get('through_balls', 0)
    tb_pct = s.get('through_balls_completed', 0) / tb * 100 if tb else None
    kp_per_match = s.get('key_passes', 0) / n
    creativity_raw = s.get('xT', 0) / n
    creativity = {
        'xT_per_match': round(creativity_raw, 3),
        'key_passes_per_match': round(kp_per_match, 2),
        'through_ball_success_pct': round(tb_pct, 1) if tb_pct is not None else None,
        'score': _scale(creativity_raw * 0.6 + kp_per_match * 0.02, 0, 0.15),
    }

    # ---------------- ③ 수비 기여도
    pressures = s.get('pressures', 0)
    press_eff = s.get('pressure_regains', 0) / pressures * 100 if pressures else None
    my_def = (s.get('tackles', 0) + s.get('interceptions', 0)
              + s.get('recoveries', 0) + s.get('blocks', 0)
              + s.get('clearances', 0) + pressures)
    ppda_contrib = my_def / team_def * 100 if team_def else None
    defending = {
        'true_interceptions': int(s.get('interceptions', 0)),
        'pressure_efficiency_pct': round(press_eff, 1) if press_eff is not None else None,
        'ppda_contribution_pct': round(ppda_contrib, 1) if ppda_contrib is not None else None,
        'score': _scale((my_def / n) + (press_eff or 0) * 0.05, 0, 8),
    }

    # ---------------- ④ 피지컬/활동량 (Carry 기반 추정 — V2 명세)
    carry_dist = s.get('carry_distance', 0)
    prog_carries = s.get('progressive_carries', 0)
    work_rate = (carry_dist / n) + my_def + s.get('passes', 0) * 0.1
    physical = {
        'carry_distance_per_match': round(carry_dist / n, 1),
        'progressive_carries_per_match': round(prog_carries / n, 2),
        'work_rate_index': round(work_rate / n, 1),
        'score': _scale(carry_dist / n, 0, 300),
        'note': 'Carry 이벤트 기반 추정치 (트래킹 데이터 연동 시 정밀화)',
    }

    # ---------------- ⑤ 심리/안정성
    passes = s.get('passes', 0)
    up = s.get('passes_up', 0)
    normal_acc = s.get('passes_completed', 0) / passes * 100 if passes else None
    up_acc = s.get('passes_up_completed', 0) / up * 100 if up else None
    # 압박 시 성공률이 평상시 대비 얼마나 유지되는가 (0 = 동일)
    pressure_drop = (normal_acc - up_acc) if (normal_acc is not None
                                              and up_acc is not None) else None
    errors = s.get('errors_lead_to_shot', 0)
    composure_raw = None
    if up_acc is not None:
        composure_raw = up_acc - errors * 5
    psychology = {
        'pass_accuracy_pct': round(normal_acc, 1) if normal_acc is not None else None,
        'pass_accuracy_under_pressure_pct': round(up_acc, 1) if up_acc is not None else None,
        'accuracy_drop_under_pressure': round(pressure_drop, 1)
            if pressure_drop is not None else None,
        'errors_lead_to_shot': int(errors),
        'score': _scale(composure_raw, 40, 95) if composure_raw is not None else None,
    }

    return {
        'matches': n_matches,
        'shooting_precision': shooting,
        'pass_creativity': creativity,
        'defensive_contribution': defending,
        'physical_activity': physical,
        'psychological_stability': psychology,
    }


def main(metrics_dir='data/metrics', out_path='data/metrics/player_profiles.json'):
    agg, matches, team_def = aggregate_players(metrics_dir)
    profiles = {}
    for p, s in agg.items():
        profiles[p] = build_profile(p, s, matches[p], team_def.get(p, 0))
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(profiles, f, indent=4, ensure_ascii=False)
    print(f'[profiler] 선수 {len(profiles)}명 능력치 프로파일 생성 → {out_path}')
    return profiles


if __name__ == '__main__':
    main()
