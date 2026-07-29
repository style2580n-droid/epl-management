# -*- coding: utf-8 -*-
"""
ML 앙상블 학습 (2026-07-16 착수 — 인수인계 문서 "다음 세션 최우선 후보" #1).

⚠️ 왜 새로 만드나: EPL 앱(index.html)의 randomForestWinProb는 EPL 데이터로
학습된 모델이라 다른 리그엔 못 쓴다고 multi_league_index.html 주석에
명시돼 있었다("EPL 데이터로 학습된 모델, 다른 리그에 안 맞음"). 이 스크립트는
그 한계를 없애려고 리그를 구분하지 않는 정규화된 피처(Elo 차이, xG 차이)만
써서 EPL+6개 리그 데이터를 한 모델로 같이 학습한다 — 리그마다 특성이 달라도
"Elo 차이가 클수록/xG가 앞설수록 이긴다"는 관계 자체는 리그 불문하고
성립하므로, 리그명을 아예 피처에서 뺀 게 의도적인 설계다.

입력: data/football.db(matches, status=FINISHED), data/master/club_elo.json,
      data/master/xg_multileague.json(6개 리그), reports/app_data.js가 쓰는
      것과 같은 EPL 시즌 지표(있으면 xG 피처에 추가 활용 — 없어도 무방).
출력: data/master/ml_ensemble.json — {classes, coef, intercept, features,
      trained_at, n_samples, holdout_accuracy, holdout_logloss}
      학습 표본이 너무 적으면(MIN_SAMPLES 미만) 아예 파일을 쓰지 않는다 —
      부실한 모델을 배포하는 것보다 "아직 없음"이 안전하다(다른 파이프라인
      단계들과 동일한 원칙: 확신 없으면 조용히 스킵하고 로그만 남긴다).

⚠️ 알려진 한계 (다음 세션에서 개선 후보):
  - Elo는 "지금 시점" 스냅샷 하나만 써서 과거 경기에 역산 적용한다(진짜
    경기 시점 Elo가 아님) — 시즌 진행되며 Elo 히스토리를 따로 쌓기 전까지는
    구조적 오차가 있다. 그래도 방향성(Elo가 높았던 팀이 대체로 더 잘한다)은
    유효해서 완전히 무의미하진 않다.
  - xG는 24-25/25-26 시즌 평균이라 특정 경기 시점과 안 맞을 수 있다.
  - 지금(2026-07-16)은 26-27 시즌 개막 전이라 FINISHED 경기가 거의 없을
    가능성이 높다 — 그 경우 25-26 시즌 데이터가 DB에 남아있어야 학습이
    된다. 하나도 없으면 MIN_SAMPLES 게이트에 걸려 조용히 스킵한다.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from sklearn.model_selection import train_test_split

DB_PATH = 'data/football.db'
ELO_PATH = 'data/master/club_elo.json'
XG_MULTI_PATH = 'data/master/xg_multileague.json'
OUT_PATH = 'data/master/ml_ensemble.json'
MIN_SAMPLES = 150  # 이보다 적으면 과적합 위험이 커서 모델을 배포하지 않는다
HOME_ADV_ELO = 65  # multi_league_index.html의 HOME_ADV와 동일 값(일관성 유지)


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _to_kr_any(name):
    """EPL은 app_export.to_kr, 6개 리그는 app_export_multileague.to_kr_league
    — collect_transfers_bsd.py의 _team_kr와 동일한 순차 시도 패턴.
    반환값: 매칭된 한글 팀명(리그 무관하게 전부 같은 이름 공간으로 취급 —
    Elo/xG 딕셔너리도 리그 구분 없이 한글 팀명으로만 키가 잡혀 있어서
    이렇게 해도 다른 리그 동명 팀과 충돌하지 않는다, app_export_multileague
    의 별칭 충돌 검사에서 이미 0건 확인됨)."""
    try:
        from app_export import to_kr as epl_to_kr
    except ImportError:
        epl_to_kr = lambda n: None
    try:
        from app_export_multileague import to_kr_league
    except ImportError:
        to_kr_league = lambda n: None

    kr = epl_to_kr(name)
    if kr:
        return kr
    hit = to_kr_league(name)
    if hit:
        return hit[1]
    return None


def _build_elo_lookup():
    rankings = _load_json(ELO_PATH, {}).get('rankings', [])
    out = {}
    for r in rankings:
        kr = _to_kr_any(r.get('club'))
        if kr and r.get('elo'):
            out[kr] = r['elo']
    return out


def _build_xg_lookup():
    """6개 리그 xG만 있음(EPL은 다른 파일/구조라 이번 1차 버전에선 생략 —
    xG 피처는 있으면 쓰고 없으면 0으로 두는 has_xg 플래그 방식이라
    EPL 경기가 섞여도 학습이 깨지지 않는다)."""
    raw = _load_json(XG_MULTI_PATH, {})
    out = {}
    for _lk, teams in raw.items():
        for team, stats in teams.items():
            xg, xga = stats.get('xG'), stats.get('xGA')
            if xg is not None and xga is not None:
                out[team] = (xg, xga)
    return out


def _load_finished_matches():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT home, away, home_goals, away_goals, date FROM matches "
        "WHERE status='FINISHED' AND home_goals IS NOT NULL "
        "AND away_goals IS NOT NULL ORDER BY date"
    ).fetchall()
    conn.close()
    return rows


def build_dataset():
    elo = _build_elo_lookup()
    xg = _build_xg_lookup()
    rows = _load_finished_matches()

    X, y = [], []
    n_with_xg = 0
    n_skipped_no_team = 0
    for r in rows:
        home_kr, away_kr = _to_kr_any(r['home']), _to_kr_any(r['away'])
        if not (home_kr and away_kr):
            n_skipped_no_team += 1
            continue
        home_elo = elo.get(home_kr, 1500) + HOME_ADV_ELO
        away_elo = elo.get(away_kr, 1500)
        elo_diff = home_elo - away_elo

        xg_diff, has_xg = 0.0, 0.0
        if home_kr in xg and away_kr in xg:
            h_xg, h_xga = xg[home_kr]
            a_xg, a_xga = xg[away_kr]
            xg_diff = (h_xg - a_xga) - (a_xg - h_xga)
            has_xg = 1.0
            n_with_xg += 1

        hg, ag = r['home_goals'], r['away_goals']
        result = 'H' if hg > ag else ('A' if hg < ag else 'D')
        X.append([elo_diff, xg_diff, has_xg])
        y.append(result)

    print(f'[train_ml_ensemble] 학습 표본 {len(X)}건 구성 '
          f'(xG 있는 경기 {n_with_xg}건, 팀명 매칭 실패로 제외 {n_skipped_no_team}건)',
          flush=True)
    return np.array(X, dtype=float), np.array(y)


def train_and_export():
    X, y = build_dataset()
    if len(X) < MIN_SAMPLES:
        print(f'[train_ml_ensemble] 표본 {len(X)}건 < 최소 {MIN_SAMPLES}건 → '
              f'과적합 위험이 커서 학습 스킵 (26-27 시즌 경기가 더 쌓이면 '
              f'재실행). 기존 {OUT_PATH}는 건드리지 않음.', flush=True)
        return

    # 2026-07-29 추가: 표본이 MIN_SAMPLES를 넘겨도 결과가 전부 한 가지
    # (예: 전부 무승부)뿐이면 LogisticRegression.fit()이 그 자리에서
    # ValueError로 죽는다("needs samples of at least 2 classes") — 실전에서
    # 실제로 터진 걸 확인함. 학습 자체가 무의미한 상황이니(분류할 클래스가
    # 하나뿐이면 아무것도 배울 게 없음) 에러 대신 안전하게 스킵한다.
    classes_present = sorted(set(y.tolist()))
    if len(classes_present) < 2:
        print(f'[train_ml_ensemble] ⚠️ 표본 {len(X)}건이지만 결과 클래스가 '
              f'{classes_present} 하나뿐 → 학습 스킵(분류 자체가 불가능). '
              f'기존 {OUT_PATH}는 건드리지 않음.', flush=True)
        return

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y)

    model = LogisticRegression(max_iter=2000)
    model.fit(X_train, y_train)

    pred = model.predict(X_test)
    proba = model.predict_proba(X_test)
    acc = accuracy_score(y_test, pred)
    ll = log_loss(y_test, proba, labels=model.classes_.tolist())

    # 베이스라인(항상 다수 클래스 예측) 대비 개선 여부도 같이 로그로 남긴다 —
    # ML이 "아무것도 안 배운 것"보다 못하면 배포 의미가 없기 때문.
    majority = max(set(y_train.tolist()), key=list(y_train).count)
    baseline_acc = float(np.mean(y_test == majority))
    print(f'[train_ml_ensemble] 홀드아웃 정확도 {acc:.3f} (베이스라인 '
          f'{baseline_acc:.3f}), logloss {ll:.3f}, 클래스 순서 '
          f'{model.classes_.tolist()}', flush=True)

    if acc < baseline_acc:
        print('[train_ml_ensemble] ⚠️ 다수클래스 베이스라인보다도 낮은 정확도 → '
              '배포하지 않고 스킵 (표본이 더 쌓이면 재시도)', flush=True)
        return

    out = {
        'features': ['eloDiff', 'xgDiff', 'hasXg'],
        'classes': model.classes_.tolist(),  # 보통 ['A','D','H'] 알파벳순
        'coef': model.coef_.tolist(),         # shape [n_classes, 3]
        'intercept': model.intercept_.tolist(),
        'trained_at': datetime.now(timezone.utc).isoformat(),
        'n_samples': int(len(X)),
        'holdout_accuracy': round(float(acc), 4),
        'holdout_baseline_accuracy': round(baseline_acc, 4),
        'holdout_logloss': round(float(ll), 4),
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f'[train_ml_ensemble] {OUT_PATH} 저장 완료', flush=True)


if __name__ == '__main__':
    train_and_export()
