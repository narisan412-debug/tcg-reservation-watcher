#!/usr/bin/env python3
"""Run the existing monitor and collect NON-notifying reference listing prices.

This calls the existing monitor.main() exactly once, intercepting its successful
Yahoo API responses for price statistics. It does not make additional API calls
or modify notification/stock decisions. It never stores Yahoo API credentials.
"""
import datetime as dt
import json
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import monitor

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / 'docs/data/price_latest.json'
HISTORY = ROOT / 'docs/data/price_history.json'
MAX_HISTORY = 1000


def valid_listing(target, hit):
    """Keep code-identifiable, single-product Yahoo listings only."""
    if not isinstance(hit, dict):
        return False
    name = hit.get('name', '')
    url = hit.get('url', '')
    price = hit.get('price')
    if not isinstance(name, str) or not isinstance(url, str):
        return False
    if not isinstance(price, (float, int)) or isinstance(price, bool) or not 0 < price < 1000000:
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('shopping.yahoo.co.jp', 'store.shopping.yahoo.co.jp'):
            return False
    except ValueError:
        return False
    if not monitor.classify(target, name):
        return False
    n = monitor.normalize(name)
    target_id = str(target.get('id', '')).upper()
    # Requiring exact product codes is intentionally stricter for price trends.
    if target_id in ('UA62BT', 'UA62ST', 'GD07') and target_id.casefold() not in n:
        return False
    if target_id == 'ONEPIECE' and not ('ワンピース' in n and ('box' in n or 'ボックス' in n)):
        return False
    return True


def summarize(target, unique_hits):
    """Summarize a target's *listed* prices, separately from purchasable flags."""
    rows = []
    for hit in unique_hits.values():
        if not valid_listing(target, hit):
            continue
        available, status = monitor.purchasable(hit, hit['name'])
        rows.append({
            'code': str(hit.get('code') or hit['url'])[:150],
            'name': hit['name'][:160],
            'price': int(hit['price']),
            'url': hit['url'],
            'availableFlag': bool(available),
            'availabilityLabel': status,
        })
    rows.sort(key=lambda x: x['price'])
    available_rows = [x for x in rows if x['availableFlag']]
    return {
        'label': target.get('label', target.get('id', '')),
        'maxPrice': target['max_price'],
        'listingCount': len(rows),
        'availableCount': len(available_rows),
        'minListedPrice': rows[0]['price'] if rows else None,
        'minAvailablePrice': available_rows[0]['price'] if available_rows else None,
        'listings': sorted({r['code']: r for r in (rows[:8] + available_rows[:4])}.values(), key=lambda x: x['price']),
        # Generic One Piece search covers multiple sets, not the same product.
        'mixedProducts': str(target.get('id', '')).lower() == 'onepiece',
    }


def save_price_data(cfg, collected, checked_at):
    by_target = {}
    for target in cfg.get('targets', []):
        if not target.get('enabled', True):
            continue
        tid = str(target.get('id', ''))
        if not tid:
            continue
        by_target[tid] = summarize(target, collected.get(tid, {}))
    latest = {
        'version': '4.3',
        'checkedAt': checked_at,
        'source': 'Yahoo!ショッピング商品検索API',
        'description': '名称・商品コードが合う商品の参考掲載価格。価格上限超過・売切れを含む。通知判定には使用しない。',
        'byTarget': by_target,
    }
    if HISTORY.exists():
        old = json.loads(HISTORY.read_text(encoding='utf-8'))
        history = old.get('records', [])
        if not isinstance(history, list):
            raise ValueError('price_history.records must be a list')
    else:
        history = []
    record = {
        'checkedAt': checked_at,
        'byTarget': {k: {field: v[field] for field in (
            'minListedPrice', 'minAvailablePrice', 'listingCount', 'availableCount')}
            for k, v in by_target.items()},
    }
    history = [r for r in history if isinstance(r, dict) and r.get('checkedAt') != checked_at]
    history.append(record)
    history.sort(key=lambda x: x.get('checkedAt', ''))
    history = history[-MAX_HISTORY:]
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    HISTORY.write_text(json.dumps({'version': '4.3', 'records': history}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'PRICE_HISTORY records={len(history)} targets={len(by_target)}')
    for k, v in by_target.items():
        print(f'PRICE {k}: listings={v["listingCount"]} available={v["availableCount"]} min={v["minListedPrice"]} min_available={v["minAvailablePrice"]}')


def run_monitor_with_capture():
    cfg = json.loads(monitor.CONFIG.read_text(encoding='utf-8'))
    queries = {}
    collected = {}
    for target in cfg.get('targets', []):
        if not target.get('enabled', True):
            continue
        tid = str(target.get('id', ''))
        collected[tid] = {}
        for q in monitor.queries_for(target):
            queries.setdefault(q, []).append(target)
    original_get_json = monitor.get_json
    def capture_get_json(url):
        result = original_get_json(url)
        try:
            query = parse_qs(urlparse(url).query).get('query', [''])[0]
            for target in queries.get(query, []):
                tid = str(target['id'])
                for hit in result.get('hits', []):
                    if not valid_listing(target, hit):
                        continue
                    code = str(hit.get('code') or hit['url'])
                    # One listing can occur in multiple query results.
                    collected[tid][code] = hit
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            print('PRICE_CAPTURE_WARNING:', exc)
        return result

    monitor.get_json = capture_get_json
    try:
        monitor.main()  # Existing notifications, results and state, unchanged.
    finally:
        monitor.get_json = original_get_json
    results = json.loads(monitor.OUT.read_text(encoding='utf-8'))
    if results.get('errors'):
        print('Price history skipped because monitoring reported errors')
        return
    checked_at = results.get('checkedAt')
    if not isinstance(checked_at, str) or not checked_at:
        raise ValueError('Missing monitored checkedAt; refusing to save prices')
    try:
        save_price_data(cfg, collected, checked_at)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Do not block saving notification history if reference-price storage fails.
        print('PRICE_SAVE_WARNING: Monitoring succeeded, but price history was not saved: ' + str(exc)[:200])


def self_test():
    box = {'id': 'UA62BT', 'label': 'Test BOX', 'query': 'UA62BT BOX', 'max_price': 6160}
    starter = {'id': 'UA62ST', 'label': 'Test ST', 'query': 'UA62ST', 'max_price': 1650}
    def hit(name, price, stock=True, code='a'):
        return {'name': name, 'price': price, 'inStock': stock,
                'url': 'https://store.shopping.yahoo.co.jp/example/' + code + '.html', 'code': code}
    cases = [
        ('valid overpriced box recorded', valid_listing(box, hit('ジョジョ UA62BT ブースターBOX', 9900)), True),
        ('no starter in box graph', valid_listing(box, hit('ジョジョ UA62ST スタートデッキ', 1500)), False),
        ('no cartons', valid_listing(box, hit('ジョジョ UA62BT BOX カートン', 10000)), False),
        ('no different series', valid_listing(box, hit('ジョジョ UA61BT BOX', 8500)), False),
        ('no unsafe URL', valid_listing(box, {**hit('ジョジョ UA62BT BOX', 8000), 'url': 'https://example.org/a'}), False),
        ('starter recognized', valid_listing(starter, hit('ジョジョ UA62ST スタートデッキ', 2000)), True),
    ]
    rows = {'a': hit('ジョジョ UA62BT BOX', 9000, False, 'a'),
            'b': hit('ジョジョ UA62BT BOX', 8000, True, 'b')}
    summary = summarize(box, rows)
    cases += [
        ('min listed', summary['minListedPrice'], 8000),
        ('min available', summary['minAvailablePrice'], 8000),
        ('listing count', summary['listingCount'], 2),
        ('no available', summarize(box, {'a': rows['a']})['minAvailablePrice'], None),
    ]
    fails = 0
    for label, actual, expect in cases:
        ok = actual == expect
        print(('PASS' if ok else 'FAIL') + ': ' + label)
        fails += 0 if ok else 1
    print(f'PRICE_SELF_TEST total={len(cases)} failed={fails}')
    if fails:
        raise SystemExit(1)


if __name__ == '__main__':
    if os.environ.get('PRICE_SELF_TEST') == '1':
        self_test()
    else:
        run_monitor_with_capture()
