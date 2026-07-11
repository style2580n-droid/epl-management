# -*- coding: utf-8 -*-
"""
데이터 보존 정책 (Retention) — 저장소 비대화 방지

  · data/events, data/metrics의 경기별 JSON 중 RETENTION_DAYS(기본 30일)
    지난 파일을 data/archive/YYYY-MM.zip으로 이동 (원본 삭제)
  · 시즌 집계(season_*.json)·프로파일 등 누적 산출물과 SQLite는 보존
  · zip은 append 모드 — 매일 실행해도 월 파일 하나에 누적

주의: 아카이브 전에 season_aggregator가 이미 누적을 반영했으므로 정보 손실 없음.
"""
import os
import time
import zipfile
from datetime import datetime, timezone

RETENTION_DAYS = int(os.getenv('RETENTION_DAYS', '30'))
TARGET_DIRS = ('data/events', 'data/metrics')
KEEP_PREFIXES = ('season_', 'player_profiles', 'transfer_impact')
ARCHIVE_DIR = 'data/archive'


def archive_old_files(retention_days=RETENTION_DAYS, now=None):
    now = now or time.time()
    cutoff = now - retention_days * 86400
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    moved = 0
    for d in TARGET_DIRS:
        if not os.path.isdir(d):
            continue
        for fname in sorted(os.listdir(d)):
            if not fname.endswith('.json'):
                continue
            if any(fname.startswith(p) for p in KEEP_PREFIXES):
                continue
            path = os.path.join(d, fname)
            mtime = os.path.getmtime(path)
            if mtime >= cutoff:
                continue
            month = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%Y-%m')
            zpath = os.path.join(ARCHIVE_DIR, f'{month}.zip')
            arcname = f'{os.path.basename(d)}/{fname}'
            with zipfile.ZipFile(zpath, 'a', zipfile.ZIP_DEFLATED) as z:
                if arcname not in z.namelist():
                    z.write(path, arcname)
            os.remove(path)
            moved += 1
    if moved:
        print(f'[retention] {moved}개 파일 → {ARCHIVE_DIR}/ 월별 아카이브')
    else:
        print('[retention] 아카이브 대상 없음')
    return moved


if __name__ == '__main__':
    archive_old_files()
