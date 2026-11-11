#!/usr/bin/env python3
"""TCG Watch v5.0: official Hiroshima shop announcements, no paid X API."""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'docs/data/site_results.json'
STATE = ROOT / 'worker/site_state.json'
SOURCE = 'https://www.c-labo.jp/shop/hiroshima/blog/'
PRODUCTS = ('ワンピース', 'ONE PIECE', 'ガンダム', 'GUNDAM', 'ジョジョ', 'ユニオンアリーナ', 'UNION ARENA', 'UA62', 'GD07')
EVENTS = ('抽選', '予約', '応募', '受付')
CLOSED = ('受付終了', '応募終了', '抽選終了', '予約終了', '中止', '完売')

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href = dict(attrs).get('href')
            self.parts = []
    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)
    def handle_endtag(self, tag):
        if tag == 'a' and self.href is not None:
            self.links.append((' '.join(' '.join(self.parts).split()), self.href))
            self.href = None
            self.parts = []

def relevant(title):
    lower = title.casefold()
    return (any(w.casefold() in lower for w in PRODUCTS)
            and any(w in title for w in EVENTS)
            and not any(w in title for w in CLOSED))

def safe_url(raw):
    if not isinstance(raw, str):
        return None
    url = urllib.parse.urljoin(SOURCE, raw)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'www.c-labo.jp':
        return None
    if not parsed.path.startswith('/shop/hiroshima/'):
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

def extract(html):
    parser = Links()
    parser.feed(html)
    found = {}
    for title, raw in parser.links:
        url = safe_url(raw)
        if url and url != SOURCE and relevant(title):
            found[url] = {'title': title[:200], 'url': url, 'source': 'カードラボ広島店',
                          'status': '要確認（受付中とは限りません）'}
    return list(found.values())

def main():
    result = {'version': '5.0-site-test', 'checkedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
              'source': SOURCE, 'status': 'ok', 'items': [], 'newItems': [], 'errors': [],
              'note': '掲載候補です。受付期間・受取方法は公式サイトで確認してください。'}
    try:
        req = urllib.request.Request(SOURCE, headers={'User-Agent': 'TCGWatch/5.0', 'Accept': 'text/html'})
        with urllib.request.urlopen(req, timeout=20) as response:
            html = response.read(2_000_000).decode('utf-8', errors='replace')
        result['items'] = extract(html)
        previous = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else None
        if previous is not None and not isinstance(previous.get('seen'), list):
            raise ValueError('Invalid state format')
        known = set(previous['seen']) if previous else set()
        result['status'] = 'baseline_created' if previous is None else 'ok'
        result['newItems'] = [] if previous is None else [x for x in result['items'] if x['url'] not in known]
        known.update(x['url'] for x in result['items'])
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({'seen': sorted(known)[-3000:]}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except Exception as exc:
        result['status'] = 'error'
        result['errors'].append(str(exc)[:250])
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"SITE status={result['status']} candidates={len(result['items'])} new={len(result['newItems'])}")
    if result['errors']:
        print('SITE errors=' + json.dumps(result['errors'], ensure_ascii=False))
        raise SystemExit(1)

def self_test():
    sample = '<a href="/shop/hiroshima/blog/123/">ワンピースカード 抽選予約</a><a href="https://evil.example/x">ジョジョ 予約</a><a href="/shop/tokyo/blog/1/">ガンダム 抽選</a>'
    rows = extract(sample)
    assert len(rows) == 1 and rows[0]['url'].endswith('/123/')
    assert relevant('ガンダムカード 予約開始')
    assert not relevant('ガンダムカード 予約終了')
    assert not relevant('ただのお知らせ')
    print('SITE_SELF_TEST PASS 4')

if __name__ == '__main__':
    self_test() if os.getenv('SITE_SELF_TEST') == '1' else main()
