# ============================================================
# File   : core/startup/ranking_entry_filter_rescue_patch.py
# Version: V1.7-PERIOD-VOLUME-GUARD
# ------------------------------------------------------------
# 目的:
#   スコア上位母数を80位まで広げた後も、
#   FLAT_PRICE_FILTER_RECURSION救済側だけ max_rank=10 のままで、
#   rank 11〜30 の高流動性候補が rank_low で落ちる問題を修正。
#
# V1.7:
#   - ランキング由来/殿様イナゴで使う 1分/3分/5分の出来高が、
#     当日累計出来高の max にならないよう runtime guard を追加。
#   - PUSH/Ranking の累計出来高は symbol + 当日単位で diff し、
#     1分/3分/5分足は対象期間内の差分出来高 sum を volume にする。
#   - 既存のランキング救済条件は V1.6 のまま維持。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG: Callable[..., tuple[bool, str]] | None = None
_IN_FILTER = False
_VOLUME_GUARD_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 999999) -> int:
    try:
        return int(_safe_float(v, float(default)))
    except Exception:
        return int(default)


def _reason_rescuable(reason_s: str) -> bool:
    if reason_s.startswith("RANKING_TECH_"):
        return True
    if reason_s in {"FILTER_RECURSION", "ORIGINAL_FILTER_RECURSION", "ORIGINAL_FILTER_UNAVAILABLE", "FLAT_PRICE_FILTER_RECURSION"}:
        return True
    if _env_bool("RANKING_ENTRY_RESCUE_FLAT_PRICE_REASON", True):
        if reason_s.startswith("BUY_PRICE_NOT_UP") or reason_s.startswith("SELL_PRICE_NOT_DOWN"):
            return True
    return False


def _is_recursion_reason(reason_s: str) -> bool:
    return reason_s in {"FILTER_RECURSION", "ORIGINAL_FILTER_RECURSION", "FLAT_PRICE_FILTER_RECURSION"}


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _rescue_allowed(row: dict[str, Any], side: str, score: float, reason: Any) -> tuple[bool, dict[str, Any]]:
    reason_s = str(reason or "")
    recursion_reason = _is_recursion_reason(reason_s)
    rank = _safe_int(_first(row, ("rank_position", "rank", "No", "no"), 999999), 999999)
    price = _safe_float(_first(row, ("price", "current_price", "CurrentPrice", "close"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "trading_volume", "TradingVolume"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "Turnover"), 0.0), 0.0)
    day = _safe_float(_first(row, ("day_change_pct", "change_percentage", "change_rate", "ChangePercentage", "ChangeRatio"), 0.0), 0.0)
    rt = str(_first(row, ("rank_type", "ranking_type", "CategoryName"), ""))
    side_u = str(side or "").upper().strip()
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume

    min_score = _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_SCORE" if recursion_reason else "RANKING_ENTRY_RESCUE_MIN_SCORE", 55.0 if recursion_reason else 60.0)
    max_rank = _env_int("RANKING_ENTRY_RESCUE_RECURSION_MAX_RANK" if recursion_reason else "RANKING_ENTRY_RESCUE_MAX_RANK", 30)
    min_turnover = _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_TURNOVER" if recursion_reason else "RANKING_ENTRY_RESCUE_MIN_TURNOVER", 30000000.0)
    min_volume = _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_VOLUME" if recursion_reason else "RANKING_ENTRY_RESCUE_MIN_VOLUME", 30000.0)
    min_abs_day = _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_ABS_DAY_PCT" if recursion_reason else "RANKING_ENTRY_RESCUE_MIN_ABS_DAY_PCT", 0.0 if recursion_reason else 3.0)
    min_price = _env_float("RANKING_ENTRY_RESCUE_MIN_PRICE", 300.0)
    max_price = _env_float("RANKING_ENTRY_RESCUE_MAX_PRICE", 7000.0)

    diag = {"reason": reason_s, "recursion_reason": recursion_reason, "rank": rank, "rank_type": rt, "side": side_u, "score": score, "price": price, "volume": volume, "turnover": turnover, "day": day, "min_score": min_score, "max_rank": max_rank, "min_turnover": min_turnover, "min_volume": min_volume}
    if not _reason_rescuable(reason_s):
        return False, {**diag, "ng": "not_rescuable_reason"}
    if score < min_score:
        return False, {**diag, "ng": "score_low"}
    if rank > max_rank:
        return False, {**diag, "ng": "rank_low"}
    if price > 0 and (price < min_price or price > max_price):
        return False, {**diag, "ng": "price_range"}
    if volume > 0 and volume < min_volume:
        return False, {**diag, "ng": "volume_low"}
    if turnover > 0 and turnover < min_turnover:
        return False, {**diag, "ng": "turnover_low"}
    if min_abs_day > 0 and abs(day) < min_abs_day:
        return False, {**diag, "ng": "day_move_low"}
    if not recursion_reason:
        if side_u == "BUY" and day <= 0:
            return False, {**diag, "ng": "buy_day_not_positive"}
        if side_u == "SELL" and day >= 0:
            return False, {**diag, "ng": "sell_day_not_negative"}
    else:
        if day != 0:
            if side_u == "BUY" and day < 0:
                return False, {**diag, "ng": "buy_day_negative"}
            if side_u == "SELL" and day > 0:
                return False, {**diag, "ng": "sell_day_positive"}
    return True, {**diag, "rescue": True}


def _try_rescue(row: Any, side: str, score: float, reason: Any) -> tuple[bool, str]:
    try:
        if not _env_bool("RANKING_ENTRY_STRONG_TECH_RESCUE_ENABLED", True):
            return False, str(reason or "RESCUE_DISABLED")
        if not isinstance(row, dict):
            return False, str(reason or "ROW_NOT_DICT")
        allowed, diag = _rescue_allowed(row, str(side or ""), _safe_float(score, 0.0), reason)
        if allowed:
            row["ranking_filter_rescue"] = True
            row["ranking_filter_rescue_reason"] = str(reason or "")
            row["ranking_filter_rescue_diag"] = str(diag)
            logger.warning("[RANKING FILTER RESCUE] allow symbol=%s side=%s diag=%s", row.get("symbol") or row.get("Symbol"), side, diag)
            return True, "RANKING_FILTER_RESCUE"
        logger.info("[RANKING FILTER RESCUE] reject symbol=%s side=%s diag=%s", row.get("symbol") or row.get("Symbol"), side, diag)
        return False, str(reason or "RESCUE_NG")
    except Exception:
        logger.exception("[RANKING FILTER RESCUE] rescue check failed symbol=%s", row.get("symbol") if isinstance(row, dict) else None)
        return False, str(reason or "RESCUE_EXCEPTION")


def _patched_passes_ranking_only_filters(row, side, prev_h, score, parts):
    global _IN_FILTER
    if _IN_FILTER:
        ok, reason = _try_rescue(row, side, score, "FILTER_RECURSION")
        return ok, reason
    _IN_FILTER = True
    try:
        ok = False
        reason: Any = "ORIGINAL_FILTER_UNAVAILABLE"
        try:
            if callable(_ORIG):
                ok, reason = _ORIG(row, side, prev_h, score, parts)
        except RecursionError:
            logger.warning("[RANKING FILTER RESCUE] original filter recursion detected symbol=%s side=%s", row.get("symbol") if isinstance(row, dict) else None, side)
            ok, reason = False, "ORIGINAL_FILTER_RECURSION"
        if ok:
            return ok, reason
        rescue_ok, rescue_reason = _try_rescue(row, side, score, reason)
        if rescue_ok:
            return True, rescue_reason
        return False, reason
    finally:
        _IN_FILTER = False


def _period_volume_from_cumulative(df, *, volume_col: str = "volume", datetime_col: str = "datetime"):
    """累計出来高を symbol + 当日単位で差分化し、対象足出来高へ変換する。"""
    import pandas as pd
    import numpy as np

    if df is None or getattr(df, "empty", True) or volume_col not in df.columns:
        return df
    out = df.copy()
    raw = pd.to_numeric(out[volume_col], errors="coerce").fillna(0.0)
    out["_raw_cumulative_volume"] = raw
    if "symbol" not in out.columns or datetime_col not in out.columns:
        out["_period_volume"] = raw.clip(lower=0.0)
        return out
    dt_s = pd.to_datetime(out[datetime_col], errors="coerce")
    out["_trade_date_for_volume"] = dt_s.dt.date
    out["_volume_order"] = range(len(out))
    out = out.sort_values(["symbol", "_trade_date_for_volume", datetime_col, "_volume_order"], kind="stable")
    prev = out.groupby(["symbol", "_trade_date_for_volume"], sort=False)["_raw_cumulative_volume"].shift(1)
    diff = out["_raw_cumulative_volume"] - prev
    # 初回行は直前累計が不明なので0扱い。累計リセット/異常なマイナスも0にする。
    period = diff.where(prev.notna(), 0.0)
    period = period.where(period >= 0, 0.0).replace([np.inf, -np.inf], 0.0).fillna(0.0)
    out["_period_volume"] = period
    out = out.sort_values("_volume_order", kind="stable").drop(columns=["_volume_order", "_trade_date_for_volume"], errors="ignore")
    return out


def _install_period_volume_guards() -> bool:
    """
    ランキング由来/殿様イナゴの足出来高を、当日累計ではなく期間出来高にする。

    - summary_incremental_engine: PUSH累計出来高をdiffし、1/3/5分足はsum。
    - ranking_technical_store: ランキング履歴の累計出来高をdiffしてテクニカル計算に使う。
    """
    global _VOLUME_GUARD_INSTALLED
    if _VOLUME_GUARD_INSTALLED:
        return True
    ok_any = False

    try:
        import pandas as pd
        import trading.summary.engine.summary_incremental_engine as sie

        cur_build = getattr(sie, "_build_bars", None)
        if callable(cur_build) and not getattr(cur_build, "_period_volume_guard_v17", False):
            def _patched_build_bars(push_df, interval: int):
                ticks = sie._normalize_push_df(push_df)
                if ticks.empty:
                    return pd.DataFrame()

                interval_n = int(interval)
                freq = f"{interval_n}min"
                work = ticks.copy()
                work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
                work = work.dropna(subset=["datetime"]).copy()
                work = work.sort_values(["symbol", "datetime"], kind="stable")
                work = _period_volume_from_cumulative(work, volume_col="volume", datetime_col="datetime")
                work["_slot"] = work["datetime"].dt.floor(freq)
                work = work.dropna(subset=["_slot"]).copy()

                def _last_text(s):
                    x = s.dropna().astype(str)
                    return x.iloc[-1] if not x.empty else ""

                bars = (
                    work.groupby(["symbol", "_slot"], as_index=False)
                    .agg(
                        symbolname=("symbolname", _last_text),
                        open=("close", "first"),
                        high=("high", "max"),
                        low=("low", "min"),
                        close=("close", "last"),
                        volume=("_period_volume", "sum"),
                        cumulative_volume=("_raw_cumulative_volume", "max"),
                        tick_count=("close", "count"),
                        first_tick_at=("datetime", "min"),
                        last_tick_at=("datetime", "max"),
                    )
                    .rename(columns={"_slot": "datetime"})
                )

                for c in ("open", "high", "low", "close", "volume", "cumulative_volume"):
                    if c in bars.columns:
                        bars[c] = pd.to_numeric(bars[c], errors="coerce")

                bars["open_price"] = bars["open"]
                bars["high_price"] = bars["high"]
                bars["low_price"] = bars["low"]
                bars["close_price"] = bars["close"]
                bars["price"] = bars["close"]
                bars["current_price"] = bars["close"]
                bars["date"] = pd.to_datetime(bars["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
                bars["time"] = pd.to_datetime(bars["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
                bars["start_time"] = bars["time"]
                bars["end_time"] = bars["time"]
                bars["time_range"] = pd.to_datetime(bars["datetime"], errors="coerce").dt.strftime("%H:%M")
                bars = bars.dropna(subset=["symbol", "datetime", "close"]).copy()
                bars = bars.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

                out = sie._add_indicators(bars, interval_n)
                sie._log_df_state("built_bars_period_volume_guard", out, interval_n)
                return out

            _patched_build_bars._period_volume_guard_v17 = True  # type: ignore[attr-defined]
            _patched_build_bars._original = cur_build  # type: ignore[attr-defined]
            sie._build_bars = _patched_build_bars
            ok_any = True
            logger.warning("[PERIOD VOLUME GUARD] patched summary_incremental_engine._build_bars volume=sum(diff(cumulative))")
    except Exception:
        logger.exception("[PERIOD VOLUME GUARD] summary incremental patch failed")

    try:
        import pandas as pd
        import trading.ranking.ranking_technical_store as rts

        cur_calc = getattr(rts, "_calculate_technicals", None)
        if callable(cur_calc) and not getattr(cur_calc, "_period_volume_guard_v17", False):
            def _patched_calculate_technicals(history):
                h = history.copy() if isinstance(history, pd.DataFrame) else pd.DataFrame()
                if h.empty:
                    return cur_calc(history)
                if "datetime" in h.columns:
                    h["datetime"] = pd.to_datetime(h["datetime"], errors="coerce")
                    h = h.dropna(subset=["datetime"]).copy()
                if "volume" in h.columns:
                    h = _period_volume_from_cumulative(h, volume_col="volume", datetime_col="datetime")
                    h["cumulative_volume"] = h.get("_raw_cumulative_volume", h["volume"])
                    h["volume"] = h["_period_volume"]
                if "turnover" in h.columns:
                    # 売買代金も累計の可能性が高い。volume差分×closeを優先して期間代金にする。
                    close = pd.to_numeric(h.get("close", 0), errors="coerce").fillna(0.0)
                    vol = pd.to_numeric(h.get("volume", 0), errors="coerce").fillna(0.0)
                    h["cumulative_turnover"] = pd.to_numeric(h["turnover"], errors="coerce").fillna(0.0)
                    h["turnover"] = close * vol
                return cur_calc(h)

            _patched_calculate_technicals._period_volume_guard_v17 = True  # type: ignore[attr-defined]
            _patched_calculate_technicals._original = cur_calc  # type: ignore[attr-defined]
            rts._calculate_technicals = _patched_calculate_technicals
            ok_any = True
            logger.warning("[PERIOD VOLUME GUARD] patched ranking_technical_store._calculate_technicals volume=diff(cumulative)")
    except Exception:
        logger.exception("[PERIOD VOLUME GUARD] ranking technical patch failed")

    _VOLUME_GUARD_INSTALLED = bool(ok_any)
    return bool(ok_any)


def install() -> bool:
    global _INSTALLED, _ORIG
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("RANKING_ENTRY_RESCUE_RECURSION_MAX_RANK", "30")
        os.environ.setdefault("RANKING_ENTRY_RESCUE_MAX_RANK", "30")
        os.environ.setdefault("RANKING_ENTRY_RESCUE_MIN_TURNOVER", "30000000")
        os.environ.setdefault("RANKING_ENTRY_RESCUE_RECURSION_MIN_TURNOVER", "30000000")
        volume_guard_ok = _install_period_volume_guards()
        import trading.ranking.entry_from_ranking as efr
        cur = getattr(efr, "_passes_ranking_only_filters", None)
        if not callable(cur):
            logger.warning("[RANKING FILTER RESCUE] target unavailable volume_guard_ok=%s", volume_guard_ok)
            return bool(volume_guard_ok)
        if getattr(cur, "_ranking_filter_rescue_v16", False):
            _INSTALLED = True
            return True
        _ORIG = cur
        _patched_passes_ranking_only_filters._ranking_filter_rescue_v16 = True  # type: ignore[attr-defined]
        _patched_passes_ranking_only_filters._original = cur  # type: ignore[attr-defined]
        efr._passes_ranking_only_filters = _patched_passes_ranking_only_filters
        _INSTALLED = True
        logger.warning("[RANKING FILTER RESCUE] installed v1.7 enabled=%s volume_guard_ok=%s min_score=%.1f recursion_min_score=%.1f max_rank=%s recursion_max_rank=%s min_turnover=%.0f recursion_min_turnover=%.0f", _env_bool("RANKING_ENTRY_STRONG_TECH_RESCUE_ENABLED", True), volume_guard_ok, _env_float("RANKING_ENTRY_RESCUE_MIN_SCORE", 60.0), _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_SCORE", 55.0), _env_int("RANKING_ENTRY_RESCUE_MAX_RANK", 30), _env_int("RANKING_ENTRY_RESCUE_RECURSION_MAX_RANK", 30), _env_float("RANKING_ENTRY_RESCUE_MIN_TURNOVER", 30000000.0), _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_TURNOVER", 30000000.0))
        return True
    except Exception:
        logger.exception("[RANKING FILTER RESCUE] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[RANKING FILTER RESCUE] auto install failed")

__all__ = ["install"]
