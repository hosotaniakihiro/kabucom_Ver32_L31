# ============================================================
# trading/logger/format_utils.py
# Ver1.1-PRODUCTION-SAFE-LOGGER-UTILS
# ------------------------------------------------------------
# ✔ DataFrame / list / dict 安全変換
# ✔ NaN / inf 完全防御
# ✔ 数値安全変換
# ✔ score整数表示
# ✔ 価格/出来高 小数点1位
# ✔ symbolname / name / symbol_name_map 自動解決
# ✔ close列互換
# ✔ volume列互換
# ✔ rsi列互換
# ✔ logger安全化
# ✔ 副作用ゼロ
# ✔ 例外完全防御
# ============================================================

from __future__ import annotations

import pandas as pd
import math

from global_state import global_data


# ============================================================
# DataFrame安全コピー
# ============================================================

def safe_copy(df):

    if df is None:
        return pd.DataFrame()

    if isinstance(df, list):

        if not df:
            return pd.DataFrame()

        return pd.DataFrame(df)

    if isinstance(df, dict):
        return pd.DataFrame([df])

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    return df.copy()


# ============================================================
# 数値Series安全化
# ============================================================

def safe_numeric(series):

    try:

        s = pd.to_numeric(series, errors="coerce")

        s = s.replace([float("inf"), float("-inf")], 0)

        return s.fillna(0)

    except Exception:

        return pd.Series(0)


# ============================================================
# float安全化
# ============================================================

def safe_float(v):

    try:

        if v is None:
            return None

        if isinstance(v, float):

            if math.isnan(v):
                return None

            if math.isinf(v):
                return None

        return float(v)

    except Exception:

        return None


# ============================================================
# 表示フォーマット
# ============================================================

def fmt_score(v):

    try:
        return int(round(float(v)))
    except Exception:
        return 0


def fmt_float(v):

    try:

        if v is None:
            return "-"

        return f"{float(v):.1f}"

    except Exception:

        return "-"


def fmt_int(v):

    try:

        if v is None:
            return "0"

        return f"{int(v):,}"

    except Exception:

        return "0"


# ============================================================
# symbol name 解決
# ============================================================

def safe_symbolname(row):

    try:

        name = getattr(row, "symbolname", None)

        if name is None or str(name).strip() == "":
            name = getattr(row, "name", None)

        if name is None or str(name).strip() == "":

            symbol = str(getattr(row, "symbol", "不明"))

            return global_data.symbol_name_map.get(symbol, symbol)

        s = str(name).strip()

        if s.lower() in ("nan", "none", ""):

            symbol = str(getattr(row, "symbol", "不明"))

            return global_data.symbol_name_map.get(symbol, symbol)

        return s

    except Exception:

        symbol = str(getattr(row, "symbol", "不明"))

        return global_data.symbol_name_map.get(symbol, symbol)


# ============================================================
# 列互換吸収
# ============================================================

def safe_close(row):

    for col in ("close", "close_price", "c"):

        v = getattr(row, col, None)

        if v is not None and pd.notna(v):
            return safe_float(v)

    return None


def safe_volume(row):

    for col in ("volume", "vol"):

        v = getattr(row, col, None)

        if v is not None and pd.notna(v):
            return safe_float(v)

    return None


def safe_rsi(row):

    for col in ("rsi", "rsi14", "rsi_14", "RSI"):

        v = getattr(row, col, None)

        if v is not None and pd.notna(v):
            return safe_float(v)

    return None


# ============================================================
# 最新bar取得
# ============================================================

def latest_per_symbol(df):

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    df = df.copy()

    df["symbol"] = df["symbol"].astype(str)

    if "datetime" in df.columns:

        try:

            df = df.sort_values("datetime")

            return df.groupby("symbol", as_index=False).tail(1)

        except Exception:

            pass

    return df.drop_duplicates("symbol", keep="last")