import sys
sys.path.insert(0, '/home/claude/work')
from compute_advanced_stats import (
    calc_xg, calc_psxg, is_big_chance, calc_ppda, calc_field_tilt,
    aggregate_match_events, _normalize_event, _shot_distance_angle,
)


def check(cond, msg):
    if not cond:
        raise AssertionError('FAIL: ' + msg)
    print('OK:', msg)


# 1) 골대 정면 가까운 거리 슈팅이 먼 거리보다 xG 높아야 함
xg_close = calc_xg(95, 50)   # 골대 5m 앞, 정중앙
xg_far = calc_xg(70, 50)     # 골대 30m 앞, 정중앙
check(xg_close > xg_far, f'가까운 슈팅(x=95)이 먼 슈팅(x=70)보다 xG 높아야 함 (close={xg_close}, far={xg_far})')

# 2) 각도 나쁜(터치라인 쪽) 슈팅이 정중앙보다 xG 낮아야 함
xg_center = calc_xg(90, 50)
xg_wide = calc_xg(90, 10)   # 같은 거리, 옆쪽 각도
check(xg_center > xg_wide, f'정중앙(y=50)이 측면(y=10)보다 xG 높아야 함 (center={xg_center}, wide={xg_wide})')

# 3) 헤더가 발슛보다 xG 낮아야 함(같은 위치)
xg_foot = calc_xg(90, 50, body_part='foot')
xg_head = calc_xg(90, 50, body_part='head')
check(xg_foot > xg_head, f'발슛이 헤더보다 xG 높아야 함 (foot={xg_foot}, head={xg_head})')

# 4) 페널티는 위치 무관 고정값
check(calc_xg(90, 50, situation='penalty') == calc_xg(95, 20, situation='penalty'),
      '페널티킥은 좌표 달라도 xG 동일해야 함')

# 5) 좌표 없으면 None(크래시 아님)
check(calc_xg(None, 50) is None, '좌표 없으면 xG는 None')

# 6) 빅찬스 임계값
check(is_big_chance(0.35) is True, 'xG 0.35는 빅찬스')
check(is_big_chance(0.1) is False, 'xG 0.1은 빅찬스 아님')
check(is_big_chance(None) is False, 'xG None은 빅찬스 아님(크래시 아님)')

# 7) PSxG: 오프타겟/블락은 사전xG보다 낮아야 함
shot_ontarget = {'x': 90, 'y': 50, 'body_part': 'foot', 'situation': 'open_play', 'outcome': 'saved'}
shot_offtarget = {'x': 90, 'y': 50, 'body_part': 'foot', 'situation': 'open_play', 'outcome': 'off_target'}
psxg_on = calc_psxg(shot_ontarget)
psxg_off = calc_psxg(shot_offtarget)
check(psxg_on > psxg_off, f'온타겟 psxg({psxg_on})가 오프타겟 psxg({psxg_off})보다 커야 함')

# 8) PPDA: 분모(상대 패스) 많고 분자(우리 수비액션) 적으면 PPDA 커야(압박 약함) 함
ppda_low_press = calc_ppda(team_def_actions_opp_third=5, opp_passes_own_third=50)   # 압박 약함(PPDA 큼)
ppda_high_press = calc_ppda(team_def_actions_opp_third=20, opp_passes_own_third=50)  # 압박 강함(PPDA 작음)
check(ppda_low_press > ppda_high_press, f'수비액션 적을수록 PPDA 커야(압박 약함) 함 (약함={ppda_low_press}, 강함={ppda_high_press})')
check(calc_ppda(0, 50) is None, '수비액션 0건이면 None(0으로 나누기 방지)')
check(calc_ppda(5, 0) is None, '상대 패스 0건이면 None')

# 9) 필드틸트: 우리 터치가 많을수록 100에 가까워야 함
tilt_dominant = calc_field_tilt(80, 20)
tilt_even = calc_field_tilt(50, 50)
check(tilt_dominant > tilt_even, f'우리 터치 압도적이면 필드틸트 더 높아야 함 ({tilt_dominant} vs {tilt_even})')
check(calc_field_tilt(0, 0) is None, '터치 합계 0이면 None')
check(tilt_even == 50.0, f'터치 동일하면 필드틸트 정확히 50이어야 함 (실제={tilt_even})')

# 10) aggregate_match_events: 홈이 슈팅 좋은 위치서 많이 쏘고 원정은 멀리서만 쐈다면
#     홈 shot_quality가 원정보다 높아야 함
synthetic_events = [
    {'type': 'shot', 'is_home': True, 'x': 92, 'y': 50, 'body_part': 'foot', 'situation': 'open_play', 'outcome': 'goal'},
    {'type': 'shot', 'is_home': True, 'x': 88, 'y': 45, 'body_part': 'foot', 'situation': 'open_play', 'outcome': 'saved'},
    {'type': 'shot', 'is_home': False, 'x': 65, 'y': 50, 'body_part': 'foot', 'situation': 'open_play', 'outcome': 'off_target'},
    {'type': 'shot', 'is_home': False, 'x': 60, 'y': 15, 'body_part': 'foot', 'situation': 'open_play', 'outcome': 'off_target'},
    {'type': 'tackle', 'is_home': True, 'x': 70, 'y': 50},
    {'type': 'tackle', 'is_home': True, 'x': 75, 'y': 40},
    {'type': 'pass', 'is_home': False, 'x': 60, 'y': 50},
    {'type': 'pass', 'is_home': False, 'x': 55, 'y': 50},
]
result = aggregate_match_events(synthetic_events)
check(result['home']['shot_quality'] > result['away']['shot_quality'],
      f"홈이 더 좋은 위치서 쐈으니 shot_quality 더 높아야 함 (home={result['home']['shot_quality']}, away={result['away']['shot_quality']})")
check(result['home']['n_shots'] == 2, '홈 슈팅 2개로 집계됐는지')
check(result['away']['n_shots'] == 2, '원정 슈팅 2개로 집계됐는지')
check(result['home']['ppda'] is not None, '홈 PPDA 계산됐는지(태클 있으니)')

# 11) _normalize_event: BSD 원시 이벤트 형태 흉내내서 정규화 확인
raw = {'type': 'shot', 'is_home': True, 'x': 90, 'y': 50, 'body_part': 'foot'}
norm = _normalize_event(raw)
check(norm['type'] == 'shot' and norm['is_home'] is True and norm['x'] == 90,
      '_normalize_event가 기본 필드 잘 매핑하는지')

print('\n전부 통과.')

# 12) build_advanced_stats_from_matches: 시즌 여러 경기 누적해서 app_export.py 출력형태로
from compute_advanced_stats import build_advanced_stats_from_matches
matches_events = {
    'm1': {'home_team': '팀A', 'away_team': '팀B', 'events': synthetic_events},
    'm2': {'home_team': '팀A', 'away_team': '팀C', 'events': [
        {'type': 'shot', 'is_home': True, 'x': 91, 'y': 48, 'body_part': 'foot', 'situation': 'open_play', 'outcome': 'goal'},
        {'type': 'shot', 'is_home': False, 'x': 55, 'y': 50, 'body_part': 'foot', 'situation': 'open_play', 'outcome': 'off_target'},
    ]},
}
season_out = build_advanced_stats_from_matches(matches_events)
check('팀A' in season_out, '팀A가 결과에 있어야 함')
check(season_out['팀A']['nMatches'] == 2, f"팀A는 2경기 집계돼야 함(실제={season_out['팀A']['nMatches']})")
check(season_out['팀A']['computed'] is True, 'computed 플래그 True여야 함(직접계산값 표시)')
check(season_out['팀A']['shotQuality'] > season_out.get('팀B', {}).get('shotQuality', 0) if '팀B' in season_out else True,
      '팀A(위치 좋은 슈팅 많음)가 shotQuality 더 높아야 함')
print("\n(build_advanced_stats_from_matches 결과 샘플)")
print(season_out['팀A'])
print('\n전부 통과. (12개 항목 추가)')
