# ============================================================
# summary_processor.py（Ver25-FINAL-STABLE-DATETIME-SYNC）
# ------------------------------------------------------------
# ・PUSH / Yahoo / DB の全形式を完全吸収
# ・内部表現は open_price / high_price / low_price / close_price / volume / trading_value
# ・resample / indicator / scoring / saver / initial rebuild の唯一前提
# ・datetime / date / time の役割を完全統一
# ============================================================

import pandas as pd
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 🔧 共通：datetime 正規化（JST naive）
# ============================================================
def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if "datetime" not in df.columns:
        raise ValueError("normalize_datetime: datetime missing")

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])

    if df.empty:
        return df

    # tz-aware → JST → naive
    try:
        sample = df["datetime"].iloc[0]
        if sample.tzinfo is not None:
            df["datetime"] = (
                df["datetime"]
                .dt.tz_convert("Asia/Tokyo")
                .dt.tz_localize(None)
            )
    except Exception:
        pass

    return df


# ============================================================
# 🔧 1分足 正規化（全入口対応・最重要）
# ============================================================
def normalize_1min(df: pd.DataFrame, src="unknown") -> pd.DataFrame:
    """
    最終的に以下へ完全統一：
    symbol / datetime /
    open_price / high_price / low_price / close_price /
    volume / trading_value
    """

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    # --- 必須 ---
    if "symbol" not in df.columns:
        raise ValueError(f"{src}: symbol missing")
    if "datetime" not in df.columns:
        raise ValueError(f"{src}: datetime missing")

    # 型保証
    df["symbol"] = df["symbol"].astype(str)

    # --- close_price ---
    if "close_price" not in df.columns:
        if "close" in df.columns:
            df["close_price"] = df["close"]
        elif "price" in df.columns:
            df["close_price"] = df["price"]
        else:
            raise ValueError(f"{src}: close_price missing")

    # --- open_price ---
    if "open_price" not in df.columns:
        if "open" in df.columns:
            df["open_price"] = df["open"]
        else:
            df["open_price"] = df["close_price"]

    # --- high_price ---
    if "high_price" not in df.columns:
        if "high" in df.columns:
            df["high_price"] = df["high"]
        else:
            df["high_price"] = df["close_price"]

    # --- low_price ---
    if "low_price" not in df.columns:
        if "low" in df.columns:
            df["low_price"] = df["low"]
        else:
            df["low_price"] = df["close_price"]

    # --- 数値型 ---
    for c in ["open_price", "high_price", "low_price", "close_price"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(
        subset=["open_price", "high_price", "low_price", "close_price"]
    )
    if df.empty:
        return pd.DataFrame()

    # --- volume ---
    if "volume" not in df.columns:
        df["volume"] = 0
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    # --- trading_value ---
    if "trading_value" not in df.columns:
        if "turnover" in df.columns:
            df["trading_value"] = pd.to_numeric(
                df["turnover"], errors="coerce"
            ).fillna(0)
        else:
            df["trading_value"] = df["volume"] * df["close_price"]

    # --- datetime 正規化 ---
    df = _normalize_datetime(df)

    if df.empty:
        return df

    # --- 1分足確定 ---
    df["datetime"] = df["datetime"].dt.floor("1min")

    return (
        df[
            [
                "symbol",
                "datetime",
                "open_price",
                "high_price",
                "low_price",
                "close_price",
                "volume",
                "trading_value",
            ]
        ]
        .sort_values(["symbol", "datetime"])
        .reset_index(drop=True)
    )


# ============================================================
# 🔥 1min → Nmin リサンプル
# ============================================================
def resample_1min_to(df_1min: pd.DataFrame, interval: int):

    if df_1min is None or df_1min.empty:
        return pd.DataFrame()

    df = df_1min.copy()

    REQUIRED = ["symbol", "datetime"]
    for c in REQUIRED:
        if c not in df.columns:
            raise ValueError(f"resample_1min_to: missing {c}")

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    if df.empty:
        return pd.DataFrame()

    out = []

    # 🔴 BUY / SELL 判定フラグ（保持）
    FLAG_COLS = [
        "dir_up", "dir_down",
        "ma_alignment", "ma_alignment_down",
        "macd_gc", "macd_dc",
        "rsi_ok", "rsi_falling",
        "rci_rising", "rci_falling",
        "vwap_break", "vwap_fail",
        "volume_spike", "volume_drop",
    ]

    for symbol, g in df.groupby("symbol"):
        g = g.sort_values("datetime").set_index("datetime")

        agg = {
            "open_price": "first",
            "high_price": "max",
            "low_price": "min",
            "close_price": "last",
            "volume": "sum",
            "trading_value": "sum",
        }

        for c in FLAG_COLS:
            if c in g.columns:
                agg[c] = "max"

        r = g.resample(
            f"{interval}min",
            origin="start_day",
        ).agg(agg)

        r = r.dropna(
            subset=[
                "open_price",
                "high_price",
                "low_price",
                "close_price",
            ]
        )

        if r.empty:
            continue

        r["symbol"] = symbol
        r["datetime"] = r.index

        # summary 互換 time_range
        start = r.index
        end = r.index + pd.Timedelta(minutes=interval)
        r["time_range"] = (
            start.strftime("%H:%M")
            + " - "
            + end.strftime("%H:%M")
        )

        out.append(r.reset_index(drop=True))

    if not out:
        return pd.DataFrame()

    return pd.concat(out, ignore_index=True)


# ============================================================
# 🔥 3min / 5min 一括生成
# ============================================================
def generate_3m_5m(df_1min: pd.DataFrame):
    return (
        resample_1min_to(df_1min, 3),
        resample_1min_to(df_1min, 5),
    )
