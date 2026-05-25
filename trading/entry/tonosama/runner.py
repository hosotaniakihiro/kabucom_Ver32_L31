# ============================================================
# File   : trading/entry/tonosama/runner.py
# Version: Ver1.3-TONOSAMA-ACTUAL-MOVEMENT-GUARD
# ------------------------------------------------------------
# ✔ 15秒ジョブが100秒以上詰まる原因を修正
# ✔ 5秒足特徴量取得を全銘柄ではなく一次フィルタ通過後の上位だけに限定
# ✔ previous_still_running 多発を防ぐため詳細ログと elapsed を追加
# ✔ 一次フィルタ / 5秒足フィルタ / raw_score フィルタの落選理由を集計ログ化
# ✔ 機能削除なし: 5秒足確認は維持しつつ取得対象を絞る
# ✔ Ver1.3: 全然動いていない銘柄のアラートを抑止
#    - 直近1分出来高
#    - 1分足 open→close 実体変化率
#    - 1分足 high-low 値幅率
#   を一次/最終フィルタに追加
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
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
)
from .time_guard import is_market_time
from .volume_surge import build_scalping_feature_df
from .five_sec_features import build_5sec_features
from .scoring import prepare_entry_scores, calc_final_score_safe
from .ai_gate import ai_check_tonosama_entry
from .pending_writer import has_tonosama_pending, build_pending_entry, add_tonosama_pending
from .notifier import notify_discord_tonosama_pending
from .utils import normalize_symbol, safe_float

logger = logging.getLogger(__name__)
_last_loop_at: dt.datetime | None = None
_LAST_FILTER_DIAG: dict[str, Any] = {}


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
    """殿様アラート用に、実際に動いているかを示す列を追加する。"""
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()

    close_col = _first_existing(x, ["close", "close_price", "current_price", "price", "close_1m"])
    open_col = _first_existing(x, ["open", "open_price", "open_1m"])
    high_col = _first_existing(x, ["high", "high_price", "high_1m"])
    low_col = _first_existing(x, ["low", "low_price", "low_1m"])
    volume_col = _first_existing(x, ["volume", "volume_1m", "latest_volume", "latest_1m_volume"])

    if close_col:
        x["_tonosama_close_for_move"] = pd.to_numeric(x[close_col], errors="coerce")
    else:
        x["_tonosama_close_for_move"] = 0.0

    if open_col:
        open_s = pd.to_numeric(x[open_col], errors="coerce")
        close_s = pd.to_numeric(x["_tonosama_close_for_move"], errors="coerce")
        x["_body_change_pct"] = ((close_s - open_s).abs() / open_s.replace(0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
    else:
        # open が無い場合は 3m/5m price_change を代用。無ければ0にして動いていない扱い。
        x["_body_change_pct"] = _num_series(x, "_max_price_change_pct", 0.0).abs()

    if high_col and low_col:
        high_s = pd.to_numeric(x[high_col], errors="coerce")
        low_s = pd.to_numeric(x[low_col], errors="coerce")
        close_s = pd.to_numeric(x["_tonosama_close_for_move"], errors="coerce")
        denom = close_s.where(close_s > 0, pd.NA)
        x["_intrabar_range_pct"] = ((high_s - low_s).abs() / denom * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
    else:
        x["_intrabar_range_pct"] = 0.0

    if volume_col:
        x["_latest_volume"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0)
    else:
        x["_latest_volume"] = 0.0

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


def _log_filter_step(
    *,
    stage: str,
    before: pd.DataFrame,
    after: pd.DataFrame,
    reason: str,
    threshold: Any,
    sample_cols: list[str],
) -> None:
    try:
        before_rows = 0 if before is None else len(before)
        after_rows = 0 if after is None else len(after)
        dropped = max(0, before_rows - after_rows)
        if dropped <= 0:
            logger.info(
                "[TONOSAMA FILTER PASS] stage=%s reason=%s before=%s after=%s threshold=%s",
                stage,
                reason,
                before_rows,
                after_rows,
                threshold,
            )
            return

        if before is None or before.empty:
            dropped_df = pd.DataFrame()
        else:
            after_index = set(after.index) if after is not None and not after.empty else set()
            dropped_df = before.loc[[idx for idx in before.index if idx not in after_index]].copy()

        logger.warning(
            "[TONOSAMA FILTER DROP] stage=%s reason=%s before=%s after=%s dropped=%s threshold=%s sample=%s",
            stage,
            reason,
            before_rows,
            after_rows,
            dropped,
            threshold,
            _sample_rows(dropped_df, sample_cols, limit=10),
        )
    except Exception:
        logger.debug("[TONOSAMA FILTER] step log failed stage=%s reason=%s", stage, reason, exc_info=True)


def _diagnose_base_frame(x: pd.DataFrame) -> None:
    try:
        if x is None or x.empty:
            logger.warning("[TONOSAMA FILTER DIAG] base empty")
            return
        cols = list(x.columns)
        symbol_count = int(x["symbol"].astype(str).nunique()) if "symbol" in x.columns else 0
        summary = {
            "rows": len(x),
            "symbols": symbol_count,
            "cols": cols[:80],
            "min_price": MIN_PRICE,
            "min_volume_surge": MIN_VOLUME_SURGE_RATIO,
            "min_price_change_pct": MIN_PRICE_CHANGE_PCT,
            "min_body_change_pct": MIN_BODY_CHANGE_PCT,
            "min_intrabar_range_pct": MIN_INTRABAR_RANGE_PCT,
            "min_latest_volume": MIN_LATEST_VOLUME,
            "min_raw_score": MIN_RAW_SCORE,
            "use_5sec_confirm": USE_5SEC_CONFIRM,
            "require_5sec_bar": REQUIRE_5SEC_BAR,
            "min_5sec_price_change_pct": MIN_5SEC_PRICE_CHANGE_PCT,
            "max_5sec_drop_pct": MAX_5SEC_DROP_PCT,
        }
        metrics = {}
        for col in ["close", "_max_volume_surge_ratio", "_max_price_change_pct", "_body_change_pct", "_intrabar_range_pct", "_latest_volume", "_slope", "_tonosama_score"]:
            if col in x.columns:
                s = pd.to_numeric(x[col], errors="coerce")
                metrics[col] = {
                    "nonnull": int(s.notna().sum()),
                    "min": round(float(s.min()), 6) if s.notna().any() else None,
                    "max": round(float(s.max()), 6) if s.notna().any() else None,
                    "mean": round(float(s.mean()), 6) if s.notna().any() else None,
                    "zero": int((s.fillna(0.0) == 0.0).sum()),
                }
        logger.warning("[TONOSAMA FILTER DIAG] base summary=%s metrics=%s head=%s", summary, metrics, _sample_rows(x, ["symbol", "symbolname", "close", "_max_volume_surge_ratio", "_max_price_change_pct", "_body_change_pct", "_intrabar_range_pct", "_latest_volume", "_slope", "score", "final_score", "score_mtf"], limit=15))
    except Exception:
        logger.debug("[TONOSAMA FILTER DIAG] base diagnose failed", exc_info=True)


def _apply_primary_filters(x: pd.DataFrame) -> pd.DataFrame:
    global _LAST_FILTER_DIAG
    _LAST_FILTER_DIAG = {}

    if x is None or x.empty:
        _LAST_FILTER_DIAG = {"stage": "primary", "base_rows": 0, "primary_rows": 0, "empty_reason": "base_empty"}
        return pd.DataFrame()

    x = _ensure_actual_movement_cols(x)
    base_rows = len(x)
    _diagnose_base_frame(x)

    sample_cols = ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_intrabar_range_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "score", "final_score", "score_mtf", "mtf"]

    if "close" in x.columns:
        before = x.copy()
        x = x[_num_series(x, "close") > MIN_PRICE]
        _log_filter_step(stage="primary", before=before, after=x, reason="close_below_min_price", threshold={"MIN_PRICE": MIN_PRICE}, sample_cols=sample_cols)
    else:
        logger.warning("[TONOSAMA FILTER WARN] stage=primary missing close column -> price filter skipped cols=%s", list(x.columns))

    before = x.copy()
    x = x[_num_series(x, "_latest_volume") >= MIN_LATEST_VOLUME]
    _log_filter_step(stage="primary", before=before, after=x, reason="latest_volume_low_flat_alert_guard", threshold={"MIN_LATEST_VOLUME": MIN_LATEST_VOLUME}, sample_cols=sample_cols)

    before = x.copy()
    x = x[_num_series(x, "_body_change_pct") >= MIN_BODY_CHANGE_PCT]
    _log_filter_step(stage="primary", before=before, after=x, reason="body_change_low_flat_alert_guard", threshold={"MIN_BODY_CHANGE_PCT": MIN_BODY_CHANGE_PCT}, sample_cols=sample_cols)

    before = x.copy()
    x = x[_num_series(x, "_intrabar_range_pct") >= MIN_INTRABAR_RANGE_PCT]
    _log_filter_step(stage="primary", before=before, after=x, reason="intrabar_range_low_flat_alert_guard", threshold={"MIN_INTRABAR_RANGE_PCT": MIN_INTRABAR_RANGE_PCT}, sample_cols=sample_cols)

    if "_max_volume_surge_ratio" in x.columns:
        before = x.copy()
        x = x[_num_series(x, "_max_volume_surge_ratio") >= MIN_VOLUME_SURGE_RATIO]
        _log_filter_step(stage="primary", before=before, after=x, reason="volume_surge_low", threshold={"MIN_VOLUME_SURGE_RATIO": MIN_VOLUME_SURGE_RATIO}, sample_cols=sample_cols)
    else:
        logger.warning("[TONOSAMA FILTER WARN] stage=primary missing _max_volume_surge_ratio -> volume surge filter skipped cols=%s", list(x.columns))

    if "_max_price_change_pct" in x.columns:
        before = x.copy()
        x = x[_num_series(x, "_max_price_change_pct") >= MIN_PRICE_CHANGE_PCT]
        _log_filter_step(stage="primary", before=before, after=x, reason="price_change_low", threshold={"MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT}, sample_cols=sample_cols)
    else:
        logger.warning("[TONOSAMA FILTER WARN] stage=primary missing _max_price_change_pct -> price change filter skipped cols=%s", list(x.columns))

    if "_slope" in x.columns:
        before = x.copy()
        x = x[_num_series(x, "_slope") >= -0.02]
        _log_filter_step(stage="primary", before=before, after=x, reason="slope_too_negative", threshold={"MIN_SLOPE": -0.02}, sample_cols=sample_cols)
    else:
        logger.warning("[TONOSAMA FILTER WARN] stage=primary missing _slope -> slope filter skipped cols=%s", list(x.columns))

    _LAST_FILTER_DIAG = {
        "stage": "primary",
        "base_rows": base_rows,
        "primary_rows": len(x),
        "thresholds": {
            "MIN_PRICE": MIN_PRICE,
            "MIN_LATEST_VOLUME": MIN_LATEST_VOLUME,
            "MIN_BODY_CHANGE_PCT": MIN_BODY_CHANGE_PCT,
            "MIN_INTRABAR_RANGE_PCT": MIN_INTRABAR_RANGE_PCT,
            "MIN_VOLUME_SURGE_RATIO": MIN_VOLUME_SURGE_RATIO,
            "MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT,
            "MIN_SLOPE": -0.02,
        },
        "survivors": _sample_rows(x, sample_cols, limit=12),
    }

    logger.warning(
        "[TONOSAMA FILTER SUMMARY] stage=primary base_rows=%s primary_rows=%s survivors=%s thresholds=%s",
        base_rows,
        len(x),
        _LAST_FILTER_DIAG.get("survivors"),
        _LAST_FILTER_DIAG.get("thresholds"),
    )

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
        logger.info(
            "[TONOSAMA ENTRY] no candidates after primary filters base_rows=%s primary_rows=%s diag=%s elapsed=%.3fs",
            base_rows,
            primary_rows,
            _LAST_FILTER_DIAG,
            time.perf_counter() - started,
        )
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

    before_head = _sample_rows(x, ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_intrabar_range_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score"], limit=12)

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

    logger.info(
        "[TONOSAMA ENTRY] feature build done base_rows=%s primary_rows=%s five_sec_rows=%s feature_missing=%s pre_5sec_head=%s post_5sec_head=%s elapsed=%.3fs",
        base_rows,
        primary_rows,
        len(x),
        feature_missing,
        before_head,
        _sample_rows(x, ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_intrabar_range_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score", "has_5sec_bar", "price_change_5s_pct", "volume_surge_ratio_5s"], limit=12),
        time.perf_counter() - started,
    )

    return x


def iter_tonosama_candidate_rows() -> pd.DataFrame:
    started = time.perf_counter()
    x = build_feature_df_with_5sec()
    if x.empty:
        return pd.DataFrame()

    x = _ensure_actual_movement_cols(x)
    sample_cols = ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_intrabar_range_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score", "has_5sec_bar", "price_change_5s_pct", "volume_surge_ratio_5s"]

    before = x.copy()
    x = x[_num_series(x, "close") > MIN_PRICE]
    _log_filter_step(stage="final", before=before, after=x, reason="close_below_min_price", threshold={"MIN_PRICE": MIN_PRICE}, sample_cols=sample_cols)

    before = x.copy()
    x = x[_num_series(x, "_latest_volume") >= MIN_LATEST_VOLUME]
    _log_filter_step(stage="final", before=before, after=x, reason="latest_volume_low_flat_alert_guard", threshold={"MIN_LATEST_VOLUME": MIN_LATEST_VOLUME}, sample_cols=sample_cols)

    before = x.copy()
    x = x[_num_series(x, "_body_change_pct") >= MIN_BODY_CHANGE_PCT]
    _log_filter_step(stage="final", before=before, after=x, reason="body_change_low_flat_alert_guard", threshold={"MIN_BODY_CHANGE_PCT": MIN_BODY_CHANGE_PCT}, sample_cols=sample_cols)

    before = x.copy()
    x = x[_num_series(x, "_intrabar_range_pct") >= MIN_INTRABAR_RANGE_PCT]
    _log_filter_step(stage="final", before=before, after=x, reason="intrabar_range_low_flat_alert_guard", threshold={"MIN_INTRABAR_RANGE_PCT": MIN_INTRABAR_RANGE_PCT}, sample_cols=sample_cols)

    before = x.copy()
    x = x[_num_series(x, "_max_volume_surge_ratio") >= MIN_VOLUME_SURGE_RATIO]
    _log_filter_step(stage="final", before=before, after=x, reason="volume_surge_low", threshold={"MIN_VOLUME_SURGE_RATIO": MIN_VOLUME_SURGE_RATIO}, sample_cols=sample_cols)

    before = x.copy()
    x = x[_num_series(x, "_max_price_change_pct") >= MIN_PRICE_CHANGE_PCT]
    _log_filter_step(stage="final", before=before, after=x, reason="price_change_low", threshold={"MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT}, sample_cols=sample_cols)

    before = x.copy()
    x = x[_num_series(x, "_slope") >= -0.02]
    _log_filter_step(stage="final", before=before, after=x, reason="slope_too_negative", threshold={"MIN_SLOPE": -0.02}, sample_cols=sample_cols)

    if USE_5SEC_CONFIRM and "has_5sec_bar" in x.columns:
        if REQUIRE_5SEC_BAR:
            before = x.copy()
            x = x[_bool_series(x, "has_5sec_bar")]
            _log_filter_step(stage="5sec", before=before, after=x, reason="missing_5sec_bar", threshold={"REQUIRE_5SEC_BAR": REQUIRE_5SEC_BAR}, sample_cols=sample_cols)

        before = x.copy()
        has_bar = _bool_series(x, "has_5sec_bar")
        chg_5s = _num_series(x, "price_change_5s_pct")
        x = x[(~has_bar) | ((chg_5s >= MIN_5SEC_PRICE_CHANGE_PCT) & (chg_5s > MAX_5SEC_DROP_PCT))]
        _log_filter_step(
            stage="5sec",
            before=before,
            after=x,
            reason="five_sec_price_change_ng",
            threshold={"MIN_5SEC_PRICE_CHANGE_PCT": MIN_5SEC_PRICE_CHANGE_PCT, "MAX_5SEC_DROP_PCT": MAX_5SEC_DROP_PCT, "REQUIRE_5SEC_BAR": REQUIRE_5SEC_BAR},
            sample_cols=sample_cols,
        )
    elif USE_5SEC_CONFIRM:
        logger.warning("[TONOSAMA FILTER WARN] stage=5sec USE_5SEC_CONFIRM=True but has_5sec_bar column missing cols=%s", list(x.columns))

    before = x.copy()
    x = x[_num_series(x, "_tonosama_score") >= MIN_RAW_SCORE]
    _log_filter_step(stage="score", before=before, after=x, reason="raw_score_low", threshold={"MIN_RAW_SCORE": MIN_RAW_SCORE}, sample_cols=sample_cols)

    if x.empty:
        logger.info(
            "[TONOSAMA ENTRY] no scalping candidates after surge/5sec/actual-move filters diag=%s elapsed=%.3fs",
            _LAST_FILTER_DIAG,
            time.perf_counter() - started,
        )
        return pd.DataFrame()

    out = x.sort_values("_tonosama_score", ascending=False).head(MAX_CANDIDATES).reset_index(drop=True)
    logger.info(
        "[TONOSAMA ENTRY] candidates ready rows=%s head=%s elapsed=%.3fs",
        len(out),
        _sample_rows(out, sample_cols, limit=12),
        time.perf_counter() - started,
    )
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
            logger.info(
                "[TONOSAMA ENTRY AI NG] symbol=%s prob=%.3f reason=%s surge=%.2f price_chg=%.2f body=%.3f range=%.3f vol=%.0f 5s=%.3f",
                symbol,
                ai_prob,
                ai_reason,
                safe_float(row.get("_max_volume_surge_ratio"), 0.0),
                safe_float(row.get("_max_price_change_pct"), 0.0),
                safe_float(row.get("_body_change_pct"), 0.0),
                safe_float(row.get("_intrabar_range_pct"), 0.0),
                safe_float(row.get("_latest_volume"), 0.0),
                safe_float(row.get("price_change_5s_pct"), 0.0),
            )
            continue

        final_score = calc_final_score_safe(row, raw_score=raw_score, ai_prob=ai_prob)
        if final_score < MIN_FINAL_SCORE:
            low_score += 1
            final_low_samples.append({"symbol": symbol, "reason": "final_score_low", "final_score": round(final_score, 4), "min_final_score": MIN_FINAL_SCORE, "raw_score": round(raw_score, 4), "ai_prob": round(ai_prob, 4)})
            continue

        entry = build_pending_entry(row, final_score=final_score, ai_prob=ai_prob, ai_reason=ai_reason)
        if add_tonosama_pending(entry):
            registered += 1
            logger.info(
                "🔥 TONOSAMA PENDING %s score=%.2f price=%.1f vol=%.0f body=%.3f%% range=%.3f%% surge=%.2fx price_chg=%.2f%% tf=%s 5s=%.3f%% ai_prob=%.3f",
                symbol,
                final_score,
                safe_float(row.get("close"), 0.0),
                safe_float(row.get("_latest_volume"), 0.0),
                safe_float(row.get("_body_change_pct"), 0.0),
                safe_float(row.get("_intrabar_range_pct"), 0.0),
                safe_float(row.get("_max_volume_surge_ratio"), 0.0),
                safe_float(row.get("_max_price_change_pct"), 0.0),
                str(row.get("_surge_tf", "")),
                safe_float(row.get("price_change_5s_pct"), 0.0),
                ai_prob,
            )
            notify_discord_tonosama_pending(entry)

    logger.info(
        "[TONOSAMA ENTRY] build done candidates=%s registered=%s duplicate=%s ai_ng=%s low_score=%s no_symbol=%s low_score_samples=%s elapsed=%.3fs",
        len(candidates),
        registered,
        duplicate,
        ai_ng,
        low_score,
        no_symbol,
        final_low_samples[:15],
        time.perf_counter() - started,
    )
    return registered


def tonosama_loop() -> int:
    global _last_loop_at
    _last_loop_at = dt.datetime.now()
    started = time.perf_counter()

    try:
        logger.info("[TONOSAMA LOOP] start at=%s", _last_loop_at.strftime("%Y-%m-%d %H:%M:%S"))

        if not is_market_time():
            logger.info("[TONOSAMA ENTRY] market closed skip")
            return 0

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
