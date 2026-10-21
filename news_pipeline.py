# ============================================================
# File   : news_pipeline.py
# ------------------------------------------------------------
# ニュース/適時開示の「保存の土台(A)」と「材料スコア算出(B)」。
#
#   A) 保存       : NewsDisclosure テーブルへ UPSERT 的に保存
#      JPX 収集   : 適時開示 HTML を取得 → 銘柄コードつきで保存
#      常駐ループ : run_loop() を data_collectors 同様にタスク化
#   B) 材料スコア : compute_news_score(code) -> 0.0〜100.0（中立=50）
#
# ⚠ このモジュールは発注経路（entry_controller / exit_handler）には
#   一切触れない。あくまで「材料をDBに溜め、銘柄別スコアを返す」までを担う。
#   売買への配線（ai_final_entry_check への注入）は次フェーズで行う。
#
# スコアロジック(B)は stdlib のみ（math/datetime）で完結させ、
# DB を使わない score_from_events() を単体テスト可能にしている。
# ============================================================

from __future__ import annotations

import os
import math
import time as _time
import logging
import configparser
import datetime as dt
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 専用ローカル DB（ホットな NAS summary DB には相乗りしない）
#   settings.ini [news] news_db_path で上書き可。既定は LOCALAPPDATA。
# ------------------------------------------------------------
_news_engine_cache = None
_NewsSession = None


def _news_db_path() -> Path:
    conf = configparser.ConfigParser()
    try:
        conf.read("settings.ini", encoding="utf-8")
        override = conf.get("news", "news_db_path", fallback="").strip()
    except Exception:
        override = ""
    if override:
        return Path(override)
    base = os.getenv("LOCALAPPDATA") or os.getenv("TEMP") or r"C:\Temp"
    return Path(base) / "autostock" / "news" / "news.db"


def _news_session():
    """ニュース専用 DB のセッション（遅延生成・キャッシュ）。"""
    global _news_engine_cache, _NewsSession
    if _NewsSession is not None:
        return _NewsSession()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base_news

    path = _news_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _news_engine_cache = create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    Base_news.metadata.create_all(_news_engine_cache)
    _NewsSession = sessionmaker(bind=_news_engine_cache)
    logger.info("[NEWS] news DB ready path=%s", path)
    return _NewsSession()

# ------------------------------------------------------------
# 材料キーワードの重み（title + keywords + summary を対象に部分一致で加点）
#   正 = 好材料（スコアを上げる） / 負 = 悪材料（下げる）
#   方向が読めないもの（「決算」「業績予想の修正」単体）はほぼ中立。
# ------------------------------------------------------------
KEYWORD_WEIGHTS: dict[str, float] = {
    # --- 強い好材料 ---
    "上方修正": +25.0,
    "過去最高": +18.0,
    "増配": +18.0,
    "復配": +18.0,
    "自己株式取得": +15.0,
    "自社株買い": +15.0,
    "株式分割": +10.0,
    "業務提携": +10.0,
    "資本提携": +10.0,
    "大型受注": +10.0,
    "受注": +6.0,
    "黒字転換": +15.0,
    "配当": +3.0,
    # --- 強い悪材料 ---
    "下方修正": -25.0,
    "減配": -18.0,
    "無配": -20.0,
    "特別損失": -12.0,
    "減損": -12.0,
    "希薄化": -15.0,
    "公募増資": -15.0,
    "第三者割当": -10.0,
    "赤字転落": -18.0,
    "業績予想の修正": +0.0,   # 方向は本文依存のため単体では中立
    "決算": +0.0,
}

# news_monitors.WATCH_WORDS より広め。save 時の keywords 抽出に使う。
WATCH_WORDS: tuple[str, ...] = tuple(KEYWORD_WEIGHTS.keys())

NEUTRAL_SCORE = 50.0
# 時間減衰の半減期（時間）。この時間が経つと材料の効きが半分になる。
HALFLIFE_HOURS = 12.0


# ============================================================
# B) スコアリング（DB非依存・単体テスト可能）
# ============================================================

def extract_keyword_weight(text: str) -> float:
    """1件分のテキストに含まれる材料ワードの重み合計（減衰前）。"""
    if not text:
        return 0.0
    total = 0.0
    for word, w in KEYWORD_WEIGHTS.items():
        if w == 0.0:
            continue
        if word in text:
            total += w
    return total


def extract_keywords(text: str) -> list[str]:
    """テキストから検出した材料ワード一覧（keywords 列保存用）。"""
    if not text:
        return []
    return [w for w in WATCH_WORDS if w in text]


def _as_datetime(value) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(str(value), fmt)
        except Exception:
            continue
    return None


def score_from_events(
    events: Iterable[dict],
    now: Optional[dt.datetime] = None,
) -> float:
    """
    材料イベント列から 0〜100 のスコアを返す（中立 50）。

    events: [{"text": "<title+keywords+summary>", "time": <datetime|str|None>}, ...]
    新しい材料ほど重く効くよう、半減期 HALFLIFE_HOURS で時間減衰する。
    """
    now = now or dt.datetime.now()
    decay_lambda = math.log(2.0) / HALFLIFE_HOURS

    adjustment = 0.0
    for ev in events:
        raw = extract_keyword_weight(ev.get("text", ""))
        if raw == 0.0:
            continue

        t = _as_datetime(ev.get("time"))
        if t is None:
            factor = 1.0
        else:
            age_h = max(0.0, (now - t).total_seconds() / 3600.0)
            factor = math.exp(-decay_lambda * age_h)

        adjustment += raw * factor

    score = NEUTRAL_SCORE + adjustment
    return max(0.0, min(100.0, score))


# ============================================================
# A) 保存の土台（DB依存）
# ============================================================

def ensure_news_table() -> None:
    """専用ローカル news DB に news テーブル群を作成する（無ければ）。"""
    # _news_session() 内で Base_news.metadata.create_all 済み。
    _news_session().close()
    logger.info("[NEWS] news テーブル確認/作成 OK")


def save_disclosure(
    source: str,
    title: str,
    url: Optional[str] = None,
    code: Optional[str] = None,
    time_val=None,
    summary: Optional[str] = None,
    intro: Optional[str] = None,
) -> bool:
    """
    1件のニュース/開示を保存する。(source, url) が既存なら重複スキップ。
    戻り値: 新規保存したら True。
    """
    from database.models import NewsDisclosure

    dt_time = _as_datetime(time_val) or dt.datetime.now()
    text = f"{title} {summary or ''} {intro or ''}"
    kw = extract_keywords(text)

    session = _news_session()
    try:
        if url:
            exists = (
                session.query(NewsDisclosure)
                .filter(NewsDisclosure.source == source, NewsDisclosure.url == url)
                .first()
            )
            if exists:
                return False

        session.add(
            NewsDisclosure(
                source=source,
                code=code,
                time=dt_time,
                title=title,
                summary=summary,
                intro=intro,
                keywords="、".join(kw) if kw else None,
                url=url,
            )
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("[NEWS] 保存失敗 source=%s title=%s", source, title)
        return False
    finally:
        session.close()


def compute_news_score(
    code: str,
    now: Optional[dt.datetime] = None,
    lookback_hours: float = 48.0,
) -> float:
    """
    銘柄 code の直近 lookback_hours の材料から news_score(0〜100) を算出。
    材料が無ければ中立 50.0。
    """
    from database.models import NewsDisclosure

    now = now or dt.datetime.now()
    since = now - dt.timedelta(hours=lookback_hours)

    session = _news_session()
    try:
        # as-of: now 時点で既知の材料だけを使う（now より後のニュースは不使用）。
        # ライブ(now=現在)では無害、シャドー検証(now=entry時刻)で未来リークを防ぐ。
        rows = (
            session.query(NewsDisclosure)
            .filter(NewsDisclosure.code == str(code))
            .filter(NewsDisclosure.time >= since)
            .filter(NewsDisclosure.time <= now)
            .all()
        )
    except Exception:
        logger.exception("[NEWS] score 取得失敗 code=%s", code)
        return NEUTRAL_SCORE
    finally:
        session.close()

    events = [
        {
            "text": f"{r.title or ''} {r.keywords or ''} {r.summary or ''}",
            "time": r.time,
        }
        for r in rows
    ]
    return score_from_events(events, now=now)


# ============================================================
# TDnet 適時開示の収集（銘柄コードを確実に取れる公式フィード）
#   一覧: https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html
#   行  : [時刻HH:MM, コード5桁, 会社名, 表題, ...] ＋ PDF(相対リンク)
# ============================================================

TDNET_BASE = "https://www.release.tdnet.info/inbs/"
_ca_installed = False


def _install_ca() -> None:
    """certifi+Windowsストア統合CAバンドルを有効化（SSL検証用・verify=Falseは使わない）。"""
    global _ca_installed
    if _ca_installed:
        return
    try:
        import sys
        if "scripts" not in sys.path:
            sys.path.insert(0, "scripts")
        import yahoo_ca_bundle as _ca
        _ca.install()
    except Exception:
        logger.warning("[NEWS] CAバンドルのinstallに失敗（SSL検証が通らない可能性）", exc_info=True)
    _ca_installed = True


def _parse_tdnet_rows(html: str, ymd: str) -> list[tuple[dt.datetime, str, str, str]]:
    """TDnet一覧HTMLから [(datetime, code4, title, pdf_url), ...] を返す。"""
    import re
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    y, m, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
    out: list[tuple[dt.datetime, str, str, str]] = []

    for tr in soup.select("table tr"):
        tds = tr.select("td")
        if len(tds) < 4:
            continue
        time_txt = tds[0].get_text(strip=True)
        mobj = re.match(r"(\d{1,2}):(\d{2})", time_txt)
        if not mobj:
            continue  # ヘッダ等
        code_raw = tds[1].get_text(strip=True)
        code4 = code_raw[:4] if code_raw else None
        title = tds[3].get_text(strip=True)

        pdf_url = ""
        a = tr.select_one('a[href$=".pdf"]') or tr.select_one("a")
        if a and a.get("href"):
            href = a["href"]
            pdf_url = href if href.startswith("http") else TDNET_BASE + href

        when = dt.datetime(y, m, d, int(mobj.group(1)), int(mobj.group(2)))
        out.append((when, code4, title, pdf_url))
    return out


def fetch_tdnet_disclosures(ymd: Optional[str] = None, max_pages: int = 20) -> list[tuple[dt.datetime, str, str, str]]:
    """
    指定日(JST, 既定=今日)のTDnet適時開示を全ページ取得。
    戻り値: [(datetime, code4, title, pdf_url), ...]
    """
    import requests

    _install_ca()
    if ymd is None:
        ymd = dt.datetime.now().strftime("%Y%m%d")

    results: list[tuple[dt.datetime, str, str, str]] = []
    for page in range(1, max_pages + 1):
        url = f"{TDNET_BASE}I_list_{page:03d}_{ymd}.html"
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.encoding = "utf-8"
        except Exception:
            logger.warning("[NEWS] TDnet取得失敗 page=%s", page, exc_info=True)
            break
        if r.status_code != 200:
            break
        rows = _parse_tdnet_rows(r.text, ymd)
        if not rows:
            break  # これ以上のページは無い（データ無しページ）
        results.extend(rows)
        if len(rows) < 100:
            break  # 最終ページ（1ページ=最大約100件）
    return results


def collect_jpx_once(ymd: Optional[str] = None) -> int:
    """TDnet開示を1回取得して保存。戻り値: 新規保存件数。"""
    ensure_news_table()

    saved = 0
    try:
        for when, code4, title, pdf_url in fetch_tdnet_disclosures(ymd):
            if save_disclosure("TDnet", title, url=pdf_url or None, code=code4, time_val=when):
                saved += 1
    except Exception:
        logger.exception("[NEWS] TDnet 収集失敗")
    logger.info("[NEWS] TDnet collect done saved=%s", saved)
    return saved


# 後方互換エイリアス
collect_tdnet_once = collect_jpx_once


def run_loop(interval_sec: int = 120) -> None:
    """常駐: interval_sec ごとに TDnet 開示を収集し続ける。"""
    ensure_news_table()
    logger.info("[NEWS] collect loop start interval=%ss", interval_sec)
    while True:
        try:
            collect_jpx_once()
        except Exception:
            logger.exception("[NEWS] loop iteration error")
        _time.sleep(interval_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        run_loop()
    else:
        n = collect_jpx_once()
        print(f"saved={n}")
