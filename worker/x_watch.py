"""TCG Watch v5: X recent-search monitor (official API only)."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'data' / 'x_results.json'
STATE = ROOT / 'worker' / 'x_state.json'
NOW = dt.datetime.now(dt.timezone.utc).isoformat()

PRODUCTS = ('ジョジョ', 'ua62bt', 'ua62st', 'ユニオンアリーナ', 'union arena', 'ガンダムカード', 'gd07', 'ワンピースカード', 'one piece card')
LOTTERY = ('抽選', '応募', 'エントリー')
RESERVE = ('予約', '予約受付', '予約開始')
CLOSED = ('受付終了', '応募終了', '抽選終了', '予約終了', '締切済', '募集終了', '完売', '中止')
HIROSHIMA = ('広島市', '広島店', '広島本店', '広島駅', '紙屋町', '八丁堀', '本通', '横川', '五日市', '安佐南', '安佐北', '宇品', '段原', 'アルパーク')
SHIP = ('配送', '発送', '郵送', '通販', 'オンライン配送', '自宅にお届け')
PICKUP = ('店頭受取', '店頭受け取り', '店舗受取', '店舗受け取り', '店頭引換', '店頭引き換え')
ONLINE = ('web抽選', 'web応募', 'オンライン抽選', 'ネット抽選', 'オンライン応募', 'webから応募', '応募フォーム', 'ネットから応募')
IN_PERSON = ('抽選券', '整理券', '店頭配布', '来店抽選')


def classify(text):
    t = text.casefold()
    if not any(p.casefold() in t for p in PRODUCTS) or any(w in t for w in CLOSED):
        return None
    local = any(w.casefold() in t for w in HIROSHIMA)
    lottery = any(w in t for w in LOTTERY)
    reserve = any(w in t for w in RESERVE)
    if lottery:
        if any(w.casefold() in t for w in ONLINE):
            if any(w in t for w in SHIP):
                return ('ネット抽選・自宅配送', 'confirmed')
            if local and any(w in t for w in PICKUP):
                return ('ネット抽選・広島市内受取', 'confirmed')
            return ('ネット抽選・受取方法要確認', 'review')
        if local and any(w in t for w in IN_PERSON):
            return ('広島市内・店頭抽選券', 'confirmed')
        return None
    if reserve and local:
        return ('広島市内・予約情報', 'confirmed')
    return None


def query_x(token, query):
    params = {'query': query, 'max_results': '25', 'tweet.fields': 'created_at,author_id',
              'expansions': 'author_id', 'user.fields': 'username'}
    url = 'https://api.x.com/2/tweets/search/recent?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token,
                                                'User-Agent': 'TCGWatch/5.0'})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def notify(topic, item):
    url = 'https://x.com/i/web/status/' + item['id']
    headers = {'Title': 'TCG Watch X: ' + item['category'], 'Click': url,
               'Tags': 'ticket', 'Content-Type': 'text/plain; charset=utf-8'}
    req = urllib.request.Request('https://ntfy.sh/' + urllib.parse.quote(topic, safe=''),
                                 data=(item['text'][:450] + '\n' + url).encode('utf-8'),
                                 headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def main():
    token = os.getenv('X_BEARER_TOKEN', '').strip()
    topic = os.getenv('NTFY_TOPIC', '').strip()
    try:
        state = json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {'seen': []}
    seen = set(state.get('seen', []))
    output = {'version': '5.0', 'checkedAt': NOW, 'status': 'waiting_for_token' if not token else 'ok',
              'items': [], 'review': [], 'notifiedCount': 0, 'errors': []}
    if token:
        # X API検索の範囲は投稿本文。引用画像や外部リンクの内容は自動判定しない。
        queries = [
            '("ジョジョ" OR "UA62BT" OR "UA62ST") (抽選 OR 予約) -is:retweet',
            '("ガンダムカード" OR "GD07") (抽選 OR 予約) -is:retweet',
            '("ワンピースカード") (抽選 OR 予約) -is:retweet',
        ]
        posts = {}
        for q in queries:
            try:
                result = query_x(token, q)
                for p in result.get('data', []):
                    posts[str(p['id'])] = p
                if result.get('errors'):
                    output['errors'].append('X API部分エラー: ' + str(result['errors'])[:180])
            except Exception as exc:
                output['errors'].append('X API検索失敗: ' + str(exc)[:180])
        for p in sorted(posts.values(), key=lambda v: v.get('created_at', ''), reverse=True):
            decision = classify(p.get('text', ''))
            if not decision:
                continue
            category, confidence = decision
            item = {'id': str(p['id']), 'category': category, 'text': p.get('text', '')[:800],
                    'createdAt': p.get('created_at'), 'url': 'https://x.com/i/web/status/' + str(p['id']),
                    'verification': confidence}
            if confidence == 'review':
                output['review'].append(item)
                continue
            output['items'].append(item)
            if item['id'] not in seen and topic:
                try:
                    notify(topic, item)
                    seen.add(item['id'])
                    output['notifiedCount'] += 1
                except Exception as exc:
                    output['errors'].append('通知失敗: ' + str(exc)[:150])
        if output['errors']:
            output['status'] = 'partial_error'
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({'seen': sorted(seen)[-3000:]}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('X status=' + output['status'] + ' matched=' + str(len(output['items'])) +
          ' review=' + str(len(output['review'])) + ' notified=' + str(output['notifiedCount']))
    if output['errors']:
        print('X errors=' + json.dumps(output['errors'], ensure_ascii=False))


if __name__ == '__main__':
    if os.getenv('X_SELF_TEST') == '1':
        tests = [
            ('ジョジョ UA62BT WEB抽選 自宅配送', 'ネット抽選・自宅配送'),
            ('ガンダムカード 広島店 店頭抽選券配布', '広島市内・店頭抽選券'),
            ('ワンピースカード 広島店 予約開始', '広島市内・予約情報'),
            ('ジョジョ UA62BT WEB抽選 東京店 店頭受取', 'ネット抽選・受取方法要確認'),
            ('ジョジョ UA62BT WEB抽選 広島店 店頭受取', 'ネット抽選・広島市内受取'),
            ('ジョジョ UA62BT 予約終了 広島店', None),
            ('ジョジョ UA62BT 東京店 予約開始', None),
        ]
        for s, expected in tests:
            actual = classify(s)
            assert (actual[0] if actual else None) == expected, (s, actual)
        print('X_SELF_TEST PASS', len(tests))
    else:
        main()
