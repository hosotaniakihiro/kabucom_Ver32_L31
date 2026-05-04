import os, time, datetime as dt, logging, requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import configparser

from database import Session_summary
from database.models import JpxDisclosure, YahooNews, ReutersNews, NikkeiNews, ShikihoNews

# ===== 設定読み込み =====
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")

WEBHOOK_URL   = conf.get("Discord", "webhook_url", fallback=None)
GEMINI_API_KEY = conf.get("Gemini", "GEMINI_API_KEY", fallback=None)
NIKKEI_URL    = conf.get("news", "nikkei_url", fallback=None)
YAHOO_URL     = conf.get("news", "yahoo_url", fallback=None)
REUTERS_URL   = conf.get("news", "reuters_url", fallback=None)
SHIKIHO_URL   = conf.get("news", "shikiho_url", fallback=None)

logger = logging.getLogger(__name__)

# ===== 共通設定 =====
WATCH_WORDS = ["業績予想", "情報修正", "上方修正", "下方修正", "配当", "決算"]

# ===== Gemini設定 =====
if GEMINI_API_KEY:
    try:
        genai.configure(
            api_key=GEMINI_API_KEY,
            client_options={"api_endpoint": "https://aistudio.googleapis.com"}
        )
        logger.info("✅ Gemini APIキー(AI Studio) を設定しました")
    except Exception as e:
        logger.error(f"Gemini設定失敗: {e}")
else:
    logger.warning("⚠️ GEMINI_API_KEY が未設定です。要約はフォールバックします。")


# =====================================================
# 🔹 共通ユーティリティ
# =====================================================
def summarize_text_gemini(text, title):
    """Geminiでニュース要約（100文字程度、日本語）"""
    if not GEMINI_API_KEY:
        return text[:100]

    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""
あなたは金融アナリストです。以下は株ニュースや開示資料です。
投資家にとって重要な要点だけを日本語で100文字程度にまとめてください。

タイトル: {title}
本文抜粋: {text[:3000]}
"""
    try:
        resp = model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        logger.warning(f"Gemini要約失敗: {e}")
        return text[:100]


def send_discord_news(title, url, summary, intro, keywords, icon, color, label, webhook_url):
    """DiscordにEmbed形式でニュース送信"""
    keywords_str = "、".join(keywords) if keywords else "なし"
    embed = {
        "title": f"{icon} {title}",
        "url": url,
        "description": (
            f"**ソース:** {label}\n\n"
            f"**検出ワード:** {keywords_str}\n\n"
            f"**要約:** {summary}\n\n"
            f"**冒頭抜粋:** {intro}\n\n"
            f"🔗 [記事全文はこちら]({url})"
        ),
        "color": color
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]})
    if resp.status_code not in (200, 204):
        logger.error(f"Discord送信失敗: {resp.status_code} {resp.text}")


def save_to_db(model_class, time_str, title, url, summary, intro, keywords, code=None, snippets=None):
    """ニュースをDB保存"""
    session = Session_summary()
    try:
        try:
            dt_time = dt.datetime.strptime(time_str, "%Y/%m/%d %H:%M")
        except Exception:
            dt_time = dt.datetime.now()

        row_data = {
            "time": dt_time,
            "title": title,
            "url": url,
            "summary": summary,
            "intro": intro,
            "keywords": "、".join(keywords) if keywords else None,
        }
        if code: row_data["code"] = code
        if snippets: row_data["snippets"] = snippets

        session.add(model_class(**row_data))
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"⚠️ DB保存失敗: {e}")
    finally:
        session.close()


# =====================================================
# 🔹 各ニュースソース
# =====================================================
def fetch_jpx_disclosures(jpx_url):
    resp = requests.get(jpx_url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for row in soup.select("div.section.tableStyle01 tbody tr"):
        cols = row.select("td")
        if len(cols) >= 3:
            time_str = cols[0].get_text(strip=True)
            code = cols[1].get_text(strip=True)
            title = cols[2].get_text(strip=True)
            link_tag = cols[2].select_one("a")
            link = "https://www.jpx.co.jp" + link_tag["href"] if link_tag else ""
            if link.endswith(".pdf"):
                results.append((time_str, f"{code} {title}", link))
    return results


def fetch_yahoo_news(yahoo_url):
    """Yahooニュース一覧を取得"""
    resp = requests.get(yahoo_url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")
    results, seen_links = [], set()

    selectors = [
        "ul.sc-ksZaOG li a",
        "a.sc-esOvli.gntWNq",
        "a.newsFeed_item_link",
        "a"
    ]

    for sel in selectors:
        for a in soup.select(sel):
            title = a.get_text(strip=True)
            link = a.get("href", "")

            if not title or len(title) < 6: continue
            if not link: continue

            if link.startswith("/news/detail"):
                link = "https://finance.yahoo.co.jp" + link

            if not link.startswith("https://finance.yahoo.co.jp/news/detail"):
                continue

            if link in seen_links: continue
            seen_links.add(link)

            results.append((dt.datetime.now().strftime("%Y/%m/%d %H:%M"), title, link))
    return results


def fetch_yahoo_article(url):
    """Yahooニュース記事本文取得"""
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")

    selectors = [
        "div.article_body p",
        "div.articleMain p",
        "div.yjnMainArticle p",
        "div.article p",
        "p"
    ]
    for sel in selectors:
        parts = soup.select(sel)
        if parts:
            text = " ".join(p.get_text(strip=True) for p in parts)
            if len(text) > 50:
                return text
    return ""


def fetch_reuters_news(reuters_url):
    resp = requests.get(reuters_url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for article in soup.select("article.story"):
        a = article.select_one("a")
        if not a: continue
        link = "https://jp.reuters.com" + a["href"]
        title = a.get_text(strip=True)
        results.append((dt.datetime.now().strftime("%Y/%m/%d %H:%M"), title, link))
    return results


def fetch_reuters_article(url):
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")
    return " ".join(p.get_text() for p in soup.select("div.StandardArticleBody_body p"))


def fetch_nikkei_news(nikkei_url):
    resp = requests.get(nikkei_url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for a in soup.select("a"):
        href = a.get("href")
        if not href or str(href).strip().lower() == "none": continue
        if "/markets/stocks" not in href: continue
        if not href.startswith("http"):
            href = "https://www.nikkei.com" + href
        title = a.get_text(strip=True)
        if title:
            results.append((dt.datetime.now().strftime("%Y/%m/%d %H:%M"), title, href))
    return results


def fetch_nikkei_article(url):
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")
    return " ".join(p.get_text() for p in soup.select("p"))


def fetch_shikiho_news(shikiho_url):
    resp = requests.get(shikiho_url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for a in soup.select("a.article_title"):
        link = "https://shikiho.toyokeizai.net" + a["href"]
        title = a.get_text(strip=True)
        results.append((dt.datetime.now().strftime("%Y/%m/%d %H:%M"), title, link))
    return results


def fetch_shikiho_article(url):
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")
    return " ".join(p.get_text() for p in soup.select("div.article-body p"))
