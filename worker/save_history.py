#!/usr/bin/env python3
"""Append successful monitoring snapshots without changing monitor.py."""
import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'docs/data/results.json'
HISTORY = ROOT / 'docs/data/history.json'
MAX_RECORDS = 1000


def main():
    data = json.loads(RESULTS.read_text(encoding='utf-8'))
    if data.get('errors'):
        print('Monitor reported errors; skip history snapshot')
        return
    checked_at = data.get('checkedAt')
    if not isinstance(checked_at, str) or not checked_at:
        raise ValueError('results.json has no checkedAt')
    try:
        dt = datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('checkedAt must include timezone')
    except ValueError as exc:
        raise ValueError('Invalid checkedAt') from exc
    old = {'version': '4.0', 'records': []}
    if HISTORY.exists():
        old = json.loads(HISTORY.read_text(encoding='utf-8'))
    records = old.get('records', [])
    if not isinstance(records, list):
        raise ValueError('history.records must be a list')
    diagnostics = data.get('diagnostics') or {}
    targets = data.get('byTarget') or {}
    record = {
        'checkedAt': checked_at,
        'apiHits': int(diagnostics.get('apiHits', 0)),
        'accepted': int(diagnostics.get('accepted', len(data.get('items') or []))),
        'newCount': int(data.get('newCount', 0)),
        'notifiedCount': int(data.get('notifiedCount', 0)),
        'byTarget': {
            str(k): {'apiHits': int(v.get('apiHits', 0)), 'accepted': int(v.get('accepted', 0))}
            for k, v in targets.items() if isinstance(v, dict)
        },
    }
    records = [r for r in records if isinstance(r, dict) and r.get('checkedAt') != checked_at]
    records.append(record)
    records.sort(key=lambda r: r.get('checkedAt', ''))
    records = records[-MAX_RECORDS:]
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps({'version': '4.0', 'records': records}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'History saved: {len(records)} records; latest={checked_at}')


if __name__ == '__main__':
    main()
