# -*- coding: utf-8 -*-
"""
이적 임팩트 분석기 (V5 Part 2.5 'AI 가치 및 통합 지표')

  · Transfer Impact Score : (이적 후 지표 - 이적 전 지표) / 이적 전 지표 × 100
  · Consistency Rating    : 경기별 지표 변동계수(CV) 기반 일관성 (100=완전 일관)
  · Squad Contribution %  : 팀 전체 득점 관여(골+어시스트) 내 선수 비중
  · Big Match Performance : ClubElo 상위권 팀 상대 경기 가중 (Elo 데이터 있을 때)

미구현(데이터 부재로 정직하게 제외): Market Value Change(Transfermarkt는
스크래핑 위험군), Injury Risk Score/Age Curve(의료·연령 이력 데이터 필요),
Tactical Role Fit(전술 라벨링 필요).
"""
import glob
import json
import math
import os

CORE_METRICS = ('xG', 'xA', 'xT', 'VAEP', 'SCA', 'key_passes',
                'progressive_passes', 'tackles', 'interceptions')


def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def transfer_impact_score(pre, post, metrics=CORE_METRICS):
    """V5 수식: 지표별 (post-pre)/pre 변화율 + 종합 점수."""
    detail, changes = {}, []
    for m in metrics:
        p0, p1 = pre.get(m, 0), post.get(m, 0)
        change = round((p1 - p0) / p0 * 100, 1) if p0 else None
        detail[m] = {'pre': p0, 'post': p1, 'change_pct': change}
        if change is not None:
            changes.append(change)
    return {
        'metrics': detail,
        'overall_change_pct': round(sum(changes) / len(changes), 1)
            if changes else None,
    }


def consistency_rating(match_values):
    """경기별 지표값 리스트 → 변동계수 역산 점수 (0~100, 높을수록 일관)."""
    vals = [v for v in match_values if isinstance(v, (int, float))]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    if mean == 0:
        return None
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    cv = std / abs(mean)
    return round(max(0.0, min(100.0, (1 - cv) * 100)), 1)


def squad_contribution(metrics_dir='data/metrics'):
    """선수별 (골+어시스트) / 소속 경기 팀 총 득점관여 비중."""
    player_ga, team_ga = {}, 0
    for path in glob.glob(os.path.join(metrics_dir, '*_metrics.json')):
        data = _load(path, {})
        for p, s in data.get('players', {}).items():
            ga = s.get('goals', 0) + s.get('assists', 0)
            player_ga[p] = player_ga.get(p, 0) + ga
            team_ga += ga
    if not team_ga:
        return {}
    return {p: round(ga / team_ga * 100, 1)
            for p, ga in sorted(player_ga.items(), key=lambda kv: -kv[1]) if ga}


def player_consistency(metrics_dir='data/metrics', metric='VAEP'):
    """경기 지표 파일들을 훑어 선수별 Consistency Rating 산출."""
    series = {}
    for path in glob.glob(os.path.join(metrics_dir, '*_metrics.json')):
        data = _load(path, {})
        for p, s in data.get('players', {}).items():
            series.setdefault(p, []).append(s.get(metric, 0))
    return {p: r for p, vals in series.items()
            if (r := consistency_rating(vals)) is not None}


def big_match_performance(metrics_dir='data/metrics',
                          elo_path='data/master/club_elo.json', top_n=10,
                          metric='VAEP'):
    """ClubElo 상위 top_n 팀 상대 경기 성과 / 그 외 경기 성과 비율 (V5 2.5-8).
    >1.0 = 빅매치에 강함. 선수 소속팀은 지표 파일의 _team 태그로 판별."""
    from normalizer import normalize_team
    elo = _load(elo_path, {})
    top = {normalize_team(r['club'])
           for r in sorted(elo.get('rankings', []),
                           key=lambda r: -r.get('elo', 0))[:top_n]}
    if not top:
        return {}
    big, other = {}, {}
    for path in glob.glob(os.path.join(metrics_dir, '*_metrics.json')):
        data = _load(path, {})
        teams = list(data.get('teams', {}).keys())
        if len(teams) != 2:
            continue
        norm = {t: normalize_team(t) for t in teams}
        for p, s in data.get('players', {}).items():
            my = s.get('_team')
            if not my:
                continue
            opp = teams[1] if my == teams[0] else teams[0]
            bucket = big if norm.get(opp) in top else other
            bucket.setdefault(p, []).append(s.get(metric, 0))
    out = {}
    for p in set(big) & set(other):
        b = sum(big[p]) / len(big[p])
        o = sum(other[p]) / len(other[p])
        if o:
            out[p] = round(b / o, 2)
    return out


def analyze_transfers(transfers_file='data/master/transfer_targets.json',
                      metrics_dir='data/metrics',
                      out_path='data/metrics/transfer_impact.json'):
    """감지된 이적 선수에 대해 가용 데이터 기반 임팩트 리포트 생성.
    (이적 전 데이터가 축적되어 있을 때 pre/post 비교, 아니면 현황만)"""
    transfers = _load(transfers_file, [])
    contrib = squad_contribution(metrics_dir)
    consist = player_consistency(metrics_dir)
    bmp = big_match_performance(metrics_dir)
    report = {}
    for t in transfers:
        name = t.get('player_name')
        report[name] = {
            'move': f"{t.get('from_team')} → {t.get('to_team')}",
            'detected_at': t.get('detected_at'),
            'squad_contribution_pct': contrib.get(name),
            'consistency_rating': consist.get(name),
            'big_match_performance': bmp.get(name),
        }
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=4, ensure_ascii=False)
    print(f'[impact] 이적 임팩트 {len(report)}건 분석 → {out_path}')
    return report


if __name__ == '__main__':
    analyze_transfers()
