# ============================================================
# File   : trading/entry/tonosama/runner.py
# Version: Ver1.9-TONOSAMA-FINAL-SLOPE-STRONG-MOVE-FAILOPEN
# ------------------------------------------------------------
# ✔ 5秒足は必須にしない。
# ✔ REQUIRE_5SEC_BAR=False の場合、5秒足が取れていても 0.0% 横ばいだけでは落とさない。
# ✔ 5秒足で MAX_5SEC_DROP_PCT 以下の強い逆行だけ落とす。
# ✔ REQUIRE_5SEC_BAR=True の場合のみ、従来通り 5秒足の正方向変化を要求する。
# ✔ 出来高急増だけで高値掴み/安値売りしない。
# ✔ Ver1.8:
#   - BUY: 上ヒゲ大 + 終値が安値圏なら、上昇率が小さくても除外
#   - SELL: 下ヒゲ大 + 終値が高値圏なら、下落率が小さくても除外
#   - 11:09ログの upper_wick=90%超 / close_pos=3〜10% のBUY通過を防止
# ✔ Ver1.9:
#   - final段階の slope_abs_too_small で _slope=0.0 の候補が全落ちする問題を補正
#   - 価格変化・値幅・出来高が十分なら slope filter を fail-open
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

import pandas as pd

try:
    from trading.ranking.active_symbol_manager import update_active_symbols
except Exception:
    update_active_symbols = None

from .config import (
    MIN_PRICE,
    MIN_FINAL_SCORE,
    MIN_VOLUME_SURGE_RATIO,
    MIN_PRICE_CHANGE_PCT,
    MIN_SLOPE,
    MIN_BODY_CHANGE_PCT,
    MIN_INTRABAR_RANGE_PCT,
    MIN_LATEST_VOLUME,
    MIN_5SEC_PRICE_CHANGE_PCT,
    MAX_5SEC_DROP_PCT,
    REQUIRE_5SEC_BAR,
    USE_5SEC_CONFIRM,
    MIN_RAW_SCORE,
    MAX_PENDING_PER_LOOP,
    MAX_CANDIDATES,
    MAX_5SEC_FEATURE_SYMBOLS,
    MAX_BUY_PRICE_CHANGE_PCT,
    MAX_BUY_CLOSE_POSITION_PCT,
    MAX_BUY_UPPER_WICK_PCT,
    BUYING_CLIMAX_MIN_SURGE_RATIO,
    BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT,
    MAX_SELL_PRICE_DROP_PCT,
    MIN_SELL_CLOSE_POSITION_PCT,
    MAX_SELL_LOWER_WICK_PCT,
    SELLING_CLIMAX_MIN_SURGE_RATIO,
    SELLING_CLIMAX_MIN_PRICE_DROP_PCT,
)
from .time_guard import is_market_time
from .volume_surge import build_scalping_feature_df
from .five_sec_features import build_5sec_features
from .scoring import prepare_entry_scores, calc_final_score_safe
from .ai_gate import ai_check_tonosama_entry
from .pending_writer import (
    has_tonosama_pending,
    build_pending_entry,
    add_tonosama_pending,
    prune_expired_tonosama_pending,
)
from .notifier import notify_discord_tonosama_pending
from .utils import normalize_symbol, safe_float

logger = logging.getLogger(__name__)
_last_loop_at: dt.datetime | None = None
_LAST_FILTER_DIAG: dict[str, Any] = {}

BUY_REJECTED_CLOSE_POSITION_PCT = 35.0
SELL_REJECTED_CLOSE_POSITION_PCT = 65.0


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
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def _bool_series(df: pd.DataFrame, col: str, default: bool = False) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="bool")
    return df[col].fillna(default).astype(bool)


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    try:
        for n in names:
            if n in df.columns:
                return n
    except Exception:
        pass
    return None


def _ensure_actual_movement_cols(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    close_col = _first_existing(x, ["close", "close_price", "current_price", "price", "close_1m"])
    open_col = _first_existing(x, ["open", "open_price", "open_1m"])
    high_col = _first_existing(x, ["high", "high_price", "high_1m"])
    low_col = _first_existing(x, ["low", "low_price", "low_1m"])
    volume_col = _first_existing(x, ["volume", "volume_1m", "latest_volume", "latest_1m_volume"])

    x["_tonosama_close_for_move"] = pd.to_numeric(x[close_col], errors="coerce") if close_col else 0.0

    if open_col:
        open_s = pd.to_numeric(x[open_col], errors="coerce")
        close_s = pd.to_numeric(x["_tonosama_close_for_move"], errors="coerce")
        x["_body_change_pct"] = ((close_s - open_s).abs() / open_s.replace(0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
        x["_signed_body_change_pct"] = ((close_s - open_s) / open_s.replace(0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
    else:
        x["_body_change_pct"] = _num_series(x, "_max_price_change_pct", 0.0).abs()
        x["_signed_body_change_pct"] = _num_series(x, "_max_price_change_pct", 0.0)

    if high_col and low_col:
        high_s = pd.to_numeric(x[high_col], errors="coerce")
        low_s = pd.to_numeric(x[low_col], errors="coerce")
        close_s = pd.to_numeric(x["_tonosama_close_for_move"], errors="coerce")
        open_s = pd.to_numeric(x[open_col], errors="coerce") if open_col else close_s
        rng = (high_s - low_s).abs().replace(0, pd.NA)
        x["_intrabar_range_pct"] = ((high_s - low_s).abs() / close_s.where(close_s > 0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
        x["_close_position_pct"] = ((close_s - low_s) / rng * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(50.0).clip(0.0, 100.0)
        x["_upper_wick_pct"] = ((high_s - pd.concat([open_s, close_s], axis=1).max(axis=1)) / rng * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0).clip(lower=0.0)
        x["_lower_wick_pct"] = ((pd.concat([open_s, close_s], axis=1).min(axis=1) - low_s) / rng * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0).clip(lower=0.0)
    else:
        x["_intrabar_range_pct"] = 0.0
        x["_close_position_pct"] = 50.0
        x["_upper_wick_pct"] = 0.0
        x["_lower_wick_pct"] = 0.0

    x["_latest_volume"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0
    return x


def _sample_rows(df: pd.DataFrame, cols: list[str], limit: int = 8) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    use_cols = [c for c in cols if c in df.columns]
    for _, row in df.head(limit).iterrows():
        item: dict[str, Any] = {}
        for c in use_cols:
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


def _log_filter_step(*, stage: str, before: pd.DataFrame, after: pd.DataFrame, reason: str, threshold: Any, sample_cols: list[str]) -> None:
    try:
        before_rows = 0 if before is None else len(before)
        after_rows = 0 if after is None else len(after)
        dropped = max(0, before_rows - after_rows)
        if dropped <= 0:
            logger.info("[TONOSAMA FILTER PASS] stage=%s reason=%s before=%s after=%s threshold=%s", stage, reason, before_rows, after_rows, threshold)
            return
        after_index = set(after.index) if after is not None and not after.empty else set()
        dropped_df = before.loc[[idx for idx in before.index if idx not in after_index]].copy() if before is not None and not before.empty else pd.DataFrame()
        logger.warning("[TONOSAMA FILTER DROP] stage=%s reason=%s before=%s after=%s dropped=%s threshold=%s sample=%s", stage, reason, before_rows, after_rows, dropped, threshold, _sample_rows(dropped_df, sample_cols, limit=10))
    except Exception:
        logger.debug("[TONOSAMA FILTER] step log failed stage=%s reason=%s", stage, reason, exc_info=True)


def _strong_move_slope_failopen_mask(x: pd.DataFrame) -> pd.Series:
    """_slope が0/欠損でも、値動き・値幅・出来高が十分ならfinal slope filterを通す。"""
    if x is None or x.empty or not _env_bool("TONOSAMA_FINAL_SLOPE_STRONG_MOVE_FAILOPEN", True):
        return pd.Series(False, index=x.index if x is not None else None, dtype="bool")
    price_abs = _num_series(x, "_max_price_change_pct", 0.0).abs()
    body_abs = _num_series(x, "_body_change_pct", 0.0).abs()
    range_pct = _num_series(x, "_intrabar_range_pct", 0.0).abs()
    latest_vol = _num_series(x, "_latest_volume", 0.0).combine(_num_series(x, "volume", 0.0), max)
    surge = _num_series(x, "_max_volume_surge_ratio", 0.0)
    slope_abs = _num_series(x, "_slope", 0.0).abs()

    min_price_change = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_PRICE_CHANGE_PCT", 0.5)
    min_body = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_BODY_PCT", 0.0)
    min_range = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_RANGE_PCT", 1.0)
    min_volume = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_VOLUME", 50000.0)
    min_surge = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_SURGE", 0.0)
    max_slope_abs = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MAX_SLOPE_ABS", max(MIN_SLOPE, 0.001))

    strong_move = (
        (slope_abs < max_slope_abs)
        & ((price_abs >= min_price_change) | (body_abs >= min_body if min_body > 0 else price_abs >= min_price_change))
        & (range_pct >= min_range)
        & (latest_vol >= min_volume)
        & (surge >= min_surge)
    )
    try:
        if strong_move.any():
            logger.warning(
                "[TONOSAMA FINAL SLOPE FAILOPEN] rescued=%s min_price_change=%.3f min_body=%.3f min_range=%.3f min_volume=%.0f min_surge=%.2f sample=%s",
                int(strong_move.sum()), min_price_change, min_body, min_range, min_volume, min_surge,
                _sample_rows(x.loc[strong_move], ["symbol", "symbolname", "close", "_latest_volume", "_max_price_change_pct", "_body_change_pct", "_intrabar_range_pct", "_max_volume_surge_ratio", "_slope", "_tonosama_score"], limit=10),
            )
    except Exception:
        logger.debug("[TONOSAMA FINAL SLOPE FAILOPEN] log failed", exc_info=True)
    return strong_move.fillna(False).astype(bool)


def _diagnose_base_frame(x: pd.DataFrame) -> None:
    try:
        if x is None or x.empty:
            logger.warning("[TONOSAMA FILTER DIAG] base empty")
            return
        summary = {
            "rows": len(x),
            "symbols": int(x["symbol"].astype(str).nunique()) if "symbol" in x.columns else 0,
            "cols": list(x.columns)[:80],
            "min_price": MIN_PRICE,
            "min_volume_surge": MIN_VOLUME_SURGE_RATIO,
            "min_price_change_pct": MIN_PRICE_CHANGE_PCT,
            "min_slope": MIN_SLOPE,
            "min_body_change_pct": MIN_BODY_CHANGE_PCT,
            "min_intrabar_range_pct": MIN_INTRABAR_RANGE_PCT,
            "min_latest_volume": MIN_LATEST_VOLUME,
            "min_raw_score": MIN_RAW_SCORE,
            "max_buy_price_change_pct": MAX_BUY_PRICE_CHANGE_PCT,
            "max_sell_price_drop_pct": MAX_SELL_PRICE_DROP_PCT,
            "buy_rejected_close_position_pct": BUY_REJECTED_CLOSE_POSITION_PCT,
            "sell_rejected_close_position_pct": SELL_REJECTED_CLOSE_POSITION_PCT,
            "use_5sec_confirm": USE_5SEC_CONFIRM,
            "require_5sec_bar": REQUIRE_5SEC_BAR,
            "min_5sec_price_change_pct": MIN_5SEC_PRICE_CHANGE_PCT,
            "max_5sec_drop_pct": MAX_5SEC_DROP_PCT,
        }
        metrics = {}
        for col in ["close", "_max_volume_surge_ratio", "_max_price_change_pct", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_latest_volume", "_slope", "_tonosama_score"]:
            if col in x.columns:
                s = pd.to_numeric(x[col], errors="coerce")
                metrics[col] = {"nonnull": int(s.notna().sum()), "min": round(float(s.min()), 6) if s.notna().any() else None, "max": round(float(s.max()), 6) if s.notna().any() else None, "mean": round(float(s.mean()), 6) if s.notna().any() else None, "zero": int((s.fillna(0.0) == 0.0).sum())}
        logger.warning("[TONOSAMA FILTER DIAG] base summary=%s metrics=%s head=%s", summary, metrics, _sample_rows(x, ["symbol", "symbolname", "close", "_max_volume_surge_ratio", "_max_price_change_pct", "_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_latest_volume", "_slope", "score", "final_score", "score_mtf"], limit=15))
    except Exception:
        logger.debug("[TONOSAMA FILTER DIAG] base diagnose failed", exc_info=True)


def _apply_climax_guards(x: pd.DataFrame, *, stage: str, sample_cols: list[str]) -> pd.DataFrame:
    if x is None or x.empty:
        return pd.DataFrame()

    surge = _num_series(x, "_max_volume_surge_ratio")
    price_chg = _num_series(x, "_max_price_change_pct")
    signed_body = _num_series(x, "_signed_body_change_pct")
    close_pos = _num_series(x, "_close_position_pct", 50.0)
    upper_wick = _num_series(x, "_upper_wick_pct")
    slope = _num_series(x, "_slope")

    buy_like = (price_chg > 0) | (signed_body > 0) | (slope > 0)
    buy_too_late = buy_like & (price_chg >= MAX_BUY_PRICE_CHANGE_PCT)
    buy_high_zone = buy_like & (close_pos >= MAX_BUY_CLOSE_POSITION_PCT) & (price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT)
    buy_upper_wick_reversal = buy_like & (upper_wick >= MAX_BUY_UPPER_WICK_PCT) & (close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT)
    buying_climax = buy_like & (surge >= BUYING_CLIMAX_MIN_SURGE_RATIO) & (
        ((price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT) & (close_pos >= MAX_BUY_CLOSE_POSITION_PCT))
        | ((upper_wick >= MAX_BUY_UPPER_WICK_PCT) & (close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT))
    )
    before = x.copy()
    x = x[~(buy_too_late | buy_high_zone | buy_upper_wick_reversal | buying_climax)]
    _log_filter_step(
        stage=stage,
        before=before,
        after=x,
        reason="buying_climax_or_upper_wick_reversal_guard",
        threshold={
            "MAX_BUY_PRICE_CHANGE_PCT": MAX_BUY_PRICE_CHANGE_PCT,
            "MAX_BUY_CLOSE_POSITION_PCT": MAX_BUY_CLOSE_POSITION_PCT,
            "MAX_BUY_UPPER_WICK_PCT": MAX_BUY_UPPER_WICK_PCT,
            "BUY_REJECTED_CLOSE_POSITION_PCT": BUY_REJECTED_CLOSE_POSITION_PCT,
            "BUYING_CLIMAX_MIN_SURGE_RATIO": BUYING_CLIMAX_MIN_SURGE_RATIO,
            "BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT": BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT,
        },
        sample_cols=sample_cols,
    )

    if x.empty:
        return x

    surge = _num_series(x, "_max_volume_surge_ratio")
    price_chg = _num_series(x, "_max_price_change_pct")
    signed_body = _num_series(x, "_signed_body_change_pct")
    close_pos = _num_series(x, "_close_position_pct", 50.0)
    lower_wick = _num_series(x, "_lower_wick_pct")
    slope = _num_series(x, "_slope")

    drop_abs = price_chg.abs()
    sell_like = (price_chg < 0) | (signed_body < 0) | (slope < 0)
    sell_too_late = sell_like & (drop_abs >= MAX_SELL_PRICE_DROP_PCT)
    sell_low_zone = sell_like & (close_pos <= MIN_SELL_CLOSE_POSITION_PCT) & (drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT)
    sell_lower_wick_reversal = sell_like & (lower_wick >= MAX_SELL_LOWER_WICK_PCT) & (close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT)
    selling_climax = sell_like & (surge >= SELLING_CLIMAX_MIN_SURGE_RATIO) & (
        ((drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT) & (close_pos <= MIN_SELL_CLOSE_POSITION_PCT))
        | ((lower_wick >= MAX_SELL_LOWER_WICK_PCT) & (close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT))
    )
    before = x.copy()
    x = x[~(sell_too_late | sell_low_zone | sell_lower_wick_reversal | selling_climax)]
    _log_filter_step(
        stage=stage,
        before=before,
        after=x,
        reason="selling_climax_or_lower_wick_reversal_guard",
        threshold={
            "MAX_SELL_PRICE_DROP_PCT": MAX_SELL_PRICE_DROP_PCT,
            "MIN_SELL_CLOSE_POSITION_PCT": MIN_SELL_CLOSE_POSITION_PCT,
            "MAX_SELL_LOWER_WICK_PCT": MAX_SELL_LOWER_WICK_PCT,
            "SELL_REJECTED_CLOSE_POSITION_PCT": SELL_REJECTED_CLOSE_POSITION_PCT,
            "SELLING_CLIMAX_MIN_SURGE_RATIO": SELLING_CLIMAX_MIN_SURGE_RATIO,
            "SELLING_CLIMAX_MIN_PRICE_DROP_PCT": SELLING_CLIMAX_MIN_PRICE_DROP_PCT,
        },
        sample_cols=sample_cols,
    )
    return x


def _apply_primary_filters(x: pd.DataFrame) -> pd.DataFrame:
    global _LAST_FILTER_DIAG
    _LAST_FILTER_DIAG = {}
    if x is None or x.empty:
        _LAST_FILTER_DIAG = {"stage": "primary", "base_rows": 0, "primary_rows": 0, "empty_reason": "base_empty"}
        return pd.DataFrame()
    x = _ensure_actual_movement_cols(x)
    base_rows = len(x)
    _diagnose_base_frame(x)
    sample_cols = ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "score", "final_score", "score_mtf", "mtf"]
    if "close" in x.columns:
        before = x.copy(); x = x[_num_series(x, "close") > MIN_PRICE]
        _log_filter_step(stage="primary", before=before, after=x, reason="close_below_min_price", threshold={"MIN_PRICE": MIN_PRICE}, sample_cols=sample_cols)
    before = x.copy(); x = x[_num_series(x, "_latest_volume") >= MIN_LATEST_VOLUME]
    _log_filter_step(stage="primary", before=before, after=x, reason="latest_volume_low_flat_alert_guard", threshold={"MIN_LATEST_VOLUME": MIN_LATEST_VOLUME}, sample_cols=sample_cols)
    before = x.copy(); x = x[_num_series(x, "_body_change_pct") >= MIN_BODY_CHANGE_PCT]
    _log_filter_step(stage="primary", before=before, after=x, reason="body_change_low_flat_alert_guard", threshold={"MIN_BODY_CHANGE_PCT": MIN_BODY_CHANGE_PCT}, sample_cols=sample_cols)
    before = x.copy(); x = x[_num_series(x, "_intrabar_range_pct") >= MIN_INTRABAR_RANGE_PCT]
    _log_filter_step(stage="primary", before=before, after=x, reason="intrabar_range_low_flat_alert_guard", threshold={"MIN_INTRABAR_RANGE_PCT": MIN_INTRABAR_RANGE_PCT}, sample_cols=sample_cols)
    if "_max_volume_surge_ratio" in x.columns:
        before = x.copy(); x = x[_num_series(x, "_max_volume_surge_ratio") >= MIN_VOLUME_SURGE_RATIO]
        _log_filter_step(stage="primary", before=before, after=x, reason="volume_surge_low", threshold={"MIN_VOLUME_SURGE_RATIO": MIN_VOLUME_SURGE_RATIO}, sample_cols=sample_cols)
    if "_max_price_change_pct" in x.columns:
        before = x.copy(); x = x[_num_series(x, "_max_price_change_pct").abs() >= MIN_PRICE_CHANGE_PCT]
        _log_filter_step(stage="primary", before=before, after=x, reason="price_change_low_abs", threshold={"MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT}, sample_cols=sample_cols)
    if "_slope" in x.columns:
        before = x.copy(); x = x[_num_series(x, "_slope").abs() >= MIN_SLOPE]
        _log_filter_step(stage="primary", before=before, after=x, reason="slope_abs_too_small", threshold={"MIN_SLOPE": MIN_SLOPE}, sample_cols=sample_cols)
    else:
        logger.warning("[TONOSAMA FILTER WARN] stage=primary missing _slope -> slope filter skipped cols=%s", list(x.columns))

    x = _apply_climax_guards(x, stage="primary", sample_cols=sample_cols)

    _LAST_FILTER_DIAG = {"stage": "primary", "base_rows": base_rows, "primary_rows": len(x), "thresholds": {"MIN_PRICE": MIN_PRICE, "MIN_LATEST_VOLUME": MIN_LATEST_VOLUME, "MIN_BODY_CHANGE_PCT": MIN_BODY_CHANGE_PCT, "MIN_INTRABAR_RANGE_PCT": MIN_INTRABAR_RANGE_PCT, "MIN_VOLUME_SURGE_RATIO": MIN_VOLUME_SURGE_RATIO, "MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT, "MIN_SLOPE": MIN_SLOPE, "MAX_BUY_PRICE_CHANGE_PCT": MAX_BUY_PRICE_CHANGE_PCT, "MAX_SELL_PRICE_DROP_PCT": MAX_SELL_PRICE_DROP_PCT, "BUY_REJECTED_CLOSE_POSITION_PCT": BUY_REJECTED_CLOSE_POSITION_PCT, "SELL_REJECTED_CLOSE_POSITION_PCT": SELL_REJECTED_CLOSE_POSITION_PCT}, "survivors": _sample_rows(x, sample_cols, limit=12)}
    logger.warning("[TONOSAMA FILTER SUMMARY] stage=primary base_rows=%s primary_rows=%s survivors=%s thresholds=%s", base_rows, len(x), _LAST_FILTER_DIAG.get("survivors"), _LAST_FILTER_DIAG.get("thresholds"))
    return x


def build_feature_df_with_5sec() -> pd.DataFrame:
    started = time.perf_counter()
    x = build_scalping_feature_df()
    if x is None or x.empty:
        logger.info("[TONOSAMA ENTRY] base feature empty")
        return pd.DataFrame()
    base_rows = len(x)
    x = _apply_primary_filters(x)
    primary_rows = len(x)
    if x.empty:
        logger.info("[TONOSAMA ENTRY] no candidates after primary filters base_rows=%s primary_rows=%s diag=%s elapsed=%.3fs", base_rows, primary_rows, _LAST_FILTER_DIAG, time.perf_counter() - started)
        return pd.DataFrame()
    try:
        x = prepare_entry_scores(x)
        if "_tonosama_score" in x.columns:
            x = x.sort_values("_tonosama_score", ascending=False)
    except Exception:
        logger.warning("[TONOSAMA ENTRY] pre 5sec prepare_entry_scores failed", exc_info=True)
    max_5sec = int(MAX_5SEC_FEATURE_SYMBOLS or 0)
    if max_5sec <= 0:
        max_5sec = int(MAX_CANDIDATES or 80)
    before_head = _sample_rows(x, ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score"], limit=12)
    x = x.head(min(max_5sec, MAX_CANDIDATES)).reset_index(drop=True)
    features = []
    feature_missing = 0
    for _, row in x.iterrows():
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        try:
            f = build_5sec_features(sym)
            if not isinstance(f, dict):
                f = {}
            if not f:
                feature_missing += 1
            f["symbol"] = sym
            features.append(f)
        except Exception:
            feature_missing += 1
            logger.warning("[TONOSAMA ENTRY] build_5sec_features failed symbol=%s", sym, exc_info=True)
            features.append({"symbol": sym})
    if features:
        x = x.merge(pd.DataFrame(features), on="symbol", how="left")
    for c in ["price_change_5s_pct", "volume_surge_ratio_5s", "latest_5sec_close", "latest_5sec_volume"]:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    x = prepare_entry_scores(x)
    logger.info("[TONOSAMA ENTRY] feature build done base_rows=%s primary_rows=%s five_sec_rows=%s feature_missing=%s pre_5sec_head=%s post_5sec_head=%s elapsed=%.3fs", base_rows, primary_rows, len(x), feature_missing, before_head, _sample_rows(x, ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score", "has_5sec_bar", "price_change_5s_pct", "volume_surge_ratio_5s"], limit=12), time.perf_counter() - started)
    return x


def _apply_5sec_filter(x: pd.DataFrame, sample_cols: list[str]) -> pd.DataFrame:
    if not USE_5SEC_CONFIRM or "has_5sec_bar" not in x.columns:
        return x
    if REQUIRE_5SEC_BAR:
        before = x.copy(); x = x[_bool_series(x, "has_5sec_bar")]
        _log_filter_step(stage="5sec", before=before, after=x, reason="missing_5sec_bar", threshold={"REQUIRE_5SEC_BAR": REQUIRE_5SEC_BAR}, sample_cols=sample_cols)
        before = x.copy(); has_bar = _bool_series(x, "has_5sec_bar"); chg_5s = _num_series(x, "price_change_5s_pct")
        x = x[(~has_bar) | (chg_5s.abs() >= MIN_5SEC_PRICE_CHANGE_PCT)]
        _log_filter_step(stage="5sec", before=before, after=x, reason="five_sec_price_change_abs_strict_ng", threshold={"MIN_5SEC_PRICE_CHANGE_PCT": MIN_5SEC_PRICE_CHANGE_PCT, "MAX_5SEC_DROP_PCT": MAX_5SEC_DROP_PCT, "REQUIRE_5SEC_BAR": REQUIRE_5SEC_BAR}, sample_cols=sample_cols)
        return x

    before = x.copy(); has_bar = _bool_series(x, "has_5sec_bar"); chg_5s = _num_series(x, "price_change_5s_pct")
    x = x[(~has_bar) | (chg_5s > MAX_5SEC_DROP_PCT)]
    _log_filter_step(stage="5sec", before=before, after=x, reason="five_sec_advisory_drop_only_strong_reverse", threshold={"MAX_5SEC_DROP_PCT": MAX_5SEC_DROP_PCT, "REQUIRE_5SEC_BAR": REQUIRE_5SEC_BAR, "MIN_5SEC_PRICE_CHANGE_PCT_IGNORED_WHEN_OPTIONAL": MIN_5SEC_PRICE_CHANGE_PCT}, sample_cols=sample_cols)
    return x


def iter_tonosama_candidate_rows() -> pd.DataFrame:
    started = time.perf_counter()
    x = build_feature_df_with_5sec()
    if x.empty:
        return pd.DataFrame()
    x = _ensure_actual_movement_cols(x)
    sample_cols = ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score", "has_5sec_bar", "price_change_5s_pct", "volume_surge_ratio_5s"]
    for reason, col, threshold in [
        ("close_below_min_price", "close", MIN_PRICE),
        ("latest_volume_low_flat_alert_guard", "_latest_volume", MIN_LATEST_VOLUME),
        ("body_change_low_flat_alert_guard", "_body_change_pct", MIN_BODY_CHANGE_PCT),
        ("intrabar_range_low_flat_alert_guard", "_intrabar_range_pct", MIN_INTRABAR_RANGE_PCT),
        ("volume_surge_low", "_max_volume_surge_ratio", MIN_VOLUME_SURGE_RATIO),
    ]:
        before = x.copy()
        if reason == "close_below_min_price":
            x = x[_num_series(x, col) > threshold]
        else:
            x = x[_num_series(x, col) >= threshold]
        _log_filter_step(stage="final", before=before, after=x, reason=reason, threshold={col: threshold}, sample_cols=sample_cols)

    before = x.copy(); x = x[_num_series(x, "_max_price_change_pct").abs() >= MIN_PRICE_CHANGE_PCT]
    _log_filter_step(stage="final", before=before, after=x, reason="price_change_low_abs", threshold={"MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT}, sample_cols=sample_cols)
    before = x.copy()
    slope_ok = _num_series(x, "_slope").abs() >= MIN_SLOPE
    slope_failopen = _strong_move_slope_failopen_mask(x)
    x = x[slope_ok | slope_failopen]
    _log_filter_step(
        stage="final",
        before=before,
        after=x,
        reason="slope_abs_too_small",
        threshold={
            "MIN_SLOPE": MIN_SLOPE,
            "failopen": "strong_move",
            "min_price_change_pct": _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_PRICE_CHANGE_PCT", 0.5),
            "min_range_pct": _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_RANGE_PCT", 1.0),
            "min_volume": _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_VOLUME", 50000.0),
        },
        sample_cols=sample_cols,
    )
    x = _apply_climax_guards(x, stage="final", sample_cols=sample_cols)

    x = _apply_5sec_filter(x, sample_cols)
    before = x.copy(); x = x[_num_series(x, "_tonosama_score") >= MIN_RAW_SCORE]
    _log_filter_step(stage="score", before=before, after=x, reason="raw_score_low", threshold={"MIN_RAW_SCORE": MIN_RAW_SCORE}, sample_cols=sample_cols)
    if x.empty:
        logger.info("[TONOSAMA ENTRY] no scalping candidates after surge/5sec/actual-move/climax filters diag=%s elapsed=%.3fs", _LAST_FILTER_DIAG, time.perf_counter() - started)
        return pd.DataFrame()
    out = x.sort_values("_tonosama_score", ascending=False).head(MAX_CANDIDATES).reset_index(drop=True)
    logger.info("[TONOSAMA ENTRY] candidates ready rows=%s head=%s elapsed=%.3fs", len(out), _sample_rows(out, sample_cols, limit=12), time.perf_counter() - started)
    return out


def build_tonosama_entries() -> int:
    started = time.perf_counter()
    candidates = iter_tonosama_candidate_rows()
    if candidates.empty:
        logger.info("[TONOSAMA ENTRY] build done candidates=0 registered=0 elapsed=%.3fs", time.perf_counter() - started)
        return 0
    registered = 0
    ai_ng = 0
    duplicate = 0
    low_score = 0
    no_symbol = 0
    final_low_samples: list[dict[str, Any]] = []
    for _, row in candidates.iterrows():
        if registered >= MAX_PENDING_PER_LOOP:
            break
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            no_symbol += 1
            continue
        if has_tonosama_pending(symbol):
            duplicate += 1
            continue
        raw_score = safe_float(row.get("_tonosama_score"), 0.0)
        if raw_score <= 0:
            low_score += 1
            final_low_samples.append({"symbol": symbol, "reason": "raw_score_le_zero", "raw_score": raw_score})
            continue
        ai_ok, ai_prob, ai_reason = ai_check_tonosama_entry(row)
        if not ai_ok:
            ai_ng += 1
            logger.info("[TONOSAMA ENTRY AI NG] symbol=%s prob=%.3f reason=%s surge=%.2f price_chg=%.2f body=%.3f range=%.3f close_pos=%.1f upper_wick=%.1f lower_wick=%.1f vol=%.0f 5s=%.3f slope=%.6f", symbol, ai_prob, ai_reason, safe_float(row.get("_max_volume_surge_ratio"), 0.0), safe_float(row.get("_max_price_change_pct"), 0.0), safe_float(row.get("_body_change_pct"), 0.0), safe_float(row.get("_intrabar_range_pct"), 0.0), safe_float(row.get("_close_position_pct"), 50.0), safe_float(row.get("_upper_wick_pct"), 0.0), safe_float(row.get("_lower_wick_pct"), 0.0), safe_float(row.get("_latest_volume"), 0.0), safe_float(row.get("price_change_5s_pct"), 0.0), safe_float(row.get("_slope"), 0.0))
            continue
        final_score = calc_final_score_safe(row, raw_score=raw_score, ai_prob=ai_prob)
        if final_score < MIN_FINAL_SCORE:
            low_score += 1
            final_low_samples.append({"symbol": symbol, "reason": "final_score_low", "final_score": round(final_score, 4), "min_final_score": MIN_FINAL_SCORE, "raw_score": round(raw_score, 4), "ai_prob": round(ai_prob, 4)})
            continue
        entry = build_pending_entry(row, final_score=final_score, ai_prob=ai_prob, ai_reason=ai_reason)
        if add_tonosama_pending(entry):
            registered += 1
            logger.info("🔥 TONOSAMA PENDING %s score=%.2f price=%.1f vol=%.0f body=%.3f%% range=%.3f%% close_pos=%.1f%% upper_wick=%.1f%% lower_wick=%.1f%% surge=%.2fx price_chg=%.2f%% tf=%s 5s=%.3f%% slope=%.6f ai_prob=%.3f", symbol, final_score, safe_float(row.get("close"), 0.0), safe_float(row.get("_latest_volume"), 0.0), safe_float(row.get("_body_change_pct"), 0.0), safe_float(row.get("_intrabar_range_pct"), 0.0), safe_float(row.get("_close_position_pct"), 50.0), safe_float(row.get("_upper_wick_pct"), 0.0), safe_float(row.get("_lower_wick_pct"), 0.0), safe_float(row.get("_max_volume_surge_ratio"), 0.0), safe_float(row.get("_max_price_change_pct"), 0.0), str(row.get("_surge_tf", "")), safe_float(row.get("price_change_5s_pct"), 0.0), safe_float(row.get("_slope"), 0.0), ai_prob)
            notify_discord_tonosama_pending(entry)
    logger.info("[TONOSAMA ENTRY] build done candidates=%s registered=%s duplicate=%s ai_ng=%s low_score=%s no_symbol=%s low_score_samples=%s elapsed=%.3fs", len(candidates), registered, duplicate, ai_ng, low_score, no_symbol, final_low_samples[:15], time.perf_counter() - started)
    return registered


def tonosama_loop() -> int:
    global _last_loop_at
    _last_loop_at = dt.datetime.now(); started = time.perf_counter()
    try:
        logger.info("[TONOSAMA LOOP] start at=%s", _last_loop_at.strftime("%Y-%m-%d %H:%M:%S"))
        if not is_market_time():
            logger.info("[TONOSAMA ENTRY] market closed skip")
            return 0
        pruned = prune_expired_tonosama_pending(reason="TONOSAMA_LOOP_START_EXPIRED")
        if pruned:
            logger.warning("[TONOSAMA LOOP] expired pending pruned at start removed=%s", pruned)
        if callable(update_active_symbols):
            try:
                update_active_symbols()
            except Exception:
                logger.warning("[TONOSAMA ENTRY] update_active_symbols skipped/failed", exc_info=True)
        registered = build_tonosama_entries()
        logger.info("[TONOSAMA LOOP] done registered=%s elapsed=%.3fs", registered, time.perf_counter() - started)
        return registered
    except Exception:
        logger.exception("[TONOSAMA ENTRY] tonosama_loop failed")
        return 0


__all__ = ["tonosama_loop", "build_tonosama_entries", "iter_tonosama_candidate_rows", "build_feature_df_with_5sec"]
