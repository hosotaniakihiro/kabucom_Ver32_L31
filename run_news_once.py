import configparser
import datetime as dt
from news_monitors import (
    fetch_yahoo_news,
    fetch_yahoo_article,
    summarize_text_gemini,
    send_discord_news
)

# ===== 設定ファイル読み込み =====
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
WEBHOOK_URL = conf.get("Discord", "webhook_url")
YAHOO_URL = conf.get("news", "yahoo_url")

if not WEBHOOK_URL:
    raise RuntimeError("❌ Discord Webhook URL が設定されていません。settings.ini を確認してください。")

print("🔍 Yahooニュースを取得中...")

# ===== ニュース取得 =====
results = fetch_yahoo_news(YAHOO_URL)

if not results:
    print("⚠️ ニュースが取得できませんでした")
else:
    for time_str, title, url in results[:3]:  # 3件だけテスト
        print(f"\n📰 {title} ({url})")

        text = fetch_yahoo_article(url)
        if not text:
            print("⚠️ 記事本文が取得できませんでした")
            continue

        summary = summarize_text_gemini(text, title)
        intro = text[:200].replace("\n", " ")

        # ===== Discord送信 =====
        send_discord_news(
            title,
            url,
            summary,
            intro,
            ["テスト"],
            icon="📰",
            color=0xFFD700,
            label="Yahoo!ニュース",
            webhook_url=WEBHOOK_URL
        )
        print(f"✅ Discord送信: {summary}")
