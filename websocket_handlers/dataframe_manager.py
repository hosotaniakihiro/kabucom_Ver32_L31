# ============================================================
# File   : websocket_handlers/dataframe_manager.py
# Version: Ver28-GLOBALDATA-PUSH-COMPAT-FIX
# ------------------------------------------------------------
# ✔ PUSHデータをシンプルに管理
# ✔ row → push_df 直接保存
# ✔ global_data.push 非依存化
# ✔ GlobalDataCompat / get_push_df / set_push_df 互換対応
# ✔ summary/calculator 用 normalize 維持
# ✔ symbol / datetime / price の最低限保証
# ✔ last_push_received_at 更新
# ✔ flush は push_df ベースで安全実行
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import pandas as pd

from global_state import global_data
from symbol_loader import load_symbol_flags_df

logger = logging.getLogger(__name__)


# ============================================================
# safe global_data helpers
# ============================================================

def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _safe_setattr(obj: Any, name: str, value: Any) -> bool:
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def _safe_dataframe(df: Any) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()
        if isinstance(df, pd.DataFrame):
            return df.copy()
        return pd.DataFrame(df).copy()
    except Exception:
        return pd.DataFrame()


def _get_push_df_safe() -> pd.DataFrame:
    """
    global_data から push_df を安全取得する。
    優先順位:
      1. global_data.get_push_df()
      2. global_data.push_df
      3. global_data.dataframes["push_df"] 等の互換
    """
    try:
        getter = _safe_getattr(global_data, "get_push_df", None)
        if callable(getter):
            return _safe_dataframe(getter())
    except Exception:
        logger.debug("[dataframe_manager] get_push_df failed", exc_info=True)

    try:
        df = _safe_getattr(global_data, "push_df", None)
        if df is not None:
            return _safe_dataframe(df)
    except Exception:
        logger.debug("[dataframe_manager] push_df access failed", exc_info=True)

    try:
        dfs = _safe_getattr(global_data, "dataframes", None)
        if isinstance(dfs, dict) and "push_df" in dfs:
            return _safe_dataframe(dfs.get("push_df"))
    except Exception:
        logger.debug("[dataframe_manager] dataframes['push_df'] access failed", exc_info=True)

    return pd.DataFrame()


def _set_push_df_safe(df: pd.DataFrame) -> bool:
    """
    global_data へ push_df を安全保存する。
    優先順位:
      1. global_data.set_push_df(df)
      2. global_data.push_df = df
      3. global_data.set_dataframe("push_df", df)
      4. global_data.dataframes["push_df"] = df
    """
    df = _safe_dataframe(df)

    try:
        setter = _safe_getattr(global_data, "set_push_df", None)
        if callable(setter):
            setter(df)
            return True
    except Exception:
        logger.debug("[dataframe_manager] set_push_df failed", exc_info=True)

    try:
        if _safe_setattr(global_data, "push_df", df):
            return True
    except Exception:
        logger.debug("[dataframe_manager] push_df setattr failed", exc_info=True)

    try:
        setter2 = _safe_getattr(global_data, "set_dataframe", None)
        if callable(setter2):
            setter2("push_df", df)
            return True
    except Exception:
        logger.debug("[dataframe_manager] set_dataframe('push_df') failed", exc_info=True)

    try:
        dfs = _safe_getattr(global_data, "dataframes", None)
        if isinstance(dfs, dict):
            dfs["push_df"] = df
            return True
    except Exception:
        logger.debug("[dataframe_manager] dataframes['push_df'] set failed", exc_info=True)

    return False


def _set_last_push_received_at(ts: Any) -> None:
    if ts is None:
        return

    try:
        t = pd.to_datetime(ts, errors="coerce")
        if pd.isna(t):
            return
    except Exception:
        return

    try:
        if _safe_setattr(global_data, "last_push_received_at", t):
            return
    except Exception:
        logger.debug("[dataframe_manager] setattr last_push_received_at failed", exc_info=True)

    try:
        setter = _safe_getattr(global_data, "set_value", None)
        if callable(setter):
            setter("last_push_received_at", t)
    except Exception:
        logger.debug("[dataframe_manager] set_value(last_push_received_at) failed", exc_info=True)


# ============================================================
# tz 正規化
# ============================================================

def make_tz_naive(x):
    try:
        if isinstance(x, dt.datetime):
            return x.replace(tzinfo=None)

        if isinstance(x, pd.Timestamp):
            if x.tzinfo is not None:
                return x.tz_convert("Asia/Tokyo").tz_localize(None)
            return x.to_pydatetime()

        if isinstance(x, str):
            t = pd.to_datetime(x, errors="coerce")
            if isinstance(t, pd.Timestamp):
                if t.tzinfo is not None:
                    return t.tz_convert("Asia/Tokyo").tz_localize(None)
                return t.to_pydatetime()

        return None
    except Exception:
        return None


# ============================================================
# summary / calculator 用 正規化
# ============================================================

def normalize_for_calculator(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    numeric_cols = [
        "price",
        "volume",
        "trading_value",
        "vwap",
        "bid_price",
        "ask_price",
        "bid_qty",
        "ask_qty",
        "high_price",
        "low_price",
        "previousclose",
        "opening_price",
        "current_price",
    ]

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "symbol" not in df.columns or "datetime" not in df.columns:
        return pd.DataFrame()

    df = df.dropna(subset=["symbol", "datetime"])

    if "price" in df.columns:
        df = df[df["price"] > 0]

    if df.empty:
        return pd.DataFrame()

    df["symbol"] = df["symbol"].astype(str)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    df["datetime"] = df["datetime"].dt.tz_localize(None)
    df["date"] = df["datetime"].dt.date

    # symbolname 補完
    if "symbolname" not in df.columns or df["symbolname"].isna().any():
        try:
            flags = load_symbol_flags_df()[["symbol", "symbolname"]].copy()
            flags["symbol"] = flags["symbol"].astype(str)
            df = df.merge(flags, on="symbol", how="left", suffixes=("", "_flags"))

            if "symbolname_flags" in df.columns:
                if "symbolname" in df.columns:
                    df["symbolname"] = df["symbolname"].fillna(df["symbolname_flags"])
                else:
                    df["symbolname"] = df["symbolname_flags"]
                df = df.drop(columns=["symbolname_flags"], errors="ignore")

        except Exception:
            logger.exception("❌ symbolname 補完失敗")

    df = df.loc[:, ~df.columns.duplicated()]

    # 重複抑制
    key_cols = [c for c in ("symbol", "datetime") if c in df.columns]
    if len(key_cols) == 2:
        try:
            df = df.drop_duplicates(subset=key_cols, keep="last")
        except Exception:
            logger.debug("duplicate drop failed", exc_info=True)

    return df.reset_index(drop=True)


# ============================================================
# push_df 直接追記
# ============================================================

def _append_to_push_df(row: dict, max_rows: int = 5000) -> None:
    try:
        df_old = _get_push_df_safe()

        df_new = normalize_for_calculator(pd.DataFrame([row]))
        if df_new.empty:
            return

        if df_old is None or df_old.empty:
            df_all = df_new
        else:
            df_old = df_old.copy().reset_index(drop=True)
            df_old = df_old.loc[:, ~df_old.columns.duplicated()]
            df_all = pd.concat([df_old, df_new], ignore_index=True)

        df_all = normalize_for_calculator(df_all)
        df_all = df_all.loc[:, ~df_all.columns.duplicated()]
        df_all = df_all.reset_index(drop=True)

        if len(df_all) > max_rows:
            df_all = df_all.iloc[-max_rows:].reset_index(drop=True)

        ok = _set_push_df_safe(df_all)
        if not ok:
            logger.warning("⚠ push_df 保存失敗 rows=%d", len(df_all))

    except Exception:
        logger.exception("❌ _append_to_push_df error")


# ============================================================
# PUSH追加
# ============================================================

def append_push_tick(row: dict, now: dt.datetime) -> None:
    try:
        if not isinstance(row, dict):
            return

        now = make_tz_naive(now)
        if now is None:
            return

        row = dict(row)
        row["datetime"] = now
        row["time"] = now
        row["received_at"] = now

        symbol = row.get("symbol")
        if not symbol:
            return

        symbol = str(symbol)
        row["symbol"] = symbol

        price = row.get("price")

        # price 欠損時は bid/ask の mid 補完
        if price is None or price <= 0:
            bid = row.get("bid_price")
            ask = row.get("ask_price")
            if bid and ask and bid > 0 and ask > 0:
                row["price"] = (bid + ask) / 2
            else:
                return

        # push_df 更新
        _append_to_push_df(row)

        # 最終受信時刻更新
        _set_last_push_received_at(now)

    except Exception:
        logger.exception("❌ append_push_tick error")


# ============================================================
# flush（push_df ベースで正規化再保存）
# 必要時だけ使う補助関数として残す
# ============================================================

def flush_push_buffer() -> None:
    try:
        df_old = _get_push_df_safe()
        if df_old is None or df_old.empty:
            return

        df_new = normalize_for_calculator(df_old)
        if df_new.empty:
            return

        ok = _set_push_df_safe(df_new.reset_index(drop=True))
        if ok:
            logger.info("[dataframe_manager] flush_push_buffer done rows=%d", len(df_new))
        else:
            logger.warning("[dataframe_manager] flush_push_buffer store failed rows=%d", len(df_new))

        # 最終受信時刻更新
        if "datetime" in df_new.columns:
            try:
                latest_dt = pd.to_datetime(df_new["datetime"], errors="coerce").dropna().max()
                if pd.notna(latest_dt):
                    _set_last_push_received_at(latest_dt)
            except Exception:
                logger.debug("last_push_received_at update failed in flush", exc_info=True)

    except Exception:
        logger.exception("❌ flush_push_buffer error")


# ============================================================
# util
# ============================================================

def get_latest_tick(symbol: str):
    df = _get_push_df_safe()

    if df is None or df.empty:
        return None

    symbol = str(symbol)
    d = df[df["symbol"] == symbol]

    if d.empty:
        return None

    return d.iloc[-1].to_dict()


def normalize_board_push(d, now):
    now = make_tz_naive(now)

    return {
        "symbol": str(d.get("Symbol")),
        "symbolname": d.get("SymbolName"),
        "bid_price": d.get("BidPrice"),
        "bid_qty": d.get("BidQty"),
        "ask_price": d.get("AskPrice"),
        "ask_qty": d.get("AskQty"),
        "price": d.get("CurrentPrice"),
        "current_price": d.get("CurrentPrice"),
        "volume": d.get("TradingVolume"),
        "vwap": d.get("VWAP"),
        "trading_value": d.get("TradingValue"),
        "high_price": d.get("HighPrice"),
        "low_price": d.get("LowPrice"),
        "previousclose": d.get("PreviousClose"),
        "opening_price": d.get("OpeningPrice"),
        "current_price_time": d.get("CurrentPriceTime"),
        "datetime": now,
        "time": now,
        "received_at": now,
        "content": json.dumps(d, ensure_ascii=False),
    }