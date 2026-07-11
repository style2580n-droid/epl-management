# ⚽ 유럽 축구 데이터 센터 — 무료 최종판

「축구 API 데이터지원범위 총정리」 문서 5장 매핑 기준 구현체.
**전부 무료/오픈데이터** — 유료·체험형·스크래핑 소스 배제.

## 데이터 소스 매핑 (문서 5장 그대로)
| 데이터 | 1순위 | 백업 | 오픈데이터 |
|---|---|---|---|
| 일정·결과·순위 | football-data.org | — | OpenFootball (키 불필요, 자동 폴백) |
| 라이브 스코어·이벤트 | apifootball.com (180/h) | — | — |
| 팀 경기통계+xG | Highlightly (100/일) | BSD | Football-Data.co.uk CSV |
| 라인업·이적·부상 | football-data diff | API-Football (100/일) | — |
| 선수 경기별 스탯 | FPL 공식 (키 불필요) | API-Football | StatsBomb Open |
| 슛 단위 xG+좌표 | BSD (3,500+경기) | — | StatsBomb Open (모델 학습) |
| 팀 전력지수 | ClubElo (무인증) | — | — |

## GitHub Secrets
| 키 | 발급처 | 비고 |
|---|---|---|
| FOOTBALL_DATA_API_KEY | football-data.org 무료 가입 | 미등록 시 OpenFootball 폴백 |
| APIFOOTBALL_COM_KEY | apifootball.com | 라이브용, 최대 쿼터(180/h) |
| HIGHLIGHTLY_KEY | highlightly.net | 팀통계+xG |
| API_FOOTBALL_KEY | api-sports.io | 라인업/부상/이적 백업 |
| BSD_API_KEY | sports.bzzoiro.com | 슛 좌표 |
| OPENAI_API_KEY | (선택) | AI 총평, 없으면 생략 |

키가 하나도 없어도 무인증 소스(OpenFootball·StatsBomb·ClubElo·FPL·
Football-Data.co.uk·TheSportsDB '123')만으로 기본 파이프라인이 동작합니다.

## 구현 지표 (65+)
- **공격/패스**: xG·npxG·PSxG(xGOT)·xA·xOVA·SCA/GCA·Progressive 3종·
  Box/FT Entries·Deep Completions·Key/Through/Smart Passes·Cross Accuracy·
  Packing Rate·Line-Breaking·Switches 등
- **수비**: xGA·PPDA·카운터프레스·듀얼%·Pressure Efficiency 등
- **점유/전술**: Possession·Field Tilt·Territory·Build-up Success/Disruption %·
  Buildup Speed·Counter Attack Freq·Transition Efficiency·
  Press Resistance/Intensity·Counterpress Success %
- **GK**: PSxG +/-·Save %·Cross Claims·Sweeper·Launch Accuracy
- **AI 가치**: xT·VAEP(근사)·OBV(근사)
- **선수 능력치 5카테고리**: 슈팅정밀도/패스창의성/수비기여도/피지컬/심리안정성 (0~100)

**한계 (문서 결론 ④와 동일)**: 현행 시즌 전체 이벤트 좌표는 무료로 불가 →
StatsBomb(과거)로 모델 학습 + BSD 슛 좌표 부분 적용이 무료 한계선.
피지컬 정밀 지표(스프린트/속도)는 트래킹 데이터 필요 → Carry 기반 추정치 제공.

## 실행
GitHub push → Secrets 등록 → Actions 활성화 → 매일 KST 자정 자동 실행
→ `reports/latest.md` + `reports/transfers/` 확인.

## 데이터 계층 (v6 추가)
```
JSON 수집 → SQLite (data/football.db: leagues/teams/players/matches/events/transfers/injuries)
          → Normalizer (entity_map: 소스별 ID → 표준 ID, 'B. Saka'='Bukayo Saka' 자동 통합)
          → Viz Engine (reports/viz/: Shot Map · Passing Network · Heatmap SVG, 의존성 0)
          → Kloppy Adapter (StatsBomb 등 이질 포맷 → 표준 스키마, 미설치 시 내장 폴백)
```
SQLite라 서버 없이 저장소에 커밋되며, `sqlite3 data/football.db`로 바로 SQL 분석 가능.
