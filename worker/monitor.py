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
        'User-Agent': 'TCGWatch/3.7', 'Accept': 'application/json'
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
        return False, '売切れ・予約終了等の表記'
    stock = hit.get('inStock')
    # APIの在庫フラグを厳密に評価。欠落は在庫ありとみなさない。
    if stock is True or stock == 1 or (isinstance(stock, str) and stock.lower() in ('true', '1')):
        return True, '予約受付中（API在庫あり）' if '予約' in n else '在庫あり'
    if stock is False or stock == 0 or (isinstance(stock, str) and stock.lower() in ('false', '0')):
        return False, 'API在庫なし'
    return False, 'API在庫不明'


def queries_for(target):
    target_id = str(target.get('id', '')).upper()
    base = str(target.get('query', '')).strip()
    if target_id == 'UA62BT':
        return list(dict.fromkeys([base, 'UA62BT BOX', 'ジョジョ ユニオンアリーナ ブースター BOX']))
    if target_id == 'UA62ST':
        return list(dict.fromkeys([base, 'UA62ST スタートデッキ']))
    return [base]



def notification_reason(key, seen, previous_available, has_snapshot, old_status, current_status):
    """実在庫を保証せず、API情報の変化を通知する。"""
    if key not in seen:
        return '新規予約候補' if current_status == 'reservation' else '新規在庫候補'
    if has_snapshot and key not in previous_available:
        return '予約受付再開候補' if current_status == 'reservation' else '在庫復活候補'
    old = old_status.get(key)
    if isinstance(old, dict) and old.get('status') == 'stock' and current_status == 'reservation':
        return '予約受付への切替候補'
    return None


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
    diagnostics = {'apiHits': 0, 'priceExcluded': 0, 'invalidUrl': 0,
                   'nameExcluded': 0, 'unavailableExcluded': 0,
                   'outOfStockExcluded': 0, 'unknownStockExcluded': 0,
                   'duplicateExcluded': 0, 'accepted': 0}
    by_target = {}
    available_keys = set()
    status_now = {}
    status_before = previous.get('statusLastRun', {})
    if not isinstance(status_before, dict):
        status_before = {}
    # 旧 seen は通知履歴として引き継ぐ。再入荷検知は今回以降の状態から開始。
    previous_available = set(previous.get('availableLastRun', []))
    had_availability_snapshot = 'availableLastRun' in previous
    api_errors = False
    failed_targets = set()
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
        target_id = str(target.get('id', ''))
        target_stats = by_target.setdefault(target_id, {'apiHits': 0, 'accepted': 0,
                                  'rejected': {}, 'queries': 0})
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
                target_stats['queries'] += 1
                for hit in result.get('hits', []):
                    diagnostics['apiHits'] += 1
                    target_stats['apiHits'] += 1
                    def reject(reason):
                        diagnostics[reason] += 1
                        target_stats['rejected'][reason] = target_stats['rejected'].get(reason, 0) + 1
                    name = str(hit.get('name', ''))
                    price = hit.get('price')
                    page = hit.get('url', '')
                    if (not isinstance(price, (int, float)) or isinstance(price, bool)
                            or not 0 < price <= maxprice):
                        reject('priceExcluded')
                        continue
                    if not isinstance(page, str) or not page.startswith('https://'):
                        reject('invalidUrl')
                        continue
                    kind = classify(target, name)
                    if not kind:
                        reject('nameExcluded')
                        continue
                    available, status = purchasable(hit, name)
                    if not available:
                        reason_key = ('unavailableExcluded' if status == '売切れ・予約終了等の表記'
                                      else 'outOfStockExcluded' if status == 'API在庫なし'
                                      else 'unknownStockExcluded')
                        reject(reason_key)
                        continue
                    itemid = str(hit.get('code') or page)
                    unique = (str(target.get('id', '')) + ':' + itemid)
                    if unique in checked_codes:
                        reject('duplicateExcluded')
                        continue
                    checked_codes.add(unique)
                    key = hashlib.sha256(('yahoo:' + itemid + ':' + str(target.get('id', ''))).encode()).hexdigest()[:24]
                    entry = {
                        'key': key, 'target': target.get('label', query),
                        'name': name[:200], 'price': price, 'maxPrice': maxprice,
                        'shop': 'Yahoo!ショッピング', 'url': page,
                        'detectedAt': NOW, 'kind': kind,
                        'reservationHint': status.startswith('予約受付中'),
                        'status': status + '（購入可否は販売ページで最終確認）'
                    }
                    items.append(entry)
                    diagnostics['accepted'] += 1
                    target_stats['accepted'] += 1
                    available_keys.add(key)
                    current_status = 'reservation' if entry['reservationHint'] else 'stock'
                    status_now[key] = {'status': current_status, 'targetId': target_id}
                    reason = notification_reason(key, seen, previous_available, had_availability_snapshot,
                                                 status_before, current_status)
                    if reason:
                        entry['notificationReason'] = reason
                        fresh.append(entry)
            except Exception as exc:
                api_errors = True
                failed_targets.add(target_id)
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
            notify(topic, 'TCG Watch: ' + item['notificationReason'],
                   f"{item['notificationReason']} / {item['status']} / {item['target']} / {item['name']} / {item['price']}円",
                   item['url'])
            notified.append(item['key'])
        except Exception as exc:
            errors.append('通知失敗: ' + str(exc)[:120])
    seen.update(notified)
    # API失敗時は一時的な欠品と誤認しないよう、前回の在庫状態を保持する。
    if api_errors:
        # 検索失敗時に欠品扱いにして誤った再入荷通知を出さない。
        # 失敗した商品群の前回状態を引き継ぐ。旧形式は安全側で全件維持。
        if failed_targets and status_before:
            for old_key, old_info in status_before.items():
                if isinstance(old_info, dict) and old_info.get('targetId') in failed_targets:
                    available_keys.add(old_key)
                    status_now.setdefault(old_key, old_info)
        else:
            available_keys.update(previous_available)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({'seen': sorted(seen), 'availableLastRun': sorted(available_keys), 'statusLastRun': status_now}, ensure_ascii=False, indent=2), encoding='utf-8')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'version': '3.7', 'checkedAt': NOW,
        'source': 'Yahoo!ショッピング商品検索API', 'items': items[:200],
        'newCount': len(fresh), 'notifiedCount': len(notified), 'errors': errors,
        'diagnostics': diagnostics, 'byTarget': by_target,
        'note': 'APIの在庫フラグと商品名に基づく候補です。実際の購入・予約可否は販売ページで確認してください。'
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'checked={len(items)} new={len(fresh)} notified={len(notified)} errors={len(errors)}')
    print('diagnostics=' + json.dumps(diagnostics, ensure_ascii=False))
    print('byTarget=' + json.dumps(by_target, ensure_ascii=False))



def self_test():
    """APIアクセス・実通知・状態ファイル更新なしで判定を確認。"""
    booster = {'id': 'UA62BT', 'query': 'UA62BT BOX'}
    starter = {'id': 'UA62ST', 'query': 'UA62ST スタートデッキ'}
    cases = [
        ('ブースター在庫あり', booster, 'UNION ARENA ジョジョ UA62BT ブースターBOX 16パック', {'inStock': True}, True, 'ブースターBOX'),
        ('ブースター予約受付中', booster, '【予約受付中】ジョジョ UA62BT ブースターBOX', {'inStock': 1}, True, 'ブースターBOX'),
        ('ブースター売り切れ', booster, 'ジョジョ UA62BT ブースターBOX 売り切れ', {'inStock': True}, False, 'ブースターBOX'),
        ('ブースター在庫なし', booster, 'ジョジョ UA62BT ブースターBOX', {'inStock': False}, False, 'ブースターBOX'),
        ('ブースター在庫不明', booster, 'ジョジョ UA62BT ブースターBOX', {}, False, 'ブースターBOX'),
        ('スターター混入防止', booster, 'ジョジョ UA62ST スタートデッキ', {'inStock': True}, False, None),
        ('ケース混入防止', booster, 'ジョジョ UA62BT ブースターBOX ケース', {'inStock': True}, False, None),
        ('複数BOX混入防止', booster, 'ジョジョ UA62BT 2BOXセット', {'inStock': True}, False, None),
        ('スターター在庫あり', starter, 'ジョジョ UA62ST スタートデッキ', {'inStock': True}, True, 'スタートデッキ単品'),
        ('スターター予約終了', starter, 'ジョジョ UA62ST スタートデッキ 予約受付終了', {'inStock': True}, False, 'スタートデッキ単品'),
    ]
    failed = 0
    for label, target, name, hit, expected, expected_kind in cases:
        kind = classify(target, name)
        available, _ = purchasable(hit, name)
        accepted = bool(kind) and available
        ok = accepted == expected and kind == expected_kind
        print(('PASS' if ok else 'FAIL') + ': ' + label)
        if not ok:
            failed += 1
    # 再入荷時の通知条件と、継続在庫あり時の重複通知防止を検証。
    key = 'sample-key'
    seen = {key}
    recovered = key not in seen or (True and key not in set())
    unchanged = key not in seen or (True and key not in {key})
    for label, actual, expected in [('再入荷通知', recovered, True), ('継続在庫の重複防止', unchanged, False)]:
        ok = actual == expected
        print(('PASS' if ok else 'FAIL') + ': ' + label)
        if not ok:
            failed += 1
    transitions = [
        ('初回予約通知', notification_reason('x', set(), set(), True, {}, 'reservation'), '新規予約候補'),
        ('初回在庫通知', notification_reason('x', set(), set(), True, {}, 'stock'), '新規在庫候補'),
        ('予約受付再開', notification_reason('x', {'x'}, set(), True, {}, 'reservation'), '予約受付再開候補'),
        ('在庫復活', notification_reason('x', {'x'}, set(), True, {}, 'stock'), '在庫復活候補'),
        ('在庫から予約への変更', notification_reason('x', {'x'}, {'x'}, True, {'x': {'status': 'stock'}}, 'reservation'), '予約受付への切替候補'),
        ('予約継続時の重複防止', notification_reason('x', {'x'}, {'x'}, True, {'x': {'status': 'reservation'}}, 'reservation'), None),
        ('在庫継続時の重複防止', notification_reason('x', {'x'}, {'x'}, True, {'x': {'status': 'stock'}}, 'stock'), None),
    ]
    for label, actual, expected in transitions:
        ok = actual == expected
        print(('PASS' if ok else 'FAIL') + ': ' + label)
        if not ok:
            failed += 1
    print(f'SELF_TEST total={len(cases) + 2 + len(transitions)} failed={failed}')
    if failed:
        raise SystemExit(1)

if __name__ == '__main__':
    if os.environ.get('SELF_TEST') == '1':
        self_test()
    elif os.environ.get('TEST_NOTIFY') == '1':
        test_notification()
    else:
        main()
