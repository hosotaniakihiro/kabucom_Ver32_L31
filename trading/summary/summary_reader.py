# ============================================================
# trading/summary/summary_reader.py
# ------------------------------------------------------------
# サマリー参照専用（表示・ログ・通知用）
# ------------------------------------------------------------
# ✔ DB更新・計算・判断は一切しない
# ✔ global_data の merged_summary を読むだけ
# ✔ 最新時刻のサマリー抽出
# ✔ NaN / 欠損耐性
# ✔ interval 型崩れ完全防御（DataFrame / str / None / bool）
# ============================================================

import logging
import pandas as pd
from typing import Optional

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# interval 正規化（最重要・完全防御）
# ============================================================
def _normalize_interval(interval) -> Optional[int]:
    """
    interval 正規化ルール

    OK:
        1 / 3 / 5
        "1min" / "3min" / "5min"

    NG:
        bool
        DataFrame / list / dict
        None
        その他文字列
    """

    # bool は int のサブクラスなので最優先で除外
    if isinstance(interval, bool):
        return None

    # "1min" 等の文字列対応
    if isinstance(interval, str):
        interval = interval.strip().lower()
        if interval.endswith("min"):
            try:
                interval = int(interval.replace("min", ""))
            except Exception:
                return None
        else:
            return None

    # int 以外は即拒否
    if not isinstance(interval, int):
        return None

    if interval not in (1, 3, 5):
        return None

    return interval


# ============================================================
def get_latest_summary_for_display(
    interval,
    *,
    columns: Optional[list[str]] = None,
) -> pd.DataFrame:
    """
    表示専用：最新サマリーを取得

    Args:
        interval:
            1 / 3 / 5 / "1min" / "3min" / "5min"
        columns:
            表示用に抽出したい列（None = 全列）

    Returns:
        pd.DataFrame:
            最新 datetime の行のみを含む DataFrame
    """

    # --------------------------------------------------------
    # interval 正規化（完全防御）
    # --------------------------------------------------------
    interval = _normalize_interval(interval)
    if interval is None:
        return pd.DataFrame()

    # --------------------------------------------------------
    # merged_summary 取得（存在確認付き）
    # --------------------------------------------------------
    try:
        getter = getattr(global_data, "get_merged_summary", None)
        if not callable(getter):
            logger.error("❌ [SUMMARY_READER] get_merged_summary not callable")
            return pd.DataFrame()

        df = getter(interval)

    except Exception:
        logger.exception(
            "❌ [SUMMARY_READER] get_merged_summary crashed interval=%s",
            interval,
        )
        return pd.DataFrame()

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # datetime 正規化
    # --------------------------------------------------------
    if "datetime" not in df.columns:
        logger.error(
            "❌ [SUMMARY_READER] datetime column missing interval=%s",
            interval,
        )
        return pd.DataFrame()

    try:
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"])
    except Exception:
        logger.exception(
            "❌ [SUMMARY_READER] datetime normalize failed interval=%s",
            interval,
        )
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # 最新時刻のみ抽出
    # --------------------------------------------------------
    try:
        latest_dt = df["datetime"].max()
        df_latest = df[df["datetime"] == latest_dt]
    except Exception:
        logger.exception(
            "❌ [SUMMARY_READER] latest datetime extract failed interval=%s",
            interval,
        )
        return pd.DataFrame()

    if df_latest.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # 列抽出（表示用）
    # --------------------------------------------------------
    if columns:
        cols = [c for c in columns if c in df_latest.columns]
        if cols:
            df_latest = df_latest[cols]

    return df_latest.reset_index(drop=True)


# ============================================================
def get_latest_summary_row_for_symbol(
    symbol: str,
    interval,
) -> Optional[dict]:
    """
    デバッグ・表示用：
    特定銘柄の最新サマリー1行を dict で返す
    """

    interval = _normalize_interval(interval)
    if interval is None:
        return None

    df = get_latest_summary_for_display(interval)
    if df.empty or "symbol" not in df.columns:
        return None

    try:
        df_sym = df[df["symbol"].astype(str) == str(symbol)]
    except Exception:
        return None

    if df_sym.empty:
        return None

    return df_sym.iloc[0].to_dict()


# ============================================================
def get_latest_summary_symbols(
    interval,
) -> list[str]:
    """
    表示用：
    最新サマリーに含まれる銘柄一覧を返す
    """

    interval = _normalize_interval(interval)
    if interval is None:
        return []

    df = get_latest_summary_for_display(interval)
    if df.empty or "symbol" not in df.columns:
        return []

    try:
        return sorted(
            df["symbol"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
    except Exception:
        return []
