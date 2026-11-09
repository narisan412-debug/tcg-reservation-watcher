
import json
import os
import pathlib
import urllib.parse
import urllib.request
import datetime
import hashlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config.json'
OUT = ROOT / 'docs/data/results.json'
STATE = ROOT / 'worker/state.json'

now = datetime.datetime.now(
    datetime.timezone.utc
).isoformat()


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'TCGWatch/0.4',
            'Accept': 'application/json'
        }
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def notify(topic, title, message, url):
    if not topic:
        return

    req = urllib.request.Request(
        'https://ntfy.sh/' + urllib.parse.quote(topic, safe=''),
        data=message.encode('utf-8'),
        headers={
            'Title': title,
            'Click': url,
            'Tags': 'bell'
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def test_notification():
    topic = os.environ.get('NTFY_TOPIC', '').strip()

    if not topic:
        print('ERROR: NTFY_TOPIC is not configured')
        raise SystemExit(1)

    try:
        notify(
            topic,
            'TCG Watch Test',
            'テスト通知です！Androidへの通知接続は正常です。',
            'https://shopping.yahoo.co.jp/'
        )
        print('Test notification sent successfully')
    except Exception as e:
        print('Test notification failed:', str(e))
        raise SystemExit(1)


def main():
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))

    previous = (
        json.loads(STATE.read_text(encoding='utf-8'))
        if STATE.exists()
        else {'seen': []}
    )

    seen = set(previous.get('seen', []))
    fresh = []
    items = []
    errors = []

    appid = os.environ.get('YAHOO_APP_ID', '').strip()

    if not appid:
        errors.append('YAHOO_APP_ID が未設定です。')

    for target in cfg.get('targets', [])[:30]:
        if not target.get('enabled', True):
            continue

        query = str(target.get('query', '')).strip()
        maxprice = target.get('max_price')

        if (
            not query
            or not isinstance(maxprice, (int, float))
            or isinstance(maxprice, bool)
            or maxprice <= 0
        ):
            errors.append('検索設定が不正: ' + query)
            continue

        if not appid:
            continue

        params = {
            'appid': appid,
            'query': query,
            'results': min(int(target.get('results', 20)), 50),
            'sort': '-score'
        }

        url = (
            'https://shopping.yahooapis.jp/'
            'ShoppingWebService/V3/itemSearch?'
            + urllib.parse.urlencode(params)
        )

        try:
            result = get_json(url)

            for hit in result.get('hits', []):
                name = str(hit.get('name', ''))
                price = hit.get('price')
                page = hit.get('url', '')

                if (
                    not isinstance(price, (int, float))
                    or isinstance(price, bool)
                    or price > maxprice
                    or price <= 0
                ):
                    continue

                if (
                    not isinstance(page, str)
                    or not page.startswith('https://')
                ):
                    continue

                tokens = [
                    t for t in re.split(r'[\s　]+', query)
                    if len(t) >= 2
                ]

                if tokens and not any(
                    t.casefold() in name.casefold()
                    for t in tokens
                ):
                    continue

                itemid = str(hit.get('code') or page)

                key = hashlib.sha256(
                    (
                        'yahoo:' + itemid + ':'
                        + str(target.get('id', ''))
                    ).encode()
                ).hexdigest()[:24]

                entry = {
                    'key': key,
                    'target': target.get('label', query),
                    'name': name[:200],
                    'price': price,
                    'maxPrice': maxprice,
                    'shop': 'Yahoo!ショッピング',
                    'url': page,
                    'detectedAt': now,
                    'status': '価格条件に合う販売候補（予約可否は要確認）'
                }

                items.append(entry)

                if key not in seen:
                    fresh.append(entry)

        except Exception as e:
            errors.append(
                'Yahoo検索失敗 ' + query + ': ' + str(e)[:180]
            )

    topic = os.environ.get('NTFY_TOPIC', '').strip()
    notified = []

    for item in fresh[:15]:
        if not topic:
            errors.append('NTFY_TOPIC が未設定です。')
            break

        try:
            notify(
                topic,
                'TCG Watch: new candidate',
                (
                    f"{item['target']} / "
                    f"{item['name']} / "
                    f"{item['price']}円"
                    '（予約可否は販売ページで確認）'
                ),
                item['url']
            )
            notified.append(item['key'])

        except Exception as e:
            errors.append('通知失敗: ' + str(e)[:100])

    # 通知に失敗した候補は次回も通知を試せるようにする
    seen.update(
        i['key'] for i in items
        if i['key'] not in {f['key'] for f in fresh}
    )
    seen.update(notified)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {'seen': sorted(seen)},
            ensure_ascii=False,
            indent=2
        ),
        encoding='utf-8'
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                'version': 3,
                'checkedAt': now,
                'source': 'Yahoo!ショッピング商品検索API',
                'items': items[:200],
                'newCount': len(fresh),
                'errors': errors,
                'note': (
                    '予約確定情報ではありません。'
                    '購入可否・送料・販売元は各ページで確認してください。'
                )
            },
            ensure_ascii=False,
            indent=2
        ),
        encoding='utf-8'
    )

    print(
        f'checked={len(items)} '
        f'new={len(fresh)} '
        f'errors={len(errors)}'
    )


if __name__ == '__main__':
    if os.environ.get('TEST_NOTIFY') == '1':
        test_notification()
    else:
        main()
