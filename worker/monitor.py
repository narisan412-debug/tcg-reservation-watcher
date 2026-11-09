import datetime
import hashlib
import json
import os
import pathlib
import re
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config.json'
OUT = ROOT / 'docs/data/results.json'
STATE = ROOT / 'worker/state.json'
NOW = datetime.datetime.now(datetime.timezone.utc).isoformat()

# 商品名にこれらが含まれる場合は購入候補から除外
UNAVAILABLE = ('売り切れ', '売切れ', '売切', '在庫切れ', '品切れ', '完売',
               '販売終了', '予約終了', '予約受付終了', '予約締切', '受付終了',
               '予約受付前', '予約開始前', '入荷待ち', '再入荷待ち',
               '取寄せ', '取り寄せ', '抽選販売', '抽選受付', '予約抽選')
ACCESSORIES = ('スリーブ', 'ローダー', 'デッキケース', 'ストレージ',
               '収納', '保護ケース', '空箱', 'パックのみ', 'オリパ',
               '福袋', 'シングルカード', 'カード単品')
MULTI = re.compile(r'(?:[2-9]|[1-9][0-9]+)\s*(?:box|ボックス|箱|ケース|カートン|個セット)', re.I)


def normalize(value):
    return re.sub(r'[\s\u3000_\-‐‑–—・「」『』【】\[\]()]', '', str(value)).casefold()


def get_json(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'TCGWatch/3.4', 'Accept': 'application/json'
    })
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def notify(topic, title, message, url):
    if not topic:
        raise ValueError('NTFY_TOPIC が未設定です')
    req = urllib.request.Request(
        'https://ntfy.sh/' + urllib.parse.quote(topic, safe=''),
        data=message.encode('utf-8'),
        headers={'Title': title, 'Click': url, 'Tags': 'bell'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        response.read()


def test_notification():
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    notify(topic, 'TCG Watch Test', 'テスト通知です！Androidへの通知接続は正常です。',
           'https://shopping.yahoo.co.jp/')
    print('Test notification sent successfully')


def classify(target, name):
    n = normalize(name)
    target_id = str(target.get('id', '')).upper()
    if any(normalize(word) in n for word in ACCESSORIES):
        return None
    if MULTI.search(name):
        return None
    if target_id == 'UA62BT':
        if not ('ua62bt' in n or ('ジョジョ' in n and 'ユニオンアリーナ' in n)):
            return None
        if 'ua62st' in n or 'スタートデッキ' in n or 'スターターデッキ' in n:
            return None
        if not ('box' in n or 'ボックス' in n or '16パック' in n):
            return None
        if 'ケース' in n or 'カートン' in n:
            return None
        return 'ブースターBOX'
    if target_id == 'UA62ST':
        if not ('ua62st' in n or ('ジョジョ' in n and 'ユニオンアリーナ' in n)):
            return None
        if 'ua62bt' in n or 'ブースターパック' in n or 'ブースターbox' in n:
            return None
        if not ('スタートデッキ' in n or 'スターターデッキ' in n or 'ua62st' in n):
            return None
        if 'box' in n or 'ボックス' in n or 'ケース' in n:
            return None
        return 'スタートデッキ単品'
    tokens = [normalize(t) for t in re.split(r'[\s\u3000]+', str(target.get('query', '')))
              if len(normalize(t)) >= 2]
    if tokens and not any(token in n for token in tokens):
        return None
    return '商品候補'


def purchasable(hit, name):
    n = normalize(name)
    if any(normalize(word) in n for word in UNAVAILABLE):
        return False, '販売終了・売切れ等の表記'
    # Yahoo!ショッピングの商品検索APIで取得した inStock が明示的に true の場合のみ通知。
    # 不明・欠落は購入可能と断定できないため通知しない。
    if hit.get('inStock') is not True:
        return False, '在庫ありを確認できない'
    return True, '予約受付中' if any(word in n for word in ('予約', 'ご予約')) else '在庫あり'


def queries_for(target):
    target_id = str(target.get('id', '')).upper()
    base = str(target.get('query', '')).strip()
    if target_id == 'UA62BT':
        return list(dict.fromkeys([base, 'UA62BT BOX', 'ジョジョ ユニオンアリーナ ブースター BOX']))
    if target_id == 'UA62ST':
        return list(dict.fromkeys([base, 'UA62ST スタートデッキ']))
    return [base]


def main():
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    try:
        previous = json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {'seen': []}
    seen = set(previous.get('seen', []))
    # 従来の seen は保持する。通知に失敗した候補は seen に追加しない。
    items, fresh, errors = [], [], []
    checked_codes = set()
    appid = os.environ.get('YAHOO_APP_ID', '').strip()
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    if not appid:
        errors.append('YAHOO_APP_ID が未設定です')

    for target in cfg.get('targets', [])[:30]:
        if not target.get('enabled', True):
            continue
        maxprice = target.get('max_price')
        if not isinstance(maxprice, (int, float)) or isinstance(maxprice, bool) or maxprice <= 0:
            errors.append('価格設定が不正: ' + str(target.get('id', '')))
            continue
        if not appid:
            continue
        for query in queries_for(target):
            if not query:
                continue
            params = {'appid': appid, 'query': query,
                      'results': min(max(int(target.get('results', 30)), 1), 50),
                      'sort': '-score'}
            url = ('https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch?'
                   + urllib.parse.urlencode(params))
            try:
                result = get_json(url)
                for hit in result.get('hits', []):
                    name = str(hit.get('name', ''))
                    price = hit.get('price')
                    page = hit.get('url', '')
                    if (not isinstance(price, (int, float)) or isinstance(price, bool)
                            or not 0 < price <= maxprice):
                        continue
                    if not isinstance(page, str) or not page.startswith('https://'):
                        continue
                    kind = classify(target, name)
                    if not kind:
                        continue
                    available, status = purchasable(hit, name)
                    if not available:
                        continue
                    itemid = str(hit.get('code') or page)
                    unique = (str(target.get('id', '')) + ':' + itemid)
                    if unique in checked_codes:
                        continue
                    checked_codes.add(unique)
                    key = hashlib.sha256(('yahoo:' + itemid + ':' + str(target.get('id', ''))).encode()).hexdigest()[:24]
                    entry = {
                        'key': key, 'target': target.get('label', query),
                        'name': name[:200], 'price': price, 'maxPrice': maxprice,
                        'shop': 'Yahoo!ショッピング', 'url': page,
                        'detectedAt': NOW, 'kind': kind,
                        'reservationHint': status == '予約受付中',
                        'status': status + '（購入可否は販売ページで最終確認）'
                    }
                    items.append(entry)
                    if key not in seen:
                        fresh.append(entry)
            except Exception as exc:
                errors.append('Yahoo検索失敗 ' + query + ': ' + str(exc)[:160])

    def priority(item):
        return (0 if item['kind'] == 'ブースターBOX' else 1,
                0 if item['reservationHint'] else 1, item['price'])
    items.sort(key=priority)
    fresh.sort(key=priority)
    notified = []
    for item in fresh[:15]:
        if not topic:
            errors.append('NTFY_TOPIC が未設定です')
            break
        try:
            notify(topic, 'TCG Watch: available candidate',
                   f"{item['status']} / {item['target']} / {item['name']} / {item['price']}円",
                   item['url'])
            notified.append(item['key'])
        except Exception as exc:
            errors.append('通知失敗: ' + str(exc)[:120])
    seen.update(notified)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({'seen': sorted(seen)}, ensure_ascii=False, indent=2), encoding='utf-8')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'version': '3.4', 'checkedAt': NOW,
        'source': 'Yahoo!ショッピング商品検索API', 'items': items[:200],
        'newCount': len(fresh), 'notifiedCount': len(notified), 'errors': errors,
        'note': 'APIの在庫フラグと商品名に基づく候補です。実際の購入・予約可否は販売ページで確認してください。'
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'checked={len(items)} new={len(fresh)} notified={len(notified)} errors={len(errors)}')


if __name__ == '__main__':
    if os.environ.get('TEST_NOTIFY') == '1':
        test_notification()
    else:
        main()
