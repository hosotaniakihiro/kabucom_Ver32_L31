# ============================================================
# symbol_loader.py（Ver14.17 SAFE-FALLBACK + ETF-BLOCK版）
# ------------------------------------------------------------
# - DB(symbol_flags.db)から銘柄リストをロード
# - ETF / レバETF（is_etf=1）は監視銘柄に一切入れない
# - buy/sell フラグが0件でも必ず銘柄を返す（SAFE）
# - ACTIVE/LIGHT=0 問題を完全防止
# - config.paths に完全統一（settings.ini 廃止）
# ============================================================

import argparse
import pandas as pd
import logging
import sqlite3

from config.paths import get_path
from tools.sync_excel_to_db import sync_excel_to_db
from utils_common import normalize_symbol

logger = logging.getLogger(__name__)


# ============================================================
# DBから銘柄リストをロード（SAFE + ETF BLOCK）
# ============================================================
def load_symbol_flags_df() -> pd.DataFrame:
    """
    symbol_flags.db から銘柄リストをロード

    ✔ ETF / レバETF（is_etf=1）は完全除外
    ✔ buy/sell フラグ互換維持
    ✔ フラグ有無に関係なく ETF除外後の全銘柄を返す（★母集団拡張）
    ✔ symbols_all が空になる事故を完全防止
    ✔ ACTIVE=100 を実現可能にする
    """

    db_path = get_path("symbol_flags_db")
    cols = ["symbol", "symbolname", "buy_target", "sell_target"]

    if not db_path.exists():
        logger.error(f"❌ symbol_flags.db が存在しません: {db_path}")
        return pd.DataFrame(columns=cols)

    try:
        with sqlite3.connect(db_path) as conn:
            df_all = pd.read_sql("SELECT * FROM symbol_flags", conn)

        if df_all.empty:
            logger.error("❌ symbol_flags テーブル自体が空です")
            return pd.DataFrame(columns=cols)

        # ----------------------------------------------------
        # 正規化
        # ----------------------------------------------------
        df_all["symbol"] = (
            df_all["symbol"]
            .astype(str)
            .apply(normalize_symbol)
        )

        df_all["symbolname"] = (
            df_all.get("symbolname", "")
            .fillna("")
            .astype(str)
        )

        # ----------------------------------------------------
        # is_etf 無い場合は 0 扱い（後方互換）
        # ----------------------------------------------------
        if "is_etf" not in df_all.columns:
            df_all["is_etf"] = 0

        # ----------------------------------------------------
        # ★ ETF / レバETF 完全除外
        # ----------------------------------------------------
        before = len(df_all)
        df_all = df_all[df_all["is_etf"] == 0]
        df_all = apply_professional_symbol_filter(df_all)
        after = len(df_all)

        if after < before:
            logger.info(
                f"🚫 ETF除外: {before - after}銘柄を監視対象から除外"
            )

        if df_all.empty:
            logger.error(
                "❌ ETF除外後に銘柄が0件になりました（設定を確認してください）"
            )
            return pd.DataFrame(columns=cols)

        # ----------------------------------------------------
        # 旧カラム互換（buy / sell）
        # ----------------------------------------------------
        if "buy" in df_all.columns and "sell" in df_all.columns:
            df_all.rename(
                columns={
                    "buy": "buy_target",
                    "sell": "sell_target",
                },
                inplace=True,
            )

        # ----------------------------------------------------
        # フラグ列が無い場合は作成（完全互換）
        # ----------------------------------------------------
        if "buy_target" not in df_all.columns:
            df_all["buy_target"] = 1

        if "sell_target" not in df_all.columns:
            df_all["sell_target"] = 1

        # ----------------------------------------------------
        # ★ 母集団は ETF除外後の全銘柄（ここが重要）
        # ----------------------------------------------------
        df = df_all.copy()

        logger.info(
            f"✅ symbol_flags ロード完了（ETF除外後）: {len(df)}件"
        )

        return df[cols].reset_index(drop=True)

    except Exception as e:
        logger.error(
            f"❌ symbol_flags 読込失敗: {e}",
            exc_info=True,
        )
        return pd.DataFrame(columns=cols)
# ============================================================
# プロ用銘柄フィルタ
# ============================================================
def apply_professional_symbol_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    プロ用銘柄フィルタ

    除外対象
    - ETF
    - ETN
    - REIT
    - PRO Market
    - 指数連動商品
    - 優先株
    - 整理銘柄
    - 監理銘柄

    目的
    - ranking精度向上
    - AI誤判定防止
    - ATS安定化
    """

    before = len(df)

    # ----------------------------------------------------
    # market_type フィルタ
    # ----------------------------------------------------
    if "market_type" in df.columns:

        exclude_types = [
            "ETF",
            "ETN",
            "REIT",
            "PRO Market",
        ]

        df = df[~df["market_type"].isin(exclude_types)]

    # ----------------------------------------------------
    # symbolname 文字列フィルタ
    # ----------------------------------------------------
    if "symbolname" in df.columns:

        exclude_keywords = [
            "ＥＴＦ",
            "ETF",
            "ＥＴＮ",
            "ETN",
            "ＲＥＩＴ",
            "REIT",
            "指数",
            "連動",
            "優先",
            "整理",
            "監理",
            "PRO",
        ]

        pattern = "|".join(exclude_keywords)

        df = df[~df["symbolname"].str.contains(pattern, na=False)]

    after = len(df)

    removed = before - after

    if removed > 0:
        logger.info(
            f"🚫 プロ用銘柄フィルタ: {removed}銘柄除外"
        )

    return df
# ============================================================
# メイン：Excel → DB 同期モード
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Excel銘柄リストをDB(symbol_flags.db)に登録"
    )
    parser.add_argument(
        "--excel",
        type=str,
        default=None,
        help="銘柄リストExcelファイルのパス（指定時のみDBを更新）",
    )
    args = parser.parse_args()

    if args.excel:
        print(f"📊 Excel → DB 同期開始: {args.excel}")
        sync_excel_to_db()
        print("✅ Excel → DB 登録完了")
    else:
        df = load_symbol_flags_df()
        if df.empty:
            print("⚠️ 銘柄リストが空です")
        else:
            print(f"✅ 銘柄ロード完了（ETF除外）: {len(df)}件")
            print(df.head(10))


if __name__ == "__main__":
    main()
