# -*- coding: utf-8 -*-
"""
MLS(미국)·노르웨이 엘리테세리엔 팀명 매핑 초안 (2026-07-24 착수).

앱 통합은 보류 상태 — 이 파일은 팀 매핑 데이터만 담는다(사용자 결정: "일단
프로필/팀매핑만, 앱 통합은 나중"). LEAGUE_TEAM_MAPS(app_export_multileague.py,
6개 리그 전용)는 안 건드림 — 이 딕셔너리는 완전히 별도.

리그 정보 (rehearse_mls_norway_probe.py 실측 확정, 2026-07-23/24):
  mls: league_id=18, season_id=158, 팀 30개
  eliteserien: league_id=54, season_id=1230, 팀 16개

⚠️ 정직하게: 아래 별칭 목록은 "1차 초안"이다. 각 팀마다 실측 근거가 다르다:
  📍 [확정] = 이번 rehearse_mls_norway_probe.py 실행 로그(teams 샘플 [:10])
      또는 훨씬 이전 세션의 파이프라인 "잡음" 로그(다른 대회 이벤트가 우리
      리그에 안 섞이는지 진단하던 로그, 예: SandefjordFotball_HamKam_207009)
      에서 BSD가 실제로 반환한 표기 그대로 가져온 것 — 신뢰도 높음.
  (표시 없음) = 웹 검색(위키피디아 2026 시즌 문서)으로 확인한 공식 클럽명
      기반 best-effort. BSD 표기가 다를 수 있다(예: "FC" 접미사 생략,
      "D.C." → "DC" 축약 패턴이 확정 팀들에서 이미 관찰됨 — 그래서 아래
      별칭에 FC 유무 두 버전을 다 넣어뒀다). 실제 팀 수집기를 돌려서
      매칭 실패 팀이 나오면(collect_fixtures_multileague.py의 [diag]
      미매칭 원문 로그 패턴과 동일하게) 그 표기를 추가할 것 — 지금 이
      초안만으로 100% 매칭된다고 가정하지 말 것.

한글명은 국내 축구 매체 통용 표기 기준. 확정된 표기 없는 팀(엘리테세리엔
소규모 클럽 다수)은 표준 노르웨이어 발음 표기로 채움 — 통용 표기와 다를 수
있음(수정 환영).
"""

# ============================================================ MLS (30팀)
# league_id=18, season_id=158
MLS_TEAM_MAP = {
    '애틀랜타 유나이티드': ['Atlanta United', 'Atlanta United FC'],       # 📍확정(구 잡음로그)
    '오스틴 FC': ['Austin FC', 'Austin'],                              # 📍확정
    'CF 몬트리올': ['CF Montréal', 'CF Montreal'],                      # 📍확정
    '샬럿 FC': ['Charlotte FC', 'Charlotte'],                          # 📍확정
    '시카고 파이어': ['Chicago Fire', 'Chicago Fire FC'],                # 📍확정
    '콜로라도 래피즈': ['Colorado Rapids'],                              # 📍확정
    '콜럼버스 크루': ['Columbus Crew', 'Columbus Crew SC'],              # 📍확정
    'DC 유나이티드': ['DC United', 'D.C. United'],                      # 📍확정
    'FC 신시내티': ['FC Cincinnati', 'Cincinnati'],
    'FC 댈러스': ['FC Dallas', 'Dallas'],                               # 📍확정
    '휴스턴 다이나모': ['Houston Dynamo', 'Houston Dynamo FC'],          # 📍확정
    '인터 마이애미': ['Inter Miami', 'Inter Miami CF'],
    'LA 갤럭시': ['LA Galaxy', 'Los Angeles Galaxy'],                   # 📍확정
    '로스앤젤레스 FC': ['Los Angeles FC', 'LAFC'],                       # 📍확정
    '미네소타 유나이티드': ['Minnesota United', 'Minnesota United FC'],   # 📍확정
    '내슈빌 SC': ['Nashville SC', 'Nashville'],                        # 📍확정
    '뉴잉글랜드 레볼루션': ['New England Revolution', 'New England'],
    '뉴욕 시티 FC': ['New York City FC', 'NYCFC'],
    '뉴욕 레드불스': ['New York Red Bulls', 'NY Red Bulls'],
    '올랜도 시티': ['Orlando City SC', 'Orlando City'],                 # 📍확정
    '필라델피아 유니온': ['Philadelphia Union', 'Philadelphia'],
    '포틀랜드 팀버스': ['Portland Timbers'],                            # 📍확정
    '리얼 솔트레이크': ['Real Salt Lake'],                              # 📍확정
    '샌디에이고 FC': ['San Diego FC', 'San Diego'],                     # 📍확정
    '산호세 어스퀘이크스': ['San Jose Earthquakes'],                     # 📍확정
    '시애틀 사운더스': ['Seattle Sounders FC', 'Seattle Sounders'],      # 📍확정
    '스포르팅 캔자스시티': ['Sporting Kansas City', 'Sporting KC'],       # 📍확정
    '세인트루이스 시티': ['St. Louis City SC', 'St Louis City',
                    'St. Louis City'],                                # 📍확정
    '토론토 FC': ['Toronto FC', 'Toronto'],                            # 📍확정
    '밴쿠버 화이트캡스': ['Vancouver Whitecaps FC', 'Vancouver Whitecaps'],  # 📍확정
}

# ============================================================ 엘리테세리엔 (16팀)
# league_id=54, season_id=1230
ELITESERIEN_TEAM_MAP = {
    '올레순': ['Aalesunds FK', 'Aalesund'],                            # 📍확정
    '보되글림트': ['Bodø/Glimt', 'Bodo/Glimt', 'FK Bodø/Glimt'],        # 📍확정
    '프레드릭스타': ['Fredrikstad FK', 'Fredrikstad'],                  # 📍확정
    '함캄': ['HamKam', 'Hamarkameratene'],                            # 📍확정
    'IK 스타트': ['IK Start', 'Start'],                                # 📍확정
    'KFUM 오슬로': ['KFUM Oslo'],                                      # 📍확정
    '크리스티안순': ['Kristiansund BK', 'Kristiansund'],                # 📍확정
    '릴레스트룀': ['Lillestrøm SK', 'Lillestrom SK', 'Lillestrøm'],     # 📍확정
    '몰데': ['Molde FK', 'Molde'],                                    # 📍확정
    '로젠보르그': ['Rosenborg BK', 'Rosenborg'],                       # 📍확정
    '브란': ['SK Brann', 'Brann'],                                    # 📍확정(구 잡음로그)
    '트롬쇠': ['Tromsø IL', 'Tromso IL', 'Tromsø'],                    # 📍확정(구 잡음로그)
    '발레렝가': ['Vålerenga IF', 'Valerenga IF', 'Vålerenga'],         # 📍확정(구 잡음로그)
    '산네피오르': ['Sandefjord Fotball', 'Sandefjord'],                 # 📍확정(구 잡음로그)
    '사르프스보르그 08': ['Sarpsborg 08', 'Sarpsborg 08 FF'],
    '비킹': ['Viking FK', 'Viking'],
}

MLS_NORWAY_TEAM_MAPS = {
    'mls': MLS_TEAM_MAP,
    'eliteserien': ELITESERIEN_TEAM_MAP,
}

LEAGUE_IDS = {
    'mls': {'league_id': 18, 'season_id': 158},
    'eliteserien': {'league_id': 54, 'season_id': 1230},
}
