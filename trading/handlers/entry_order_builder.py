# ==========================================================
# trading/handlers/entry_order_builder.py
# Ver2.1.1-FIX-MISSING-LOGGER
# ----------------------------------------------------------
# ✔ 注文条件（price / order_type / qty）を決定するだけ
# ✔ 副作用ゼロ（発注・global_state 操作なし）
# ✔ SUMMARY / RANKING / TONOSAMA 共通
# ✔ BUY のみ最小単元救済（QTY_ZERO 根絶）
# ✔ qty_override 対応（ENTRY_CONTROLLER 最新版互換）
# ✔ SUMMARY_AI で板が無い場合は MARKET ではなく close/vwap 指値にする
# ✔ SUMMARY_AI の約定優先指値を導入
# ✔ 低ボラ最終防衛を安全化
# ✔ SUMMARY_AI 発注直前に5秒足の向きを確認
# ✔ SUMMARY_AI 発注直前にMTF/傾きの逆方向をブロック
# ✔ スプレッド上限を環境変数化し、既定0.15%へ厳格化
# ✔ SELL信用新規で base_price より下のLIMITを作らない
# ✔ Ver2.1.1: board retry logging で logger 未定義になり、
#              _execute_best_candidate が NameError で False になる問題を修正
#
# 【Ver2.0 追加】
# ✔ 低変動ガードを SUMMARY_AI 限定から RANKING / TONOSAMA にも拡張
# ✔ ENTRY_ORDER_LOW_MOVE_SOURCES で対象sourceを環境変数制御
# ✔ 変動の少ないランキング銘柄を発注直前で止める
#
# 【Ver2.1 追加】
# ✔ A/B PUSHローテーション対策: SUMMARY_AI 発注直前の板取得を短時間リトライ
# ✔ 板が一時的に無いだけで即 fallback/skip しにくくする
# ==========================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Any, Iterable, Optional, Tuple

import pandas as pd

from global_state import global_data
from trading.exit.symbol_trade_guard import is_entry_blocked
from utils_common import (
    get_latest_bid_ask,
    calculate_qty_by_budget,
    get_tick_size,
)

logger = logging.getLogger(__name__)

ALLOW_MARKET_IF_BAD_BOARD = str(os.getenv("ALLOW_MARKET_IF_BAD_BOARD", "1")).lower() not in {
    "0", "false", "no", "off",
}

MAX_SPREAD_PCT_FOR_LIMIT = float(
    os.getenv("MAX_SPREAD_PCT_FOR_LIMIT", os.getenv("ENTRY_ORDER_MAX_SPREAD_PCT", "0.15"))
)

MIN_ENTRY_QTY = 100
SUMMARY_AGGRESSIVE_LIMIT_TICKS = int(float(os.getenv("SUMMARY_AGGRESSIVE_LIMIT_TICKS", "1")))
SELL_CREDIT_LIMIT_DOWN_TICK_ENABLED = str(os.getenv("SELL_CREDIT_LIMIT_DOWN_TICK_ENABLED", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}
SELL_CREDIT_LIMIT_UP_TICK_WHEN_BOARD_MISSING = int(float(os.getenv("SELL_CREDIT_LIMIT_UP_TICK_WHEN_BOARD_MISSING", "0")))

ENTRY_ORDER_5S_GUARD_ENABLED = str(os.getenv("ENTRY_ORDER_5S_GUARD_ENABLED", "1")).lower() not in {
    "0", "false", "no", "off",
}
ENTRY_ORDER_REQUIRE_5S_DATA = str(os.getenv("ENTRY_ORDER_REQUIRE_5S_DATA", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}
ENTRY_ORDER_5S_MIN_BARS = int(float(os.getenv("ENTRY_ORDER_5S_MIN_BARS", "2")))
ENTRY_ORDER_5S_MAX_AGE_SEC = float(os.getenv("ENTRY_ORDER_5S_MAX_AGE_SEC", "20"))

ENTRY_ORDER_MTF_GUARD_ENABLED = str(os.getenv("ENTRY_ORDER_MTF_GUARD_ENABLED", "1")).lower() not in {
    "0", "false", "no", "off",
}
ENTRY_ORDER_REQUIRE_MTF_DATA = str(os.getenv("ENTRY_ORDER_REQUIRE_MTF_DATA", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}
ENTRY_ORDER_MTF_SLOPE_EPS = float(os.getenv("ENTRY_ORDER_MTF_SLOPE_EPS", "0.0"))
ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF = str(os.getenv("ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF", "1")).lower() not in {
    "0", "false", "no", "off",
}

ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED = str(os.getenv("ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED", "1")).lower() not in {
    "0", "false", "no", "off",
}
ENTRY_ORDER_LOW_MOVE_SOURCES = {
    s.strip().upper()
    for s in os.getenv("ENTRY_ORDER_LOW_MOVE_SOURCES", "SUMMARY_AI,RANKING,TONOSAMA,EARLY_SCALP").split(",")
    if s.strip()
}
ENTRY_ORDER_MIN_RANGE_PCT = float(os.getenv("ENTRY_ORDER_MIN_RANGE_PCT", "0.006"))
ENTRY_ORDER_MIN_ATR_RATIO = float(os.getenv("ENTRY_ORDER_MIN_ATR_RATIO", "0.0035"))
ENTRY_ORDER_REQUIRE_ATR = str(os.getenv("ENTRY_ORDER_REQUIRE_ATR", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}
ENTRY_ORDER_REQUIRE_HIGH_LOW = str(os.getenv("ENTRY_ORDER_REQUIRE_HIGH_LOW", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}

ENTRY_ORDER_BOARD_RETRY_ENABLED = str(os.getenv("ENTRY_ORDER_BOARD_RETRY_ENABLED", "1")).lower() not in {
    "0", "false", "no", "off",
}
ENTRY_ORDER_BOARD_RETRY_SEC = float(os.getenv("ENTRY_ORDER_BOARD_RETRY_SEC", "5.6"))
ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC = float(os.getenv("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.4"))
ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY = str(os.getenv("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}


def _ok(**kwargs) -> Dict[str, Any]:
    return {"ok": True, "reason": "OK", "detail": kwargs}


def _ng(reason: str, **detail) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _first(row: Dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    try:
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
    except Exception:
        pass
    return default


def _row_get(row: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(row, dict) and name in row:
                v = row.get(name)
                if v not in (None, ""):
                    return v
            if hasattr(row, "get"):
                v = row.get(name, None)
                if v not in (None, ""):
                    return v
            if hasattr(row, name):
                v = getattr(row, name)
                if v not in (None, ""):
                    return v
        except Exception:
            continue
    return default


def _entry_get(entry_row: Any, *names: str, default: Any = None) -> Any:
    return _row_get(entry_row or {}, *names, default=default)


def _parse_dt(v: Any) -> Optional[dt.datetime]:
    try:
        if v is None or v == "":
            return None
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None) if v.tzinfo else v
        s = str(v).strip()
        if not s:
            return None
        s = s.replace("T", " ").split("+", 1)[0]
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _trade_guard_block(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        blocked, reason, detail = is_entry_blocked(symbol)
        if blocked:
            return _ng(reason, symbol=symbol, **(detail or {}))
    except Exception:
        return None
    return None


def _source_uses_low_move_guard(source: str) -> bool:
    src = str(source or "").upper()
    if "ALL" in ENTRY_ORDER_LOW_MOVE_SOURCES:
        return True
    return src in ENTRY_ORDER_LOW_MOVE_SOURCES


def _get_board_with_retry(symbol: str, *, source: str, side: str) -> Optional[Dict[str, Any]]:
    """
    A/Bローテーションでは、対象銘柄が未登録側にいる瞬間だけ板が取れない。
    SUMMARY_AIの発注直前だけ短時間待って再取得し、即スキップを減らす。
    """
    attempts = 0
    deadline = time.monotonic() + max(0.0, ENTRY_ORDER_BOARD_RETRY_SEC)
    interval = max(0.05, ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC)

    while True:
        attempts += 1
        try:
            board = get_latest_bid_ask(symbol)
        except Exception:
            board = None

        if board:
            if attempts > 1:
                logger.info(
                    "[ENTRY ORDER BOARD RETRY] board recovered symbol=%s side=%s source=%s attempts=%d",
                    symbol,
                    side,
                    source,
                    attempts,
                )
            return board

        if not ENTRY_ORDER_BOARD_RETRY_ENABLED:
            return None

        if time.monotonic() >= deadline:
            logger.warning(
                "[ENTRY ORDER BOARD RETRY] board missing after retry symbol=%s side=%s source=%s attempts=%d retry_sec=%.3f",
                symbol,
                side,
                source,
                attempts,
                ENTRY_ORDER_BOARD_RETRY_SEC,
            )
            return None

        time.sleep(interval)


def _low_move_hard_block_core(entry_row: Dict[str, Any], *, symbol: str, source: str) -> Optional[Dict[str, Any]]:
    if not ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED:
        return None
    if not _source_uses_low_move_guard(source):
        return None

    row = entry_row or {}
    close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
    atr = _safe_float(_first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0), 0.0)

    if close <= 0:
        return _ng(
            "LOW_MOVE_NO_CLOSE",
            symbol=symbol,
            source=source,
            close=close,
            min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
            min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
        )

    range_checked = False
    if high > 0 and low > 0 and high >= low:
        range_checked = True
        range_value = high - low
        range_pct = range_value / close if close > 0 else 0.0
        if range_pct < ENTRY_ORDER_MIN_RANGE_PCT:
            return _ng(
                "LOW_MOVE_RANGE_TOO_SMALL",
                symbol=symbol,
                source=source,
                close=close,
                high=high,
                low=low,
                range_value=range_value,
                range_pct=range_pct,
                min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
            )
    elif ENTRY_ORDER_REQUIRE_HIGH_LOW:
        return _ng(
            "LOW_MOVE_NO_HIGH_LOW",
            symbol=symbol,
            source=source,
            close=close,
            high=high,
            low=low,
            min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
            min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
        )

    if atr > 0:
        atr_ratio = atr / close if close > 0 else 0.0
        if atr_ratio < ENTRY_ORDER_MIN_ATR_RATIO:
            return _ng(
                "LOW_MOVE_ATR_TOO_SMALL",
                symbol=symbol,
                source=source,
                close=close,
                atr=atr,
                atr_ratio=atr_ratio,
                min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
            )
    elif ENTRY_ORDER_REQUIRE_ATR:
        return _ng(
            "LOW_MOVE_NO_ATR",
            symbol=symbol,
            source=source,
            close=close,
            atr=atr,
            range_checked=range_checked,
            min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
        )

    return None


def _summary_mtf_direction_guard(entry_row: Dict[str, Any], *, symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    """発注直前の MTF 方向ガード。

    旧 core/startup/entry_mtf_short_required_daily_optional_patch V1.4 が
    この関数を「日足MTFを必須にせず 1分/3分/5分の 2 of 3 で判定する」版へ
    差し替えていた。その実装は
    trading/handlers/short_mtf_direction_guard.py へ移設済みで、
    有効なプロセスではそちらを使う。
    """
    from trading.handlers import short_mtf_direction_guard as _short_mtf

    if _short_mtf.short_mtf_guard_enabled():
        return _short_mtf.short_mtf_direction_guard(entry_row, symbol=symbol, side=side, source=source)
    return _summary_mtf_direction_guard_legacy(entry_row, symbol=symbol, side=side, source=source)


def _summary_mtf_direction_guard_legacy(entry_row: Dict[str, Any], *, symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not ENTRY_ORDER_MTF_GUARD_ENABLED:
        return None
    if str(source or "").upper() != "SUMMARY_AI":
        return None
    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None
    row = entry_row or {}
    eps = abs(float(ENTRY_ORDER_MTF_SLOPE_EPS or 0.0))
    mtf = _safe_float_or_none(_entry_get(row, "mtf", "score_mtf", "mtf_score", "score_mtf_merged"))
    slope = _safe_float_or_none(_entry_get(row, "slope_atr_scaled", "slope", "score_slope"))
    slope_1m = _safe_float_or_none(_entry_get(row, "slope_atr_scaled_1m", "slope_1m", "slope1m"))
    slope_3m = _safe_float_or_none(_entry_get(row, "slope_atr_scaled_3m", "slope_3m", "slope3m"))
    slope_5m = _safe_float_or_none(_entry_get(row, "slope_atr_scaled_5m", "slope_5m", "slope5m"))
    values = {"mtf": mtf, "slope": slope, "slope_1m": slope_1m, "slope_3m": slope_3m, "slope_5m": slope_5m}
    available = {k: v for k, v in values.items() if v is not None}
    if not available:
        if ENTRY_ORDER_REQUIRE_MTF_DATA:
            return _ng("MTF_NO_DATA", symbol=symbol, side=side_u, require_mtf=True)
        return None
    if side_u == "BUY":
        bad = {k: v for k, v in available.items() if v < -eps}
        if bad:
            return _ng("MTF_NOT_BUY_ALIGNED", symbol=symbol, side=side_u, bad=bad, values=values, eps=eps)
    else:
        if ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF:
            bad = {k: v for k, v in available.items() if k != "mtf" and v > eps}
        else:
            bad = {k: v for k, v in available.items() if v > eps}
        if bad:
            return _ng("MTF_NOT_SELL_ALIGNED", symbol=symbol, side=side_u, bad=bad, values=values, eps=eps)
    return None


def _get_5s_df(symbol: str):
    try:
        m = getattr(global_data, "realtime_5s", None)
        if isinstance(m, dict):
            return m.get(str(symbol)) or m.get(symbol) or m.get(str(symbol).zfill(4))
    except Exception:
        pass
    for attr in ("df_5s_by_symbol", "realtime_5sec", "realtime_5sec_bars", "five_sec_bars", "bars_5s"):
        try:
            m = getattr(global_data, attr, None)
            if isinstance(m, dict):
                df = m.get(str(symbol)) or m.get(symbol) or m.get(str(symbol).zfill(4))
                if df is not None:
                    return df
        except Exception:
            continue
    return None


def _extract_5s_rows(df: Any) -> Optional[Tuple[Any, Any, int]]:
    try:
        if df is None:
            return None
        n = len(df)
        if n < max(2, ENTRY_ORDER_5S_MIN_BARS):
            return None
        if hasattr(df, "iloc"):
            return df.iloc[-2], df.iloc[-1], n
        if isinstance(df, (list, tuple)):
            return df[-2], df[-1], n
    except Exception:
        return None
    return None


def _five_sec_pre_entry_guard(symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not ENTRY_ORDER_5S_GUARD_ENABLED:
        return None
    if str(source or "").upper() != "SUMMARY_AI":
        return None
    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None
    rows = _extract_5s_rows(_get_5s_df(symbol))
    if rows is None:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng("FIVE_SEC_NO_DATA", symbol=symbol, side=side_u, require_5s=True, min_bars=ENTRY_ORDER_5S_MIN_BARS)
        return None
    prev, last, nrows = rows
    prev_close = _safe_float(_row_get(prev, "close_price", "close", "price", "current_price", "Close", default=0.0), 0.0)
    prev_high = _safe_float(_row_get(prev, "high_price", "high", "High", default=0.0), 0.0)
    prev_low = _safe_float(_row_get(prev, "low_price", "low", "Low", default=0.0), 0.0)
    last_open = _safe_float(_row_get(last, "open_price", "open", "Open", default=0.0), 0.0)
    last_close = _safe_float(_row_get(last, "close_price", "close", "price", "current_price", "Close", default=0.0), 0.0)
    last_high = _safe_float(_row_get(last, "high_price", "high", "High", default=0.0), 0.0)
    last_low = _safe_float(_row_get(last, "low_price", "low", "Low", default=0.0), 0.0)
    last_dt = _parse_dt(_row_get(last, "datetime", "timestamp", "time", "created_at", "updated_at", default=None))
    age_sec = None
    if last_dt is not None:
        try:
            age_sec = abs((dt.datetime.now() - last_dt).total_seconds())
        except Exception:
            age_sec = None
    if age_sec is not None and age_sec > ENTRY_ORDER_5S_MAX_AGE_SEC:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng("FIVE_SEC_STALE", symbol=symbol, side=side_u, age_sec=age_sec, max_age_sec=ENTRY_ORDER_5S_MAX_AGE_SEC, nrows=nrows)
        return None
    if last_close <= 0 or prev_close <= 0:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng("FIVE_SEC_INVALID_PRICE", symbol=symbol, side=side_u, prev_close=prev_close, last_close=last_close, nrows=nrows)
        return None
    if last_open <= 0:
        last_open = prev_close
    if last_high <= 0:
        last_high = max(last_open, last_close)
    if last_low <= 0:
        last_low = min(last_open, last_close)
    if prev_high <= 0:
        prev_high = prev_close
    if prev_low <= 0:
        prev_low = prev_close
    bullish = (last_close >= last_open) and ((last_close >= prev_close) or (last_high > prev_high))
    bearish = (last_close <= last_open) and ((last_close <= prev_close) or (last_low < prev_low))
    detail = {"symbol": symbol, "side": side_u, "nrows": nrows, "prev_close": prev_close, "last_open": last_open, "last_close": last_close, "prev_high": prev_high, "last_high": last_high, "prev_low": prev_low, "last_low": last_low, "age_sec": age_sec, "bullish": bullish, "bearish": bearish}
    if side_u == "BUY" and not bullish:
        return _ng("FIVE_SEC_NOT_BULLISH", **detail)
    if side_u == "SELL" and not bearish:
        return _ng("FIVE_SEC_NOT_BEARISH", **detail)
    return None


def _round_price(price: float, side: str) -> float:
    tick = get_tick_size(price)
    if side == "BUY":
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick


def _aggressive_limit_price(base_price: float, side: str, ticks: int = SUMMARY_AGGRESSIVE_LIMIT_TICKS) -> float:
    p = float(base_price)
    if p <= 0:
        return p
    side_u = str(side or "").upper()
    rounded = _round_price(p, side_u)
    tick = get_tick_size(rounded)
    n = max(0, int(ticks or 0))
    if side_u == "SELL" and not SELL_CREDIT_LIMIT_DOWN_TICK_ENABLED:
        up_ticks = max(0, int(SELL_CREDIT_LIMIT_UP_TICK_WHEN_BOARD_MISSING or 0))
        return rounded + tick * up_ticks
    if n <= 0:
        return rounded
    if side_u == "BUY":
        return rounded + tick * n
    return max(tick, rounded - tick * n)


def five_sec_breakout(symbol: str, side: str) -> Optional[float]:
    rows = _extract_5s_rows(_get_5s_df(symbol))
    if rows is None:
        return None
    prev, last, _nrows = rows
    prev_high = _safe_float(_row_get(prev, "high_price", "high", "High", default=0.0), 0.0)
    prev_low = _safe_float(_row_get(prev, "low_price", "low", "Low", default=0.0), 0.0)
    last_high = _safe_float(_row_get(last, "high_price", "high", "High", default=0.0), 0.0)
    last_low = _safe_float(_row_get(last, "low_price", "low", "Low", default=0.0), 0.0)
    if side == "BUY" and last_high > 0 and prev_high > 0 and last_high > prev_high:
        return float(last_high)
    if side == "SELL" and last_low > 0 and prev_low > 0 and last_low < prev_low:
        return float(last_low)
    return None


# ============================================================
# 5秒足ブレイク無しの LIMIT フォールバック
#   旧 core/startup/entry_order_5s_breakout_optional_patch.py (V1)
# ------------------------------------------------------------
# RANKING / TONOSAMA / EARLY_SCALP が「5秒足ブレイクが無い」だけを理由に
# NO_5S_BREAKOUT で注文生成 NG になるのを緩和する。ユーザー方針は
# 「5秒足は必須にしない」。LOW_LIQUIDITY / QTY_ZERO など他の NG 理由は
# 変更しない。SUMMARY_AI は既存の5秒足ガード/板touch系を尊重して原則対象外。
#
# 旧パッチは build_entry_order を外側からラップしていたが、
# NO_5S_BREAKOUT を返すのは下の 1 箇所だけなので、そこに直接置いた。
# これによりラップ順のレース (summary_ai_fast_order_builder_patch が
# _original を辿って 5s ラッパーを捨てうる問題) も解消する。
# 一方でフォールバック注文は rest_full_board / entry_limit_passive など
# 外側のラッパーを通るようになる = ガードが増える方向の差異がある。
# ============================================================
_FIVE_SEC_OPTIONAL_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)
_FIVE_SEC_OPTIONAL_DB_PROCESS_ENVS = (
    "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
    "AUTOSTOCK_SUMMARY_DB_WRITER",
    "AUTOSTOCK_MAIN_DATABASE_PROCESS",
    "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
)
_FIVE_SEC_OPTIONAL_DEFAULT_SOURCES = "RANKING,TONOSAMA,EARLY_SCALP,ENTRY"


def _5s_env_bool(name: str, default: bool = True) -> bool:
    """旧パッチの _env_bool。真値集合に "enable" を含まない点をそのまま残す。"""
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enabled"}
    except Exception:
        return bool(default)


def _5s_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _five_sec_optional_scope_enabled() -> bool:
    """このプロセスで NO_5S_BREAKOUT フォールバックを使うか。

    旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 =
    main lite の background loop か SITECUSTOMIZE_ENABLE_FULL_NONMAIN=1 の
    ときだけ install されていた。DISABLE 名が実体とズレている
    (DISABLE_TONOSAMA_5SEC_ADVISORY_PATCH) のは削除済みパッチの名残だが、
    運用で使われうるのでそのまま再現する。
    ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_SCOPE_ENABLED=1/0 で明示上書きできる。
    """
    try:
        raw = os.getenv("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _5s_env_bool("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_SCOPE_ENABLED", False)
        if str(os.environ.get("DISABLE_TONOSAMA_5SEC_ADVISORY_PATCH", "")).strip() == "1":
            return False
        if not _5s_env_bool("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_ENABLED", True):
            return False
        argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
        argv = " ".join(argv_list)
        if any(name in argv for name in _FIVE_SEC_OPTIONAL_DB_PROCESS_NAMES):
            return False
        if any(_5s_env_bool(name, False) for name in _FIVE_SEC_OPTIONAL_DB_PROCESS_ENVS):
            return False
        if any(a.endswith("/main.py") or a == "main.py" for a in argv_list) and _5s_env_bool("SITECUSTOMIZE_MAIN_LITE", True):
            return True
        return _5s_env_bool("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False)
    except Exception:
        return False


def _five_sec_optional_source_allowed(source: str) -> bool:
    src = str(source or "").upper()
    allowed = {
        s.strip().upper()
        for s in os.getenv("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_SOURCES", _FIVE_SEC_OPTIONAL_DEFAULT_SOURCES).split(",")
        if s.strip()
    }
    return "ALL" in allowed or src in allowed


def _five_sec_optional_row_to_dict(row: Any) -> Dict[str, Any]:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _five_sec_optional_fallback_order(*, symbol: str, side: str, source: str, entry_row: Any, qty_override: Optional[int]) -> Optional[Dict[str, Any]]:
    """NO_5S_BREAKOUT の代わりに返す注文。適用対象外なら None。

    旧パッチは NG 側の理由も差し替えていた (NO_PRICE_SOURCE_AFTER_NO_5S_BREAKOUT
    など) ので、dict を返したらそのまま返す。
    """
    if not _five_sec_optional_scope_enabled():
        return None
    source_u = str(source or "").upper()
    if not _five_sec_optional_source_allowed(source_u):
        return None
    if source_u == "SUMMARY_AI" and not _5s_env_bool("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_FOR_SUMMARY", False):
        return None

    side_u = str(side or "").upper()
    symbol_s = str(symbol or "").strip()
    row = _five_sec_optional_row_to_dict(entry_row)
    base_price = _safe_float(_first(row, ("close_price", "price", "current_price", "close", "vwap"), 0.0), 0.0)
    if base_price <= 0:
        return {"ok": False, "reason": "NO_PRICE_SOURCE_AFTER_NO_5S_BREAKOUT", "detail": {"symbol": symbol_s, "side": side_u, "source": source_u}}

    try:
        price = float(_round_price(base_price, side_u))
    except Exception:
        price = float(base_price)

    volume = _safe_float(_first(row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    trading_value = price * volume if price > 0 else 0.0
    min_turnover = _5s_env_float("ENTRY_ORDER_5S_FALLBACK_MIN_TRADING_VALUE", 3_000_000.0)
    if trading_value < min_turnover:
        return {
            "ok": False,
            "reason": "LOW_LIQUIDITY_AFTER_NO_5S_BREAKOUT",
            "detail": {"symbol": symbol_s, "side": side_u, "source": source_u, "price": price, "volume": volume, "trading_value": trading_value, "min_trading_value": min_turnover},
        }

    if qty_override is not None:
        qty = int(qty_override)
        if qty <= 0:
            return {"ok": False, "reason": "INVALID_QTY_OVERRIDE", "detail": {"qty_override": qty_override}}
    else:
        try:
            qty = int(calculate_qty_by_budget(price))
        except Exception:
            qty = 0
        if qty <= 0 and side_u == "BUY":
            qty = 100
    if qty <= 0:
        return {"ok": False, "reason": "QTY_ZERO_AFTER_NO_5S_BREAKOUT", "detail": {"symbol": symbol_s, "side": side_u, "price": price, "base_price": base_price}}

    detail = {
        "order_type": "LIMIT",
        "price": price,
        "base_price": base_price,
        "qty": qty,
        "spread_pct": None,
        "max_spread_pct": None,
        "board": False,
        "price_source": "close_limit_fallback_no_5s_breakout",
        "fallback_from": "NO_5S_BREAKOUT",
        "five_sec_breakout_optional": True,
        "source": source_u,
        "symbol": symbol_s,
        "side": side_u,
        "trading_value": trading_value,
    }
    if qty_override is not None:
        detail["qty_override"] = True
    logger.warning(
        "[ENTRY ORDER 5S OPTIONAL] ALLOW_NO_5S_BREAKOUT symbol=%s side=%s source=%s price=%.4f base=%.4f qty=%s volume=%.0f trading_value=%.0f",
        symbol_s,
        side_u,
        source_u,
        price,
        base_price,
        qty,
        volume,
        trading_value,
    )
    return _ok(**detail)


def _build_entry_order_core(*, symbol: str, side: str, source: str, entry_row: Dict[str, Any], qty_override: Optional[int] = None) -> Dict[str, Any]:
    price = None
    base_price = None
    spread_pct = None
    board = None
    price_source = None
    side = side.upper()
    source = source.upper()

    trade_guard_ng = _trade_guard_block(symbol)
    if trade_guard_ng is not None:
        return trade_guard_ng

    low_move_ng = _low_move_hard_block(entry_row, symbol=symbol, source=source)
    if low_move_ng is not None:
        return low_move_ng

    mtf_ng = _summary_mtf_direction_guard(entry_row, symbol=symbol, side=side, source=source)
    if mtf_ng is not None:
        return mtf_ng

    five_sec_ng = _five_sec_pre_entry_guard(symbol=symbol, side=side, source=source)
    if five_sec_ng is not None:
        return five_sec_ng

    if source == "SUMMARY_AI":
        board = _get_board_with_retry(symbol, source=source, side=side)
        if board:
            bid = board.get("bid_price")
            ask = board.get("ask_price")
            if not bid or not ask or bid <= 0 or ask <= 0:
                if ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY:
                    return _ng("INVALID_BOARD", board=board, require_board=True)
                board = None
            else:
                spread_pct = (ask - bid) / bid * 100
                base_price = bid if side == "SELL" else ask
                price_source = "board_bid_ask_safe_limit"
                if ALLOW_MARKET_IF_BAD_BOARD and spread_pct > MAX_SPREAD_PCT_FOR_LIMIT:
                    return _ng("SPREAD_TOO_WIDE", symbol=symbol, side=side, spread_pct=spread_pct, max_spread_pct=MAX_SPREAD_PCT_FOR_LIMIT, bid=bid, ask=ask)
                price = _aggressive_limit_price(float(base_price), side)
                order_type = "LIMIT"
        if not board:
            if ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY:
                return _ng(
                    "STRICT_BOARD_MISSING",
                    symbol=symbol,
                    side=side,
                    source=source,
                    retry_enabled=ENTRY_ORDER_BOARD_RETRY_ENABLED,
                    retry_sec=ENTRY_ORDER_BOARD_RETRY_SEC,
                    message="板が取れないためSUMMARY_AI新規エントリーを停止",
                )
            base_price = entry_row.get("close_price") or entry_row.get("price") or entry_row.get("current_price") or entry_row.get("close") or entry_row.get("vwap")
            if not base_price or base_price <= 0:
                return _ng("NO_PRICE_SOURCE")
            price = _aggressive_limit_price(float(base_price), side)
            order_type = "LIMIT"
            price_source = "summary_fallback_safe_limit_after_board_retry"
    else:
        base_price = five_sec_breakout(symbol, side)
        if not base_price:
            fallback = _five_sec_optional_fallback_order(
                symbol=symbol, side=side, source=source, entry_row=entry_row, qty_override=qty_override
            )
            if fallback is not None:
                return fallback
            return _ng("NO_5S_BREAKOUT")
        price = _round_price(base_price, side)
        order_type = "STOP"
        price_source = "five_sec_breakout"

    try:
        volume = float(entry_row.get("volume") or 0.0)
    except Exception:
        volume = 0.0
    effective_price = price if price else base_price
    trading_value = effective_price * volume if effective_price else 0.0
    if trading_value < 3_000_000:
        return _ng("LOW_LIQUIDITY", trading_value=trading_value, price=effective_price, volume=volume)

    forced_min_qty = False
    if qty_override is not None:
        qty = int(qty_override)
        if qty <= 0:
            return _ng("INVALID_QTY_OVERRIDE", qty_override=qty_override)
    else:
        if trading_value < 8_000_000:
            qty = MIN_ENTRY_QTY
            forced_min_qty = True
        else:
            qty = calculate_qty_by_budget(effective_price)
            if qty <= 0 and side == "BUY":
                qty = MIN_ENTRY_QTY
                forced_min_qty = True
    if qty <= 0:
        return _ng("QTY_ZERO", side=side, price=price, base_price=base_price, spread_pct=spread_pct, trading_value=trading_value)

    detail = {
        "order_type": order_type,
        "price": price,
        "base_price": base_price,
        "qty": qty,
        "spread_pct": spread_pct,
        "max_spread_pct": MAX_SPREAD_PCT_FOR_LIMIT,
        "board": bool(board),
        "price_source": price_source,
        "aggressive_limit_ticks": SUMMARY_AGGRESSIVE_LIMIT_TICKS if source == "SUMMARY_AI" and order_type == "LIMIT" and side == "BUY" else 0,
        "sell_credit_limit_down_tick_enabled": SELL_CREDIT_LIMIT_DOWN_TICK_ENABLED,
        "trade_guard": True,
        "low_move_guard": bool(ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED and _source_uses_low_move_guard(source)),
        "low_move_sources": sorted(ENTRY_ORDER_LOW_MOVE_SOURCES),
        "five_sec_guard": bool(ENTRY_ORDER_5S_GUARD_ENABLED and source == "SUMMARY_AI"),
        "require_5s_data": ENTRY_ORDER_REQUIRE_5S_DATA,
        "mtf_guard": bool(ENTRY_ORDER_MTF_GUARD_ENABLED and source == "SUMMARY_AI"),
        "require_mtf_data": ENTRY_ORDER_REQUIRE_MTF_DATA,
        "sell_ignore_positive_mtf": ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF,
        "board_retry_enabled": ENTRY_ORDER_BOARD_RETRY_ENABLED,
        "board_retry_sec": ENTRY_ORDER_BOARD_RETRY_SEC,
        "require_board_for_summary": ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY,
    }
    if qty_override is not None:
        detail["qty_override"] = True
    if forced_min_qty:
        detail["forced_min_qty"] = True
        detail["liquidity_class"] = "LOW"
    return _ok(**detail)


# ============================================================
# SUMMARY_AI レンジ整合による低変動ガードの再判定
# ------------------------------------------------------------
# 旧 core/startup/summary_ai_range_consistency_patch.py (V1) を本文化。
#
# SUMMARY_AI 経路で LOW_MOVE_RANGE_TOO_SMALL / LOW_MOVE_ATR_TOO_SMALL に
# なった行について、entry_row 内の別ソースの high/low からレンジを取り直し、
# 閾値を満たすなら high/low/range_pct/atr を整合させたうえで通過させる。
# 低変動ガード自体は緩めない (閾値は ENTRY_ORDER_MIN_RANGE_PCT /
# ENTRY_ORDER_MIN_ATR_RATIO をそのまま使う)。
#
# 【重要】この関数を消す/順序を変える場合は
# core/startup/low_volatility_entry_guard_patch.py と
# core/startup/summary_ai_order_builder_range_repair_patch.py の
# _low_move_hard_block ラッパ (現在は死にコード) を必ず同時に確認すること。
# 3者とも `getattr(cur, "_original", cur)` で素の実装を掴み直す設計のため、
# 本文化前は「最後に install された旧 range_consistency」だけが効いていた。
# ============================================================

def _rc_f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _rc_first(d: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        try:
            x = _rc_f(d.get(k), 0.0)
            if x > 0:
                return x
        except Exception:
            pass
    return 0.0


def _rc_ratio(close: float, high: float, low: float) -> float:
    """旧 summary_ai_range_consistency_patch._ratio。

    _low_move_hard_block を本文化した際に定義を移し忘れており、
    _rc_best_range 内の呼び出しが NameError になっていたので補った
    (原本 4777d87 の実装をそのまま復元)。
    """
    if close <= 0 or high <= 0 or low <= 0 or high < low:
        return 0.0
    return max(0.0, (high - low) / close)


def _rc_best_range(row: dict[str, Any], close: float) -> tuple[str, float, float, float]:
    cond = row.get("entry_conditions") if isinstance(row.get("entry_conditions"), dict) else {}
    candidates: list[tuple[str, float, float, float]] = []

    for label, src in (("bar", row), ("row_day", row), ("conditions_day", cond), ("recent", row)):
        if label == "bar":
            hi = _rc_first(src, ("high_price", "high"))
            lo = _rc_first(src, ("low_price", "low"))
        elif label == "recent":
            hi = _rc_first(src, ("recent_high", "range_high", "high_1m_max"))
            lo = _rc_first(src, ("recent_low", "range_low", "low_1m_min"))
        else:
            hi = _rc_first(src, ("day_high", "today_high", "session_high", "intraday_high", "HighPrice"))
            lo = _rc_first(src, ("day_low", "today_low", "session_low", "intraday_low", "LowPrice"))
        if hi > 0 and lo > 0:
            h, l = max(hi, lo), min(hi, lo)
            candidates.append((label, h, l, _rc_ratio(close, h, l)))

    explicit = max(
        _rc_first(row, ("range_pct", "intraday_range_pct", "summary_ai_range_repair_pct")),
        _rc_first(cond, ("range_pct", "intraday_range_pct", "day_range_pct")),
    )
    if explicit > 0:
        width = close * explicit
        candidates.append(("explicit", close + width / 2.0, max(0.01, close - width / 2.0), explicit))

    if not candidates:
        return ("missing", 0.0, 0.0, 0.0)
    return max(candidates, key=lambda x: x[3])


_RC_VERSION = "V1-SUMMARY-AI-RANGE-CONSISTENCY"


def _low_move_hard_block(entry_row: Dict[str, Any], *, symbol: str, source: str) -> Optional[Dict[str, Any]]:
    result = _low_move_hard_block_core(entry_row, symbol=symbol, source=source)
    if result is None:
        return None
    try:
        if str(source or "").upper() != "SUMMARY_AI" or not isinstance(entry_row, dict):
            return result
        reason = str(result.get("reason") if isinstance(result, dict) else "").upper()
        if reason not in {"LOW_MOVE_RANGE_TOO_SMALL", "LOW_MOVE_ATR_TOO_SMALL"}:
            return result
        close = _rc_first(entry_row, ("close_price", "close", "price", "current_price"))
        if close <= 0:
            return result
        min_range = _rc_f(globals().get("ENTRY_ORDER_MIN_RANGE_PCT", 0.006), 0.006)
        min_atr = _rc_f(globals().get("ENTRY_ORDER_MIN_ATR_RATIO", 0.0035), 0.0035)
        label, high, low, rpct = _rc_best_range(entry_row, close)
        width = max(0.0, high - low)
        atr0 = _rc_first(entry_row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"))
        atr = max(atr0, width)
        atr_ratio = atr / close if close > 0 and atr > 0 else 0.0
        if rpct >= min_range and atr_ratio >= min_atr:
            entry_row["high"] = high
            entry_row["high_price"] = high
            entry_row["low"] = low
            entry_row["low_price"] = low
            entry_row["range_pct"] = rpct
            entry_row["intraday_range_pct"] = rpct
            entry_row["atr"] = atr
            entry_row["atr_1m"] = atr
            entry_row["ATR"] = atr
            logger.warning(
                "[SUMMARY AI RANGE CONSISTENCY] allow symbol=%s reason=%s source=%s high=%.2f low=%.2f range_pct=%.6f atr_ratio=%.6f min_range=%.6f min_atr=%.6f version=%s",
                symbol, reason, label, high, low, rpct, atr_ratio, min_range, min_atr, _RC_VERSION,
            )
            return None
        return result
    except Exception:
        logger.exception("[SUMMARY AI RANGE CONSISTENCY] failed symbol=%s version=%s", symbol, _RC_VERSION)
        return result


# ============================================================
# SUMMARY_AI FAST ORDER BUILDER
# (旧 core/startup/summary_ai_fast_order_builder_patch.py を本文化)
# ------------------------------------------------------------
# SUMMARY_AI 由来の発注では
#   - snapshot 由来の order kwargs を強制的に整える
#   - 低変動レンジを合成レンジで補修する
#   - 板待ちリトライ秒などのログを出す
# を本体の前段で行う。ネイティブは _build_entry_order_core へ改名した。
#
# 実 install 順を測定した結果、この層は build_entry_order を包む 7 パッチの
# **最内層** (_ORIGINAL_BUILD_ENTRY_ORDER が native を指す) だった。
# 最内層を本文へ畳んでも外側 6 層の順序は変わらない。
#
# 旧 install の _ensure_entry_order_builder_logger は、eob.logger が
# 無い場合の保険だった。本文化後は同じモジュール内なので不要。
# ============================================================

_FOB_VERSION = "V4.0-SUMMARY-AI-BREAKOUT-SR-GUARD"
_FOB__INSTALLED = False
_FOB__ORIGINAL_BUILD_ENTRY_ORDER = None
_FOB__TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "allow", "allowed", "enable", "enabled"}
_FOB__SUMMARY_SOURCE_SET = {"SUMMARY", "SUMMARY_AI", "PUSH", "PUSH_SUMMARY", "STOCK_SUMMARY"}


def _fob_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _fob_first(row: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        try:
            v = row.get(name)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _fob_truthy(v: Any) -> bool:
    try:
        if isinstance(v, bool):
            return v
        return str(v or "").strip().lower() in _FOB__TRUE_SET
    except Exception:
        return False


def _fob_row_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    row = kwargs.get("entry_row")
    if isinstance(row, dict):
        return row
    row = {}
    kwargs["entry_row"] = row
    return row


def _fob_is_summary_ai_order(kwargs: dict[str, Any]) -> bool:
    try:
        source = str(kwargs.get("source") or "").strip().upper()
        row = kwargs.get("entry_row") if isinstance(kwargs.get("entry_row"), dict) else {}
        row_source = str(row.get("source") or "").strip().upper()
        entry_type = str(row.get("entry_type") or kwargs.get("entry_type") or "").strip().upper()
        pipeline_source = str(row.get("pipeline_source") or "").strip().upper()
        joined = "|".join(str(x or "").upper() for x in (source, row_source, entry_type, pipeline_source, row.get("reason"), row.get("ai_reason"), row.get("skip_reason")))
        if source in _FOB__SUMMARY_SOURCE_SET or row_source in _FOB__SUMMARY_SOURCE_SET or pipeline_source == "SUMMARY":
            return True
        if "SUMMARY_AI" in joined or "SRC=SUMMARY" in joined:
            return True
        if _fob_truthy(row.get("ai_gate_allow")) or _fob_truthy(row.get("preapproved")) or _fob_truthy(row.get("summary_ai_ok")):
            return True
        return False
    except Exception:
        return False


def _fob_coerce_summary_ai_snapshot_order(kwargs: dict[str, Any]) -> bool:
    try:
        if not _fob_is_summary_ai_order(kwargs):
            return False
        row = _fob_row_from_kwargs(kwargs)
        symbol = kwargs.get("symbol") or row.get("symbol")
        side = str(kwargs.get("side") or row.get("side") or row.get("entry_decision") or row.get("ai_side") or "").strip().upper()
        old_source = str(kwargs.get("source") or row.get("source") or "").strip().upper()
        if old_source and old_source != "SUMMARY_AI":
            row.setdefault("original_source", old_source)
        kwargs["source"] = "SUMMARY_AI"
        row["source"] = "SUMMARY_AI"
        row["entry_type"] = "SUMMARY_AI"
        if side in {"BUY", "SELL"}:
            kwargs["side"] = side
            row.setdefault("side", side)
            row.setdefault("entry_decision", side)
            row.setdefault("ai_side", side)
        price_f = _fob_safe_float(_fob_first(row, ("close_price", "price", "current_price", "close", "last_price", "vwap", "base_price", "display_price"), 0.0), 0.0)
        if price_f > 0:
            row.setdefault("close_price", price_f)
            row.setdefault("price", price_f)
            row.setdefault("current_price", price_f)
            row.setdefault("close", price_f)
        vol = _fob_safe_float(_fob_first(row, ("volume", "vol", "latest_volume", "display_volume", "_latest_volume"), 0.0), 0.0)
        if vol <= 0:
            turnover = _fob_safe_float(_fob_first(row, ("turnover", "trading_value", "sales_value", "display_turnover"), 0.0), 0.0)
            if turnover > 0 and price_f > 0:
                vol = turnover / price_f
        if vol > 0:
            row.setdefault("volume", vol)
        logger.warning("[SUMMARY AI FAST ORDER BUILDER] snapshot normalized symbol=%s side=%s source %s->SUMMARY_AI price=%s volume=%s version=%s", symbol, side, old_source, row.get("close_price") or row.get("price") or row.get("current_price") or row.get("close"), row.get("volume"), _FOB_VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] snapshot normalize failed version=%s", _FOB_VERSION)
        return False


def _fob_range_pct(high: float, low: float, close: float) -> float:
    try:
        if close <= 0 or high <= 0 or low <= 0 or high < low:
            return 0.0
        return (high - low) / close
    except Exception:
        return 0.0


def _fob_ranking_move_rescue_ok(row: dict[str, Any], *, min_range_pct: float) -> tuple[bool, str]:
    try:
        from trading.filters import volatility_filter as vf
        fn = getattr(vf, "_ranking_move_rescue", None)
        if not callable(fn):
            return False, "no_rescue_func"
        ok = bool(fn(row, min_pct=float(min_range_pct), label="fast_order_low_move"))
        return ok, "ranking_move_rescue" if ok else "ranking_move_rescue_ng"
    except Exception as e:
        logger.debug("[SUMMARY AI FAST ORDER BUILDER] ranking move rescue check failed err=%s", e, exc_info=True)
        return False, f"ranking_move_rescue_error:{e}"


def _fob_apply_synthetic_range(row: dict[str, Any], *, close: float, min_range_pct: float, reason: str) -> None:
    width = max(close * float(min_range_pct) * 1.05, close * 0.001, 1.0)
    high = close + width / 2.0
    low = max(0.01, close - width / 2.0)
    pct = _fob_range_pct(high, low, close)
    atr_proxy = max(_fob_safe_float(_fob_first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0), 0.0), max(0.0, high - low))
    row.update({"high_price": high, "low_price": low, "high": high, "low": low, "range_pct": pct, "intraday_range_pct": pct, "summary_ai_range_repaired": True, "summary_ai_range_repair_reason": reason, "summary_ai_range_repair_pct": pct})
    if atr_proxy > 0:
        row["atr"] = atr_proxy
        row["atr_1m"] = atr_proxy
        row["ATR"] = atr_proxy


def _fob_repair_summary_ai_low_move_range(kwargs: dict[str, Any]) -> bool:
    try:
        if not _fob_is_summary_ai_order(kwargs):
            return False
        row = kwargs.get("entry_row")
        if not isinstance(row, dict):
            return False
        cond = row.get("entry_conditions") if isinstance(row.get("entry_conditions"), dict) else {}
        close = _fob_safe_float(_fob_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
        if close <= 0:
            return False
        min_range_pct = _fob_safe_float(os.getenv("ENTRY_ORDER_MIN_RANGE_PCT", globals().get("ENTRY_ORDER_MIN_RANGE_PCT", 0.006)), 0.006)
        cur_high = _fob_safe_float(_fob_first(row, ("high_price", "high"), 0.0), 0.0)
        cur_low = _fob_safe_float(_fob_first(row, ("low_price", "low"), 0.0), 0.0)
        if _fob_range_pct(cur_high, cur_low, close) >= min_range_pct:
            return False
        candidates: list[tuple[str, float, float, float]] = []
        for label, src in (("row_day", row), ("conditions_day", cond)):
            hi = _fob_safe_float(_fob_first(src, ("day_high", "today_high", "session_high", "intraday_high", "HighPrice", "high_3m", "high_5m"), 0.0), 0.0)
            lo = _fob_safe_float(_fob_first(src, ("day_low", "today_low", "session_low", "intraday_low", "LowPrice", "low_3m", "low_5m"), 0.0), 0.0)
            if hi > 0 and lo > 0:
                h, l = max(hi, lo), min(hi, lo)
                candidates.append((label, h, l, _fob_range_pct(h, l, close)))
        for src_label, src in (("row", row), ("conditions", cond)):
            rp = _fob_safe_float(_fob_first(src, ("range_pct", "intraday_range_pct", "day_range_pct", "summary_ai_range_repair_pct", "_intrabar_range_pct", "_max_price_change_pct"), 0.0), 0.0)
            if rp > 1.0:
                rp = rp / 100.0
            if rp > 0:
                width = close * rp
                candidates.append((f"{src_label}_range_pct", close + width / 2.0, max(0.01, close - width / 2.0), rp))
        best = None
        for cand in candidates:
            if cand[3] >= min_range_pct and (best is None or cand[3] > best[3]):
                best = cand
        if best is None:
            rescue_ok, rescue_reason = _fob_ranking_move_rescue_ok(row, min_range_pct=min_range_pct)
            if rescue_ok:
                _fob_apply_synthetic_range(row, close=close, min_range_pct=min_range_pct, reason=rescue_reason)
                logger.warning("[SUMMARY AI FAST ORDER BUILDER] range/atr repaired by ranking rescue symbol=%s side=%s close=%.2f min_range_pct=%.6f high=%.2f low=%.2f atr=%.4f version=%s", kwargs.get("symbol"), kwargs.get("side"), close, min_range_pct, row.get("high"), row.get("low"), row.get("atr"), _FOB_VERSION)
                return True
            logger.info("[SUMMARY AI FAST ORDER BUILDER] range repair not enough symbol=%s side=%s candidates=%s rescue=%s version=%s", kwargs.get("symbol"), kwargs.get("side"), candidates, rescue_reason, _FOB_VERSION)
            return False
        reason, high, low, pct = best
        atr_proxy = max(_fob_safe_float(_fob_first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0), 0.0), max(0.0, high - low))
        row.update({"high_price": high, "low_price": low, "high": high, "low": low, "range_pct": pct, "intraday_range_pct": pct, "summary_ai_range_repaired": True, "summary_ai_range_repair_reason": reason, "summary_ai_range_repair_pct": pct})
        if atr_proxy > 0:
            row["atr"] = atr_proxy
            row["atr_1m"] = atr_proxy
            row["ATR"] = atr_proxy
        logger.warning("[SUMMARY AI FAST ORDER BUILDER] range/atr repaired symbol=%s side=%s reason=%s high=%.2f low=%.2f range_pct=%.6f atr=%.4f version=%s", kwargs.get("symbol"), kwargs.get("side"), reason, high, low, pct, atr_proxy, _FOB_VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] range repair failed symbol=%s side=%s version=%s", kwargs.get("symbol"), kwargs.get("side"), _FOB_VERSION)
        return False


_FOB_SCOPE: bool | None = None
_FOB_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def _fob_scope_enabled() -> bool:
    """旧 install スコープ (main.py プロセス) の再現。

    旧パッチは summary_ai_runtime_consistency_patch (sitecustomize の
    BACKGROUND_MAIN_PATCHES) と summary_ai_board_missing_defer_patch から
    install されており、どちらも main.py プロセス限定だった。
    """
    global _FOB_SCOPE
    if _FOB_SCOPE is not None:
        return _FOB_SCOPE
    enabled = False
    try:
        argv_parts = [str(x).replace("\\", "/").lower() for x in sys.argv]
        argv = " ".join(argv_parts)
        if not any(n in argv for n in _FOB_DB_PROCESS_NAMES):
            enabled = any(x.endswith("/main.py") or x == "main.py" for x in argv_parts)
    except Exception:
        enabled = False
    _FOB_SCOPE = enabled
    return enabled


def _fob_apply_env_and_constants() -> None:
    """旧 install() の env 強制と eob 定数の上書きを再現する。"""
    global ENTRY_ORDER_BOARD_RETRY_SEC, ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC
    global ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY
    if not _fob_scope_enabled():
        return
    try:
        retry_sec = _fob_safe_float(os.getenv("SUMMARY_AI_PUSH_ROTATION_BOARD_RETRY_SEC", "10.5"), 10.5)
        retry_sec = max(5.0, min(retry_sec, 12.0))
        os.environ["ENTRY_ORDER_BOARD_RETRY_SEC"] = str(retry_sec)
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.2")
        os.environ["SUMMARY_AI_BOARD_RETRY_REASON"] = "push_rotation_wait_2turns"
        os.environ["ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY"] = "1"
        os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "1"
        os.environ["ENTRY_LIMIT_ALLOW_WITHOUT_BOARD"] = "0"
        ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY = True
        ENTRY_ORDER_BOARD_RETRY_SEC = _fob_safe_float(os.environ.get("ENTRY_ORDER_BOARD_RETRY_SEC"), retry_sec)
        ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC = _fob_safe_float(os.environ.get("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"), 0.2)
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] env/constant apply failed")


def _build_entry_order_fob(*args, **kwargs):
    if not _fob_scope_enabled():
        return _build_entry_order_core(*args, **kwargs)
    summary_like = _fob_is_summary_ai_order(kwargs)
    symbol = kwargs.get("symbol")
    side = kwargs.get("side")
    if summary_like:
        logger.info(
            "[SUMMARY AI FAST ORDER BUILDER] start symbol=%s side=%s source=%s retry_sec=%s retry_interval=%s reason=push_rotation_wait_2turns",
            symbol, side, kwargs.get("source"),
            globals().get("ENTRY_ORDER_BOARD_RETRY_SEC"),
            globals().get("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"),
        )
        _fob_coerce_summary_ai_snapshot_order(kwargs)
        _fob_repair_summary_ai_low_move_range(kwargs)
    result = _build_entry_order_core(*args, **kwargs)
    if summary_like:
        if isinstance(result, dict) and result.get("reason") == "STRICT_BOARD_MISSING":
            logger.warning(
                "[SUMMARY AI FAST ORDER BUILDER] board missing hard block kept after push rotation wait symbol=%s side=%s retry_sec=%s detail=%s",
                symbol, side, globals().get("ENTRY_ORDER_BOARD_RETRY_SEC"), result.get("detail"),
            )
        logger.warning(
            "[SUMMARY AI FAST ORDER BUILDER] done symbol=%s side=%s ok=%s reason=%s detail=%s",
            symbol, side,
            isinstance(result, dict) and result.get("ok"),
            result.get("reason") if isinstance(result, dict) else type(result).__name__,
            result.get("detail") if isinstance(result, dict) else None,
        )
    return result


_fob_apply_env_and_constants()


# ============================================================
# BREAKOUT SUPPORT/RESISTANCE GUARD (発注側)
# (旧 core/startup/breakout_support_resistance_guard_patch.py を本文化)
# ------------------------------------------------------------
# 支持線/抵抗線の直前や未確定ブレイクでの発注を止める。
# 実 install 順の測定で、この層は build_entry_order を包む 7 パッチの
# うち fast_order_builder のすぐ外側だった。同じ位置に置いている:
#   _build_entry_order_core (native)
#     <- _build_entry_order_fob   (SUMMARY_AI fast path)
#       <- build_entry_order      (この層)
# ============================================================

_BSR_VERSION = "V1-BREAKOUT-SUPPORT-RESISTANCE-GUARD"
_BSR__INSTALLED = False
_BSR__ORIG_BASE_FILTER = None
_BSR__ORIG_SORT_KEY = None
_BSR__ORIG_BUILD_ORDER = None


def _bsr_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _bsr_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _bsr_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _bsr_norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return "BUY"


def _bsr_as_dict(v: Any) -> dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _bsr_iter_sources(item: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield item
    for k in ("entry_row", "source_row", "ai_row", "row", "conditions", "entry_conditions"):
        d = item.get(k)
        if isinstance(d, dict):
            yield d


def _bsr_first(item: dict[str, Any], names: tuple[str, ...]) -> float:
    for d in _bsr_iter_sources(item):
        for n in names:
            x = _bsr_safe_float(d.get(n), 0.0)
            if x > 0:
                return x
    return 0.0


def _bsr_pick_symbol(item: dict[str, Any]) -> str:
    for d in _bsr_iter_sources(item):
        v = d.get("symbol") or d.get("Symbol") or d.get("code") or d.get("銘柄コード")
        if v:
            s = str(v).strip()
            if s.endswith(".0") and s[:-2].isdigit():
                s = s[:-2]
            return s
    return ""


def _bsr_pick_side(item: dict[str, Any]) -> str:
    for d in _bsr_iter_sources(item):
        v = d.get("side") or d.get("entry_decision") or d.get("ai_side")
        if v:
            return _bsr_norm_side(v)
    return "BUY"


def _bsr_pick_price(item: dict[str, Any]) -> float:
    return _bsr_first(item, ("price", "close_price", "current_price", "close", "last_price", "display_price", "CurrentPrice"))


def _bsr_pick_volume(item: dict[str, Any]) -> float:
    return _bsr_first(item, ("volume", "vol", "latest_volume", "display_volume", "Volume", "TradingVolume"))


def _bsr_pick_recent_avg_volume(item: dict[str, Any]) -> float:
    return _bsr_first(item, ("avg_volume_5", "volume_ma5", "avg_5m_volume", "recent_avg_volume", "volume_avg", "rolling_volume_avg"))


def _bsr_pick_level(item: dict[str, Any], names: tuple[str, ...]) -> float:
    return _bsr_first(item, names)


def _bsr_level_names() -> dict[str, tuple[str, ...]]:
    return {
        "prev_high": ("prev_high", "previous_high", "prev_day_high", "previous_day_high", "yesterday_high", "prior_high", "daily_prev_high", "前日高値"),
        "prev_low": ("prev_low", "previous_low", "prev_day_low", "previous_day_low", "yesterday_low", "prior_low", "daily_prev_low", "前日安値"),
        "resistance": ("resistance", "resistance_price", "upper_resistance", "recent_high", "recent_swing_high", "breakout_high", "rolling_high", "high_20", "high_10", "day_high", "today_high", "session_high", "intraday_high"),
        "support": ("support", "support_price", "lower_support", "recent_low", "recent_swing_low", "breakdown_low", "rolling_low", "low_20", "low_10", "day_low", "today_low", "session_low", "intraday_low"),
    }


def _bsr_score(item: dict[str, Any]) -> float:
    side = _bsr_pick_side(item)
    keys = ("sell_score", "score_total", "final_score", "score") if side == "SELL" else ("buy_score", "score_total", "final_score", "score")
    return max(_bsr_safe_float(item.get(k), 0.0) for k in keys)


def _bsr_diagnose(item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    side = _bsr_pick_side(item)
    symbol = _bsr_pick_symbol(item)
    price = _bsr_pick_price(item)
    if price <= 0:
        return True, {"reason": "no_price", "symbol": symbol, "side": side}
    names = _bsr_level_names()
    prev_high = _bsr_pick_level(item, names["prev_high"])
    prev_low = _bsr_pick_level(item, names["prev_low"])
    resistance = max(prev_high, _bsr_pick_level(item, names["resistance"]))
    support_candidates = [x for x in (prev_low, _bsr_pick_level(item, names["support"])) if x > 0]
    support = min(support_candidates) if support_candidates else 0.0
    near_pct = _bsr_env_float("BREAKOUT_SR_NEAR_BLOCK_PCT", 0.0015)
    break_pct = _bsr_env_float("BREAKOUT_SR_BREAK_CONFIRM_PCT", 0.0005)
    strong_break_pct = _bsr_env_float("BREAKOUT_SR_STRONG_BREAK_PCT", 0.0020)
    require_volume = _bsr_env_bool("BREAKOUT_SR_REQUIRE_VOLUME_FOR_BREAK", False)
    vol = _bsr_pick_volume(item)
    avg_vol = _bsr_pick_recent_avg_volume(item)
    volume_ok = True
    if require_volume and avg_vol > 0 and vol > 0:
        volume_ok = vol >= avg_vol * _bsr_env_float("BREAKOUT_SR_VOLUME_RATIO", 1.15)
    detail = {
        "symbol": symbol,
        "side": side,
        "price": price,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "resistance": resistance,
        "support": support,
        "near_pct": near_pct,
        "break_pct": break_pct,
        "strong_break_pct": strong_break_pct,
        "volume": vol,
        "avg_volume": avg_vol,
        "volume_ok": volume_ok,
        "score": _bsr_score(item),
    }
    if side == "BUY":
        if resistance > 0:
            gap = (resistance - price) / price
            break_margin = (price - resistance) / resistance if resistance > 0 else 0.0
            detail.update({"gap_to_resistance_pct": gap, "break_margin_pct": break_margin})
            if 0 <= gap <= near_pct:
                detail["reason"] = "near_resistance_block"
                return False, detail
            if break_margin >= break_pct and volume_ok:
                detail["reason"] = "resistance_breakout_ok"
                return True, detail
            if break_margin >= strong_break_pct:
                detail["reason"] = "strong_resistance_breakout_ok"
                return True, detail
        detail["reason"] = "no_resistance_level_or_not_near"
        return True, detail
    # SELL
    if support > 0:
        gap = (price - support) / price
        break_margin = (support - price) / support if support > 0 else 0.0
        detail.update({"gap_to_support_pct": gap, "break_margin_pct": break_margin})
        if 0 <= gap <= near_pct:
            detail["reason"] = "near_support_block"
            return False, detail
        if break_margin >= break_pct and volume_ok:
            detail["reason"] = "support_breakdown_ok"
            return True, detail
        if break_margin >= strong_break_pct:
            detail["reason"] = "strong_support_breakdown_ok"
            return True, detail
    detail["reason"] = "no_support_level_or_not_near"
    return True, detail


def _bsr_breakout_bonus(item: dict[str, Any]) -> float:
    if not isinstance(item, dict):
        return 0.0
    ok, detail = _bsr_diagnose(item)
    reason = str(detail.get("reason") or "")
    if not ok:
        return -1.0 * _bsr_env_float("BREAKOUT_SR_BLOCK_SORT_PENALTY", 1000.0)
    if "strong_" in reason:
        return _bsr_env_float("BREAKOUT_SR_STRONG_BONUS", 0.30)
    if "breakout_ok" in reason or "breakdown_ok" in reason:
        return _bsr_env_float("BREAKOUT_SR_BREAK_BONUS", 0.15)
    return 0.0


def _build_entry_order_bsr(*args, **kwargs):
    try:
        row = kwargs.get("entry_row") if isinstance(kwargs.get("entry_row"), dict) else {}
        item = dict(row)
        item.setdefault("symbol", kwargs.get("symbol"))
        item.setdefault("side", kwargs.get("side"))
        if _bsr_env_bool("BREAKOUT_SR_ORDER_BUILDER_GUARD", True) and item:
            ok, detail = _bsr_diagnose(item)
            if not ok:
                logger.warning("[BREAKOUT SR GUARD] ORDER_BUILD_NG detail=%s", detail)
                return {"ok": False, "reason": str(detail.get("reason") or "BREAKOUT_SR_GUARD_NG").upper(), "detail": detail}
            row["breakout_sr_detail"] = detail
            row["breakout_sr_reason"] = detail.get("reason")
    except Exception:
        logger.debug("[BREAKOUT SR GUARD] order builder precheck failed; fail-open", exc_info=True)
    return _build_entry_order_fob(*args, **kwargs)


# ============================================================
# ENTRY PRICE IMPROVEMENT
# (旧 core/startup/entry_price_improvement_patch.py を本文化)
# ------------------------------------------------------------
# 出来上がった注文の指値を、板と地合いに応じて 1〜数ティック有利側へ寄せる。
# 実 install 順の測定で、この層は breakout SR ガードのすぐ外側だった。
# 同じ位置に置いている:
#   _build_entry_order_core (native)
#     <- _build_entry_order_fob   (SUMMARY_AI fast path)
#       <- _build_entry_order_bsr (支持線/抵抗線ガード)
#         <- build_entry_order    (この層)
#
# 旧 install の _chain_contains / _is_currently_wrapped は、
# board_touch ラッパーとの二重ラップ・循環を避けるための防御だった。
# 本文化して固定の呼び出し順になったので不要。
# ============================================================

_EPI__INSTALLED = False
_EPI__ORIG_BUILD_ENTRY_ORDER = None


def _epi_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _epi_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _epi_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _epi_first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _epi_row_to_dict(row: Any) -> dict:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _epi_norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _epi_norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _epi_get_tick_size(price: float) -> float:
    try:
        from utils_common import get_tick_size
        tick = float(get_tick_size(float(price)))
        if tick > 0:
            return tick
    except Exception:
        pass
    return 1.0


def _epi_round_to_tick(price: float, *, side: str) -> float:
    tick = _epi_get_tick_size(price)
    if tick <= 0:
        tick = 1.0
    if side == "BUY":
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick


def _epi_get_board(symbol: str, row: dict | None = None) -> dict:
    row = row or {}
    bid = _epi_safe_float(_epi_first(row, ("bid_price", "bid", "best_bid", "BidPrice"), 0.0), 0.0)
    ask = _epi_safe_float(_epi_first(row, ("ask_price", "ask", "best_ask", "AskPrice"), 0.0), 0.0)
    bid_qty = _epi_safe_float(_epi_first(row, ("bid_qty", "best_bid_qty", "BidQty", "bid_volume"), 0.0), 0.0)
    ask_qty = _epi_safe_float(_epi_first(row, ("ask_qty", "best_ask_qty", "AskQty", "ask_volume"), 0.0), 0.0)

    if bid > 0 and ask > 0:
        return {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "source": "entry_row"}

    try:
        from utils_common import get_latest_bid_ask
        q = get_latest_bid_ask(symbol)
        if isinstance(q, dict):
            bid = _epi_safe_float(q.get("bid_price") or q.get("bid") or q.get("best_bid") or q.get("BidPrice"), 0.0)
            ask = _epi_safe_float(q.get("ask_price") or q.get("ask") or q.get("best_ask") or q.get("AskPrice"), 0.0)
            bid_qty = _epi_safe_float(q.get("bid_qty") or q.get("best_bid_qty") or q.get("BidQty") or q.get("bid_volume"), 0.0)
            ask_qty = _epi_safe_float(q.get("ask_qty") or q.get("best_ask_qty") or q.get("AskQty") or q.get("ask_volume"), 0.0)
        elif isinstance(q, (list, tuple)) and len(q) >= 2:
            bid = _epi_safe_float(q[0], 0.0)
            ask = _epi_safe_float(q[1], 0.0)
        if bid > 0 and ask > 0:
            return {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "source": "get_latest_bid_ask"}
    except Exception:
        logger.debug("[ENTRY PRICE IMPROVE] get_latest_bid_ask failed symbol=%s", symbol, exc_info=True)

    return {"bid": 0.0, "ask": 0.0, "bid_qty": 0.0, "ask_qty": 0.0, "source": "missing"}


def _epi_is_contrarian(row: dict, detail: dict) -> tuple[bool, str]:
    values = []
    for src in (row, detail):
        for k in (
            "climax_reversal_exception",
            "contrarian_reversed",
            "contrarian_half_size",
            "reverse_half_size",
        ):
            try:
                if bool(src.get(k)):
                    return True, k
            except Exception:
                pass
        for k in ("climax_type", "reverse_reason", "price_source", "entry_type", "source"):
            try:
                v = str(src.get(k) or "").lower()
                if v:
                    values.append(v)
            except Exception:
                pass
    joined = "|".join(values)
    if "contrarian" in joined or "climax" in joined or "reversal" in joined:
        return True, joined[:120]
    return False, ""


def _epi_is_strong_trend(row: dict, side: str) -> tuple[bool, str]:
    score_buy = _epi_safe_float(_epi_first(row, ("score_buy", "buy_score", "disp_buy_score", "buy"), 0.0), 0.0)
    score_sell = _epi_safe_float(_epi_first(row, ("score_sell", "sell_score", "disp_sell_score", "sell"), 0.0), 0.0)
    slope = _epi_safe_float(_epi_first(row, ("slope_atr_scaled", "score_slope", "disp_slope", "slope"), 0.0), 0.0)
    pc3 = _epi_safe_float(_epi_first(row, ("price_change_3", "change_3", "ret_3", "return_3"), 0.0), 0.0)
    pc5 = _epi_safe_float(_epi_first(row, ("price_change_5", "change_5", "ret_5", "return_5"), 0.0), 0.0)

    if abs(pc3) <= 1.0:
        pc3 *= 100.0
    if abs(pc5) <= 1.0:
        pc5 *= 100.0

    min_score = _epi_env_float("ENTRY_PRICE_IMPROVE_STRONG_SCORE", 7.0)
    min_slope = _epi_env_float("ENTRY_PRICE_IMPROVE_STRONG_SLOPE", 0.03)
    min_recent = _epi_env_float("ENTRY_PRICE_IMPROVE_STRONG_RECENT_PCT", 0.0)

    if side == "BUY":
        ok = score_buy >= min_score and slope >= min_slope and pc3 >= min_recent and pc5 >= min_recent
        return ok, f"score_buy={score_buy:.3f},slope={slope:.6f},pc3={pc3:.3f},pc5={pc5:.3f}"

    ok = score_sell >= min_score and slope <= -min_slope and pc3 <= -min_recent and pc5 <= -min_recent
    return ok, f"score_sell={score_sell:.3f},slope={slope:.6f},pc3={pc3:.3f},pc5={pc5:.3f}"


def _epi_improved_price(*, symbol: str, side: str, row: dict, detail: dict) -> tuple[float, str, str, dict]:
    board = _epi_get_board(symbol, row)
    bid = _epi_safe_float(board.get("bid"), 0.0)
    ask = _epi_safe_float(board.get("ask"), 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return 0.0, "KEEP_ORIGINAL", "board_missing", board

    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    max_spread = _epi_env_float("ENTRY_PRICE_IMPROVE_MAX_SPREAD_PCT", 0.15)
    if spread_pct > max_spread:
        return 0.0, "KEEP_ORIGINAL", f"spread_too_wide:{spread_pct:.4f}>{max_spread:.4f}", {**board, "spread_pct": spread_pct}

    tick = _epi_get_tick_size(mid)
    passive_tick_offset = int(_epi_env_float("ENTRY_PRICE_IMPROVE_PASSIVE_TICKS", 0.0))
    normal_tick_offset = int(_epi_env_float("ENTRY_PRICE_IMPROVE_NORMAL_TICKS", 1.0))

    contrarian, contra_reason = _epi_is_contrarian(row, detail)
    strong, strong_reason = _epi_is_strong_trend(row, side)

    if side == "BUY":
        if contrarian:
            price = min(bid + tick * max(0, passive_tick_offset), ask)
            mode = "PASSIVE_BID"
            reason = f"contrarian:{contra_reason}"
        elif strong and _epi_env_bool("ENTRY_PRICE_IMPROVE_ALLOW_AGGRESSIVE_STRONG_TREND", True):
            price = ask
            mode = "AGGRESSIVE_ASK"
            reason = f"strong_trend:{strong_reason}"
        else:
            price = min(bid + tick * max(0, normal_tick_offset), ask)
            mode = "BID_PLUS_TICK"
            reason = "normal_price_improve"
        return _epi_round_to_tick(price, side="BUY"), mode, reason, {**board, "spread_pct": spread_pct, "tick": tick}

    if contrarian:
        price = max(ask - tick * max(0, passive_tick_offset), bid)
        mode = "PASSIVE_ASK"
        reason = f"contrarian:{contra_reason}"
    elif strong and _epi_env_bool("ENTRY_PRICE_IMPROVE_ALLOW_AGGRESSIVE_STRONG_TREND", True):
        price = bid
        mode = "AGGRESSIVE_BID"
        reason = f"strong_trend:{strong_reason}"
    else:
        price = max(ask - tick * max(0, normal_tick_offset), bid)
        mode = "ASK_MINUS_TICK"
        reason = "normal_price_improve"
    return _epi_round_to_tick(price, side="SELL"), mode, reason, {**board, "spread_pct": spread_pct, "tick": tick}


def _epi_patch_order_result(result: dict, *, symbol: str, side: str, source: str, entry_row: Any) -> dict:
    if not _epi_env_bool("ENTRY_PRICE_IMPROVEMENT_ENABLED", True):
        return result
    if not isinstance(result, dict) or not result.get("ok"):
        return result

    detail = result.get("detail")
    if not isinstance(detail, dict):
        return result

    order_type = str(detail.get("order_type") or "").upper()
    if order_type != "LIMIT":
        return result

    row = _epi_row_to_dict(entry_row)
    side_n = _epi_norm_side(side)
    symbol_n = _epi_norm_symbol(symbol or _epi_first(row, ("symbol", "code", "stock_code"), ""))
    if side_n not in {"BUY", "SELL"}:
        return result

    old_price = _epi_safe_float(detail.get("price"), 0.0)
    new_price, mode, reason, board = _epi_improved_price(symbol=symbol_n, side=side_n, row=row, detail=detail)
    if new_price <= 0:
        logger.info(
            "[ENTRY PRICE IMPROVE] KEEP symbol=%s side=%s source=%s old_price=%s mode=%s reason=%s board=%s",
            symbol_n,
            side_n,
            source,
            old_price,
            mode,
            reason,
            board,
        )
        detail["price_improve_mode"] = mode
        detail["price_improve_reason"] = reason
        return result

    detail["price_before_improve"] = old_price
    detail["price"] = new_price
    detail["base_price"] = new_price
    detail["price_source"] = "bid_ask_price_improvement"
    detail["price_improve_mode"] = mode
    detail["price_improve_reason"] = reason
    detail["price_improve_board"] = board
    detail["price_improved"] = True

    logger.warning(
        "[ENTRY PRICE IMPROVE] symbol=%s side=%s source=%s mode=%s old_price=%s new_price=%s bid=%s ask=%s spread_pct=%s reason=%s",
        symbol_n,
        side_n,
        source,
        mode,
        old_price,
        new_price,
        board.get("bid"),
        board.get("ask"),
        board.get("spread_pct"),
        reason,
    )
    return result


def _build_entry_order_epi(*args, **kwargs):
    result = _build_entry_order_bsr(*args, **kwargs)
    try:
        return _epi_patch_order_result(
            result,
            symbol=kwargs.get("symbol"),
            side=kwargs.get("side"),
            source=kwargs.get("source"),
            entry_row=kwargs.get("entry_row"),
        )
    except Exception:
        logger.exception("[ENTRY PRICE IMPROVE] patch failed; keep original result")
        return result


# ============================================================
# ENTRY LIMIT BOARD TOUCH
# (旧 core/startup/entry_limit_passive_runtime_patch.py を本文化)
# ------------------------------------------------------------
# SUMMARY_AI の LIMIT 注文を「BUY=ask / SELL=bid の板タッチ」に寄せ、
# 板が取れないときは fallback 指値にする。さらに SUMMARY_AI 向けの
# 厳格ガード (_elp_summary_ai_strict_guard) を通す。
#
# チェーン (すべて本ファイル内):
#   _build_entry_order_core (native)
#     <- _build_entry_order_fob  SUMMARY_AI fast path
#       <- _build_entry_order_bsr 支持線/抵抗線ガード
#         <- _build_entry_order_epi 指値の有利側寄せ
#           <- build_entry_order    この層
#
# 旧 install が行っていた定数の上書き 3 件と
# _aggressive_limit_price の差し替えは _elp_apply_constants() が担う。
# _chain_contains による二重ラップ防止は、順序が固定になったので不要。
# ============================================================

_ELP__INSTALLED = False


def _elp_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _elp_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _elp_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _elp_safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _elp_row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _elp_first(row: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _elp_is_explicit_false(v: Any) -> bool:
    try:
        if v is None:
            return False
        s = str(v).strip().lower()
        return s in {"0", "false", "no", "n", "off"}
    except Exception:
        return False


def _elp_is_true(v: Any) -> bool:
    try:
        if v is None:
            return False
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return False


def _elp_ng(reason: str, **detail: Any) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


def _elp_summary_ai_strict_guard(*, symbol: str, side: str, row: dict, detail: dict) -> Optional[Dict[str, Any]]:
    """SUMMARY_AI 新規エントリーの strict guard。

    旧 core/startup/entry_mtf_short_required_daily_optional_patch V1.4 が
    この関数を「短期MTF方向判定だけ行う」版へ差し替えていた。
    その実装は trading/handlers/short_mtf_direction_guard.py へ移設済みで、
    有効なプロセスではそちらを使う。
    """
    from trading.handlers import short_mtf_direction_guard as _short_mtf

    if _short_mtf.short_mtf_guard_enabled():
        return _short_mtf.summary_ai_strict_guard(symbol=symbol, side=side, row=row, detail=detail)
    return _elp_summary_ai_strict_guard_legacy(symbol=symbol, side=side, row=row, detail=detail)


def _elp_summary_ai_strict_guard_legacy(*, symbol: str, side: str, row: dict, detail: dict) -> Optional[Dict[str, Any]]:
    side_u = str(side or "").upper()

    board_ok = bool(detail.get("board"))
    price_source = str(detail.get("price_source") or "")
    if not board_ok or "fallback" in price_source.lower():
        allow_without_board = _elp_env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", True)
        strict_board = _elp_env_bool("ENTRY_STRICT_BOARD_REQUIRED", False)
        if strict_board and not allow_without_board:
            return _elp_ng(
                "STRICT_BOARD_MISSING",
                symbol=symbol,
                side=side_u,
                board=detail.get("board"),
                price_source=price_source,
                message="板が取れないためSUMMARY_AI新規エントリーを停止",
            )
        logger.warning(
            "[ENTRY LIMIT BOARD TOUCH] BOARD_MISSING_ALLOW symbol=%s side=%s price_source=%s strict_board=%s allow_without_board=%s",
            symbol,
            side_u,
            price_source,
            strict_board,
            allow_without_board,
        )

    if _elp_env_bool("ENTRY_STRICT_SUMMARY_REQUIRE_TECHNICAL_READY", True):
        technical_ready = row.get("technical_ready")
        usable_technical_ready = row.get("usable_technical_ready")
        if _elp_is_explicit_false(technical_ready) and not _elp_is_true(usable_technical_ready):
            return _elp_ng(
                "STRICT_TECHNICAL_NOT_READY",
                symbol=symbol,
                side=side_u,
                technical_ready=technical_ready,
                usable_technical_ready=usable_technical_ready,
                message="technical_ready=False のためSUMMARY_AI新規エントリーを停止",
            )
        if technical_ready is None:
            logger.warning(
                "[ENTRY LIMIT BOARD TOUCH] technical_ready missing -> continue to slope/MTF guard symbol=%s side=%s usable_technical_ready=%s",
                symbol,
                side_u,
                usable_technical_ready,
            )

    slope = _elp_safe_float_or_none(_elp_first(row, ("slope_atr_scaled", "slope", "score_slope", "disp_slope"), None))
    slope_eps = abs(_elp_env_float("ENTRY_STRICT_SUMMARY_SLOPE_EPS", 0.001))
    if slope is None:
        return _elp_ng("STRICT_SLOPE_MISSING", symbol=symbol, side=side_u, slope=slope)
    if side_u == "BUY" and slope <= slope_eps:
        return _elp_ng("STRICT_BUY_SLOPE_NOT_POSITIVE", symbol=symbol, side=side_u, slope=slope, min_slope=slope_eps)
    if side_u == "SELL" and slope >= -slope_eps:
        return _elp_ng("STRICT_SELL_SLOPE_NOT_NEGATIVE", symbol=symbol, side=side_u, slope=slope, max_slope=-slope_eps)

    mtf_values = {
        "mtf": _elp_safe_float_or_none(_elp_first(row, ("mtf", "score_mtf", "mtf_score", "score_mtf_merged"), None)),
        "slope_1m": _elp_safe_float_or_none(_elp_first(row, ("slope_atr_scaled_1m", "slope_1m", "slope1m"), None)),
        "slope_3m": _elp_safe_float_or_none(_elp_first(row, ("slope_atr_scaled_3m", "slope_3m", "slope3m"), None)),
        "slope_5m": _elp_safe_float_or_none(_elp_first(row, ("slope_atr_scaled_5m", "slope_5m", "slope5m"), None)),
    }
    available = {k: v for k, v in mtf_values.items() if v is not None}
    require_mtf = _elp_env_bool("ENTRY_STRICT_SUMMARY_REQUIRE_MTF_DATA", True)
    mtf_eps = abs(_elp_env_float("ENTRY_STRICT_SUMMARY_MTF_EPS", 0.0))
    if require_mtf and not available:
        return _elp_ng("STRICT_MTF_MISSING", symbol=symbol, side=side_u, values=mtf_values)
    if side_u == "BUY":
        bad = {k: v for k, v in available.items() if v < -mtf_eps}
        if bad:
            return _elp_ng("STRICT_MTF_NOT_BUY_ALIGNED", symbol=symbol, side=side_u, bad=bad, values=mtf_values)
    if side_u == "SELL":
        bad = {k: v for k, v in available.items() if v > mtf_eps}
        if bad:
            return _elp_ng("STRICT_MTF_NOT_SELL_ALIGNED", symbol=symbol, side=side_u, bad=bad, values=mtf_values)

    return None


def _elp_board_touch_limit_price(base_price: float, side: str, ticks: int = 0) -> float:
    p = _elp_safe_float(base_price, 0.0)
    if p <= 0:
        return p
    side_u = str(side or "").upper()
    return _round_price(p, side_u)


_elp_board_touch_limit_price._entry_board_touch = True  # type: ignore[attr-defined]


def _elp_apply_constants() -> None:
    """旧 install() の定数上書きと _aggressive_limit_price 差し替えを再現する。"""
    global SUMMARY_AGGRESSIVE_LIMIT_TICKS, ENTRY_ORDER_REQUIRE_MTF_DATA
    global ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF, _aggressive_limit_price
    SUMMARY_AGGRESSIVE_LIMIT_TICKS = 0
    ENTRY_ORDER_REQUIRE_MTF_DATA = _elp_env_bool("ENTRY_STRICT_SUMMARY_REQUIRE_MTF_DATA", True)
    ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF = False
    _aggressive_limit_price = _elp_board_touch_limit_price


def _build_entry_order_elp(*args, **kwargs):
    ret = _build_entry_order_epi(*args, **kwargs)
    try:
        src = str(kwargs.get("source") or "").upper()
        side = str(kwargs.get("side") or "").upper()
        symbol = str(kwargs.get("symbol") or "").strip()
        row = _elp_row_to_dict(kwargs.get("entry_row"))

        if isinstance(ret, dict) and ret.get("ok"):
            detail = ret.get("detail")
            if isinstance(detail, dict) and src == "SUMMARY_AI" and detail.get("order_type") == "LIMIT":
                detail["aggressive_limit_ticks"] = 0
                detail["entry_limit_mode"] = "BOARD_TOUCH_OR_FALLBACK"
                detail["price_rule"] = "BUY=ask / SELL=bid / board_missing=fallback_limit"
                if detail.get("board"):
                    detail["price_source"] = "board_bid_ask_touch"
                else:
                    detail["price_source"] = "summary_fallback_touch"

                strict_ng = _elp_summary_ai_strict_guard(symbol=symbol, side=side, row=row, detail=detail)
                if strict_ng is not None:
                    logger.warning(
                        "[ENTRY LIMIT BOARD TOUCH] NG symbol=%s side=%s reason=%s detail=%s",
                        symbol, side, strict_ng.get("reason"), strict_ng.get("detail"),
                    )
                    return strict_ng

                logger.warning(
                    "[ENTRY LIMIT BOARD TOUCH] symbol=%s side=%s price=%s base=%s rule=%s guard=OK",
                    symbol, side, detail.get("price"), detail.get("base_price"), detail.get("price_rule"),
                )
    except Exception:
        logger.debug("[ENTRY LIMIT BOARD TOUCH] detail patch failed", exc_info=True)
    return ret


_elp_apply_constants()


# ------------------------------------------------------------
# entry_order_mtf_slope_fill_patch について
# ------------------------------------------------------------
# 同パッチの build_entry_order ラッパー (発注前に entry_row の 3m/5m 傾きと
# high/low を summary DB から補う前処理) は **移植していない**。
#
# 実 install 順で直後に入る entry_order_short_mtf_neutral_rescue_patch が
#   _ORIGINAL = getattr(original, "_original", original)
# で 1 段剥がすため、mtf 層は捨てられて一度も実行されていなかった。
# HEAD~1 の両パッチを実 install 順で組み立てて build_entry_order を呼び、
# 前処理に到達しないことを実測で確認済み。
#
# 本文へ組み込むと「今まで動いていなかった前埋め」が有効になり、
# 前段フィルタの通過率が変わってしまうため、意図的に移していない。
# 有効化したい場合は旧パッチの _fill_entry_row 相当をここへ足すこと
# (env: 旧パッチは無条件だった)。
# ------------------------------------------------------------


# ============================================================
# SHORT MTF NEUTRAL RESCUE
# (旧 core/startup/entry_order_short_mtf_neutral_rescue_patch.py を本文化)
# ------------------------------------------------------------
# SHORT MTF 方向ガードで落ちた注文のうち、傾きが全ゼロ (= 中立) で
# スコアが足りているものを、ガードを一時的に無効化して 1 回だけ再試行する。
#
# チェーン (すべて本ファイル内):
#   _build_entry_order_core (native)
#     <- _build_entry_order_fob  SUMMARY_AI fast path
#       <- _build_entry_order_bsr 支持線/抵抗線ガード
#         <- _build_entry_order_epi 指値の有利側寄せ
#           <- _build_entry_order_elp 板タッチ + SUMMARY_AI 厳格ガード
#             <- build_entry_order    この層 (中立時の再試行)
#
# 旧 install はさらに _summary_mtf_direction_guard と
# _elp_summary_ai_strict_guard を個別にラップしていた
# (_patch_direct_guards)。それも _smr_wrap_guard() で再現する。
#
# 旧 _TempShortMtfBypass は eob モジュールを引数に取っていたが、
# 本文化後はこのモジュール自身なので sys.modules[__name__] を使う。
# ============================================================

_SMR__INSTALLED = False
_SMR__ORIGINAL = None


def _smr_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _smr_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _smr_sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _smr_first(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return None


def _smr_norm(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _smr_score_for_side(row: dict[str, Any], side: str) -> float:
    side_u = _smr_norm(side)
    if side_u == "BUY":
        return max(
            _smr_sf(row.get("score_buy")),
            _smr_sf(row.get("buy_score")),
            _smr_sf(row.get("score")),
            _smr_sf(row.get("final_score")),
            _smr_sf(row.get("display_score")),
        )
    if side_u == "SELL":
        return max(
            _smr_sf(row.get("score_sell")),
            _smr_sf(row.get("sell_score")),
            abs(_smr_sf(row.get("score"))),
            abs(_smr_sf(row.get("final_score"))),
            abs(_smr_sf(row.get("display_score"))),
        )
    return 0.0


def _smr_slope_values(row: dict[str, Any]) -> dict[str, float]:
    return {
        "slope_1m": _smr_sf(_smr_first(row, "slope_atr_scaled_1m", "slope_1m", "slope1m", "slope_atr_scaled", "slope", "score_slope")),
        "slope_3m": _smr_sf(_smr_first(row, "slope_atr_scaled_3m", "slope_3m", "slope3m")),
        "slope_5m": _smr_sf(_smr_first(row, "slope_atr_scaled_5m", "slope_5m", "slope5m")),
    }


def _smr_is_all_zero_slopes(slopes: dict[str, float]) -> bool:
    eps = abs(_smr_env_float("ENTRY_ORDER_SHORT_MTF_ZERO_EPS", 0.0000001))
    return all(abs(float(v or 0.0)) <= eps for v in slopes.values())


def _smr_is_short_mtf_reason(reason: Any) -> bool:
    s = _smr_norm(reason)
    return s in {
        "SHORT_MTF_NOT_BUY_ALIGNED",
        "SHORT_MTF_NOT_SELL_ALIGNED",
        "MTF_NOT_BUY_ALIGNED",
        "MTF_NOT_SELL_ALIGNED",
    }


def _smr_is_summary_ai_like(row: dict[str, Any], source: str) -> bool:
    src = _smr_norm(source or row.get("source"))
    et = _smr_norm(row.get("entry_type"))
    return src in {"SUMMARY_AI", "SUMMARY"} or et == "SUMMARY_AI"


def _smr_can_rescue(row: dict[str, Any], *, symbol: str, side: str, source: str, result: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    if not _smr_env_bool("ENTRY_ORDER_SHORT_MTF_NEUTRAL_RESCUE", True):
        return False, {"reason": "disabled"}
    if not isinstance(row, dict):
        return False, {"reason": "row_not_dict"}
    if not _smr_is_summary_ai_like(row, source):
        return False, {"reason": "not_summary_ai_like", "source": source, "entry_type": row.get("entry_type")}

    side_u = _smr_norm(side or row.get("side") or row.get("entry_decision"))
    if side_u not in {"BUY", "SELL"}:
        return False, {"reason": "bad_side", "side": side}

    score = _smr_score_for_side(row, side_u)
    min_score = _smr_env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_MIN_SCORE", 1.0)
    if score < min_score:
        return False, {"reason": "score_low", "score": score, "min_score": min_score}

    eps = abs(_smr_env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_EPS", 0.0))
    slopes = _smr_slope_values(row)

    # すべて0.0は反対方向ではなく、未確定・中立として通す。
    if _smr_env_bool("ENTRY_ORDER_ALLOW_ALL_ZERO_SHORT_MTF", True) and _smr_is_all_zero_slopes(slopes):
        return True, {
            "reason": "all_zero_short_mtf_neutral_rescue",
            "symbol": symbol or row.get("symbol"),
            "side": side_u,
            "score": score,
            "min_score": min_score,
            "slopes": slopes,
            "eps": eps,
            "source": source or row.get("source"),
            "entry_type": row.get("entry_type"),
            "original_reason": result.get("reason") if isinstance(result, dict) else None,
        }

    s1 = slopes["slope_1m"]
    if side_u == "BUY":
        aligned_1m = s1 > eps
        hard_opposite = {k: v for k, v in slopes.items() if k != "slope_1m" and v < -eps}
    else:
        aligned_1m = s1 < -eps
        hard_opposite = {k: v for k, v in slopes.items() if k != "slope_1m" and v > eps}

    if not aligned_1m:
        return False, {"reason": "one_min_not_aligned", "slopes": slopes, "eps": eps, "score": score}
    if hard_opposite:
        return False, {"reason": "higher_tf_opposite", "hard_opposite": hard_opposite, "slopes": slopes, "eps": eps, "score": score}

    return True, {
        "reason": "short_mtf_neutral_rescue",
        "symbol": symbol or row.get("symbol"),
        "side": side_u,
        "score": score,
        "min_score": min_score,
        "slopes": slopes,
        "eps": eps,
        "source": source or row.get("source"),
        "entry_type": row.get("entry_type"),
        "original_reason": result.get("reason") if isinstance(result, dict) else None,
    }


def _smr_row_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    row = kwargs.get("entry_row") or kwargs.get("row")
    if isinstance(row, dict):
        return row
    for a in args:
        if isinstance(a, dict):
            return a
    return {}


def _smr_context_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[dict[str, Any], str, str, str]:
    row = _smr_row_from_args(args, kwargs)
    symbol = str(kwargs.get("symbol") or row.get("symbol") or "")
    side = str(kwargs.get("side") or row.get("side") or row.get("entry_decision") or "")
    source = str(kwargs.get("source") or row.get("source") or row.get("entry_type") or "")
    return row, symbol, side, source


class _smr_TempShortMtfBypass:
    def __init__(self):
        self.eob = sys.modules[__name__]
        self.attrs: dict[tuple[Any, str], Any] = {}
        self.env: dict[str, str | None] = {}

    @staticmethod
    def _noop(*_args: Any, **_kwargs: Any):
        return None

    def __enter__(self):
        for obj, name, value in [
            (self.eob, "ENTRY_ORDER_MTF_GUARD_ENABLED", False),
            (self.eob, "ENTRY_ORDER_REQUIRE_MTF_DATA", False),
        ]:
            try:
                self.attrs[(obj, name)] = getattr(obj, name)
                setattr(obj, name, value)
            except Exception:
                pass
        # entry_limit_passive_runtime_patch は entry_order_builder へ本文化済み。
        for obj_name, guard_name in (("trading.handlers.entry_order_builder", "_elp_summary_ai_strict_guard"),):
            try:
                mod = __import__(obj_name, fromlist=["dummy"])
                old_strict = getattr(mod, guard_name, None)
                if callable(old_strict):
                    self.attrs[(mod, guard_name)] = old_strict
                    setattr(mod, guard_name, self._noop)
            except Exception:
                pass
        for name, value in {
            "ENTRY_ORDER_MTF_GUARD_ENABLED": "0",
            "ENTRY_ORDER_REQUIRE_MTF_DATA": "0",
            "ENTRY_SHORT_MTF_REQUIRED": "0",
            "ENTRY_SHORT_MTF_FORCE_2OF3": "0",
            "ENTRY_SHORT_MTF_REQUIRE_ALL": "0",
            "ENTRY_SHORT_MTF_MIN_ALIGNED": "1",
            "ENTRY_SHORT_MTF_MIN_AVAILABLE": "1",
        }.items():
            self.env[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, exc_type, exc, tb):
        for (obj, name), value in self.attrs.items():
            try:
                setattr(obj, name, value)
            except Exception:
                pass
        for name, value in self.env.items():
            try:
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            except Exception:
                pass
        return False


def _smr_wrap_guard(name: str) -> bool:
    """旧 _patch_guard_function 相当。同じモジュール内の guard を包む。"""
    old = globals().get(name)
    if not callable(old) or getattr(old, "_short_mtf_neutral_guard_v5", False):
        return False
    original = getattr(old, "_original", old)

    def guard_wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            if result is None:
                return None
            if isinstance(result, dict) and not _smr_is_short_mtf_reason(result.get("reason")):
                return result
            row, symbol, side, source = _smr_context_from_args(args, kwargs)
            ok, detail = _smr_can_rescue(row, symbol=symbol, side=side, source=source,
                                         result=result if isinstance(result, dict) else None)
            if ok:
                logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] guard bypass v5 target=%s detail=%s original=%s", name, detail, result)
                return None
        except Exception:
            logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] guard wrapper failed target=%s", name)
        return result

    guard_wrapper._short_mtf_neutral_guard_v5 = True  # type: ignore[attr-defined]
    guard_wrapper._original = original  # type: ignore[attr-defined]
    globals()[name] = guard_wrapper
    return True


def _build_entry_order_smr(*args, **kwargs):
    result = _build_entry_order_elp(*args, **kwargs)
    try:
        if not isinstance(result, dict) or bool(result.get("ok")):
            return result
        if not _smr_is_short_mtf_reason(result.get("reason")):
            return result

        row, symbol, side, source = _smr_context_from_args(args, kwargs)
        if not row:
            return result

        ok, detail = _smr_can_rescue(row, symbol=symbol, side=side, source=source, result=result)
        if not ok:
            logger.info("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] no_rescue detail=%s result=%s", detail, result)
            return result

        with _smr_TempShortMtfBypass():
            retry = _build_entry_order_elp(*args, **kwargs)

        if isinstance(retry, dict) and bool(retry.get("ok")):
            retry = dict(retry)
            d = dict(retry.get("detail") or {})
            d["short_mtf_neutral_rescue"] = True
            d["short_mtf_neutral_rescue_detail"] = detail
            retry["detail"] = d
            logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] rescued v5 detail=%s retry=%s", detail, retry)
            return retry

        logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] retry_still_ng_v5 detail=%s retry=%s", detail, retry)
        return retry
    except Exception:
        logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] wrapper failed; return original result")
        return result


def apply_short_mtf_guard_wraps() -> bool:
    """旧 entry_order_short_mtf_neutral_rescue_patch._patch_direct_guards 相当。

    **import 時ではなく、旧 install と同じ位置 (main.py の patches リスト)
    から呼ぶこと。** entry_ma5_breakout_count_patch (main.py:249) が同じ
    guard を包んでおり、旧構成では
      ma5 が先 -> short_mtf_rescue が後 (= 外側)
    で、rescue が ma5 の NG を解除できる関係だった。
    import 時に掛けると順序が反転して解除できなくなる。
    """
    ok = False
    for name in ("_summary_mtf_direction_guard", "_elp_summary_ai_strict_guard"):
        try:
            ok = _smr_wrap_guard(name) or ok
        except Exception:
            logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] guard wrap failed name=%s", name)
    return ok


# ============================================================
# REST FULL BOARD ENTRY
# (旧 core/startup/rest_full_board_entry_patch.py を本文化)
# ------------------------------------------------------------
# 出来上がった注文について kabu の REST /board を取り直し、
# 板の厚み・偏りを見て指値を決め直す (必要なら NG にする)。
# 実 install 順の測定で、この層は build_entry_order を包む 7 パッチの
# **最外周** (startup.py:1009 = main.py:435 の system_startup 経由) だった。
#
# 最終チェーン (すべて本ファイル内):
#   _build_entry_order_core (native)
#     <- _build_entry_order_fob  SUMMARY_AI fast path
#       <- _build_entry_order_bsr 支持線/抵抗線ガード
#         <- _build_entry_order_epi 指値の有利側寄せ
#           <- _build_entry_order_elp 板タッチ + SUMMARY_AI 厳格ガード
#             <- _build_entry_order_smr 中立時の再試行
#               <- build_entry_order    REST /board で指値を決め直す (この層)
#
# 有効/無効は旧 install と同じ ENTRY_REST_FULL_BOARD_ENABLED (既定 True)。
# ============================================================

_RFB__INSTALLED = False


def _rfb_env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(default)


def _rfb_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _rfb_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if not math.isnan(x) and not math.isinf(x) else float(default)
    except Exception:
        return float(default)


def _rfb_norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


_RFB_CACHE: dict[str, tuple[float, dict]] = {}
_RFB_LAST_CALL: dict[str, float] = {}


def _rfb_norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _rfb_source_enabled(source: str) -> bool:
    if not _rfb_env_bool("ENTRY_REST_FULL_BOARD_ENABLED", True):
        return False
    src = str(source or "").upper()
    allowed = {x.strip().upper() for x in os.getenv("ENTRY_REST_FULL_BOARD_SOURCES", "SUMMARY_AI,RANKING,TONOSAMA,EARLY_SCALP,ENTRY").split(",") if x.strip()}
    return "ALL" in allowed or src in allowed


def _rfb_tick(price: float) -> float:
    try:
        from utils_common import get_tick_size
        t = float(get_tick_size(price))
        return t if t > 0 else 1.0
    except Exception:
        return 1.0


def _rfb_round(price: float, side: str) -> float:
    t = _rfb_tick(price)
    return math.ceil(price / t) * t if side == "BUY" else math.floor(price / t) * t


def _rfb_get_token() -> str | None:
    try:
        from token_manager import get_valid_token, refresh_token
        return get_valid_token() or refresh_token()
    except Exception:
        logger.debug("[REST FULL BOARD] token unavailable", exc_info=True)
        return None


def _rfb_fetch_board(symbol: str, *, force: bool = False) -> dict | None:
    now = time.monotonic()
    cache_sec = _rfb_env_float("ENTRY_REST_FULL_BOARD_CACHE_SEC", 0.8)
    cached = _RFB_CACHE.get(symbol)
    if not force and cached and now - cached[0] <= cache_sec:
        out = dict(cached[1])
        out["cache_hit"] = True
        return out

    min_interval = _rfb_env_float("ENTRY_REST_FULL_BOARD_MIN_INTERVAL_SEC", 0.7)
    if not force and cached and now - _RFB_LAST_CALL.get(symbol, 0.0) < min_interval:
        out = dict(cached[1])
        out["cache_hit"] = True
        out["rate_limited_cache"] = True
        return out

    token = _rfb_get_token()
    if not token:
        return None
    base = os.getenv("KABUSAPI_BASE_URL", "http://localhost:18080/kabusapi").rstrip("/")
    exchange = os.getenv("ENTRY_REST_FULL_BOARD_EXCHANGE", "1")
    url = f"{base}/board/{urllib.parse.quote(symbol + '@' + exchange)}"
    req = urllib.request.Request(url, headers={"X-API-KEY": token}, method="GET")
    _RFB_LAST_CALL[symbol] = now
    try:
        with urllib.request.urlopen(req, timeout=_rfb_env_float("ENTRY_REST_FULL_BOARD_TIMEOUT_SEC", 0.8)) as res:
            raw = json.loads(res.read().decode("utf-8", errors="replace"))
    except Exception:
        logger.debug("[REST FULL BOARD] request failed symbol=%s force=%s", symbol, force, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None

    depth = int(_rfb_env_float("ENTRY_REST_FULL_BOARD_DEPTH", 10))
    bids, asks = [], []
    for i in range(1, max(1, depth) + 1):
        b = raw.get(f"Buy{i}") if isinstance(raw.get(f"Buy{i}"), dict) else {}
        a = raw.get(f"Sell{i}") if isinstance(raw.get(f"Sell{i}"), dict) else {}
        bp, bq = _rfb_safe_float(b.get("Price")), _rfb_safe_float(b.get("Qty"))
        ap, aq = _rfb_safe_float(a.get("Price")), _rfb_safe_float(a.get("Qty"))
        if bp > 0 and bq > 0:
            bids.append({"level": i, "price": bp, "qty": bq})
        if ap > 0 and aq > 0:
            asks.append({"level": i, "price": ap, "qty": aq})

    bid = _rfb_safe_float(raw.get("BidPrice")) or (bids[0]["price"] if bids else 0.0)
    ask = _rfb_safe_float(raw.get("AskPrice")) or (asks[0]["price"] if asks else 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    board = {"bid": bid, "ask": ask, "bids": bids, "asks": asks, "source": "rest_board", "force": bool(force)}
    _RFB_CACHE[symbol] = (now, board)
    return dict(board)


def _rfb_sum_qty(levels: list[dict], n: int) -> float:
    return sum(_rfb_safe_float(x.get("qty")) for x in levels[: max(1, n)])


def _rfb_thick(levels: list[dict], fallback_price: float) -> dict:
    min_qty = _rfb_env_float("ENTRY_REST_FULL_BOARD_THICK_MIN_QTY", 500.0)
    usable = [x for x in levels if _rfb_safe_float(x.get("qty")) >= min_qty]
    src = usable or levels or [{"level": 1, "price": fallback_price, "qty": 0.0}]
    return max(src, key=lambda x: _rfb_safe_float(x.get("qty")))


def _rfb_find_level_qty(levels: list[dict], price: float) -> float:
    for lv in levels:
        if abs(_rfb_safe_float(lv.get("price")) - float(price)) < 1e-9:
            return _rfb_safe_float(lv.get("qty"))
    return 0.0


def _rfb_decide_price(board: dict, side: str) -> tuple[float, str, dict]:
    bid, ask = _rfb_safe_float(board.get("bid")), _rfb_safe_float(board.get("ask"))
    mid = (bid + ask) / 2.0
    t = _rfb_tick(mid)
    bids, asks = list(board.get("bids") or []), list(board.get("asks") or [])
    touch = _rfb_env_bool("ENTRY_REST_FULL_BOARD_ALLOW_TOUCH_ASK_BID", True)
    if side == "BUY":
        lv = _rfb_thick(bids, bid)
        p = max(_rfb_safe_float(lv.get("price")) + t, bid + t)
        p = min(p, ask if touch else max(bid, ask - t))
        mode = "REST_FULL_BOARD_BUY_THICK_BID_PLUS_TICK"
    else:
        lv = _rfb_thick(asks, ask)
        p = min(_rfb_safe_float(lv.get("price")) - t, ask - t)
        p = max(p, bid if touch else min(ask, bid + t))
        mode = "REST_FULL_BOARD_SELL_THICK_ASK_MINUS_TICK"
    n = int(_rfb_env_float("ENTRY_REST_FULL_BOARD_IMBALANCE_DEPTH", 5))
    return _rfb_round(p, side), mode, {"bid": bid, "ask": ask, "tick": t, "thick_level": lv, "bid_total_topN": _rfb_sum_qty(bids, n), "ask_total_topN": _rfb_sum_qty(asks, n), "depth": n}


def _rfb_imbalance_ng(board: dict, side: str) -> dict | None:
    if not _rfb_env_bool("ENTRY_REST_FULL_BOARD_IMBALANCE_GUARD_ENABLED", True):
        return None
    n = int(_rfb_env_float("ENTRY_REST_FULL_BOARD_IMBALANCE_DEPTH", 5))
    bids, asks = list(board.get("bids") or []), list(board.get("asks") or [])
    bid_total = _rfb_sum_qty(bids, n)
    ask_total = _rfb_sum_qty(asks, n)
    min_same = _rfb_env_float("ENTRY_REST_FULL_BOARD_MIN_SAME_SIDE_TOTAL", 300.0)
    max_opp_ratio = _rfb_env_float("ENTRY_REST_FULL_BOARD_MAX_OPPOSITE_RATIO", 2.5)
    min_ratio_denom = max(1.0, _rfb_env_float("ENTRY_REST_FULL_BOARD_RATIO_MIN_DENOM", 100.0))

    if side == "BUY":
        ratio = ask_total / max(bid_total, min_ratio_denom)
        if bid_total < min_same:
            return {"reason": "REST_FULL_BOARD_BUY_SUPPORT_TOO_THIN", "bid_total_topN": bid_total, "ask_total_topN": ask_total, "depth": n, "min_same_side_total": min_same}
        if ratio > max_opp_ratio:
            return {"reason": "REST_FULL_BOARD_BUY_SELL_WALL_TOO_HEAVY", "bid_total_topN": bid_total, "ask_total_topN": ask_total, "opposite_ratio": ratio, "max_opposite_ratio": max_opp_ratio, "depth": n}
    elif side == "SELL":
        ratio = bid_total / max(ask_total, min_ratio_denom)
        if ask_total < min_same:
            return {"reason": "REST_FULL_BOARD_SELL_RESISTANCE_TOO_THIN", "bid_total_topN": bid_total, "ask_total_topN": ask_total, "depth": n, "min_same_side_total": min_same}
        if ratio > max_opp_ratio:
            return {"reason": "REST_FULL_BOARD_SELL_BUY_WALL_TOO_HEAVY", "bid_total_topN": bid_total, "ask_total_topN": ask_total, "opposite_ratio": ratio, "max_opposite_ratio": max_opp_ratio, "depth": n}
    return None


def _rfb_double_check_ng(symbol: str, first_board: dict, side: str) -> dict | None:
    if not _rfb_env_bool("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_ENABLED", True):
        return None
    wait_sec = max(0.0, _rfb_env_float("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_WAIT_SEC", 0.25))
    remain_ratio = max(0.0, min(1.0, _rfb_env_float("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_MIN_REMAIN_RATIO", 0.60)))
    n = int(_rfb_env_float("ENTRY_REST_FULL_BOARD_IMBALANCE_DEPTH", 5))

    first_bids = list(first_board.get("bids") or [])
    first_asks = list(first_board.get("asks") or [])
    if side == "BUY":
        first_levels = first_bids
        first_total = _rfb_sum_qty(first_bids, n)
        thick1 = _rfb_thick(first_bids, _rfb_safe_float(first_board.get("bid")))
        level_key = "bid"
    else:
        first_levels = first_asks
        first_total = _rfb_sum_qty(first_asks, n)
        thick1 = _rfb_thick(first_asks, _rfb_safe_float(first_board.get("ask")))
        level_key = "ask"

    thick_price = _rfb_safe_float(thick1.get("price"))
    thick_qty1 = _rfb_safe_float(thick1.get("qty"))
    if wait_sec > 0:
        time.sleep(wait_sec)

    second = _rfb_fetch_board(symbol, force=True)
    if not second:
        if _rfb_env_bool("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_FAIL_OPEN", True):
            return None
        return {"reason": "REST_FULL_BOARD_DOUBLE_CHECK_MISSING", "wait_sec": wait_sec, "first_total_topN": first_total, "thick_level_first": thick1}

    second_bids = list(second.get("bids") or [])
    second_asks = list(second.get("asks") or [])
    if side == "BUY":
        second_levels = second_bids
        second_total = _rfb_sum_qty(second_bids, n)
    else:
        second_levels = second_asks
        second_total = _rfb_sum_qty(second_asks, n)
    thick_qty2 = _rfb_find_level_qty(second_levels, thick_price)

    total_floor = first_total * remain_ratio
    thick_floor = thick_qty1 * remain_ratio
    if first_total > 0 and second_total < total_floor:
        return {
            "reason": f"REST_FULL_BOARD_{side}_SIDE_TOTAL_DISAPPEARED",
            "wait_sec": wait_sec,
            "remain_ratio": remain_ratio,
            "first_total_topN": first_total,
            "second_total_topN": second_total,
            "depth": n,
            "level_side": level_key,
        }
    if thick_qty1 > 0 and thick_qty2 < thick_floor:
        return {
            "reason": f"REST_FULL_BOARD_{side}_THICK_LEVEL_DISAPPEARED",
            "wait_sec": wait_sec,
            "remain_ratio": remain_ratio,
            "thick_price": thick_price,
            "thick_qty_first": thick_qty1,
            "thick_qty_second": thick_qty2,
            "thick_level_first": thick1,
            "level_side": level_key,
        }
    logger.info(
        "[REST FULL BOARD] DOUBLE_CHECK_OK symbol=%s side=%s wait=%.3f first_total=%.0f second_total=%.0f thick_price=%s qty %.0f->%.0f",
        symbol, side, wait_sec, first_total, second_total, thick_price, thick_qty1, thick_qty2,
    )
    return None


def _rfb_patch_result(ret: dict, *, symbol: str, side: str, source: str) -> dict:
    if not isinstance(ret, dict) or not ret.get("ok") or not _rfb_source_enabled(source):
        return ret
    detail = ret.get("detail")
    if not isinstance(detail, dict) or str(detail.get("order_type") or "").upper() != "LIMIT":
        return ret
    sym, side = _rfb_norm_symbol(symbol), _rfb_norm_side(side)
    if side not in {"BUY", "SELL"} or not sym:
        return ret
    board = _rfb_fetch_board(sym)
    if not board:
        detail["rest_full_board"] = False
        detail["rest_full_board_reason"] = "missing_keep_existing"
        return ret
    bid, ask = _rfb_safe_float(board.get("bid")), _rfb_safe_float(board.get("ask"))
    spread_pct = ((ask - bid) / ((ask + bid) / 2.0)) * 100.0
    max_spread = _rfb_env_float("ENTRY_REST_FULL_BOARD_MAX_SPREAD_PCT", _rfb_env_float("ENTRY_MAX_SPREAD_PCT", 0.15))
    if spread_pct > max_spread and _rfb_env_bool("ENTRY_REST_FULL_BOARD_STRICT_GUARD", True):
        return {"ok": False, "reason": "REST_FULL_BOARD_SPREAD_TOO_WIDE", "detail": {**detail, "bid": bid, "ask": ask, "spread_pct": spread_pct, "max_spread_pct": max_spread}}

    imbalance_ng = _rfb_imbalance_ng(board, side)
    if imbalance_ng is not None:
        if _rfb_env_bool("ENTRY_REST_FULL_BOARD_IMBALANCE_STRICT", True):
            logger.warning("[REST FULL BOARD] IMBALANCE_NG symbol=%s side=%s source=%s detail=%s", sym, side, source, imbalance_ng)
            return {"ok": False, "reason": imbalance_ng.get("reason") or "REST_FULL_BOARD_IMBALANCE_NG", "detail": {**detail, **imbalance_ng, "rest_full_board": True, "spread_pct": spread_pct}}
        detail["rest_full_board_imbalance_warning"] = imbalance_ng

    double_ng = _rfb_double_check_ng(sym, board, side)
    if double_ng is not None:
        if _rfb_env_bool("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_STRICT", True):
            logger.warning("[REST FULL BOARD] DOUBLE_CHECK_NG symbol=%s side=%s source=%s detail=%s", sym, side, source, double_ng)
            return {"ok": False, "reason": double_ng.get("reason") or "REST_FULL_BOARD_DOUBLE_CHECK_NG", "detail": {**detail, **double_ng, "rest_full_board": True, "spread_pct": spread_pct}}
        detail["rest_full_board_double_check_warning"] = double_ng

    old = _rfb_safe_float(detail.get("price"))
    new, mode, meta = _rfb_decide_price(board, side)
    if new <= 0:
        return ret
    detail["price_before_rest_full_board"] = old
    detail["price"] = new
    detail["base_price"] = new
    detail["price_source"] = "rest_full_board_price"
    detail["rest_full_board"] = True
    detail["rest_full_board_mode"] = mode
    detail["rest_full_board_meta"] = {**meta, "spread_pct": spread_pct}
    logger.warning("[REST FULL BOARD] symbol=%s side=%s source=%s mode=%s old=%s new=%s meta=%s cache=%s", sym, side, source, mode, old, new, detail["rest_full_board_meta"], bool(board.get("cache_hit")))
    return ret


def build_entry_order(*args, **kwargs):
    ret = _build_entry_order_smr(*args, **kwargs)
    if not _rfb_env_bool("ENTRY_REST_FULL_BOARD_ENABLED", True):
        return ret
    try:
        return _rfb_patch_result(
            ret,
            symbol=kwargs.get("symbol", ""),
            side=kwargs.get("side", ""),
            source=kwargs.get("source", ""),
        )
    except Exception:
        logger.exception("[REST FULL BOARD] patch failed; keep original")
        return ret


# ============================================================
# ENTRY MA5 BREAKOUT COUNT GUARD
# (旧 core/startup/entry_ma5_breakout_count_patch.py を本文化)
# ------------------------------------------------------------
# MA5 を割って/超えてから何本目かを数え、想定レンジ外のブレイクや
# ヒゲだけの往復を弾く。旧パッチは
#   entry_order_builder._summary_mtf_direction_guard
#   entry_order_builder._elp_summary_ai_strict_guard
# の 2 つのガード関数を包み、「内側が通った (None) ときだけ」自分の
# 判定を足す形だった。同じ形で本文へ入れてある。
#
# **順序に注意**: entry_order_short_mtf_neutral_rescue の guard ラップは
# この層より外側でなければならない (rescue が MA5 の NG を解除できる関係)。
# rescue 側は apply_short_mtf_guard_wraps() として main.py の patches
# リストから呼ばれるので、import 時に掛かるこの層より必ず後になる。
# ============================================================

_M5B_PATCHED = False


def _m5b_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _m5b_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _m5b_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _m5b_norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.endswith(".T"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _m5b_ng(reason: str, **detail: Any) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


def _m5b_parse_tfs() -> list[int]:
    raw = os.getenv("ENTRY_MA5_BREAKOUT_TFS", "3,5")
    out: list[int] = []
    try:
        for x in str(raw).replace(";", ",").split(","):
            x = x.strip()
            if not x:
                continue
            v = int(float(x))
            if v in {1, 3, 5} and v not in out:
                out.append(v)
    except Exception:
        pass
    return out or [3, 5]


def _m5b_summary_db_path() -> str:
    ymd = os.getenv("KABU_TODAY") or os.getenv("TARGET_DATE") or dt.datetime.now().strftime("%Y%m%d")
    root = os.getenv("SUMMARY_DB_DIR") or r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary"
    return os.path.join(root, f"summary{ymd}.db")


def _m5b_latest_rows_from_df(df: Any, symbol: str, limit: int) -> list[dict[str, Any]]:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return []
        sym = _m5b_norm_symbol(symbol)
        work = df.copy()
        work["__sym__"] = work["symbol"].map(_m5b_norm_symbol)
        work = work[work["__sym__"] == sym].copy()
        if work.empty:
            return []
        time_col = None
        for c in ("datetime", "end_time", "start_time", "time"):
            if c in work.columns:
                time_col = c
                break
        if time_col:
            work["__dt__"] = pd.to_datetime(work[time_col], errors="coerce")
            work = work.sort_values("__dt__", kind="stable")
        return [dict(x) for x in work.tail(limit).to_dict("records")]
    except Exception:
        logger.debug("[MA5 BREAKOUT] df rows lookup failed symbol=%s", symbol, exc_info=True)
        return []


def _m5b_history_from_gc(symbol: str, tf: int, limit: int) -> list[dict[str, Any]]:
    try:
        from core.global_context.context import global_context as GC
    except Exception:
        return []

    for getter_name in ("get_push_merged_summary", "get_merged_summary", "get_summary_history", "get_push_summary"):
        try:
            getter = getattr(GC, getter_name, None)
            if not callable(getter):
                continue
            if getter_name == "get_merged_summary":
                df = getter(tf, source="push")
            else:
                df = getter(tf)
            rows = _m5b_latest_rows_from_df(df, symbol, limit)
            if rows:
                return rows
        except Exception:
            logger.debug("[MA5 BREAKOUT] GC getter failed getter=%s tf=%s symbol=%s", getter_name, tf, symbol, exc_info=True)
    return []


def _m5b_history_from_db(symbol: str, tf: int, limit: int) -> list[dict[str, Any]]:
    if not _m5b_env_bool("ENTRY_MA5_BREAKOUT_DB_BACKFILL", True):
        return []

    sym = _m5b_norm_symbol(symbol)
    today = dt.datetime.now().strftime("%Y%m%d")
    key = (sym, int(tf), today)
    if key in _m5b_db_cache:
        return _m5b_db_cache[key]

    db = _m5b_summary_db_path()
    table = f"stock_summary_{int(tf)}min"
    rows_out: list[dict[str, Any]] = []
    try:
        if not Path(db).exists():
            _m5b_db_cache[key] = []
            return []

        sql = f"""
            SELECT datetime,
                   open, open_price,
                   high, high_price,
                   low, low_price,
                   close, close_price, price,
                   ma5
              FROM {table}
             WHERE CAST(symbol AS TEXT)=?
             ORDER BY datetime DESC
             LIMIT ?
        """
        with sqlite3.connect(db, timeout=1.0) as conn:
            conn.execute("PRAGMA query_only=ON")
            got = conn.execute(sql, (sym, int(limit))).fetchall()

        for r in reversed(got or []):
            rows_out.append({
                "datetime": r[0],
                "open": r[1],
                "open_price": r[2],
                "high": r[3],
                "high_price": r[4],
                "low": r[5],
                "low_price": r[6],
                "close": r[7],
                "close_price": r[8],
                "price": r[9],
                "ma5": r[10],
            })
        if rows_out:
            logger.warning("[MA5 BREAKOUT] DB history symbol=%s tf=%s rows=%s db=%s", sym, tf, len(rows_out), db)
    except Exception as e:
        logger.debug("[MA5 BREAKOUT] DB history failed symbol=%s tf=%s err=%s", sym, tf, e, exc_info=False)

    _m5b_db_cache[key] = rows_out
    return rows_out


def _m5b_history(symbol: str, tf: int, limit: int) -> list[dict[str, Any]]:
    rows = _m5b_history_from_gc(symbol, tf, limit)
    if len(rows) >= 2:
        return rows
    db_rows = _m5b_history_from_db(symbol, tf, limit)
    if len(db_rows) >= len(rows):
        return db_rows
    return rows


def _m5b_pick_price(row: dict[str, Any]) -> float:
    for k in ("close", "close_price", "price", "current_price"):
        v = _m5b_safe_float(row.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _m5b_pick_open(row: dict[str, Any]) -> float:
    for k in ("open", "open_price"):
        v = _m5b_safe_float(row.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _m5b_pick_high(row: dict[str, Any]) -> float:
    for k in ("high", "high_price"):
        v = _m5b_safe_float(row.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _m5b_pick_low(row: dict[str, Any]) -> float:
    for k in ("low", "low_price"):
        v = _m5b_safe_float(row.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _m5b_pick_ma5(row: dict[str, Any]) -> float:
    for k in ("ma5", "MA5", "ma_5"):
        v = _m5b_safe_float(row.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _m5b_tick_size(price: float) -> float:
    """概算ティック。東証の細かい価格帯を完全再現せず、安全側でヒゲ判定に使う。"""
    p = float(price or 0.0)
    if p < 1000:
        return 1.0
    if p < 3000:
        return 1.0
    if p < 5000:
        return 5.0
    if p < 30000:
        return 10.0
    if p < 50000:
        return 50.0
    return 100.0


def _m5b_wick_info(row: dict[str, Any], side: str) -> dict[str, Any]:
    open_p = _m5b_pick_open(row)
    high = _m5b_pick_high(row)
    low = _m5b_pick_low(row)
    close = _m5b_pick_price(row)
    base = close or open_p
    tick = _m5b_tick_size(base)

    upper = max(0.0, high - max(open_p, close)) if high > 0 and open_p > 0 and close > 0 else 0.0
    lower = max(0.0, min(open_p, close) - low) if low > 0 and open_p > 0 and close > 0 else 0.0
    upper_ticks = upper / tick if tick > 0 else 0.0
    lower_ticks = lower / tick if tick > 0 else 0.0

    side_u = str(side or "").upper()
    target_ticks = upper_ticks if side_u == "BUY" else lower_ticks
    target_name = "upper_wick" if side_u == "BUY" else "lower_wick"
    return {
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "tick_size": tick,
        "upper_wick": upper,
        "lower_wick": lower,
        "upper_wick_ticks": upper_ticks,
        "lower_wick_ticks": lower_ticks,
        "target_wick_name": target_name,
        "target_wick_ticks": target_ticks,
    }


def _m5b_bars_since_ma5_break(rows: list[dict[str, Any]], side: str) -> tuple[Optional[int], dict[str, Any]]:
    if len(rows) < 2:
        return None, {"reason": "not_enough_rows", "rows": len(rows)}

    side_u = str(side or "").upper()
    states: list[dict[str, Any]] = []
    for r in rows:
        close = _m5b_pick_price(r)
        ma5 = _m5b_pick_ma5(r)
        if side_u == "SELL":
            active = bool(close > 0 and ma5 > 0 and close < ma5)
        else:
            active = bool(close > 0 and ma5 > 0 and close > ma5)
        states.append({
            "datetime": r.get("datetime") or r.get("end_time") or r.get("time"),
            "close": close,
            "ma5": ma5,
            "active": active,
            "side": side_u,
            "raw": r,
        })

    latest = states[-1]
    if not latest["active"]:
        return None, {"reason": "latest_not_broken_ma5", "latest": latest, "prev": states[-2] if len(states) >= 2 else None}

    run = 0
    for st in reversed(states):
        if bool(st["active"]):
            run += 1
        else:
            break

    break_index = len(states) - run
    prev = states[break_index - 1] if break_index - 1 >= 0 else None
    break_state = states[break_index] if 0 <= break_index < len(states) else latest
    if prev is not None and bool(prev.get("active")):
        return None, {"reason": "break_not_found_in_lookback", "run_active": run, "latest": latest}

    return run, {
        "reason": "ok",
        "bars_since_break": run,
        "latest": latest,
        "break_bar": break_state,
        "prev_before_break": prev,
    }


def _m5b_ma5_breakout_guard(entry_row: dict[str, Any], *, symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not _m5b_env_bool("ENTRY_MA5_BREAKOUT_ENABLED", True):
        return None
    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None
    if str(source or "").upper() != "SUMMARY_AI":
        return None

    sym = _m5b_norm_symbol(symbol or (entry_row or {}).get("symbol"))
    if not sym:
        return None

    min_bar = max(1, _m5b_env_int("ENTRY_MA5_BREAKOUT_MIN_BAR", 1))
    max_bar = max(min_bar, _m5b_env_int("ENTRY_MA5_BREAKOUT_MAX_BAR", 3))
    lookback = max(max_bar + 2, _m5b_env_int("ENTRY_MA5_BREAKOUT_LOOKBACK", 20))
    require_data = _m5b_env_bool("ENTRY_MA5_BREAKOUT_REQUIRE_DATA", True)
    wick_enabled = _m5b_env_bool("ENTRY_MA5_WICK_STAY_ENABLED", True)
    wick_only_first = _m5b_env_bool("ENTRY_MA5_WICK_STAY_ONLY_FIRST_BAR", True)
    wick_ticks_limit = max(0, _m5b_env_int("ENTRY_MA5_WICK_STAY_TICKS", 2))

    details: dict[str, Any] = {}
    for tf in _m5b_parse_tfs():
        rows = _m5b_history(sym, tf, lookback)
        bars, diag = _m5b_bars_since_ma5_break(rows, side_u)
        details[f"{tf}m"] = {"bars": bars, **diag}

        if bars is None:
            if require_data:
                return _m5b_ng(
                    "MA5_BREAKOUT_MISSING_OR_NOT_BROKEN",
                    symbol=sym,
                    side=side_u,
                    tf=tf,
                    min_bar=min_bar,
                    max_bar=max_bar,
                    details=details,
                )
            continue

        if not (min_bar <= int(bars) <= max_bar):
            return _m5b_ng(
                "MA5_BREAKOUT_BAR_RANGE_NG",
                symbol=sym,
                side=side_u,
                tf=tf,
                bars_since_break=int(bars),
                min_bar=min_bar,
                max_bar=max_bar,
                details=details,
            )

        if wick_enabled and wick_ticks_limit > 0:
            should_check = (not wick_only_first) or int(bars) == 1
            if should_check:
                break_bar = (diag.get("break_bar") or {}).get("raw") or {}
                wick = _m5b_wick_info(break_bar, side_u)
                details[f"{tf}m"]["wick"] = wick
                target_ticks = _m5b_safe_float(wick.get("target_wick_ticks"), 0.0)
                if target_ticks >= float(wick_ticks_limit):
                    return _m5b_ng(
                        "MA5_BREAKOUT_WICK_STAY",
                        symbol=sym,
                        side=side_u,
                        tf=tf,
                        bars_since_break=int(bars),
                        wick_ticks=target_ticks,
                        wick_limit_ticks=wick_ticks_limit,
                        wick_rule="BUY=upper_wick SELL=lower_wick",
                        details=details,
                    )

    logger.warning(
        "[MA5 BREAKOUT] OK symbol=%s side=%s tfs=%s range=%s-%s wick_enabled=%s wick_ticks=%s details=%s",
        sym,
        side_u,
        _m5b_parse_tfs(),
        min_bar,
        max_bar,
        wick_enabled,
        wick_ticks_limit,
        details,
    )
    return None


_m5b_db_cache: dict[str, tuple[float, list]] = {}


def _m5b_apply_env_defaults() -> None:
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_ENABLED", "1")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_TFS", "3,5")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_MIN_BAR", "1")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_MAX_BAR", "3")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_LOOKBACK", "20")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_REQUIRE_DATA", "1")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_DB_BACKFILL", "1")
    os.environ.setdefault("ENTRY_MA5_WICK_STAY_ENABLED", "1")
    os.environ.setdefault("ENTRY_MA5_WICK_STAY_ONLY_FIRST_BAR", "1")
    os.environ.setdefault("ENTRY_MA5_WICK_STAY_TICKS", "2")


def _m5b_wrap_guard(name: str, source_name: str) -> bool:
    """旧 _patch_guard_function 相当。内側が None のときだけ MA5 判定を足す。"""
    old = globals().get(name)
    if not callable(old) or getattr(old, "_ma5_breakout_v11", False):
        return False

    def _wrapped(*args, **kwargs):
        ret = old(*args, **kwargs)
        if ret is not None:
            return ret
        try:
            symbol = kwargs.get("symbol") or ""
            side = kwargs.get("side") or ""
            row = kwargs.get("row") or kwargs.get("entry_row") or {}
            if args and isinstance(args[0], dict) and not row:
                row = args[0]
            if not isinstance(row, dict):
                row = {}
            ng = _m5b_ma5_breakout_guard(row, symbol=symbol, side=side, source="SUMMARY_AI")
            if ng is not None:
                logger.warning(
                    "[MA5 BREAKOUT] NG source=%s symbol=%s side=%s reason=%s detail=%s",
                    source_name, symbol, side, ng.get("reason"), ng.get("detail"),
                )
                return ng
        except Exception:
            logger.debug("[MA5 BREAKOUT] wrapped guard failed source=%s", source_name, exc_info=True)
        return None

    _wrapped._ma5_breakout_v11 = True  # type: ignore[attr-defined]
    _wrapped._original = old  # type: ignore[attr-defined]
    globals()[name] = _wrapped
    return True


_m5b_apply_env_defaults()
for _m5b_name, _m5b_src in (("_summary_mtf_direction_guard", "entry_order_builder"),
                            ("_elp_summary_ai_strict_guard", "entry_limit_passive")):
    try:
        _m5b_wrap_guard(_m5b_name, _m5b_src)
    except Exception:
        logger.exception("[MA5 BREAKOUT] guard wrap failed name=%s", _m5b_name)


# ============================================================
# ENTRY FINAL FILTER 既定値
# (旧 core/startup/entry_final_filter_failopen_patch.py の env / 定数部分)
# ------------------------------------------------------------
# 旧 install が入れていた env 既定値 (setdefault のみ) と
# entry_order_builder の 4 定数の上書きをここへ移した。
# **import 時ではなく main.py の patches リストから呼ぶこと**
# (旧 install と同じ :249 の位置。tonosama_history_failclose_strict_patch の
#  watcher が同じ定数を書き戻す関係があるため、位置を変えると最終値が変わる)。
#
# range_5m_filter の fail-open ラップと atr_1m_filter のラップは
# それぞれ trading/handlers/entry_controller.py へ本文化 / 撤去済み。
# ============================================================

_EFF_ENV_DEFAULTS = {
'ENTRY_ALLOW_ENTRY_WITHOUT_BOARD':'1','ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN':'1','ATR_1M_FILTER_TONOSAMA_MIN_BARS':'14','PENDING_PROTECT_PUSH_SYMBOLS':'1','PENDING_PROTECT_PUSH_MAX_KEEP':'50','ENTRY_BOARD_RETRY_ENABLED':'1','ENTRY_BOARD_RETRY_WAIT_SEC':'4.5','ENTRY_BOARD_RETRY_COUNT':'1','ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC':'0.3','ENTRY_BOARD_RETRY_EXTRA_COUNT':'1','ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING':'0','ENTRY_SHORT_MTF_REQUIRED':'1','ENTRY_SHORT_MTF_REQUIRE_ALL':'1','ENTRY_SHORT_MTF_SLOPE_EPS':'0.0','ENTRY_DAILY_MTF_OPTIONAL':'1','ENTRY_MA5_BREAKOUT_ENABLED':'1','ENTRY_MA5_BREAKOUT_TFS':'3,5','ENTRY_MA5_BREAKOUT_MIN_BAR':'1','ENTRY_MA5_BREAKOUT_MAX_BAR':'3','ENTRY_MA5_BREAKOUT_LOOKBACK':'20','ENTRY_MA5_BREAKOUT_REQUIRE_DATA':'1','ENTRY_MA5_BREAKOUT_DB_BACKFILL':'1','ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN':'1','ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN':'0'
}

_EFF_SCALPING_DEFAULTS = {
'ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL':'2','ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY':'1','ENTRY_WINNING_SYMBOL_REENTRY_ENABLED':'1','ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES':'4','ENTRY_WINNING_SYMBOL_MIN_DAILY_PNL':'1','ENTRY_WINNING_SYMBOL_REQUIRE_WIN_GT_LOSS':'1','ENTRY_WINNING_SYMBOL_IGNORE_SENT_ONLY':'1','ENTRY_STOP_AFTER_FIRST_LOSS_ONLY_IF_NET_NEGATIVE':'1',
        'RANGE_5M_FILTER_NG_FAIL_OPEN':'1','LOW_MOVE_TONOSAMA_MIN_RANGE_PCT':'0.005','LOW_MOVE_TONOSAMA_STRONG_RANGE_PCT':'0.010','LOW_MOVE_MIN_RANGE_PCT_HIGH_PRICE':'0.005','LOW_MOVE_MIN_RANGE_PCT_LOW_PRICE':'0.010','LOW_MOVE_RANKING_MIN_RANGE_PCT_HIGH_PRICE':'0.005','LOW_MOVE_RANKING_MIN_RANGE_PCT_LOW_PRICE':'0.008','LOW_MOVE_RANKING_MIN_SCORE_FOR_NO_HIGHLOW':'55.0','LOW_MOVE_RANKING_MIN_ABS_SLOPE':'0.0005','LOW_MOVE_TONOSAMA_MIN_ABS_SLOPE':'0.00005','LOW_MOVE_MIN_ABS_SLOPE_HIGH_PRICE':'0.0001','LOW_MOVE_MIN_ABS_SLOPE_LOW_PRICE':'0.00015',
        'ENTRY_ORDER_MIN_RANGE_PCT':'0.005','ENTRY_ORDER_MIN_ATR_RATIO':'0.0025','ENTRY_ORDER_REQUIRE_ATR':'0','ENTRY_ORDER_REQUIRE_HIGH_LOW':'0','ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH':'1.0','ENTRY_DIRECTION_CONFIRM_STRICT':'0','ENTRY_ORDER_SHORT_MTF_NEUTRAL_MIN_SCORE':'1.0','ENTRY_ORDER_SHORT_MTF_NEUTRAL_EPS':'0.0',
}


_EFF_ENV_SET: list[str] = []


def _eff_setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
            _EFF_ENV_SET.append(f"{name}={value}")
    except Exception:
        pass


def apply_entry_final_filter_defaults() -> bool:
    """旧 entry_final_filter_failopen_patch.install() の env / 定数部分。"""
    _EFF_ENV_SET.clear()
    for k, v in _EFF_ENV_DEFAULTS.items():
        _eff_setdefault_env(k, v)
    for k, v in _EFF_SCALPING_DEFAULTS.items():
        _eff_setdefault_env(k, v)
    if _EFF_ENV_SET:
        logger.warning("[ENTRY FINAL FILTER FAILOPEN] env defaults set count=%s keys=%s",
                       len(_EFF_ENV_SET), [x.split("=", 1)[0] for x in _EFF_ENV_SET])
    global ENTRY_ORDER_MIN_RANGE_PCT, ENTRY_ORDER_MIN_ATR_RATIO
    global ENTRY_ORDER_REQUIRE_ATR, ENTRY_ORDER_REQUIRE_HIGH_LOW
    try:
        ENTRY_ORDER_MIN_RANGE_PCT = _fob_safe_float(os.getenv("ENTRY_ORDER_MIN_RANGE_PCT"), 0.005)
        ENTRY_ORDER_MIN_ATR_RATIO = _fob_safe_float(os.getenv("ENTRY_ORDER_MIN_ATR_RATIO"), 0.0025)
        ENTRY_ORDER_REQUIRE_ATR = _bsr_env_bool("ENTRY_ORDER_REQUIRE_ATR", False)
        ENTRY_ORDER_REQUIRE_HIGH_LOW = _bsr_env_bool("ENTRY_ORDER_REQUIRE_HIGH_LOW", False)
        logger.warning(
            "[ENTRY FINAL FILTER FAILOPEN] entry_order_builder constants patched min_range=%.4f min_atr=%.4f require_atr=%s require_high_low=%s",
            ENTRY_ORDER_MIN_RANGE_PCT, ENTRY_ORDER_MIN_ATR_RATIO,
            ENTRY_ORDER_REQUIRE_ATR, ENTRY_ORDER_REQUIRE_HIGH_LOW,
        )
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] entry_order_builder constant patch failed")

    for mod_name, label in (("core.startup.board_retry_patch", "board retry patch"),
                            ("trading.handlers.short_mtf_direction_guard", "short MTF daily optional guard")):
        try:
            mod = __import__(mod_name, fromlist=["install"])
            fn = getattr(mod, "install", None)
            ok = fn() if callable(fn) else False
            logger.warning("[ENTRY FINAL FILTER FAILOPEN] %s installed=%s", label, ok)
        except Exception:
            logger.exception("[ENTRY FINAL FILTER FAILOPEN] %s install failed", label)
    return True
