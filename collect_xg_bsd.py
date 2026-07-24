# -*- coding: utf-8 -*-
import json
import os
import requests
import time

# BSD API 설정
# 다른 스크립트들과 동일하게 GitHub Secrets(BSD_API_KEY)에서 읽는다.
API_TOKEN = os.getenv('BSD_API_KEY', '')
BASE_URL = 'https://sports.bzzoiro.com/api'
OUT_PATH = 'data/master/xg_multileague.json'

# 리그 ID 매핑 (BSD 기준)
LEAGUES = {
    'laliga': 3,
    'bundesliga': 5,
    'seriea': 4,
    'ligue1': 6,
    'eredivisie': 10,
    'championship': 12,
    # 2026-07-24 추가: rehearse_mls_norway_xg_probe.py 실측 확정 —
    # MLS는 종료경기 200건 중 200건(100%) xG 있음, 팀명매칭 30/30 확인됨.
    # ELO(ClubElo)가 미국을 아예 커버 안 해서 MLS는 xG로 대체.
    # 엘리테세리엔은 여기 안 넣음 — ELO가 이미 16/16 완전 커버돼서 불필요하고,
    # 같은 프로브에서 TARGET_YEARS(2024/2025) 시즌엔 이벤트 자체가 0건이라
    # (season_id 매핑 이슈로 보임, 원인 미확정) 지금 넣어봐야 빈 결과만 남음.
    'mls': 18,
}

# 대상 시즌 연도
TARGET_YEARS = [2024, 2025]

def get_headers():
    return {'Authorization': f'Token {API_TOKEN}'}

def get_season_ids(league_id):
    url = f'{BASE_URL}/v2/leagues/{league_id}/seasons/'
    try:
        r = requests.get(url, headers=get_headers(), timeout=30)
        if r.status_code == 200:
            seasons = r.json().get('seasons', [])
            # 2024, 2025 연도가 포함된 시즌 ID 추출
            return [s['id'] for s in seasons if s['year'] in TARGET_YEARS]
    except Exception as e:
        print(f"Error fetching seasons for league {league_id}: {e}")
    return []

_to_kr_league = None
try:
    from app_export_multileague import to_kr_league as _to_kr_league
    print('[collect_xg_bsd] to_kr_league 임포트 성공 (실제 파이프라인 매핑 사용)')
except ImportError as e:
    print(f'[collect_xg_bsd] ⚠️ to_kr_league 임포트 실패: {e} '
          f'→ 이 스크립트는 app_export_multileague.py와 같은 폴더에서 '
          f'실행해야 한다')


def to_kr_name(team_name, league_key):
    """실제 파이프라인의 LEAGUE_TEAM_MAPS로만 매칭한다. 임포트가 안 됐거나
    매칭이 안 되면 None을 반환해 해당 팀은 결과에서 제외한다 — 손으로
    적은 임시 매핑을 쓰면 팀명이 부정확하게 섞일 위험이 있어 쓰지 않는다.
    (참고: 24-25/25-26 시즌엔 있었지만 26-27 시즌엔 없는 팀 — 예: 강등된
    팀 — 은 LEAGUE_TEAM_MAPS에 없어서 자동으로 걸러지는 게 정상이다.)"""
    if not _to_kr_league:
        return None
    hit = _to_kr_league(team_name)
    if hit and hit[0] == league_key:
        return hit[1]
    return None

def main():
    final_result = {}
    all_unmatched = set()
    for lkey, lid in LEAGUES.items():
        print(f"Collecting xG for {lkey} (BSD ID: {lid})...")
        team_stats = {}
        sids = get_season_ids(lid)
        
        for sid in sids:
            # BSD /api/events/ (버전 프리픽스 없음) — 목록 응답에 xG 필드가
            # home_xg_live/away_xg_live 이름으로 포함돼 있음을 실측 확인함
            # (2026-07-15, Manus 조사). 기존에 v2로 확인했던 stats.home.xg.actual
            # 필드(완료 경기엔 None)와는 다른 필드라 별개로 취급한다.
            url = f'{BASE_URL}/events/?league={lid}&season={sid}&status=finished&limit=400'
            try:
                r = requests.get(url, headers=get_headers(), timeout=30)
                if r.status_code != 200:
                    print(f"[collect_xg_bsd] HTTP {r.status_code}: {lkey} season={sid}")
                    continue
                
                events = r.json().get('results', [])
                for ev in events:
                    h_xg = ev.get('home_xg_live')
                    a_xg = ev.get('away_xg_live')
                    
                    # xG 데이터가 있는 경기만 수집
                    if h_xg is not None and a_xg is not None:
                        h_name = to_kr_name(ev['home_team'], lkey)
                        a_name = to_kr_name(ev['away_team'], lkey)
                        if h_name is None:
                            all_unmatched.add(f'{lkey}:{ev["home_team"]}')
                        if a_name is None:
                            all_unmatched.add(f'{lkey}:{ev["away_team"]}')
                        # 매칭 안 된 팀(None)은 건너뛴다 — 안 그러면 서로 다른
                        # 팀들의 스탯이 team_stats[None] 하나로 뒤섞여버린다.
                        if h_name is None or a_name is None:
                            continue
                        
                        # 홈 팀 스탯 누적
                        if h_name not in team_stats: team_stats[h_name] = {'xg': 0, 'xga': 0, 'mp': 0}
                        team_stats[h_name]['xg'] += h_xg
                        team_stats[h_name]['xga'] += a_xg
                        team_stats[h_name]['mp'] += 1
                        
                        # 원정 팀 스탯 누적
                        if a_name not in team_stats: team_stats[a_name] = {'xg': 0, 'xga': 0, 'mp': 0}
                        team_stats[a_name]['xg'] += a_xg
                        team_stats[a_name]['xga'] += h_xg
                        team_stats[a_name]['mp'] += 1
            except Exception as e:
                print(f"Error in {lkey} season {sid}: {e}")
                continue
        
        # 팀별 평균 xG, xGA 계산
        league_res = {}
        for team, stats in team_stats.items():
            if stats['mp'] > 0:
                league_res[team] = {
                    'xG': round(stats['xg'] / stats['mp'], 2),
                    'xGA': round(stats['xga'] / stats['mp'], 2)
                }
        final_result[lkey] = league_res
        print(f"Done {lkey}: {len(league_res)} teams processed.")

    # 결과 저장 (JSON 형식)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(final_result, f, ensure_ascii=False, indent=2)
    print(f"Successfully saved to {OUT_PATH}")
    if all_unmatched:
        sample = sorted(all_unmatched)[:20]
        print(f'[collect_xg_bsd] ⚠️ 한글 매칭 안 된 팀명 {len(all_unmatched)}개 '
              f'(24-25/25-26엔 있었지만 26-27 로스터엔 없는 강등팀일 가능성 높음, '
              f'샘플): {sample}')

if __name__ == '__main__':
    main()
