# ============================================================
# File   : core/startup/tonosama_history_missing_guard_patch.py
# Version: V4.0-HISTORY-MISSING-QUALITY-GUARD
# ------------------------------------------------------------
# 目的:
#   1) 3m/5m summary が stale/empty でも、1m summary が新鮮なら
#      raw1 から 3m/5m を即時 resample して Tonosama 候補0を防ぐ。
#   2) sitecustomize の controlled fail-open 設定を OFF に戻さない。
#   3) history_missing/failopen 行でも、低出来高・低変動・5秒無反応の
#      弱い候補はDROPする。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_BUILD = None
_ORIGINAL_ADD_SURGE = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
            logger.warning("[TONOSAMA HISTORY GUARD] env default set %s=%s", name, value)
    except Exception:
        pass


def _set_env(name: str, value: str) -> None:
    try:
        old = os.getenv(name)
        os.environ[name] = str(value)
        if str(old) != str(value):
            logger.warning("[TONOSAMA HISTORY GUARD] env forced %s: %s -> %s", name, old, value)
    except Exception:
        pass


def _enable_controlled_history_fallback_defaults() -> None:
    """
    raw1 が新鮮な場合は復旧を優先し、最後の保険として controlled fail-open も許可する。
    ただし V4 では fail-open 行に品質ガードを追加し、低出来高・低変動は落とす。
    """
    if _env_bool("TONOSAMA_FORCE_HISTORY_FAILCLOSE", False):
        logger.warning("[TONOSAMA HISTORY GUARD] explicit fail-close requested by env")
        return
    _set_env("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", "1")
    _set_env("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", "1")
    _set_env("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", "1")
    _set_env("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", "0")
    _setdefault_env("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", "3.0")
    _setdefault_env("TONOSAMA_RAW1_RESAMPLE_FALLBACK", "1")
    _setdefault_env("TONOSAMA_HISTORY_MISSING_QUALITY_GUARD", "1")


def _patch_volatility_filter_thresholds() -> None:
    try:
        import trading.filters.volatility_filter as vf

        atr_min = _env_float("ENTRY_ATR_1M_MIN_RATIO", 0.0010)
        range5_min = _env_float("ENTRY_RANGE_5M_MIN_PCT", 0.008)
        row_range_min = _env_float("ENTRY_ROW_RANGE_MIN_PCT", 0.003)

        vf.DEFAULT_ATR_1M_MIN_RATIO = atr_min
        vf.DEFAULT_RANGE_5M_MIN_PCT = range5_min
        vf.DEFAULT_ENTRY_ROW_RANGE_MIN_PCT = row_range_min

        for name, defaults in (
            ("_atr_1m_filter_from_entry_row", (atr_min,)),
            ("_range_5m_filter_from_entry_row", (range5_min,)),
            ("_entry_row_range_ok", (row_range_min,)),
        ):
            try:
                getattr(vf, name).__defaults__ = defaults
            except Exception:
                pass
        try:
            if getattr(vf.atr_1m_filter, "__kwdefaults__", None):
                vf.atr_1m_filter.__kwdefaults__["min_ratio"] = atr_min
        except Exception:
            pass
        try:
            if getattr(vf.range_5m_filter, "__kwdefaults__", None):
                vf.range_5m_filter.__kwdefaults__["min_pct"] = range5_min
        except Exception:
            pass

        logger.warning(
            "[VOL FILTER THRESHOLD PATCH] installed atr_min_ratio=%.6f range5_min_pct=%.6f entry_row_range_min_pct=%.6f",
            atr_min,
            range5_min,
            row_range_min,
        )
    except Exception:
        logger.exception("[VOL FILTER THRESHOLD PATCH] install failed")


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if df is None or df.empty or col not in df.columns:
            return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    except Exception:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")


def _max_existing_numeric(df: pd.DataFrame, names: list[str], default: float = 0.0) -> pd.Series:
    try:
        found = [n for n in names if n in df.columns]
        if not found:
            return pd.Series(default, index=df.index, dtype="float64")
        out = pd.Series(default, index=df.index, dtype="float64")
        for n in found:
            out = pd.concat([out, _num(df, n, default)], axis=1).max(axis=1).fillna(default)
        return out
    except Exception:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")


def _resample_1m_to_interval(vs: Any, interval: int) -> pd.DataFrame:
    try:
        if not _env_bool("TONOSAMA_RAW1_RESAMPLE_FALLBACK", True):
            return pd.DataFrame()
        raw1 = vs.normalize_summary_base(vs.load_merged_summary(1), interval=1)
        if raw1 is None or raw1.empty or "datetime" not in raw1.columns:
            return pd.DataFrame()
        x = raw1.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"])
        if x.empty:
            return pd.DataFrame()

        open_col = _first_existing(x, ["open", "open_price", "close", "close_price"])
        high_col = _first_existing(x, ["high", "high_price", "close", "close_price"])
        low_col = _first_existing(x, ["low", "low_price", "close", "close_price"])
        close_col = _first_existing(x, ["close", "close_price", "current_price", "price"])
        volume_col = _first_existing(x, ["volume", "trading_volume", "latest_volume"])
        if close_col is None:
            return pd.DataFrame()

        x["open"] = pd.to_numeric(x[open_col], errors="coerce") if open_col else pd.to_numeric(x[close_col], errors="coerce")
        x["high"] = pd.to_numeric(x[high_col], errors="coerce") if high_col else pd.to_numeric(x[close_col], errors="coerce")
        x["low"] = pd.to_numeric(x[low_col], errors="coerce") if low_col else pd.to_numeric(x[close_col], errors="coerce")
        x["close"] = pd.to_numeric(x[close_col], errors="coerce")
        x["volume"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0
        x = x.dropna(subset=["close"])
        if x.empty:
            return pd.DataFrame()

        out_parts: list[pd.DataFrame] = []
        rule = f"{int(interval)}min"
        for symbol, g in x.sort_values("datetime").groupby("symbol"):
            try:
                gg = g.set_index("datetime")
                rr = gg.resample(rule, label="right", closed="right").agg(
                    open=("open", "first"),
                    high=("high", "max"),
                    low=("low", "min"),
                    close=("close", "last"),
                    volume=("volume", "sum"),
                ).dropna(subset=["close"])
                if rr.empty:
                    continue
                rr = rr.reset_index()
                rr["symbol"] = symbol
                try:
                    rr["symbolname"] = str(g["symbolname"].dropna().iloc[-1]) if "symbolname" in g.columns and not g["symbolname"].dropna().empty else symbol
                except Exception:
                    rr["symbolname"] = symbol
                rr["source"] = f"tonosama_raw1_resample_{interval}m"
                rr["interval"] = int(interval)
                out_parts.append(rr)
            except Exception:
                logger.debug("[TONOSAMA RAW1 RESAMPLE] symbol failed interval=%s symbol=%s", interval, symbol, exc_info=True)
        out = pd.concat(out_parts, ignore_index=True) if out_parts else pd.DataFrame()
        if not out.empty:
            logger.warning(
                "[TONOSAMA RAW1 RESAMPLE] built interval=%sm rows=%s symbols=%s latest=%s",
                interval,
                len(out),
                out["symbol"].nunique() if "symbol" in out.columns else 0,
                out["datetime"].max() if "datetime" in out.columns else None,
            )
        return out
    except Exception:
        logger.exception("[TONOSAMA RAW1 RESAMPLE] failed interval=%s", interval)
        return pd.DataFrame()


def _compute_surge_features(vs: Any, df: pd.DataFrame, *, interval: int, source_label: str) -> pd.DataFrame:
    try:
        x = vs.normalize_summary_base(df, interval=interval)
        if x is None or x.empty:
            return pd.DataFrame()
        interval_i = int(interval)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"]).sort_values(["symbol", "datetime"])
        if x.empty:
            return pd.DataFrame()

        g = x.groupby("symbol", group_keys=False)
        avg_col = f"prev{vs.VOLUME_AVG_LOOKBACK_BARS}_volume_avg_{interval_i}m"
        ratio_col = f"volume_surge_ratio_{interval_i}m"
        prev_close_col = f"prev_close_{interval_i}m"
        price_chg_col = f"price_change_pct_{interval_i}m"
        up_streak_col = f"prev_{interval_i}m_up_streak"
        down_streak_col = f"prev_{interval_i}m_down_streak"
        last_delta_col = f"prev_{interval_i}m_last_delta_pct"

        x["volume"] = pd.to_numeric(x["volume"], errors="coerce").fillna(0.0) if "volume" in x.columns else 0.0
        x["close"] = pd.to_numeric(x["close"], errors="coerce")
        x[avg_col] = g["volume"].transform(lambda s: s.shift(1).rolling(vs.VOLUME_AVG_LOOKBACK_BARS, min_periods=2).mean())
        x[ratio_col] = pd.to_numeric(x["volume"] / x[avg_col].replace(0, pd.NA), errors="coerce").replace([float("inf"), -float("inf")], pd.NA)
        x[prev_close_col] = g["close"].shift(1)
        x[price_chg_col] = ((x["close"] - x[prev_close_col]) / x[prev_close_col].replace(0, pd.NA) * 100.0)
        x[price_chg_col] = pd.to_numeric(x[price_chg_col], errors="coerce").replace([float("inf"), -float("inf")], pd.NA)
        x[last_delta_col] = x[price_chg_col].fillna(0.0)
        x[f"_is_{interval_i}m_up"] = pd.to_numeric(x["close"], errors="coerce") > pd.to_numeric(x[prev_close_col], errors="coerce")
        x[f"_is_{interval_i}m_down"] = pd.to_numeric(x["close"], errors="coerce") < pd.to_numeric(x[prev_close_col], errors="coerce")
        x[up_streak_col] = g[f"_is_{interval_i}m_up"].apply(vs._consecutive_true_counts)
        x[down_streak_col] = g[f"_is_{interval_i}m_down"].apply(vs._consecutive_true_counts)

        if x[price_chg_col].isna().all():
            fallback_chg = vs._intrabar_price_change_pct(x, interval_i)
            if fallback_chg.notna().any():
                x[price_chg_col] = fallback_chg
                logger.warning("[TONOSAMA SURGE ROLLING PATCH] price_change fallback open_to_close interval=%sm rows=%s nonnull=%s", interval_i, len(x), int(fallback_chg.notna().sum()))

        recent = vs._filter_recent_rows(x, interval=interval_i, label=f"feature_latest_after_rolling:{source_label}")
        if recent.empty:
            return pd.DataFrame()
        latest = recent.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"]).groupby("symbol", group_keys=False).tail(1)
        keep_cols = ["symbol", "datetime", "close", "volume", avg_col, ratio_col, prev_close_col, price_chg_col, up_streak_col, down_streak_col, last_delta_col]
        for c in keep_cols:
            if c not in latest.columns:
                latest[c] = pd.NA
        latest = latest[keep_cols].copy().rename(columns={"datetime": f"datetime_{interval_i}m", "close": f"close_{interval_i}m", "volume": f"volume_{interval_i}m"})
        for c in [up_streak_col, down_streak_col]:
            latest[c] = pd.to_numeric(latest[c], errors="coerce").fillna(0).astype(int)
        latest[last_delta_col] = pd.to_numeric(latest[last_delta_col], errors="coerce").fillna(0.0)
        logger.warning(
            "[TONOSAMA SURGE ROLLING PATCH] %sm latest rows=%s source=%s ratio_nonnull=%s up_ge3=%s down_ge3=%s head=%s",
            interval_i,
            len(latest),
            source_label,
            int(pd.to_numeric(latest[ratio_col], errors="coerce").notna().sum()) if ratio_col in latest.columns else 0,
            int((latest[up_streak_col] >= 3).sum()),
            int((latest[down_streak_col] >= 3).sum()),
            latest[[c for c in ["symbol", f"close_{interval_i}m", up_streak_col, down_streak_col, last_delta_col, ratio_col, price_chg_col] if c in latest.columns]].head(12).to_dict("records"),
        )
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[TONOSAMA SURGE ROLLING PATCH] compute failed interval=%s source=%s", interval, source_label)
        return pd.DataFrame()


def _patch_volume_surge_rolling_before_recent() -> None:
    global _ORIGINAL_ADD_SURGE
    try:
        import trading.entry.tonosama.volume_surge as vs

        cur = getattr(vs, "add_volume_surge_features", None)
        if not callable(cur):
            logger.warning("[TONOSAMA SURGE ROLLING PATCH] target add_volume_surge_features not callable")
            return
        if getattr(cur, "_raw1_resample_fallback_patch", False):
            return
        _ORIGINAL_ADD_SURGE = cur

        def _patched_add_volume_surge_features(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
            interval_i = int(interval)
            try:
                out = _compute_surge_features(vs, df, interval=interval_i, source_label="summary_db")
                if not out.empty:
                    return out
                if interval_i in (3, 5):
                    rebuilt = _resample_1m_to_interval(vs, interval_i)
                    out = _compute_surge_features(vs, rebuilt, interval=interval_i, source_label="raw1_resample")
                    if not out.empty:
                        logger.warning("[TONOSAMA SURGE ROLLING PATCH] recovered stale/empty %sm history from raw1 rows=%s", interval_i, len(out))
                        return out
                return _ORIGINAL_ADD_SURGE(df, interval=interval_i) if callable(_ORIGINAL_ADD_SURGE) else pd.DataFrame()
            except Exception:
                logger.exception("[TONOSAMA SURGE ROLLING PATCH] patched add_volume_surge_features failed interval=%s", interval_i)
                return _ORIGINAL_ADD_SURGE(df, interval=interval_i) if callable(_ORIGINAL_ADD_SURGE) else pd.DataFrame()

        _patched_add_volume_surge_features._raw1_resample_fallback_patch = True  # type: ignore[attr-defined]
        _patched_add_volume_surge_features._original = cur  # type: ignore[attr-defined]
        vs.add_volume_surge_features = _patched_add_volume_surge_features
        logger.warning("[TONOSAMA SURGE ROLLING PATCH] installed raw1 resample fallback")
    except Exception:
        logger.exception("[TONOSAMA SURGE ROLLING PATCH] install failed")


def _sample(df: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    cols = [c for c in [
        "symbol", "symbolname", "close", "volume", "_latest_volume", "_max_volume_surge_ratio", "_max_price_change_pct",
        "_surge_tf", "_volume_surge_history_missing", "_volume_surge_failopen",
        "_body_change_pct", "_intrabar_range_pct", "_slope", "price_change_5s_pct", "volume_surge_ratio_5s",
        "_history_failopen_turnover", "_history_failopen_quality_ok",
    ] if c in df.columns]
    out: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        item: dict[str, Any] = {}
        for c in cols:
            v = row.get(c)
            try:
                if pd.isna(v):
                    v = None
            except Exception:
                pass
            if isinstance(v, float):
                v = round(v, 6)
            item[c] = v
        out.append(item)
    return out


def _history_missing_quality_mask(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(False, index=df.index if df is not None else None, dtype="bool")

    latest_vol = _max_existing_numeric(df, ["_latest_volume", "latest_volume", "volume", "volume_1m", "volume_3m", "volume_5m"], 0.0)
    close = _max_existing_numeric(df, ["close", "close_price", "current_price", "price"], 0.0)
    turnover = latest_vol * close

    price_abs = _num(df, "_max_price_change_pct", 0.0).abs()
    body_abs = _num(df, "_body_change_pct", 0.0).abs()
    range_abs = _num(df, "_intrabar_range_pct", 0.0).abs()
    five_abs = _num(df, "price_change_5s_pct", 0.0).abs()
    five_surge = _num(df, "volume_surge_ratio_5s", 0.0)

    min_volume = _env_float("TONOSAMA_HISTORY_MISSING_MIN_VOLUME", 500000.0)
    min_turnover = _env_float("TONOSAMA_HISTORY_MISSING_MIN_TURNOVER", 10000000.0)
    min_price = _env_float("TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT", 0.50)
    min_body = _env_float("TONOSAMA_HISTORY_MISSING_MIN_BODY_PCT", 0.30)
    min_range = _env_float("TONOSAMA_HISTORY_MISSING_MIN_RANGE_PCT", 1.00)
    min_5sec = _env_float("TONOSAMA_HISTORY_MISSING_MIN_5SEC_CHANGE_PCT", 0.05)
    min_5sec_surge = _env_float("TONOSAMA_HISTORY_MISSING_MIN_5SEC_SURGE", 1.20)

    liquidity_ok = (latest_vol >= min_volume) & (turnover >= min_turnover)
    movement_ok = (price_abs >= min_price) | (body_abs >= min_body) | (range_abs >= min_range) | (five_abs >= min_5sec)
    five_ok = (five_abs >= min_5sec) | (five_surge >= min_5sec_surge) | (five_surge <= 0)

    ok = (liquidity_ok & movement_ok & five_ok).fillna(False).astype(bool)
    try:
        df["_history_failopen_turnover"] = turnover
        df["_history_failopen_quality_ok"] = ok
    except Exception:
        pass
    return ok


def _pass_history_missing_failopen(df: pd.DataFrame, *, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    has_missing = "_volume_surge_history_missing" in df.columns
    has_failopen = "_volume_surge_failopen" in df.columns
    if not has_missing and not has_failopen:
        return df
    try:
        x = df.copy()
        missing = x.get("_volume_surge_history_missing", pd.Series(False, index=x.index)).fillna(False).astype(bool)
        failopen = x.get("_volume_surge_failopen", pd.Series(False, index=x.index)).fillna(False).astype(bool)
        affected_mask = (missing | failopen).fillna(False).astype(bool)
        affected = int(affected_mask.sum())
    except Exception:
        logger.debug("[TONOSAMA HISTORY GUARD] affected mask failed stage=%s", stage, exc_info=True)
        return df

    if not affected:
        logger.warning(
            "[TONOSAMA HISTORY GUARD] pass-through history-missing/failopen rows stage=%s rows=%s affected=0 sample=[]",
            stage,
            len(df),
        )
        return df

    if not _env_bool("TONOSAMA_HISTORY_MISSING_QUALITY_GUARD", True):
        logger.warning(
            "[TONOSAMA HISTORY GUARD] pass-through history-missing/failopen rows stage=%s rows=%s affected=%s quality_guard=0 sample=%s",
            stage,
            len(x),
            affected,
            _sample(x.loc[affected_mask].copy()),
        )
        return x

    quality_ok = _history_missing_quality_mask(x)
    drop_mask = affected_mask & ~quality_ok
    kept = x.loc[~drop_mask].copy()
    dropped = int(drop_mask.sum())
    if dropped:
        logger.warning(
            "[TONOSAMA HISTORY GUARD] drop weak history-missing/failopen rows stage=%s before=%s after=%s dropped=%s thresholds=%s sample=%s",
            stage,
            len(x),
            len(kept),
            dropped,
            {
                "min_volume": _env_float("TONOSAMA_HISTORY_MISSING_MIN_VOLUME", 500000.0),
                "min_turnover": _env_float("TONOSAMA_HISTORY_MISSING_MIN_TURNOVER", 10000000.0),
                "min_price_change_pct": _env_float("TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT", 0.50),
                "min_body_pct": _env_float("TONOSAMA_HISTORY_MISSING_MIN_BODY_PCT", 0.30),
                "min_range_pct": _env_float("TONOSAMA_HISTORY_MISSING_MIN_RANGE_PCT", 1.00),
                "min_5sec_change_pct": _env_float("TONOSAMA_HISTORY_MISSING_MIN_5SEC_CHANGE_PCT", 0.05),
            },
            _sample(x.loc[drop_mask].copy()),
        )
    logger.warning(
        "[TONOSAMA HISTORY GUARD] pass-through history-missing/failopen rows stage=%s rows=%s affected=%s kept=%s dropped=%s sample=%s",
        stage,
        len(x),
        affected,
        int((affected_mask & quality_ok).sum()),
        dropped,
        _sample(kept.loc[affected_mask.reindex(kept.index, fill_value=False)].copy()) if not kept.empty else [],
    )
    return kept


def _patched_build_scalping_feature_df(*args, **kwargs):
    df = _ORIGINAL_BUILD(*args, **kwargs) if callable(_ORIGINAL_BUILD) else pd.DataFrame()
    if isinstance(df, pd.DataFrame):
        return _pass_history_missing_failopen(df, stage="build_scalping_feature_df")
    return df


def install() -> bool:
    global _PATCHED, _ORIGINAL_BUILD
    if _PATCHED:
        return True

    _enable_controlled_history_fallback_defaults()
    _patch_volatility_filter_thresholds()
    _patch_volume_surge_rolling_before_recent()

    if not _env_bool("TONOSAMA_HISTORY_MISSING_GUARD_ENABLED", True):
        logger.warning("[TONOSAMA HISTORY GUARD] disabled by env")
        return False
    try:
        import trading.entry.tonosama.runner as runner
    except Exception:
        logger.exception("[TONOSAMA HISTORY GUARD] import runner failed")
        return False
    try:
        cur = getattr(runner, "build_scalping_feature_df", None)
        if not callable(cur):
            logger.warning("[TONOSAMA HISTORY GUARD] target build_scalping_feature_df not callable")
            return False
        if getattr(cur, "_tonosama_history_guard_patch_v4", False):
            _PATCHED = True
            return True
        _ORIGINAL_BUILD = cur
        _patched_build_scalping_feature_df._tonosama_history_guard_patch_v4 = True  # type: ignore[attr-defined]
        _patched_build_scalping_feature_df._original = cur  # type: ignore[attr-defined]
        runner.build_scalping_feature_df = _patched_build_scalping_feature_df
        _PATCHED = True
        logger.warning(
            "[TONOSAMA HISTORY GUARD] installed v4 raw1_resample=%s allow_history_missing=%s drop_history_missing=%s quality_guard=%s min_volume=%.0f min_price_change=%.3f min_range=%.3f",
            _env_bool("TONOSAMA_RAW1_RESAMPLE_FALLBACK", True),
            _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", True),
            _env_bool("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", False),
            _env_bool("TONOSAMA_HISTORY_MISSING_QUALITY_GUARD", True),
            _env_float("TONOSAMA_HISTORY_MISSING_MIN_VOLUME", 500000.0),
            _env_float("TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT", 0.50),
            _env_float("TONOSAMA_HISTORY_MISSING_MIN_RANGE_PCT", 1.00),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA HISTORY GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA HISTORY GUARD] auto install failed")


__all__ = ["install"]
