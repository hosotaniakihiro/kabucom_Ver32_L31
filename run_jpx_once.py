# run_jpx_once.py
import logging
import requests
import pandas as pd
import io   # ← 追加
import configparser
import google.generativeai as genai
from utils.alerts_util import send_discord_notify  # 共通通知関数を流用

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")

WEBHOOK_URL = conf.get("Discord", "webhook_url", fallback=None)
GEMINI_API_KEY = conf.get("Gemini", "GEMINI_API_KEY", fallback=None)
JPX_CSV_URL = "https://www.jpx.co.jp/listing/disclosure/index.csv"  # 公式CSV URL

logger = logging.getLogger(__name__)

# ===== Gemini設定 =====
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY,
                        client_options={"api_endpoint": "https://aistudio.googleapis.com"})
        logger.info("✅ Gemini APIキーを設定しました")
    except Exception as e:
        logger.error(f"Gemini設定失敗: {e}")
else:
    logger.warning("⚠️ GEMINI_API_KEY が未設定です。")

# ===== Gemini 要約 =====
def summarize_text_gemini(text, title):
    if not GEMINI_API_KEY:
        return text[:100]

    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""
あなたは金融アナリストです。以下はJPX開示の内容です。
投資家にとって重要な要点を100文字程度で日本語要約してください。

タイトル: {title}
本文抜粋: {text[:1000]}
"""
    try:
        resp = model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        logger.warning(f"Gemini要約失敗: {e}")
        return text[:100]

#results = fetch_jpx_disclosures_csv()

def fetch_jpx_disclosures_csv():
    print("🔍 JPX開示CSVを取得中...")

    # JPXのCSV公開URL（例：適時開示のCSV）
    url = "https://www.jpx.co.jp/listing/disclosure/index.html"  # 実際はCSVの直リンクに変更が必要

    resp = requests.get(url)
    resp.encoding = "shift_jis"  # JPXはShift-JISのことが多い

    print("=== CSV先頭500文字 ===")
    print(resp.text[:500])  # 生のテキスト先頭を確認
    print("======================")

    try:
        # 区切り文字を自動推定
        df = pd.read_csv(io.StringIO(resp.text), sep=None, engine="python")
        print("✅ CSV列名:", df.columns.tolist())
        print(df.head())  # 先頭5行を表示
    except Exception as e:
        print(f"❌ CSV読み込みエラー: {e}")
        return []

    return df

def run_once():
    print("🔍 JPX開示CSVを取得中...")
    try:
        disclosures = fetch_jpx_disclosures_csv()
        if not disclosures:
            print("⚠️ CSVから開示が取得できませんでした")
            return

        for time_str, title, url in disclosures[:5]:  # 最新5件だけ通知
            summary = summarize_text_gemini(title, title)
            intro = title[:100]
            msg = (
                f"📘 JPX開示\n"
                f"🕒 {time_str}\n"
                f"📌 {title}\n"
                f"🔗 {url}\n"
                f"📝 {summary}"
            )
            send_discord_notify(msg, WEBHOOK_URL)
            print(msg)

    except Exception as e:
        logger.error(f"JPX処理エラー: {e}")

if __name__ == "__main__":
    results = fetch_jpx_disclosures_csv()
