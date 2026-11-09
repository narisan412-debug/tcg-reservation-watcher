
def test_notification():
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    if not topic:
        print('ERROR: NTFY_TOPIC is not set')
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
