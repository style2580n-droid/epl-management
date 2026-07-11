# -*- coding: utf-8 -*-
"""
시각화 엔진 — 구단급 시각 자료 3종을 외부 라이브러리 없이 SVG로 생성
  · Shot Map        : 슛 위치를 xG 크기·결과 색상으로 표시
  · Passing Network : 선수 평균 위치 노드 + 패스 횟수 가중 엣지
  · Heatmap         : 팀 이벤트 밀도 격자

SVG는 GitHub/브라우저에서 바로 렌더링되고 저장소에 커밋 가능 (matplotlib 불필요).
좌표계: StatsBomb 120x80 → SVG 840x560 (7배 스케일).
"""
import json
import math
import os
from collections import defaultdict

SCALE = 7
W, H = 120 * SCALE, 80 * SCALE
GREEN, LINE = '#2e7d46', '#e8f5e9'


def _pitch(extra=''):
    """축구장 배경 SVG 요소들."""
    s = SCALE
    return f'''<rect width="{W}" height="{H}" fill="{GREEN}"/>
<g stroke="{LINE}" stroke-width="2" fill="none">
  <rect x="0" y="0" width="{W}" height="{H}"/>
  <line x1="{W/2}" y1="0" x2="{W/2}" y2="{H}"/>
  <circle cx="{W/2}" cy="{H/2}" r="{9.15*s}"/>
  <rect x="0" y="{18*s}" width="{18*s}" height="{44*s}"/>
  <rect x="{W-18*s}" y="{18*s}" width="{18*s}" height="{44*s}"/>
  <rect x="0" y="{30*s}" width="{6*s}" height="{20*s}"/>
  <rect x="{W-6*s}" y="{30*s}" width="{6*s}" height="{20*s}"/>
</g>{extra}'''


def _svg(body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H+40}" '
            f'viewBox="0 0 {W} {H+40}">'
            f'<text x="{W/2}" y="24" text-anchor="middle" font-size="20" '
            f'font-family="sans-serif" fill="#222">{title}</text>'
            f'<g transform="translate(0,40)">{body}</g></svg>')


def _pt(x, y):
    return x * SCALE, y * SCALE


# ================================================================ Shot Map
def shot_map(events, team, xg_model=None, title=None):
    if xg_model is None:
        from impact_engine import XGModel
        xg_model = XGModel()
    shots = []
    for e in events:
        if e.get('type') == 'Shot' and e.get('team') == team \
                and e.get('x') is not None:
            xg = e.get('xg') or xg_model.predict(
                e['x'], e['y'], e.get('situation') == 'Set Piece',
                e.get('situation') == 'Penalty')
            shots.append((e['x'], e['y'], xg, e.get('outcome'),
                          e.get('player', '')))
    circles = []
    for x, y, xg, outcome, player in shots:
        cx, cy = _pt(x, y)
        r = 6 + xg * 30
        color = '#ff5252' if outcome == 'Goal' else \
                '#ffd54f' if outcome == 'Saved' else '#90a4ae'
        circles.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="{color}" '
            f'fill-opacity="0.85" stroke="#fff" stroke-width="1.5">'
            f'<title>{player} xG={xg:.2f} {outcome or ""}</title></circle>')
    legend = (f'<g font-family="sans-serif" font-size="14" fill="#fff">'
              f'<circle cx="20" cy="{H-20}" r="7" fill="#ff5252"/>'
              f'<text x="32" y="{H-15}">Goal</text>'
              f'<circle cx="100" cy="{H-20}" r="7" fill="#ffd54f"/>'
              f'<text x="112" y="{H-15}">Saved</text>'
              f'<circle cx="190" cy="{H-20}" r="7" fill="#90a4ae"/>'
              f'<text x="202" y="{H-15}">Other · size=xG</text></g>')
    return _svg(_pitch(''.join(circles) + legend),
                title or f'{team} Shot Map ({len(shots)} shots)')


# ========================================================= Passing Network
def passing_network(events, team, min_passes=2, title=None):
    pos = defaultdict(list)          # 선수 평균 위치
    links = defaultdict(int)         # (from, to) 성공 패스 수
    prev = None
    for e in events:
        if e.get('team') != team:
            prev = None
            continue
        pl = e.get('player')
        if e.get('x') is not None and pl:
            pos[pl].append((e['x'], e['y']))
        if e.get('type') == 'Pass':
            if e.get('outcome') not in ('Incomplete', 'Out'):
                receiver = e.get('receiver')
                if receiver:
                    links[(pl, receiver)] += 1
                    if e.get('end_x') is not None:
                        pos[receiver].append((e['end_x'], e['end_y']))
                elif prev and prev != pl:
                    links[(prev, pl)] += 1
            prev = pl
        elif e.get('type') in ('Carry', 'Shot'):
            prev = pl
    avg = {p: (sum(x for x, _ in pts) / len(pts),
               sum(y for _, y in pts) / len(pts))
           for p, pts in pos.items() if pts}
    edges, nodes = [], []
    for (a, b), n in links.items():
        if n < min_passes or a not in avg or b not in avg:
            continue
        (x1, y1), (x2, y2) = _pt(*avg[a]), _pt(*avg[b])
        edges.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" '
                     f'y2="{y2:.0f}" stroke="#fff" stroke-opacity="0.55" '
                     f'stroke-width="{min(1 + n, 10)}"/>')
    counts = defaultdict(int)
    for (a, b), n in links.items():
        counts[a] += n
        counts[b] += n
    for p, (x, y) in avg.items():
        cx, cy = _pt(x, y)
        r = 10 + min(counts.get(p, 0), 30) * 0.6
        last = p.split()[-1][:10]
        nodes.append(
            f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="#1565c0" '
            f'stroke="#fff" stroke-width="2"><title>{p}</title></circle>'
            f'<text x="{cx:.0f}" y="{cy - r - 4:.0f}" text-anchor="middle" '
            f'font-size="13" font-family="sans-serif" fill="#fff">{last}</text>')
    return _svg(_pitch(''.join(edges) + ''.join(nodes)),
                title or f'{team} Passing Network')


# ================================================================ Heatmap
def heatmap(events, team, cols=24, rows=16, title=None):
    grid = [[0] * cols for _ in range(rows)]
    for e in events:
        if e.get('team') == team and e.get('x') is not None:
            c = min(int(e['x'] / 120 * cols), cols - 1)
            r = min(int(e['y'] / 80 * rows), rows - 1)
            grid[r][c] += 1
    peak = max((v for row in grid for v in row), default=1) or 1
    cells = []
    cw, ch = W / cols, H / rows
    for r in range(rows):
        for c in range(cols):
            if grid[r][c] == 0:
                continue
            op = 0.15 + 0.75 * (grid[r][c] / peak)
            cells.append(f'<rect x="{c*cw:.0f}" y="{r*ch:.0f}" width="{cw:.0f}" '
                         f'height="{ch:.0f}" fill="#ff7043" '
                         f'fill-opacity="{op:.2f}"/>')
    return _svg(_pitch(''.join(cells)),
                title or f'{team} Activity Heatmap')


# =================================================================== main
def render_match(events_path, out_dir='reports/viz'):
    with open(events_path, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    events, home, away = payload['events'], payload['home'], payload['away']
    name = os.path.splitext(os.path.basename(events_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    outputs = []
    for team in (home, away):
        for kind, fn in (('shotmap', shot_map),
                         ('network', passing_network),
                         ('heatmap', heatmap)):
            svg = fn(events, team)
            path = os.path.join(out_dir, f'{name}_{team}_{kind}.svg')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(svg)
            outputs.append(path)
    print(f'[viz] {name}: SVG {len(outputs)}장 생성 → {out_dir}/')
    return outputs


if __name__ == '__main__':
    import glob
    import sys
    paths = sys.argv[1:] or glob.glob('data/events/*.json')
    for p in paths:
        try:
            render_match(p)
        except Exception as ex:
            print(f'[viz] {p} 실패: {ex}')
