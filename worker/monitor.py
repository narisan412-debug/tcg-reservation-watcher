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
NOW = datetime.datetime.now(datetime.timezone.utc).isoformat()

BLOCK = ('スリーブ', 'ローダー', 'デッキケース', '収納ケース', '保管ケース', '空箱', '空ボックス', '空box', 'カードケース', 'プレイマット', 'シングルカード', 'シングル', 'オリパ', '福袋', '中古', '開封済', 'バラ売り', 'バラパック', 'パックセット', 'boxケース', 'box用ケース', 'プロテクター', 'アクリル', '予約券', '抽選券')
MULTI = re.compile(r'(?:[2-9]|[1-9][0-9]+)\s*(?:box|ボックス|箱|個|点|セット|ケース)|(?:box|ボックス|箱|個|点)\s*[×xX*]\s*(?:[2-9]|[1-9][0-9]+)|(?:[2-9]|[1-9][0-9]+)\s*(?:box|ボックス)\s*セット', re.I)

def norm(s):
    return re.sub(r'[\s\u3000\-ー_・「」『』\[\]【】()（）]+', '', str(s)).casefold()

def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'TCGWatch/3.3', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)

def notify(topic, title, message, url):
    if not topic:
        raise ValueError('NTFY_TOPIC が未設定')
    req = urllib.request.Request(
        'https://ntfy.sh/' + urllib.parse.quote(topic, safe=''),
        data=message.encode('utf-8'),
        headers={'Title': title, 'Click': url, 'Tags': 'bell'}, method='POST'
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()

def test_notification():
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    if not topic:
        print('ERROR: NTFY_TOPIC is not configured')
        raise SystemExit(1)
    try:
        notify(topic, 'TCG Watch Test', 'テスト通知です！Androidへの通知接続は正常です。', 'https://shopping.yahoo.co.jp/')
        print('Test notification sent successfully')
    except Exception as e:
        print('Test notification failed:', str(e))
        raise SystemExit(1)

def classify(target, name):
    n = norm(name)
    target_id = str(target.get('id', '')).upper()
    if any(norm(word) in n for word in BLOCK):
        return None
    if MULTI.search(name):
        return None
    if target_id == 'UA62BT':
        if 'ua62st' in n or 'スタートデッキ' in n or 'スターターデッキ' in n:
            return None
        if not ('ジョジョ' in n or 'jojo' in n or 'ua62bt' in n):
            return None
        if 'ua62bt' not in n and not ('ユニオンアリーナ' in n or 'unionarena' in n):
            return None
        if not ('box' in n or 'ボックス' in n or '16パック' in n):
            return None
        if 'パック' in n and '16パック' not in n and 'box' not in n and 'ボックス' not in n:
            return None
        return 'ブースターBOX'
    if target_id == 'UA62ST':
        if 'ua62bt' in n or 'ブースターパック' in n:
            return None
        if not ('ジョジョ' in n or 'jojo' in n or 'ua62st' in n):
            return None
        if not ('ua62st' in n or 'スタートデッキ' in n or 'スターターデッキ' in n):
            return None
        if 'box' in n or 'ボックス' in n or 'カートン' in n:
            return None
        return 'スタートデッキ単品'
    # Existing targets retain broadly compatible matching, but reject obvious accessories.
    tokens = [t for t in re.split(r'[\s\u3000]+', str(target.get('query', ''))) if len(t) >= 2]
    if tokens and not any(norm(t) in n for t in tokens):
        return None
    return '商品候補'

def main():
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    try:
        previous = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'seen': []}
    except (ValueError, OSError):
        previous = {'seen': []}
    seen = set(previous.get('seen', []))
    fresh, items, errors = [], [], []
    appid = os.environ.get('YAHOO_APP_ID', '').strip()
    if not appid:
        errors.append('YAHOO_APP_ID が未設定です。')
    for target in cfg.get('targets', [])[:30]:
        if not target.get('enabled', True):
            continue
        query = str(target.get('query', '')).strip()
        maxprice = target.get('max_price')
        if not query or not isinstance(maxprice, (int, float)) or isinstance(maxprice, bool) or maxprice <= 0:
            errors.append('検索設定が不正: ' + query)
            continue
        if not appid:
            continue
        queries = [query]
        if str(target.get('id', '')).upper() == 'UA62BT':
            queries = ['UA62BT BOX', 'ジョジョ ユニオンアリーナ BOX', query]
        elif str(target.get('id', '')).upper() == 'UA62ST':
            queries = ['UA62ST', 'ジョジョ スタートデッキ', query]
        for q in dict.fromkeys(queries):
            params = {'appid': appid, 'query': q, 'results': min(max(int(target.get('results', 30)), 1), 50), 'sort': '-score'}
            url = 'https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch?' + urllib.parse.urlencode(params)
            try:
                result = get_json(url)
                for hit in result.get('hits', []):
                    name = str(hit.get('name', ''))
                    price = hit.get('price')
                    page = hit.get('url', '')
                    if not isinstance(price, (int, float)) or isinstance(price, bool) or not 0 < price <= maxprice:
                        continue
                    if not isinstance(page, str) or not page.startswith('https://'):
                        continue
                    kind = classify(target, name)
                    if not kind:
                        continue
                    itemid = str(hit.get('code') or page)
                    key = hashlib.sha256(('yahoo:' + itemid + ':' + str(target.get('id', ''))).encode()).hexdigest()[:24]
                    if any(i['key'] == key for i in items):
                        continue
                    is_reservation = any(word in norm(name) for word in ('予約', '発売前', '予約受付'))
                    entry = {
                        'key': key, 'target': target.get('label', query), 'name': name[:200],
                        'price': price, 'maxPrice': maxprice, 'shop': 'Yahoo!ショッピング',
                        'url': page, 'detectedAt': NOW, 'kind': kind,
                        'reservationHint': is_reservation,
                        'status': '予約表記あり・販売ページで要確認' if is_reservation else '価格条件に合う販売候補（予約可否は要確認）'
                    }
                    items.append(entry)
                    if key not in seen:
                        fresh.append(entry)
            except Exception as e:
                errors.append('Yahoo検索失敗 ' + q + ': ' + str(e)[:180])
    items.sort(key=lambda i: (i['target'] != 'ジョジョの奇妙な冒険 ブースターBOX' and i.get('kind') != 'ブースターBOX', not i['reservationHint'], i['price']))
    fresh.sort(key=lambda i: (i.get('kind') != 'ブースターBOX', not i['reservationHint'], i['price']))
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    notified = []
    for item in fresh[:15]:
        if not topic:
            errors.append('NTFY_TOPIC が未設定です。')
            break
        try:
            notify(topic, 'TCG Watch: new candidate', f"{item['target']} / {item['kind']} / {item['name']} / {item['price']}円（予約可否は販売ページで確認）", item['url'])
            notified.append(item['key'])
        except Exception as e:
            errors.append('通知失敗: ' + str(e)[:100])
    seen.update(i['key'] for i in items if i['key'] not in {f['key'] for f in fresh})
    seen.update(notified)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({'seen': sorted(seen)}, ensure_ascii=False, indent=2), encoding='utf-8')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'version': '3.3', 'checkedAt': NOW, 'source': 'Yahoo!ショッピング商品検索API',
        'items': items[:200], 'newCount': len(fresh), 'errors': errors,
        'note': '予約確定情報ではありません。送料・数量・販売元・予約可否は販売ページで確認してください。'
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'checked={len(items)} new={len(fresh)} errors={len(errors)}')

if __name__ == '__main__':
    if os.environ.get('TEST_NOTIFY') == '1':
        test_notification()
    else:
        main()
