# ============================================================
# File   : trading/entry/tonosama/volume_surge.py
# Version: Ver1.8-TONOSAMA-RECENT-SAFE-FAILOPEN
# ------------------------------------------------------------
# 目的:
#   殿様エントリー用の出来高急増・価格変化特徴量を作る。
#
# Ver1.8:
#   - Ver1.7 の recent-only は維持する。
#   - ただし recent filter 後の当日直近行だけで 3m/5m 出来高履歴が不足する場合は、
#     TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING または
#     TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY が有効なら safe fail-open する。
#   - 10:51〜10:52ログのように legacy_failopen=True / allow_without_history=True なのに
#     no fail-open -> return empty になる問題を修正。
#   - 古い前日/Yahoo行は recent filter で除外済みなので、全銘柄無条件通過ではない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os

import pandas as pd

from .config import VOLUME_AVG_LOOKBACK_BARS
from .summary_loader import load_merged_summary, normalize_summary_base
from .utils import safe_float

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
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


def _now_naive() -> dt.datetime:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive().replace(tzinfo=None)
    except Exception:
        return dt.datetime.now()


def _normalize_datetime_col(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if "datetime" not in df.columns:
        return df.copy()
    x = df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
    return x.dropna(subset=["datetime"])


def _filter_recent_rows(df: pd.DataFrame, *, interval: int, label: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if not _env_bool("TONOSAMA_RECENT_ONLY", True):
        return df.copy()
    if "datetime" not in df.columns:
        return df.copy()

    x = _normalize_datetime_col(df)
    if x.empty:
        return x

    now = _now_naive()
    max_age_min = max(1.0, _env_float("TONOSAMA_RECENT_MAX_AGE_MIN", 30.0))
    cutoff = pd.Timestamp(now - dt.timedelta(minutes=max_age_min))
    today = pd.Timestamp(now.date())

    before = len(x)
    x = x[(x["datetime"] >= cutoff) & (x["datetime"] >= today)].copy()
    logger.warning(
        "[TONOSAMA SURGE] recent filter label=%s interval=%s before=%s after=%s cutoff=%s today=%s",
        label,
        interval,
        before,
        len(x),
        cutoff,
        today.date(),
    )
    return x


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _intrabar_price_change_pct(df: pd.DataFrame, interval: int) -> pd.Series:
    try:
        close_col = _first_existing(df, [f"close_{interval}m", "close", "close_price", "current_price", "price"])
        open_col = _first_existing(df, [f"open_{interval}m", "open", "open_price"])
        if close_col is None or open_col is None:
            return pd.Series(pd.NA, index=df.index, dtype="float64")
        close = pd.to_numeric(df[close_col], errors="coerce")
        open_ = pd.to_numeric(df[open_col], errors="coerce")
        return ((close - open_) / open_.replace(0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA)
    except Exception:
        logger.debug("[TONOSAMA SURGE] intrabar price change failed interval=%s", interval, exc_info=True)
        return pd.Series(pd.NA, index=df.index if df is not None else None, dtype="float64")


def add_volume_surge_features(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    x = normalize_summary_base(df, interval=interval)
    x = _filter_recent_rows(x, interval=interval, label="feature_source")
    if x.empty:
        return pd.DataFrame()

    interval = int(interval)
    x = x.sort_values(["symbol", "datetime"])
    g = x.groupby("symbol", group_keys=False)

    avg_col = f"prev{VOLUME_AVG_LOOKBACK_BARS}_volume_avg_{interval}m"
    ratio_col = f"volume_surge_ratio_{interval}m"

    x[avg_col] = g["volume"].transform(
        lambda s: s.shift(1).rolling(VOLUME_AVG_LOOKBACK_BARS, min_periods=2).mean()
    )
    x[ratio_col] = x["volume"] / x[avg_col].replace(0, pd.NA)
    x[ratio_col] = pd.to_numeric(x[ratio_col], errors="coerce").replace([float("inf"), -float("inf")], pd.NA)

    prev_close_col = f"prev_close_{interval}m"
    price_chg_col = f"price_change_pct_{interval}m"
    x[prev_close_col] = g["close"].shift(1)
    x[price_chg_col] = ((x["close"] - x[prev_close_col]) / x[prev_close_col].replace(0, pd.NA) * 100.0)
    x[price_chg_col] = pd.to_numeric(x[price_chg_col], errors="coerce").replace([float("inf"), -float("inf")], pd.NA)

    if x[price_chg_col].isna().all():
        fallback_chg = _intrabar_price_change_pct(x, interval)
        if fallback_chg.notna().any():
            x[price_chg_col] = fallback_chg
            logger.warning(
                "[TONOSAMA SURGE] price_change fallback open_to_close interval=%sm rows=%s nonnull=%s",
                interval,
                len(x),
                int(fallback_chg.notna().sum()),
            )

    latest = x.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"]).groupby("symbol", group_keys=False).tail(1)

    keep_cols = ["symbol", "datetime", "close", "volume", avg_col, ratio_col, prev_close_col, price_chg_col]
    for c in keep_cols:
        if c not in latest.columns:
            latest[c] = pd.NA

    latest = latest[keep_cols].copy().rename(
        columns={"datetime": f"datetime_{interval}m", "close": f"close_{interval}m", "volume": f"volume_{interval}m"}
    )
    return latest.reset_index(drop=True)


def _ensure_open_close_aliases(out: pd.DataFrame) -> pd.DataFrame:
    if out is None or out.empty:
        return pd.DataFrame()
    x = out.copy()
    for src, dst in [("open_price", "open"), ("high_price", "high"), ("low_price", "low"), ("close_price", "close")]:
        if src in x.columns and dst not in x.columns:
            x[dst] = pd.to_numeric(x[src], errors="coerce")
    return x


def _fallback_price_change_from_1m(out: pd.DataFrame) -> pd.Series:
    try:
        x = _ensure_open_close_aliases(out)
        return _intrabar_price_change_pct(x, 1)
    except Exception:
        return pd.Series(pd.NA, index=out.index if out is not None else None, dtype="float64")


def _default_failopen_value() -> float:
    try:
        from .config import MIN_VOLUME_SURGE_RATIO
        return max(3.0, float(MIN_VOLUME_SURGE_RATIO))
    except Exception:
        return 3.0


def _safe_failopen_enabled() -> bool:
    failopen = _env_bool("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", True)
    allow = _env_bool("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", True)
    force = _env_bool("TONOSAMA_ALLOW_DANGEROUS_SURGE_FAILOPEN", False)
    return bool(failopen or allow or force)


def _apply_history_unavailable_policy(out: pd.DataFrame) -> pd.DataFrame:
    if out is None or out.empty:
        return pd.DataFrame()

    x = out.copy()
    ratio_cols = [c for c in ["volume_surge_ratio_3m", "volume_surge_ratio_5m"] if c in x.columns]
    price_cols = [c for c in ["price_change_pct_3m", "price_change_pct_5m"] if c in x.columns]

    for c in ratio_cols + price_cols:
        x[c] = pd.to_numeric(x[c], errors="coerce").replace([float("inf"), -float("inf")], pd.NA)

    failopen_enabled = _safe_failopen_enabled()
    allow_without_history = _env_bool("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", True)
    legacy_failopen = _env_bool("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", True)
    force = _env_bool("TONOSAMA_ALLOW_DANGEROUS_SURGE_FAILOPEN", False)
    failopen_value = _env_float("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", _default_failopen_value())

    if ratio_cols:
        ratio_df = x[ratio_cols]
        ratio_missing_all = ratio_df.isna().all(axis=1)
        if bool(ratio_missing_all.any()):
            x.loc[ratio_missing_all, "_volume_surge_history_missing"] = True
            if failopen_enabled:
                for c in ratio_cols:
                    x.loc[ratio_missing_all, c] = failopen_value
                x.loc[ratio_missing_all, "_volume_surge_failopen"] = True
                logger.warning(
                    "[TONOSAMA SURGE] volume_surge history missing -> safe fail-open rows=%s ratio_cols=%s value=%.3f legacy_failopen=%s allow_without_history=%s force=%s",
                    int(ratio_missing_all.sum()), ratio_cols, failopen_value, legacy_failopen, allow_without_history, force,
                )
            else:
                for c in ratio_cols:
                    x.loc[ratio_missing_all, c] = 0.0
                x.loc[ratio_missing_all, "_volume_surge_failopen"] = False
                logger.warning(
                    "[TONOSAMA SURGE] volume_surge history missing -> no fail-open rows=%s ratio_cols=%s value=0.0 legacy_failopen=%s allow_without_history=%s force=%s",
                    int(ratio_missing_all.sum()), ratio_cols, legacy_failopen, allow_without_history, force,
                )

    if price_cols:
        price_df = x[price_cols]
        price_missing_all = price_df.isna().all(axis=1)
        if bool(price_missing_all.any()):
            fallback_1m = _fallback_price_change_from_1m(x)
            if fallback_1m.notna().any():
                for c in price_cols:
                    x.loc[price_missing_all, c] = fallback_1m.loc[price_missing_all]
                x.loc[price_missing_all, "_price_change_fallback_1m"] = True
                logger.warning(
                    "[TONOSAMA SURGE] price_change fallback from 1m open_to_close rows=%s price_cols=%s nonnull=%s",
                    int(price_missing_all.sum()), price_cols, int(fallback_1m.notna().sum()),
                )
            else:
                for c in price_cols:
                    x.loc[price_missing_all, c] = 0.0

    return x


def _all_surge_history_missing(df3: pd.DataFrame, df5: pd.DataFrame) -> bool:
    vals = []
    for df, col in ((df3, "volume_surge_ratio_3m"), (df5, "volume_surge_ratio_5m")):
        if isinstance(df, pd.DataFrame) and not df.empty and col in df.columns:
            vals.append(pd.to_numeric(df[col], errors="coerce").notna().any())
    return not any(vals)


def build_scalping_feature_df() -> pd.DataFrame:
    raw1 = normalize_summary_base(load_merged_summary(1), interval=1)
    df1 = _filter_recent_rows(raw1, interval=1, label="base_1m")
    if df1.empty:
        logger.warning("[TONOSAMA SURGE] base 1m recent empty raw_rows=%s", len(raw1) if isinstance(raw1, pd.DataFrame) else 0)
        return pd.DataFrame()

    df3 = add_volume_surge_features(load_merged_summary(3), interval=3)
    df5 = add_volume_surge_features(load_merged_summary(5), interval=5)

    missing_history = _all_surge_history_missing(df3, df5)
    can_failopen = _safe_failopen_enabled()
    if missing_history and not can_failopen:
        logger.warning(
            "[TONOSAMA SURGE] no usable 3m/5m volume surge history after recent filter -> return empty base_rows=%s df3=%s df5=%s failopen=%s",
            len(df1),
            len(df3) if isinstance(df3, pd.DataFrame) else 0,
            len(df5) if isinstance(df5, pd.DataFrame) else 0,
            can_failopen,
        )
        return pd.DataFrame()

    out = df1.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"]).groupby("symbol", group_keys=False).tail(1).copy()
    if out.empty:
        return pd.DataFrame()

    if not df3.empty:
        out = out.merge(df3, on="symbol", how="left")
    if not df5.empty:
        out = out.merge(df5, on="symbol", how="left")

    for c in [
        "volume_surge_ratio_3m", "volume_surge_ratio_5m",
        "price_change_pct_3m", "price_change_pct_5m",
        "prev5_volume_avg_3m", "prev5_volume_avg_5m",
        "volume_3m", "volume_5m",
    ]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    out = _apply_history_unavailable_policy(out)
    if out.empty:
        return pd.DataFrame()

    vol_cols = [c for c in ["volume_surge_ratio_3m", "volume_surge_ratio_5m"] if c in out.columns]
    price_cols = [c for c in ["price_change_pct_3m", "price_change_pct_5m"] if c in out.columns]

    out["_max_volume_surge_ratio"] = out[vol_cols].max(axis=1, skipna=True) if vol_cols else 0.0
    out["_max_price_change_pct"] = out[price_cols].max(axis=1, skipna=True) if price_cols else 0.0
    out["_max_volume_surge_ratio"] = pd.to_numeric(out["_max_volume_surge_ratio"], errors="coerce").fillna(0.0)
    out["_max_price_change_pct"] = pd.to_numeric(out["_max_price_change_pct"], errors="coerce").fillna(0.0)

    history_missing = out.get("_volume_surge_history_missing", pd.Series(False, index=out.index)).fillna(False).astype(bool)
    failopen_col = out.get("_volume_surge_failopen", pd.Series(False, index=out.index)).fillna(False).astype(bool)
    if bool(history_missing.all()) and not bool(failopen_col.any()):
        logger.warning(
            "[TONOSAMA SURGE] all rows missing volume surge history -> return empty rows=%s reason=require_surge_history failopen=%s",
            len(out), can_failopen,
        )
        return pd.DataFrame()

    out["_surge_tf"] = ""
    if "volume_surge_ratio_3m" in out.columns and "volume_surge_ratio_5m" in out.columns:
        out["_surge_tf"] = out.apply(
            lambda r: "3m" if safe_float(r.get("volume_surge_ratio_3m"), 0) >= safe_float(r.get("volume_surge_ratio_5m"), 0) else "5m",
            axis=1,
        )
    elif "volume_surge_ratio_3m" in out.columns:
        out["_surge_tf"] = "3m"
    elif "volume_surge_ratio_5m" in out.columns:
        out["_surge_tf"] = "5m"

    try:
        failopen_col = out.get("_volume_surge_failopen", pd.Series(False, index=out.index)).fillna(False).astype(bool)
        history_missing_col = out.get("_volume_surge_history_missing", pd.Series(False, index=out.index)).fillna(False).astype(bool)
        logger.warning(
            "[TONOSAMA SURGE] feature summary rows=%s vol_cols=%s price_cols=%s volume_surge_nonzero=%s price_change_nonzero=%s history_missing_rows=%s failopen_rows=%s price_fallback_rows=%s head=%s",
            len(out), vol_cols, price_cols,
            int((out["_max_volume_surge_ratio"].fillna(0) != 0).sum()),
            int((out["_max_price_change_pct"].fillna(0) != 0).sum()),
            int(history_missing_col.sum()),
            int(failopen_col.sum()),
            int(out.get("_price_change_fallback_1m", pd.Series(False, index=out.index)).fillna(False).astype(bool).sum()),
            out[[c for c in ["symbol", "symbolname", "close", "_max_volume_surge_ratio", "_max_price_change_pct", "_surge_tf", "_volume_surge_history_missing", "_volume_surge_failopen", "_price_change_fallback_1m"] if c in out.columns]].head(12).to_dict("records"),
        )
    except Exception:
        logger.debug("[TONOSAMA SURGE] feature summary log failed", exc_info=True)

    return out.reset_index(drop=True)
