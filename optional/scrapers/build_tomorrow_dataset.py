import logging
import pandas as pd
from typing import List
from datetime import datetime

# ★ 正しい依存はこれだけ
from optional.scrapers.kabutan_article_parser import parse_tomorrow_article

logger = logging.getLogger(__name__)


# ============================================================
# 共通ユーティリティ
# ============================================================

def _normalize_symbol(symbol: str) -> str:
    if not symbol:
        return ""
    s = str(symbol).strip()
    s = s.replace(".T", "")
    return s.zfill(4)


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    必須列を保証
    """
    required_cols = [
        "symbol",
        "symbolname",
        "headline",
        "date",
        "category",
        "source",
    ]

    for col in required_cols:
        if col not in df.columns:
            df[col] = None

    return df


def _normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    列正規化・symbol補正
    """
    df = _ensure_columns(df)

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].apply(_normalize_symbol)

    # symbolnameが空ならsymbolで補完
    if "symbolname" in df.columns:
        df["symbolname"] = df.apply(
            lambda r: r["symbolname"]
            if pd.notna(r["symbolname"]) and r["symbolname"]
            else r["symbol"],
            axis=1,
        )

    return df


# ============================================================
# メインAPI
# ============================================================

def build_tomorrow_dataset(
    urls: List[str],
) -> pd.DataFrame:
    """
    株探「明日の好悪材料」統合データを作成
    urls は外部から渡される
    """

    logger.info("🚀 build_tomorrow_dataset start")

    # --------------------------------------------------------
    # 入力チェック
    # --------------------------------------------------------
    if not urls or not isinstance(urls, list):
        logger.warning("⚠ no tomorrow article urls")
        return pd.DataFrame()

    dfs: List[pd.DataFrame] = []

    # --------------------------------------------------------
    # 各記事パース
    # --------------------------------------------------------
    for i, url in enumerate(urls, 1):

        if not url or not isinstance(url, str):
            logger.warning("⚠ invalid url skipped: %s", url)
            continue

        try:
            df = parse_tomorrow_article(url)

            if df is None or not isinstance(df, pd.DataFrame):
                logger.warning("⚠ parse returned invalid type: %s", url)
                continue

            if df.empty:
                logger.warning("⚠ empty article skipped: %s", url)
                continue

            df = _normalize_dataframe(df)

            dfs.append(df)

            logger.info(
                "✅ parsed [%d/%d]: %s rows=%d",
                i,
                len(urls),
                url,
                len(df),
            )

        except Exception:
            logger.exception("❌ parse failed: %s", url)

    # --------------------------------------------------------
    # 全部空なら終了
    # --------------------------------------------------------
    if not dfs:
        logger.warning("⚠ no valid tomorrow articles parsed")
        return pd.DataFrame()

    # --------------------------------------------------------
    # 結合
    # --------------------------------------------------------
    out = pd.concat(dfs, ignore_index=True)

    # --------------------------------------------------------
    # 重複排除（symbol×headline×date）
    # --------------------------------------------------------
    subset_cols = [c for c in ["symbol", "headline", "date"] if c in out.columns]

    if subset_cols:
        before = len(out)
        out = out.drop_duplicates(subset=subset_cols, keep="first")
        after = len(out)

        if before != after:
            logger.info("♻ duplicates removed: %d", before - after)

    # --------------------------------------------------------
    # 最終ログ
    # --------------------------------------------------------
    logger.info(
        "📊 tomorrow dataset built: rows=%d symbols=%s",
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else "N/A",
    )

    return out


# ============================================================
# 単体テスト
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_urls = [
        "https://kabutan.jp/news/marketnews/?b=n202602120123",
    ]

    df = build_tomorrow_dataset(test_urls)

    print(df.head())
    print("TOTAL:", len(df))