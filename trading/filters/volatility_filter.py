# ==========================================================
# File   : trading/filters/volatility_filter.py
# Version: PRODUCTION-STABLE-VOLATILITY-FILTER-V2-CALL-COMPAT
# ----------------------------------------------------------
# ✔ ENTRY前専用・副作用ゼロ
# ✔ ATR(1m) + 直近5分値幅 で「動く銘柄だけ」通す
# ✔ SUMMARY / RANKING 共通
# ✔ 説明可能（数値・閾値・理由を返す）
# ✔ 旧API: atr_1m_filter(df_1m=..., symbol=...) -> (ng, detail)
# ✔ 新/誤用互換: atr_1m_filter(entry_row) -> bool allow
# ✔ entry_controller.py の TypeError を防止
# ==========================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from utils_common import safe_float

logger = logging.getLogger(__name__)


# ==========================================================
# helpers
# ==========================================================

def _normalize_symbol(value: Any) -> str:
    try:
        s = str(value or "").strip()
        if s.endswith(".0"):
            ss = s[:-2]
            if ss.isdigit():
                return ss
        return s
    except Exception:
        return ""


def _first(row: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if isinstance(row, pd.Series):
            return row.to_dict()
        if hasattr(row, "to_dict"):
            v = row.to_dict()
            if isinstance(v, dict):
                return dict(v)
        return {}
    except Exception:
        return {}


def _is_entry_row_call(entry_row: Any, df: Any, symbol: Any) -> bool:
    """
    entry_controller.py からの
        atr_1m_filter(entry_row)
        range_5m_filter(entry_row)
    呼び出しを判定する。
    """
    return entry_row is not None and df is None and symbol is None


def _get_global_summary_df(interval: int) -> pd.DataFrame:
    """
    entry_row 互換呼び出し時に、可能なら global_data から summary df を拾う。
    取得できなければ empty DataFrame。
    """
    try:
        from global_state import global_data
    except Exception:
        return pd.DataFrame()

    attr_names = []
    if int(interval) == 1:
        attr_names = ["summary_1m_df", "df_1m_summary", "push_summary_1m_df"]
    elif int(interval) == 5:
        attr_names = ["summary_5m_df", "df_5m_summary", "push_summary_5m_df"]
    else:
        attr_names = [f"summary_{interval}m_df", f"df_{interval}m_summary"]

    for name in attr_names:
        try:
            df = getattr(global_data, name, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        except Exception:
            pass

    getter_candidates = [
        "get_push_merged_summary",
        "get_merged_summary",
        "get_summary_cache",
        "get_summary",
        "get_multi_summary",
    ]

    for getter_name in getter_candidates:
        try:
            getter = getattr(global_data, getter_name, None)
            if callable(getter):
                df = getter(int(interval))
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df
        except Exception:
            pass

    return pd.DataFrame()


def _ensure_ohlc_aliases(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        x = df.copy()
        alias_map = {
            "high_price": ("high_price", "high", "High"),
            "low_price": ("low_price", "low", "Low"),
            "close_price": ("close_price", "close", "Close", "price", "current_price"),
        }

        for dst, candidates in alias_map.items():
            if dst in x.columns:
                continue
            for src in candidates:
                if src in x.columns:
                    x[dst] = x[src]
                    break

        return x
    except Exception:
        return df


# ==========================================================
# entry_row compatibility mode
# ==========================================================

def _atr_1m_filter_from_entry_row(entry_row: Any, min_ratio: float = 0.0025) -> bool:
    """
    entry_controller.py 互換用。

    Returns
    -------
    bool
        True  = ENTRY許可
        False = ENTRY拒否

    重要:
      entry_row にATR情報が無いだけで全落ちさせると、
      SUMMARY_AI の発注経路が止まる。
      そのため、entry_rowから判定不能な場合は global_data の1分足を試し、
      それでも無ければ fail-open で通す。
    """
    row = _row_to_dict(entry_row)
    symbol = _normalize_symbol(
        _first(row, ("symbol", "Symbol", "銘柄コード", "code", "stock_code"), "")
    )

    price = safe_float(
        _first(row, ("close_price", "close", "price", "current_price"), 0),
        0,
    )

    atr = safe_float(
        _first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0),
        0,
    )

    # entry_rowにATRがあればそれで判定
    if atr > 0 and price > 0:
        ratio = atr / price
        ok = ratio >= float(min_ratio)
        logger.info(
            "[VOL FILTER] ATR entry_row symbol=%s ok=%s atr=%.6f price=%.3f ratio=%.6f min_ratio=%.6f",
            symbol,
            ok,
            atr,
            price,
            ratio,
            min_ratio,
        )
        return bool(ok)

    # ATRが無ければ global_data の1分足で旧ロジックを試す
    if symbol:
        df_1m = _get_global_summary_df(1)
        if isinstance(df_1m, pd.DataFrame) and not df_1m.empty:
            try:
                ng, detail = atr_1m_filter(df_1m=df_1m, symbol=symbol, min_ratio=min_ratio)
                logger.info(
                    "[VOL FILTER] ATR df fallback symbol=%s allow=%s detail=%s",
                    symbol,
                    not bool(ng),
                    detail,
                )
                return not bool(ng)
            except Exception:
                logger.exception("[VOL FILTER] ATR df fallback failed symbol=%s", symbol)

    logger.info(
        "[VOL FILTER] ATR pass-open symbol=%s reason=no_atr_data price=%s",
        symbol,
        price,
    )
    return True


def _range_5m_filter_from_entry_row(entry_row: Any, min_pct: float = 0.008) -> bool:
    """
    entry_controller.py 互換用。

    Returns
    -------
    bool
        True  = ENTRY許可
        False = ENTRY拒否
    """
    row = _row_to_dict(entry_row)
    symbol = _normalize_symbol(
        _first(row, ("symbol", "Symbol", "銘柄コード", "code", "stock_code"), "")
    )

    close = safe_float(
        _first(row, ("close_price", "close", "price", "current_price"), 0),
        0,
    )
    high = safe_float(_first(row, ("high_price", "high"), 0), 0)
    low = safe_float(_first(row, ("low_price", "low"), 0), 0)

    if high > 0 and low > 0 and close > 0 and high >= low:
        ratio = (high - low) / close
        ok = ratio >= float(min_pct)
        logger.info(
            "[VOL FILTER] RANGE entry_row symbol=%s ok=%s range=%.6f price=%.3f ratio=%.6f min_pct=%.6f",
            symbol,
            ok,
            high - low,
            close,
            ratio,
            min_pct,
        )
        return bool(ok)

    if symbol:
        df_5m = _get_global_summary_df(5)
        if isinstance(df_5m, pd.DataFrame) and not df_5m.empty:
            try:
                ng, detail = range_5m_filter(df_5m=df_5m, symbol=symbol, min_pct=min_pct)
                logger.info(
                    "[VOL FILTER] RANGE df fallback symbol=%s allow=%s detail=%s",
                    symbol,
                    not bool(ng),
                    detail,
                )
                return not bool(ng)
            except Exception:
                logger.exception("[VOL FILTER] RANGE df fallback failed symbol=%s", symbol)

    logger.info(
        "[VOL FILTER] RANGE pass-open symbol=%s reason=no_range_data close=%s",
        symbol,
        close,
    )
    return True


# ==========================================================
# ATR(1分) ベース 実効ボラフィルタ
# ==========================================================

def atr_1m_filter(
    entry_row: Any = None,
    *,
    df_1m: pd.DataFrame | None = None,
    symbol: str | None = None,
    min_ratio: float = 0.0025,  # 0.25%
):
    """
    1分足 ATR ベースの実効ボラティリティ判定。

    呼び出し形式1: 旧正式API
        atr_1m_filter(df_1m=df, symbol="4169")
        -> (ng: bool, detail: dict)

    呼び出し形式2: entry_controller互換API
        atr_1m_filter(entry_row)
        -> bool allow
    """

    if _is_entry_row_call(entry_row, df_1m, symbol):
        return _atr_1m_filter_from_entry_row(entry_row, min_ratio=min_ratio)

    symbol = _normalize_symbol(symbol)
    df_1m = _ensure_ohlc_aliases(df_1m if isinstance(df_1m, pd.DataFrame) else pd.DataFrame())

    # --------------------------------------------------
    # DF チェック
    # --------------------------------------------------
    if df_1m is None or df_1m.empty:
        return True, {
            "reason": "1m未生成",
            "atr": None,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": None,
            "bars": 0,
        }

    if "symbol" not in df_1m.columns:
        return True, {
            "reason": "symbol列なし",
            "atr": None,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": None,
            "bars": 0,
        }

    required = {"high_price", "low_price", "close_price"}
    if not required.issubset(set(df_1m.columns)):
        return True, {
            "reason": "OHLC列不足",
            "atr": None,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": None,
            "bars": 0,
            "missing": sorted(required - set(df_1m.columns)),
        }

    d = df_1m[df_1m["symbol"].astype(str).str.replace(r"\.0$", "", regex=True) == symbol]
    bars = len(d)

    if bars < 15:
        return True, {
            "reason": "1m本数不足",
            "atr": None,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": None,
            "bars": bars,
        }

    # --------------------------------------------------
    # ATR(14) 計算
    # --------------------------------------------------
    highs = pd.to_numeric(d["high_price"], errors="coerce").fillna(0).values
    lows = pd.to_numeric(d["low_price"], errors="coerce").fillna(0).values
    closes = pd.to_numeric(d["close_price"], errors="coerce").fillna(0).values

    tr = []
    for i in range(1, bars):
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    if len(tr) < 14:
        return True, {
            "reason": "ATR計算不可",
            "atr": None,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": None,
            "bars": bars,
        }

    atr = sum(tr[-14:]) / 14
    price = safe_float(closes[-1], 0)

    if atr <= 0 or price <= 0:
        return True, {
            "reason": "価格異常",
            "atr": atr,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": price,
            "bars": bars,
        }

    atr_ratio = atr / price

    # --------------------------------------------------
    # 判定
    # --------------------------------------------------
    if atr_ratio < min_ratio:
        return True, {
            "reason": "ATR不足",
            "atr": atr,
            "atr_ratio": atr_ratio,
            "min_ratio": min_ratio,
            "price": price,
            "bars": bars,
        }

    # OK
    return False, {
        "reason": "OK",
        "atr": atr,
        "atr_ratio": atr_ratio,
        "min_ratio": min_ratio,
        "price": price,
        "bars": bars,
    }


# ==========================================================
# 直近5分 高安幅 ボラフィルタ
# ==========================================================

def range_5m_filter(
    entry_row: Any = None,
    *,
    df_5m: pd.DataFrame | None = None,
    symbol: str | None = None,
    min_pct: float = 0.008,
):
    """
    直近5分足の高安値幅による実効ボラ判定。

    呼び出し形式1: 旧正式API
        range_5m_filter(df_5m=df, symbol="4169")
        -> (ng: bool, detail: dict)

    呼び出し形式2: entry_controller互換API
        range_5m_filter(entry_row)
        -> bool allow
    """

    if _is_entry_row_call(entry_row, df_5m, symbol):
        return _range_5m_filter_from_entry_row(entry_row, min_pct=min_pct)

    symbol = _normalize_symbol(symbol)
    df_5m = _ensure_ohlc_aliases(df_5m if isinstance(df_5m, pd.DataFrame) else pd.DataFrame())

    if df_5m is None or df_5m.empty:
        return True, {
            "reason": "5m未生成",
            "range": None,
            "ratio": None,
            "min_pct": min_pct,
            "price": None,
        }

    if "symbol" not in df_5m.columns:
        return True, {
            "reason": "symbol列なし",
            "range": None,
            "ratio": None,
            "min_pct": min_pct,
            "price": None,
        }

    required = {"high_price", "low_price", "close_price"}
    if not required.issubset(set(df_5m.columns)):
        return True, {
            "reason": "OHLC列不足",
            "range": None,
            "ratio": None,
            "min_pct": min_pct,
            "price": None,
            "missing": sorted(required - set(df_5m.columns)),
        }

    d = df_5m[df_5m["symbol"].astype(str).str.replace(r"\.0$", "", regex=True) == symbol]
    if d.empty:
        return True, {
            "reason": "5mデータなし",
            "range": None,
            "ratio": None,
            "min_pct": min_pct,
            "price": None,
        }

    high = safe_float(d.iloc[-1].get("high_price"), 0)
    low = safe_float(d.iloc[-1].get("low_price"), 0)
    close = safe_float(d.iloc[-1].get("close_price"), 0)

    if high <= 0 or low <= 0 or close <= 0:
        return True, {
            "reason": "価格異常",
            "range": None,
            "ratio": None,
            "min_pct": min_pct,
            "price": close,
        }

    r = high - low
    ratio = r / close

    if ratio < min_pct:
        return True, {
            "reason": "RANGE不足",
            "range": r,
            "ratio": ratio,
            "min_pct": min_pct,
            "price": close,
        }

    return False, {
        "reason": "OK",
        "range": r,
        "ratio": ratio,
        "min_pct": min_pct,
        "price": close,
    }
