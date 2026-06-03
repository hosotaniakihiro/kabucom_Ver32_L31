# ============================================================
# File   : core/startup/tonosama_history_missing_guard_patch.py
# Version: V2.5-HISTORY-FAIL-CLOSE-ROLLING-BEFORE-RECENT
# ------------------------------------------------------------
# 目的:
#   1) 3m/5mの出来高急増履歴が無い状態で controlled fail-open された
#      TONOSAMA候補を、既定で全件DROPする。
#   2) 起動時sitecustomize.pyが入れた fail-open 系ENVも、明示許可が無い限りOFFへ戻す。
#   3) ATR/値幅フィルタの閾値を実運用向けに緩和し、ENTRY_ATR_1M_MIN_RATIO 等で調整可能にする。
#   4) TONOSAMA volume_surge は「直近だけに絞ってからrolling」ではなく、
#      当日履歴全体でrolling平均/連続本数を作ってから最新足だけ鮮度判定する。
#
# 背景:
#   13:02ログで 3m rows はあるが、recent filter 後に rolling 元が不足し、
#   volume_surge_ratio が作れず base feature empty になっていた。
#
# ENV:
#   TONOSAMA_EXPLICIT_ALLOW_SURGE_FAILOPEN=1 # set only if old fail-open behavior is needed
#   TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY=0   # default reject
#   TONOSAMA_DROP_HISTORY_MISSING_ENTRY=1    # default drop
#   ENTRY_ATR_1M_MIN_RATIO=0.0010
#   ENTRY_RANGE_5M_MIN_PCT=0.008
#   ENTRY_ROW_RANGE_MIN_PCT=0.003
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
        return float(raw)
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


def _disable_surge_failopen_defaults() -> None:
    if _env_bool("TONOSAMA_EXPLICIT_ALLOW_SURGE_FAILOPEN", False):
        logger.warning("[TONOSAMA HISTORY GUARD] surge fail-open explicitly allowed by env")
        return
    _set_env("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", "0")
    _set_env("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", "0")
    _set_env("TONOSAMA_FORCE_SURGE_FAILOPEN", "0")
    _set_env("TONOSAMA_ALLOW_EARLY_SURGE_FAILOPEN", "0")


def _patch_volatility_filter_thresholds() -> None:
    try:
        import trading.filters.volatility_filter as vf

        atr_min = _env_float("ENTRY_ATR_1M_MIN_RATIO", 0.0010)
        range5_min = _env_float("ENTRY_RANGE_5M_MIN_PCT", 0.008)
        row_range_min = _env_float("ENTRY_ROW_RANGE_MIN_PCT", 0.003)

        vf.DEFAULT_ATR_1M_MIN_RATIO = atr_min
        vf.DEFAULT_RANGE_5M_MIN_PCT = range5_min
        vf.DEFAULT_ENTRY_ROW_RANGE_MIN_PCT = row_range_min

        try:
            vf._atr_1m_filter_from_entry_row.__defaults__ = (atr_min,)
        except Exception:
            pass
        try:
            vf._range_5m_filter_from_entry_row.__defaults__ = (range5_min,)
        except Exception:
            pass
        try:
            vf._entry_row_range_ok.__defaults__ = (row_range_min,)
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


def _patch_volume_surge_rolling_before_recent() -> None:
    global _ORIGINAL_ADD_SURGE
    try:
        import trading.entry.tonosama.volume_surge as vs

        cur = getattr(vs, "add_volume_surge_features", None)
        if not callable(cur):
            logger.warning("[TONOSAMA SURGE ROLLING PATCH] target add_volume_surge_features not callable")
            return
        if getattr(cur, "_rolling_before_recent_patch", False):
            return
        _ORIGINAL_ADD_SURGE = cur

        def _patched_add_volume_surge_features(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
            try:
                x = vs.normalize_summary_base(df, interval=interval)
                if x.empty:
                    return pd.DataFrame()
                interval_i = int(interval)
                x = x.sort_values(["symbol", "datetime"])
                g = x.groupby("symbol", group_keys=False)
                avg_col = f"prev{vs.VOLUME_AVG_LOOKBACK_BARS}_volume_avg_{interval_i}m"
                ratio_col = f"volume_surge_ratio_{interval_i}m"
                prev_close_col = f"prev_close_{interval_i}m"
                price_chg_col = f"price_change_pct_{interval_i}m"
                up_streak_col = f"prev_{interval_i}m_up_streak"
                down_streak_col = f"prev_{interval_i}m_down_streak"
                last_delta_col = f"prev_{interval_i}m_last_delta_pct"

                x[avg_col] = g["volume"].transform(
                    lambda s: s.shift(1).rolling(vs.VOLUME_AVG_LOOKBACK_BARS, min_periods=2).mean()
                )
                x[ratio_col] = x["volume"] / x[avg_col].replace(0, pd.NA)
                x[ratio_col] = pd.to_numeric(x[ratio_col], errors="coerce").replace([float("inf"), -float("inf")], pd.NA)

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
                        logger.warning(
                            "[TONOSAMA SURGE ROLLING PATCH] price_change fallback open_to_close interval=%sm rows=%s nonnull=%s",
                            interval_i,
                            len(x),
                            int(fallback_chg.notna().sum()),
                        )

                recent = vs._filter_recent_rows(x, interval=interval_i, label="feature_latest_after_rolling")
                if recent.empty:
                    return pd.DataFrame()

                latest = recent.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"]).groupby("symbol", group_keys=False).tail(1)
                keep_cols = [
                    "symbol", "datetime", "close", "volume", avg_col, ratio_col,
                    prev_close_col, price_chg_col, up_streak_col, down_streak_col, last_delta_col,
                ]
                for c in keep_cols:
                    if c not in latest.columns:
                        latest[c] = pd.NA
                latest = latest[keep_cols].copy().rename(
                    columns={"datetime": f"datetime_{interval_i}m", "close": f"close_{interval_i}m", "volume": f"volume_{interval_i}m"}
                )
                for c in [up_streak_col, down_streak_col]:
                    latest[c] = pd.to_numeric(latest[c], errors="coerce").fillna(0).astype(int)
                latest[last_delta_col] = pd.to_numeric(latest[last_delta_col], errors="coerce").fillna(0.0)

                logger.warning(
                    "[TONOSAMA SURGE ROLLING PATCH] %sm latest rows=%s ratio_nonnull=%s up_ge3=%s down_ge3=%s head=%s",
                    interval_i,
                    len(latest),
                    int(pd.to_numeric(latest[ratio_col], errors="coerce").notna().sum()) if ratio_col in latest.columns else 0,
                    int((latest[up_streak_col] >= 3).sum()),
                    int((latest[down_streak_col] >= 3).sum()),
                    latest[[c for c in ["symbol", f"close_{interval_i}m", up_streak_col, down_streak_col, last_delta_col, ratio_col, price_chg_col] if c in latest.columns]].head(12).to_dict("records"),
                )
                return latest.reset_index(drop=True)
            except Exception:
                logger.exception("[TONOSAMA SURGE ROLLING PATCH] patched add_volume_surge_features failed interval=%s", interval)
                return _ORIGINAL_ADD_SURGE(df, interval=interval) if callable(_ORIGINAL_ADD_SURGE) else pd.DataFrame()

        _patched_add_volume_surge_features._rolling_before_recent_patch = True  # type: ignore[attr-defined]
        _patched_add_volume_surge_features._original = cur  # type: ignore[attr-defined]
        vs.add_volume_surge_features = _patched_add_volume_surge_features
        logger.warning("[TONOSAMA SURGE ROLLING PATCH] installed compute rolling before recent filter")
    except Exception:
        logger.exception("[TONOSAMA SURGE ROLLING PATCH] install failed")


def _bool_series(df: pd.DataFrame, col: str, default: bool = False) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="bool")
    try:
        s = df[col]
        if getattr(s, "dtype", None) == bool:
            return s.fillna(default).astype(bool)
        return s.fillna(default).astype(str).str.strip().str.lower().isin(_TRUE)
    except Exception:
        return pd.Series(default, index=df.index, dtype="bool")


def _sample(df: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    cols = [c for c in [
        "symbol", "symbolname", "close", "_max_volume_surge_ratio", "_max_price_change_pct",
        "_surge_tf", "_volume_surge_history_missing", "_volume_surge_failopen",
        "_body_change_pct", "_intrabar_range_pct", "_slope", "price_change_5s_pct",
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


def _drop_history_missing_failopen(df: pd.DataFrame, *, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    has_missing = "_volume_surge_history_missing" in df.columns
    has_failopen = "_volume_surge_failopen" in df.columns
    if not has_missing and not has_failopen:
        return df

    missing = _bool_series(df, "_volume_surge_history_missing", False) if has_missing else pd.Series(False, index=df.index)
    failopen = _bool_series(df, "_volume_surge_failopen", False) if has_failopen else pd.Series(False, index=df.index)
    hist_mask = missing | failopen
    affected = int(hist_mask.sum()) if bool(hist_mask.any()) else 0
    if affected <= 0:
        return df

    allow = _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", False)
    drop = _env_bool("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", True)
    if allow and not drop:
        logger.warning(
            "[TONOSAMA HISTORY GUARD] explicitly pass-through history-missing/failopen rows stage=%s rows=%s affected=%s sample=%s",
            stage,
            len(df),
            affected,
            _sample(df.loc[hist_mask].copy()),
        )
        return df

    out = df.loc[~hist_mask].copy()
    logger.warning(
        "[TONOSAMA HISTORY GUARD] fail-close dropped history-missing/failopen rows stage=%s before=%s after=%s dropped=%s allow=%s drop=%s sample=%s",
        stage,
        len(df),
        len(out),
        affected,
        allow,
        drop,
        _sample(df.loc[hist_mask].copy()),
    )
    return out


def _patched_build_scalping_feature_df(*args, **kwargs):
    df = _ORIGINAL_BUILD(*args, **kwargs) if callable(_ORIGINAL_BUILD) else pd.DataFrame()
    if isinstance(df, pd.DataFrame):
        return _drop_history_missing_failopen(df, stage="build_scalping_feature_df")
    return df


def install() -> bool:
    global _PATCHED, _ORIGINAL_BUILD
    if _PATCHED:
        return True

    _disable_surge_failopen_defaults()
    _patch_volatility_filter_thresholds()
    _patch_volume_surge_rolling_before_recent()

    _setdefault_env("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", "0")
    _setdefault_env("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", "1")

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
        if getattr(cur, "_tonosama_history_guard_patch", False):
            _PATCHED = True
            return True
        _ORIGINAL_BUILD = cur
        _patched_build_scalping_feature_df._tonosama_history_guard_patch = True  # type: ignore[attr-defined]
        _patched_build_scalping_feature_df._original = cur  # type: ignore[attr-defined]
        runner.build_scalping_feature_df = _patched_build_scalping_feature_df
        _PATCHED = True
        logger.warning(
            "[TONOSAMA HISTORY GUARD] installed v2.5 fail_close allow=%s drop=%s explicit_failopen=%s",
            _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", False),
            _env_bool("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", True),
            _env_bool("TONOSAMA_EXPLICIT_ALLOW_SURGE_FAILOPEN", False),
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
