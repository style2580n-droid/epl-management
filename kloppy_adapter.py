# -*- coding: utf-8 -*-
"""
이벤트 포맷 표준화 어댑터 (Kloppy 통합)

역할: StatsBomb Open Data 등 소스별로 다른 이벤트 포맷을
      우리 impact_engine 표준 스키마(120x80, type/team/player/x/y/...)로 변환.

전략:
  · kloppy가 설치되어 있으면(GitHub Actions에선 requirements로 설치됨)
    검증된 kloppy 로더/좌표계 변환을 사용
  · 미설치 환경에선 내장 폴백 매퍼로 동일 스키마 산출 (동작 보장)
"""
import json

try:
    from kloppy import statsbomb as _kloppy_sb
    HAS_KLOPPY = True
except ImportError:
    HAS_KLOPPY = False

STANDARD_TYPES = {
    'Pass': 'Pass', 'Shot': 'Shot', 'Carry': 'Carry', 'Dribble': 'Dribble',
    'Pressure': 'Pressure', 'Duel': 'Duel', 'Interception': 'Interception',
    'Ball Recovery': 'Recovery', 'Block': 'Block', 'Clearance': 'Clearance',
    'Foul Committed': 'Foul', 'Foul Won': 'Foul Won', 'Offside': 'Offside',
    'Goal Keeper': 'Save',
}


def _sec(ev):
    return {'minute': ev.get('minute', 0), 'second': ev.get('second', 0)}


# ============================================== 내장 폴백: StatsBomb → 표준
def _fallback_statsbomb(raw_events):
    """kloppy 없이 StatsBomb 원본 JSON을 표준 스키마로 변환.
    StatsBomb도 120x80 좌표계라 스케일 변환 불필요."""
    out = []
    for ev in raw_events:
        t = STANDARD_TYPES.get((ev.get('type') or {}).get('name'))
        if not t:
            continue
        loc = ev.get('location') or [None, None]
        e = {
            'type': t,
            'team': (ev.get('team') or {}).get('name'),
            'player': (ev.get('player') or {}).get('name'),
            'x': loc[0], 'y': loc[1],
            **_sec(ev),
            'under_pressure': bool(ev.get('under_pressure')),
        }
        if t == 'Pass':
            p = ev.get('pass', {})
            end = p.get('end_location') or [None, None]
            e.update({
                'end_x': end[0], 'end_y': end[1],
                'outcome': 'Incomplete' if p.get('outcome') else 'Complete',
                'cross': bool(p.get('cross')),
                'through_ball': (p.get('technique') or {}).get('name') == 'Through Ball',
                'receiver': (p.get('recipient') or {}).get('name'),
            })
        elif t == 'Shot':
            s = ev.get('shot', {})
            end = s.get('end_location') or [None, None, None]
            outcome = (s.get('outcome') or {}).get('name')
            e.update({
                'outcome': 'Goal' if outcome == 'Goal'
                           else 'Saved' if outcome == 'Saved' else outcome,
                'xg': s.get('statsbomb_xg'),
                'shot_end_y': end[1] if len(end) > 1 else None,
                'shot_end_z': end[2] if len(end) > 2 else None,
                'situation': 'Penalty' if (s.get('type') or {}).get('name') == 'Penalty'
                             else 'Set Piece' if (s.get('type') or {}).get('name')
                             in ('Free Kick', 'Corner') else 'Open Play',
                'freeze_frame': [
                    {'x': ff['location'][0], 'y': ff['location'][1]}
                    for ff in (s.get('freeze_frame') or [])
                    if not ff.get('teammate')],
            })
        elif t == 'Carry':
            end = (ev.get('carry') or {}).get('end_location') or [None, None]
            e.update({'end_x': end[0], 'end_y': end[1]})
        elif t == 'Duel':
            outcome = ((ev.get('duel') or {}).get('outcome') or {}).get('name', '')
            e['outcome'] = 'Won' if 'Won' in outcome or 'Success' in outcome else 'Lost'
        elif t == 'Dribble':
            outcome = ((ev.get('dribble') or {}).get('outcome') or {}).get('name')
            e['outcome'] = 'Won' if outcome == 'Complete' else 'Lost'
        out.append(e)
    return out


def _via_kloppy(events_path):
    """kloppy 로더 사용 (통일 좌표계 → 우리 120x80 스키마)."""
    ds = _kloppy_sb.load(event_data=events_path,
                         coordinates='statsbomb')
    out = []
    for ev in ds.events:
        name = type(ev).__name__.replace('Event', '')
        coord = ev.coordinates
        rec = {
            'type': {'BallOut': None, 'GenericEvent': None}.get(name, name),
            'team': str(ev.team) if ev.team else None,
            'player': str(ev.player) if ev.player else None,
            'x': coord.x if coord else None,
            'y': coord.y if coord else None,
            'minute': int(ev.timestamp.total_seconds() // 60)
                      if ev.timestamp else 0,
            'second': int(ev.timestamp.total_seconds() % 60)
                      if ev.timestamp else 0,
        }
        if rec['type']:
            out.append(rec)
    return out


def convert_statsbomb(events_source, prefer_kloppy=True):
    """
    StatsBomb 이벤트 → 표준 스키마.
    events_source: 파일 경로 또는 이미 로드된 원본 리스트.
    """
    if isinstance(events_source, str):
        if HAS_KLOPPY and prefer_kloppy:
            try:
                return _via_kloppy(events_source), 'kloppy'
            except Exception as ex:
                print(f'[kloppy] 실패 → 내장 폴백 사용: {ex}')
        with open(events_source, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    else:
        raw = events_source
    return _fallback_statsbomb(raw), 'builtin'


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        events, engine = convert_statsbomb(sys.argv[1])
        print(f'[adapter] {engine} 엔진으로 {len(events)}건 변환')
    else:
        print(f'kloppy 설치 여부: {HAS_KLOPPY}')
