# -*- coding: utf-8 -*-
"""
xG 모델 학습기 — StatsBomb Open Data 기반 (문서 결론: 과거 데이터로 모델 학습)

흐름:
  1. StatsBomb Open Data에서 대회/경기 목록 조회 (키 불필요, GitHub raw)
  2. 경기 이벤트에서 슛 표본 추출 (위치, 상황, 골 여부)
  3. impact_engine.XGModel.fit()으로 로지스틱 회귀 학습
  4. 계수를 data/models/xg_coefficients.json에 저장
     → XGModel.load()가 자동으로 읽어 전 지표에 반영

검증: 학습 후 근거리/원거리 xG 단조성 + 실제 골 비율 대비 보정(calibration) 출력.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from api_clients import StatsBombOpenClient
from impact_engine import XGModel

MODEL_PATH = 'data/models/xg_coefficients.json'
MAX_MATCHES = int(os.getenv('XG_TRAIN_MATCHES', '40'))  # 쿼터/시간 고려 기본 40경기


def collect_shots(max_matches=MAX_MATCHES):
    sb = StatsBombOpenClient()
    comps, ok = sb.competitions()
    if not ok or not comps:
        print('[xg-train] competitions 조회 실패')
        return []
    shots, used = [], 0
    for comp in comps:
        if used >= max_matches:
            break
        matches, ok = sb.matches(comp['competition_id'], comp['season_id'])
        if not ok or not matches:
            continue
        for m in matches:
            if used >= max_matches:
                break
            events, ok = sb.events(m['match_id'])
            if not ok or not events:
                continue
            for ev in events:
                if (ev.get('type') or {}).get('name') != 'Shot':
                    continue
                loc = ev.get('location') or [None, None]
                if loc[0] is None:
                    continue
                s = ev.get('shot', {})
                stype = (s.get('type') or {}).get('name')
                if stype == 'Penalty':
                    continue  # 페널티는 고정값 사용
                shots.append({
                    'x': loc[0], 'y': loc[1],
                    'set_piece': stype in ('Free Kick', 'Corner'),
                    'goal': 1 if (s.get('outcome') or {}).get('name') == 'Goal' else 0,
                })
            used += 1
        print(f'[xg-train] {comp.get("competition_name")}: 누적 {used}경기 / '
              f'슛 {len(shots)}개')
    return shots


def train(shots, epochs=400, lr=0.05):
    model = XGModel().fit(shots, epochs=epochs, lr=lr)
    # 보정 검증
    if shots:
        avg_pred = sum(model.predict(s['x'], s['y'], s['set_piece'])
                       for s in shots) / len(shots)
        goal_rate = sum(s['goal'] for s in shots) / len(shots)
        print(f'[xg-train] 표본 {len(shots)}개 | 실제 골 비율 {goal_rate:.3f} '
              f'| 평균 예측 xG {avg_pred:.3f}')
    close = model.predict(114, 40)
    far = model.predict(85, 40)
    assert close > far, '학습 후 단조성 붕괴 — 저장 중단'
    return model


def save(model, path=MODEL_PATH):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'w0': model.w0, 'w_dist': model.w_dist,
                   'w_angle': model.w_angle, 'w_setpiece': model.w_setpiece,
                   'trained': True}, f, indent=4)
    print(f'[xg-train] 계수 저장 → {path}')


def main():
    shots = collect_shots()
    if len(shots) < 200:
        print(f'[xg-train] 표본 부족({len(shots)}) → 기존 계수 유지')
        return
    save(train(shots))


if __name__ == '__main__':
    main()
