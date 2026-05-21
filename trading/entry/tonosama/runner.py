# ============================================================
# File   : trading/entry/tonosama/runner.py
# Version: Ver1.1-TONOSAMA-ENTRY-RUNNER-FAST-5SEC
# ------------------------------------------------------------
# ✔ 15秒ジョブが100秒以上詰まる原因を修正
# ✔ 5秒足特徴量取得を全銘柄ではなく一次フィルタ通過後の上位だけに限定
# ✔ previous_still_running 多発を防ぐため詳細ログと elapsed を追加
# ✔ 機能削除なし: 5秒足確認は維持しつつ取得対象を絞る
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import time

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


def _apply_primary_filters(x: pd.DataFrame) -> pd.DataFrame:
    """
    5秒足を取りに行く前の軽量フィルタ。

    ここで候補を絞らず全銘柄に build_5sec_features() を実行すると、
    15秒ジョブが100秒以上詰まり、schedule_loop 側で previous_still_running になる。
    """
    if x is None or x.empty:
        return pd.DataFrame()

    x = x.copy()

    if "close" in x.columns:
        x = x[pd.to_numeric(x["close"], errors="coerce").fillna(0.0) > MIN_PRICE]

    if "_max_volume_surge_ratio" in x.columns:
        x = x[pd.to_numeric(x["_max_volume_surge_ratio"], errors="coerce").fillna(0.0) >= MIN_VOLUME_SURGE_RATIO]

    if "_max_price_change_pct" in x.columns:
        x = x[pd.to_numeric(x["_max_price_change_pct"], errors="coerce").fillna(0.0) >= MIN_PRICE_CHANGE_PCT]

    if "_slope" in x.columns:
        x = x[pd.to_numeric(x["_slope"], errors="coerce").fillna(0.0) >= -0.02]

    return x


def build_feature_df_with_5sec() -> pd.DataFrame:
    started = time.perf_counter()

    x = build_scalping_feature_df()
    if x is None or x.empty:
        logger.info("[TONOSAMA ENTRY] base feature empty")
        return pd.DataFrame()

    base_rows = len(x)

    # 5秒足取得前に一次フィルタで絞る
    x = _apply_primary_filters(x)
    primary_rows = len(x)

    if x.empty:
        logger.info(
            "[TONOSAMA ENTRY] no candidates after primary filters base_rows=%s elapsed=%.3fs",
            base_rows,
            time.perf_counter() - started,
        )
        return pd.DataFrame()

    # スコア列がある場合は5秒足確認前に強い候補から処理する
    try:
        x = prepare_entry_scores(x)
        if "_tonosama_score" in x.columns:
            x = x.sort_values("_tonosama_score", ascending=False)
    except Exception:
        logger.warning("[TONOSAMA ENTRY] pre 5sec prepare_entry_scores failed", exc_info=True)

    max_5sec = int(MAX_5SEC_FEATURE_SYMBOLS or 0)
    if max_5sec <= 0:
        max_5sec = int(MAX_CANDIDATES or 80)

    # 5秒足特徴量取得は上位だけに限定
    x = x.head(min(max_5sec, MAX_CANDIDATES)).reset_index(drop=True)

    features = []
    for _, row in x.iterrows():
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        try:
            f = build_5sec_features(sym)
            if not isinstance(f, dict):
                f = {}
            f["symbol"] = sym
            features.append(f)
        except Exception:
            logger.warning("[TONOSAMA ENTRY] build_5sec_features failed symbol=%s", sym, exc_info=True)
            features.append({"symbol": sym})

    if features:
        x = x.merge(pd.DataFrame(features), on="symbol", how="left")

    for c in ["price_change_5s_pct", "volume_surge_ratio_5s", "latest_5sec_close", "latest_5sec_volume"]:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")

    x = prepare_entry_scores(x)

    logger.info(
        "[TONOSAMA ENTRY] feature build done base_rows=%s primary_rows=%s five_sec_rows=%s elapsed=%.3fs",
        base_rows,
        primary_rows,
        len(x),
        time.perf_counter() - started,
    )

    return x


def iter_tonosama_candidate_rows() -> pd.DataFrame:
    started = time.perf_counter()
    x = build_feature_df_with_5sec()
    if x.empty:
        return pd.DataFrame()

    # build_feature_df_with_5sec 側で一次フィルタ済みだが、互換性のため最終確認も残す
    x = x[x["close"] > MIN_PRICE]
    x = x[x["_max_volume_surge_ratio"].fillna(0.0) >= MIN_VOLUME_SURGE_RATIO]
    x = x[x["_max_price_change_pct"].fillna(0.0) >= MIN_PRICE_CHANGE_PCT]
    x = x[x["_slope"].fillna(0.0) >= -0.02]

    if USE_5SEC_CONFIRM and "has_5sec_bar" in x.columns:
        if REQUIRE_5SEC_BAR:
            x = x[x["has_5sec_bar"].fillna(False).astype(bool)]
        x = x[
            (~x["has_5sec_bar"].fillna(False).astype(bool))
            | (
                (x["price_change_5s_pct"].fillna(0.0) >= MIN_5SEC_PRICE_CHANGE_PCT)
                & (x["price_change_5s_pct"].fillna(0.0) > MAX_5SEC_DROP_PCT)
            )
        ]

    x = x[x["_tonosama_score"].fillna(0.0) >= MIN_RAW_SCORE]

    if x.empty:
        logger.info(
            "[TONOSAMA ENTRY] no scalping candidates after surge/5sec filters elapsed=%.3fs",
            time.perf_counter() - started,
        )
        return pd.DataFrame()

    out = x.sort_values("_tonosama_score", ascending=False).head(MAX_CANDIDATES).reset_index(drop=True)
    logger.info(
        "[TONOSAMA ENTRY] candidates ready rows=%s elapsed=%.3fs",
        len(out),
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

    for _, row in candidates.iterrows():
        if registered >= MAX_PENDING_PER_LOOP:
            break

        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue

        if has_tonosama_pending(symbol):
            duplicate += 1
            continue

        raw_score = safe_float(row.get("_tonosama_score"), 0.0)
        if raw_score <= 0:
            low_score += 1
            continue

        ai_ok, ai_prob, ai_reason = ai_check_tonosama_entry(row)
        if not ai_ok:
            ai_ng += 1
            logger.info(
                "[TONOSAMA ENTRY AI NG] symbol=%s prob=%.3f reason=%s surge=%.2f price_chg=%.2f 5s=%.3f",
                symbol,
                ai_prob,
                ai_reason,
                safe_float(row.get("_max_volume_surge_ratio"), 0.0),
                safe_float(row.get("_max_price_change_pct"), 0.0),
                safe_float(row.get("price_change_5s_pct"), 0.0),
            )
            continue

        final_score = calc_final_score_safe(row, raw_score=raw_score, ai_prob=ai_prob)
        if final_score < MIN_FINAL_SCORE:
            low_score += 1
            continue

        entry = build_pending_entry(row, final_score=final_score, ai_prob=ai_prob, ai_reason=ai_reason)
        if add_tonosama_pending(entry):
            registered += 1
            logger.info(
                "🔥 TONOSAMA PENDING %s score=%.2f price=%.1f surge=%.2fx price_chg=%.2f%% tf=%s 5s=%.3f%% ai_prob=%.3f",
                symbol,
                final_score,
                safe_float(row.get("close"), 0.0),
                safe_float(row.get("_max_volume_surge_ratio"), 0.0),
                safe_float(row.get("_max_price_change_pct"), 0.0),
                str(row.get("_surge_tf", "")),
                safe_float(row.get("price_change_5s_pct"), 0.0),
                ai_prob,
            )
            notify_discord_tonosama_pending(entry)

    logger.info(
        "[TONOSAMA ENTRY] build done candidates=%s registered=%s duplicate=%s ai_ng=%s low_score=%s elapsed=%.3fs",
        len(candidates),
        registered,
        duplicate,
        ai_ng,
        low_score,
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
