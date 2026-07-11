# -*- coding: utf-8 -*-
"""
Matrix 병렬 Job 결과 병합기 v2
- transfer_targets.json : 리스트 병합 + (player_id, detected_at) 중복 제거
- previous_squads / leagues / teams / players_pl / fetch_state : dict 병합
- events/, metrics/ : 파일 복사
"""
import glob
import json
import os
import shutil
import sys

DICT_FILES = ['master/previous_squads.json', 'master/leagues.json',
              'master/teams.json', 'master/players_pl.json', 'fetch_state.json',
              # 보고서 3.2 지적 해소: 수집기 산출물 전체 병합
              'master/club_elo.json', 'master/match_stats.json',
              'master/live_scores.json', 'master/fixtures_openfootball.json',
              'master/injuries_af.json', 'master/lineups.json']


def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=4, ensure_ascii=False)


def merge(artifacts_dir, out_dir):
    transfers = _load(os.path.join(out_dir, 'master/transfer_targets.json'), [])
    seen = {(t.get('player_id'), t.get('detected_at')) for t in transfers}
    dicts = {f: _load(os.path.join(out_dir, f), {}) for f in DICT_FILES}

    for art in sorted(glob.glob(os.path.join(artifacts_dir, 'data-*'))):
        for t in _load(os.path.join(art, 'master/transfer_targets.json'), []):
            key = (t.get('player_id'), t.get('detected_at'))
            if key not in seen:
                transfers.append(t)
                seen.add(key)
        for f in DICT_FILES:
            dicts[f].update(_load(os.path.join(art, f), {}))
        for sub in ('events', 'metrics'):
            src = os.path.join(art, sub)
            if os.path.isdir(src):
                dst = os.path.join(out_dir, sub)
                os.makedirs(dst, exist_ok=True)
                for fp in glob.glob(os.path.join(src, '*.json')):
                    shutil.copy2(fp, dst)

    _save(os.path.join(out_dir, 'master/transfer_targets.json'), transfers)
    for f, obj in dicts.items():
        _save(os.path.join(out_dir, f), obj)
    print(f'[merge] 이적 {len(transfers)}건 병합 완료')


if __name__ == '__main__':
    merge(sys.argv[1] if len(sys.argv) > 1 else 'artifacts/',
          sys.argv[2] if len(sys.argv) > 2 else 'data/')
