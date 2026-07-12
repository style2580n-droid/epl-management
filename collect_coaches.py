# -*- coding: utf-8 -*-
"""
API-Football의 전용 감독(coach) 엔드포인트로 EPL 20개 구단의
'현재' 감독을 직접 조회해 data/master/coaches.json에 저장한다.

기존 teams.coach(football-data.org)나 lineups.coach(경기 라인업 발표 후에만
존재)와 달리, 이 스크립트는 시즌 진행 여부와 무관하게 지금 바로 감독 이름을
가져올 수 있다. app_export.py가 이 파일을 최우선 감독 소스로 사용한다.

실행: API_FOOTBALL_KEY(1/2) 환경변수 필요. 없으면 조용히 스킵(예외 없음).
"""
import json
import os

from api_clients import APIFootballClient
from app_export import to_kr

OUT_PATH = 'data/master/coaches.json'
PL_LEAGUE_ID = 39          # API-Football 리그 ID: Premier League
SEASON = int(os.getenv('AF_SEASON', '2026'))  # 2026 = 2026-27 시즌


def _unwrap(resp):
    """APIFootballClient.get()이 (data, changed) 튜플이나 data 단독,
    또는 실패 시 None을 반환하는 모든 경우를 방어적으로 처리."""
    if resp is None:
        return None
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp


def _current_coach_name(coach_entries):
    """/coachs 응답의 career 배열에서 'end'가 없는(현재 재임 중) 항목을
    가진 인물을 우선 선택. 없으면 첫 번째 결과로 폴백."""
    for person in coach_entries or []:
        career = person.get('career') or []
        if any(c.get('end') is None for c in career):
            return person.get('name')
    if coach_entries:
        return coach_entries[0].get('name')
    return None


def main():
    client = APIFootballClient()
    if not client.enabled:
        print('[collect_coaches] API_FOOTBALL_KEY 미등록 → 스킵')
        return

    teams_data = _unwrap(client.teams(PL_LEAGUE_ID, SEASON))
    team_list = (teams_data or {}).get('response', [])
    if not team_list:
        print(f'[collect_coaches] {SEASON} 시즌 팀 목록을 못 가져옴 '
              f'(응답 비어있음 — 시즌 번호 확인 필요할 수 있음)')
        return

    coaches = {}
    for entry in team_list:
        team = entry.get('team', {})
        team_id, team_name = team.get('id'), team.get('name')
        kr = to_kr(team_name)
        if not team_id or not kr:
            continue
        coach_data = _unwrap(client.coach(team_id))
        entries = (coach_data or {}).get('response', [])
        name = _current_coach_name(entries)
        if name:
            coaches[kr] = name
            print(f'[collect_coaches] {kr}: {name}')
        else:
            print(f'[collect_coaches] {kr}: 감독 정보 없음 (team_id={team_id})')

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(coaches, f, ensure_ascii=False, indent=1)
    print(f'[collect_coaches] 완료: {len(coaches)}개 팀 저장')


if __name__ == '__main__':
    main()
