# ============================================================
# File   : core/startup/ranking_entry_runtime_fix_patch.py
# Version: V1.0-RANKING-ENTRY-TECH-FALLBACK-VOLUME-UNITS
# ------------------------------------------------------------
# 目的:
#   ranking_entry で以下の状態になる問題を抑止する。
#     - ranking_technical attached symbols=0
#     - 出来高ランキング値が 786.7 / 20633.9 のような表示単位のまま扱われ、
#       MIN_VOLUME=30000 に届かず VOLUME_NG になる
#
# 修正内容:
#   1. entry_from_ranking.save_ranking_pseudo_technicals をラップ
#      - 専用 ranking_technical_1min の計算が空でも、summary_history_cache から
#        同一銘柄の最新テクニカルを fallback attach する
#   2. entry_from_ranking._normalize_ranking_row_for_entry をラップ
#      - volume が閾値未満のとき、ランキング表示単位を考慮して x1000 補正
#      - turnover は補正済み volume と price から再計算し、必要なら表示単位も補正
#
# ENV:
#   RANKING_ENTRY_RUNTIME_FIX_ENABLED=1
#   RANKING_ENTRY_VOLUME_UNIT_MULTIPLIER=1000
#   RANKING_ENTRY_TURNOVER_UNIT_MULTIPLIER=1000000
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_NORMALIZE = None
_ORIGINAL_SAVE_TECH = None

_TECH_COLS = (
    "ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "hist",
    "atr", "slope", "slope_atr_scaled", "vwap", "score_buy", "score_sell",
    "score_total", "ranking_tech_score", "technical_ready", "symbol_hist_len",
)


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s.replace(",", "").replace("%", ""))
    except Exception:
        return default


def _nonzero(v: Any) -> bool:
    try:
        return abs(float(v)) > 1e-12
    except Exception:
        return False


def _current_thresholds() -> tuple[float, float]:
    try:
        from config.ranking_entry_config import RANKING_ENTRY_CONFIG
        vol_cfg = RANKING_ENTRY_CONFIG.get("VOLUME", {}) or {}
        min_volume = _safe_float(vol_cfg.get("MIN_VOLUME", 30000), 30000)
        min_turnover = _safe_float(vol_cfg.get("MIN_TURNOVER", 10000000), 10000000)
        return max(1.0, min_volume), max(1.0, min_turnover)
    except Exception:
        return 30000.0, 10000000.0


def _patched_normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = _ORIGINAL_NORMALIZE(row) if callable(_ORIGINAL_NORMALIZE) else dict(row or {})
    try:
        if not _env_bool("RANKING_ENTRY_NORMALIZE_VOLUME_UNITS", True):
            return out

        min_volume, min_turnover = _current_thresholds()
        volume_mul = _env_float("RANKING_ENTRY_VOLUME_UNIT_MULTIPLIER", 1000.0)
        turnover_mul = _env_float("RANKING_ENTRY_TURNOVER_UNIT_MULTIPLIER", 1000000.0)

        price = _safe_float(out.get("price") or out.get("current_price") or out.get("close_price"), 0.0)
        volume = _safe_float(out.get("volume"), 0.0)
        turnover = _safe_float(out.get("turnover") or out.get("trading_value"), 0.0)
        raw_volume = volume
        raw_turnover = turnover

        # kabuランキングの表示値は、小数を持つ「千株」系で来ることがある。
        # そのまま 30,000株閾値と比較すると 20,633.9 でも VOLUME_NG になるため、
        # 閾値未満かつ表示単位らしい値は x1000 に補正する。
        if volume > 0 and volume < min_volume and volume_mul > 1:
            volume = volume * volume_mul
            out["volume"] = volume
            out["trading_volume"] = volume
            out["ranking_volume_raw"] = raw_volume
            out["ranking_volume_unit_multiplier"] = volume_mul

        # 売買代金が表示単位の可能性がある場合も補正。ただし price*volume の方が
        # 大きければそちらを優先する。
        implied_turnover = price * volume if price > 0 and volume > 0 else 0.0
        if turnover > 0 and turnover < min_turnover and turnover_mul > 1:
            turnover = max(turnover * turnover_mul, implied_turnover)
            out["ranking_turnover_raw"] = raw_turnover
            out["ranking_turnover_unit_multiplier"] = turnover_mul
        elif implied_turnover > turnover:
            turnover = implied_turnover

        if turnover > 0:
            out["turnover"] = turnover
            out["trading_value"] = turnover

        if raw_volume != volume or raw_turnover != turnover:
            logger.info(
                "[RANKING ENTRY FIX] normalized units symbol=%s price=%s volume %s->%s turnover %s->%s min_volume=%s min_turnover=%s",
                out.get("symbol"), price, raw_volume, volume, raw_turnover, turnover, min_volume, min_turnover,
            )
    except Exception:
        logger.exception("[RANKING ENTRY FIX] normalize row wrapper failed")
    return out


def _get_gc_history(tf: int) -> pd.DataFrame:
    try:
        from core.global_context.context import global_context as GC
        try:
            df = GC.get_summary_history(tf, source="push")
        except TypeError:
            df = GC.get_summary_history(tf)
        if isinstance(df, pd.DataFrame):
            return df.copy()
    except Exception:
        pass
    return pd.DataFrame()


def _latest_summary_tech_map(symbols: set[str]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for tf in (1, 3, 5):
        df = _get_gc_history(tf)
        if df.empty or "symbol" not in df.columns:
            continue
        work = df.copy()
        work["symbol"] = work["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        work = work[work["symbol"].isin(symbols)].copy()
        if work.empty:
            continue
        if "datetime" in work.columns:
            work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
            work = work.sort_values(["symbol", "datetime"], kind="stable")
        latest = work.groupby("symbol", as_index=False).tail(1)
        for _, r in latest.iterrows():
            sym = str(r.get("symbol") or "").strip()
            if not sym or sym in result:
                # 1分足を最優先、無ければ3分/5分
                continue
            tech: Dict[str, Any] = {}
            for c in _TECH_COLS:
                if c in r.index:
                    tech[c] = r.get(c)
            if "hist" in tech and "macd_hist" not in tech:
                tech["macd_hist"] = tech.get("hist")
            if "ranking_tech_score" not in tech:
                tech["ranking_tech_score"] = _safe_float(tech.get("score_total"), 0.0)
            ready = any(_nonzero(tech.get(c)) for c in ("macd", "signal", "rsi", "slope", "slope_atr_scaled", "score_total"))
            tech["ranking_tech_ready"] = 1 if ready else 0
            tech["ranking_tech_reason"] = f"SUMMARY_HISTORY_FALLBACK_TF{tf}" if ready else f"SUMMARY_HISTORY_FALLBACK_WEAK_TF{tf}"
            tech["ranking_tech_datetime"] = r.get("datetime") if "datetime" in r.index else None
            tech["ranking_tech_db"] = "summary_history_cache"
            result[sym] = tech
    return result


def _patched_save_ranking_pseudo_technicals(rows: List[Dict[str, Any]], *args, **kwargs) -> Dict[str, Dict[str, Any]]:
    base: Dict[str, Dict[str, Any]] = {}
    try:
        if callable(_ORIGINAL_SAVE_TECH):
            base = _ORIGINAL_SAVE_TECH(rows, *args, **kwargs) or {}
    except Exception:
        logger.exception("[RANKING ENTRY FIX] original save_ranking_pseudo_technicals failed")
        base = {}

    try:
        symbols = {str(r.get("symbol") or "").strip() for r in rows or [] if str(r.get("symbol") or "").strip()}
        missing = {s for s in symbols if s not in base}
        fallback = _latest_summary_tech_map(missing)
        if fallback:
            merged = dict(base)
            merged.update(fallback)
            logger.warning(
                "[RANKING ENTRY FIX] technical fallback attached base=%s fallback=%s total=%s missing_symbols=%s",
                len(base), len(fallback), len(merged), len(missing),
            )
            return merged
        logger.warning(
            "[RANKING ENTRY FIX] technical fallback none base=%s symbols=%s missing=%s",
            len(base), len(symbols), len(missing),
        )
        return base
    except Exception:
        logger.exception("[RANKING ENTRY FIX] fallback tech map failed")
        return base


def install() -> bool:
    global _PATCHED, _ORIGINAL_NORMALIZE, _ORIGINAL_SAVE_TECH
    if _PATCHED:
        return True
    if not _env_bool("RANKING_ENTRY_RUNTIME_FIX_ENABLED", True):
        logger.warning("[RANKING ENTRY FIX] disabled by env")
        return False
    try:
        import trading.ranking.entry_from_ranking as efr

        norm = getattr(efr, "_normalize_ranking_row_for_entry", None)
        if callable(norm) and not getattr(norm, "_ranking_entry_fix_patch", False):
            _ORIGINAL_NORMALIZE = norm
            _patched_normalize_row._ranking_entry_fix_patch = True  # type: ignore[attr-defined]
            efr._normalize_ranking_row_for_entry = _patched_normalize_row
            logger.warning("[RANKING ENTRY FIX] patched _normalize_ranking_row_for_entry")

        save = getattr(efr, "save_ranking_pseudo_technicals", None)
        if callable(save) and not getattr(save, "_ranking_entry_fix_patch", False):
            _ORIGINAL_SAVE_TECH = save
            _patched_save_ranking_pseudo_technicals._ranking_entry_fix_patch = True  # type: ignore[attr-defined]
            efr.save_ranking_pseudo_technicals = _patched_save_ranking_pseudo_technicals
            logger.warning("[RANKING ENTRY FIX] patched save_ranking_pseudo_technicals")

        _PATCHED = True
        logger.warning("[RANKING ENTRY FIX] installed V1 tech-fallback volume-unit-normalizer")
        return True
    except Exception:
        logger.exception("[RANKING ENTRY FIX] install failed")
        return False


__all__ = ["install"]
