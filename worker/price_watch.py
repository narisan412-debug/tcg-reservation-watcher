
#!/usr/bin/env python3
"""Run the monitor and store reference prices within configured limits."""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import monitor

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / 'docs/data/price_latest.json'
HISTORY = ROOT / 'docs/data/price_history.json'
MAX_HISTORY = 1000


def within_limit(price, limit):
    return (
        isinstance(price, (float, int))
        and not isinstance(price, bool)
        and isinstance(limit, (float, int))
        and not isinstance(limit, bool)
        and 0 < price <= limit
    )


def valid_listing(target, hit):
    if not isinstance(hit, dict):
        return False

    name = hit.get('name')
    url = hit.get('url')
    price = hit.get('price')

    if not isinstance(name, str) or not isinstance(url, str):
        return False

    if not within_limit(price, target.get('max_price')):
        return False

    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in (
            'shopping.yahoo.co.jp',
            'store.shopping.yahoo.co.jp'
        ):
            return False
    except ValueError:
        return False

    if not monitor.classify(target, name):
        return False

    n = monitor.normalize(name)
    target_id = str(target.get('id', '')).upper()

    if target_id in ('UA62BT', 'UA62ST', 'GD07'):
        if target_id.casefold() not in n:
            return False

    if target_id == 'ONEPIECE':
        if not (
            'ワンピース' in n
            and ('box' in n or 'ボックス' in n)
        ):
            return False

    return True


def summarize(target, unique_hits):
    rows = []

    for hit in unique_hits.values():
        if not valid_listing(target, hit):
            continue

        available, status = monitor.purchasable(
            hit, hit['name']
        )

        rows.append({
            'code': str(
                hit.get('code') or hit['url']
            )[:150],
            'name': hit['name'][:160],
            'price': int(hit['price']),
            'url': hit['url'],
            'availableFlag': bool(available),
            'availabilityLabel': status,
        })

    rows.sort(key=lambda x: x['price'])

    available_rows = [
        r for r in rows if r['availableFlag']
    ]

    sampled = {
        r['code']: r
        for r in (rows[:8] + available_rows[:4])
    }

    return {
        'label': target.get(
            'label', target.get('id', '')
        ),
        'maxPrice': target['max_price'],
        'listingCount': len(rows),
        'availableCount': len(available_rows),
        'minListedPrice': (
            rows[0]['price'] if rows else None
        ),
        'minAvailablePrice': (
            available_rows[0]['price']
            if available_rows else None
        ),
        'listings': sorted(
            sampled.values(),
            key=lambda x: x['price']
        ),
        'mixedProducts': (
            str(target.get('id', '')).lower()
            == 'onepiece'
        ),
    }


def clean_history_record(record, limits):
    if not isinstance(record, dict):
        return None

    existing = record.get('byTarget', {})

    if not isinstance(existing, dict):
        existing = {}

    cleaned = {}

    for tid, limit in limits.items():
        old = existing.get(tid)

        if not isinstance(old, dict):
            continue

        row = dict(old)

        for field, count_field in (
            ('minListedPrice', 'listingCount'),
            ('minAvailablePrice', 'availableCount')
        ):
            value = row.get(field)

            if not within_limit(value, limit):
                row[field] = None
                row[count_field] = 0

        cleaned[tid] = row

    return {
        'checkedAt': record.get('checkedAt'),
        'byTarget': cleaned,
    }


def save_price_data(cfg, collected, checked_at):
    targets = [
        t for t in cfg.get('targets', [])
        if t.get('enabled', True)
    ]

    by_target = {}
    limits = {}

    for target in targets:
        tid = str(target.get('id', ''))

        if not tid:
            continue

        limits[tid] = target.get('max_price')
        by_target[tid] = summarize(
            target, collected.get(tid, {})
        )

    latest = {
        'version': '4.5',
        'checkedAt': checked_at,
        'source': 'Yahoo!ショッピング商品検索API',
        'description': (
            '設定上限以下かつ商品一致の参考価格。'
            '売切れは区別して記録。'
            '価格超過は除外。通知判定は従来どおり。'
        ),
        'byTarget': by_target,
    }

    if HISTORY.exists():
        old = json.loads(
            HISTORY.read_text(encoding='utf-8')
        )
        history = old.get('records', [])

        if not isinstance(history, list):
            raise ValueError(
                'price_history.records must be a list'
            )
    else:
        history = []

    history = [
        clean_history_record(r, limits)
        for r in history
        if isinstance(r, dict)
        and r.get('checkedAt') != checked_at
    ]

    history = [
        r for r in history
        if r is not None and r.get('checkedAt')
    ]

    record = {
        'checkedAt': checked_at,
        'byTarget': {
            k: {
                field: v[field]
                for field in (
                    'minListedPrice',
                    'minAvailablePrice',
                    'listingCount',
                    'availableCount'
                )
            }
            for k, v in by_target.items()
        },
    }

    history.append(record)

    history.sort(
        key=lambda x: x.get('checkedAt', '')
    )

    history = history[-MAX_HISTORY:]

    LATEST.parent.mkdir(
        parents=True, exist_ok=True
    )

    LATEST.write_text(
        json.dumps(
            latest,
            ensure_ascii=False,
            indent=2
        ) + '\n',
        encoding='utf-8'
    )

    HISTORY.write_text(
        json.dumps(
            {
                'version': '4.5',
                'records': history
            },
            ensure_ascii=False,
            indent=2
        ) + '\n',
        encoding='utf-8'
    )

    print(
        f'PRICE_HISTORY records={len(history)} '
        f'targets={len(by_target)}'
    )

    for k, v in by_target.items():
        print(
            f'PRICE {k}: '
            f'listings={v["listingCount"]} '
            f'available={v["availableCount"]} '
            f'min={v["minListedPrice"]} '
            f'min_available={v["minAvailablePrice"]}'
        )


def run_monitor_with_capture():
    cfg = json.loads(
        monitor.CONFIG.read_text(
            encoding='utf-8'
        )
    )

    queries = {}
    collected = {}

    for target in cfg.get('targets', []):
        if not target.get('enabled', True):
            continue

        tid = str(target.get('id', ''))
        collected[tid] = {}

        for query in monitor.queries_for(target):
            queries.setdefault(
                query, []
            ).append(target)

    original_get_json = monitor.get_json

    def capture_get_json(url):
        result = original_get_json(url)

        try:
            query = parse_qs(
                urlparse(url).query
            ).get('query', [''])[0]

            for target in queries.get(query, []):
                tid = str(target['id'])

                for hit in result.get('hits', []):
                    if valid_listing(target, hit):
                        code = str(
                            hit.get('code') or hit['url']
                        )
                        collected[tid][code] = hit

        except (
            TypeError,
            ValueError,
            AttributeError,
            KeyError
        ) as exc:
            print(
                'PRICE_CAPTURE_WARNING:', exc
            )

        return result

    monitor.get_json = capture_get_json

    try:
        monitor.main()
    finally:
        monitor.get_json = original_get_json

    results = json.loads(
        monitor.OUT.read_text(
            encoding='utf-8'
        )
    )

    if results.get('errors'):
        print(
            'Price history skipped because '
            'monitoring reported errors'
        )
        return

    checked_at = results.get('checkedAt')

    if not isinstance(checked_at, str) or not checked_at:
        raise ValueError(
            'Missing checkedAt'
        )

    try:
        save_price_data(
            cfg, collected, checked_at
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError
    ) as exc:
        print(
            'PRICE_SAVE_WARNING: '
            + str(exc)[:200]
        )


def self_test():
    box = {
        'id': 'UA62BT',
        'label': 'Test BOX',
        'query': 'UA62BT BOX',
        'max_price': 6160,
    }

    starter = {
        'id': 'UA62ST',
        'label': 'Test ST',
        'query': 'UA62ST',
        'max_price': 1650,
    }

    def hit(name, price, stock=True, code='a'):
        return {
            'name': name,
            'price': price,
            'inStock': stock,
            'url': (
                'https://store.shopping.yahoo.co.jp/'
                'example/' + code + '.html'
            ),
            'code': code,
        }

    cases = [
        (
            'overpriced box excluded',
            valid_listing(
                box,
                hit('ジョジョ UA62BT ブースターBOX', 9900)
            ),
            False
        ),
        (
            'limit price accepted',
            valid_listing(
                box,
                hit('ジョジョ UA62BT ブースターBOX', 6160)
            ),
            True
        ),
        (
            'under limit accepted',
            valid_listing(
                box,
                hit('ジョジョ UA62BT ブースターBOX', 5000)
            ),
            True
        ),
        (
            'starter excluded from box',
            valid_listing(
                box,
                hit('ジョジョ UA62ST スタートデッキ', 1500)
            ),
            False
        ),
        (
            'carton excluded',
            valid_listing(
                box,
                hit('ジョジョ UA62BT BOX カートン', 5000)
            ),
            False
        ),
        (
            'other code excluded',
            valid_listing(
                box,
                hit('ジョジョ UA61BT BOX', 5000)
            ),
            False
        ),
        (
            'unsafe URL excluded',
            valid_listing(
                box,
                {
                    **hit(
                        'ジョジョ UA62BT BOX', 5000
                    ),
                    'url': 'https://example.org/a'
                }
            ),
            False
        ),
        (
            'overpriced starter excluded',
            valid_listing(
                starter,
                hit('ジョジョ UA62ST スタートデッキ', 2000)
            ),
            False
        ),
        (
            'starter at cap accepted',
            valid_listing(
                starter,
                hit('ジョジョ UA62ST スタートデッキ', 1650)
            ),
            True
        ),
    ]

    rows = {
        'a': hit(
            'ジョジョ UA62BT BOX',
            6000, False, 'a'
        ),
        'b': hit(
            'ジョジョ UA62BT BOX',
            6160, True, 'b'
        ),
        'c': hit(
            'ジョジョ UA62BT BOX',
            9000, True, 'c'
        ),
    }

    summary = summarize(box, rows)

    cases += [
        (
            'min listed',
            summary['minListedPrice'],
            6000
        ),
        (
            'min available',
            summary['minAvailablePrice'],
            6160
        ),
        (
            'listed count',
            summary['listingCount'],
            2
        ),
        (
            'available count',
            summary['availableCount'],
            1
        ),
        (
            'soldout only',
            summarize(
                box,
                {'a': rows['a']}
            )['minAvailablePrice'],
            None
        ),
    ]

    old = {
        'checkedAt': '2026-11-10T00:00:00+00:00',
        'byTarget': {
            'UA62BT': {
                'minListedPrice': 9000,
                'minAvailablePrice': 10800,
                'listingCount': 5,
                'availableCount': 2
            },
            'UA62ST': {
                'minListedPrice': 1630,
                'minAvailablePrice': 3780,
                'listingCount': 5,
                'availableCount': 2
            }
        }
    }

    cleaned = clean_history_record(
        old,
        {
            'UA62BT': 6160,
            'UA62ST': 1650
        }
    )

    cases += [
        (
            'old overpriced listed removed',
            cleaned['byTarget']['UA62BT']['minListedPrice'],
            None
        ),
        (
            'old overpriced available removed',
            cleaned['byTarget']['UA62BT']['minAvailablePrice'],
            None
        ),
        (
            'old under-cap listing retained',
            cleaned['byTarget']['UA62ST']['minListedPrice'],
            1630
        ),
        (
            'old overpriced starter available removed',
            cleaned['byTarget']['UA62ST']['minAvailablePrice'],
            None
        ),
    ]

    failures = 0

    for label, actual, expected in cases:
        passed = actual == expected

        print(
            ('PASS' if passed else 'FAIL')
            + ': ' + label
        )

        failures += int(not passed)

    print(
        f'PRICE_SELF_TEST total={len(cases)} '
        f'failed={failures}'
    )

    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    if os.environ.get('PRICE_SELF_TEST') == '1':
        self_test()
    else:
        run_monitor_with_capture()
