# ============================================================
# File   : core/startup/ranking_entry_snapshot_technical_alias_patch.py
# Version: V1.0-RANKING-SNAPSHOT-TECH-ALIAS
# ------------------------------------------------------------
# Purpose:
#   ranking_technical_1min table is sometimes missing while ranking_snapshot_1min
#   already contains ma5_1m/ma25_1m/macd_1m/slope_1m/... columns.
#   Without aliases, pending rows get tech=0 and final entry guards reject as
#   ma_missing/no_momentum. This patch maps snapshot technical columns to the
#   unsuffixed names used by entry_controller guards.
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIG_ATTACH = None


_ALIAS_MAP = {
    "ma5": ("ma5_1m", "disp_ma5", "display_ma5"),
    "ma25": ("ma25_1m", "disp_ma25", "display_ma25"),
    "ma75": ("ma75_1m", "disp_ma75", "display_ma75"),
    "rsi": ("rsi_1m", "disp_rsi"),
    "macd": ("macd_1m", "disp_macd"),
    "signal": ("signal_1m", "macd_signal_1m", "disp_signal"),
    "macd_hist": ("macd_hist_1m", "hist_1m", "disp_macd_hist"),
    "atr": ("atr_1m", "disp_atr"),
    "slope": ("slope_1m", "ma5_slope_1m", "slope_pct_1m", "disp_slope"),
    "slope_atr_scaled": ("slope_atr_scaled_1m", "disp_slope_atr_scaled"),
    "vwap": ("vwap_1m", "disp_vwap"),
    "price_change_pct": ("price_change_pct_1m", "change_percentage", "change_rate"),
    "volume_ratio5": ("volume_ratio5_1m",),
}


def _is_blank(v: Any) -> bool:
    try:
        if v is None:
            return True
        s = str(v).strip()
        return s == "" or s.lower() in {"nan", "none", "nat", "<na>"}
    except Exception:
        return True


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if _is_blank(v):
            return default
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return default


def _copy_aliases(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row or {})
    copied: list[str] = []
    for dst, srcs in _ALIAS_MAP.items():
        cur = out.get(dst)
        if not _is_blank(cur) and _safe_float(cur, 0.0) != 0.0:
            continue
        for src in srcs:
            if src in out and not _is_blank(out.get(src)):
                out[dst] = out.get(src)
                copied.append(f"{src}->{dst}")
                break

    close = _safe_float(out.get("close") or out.get("close_price") or out.get("current_price") or out.get("price"), 0.0)
    ma5 = _safe_float(out.get("ma5"), 0.0)
    ma25 = _safe_float(out.get("ma25"), 0.0)
    slope = _safe_float(out.get("slope"), 0.0)
    macd = _safe_float(out.get("macd"), 0.0)
    signal = _safe_float(out.get("signal"), 0.0)
    atr = _safe_float(out.get("atr"), 0.0)
    rsi = _safe_float(out.get("rsi"), 50.0)

    ready = int(close > 0 and (ma5 > 0 or ma25 > 0 or abs(slope) > 0 or abs(macd) > 0 or atr > 0))
    if ready:
        out["ranking_tech_ready"] = 1
        out["ranking_tech_reason"] = "snapshot_alias"
        score = 0.0
        if close > 0 and ma5 > 0:
            score += min(20.0, abs(close - ma5) / close * 1000.0)
        if ma5 > 0 and ma25 > 0:
            score += min(20.0, abs(ma5 - ma25) / close * 1000.0 if close > 0 else 0.0)
        score += min(25.0, abs(slope) * 10000.0)
        score += min(15.0, abs(macd - signal) * 10.0)
        score += min(10.0, atr / close * 1000.0 if close > 0 else 0.0)
        if rsi != 50.0:
            score += min(10.0, abs(rsi - 50.0) / 5.0)
        out["ranking_tech_score"] = max(_safe_float(out.get("ranking_tech_score"), 0.0), round(score, 4))
        out["ranking_tech_source"] = "ranking_snapshot_alias"

    if copied:
        try:
            logger.warning(
                "[RANKING SNAPSHOT TECH ALIAS] symbol=%s copied=%s ready=%s tech_score=%s close=%s ma5=%s ma25=%s slope=%s macd=%s signal=%s",
                out.get("symbol"), copied[:12], out.get("ranking_tech_ready"), out.get("ranking_tech_score"),
                close, ma5, ma25, slope, macd, signal,
            )
        except Exception:
            pass
    return out


def _patched_attach(row: Dict[str, Any], tech_map: Dict[str, Dict[str, Any]] | None = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        base = _ORIG_ATTACH(row, tech_map, *args, **kwargs) if callable(_ORIG_ATTACH) else dict(row or {})
    except Exception:
        logger.warning("[RANKING SNAPSHOT TECH ALIAS] original attach failed; using row", exc_info=False)
        base = dict(row or {})
    try:
        # Keep original row columns too; some attach implementations may drop unknown snapshot columns.
        merged = dict(row or {})
        if isinstance(base, dict):
            merged.update(base)
        return _copy_aliases(merged)
    except Exception:
        logger.warning("[RANKING SNAPSHOT TECH ALIAS] alias copy failed", exc_info=False)
        return base if isinstance(base, dict) else dict(row or {})


def install() -> bool:
    global _PATCHED, _ORIG_ATTACH
    if _PATCHED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        cur = getattr(efr, "attach_ranking_technicals", None)
        if callable(cur) and not getattr(cur, "_ranking_snapshot_tech_alias_v1", False):
            _ORIG_ATTACH = cur
            _patched_attach._ranking_snapshot_tech_alias_v1 = True  # type: ignore[attr-defined]
            _patched_attach._original = cur  # type: ignore[attr-defined]
            efr.attach_ranking_technicals = _patched_attach
            try:
                import trading.ranking.ranking_technical_store as store
                store.attach_ranking_technicals = _patched_attach
            except Exception:
                pass
            logger.warning("[RANKING SNAPSHOT TECH ALIAS] installed")
        else:
            logger.warning("[RANKING SNAPSHOT TECH ALIAS] attach missing or already patched")
        _PATCHED = True
        return True
    except Exception:
        logger.exception("[RANKING SNAPSHOT TECH ALIAS] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING SNAPSHOT TECH ALIAS] auto install failed")


__all__ = ["install"]
