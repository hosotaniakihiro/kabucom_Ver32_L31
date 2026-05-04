# ============================================================
# File   : trading/entry/tonosama/runner.py
# Version: Ver1.0-TONOSAMA-ENTRY-RUNNER
# ============================================================
from __future__ import annotations
import datetime as dt, logging
import pandas as pd
try:
    from trading.ranking.active_symbol_manager import update_active_symbols
except Exception:
    update_active_symbols = None
from .config import MIN_PRICE, MIN_FINAL_SCORE, MIN_VOLUME_SURGE_RATIO, MIN_PRICE_CHANGE_PCT, MIN_5SEC_PRICE_CHANGE_PCT, MAX_5SEC_DROP_PCT, REQUIRE_5SEC_BAR, USE_5SEC_CONFIRM, MIN_RAW_SCORE, MAX_PENDING_PER_LOOP, MAX_CANDIDATES
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

def build_feature_df_with_5sec() -> pd.DataFrame:
    x = build_scalping_feature_df()
    if x.empty: return pd.DataFrame()
    features=[]
    for _, row in x.iterrows():
        sym=normalize_symbol(row.get("symbol")); f=build_5sec_features(sym); f["symbol"]=sym; features.append(f)
    if features:
        x=x.merge(pd.DataFrame(features), on="symbol", how="left")
    for c in ["price_change_5s_pct", "volume_surge_ratio_5s", "latest_5sec_close", "latest_5sec_volume"]:
        if c in x.columns: x[c]=pd.to_numeric(x[c], errors="coerce")
    return prepare_entry_scores(x)

def iter_tonosama_candidate_rows() -> pd.DataFrame:
    x=build_feature_df_with_5sec()
    if x.empty: return pd.DataFrame()
    x=x[x["close"] > MIN_PRICE]
    x=x[x["_max_volume_surge_ratio"].fillna(0.0) >= MIN_VOLUME_SURGE_RATIO]
    x=x[x["_max_price_change_pct"].fillna(0.0) >= MIN_PRICE_CHANGE_PCT]
    x=x[x["_slope"].fillna(0.0) >= -0.02]
    if USE_5SEC_CONFIRM and "has_5sec_bar" in x.columns:
        if REQUIRE_5SEC_BAR: x=x[x["has_5sec_bar"].fillna(False).astype(bool)]
        x=x[(~x["has_5sec_bar"].fillna(False).astype(bool)) | ((x["price_change_5s_pct"].fillna(0.0) >= MIN_5SEC_PRICE_CHANGE_PCT) & (x["price_change_5s_pct"].fillna(0.0) > MAX_5SEC_DROP_PCT))]
    x=x[x["_tonosama_score"].fillna(0.0) >= MIN_RAW_SCORE]
    if x.empty:
        logger.info("[TONOSAMA ENTRY] no scalping candidates after surge/5sec filters"); return pd.DataFrame()
    return x.sort_values("_tonosama_score", ascending=False).head(MAX_CANDIDATES).reset_index(drop=True)

def build_tonosama_entries() -> int:
    candidates=iter_tonosama_candidate_rows()
    if candidates.empty: return 0
    registered=0
    for _, row in candidates.iterrows():
        if registered >= MAX_PENDING_PER_LOOP: break
        symbol=normalize_symbol(row.get("symbol"))
        if not symbol or has_tonosama_pending(symbol): continue
        raw_score=safe_float(row.get("_tonosama_score"),0.0)
        if raw_score <= 0: continue
        ai_ok, ai_prob, ai_reason=ai_check_tonosama_entry(row)
        if not ai_ok:
            logger.info("[TONOSAMA ENTRY AI NG] symbol=%s prob=%.3f reason=%s surge=%.2f price_chg=%.2f 5s=%.3f", symbol, ai_prob, ai_reason, safe_float(row.get("_max_volume_surge_ratio"),0.0), safe_float(row.get("_max_price_change_pct"),0.0), safe_float(row.get("price_change_5s_pct"),0.0)); continue
        final_score=calc_final_score_safe(row, raw_score=raw_score, ai_prob=ai_prob)
        if final_score < MIN_FINAL_SCORE: continue
        entry=build_pending_entry(row, final_score=final_score, ai_prob=ai_prob, ai_reason=ai_reason)
        if add_tonosama_pending(entry):
            registered += 1
            logger.info("🔥 TONOSAMA PENDING %s score=%.2f price=%.1f surge=%.2fx price_chg=%.2f%% tf=%s 5s=%.3f%% ai_prob=%.3f", symbol, final_score, safe_float(row.get("close"),0.0), safe_float(row.get("_max_volume_surge_ratio"),0.0), safe_float(row.get("_max_price_change_pct"),0.0), str(row.get("_surge_tf", "")), safe_float(row.get("price_change_5s_pct"),0.0), ai_prob)
            notify_discord_tonosama_pending(entry)
    logger.info("[TONOSAMA ENTRY] build done candidates=%s registered=%s", len(candidates), registered)
    return registered

def tonosama_loop() -> int:
    global _last_loop_at
    _last_loop_at=dt.datetime.now()
    try:
        if not is_market_time():
            logger.info("[TONOSAMA ENTRY] market closed skip"); return 0
        if callable(update_active_symbols):
            try: update_active_symbols()
            except Exception: logger.warning("[TONOSAMA ENTRY] update_active_symbols skipped/failed", exc_info=True)
        return build_tonosama_entries()
    except Exception:
        logger.exception("[TONOSAMA ENTRY] tonosama_loop failed"); return 0
