# -*- coding: utf-8 -*-
"""
리포트 생성기 v2 — 매일 자정 자동 분석 리포트 (새 틀 5장 Step 3)
구성: ① 리그 순위 요약 ② 이적 감지 ③ 경기 고급 지표 ④ 선수 랭킹(SCA/xT/VAEP)
"""
import glob
import json
import os
from datetime import datetime, timezone

LEAGUE_NAMES = {'PL': '프리미어리그', 'PD': '라리가', 'BL1': '분데스리가',
                'SA': '세리에 A', 'FL1': '리그 1'}


def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def build_report():
    now = datetime.now(timezone.utc)
    L = [f'# ⚽ 유럽 축구 데이터 센터 — 데일리 리포트 {now.strftime("%Y-%m-%d")}',
         '', f'생성(UTC): {now.strftime("%Y-%m-%d %H:%M")}', '']

    # ------------------------------------------------ ① 리그 순위 (TOP 4)
    leagues = _load('data/master/leagues.json', {})
    if leagues:
        L += ['## 🏆 리그 순위 (상위 4팀)', '']
        for code, info in leagues.items():
            name = LEAGUE_NAMES.get(code, code)
            top = info.get('standings', [])[:4]
            if not top:
                continue
            row = ' · '.join(f"{r['position']}.{r['team']}({r['points']}pt)"
                             for r in top)
            L.append(f'- **{name}**: {row}')
        L.append('')

    # ------------------------------------------------ ② 이적 감지
    transfers = _load('data/master/transfer_targets.json', [])
    today = now.date().isoformat()
    todays = [t for t in transfers if t.get('detected_at', '').startswith(today)]
    L += ['## 🔁 이적 감지', '']
    if todays:
        L += ['| 선수 | 이전 팀 | 새 팀 | 리그 |', '|---|---|---|---|']
        for t in todays:
            L.append(f"| {t['player_name']} | {t['from_team']} | {t['to_team']} "
                     f"| {LEAGUE_NAMES.get(t.get('league'), t.get('league', '-'))} |")
        L += ['', f'오늘 {len(todays)}건 (누적 {len(transfers)}건)', '']
    else:
        L += [f'오늘 신규 이적 없음 (누적 {len(transfers)}건)', '']

    # ------------------------------------------------ ③ 경기 지표
    metric_files = sorted(glob.glob('data/metrics/*_metrics.json'))
    L += ['## 📊 경기 고급 지표', '']
    all_players = {}
    if metric_files:
        for path in metric_files:
            data = _load(path, {})
            teams = data.get('teams', {})
            if not teams:
                continue
            L += [f'### {os.path.basename(path).replace("_metrics.json", "")}', '',
                  '| 팀 | 득점 | xG | npxG | xA | xT | VAEP | PPDA | Field Tilt | 점유율 |',
                  '|---|---|---|---|---|---|---|---|---|---|']
            for team, m in teams.items():
                L.append(
                    f"| {team} | {int(m.get('goals', 0))} | {m.get('xG', 0)} "
                    f"| {m.get('npxG', 0)} | {m.get('xA', 0)} | {m.get('xT', 0)} "
                    f"| {m.get('VAEP', 0)} "
                    f"| {m.get('PPDA') if m.get('PPDA') is not None else '-'} "
                    f"| {str(m.get('field_tilt_pct')) + '%' if m.get('field_tilt_pct') is not None else '-'} "
                    f"| {str(m.get('possession_pct')) + '%' if m.get('possession_pct') is not None else '-'} |")
            L.append('')
            for p, s in data.get('players', {}).items():
                agg = all_players.setdefault(p, {})
                for k, v in s.items():
                    if isinstance(v, (int, float)):
                        agg[k] = agg.get(k, 0) + v
    else:
        L += ['집계된 경기 지표 없음', '']

    # ------------------------------------------------ ④ 선수 랭킹
    if all_players:
        L += ['## 🌟 선수 랭킹', '']
        for title, key in [('찬스 메이킹 (SCA)', 'SCA'),
                           ('위협 창출 (xT)', 'xT'),
                           ('종합 기여 (VAEP)', 'VAEP')]:
            ranked = sorted(((p, s.get(key, 0)) for p, s in all_players.items()),
                            key=lambda kv: kv[1], reverse=True)[:5]
            ranked = [(p, v) for p, v in ranked if v > 0]
            if ranked:
                L.append(f'**{title} TOP 5**')
                L.append('')
                for p, v in ranked:
                    L.append(f'- {p}: {round(v, 3)}')
                L.append('')

    # ------------------------------------------------ 시각 자료 (⑥ 업그레이드)
    viz_files = sorted(glob.glob('reports/viz/*.svg'))
    if viz_files:
        L += ['## 🎨 경기 시각 자료', '']
        by_match = {}
        for v in viz_files:
            base = os.path.basename(v)
            match = base.rsplit('_', 2)[0]
            by_match.setdefault(match, []).append(base)
        for match, files in by_match.items():
            links = ' · '.join(
                f'[{f.rsplit("_",1)[1].replace(".svg","")}'
                f'({f.rsplit("_",2)[1]})](viz/{f})' for f in files)
            L.append(f'- **{match}**: {links}')
        L.append('')

    # ------------------------------------------------ ⑤ 선수 능력치 프로파일
    profiles = _load('data/metrics/player_profiles.json', {})
    if profiles:
        L += ['## 🧬 선수 능력치 프로파일 (V2 고급화 매핑)', '',
              '| 선수 | 슈팅정밀도 | 패스창의성 | 수비기여도 | 피지컬 | 심리안정성 |',
              '|---|---|---|---|---|---|']
        def _avg(pr):
            scores = [pr[c].get('score') for c in
                      ('shooting_precision', 'pass_creativity',
                       'defensive_contribution', 'physical_activity',
                       'psychological_stability') if pr[c].get('score') is not None]
            return sum(scores) / len(scores) if scores else 0
        ranked = sorted(profiles.items(), key=lambda kv: _avg(kv[1]), reverse=True)
        for p, pr in ranked[:10]:
            row = [str(pr[c].get('score')) if pr[c].get('score') is not None else '-'
                   for c in ('shooting_precision', 'pass_creativity',
                             'defensive_contribution', 'physical_activity',
                             'psychological_stability')]
            L.append(f'| {p} | ' + ' | '.join(row) + ' |')
        L.append('')

    return '\n'.join(L)


def build_transfer_report(transfers, todays):
    """reports/transfers/ 전용 이적 상세 보고서 (Archive Part 5)."""
    now = datetime.now(timezone.utc)
    L = [f'# 🔁 이적 감지 상세 보고서 — {now.strftime("%Y-%m-%d")}', '']
    if not todays:
        L.append('오늘 감지된 신규 이적이 없습니다.')
    for t in todays:
        L += [f"## {t['player_name']}",
              f"- 이동: **{t['from_team']} → {t['to_team']}**",
              f"- 리그: {LEAGUE_NAMES.get(t.get('league'), t.get('league', '-'))}",
              f"- 감지 시각: {t.get('detected_at', '-')}", '']
    L.append(f'누적 감지 이적: {len(transfers)}건')
    return '\n'.join(L)


def _ai_summary(report_text):
    """OPENAI_API_KEY가 있으면 AI 총평 생성, 없으면 건너뜀 (Archive Part 5)."""
    key = os.getenv('OPENAI_API_KEY')
    if not key:
        return None
    try:
        import requests
        r = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {key}'},
            json={'model': 'gpt-4o-mini', 'max_tokens': 500, 'messages': [
                {'role': 'user',
                 'content': '다음 축구 데이터 리포트를 스카우트 관점에서 '
                            '5문장 이내로 총평해줘:\n\n' + report_text[:6000]}]},
            timeout=60)
        if r.status_code == 200:
            return r.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f'[ai] 요약 실패(건너뜀): {e}')
    return None


def main():
    os.makedirs('reports/transfers', exist_ok=True)
    report = build_report()

    summary = _ai_summary(report)
    if summary:
        report += '\n## 🤖 AI 총평\n\n' + summary + '\n'

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d')
    for path in (f'reports/daily_report_{stamp}.md', 'reports/latest.md'):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(report)

    transfers = _load('data/master/transfer_targets.json', [])
    today = datetime.now(timezone.utc).date().isoformat()
    todays = [t for t in transfers if t.get('detected_at', '').startswith(today)]
    with open(f'reports/transfers/transfer_report_{stamp}.md', 'w',
              encoding='utf-8') as f:
        f.write(build_transfer_report(transfers, todays))
    print(f'[report] daily + transfers 보고서 생성 완료')


if __name__ == '__main__':
    main()
