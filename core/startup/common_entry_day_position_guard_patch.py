# ============================================================
# File   : core/startup/common_entry_day_position_guard_patch.py
# Version: Ver1.0-COMMON-DAY-POSITION-GUARD
# ------------------------------------------------------------
# 目的:
#   TONOSAMA / RANKING / SUMMARY_AI など pending_manager.add_pending を通る
#   全エントリーに、当日レンジ位置ガードを共通適用する。
#
#   SELL: 当日安値圏で、始値比/VWAP乖離/短期価格変化が下方向に伸び切った候補を拒否。
#   BUY : 当日高値圏で、始値比/VWAP乖離/短期価格変化が上方向に伸び切った候補を拒否。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "Ver1.0-COMMON-DAY-POSITION-GUARD"
_INSTALLED = False
_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _env_bool(name: str, default: bool) -> bool:
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


def _env_csv(name: str, default: str) -> set[str]:
    try:
        raw = os.getenv(name, default)
        return {str(x).strip().upper() for x in str(raw).split(",") if str(x).strip()}
    except Exception:
        return {"TONOSAMA", "RANKING", "SUMMARY", "SUMMARY_AI", "AI", "PUSH"}


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        if pd.isna(v):
            return float(default)
        s = str(v).strip().replace(",", "")
        if s == "":
            return float(default)
        return float(s)
    except Exception:
        return float(default)


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    try:
        for n in names:
            if n in df.columns:
                return n
    except Exception:
        pass
    return None


def _entry_side(entry: dict[str, Any], pm: Any = None) -> str:
    try:
        if pm is not None:
            fn = getattr(pm, "_entry_side", None)
            if callable(fn):
                side = str(fn(entry) or "").upper()
                if side in {"BUY", "SELL"}:
                    return side
    except Exception:
        pass
    for k in ("side", "entry_decision", "ai_side"):
        s = str(entry.get(k) or "").strip().upper()
        if s in {"BUY", "LONG", "2", "買", "買い"}:
            return "BUY"
        if s in {"SELL", "SHORT", "1", "売", "売り"}:
            return "SELL"
    try:
        if _safe_float(entry.get("score_sell"), 0.0) > _safe_float(entry.get("score_buy"), 0.0):
            return "SELL"
    except Exception:
        pass
    return "BUY"


def _entry_price(entry: dict[str, Any]) -> float:
    for k in ("price", "close", "current_price", "entry_price"):
        v = _safe_float(entry.get(k), 0.0)
        if v > 0:
            return v
    cond = entry.get("entry_conditions") or {}
    if isinstance(cond, dict):
        for k in ("price", "close", "current_price", "latest_5sec_close"):
            v = _safe_float(cond.get(k), 0.0)
            if v > 0:
                return v
    return 0.0


def _load_symbol_1m_today(symbol: str) -> pd.DataFrame:
    try:
        from trading.entry.tonosama.summary_loader import load_merged_summary, normalize_summary_base

        raw = normalize_summary_base(load_merged_summary(1), interval=1)
        if raw is None or raw.empty or "symbol" not in raw.columns:
            return pd.DataFrame()
        x = raw.copy()
        x["symbol"] = x["symbol"].map(_norm_symbol)
        x = x[x["symbol"] == symbol].copy()
        if x.empty:
            return pd.DataFrame()
        if "datetime" in x.columns:
            x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.dropna(subset=["datetime"]).sort_values("datetime")
            if not x.empty:
                today = pd.Timestamp(dt.datetime.now().date())
                xt = x[x["datetime"] >= today].copy()
                if not xt.empty:
                    x = xt
                else:
                    latest_day = x["datetime"].max().date()
                    x = x[x["datetime"].dt.date == latest_day].copy()
        return x
    except Exception:
        logger.debug("[COMMON DAY POSITION GUARD] load 1m failed symbol=%s", symbol, exc_info=True)
        return pd.DataFrame()


def _calc_day_metrics(symbol: str, price: float) -> dict[str, Any] | None:
    symbol = _norm_symbol(symbol)
    if not symbol:
        return None
    ttl = max(0.5, _env_float("COMMON_ENTRY_DAY_POSITION_CACHE_SEC", 3.0))
    now = time.time()
    cached = _CACHE.get(symbol)
    if cached and now - cached[0] <= ttl:
        return cached[1]

    x = _load_symbol_1m_today(symbol)
    if x.empty:
        _CACHE[symbol] = (now, None)
        return None

    close_col = _first_existing(x, ["close", "close_1m", "close_price", "current_price", "price"])
    open_col = _first_existing(x, ["open", "open_1m", "open_price"])
    high_col = _first_existing(x, ["high", "high_1m", "high_price"])
    low_col = _first_existing(x, ["low", "low_1m", "low_price"])
    vol_col = _first_existing(x, ["volume", "volume_1m", "latest_volume", "latest_1m_volume"])
    if close_col is None:
        _CACHE[symbol] = (now, None)
        return None

    close = pd.to_numeric(x[close_col], errors="coerce")
    open_s = pd.to_numeric(x[open_col], errors="coerce") if open_col else close
    high_s = pd.to_numeric(x[high_col], errors="coerce") if high_col else close
    low_s = pd.to_numeric(x[low_col], errors="coerce") if low_col else close
    vol = pd.to_numeric(x[vol_col], errors="coerce").fillna(0.0) if vol_col else pd.Series(0.0, index=x.index)

    px = float(price or 0.0)
    if px <= 0 and close.notna().any():
        px = float(close.dropna().iloc[-1])
    valid_close = close.dropna()
    if px <= 0 or valid_close.empty:
        _CACHE[symbol] = (now, None)
        return None

    day_high = max(float(pd.concat([high_s, close], axis=1).max(axis=1).max()), px)
    day_low = min(float(pd.concat([low_s, close], axis=1).min(axis=1).min()), px)
    day_open_candidates = open_s.dropna()
    day_open = float(day_open_candidates.iloc[0]) if not day_open_candidates.empty else float(valid_close.iloc[0])
    rng = day_high - day_low
    day_position = ((px - day_low) / rng * 100.0) if rng > 0 else 50.0
    from_open = ((px - day_open) / day_open * 100.0) if day_open > 0 else 0.0
    vwap = 0.0
    try:
        mask = (vol > 0) & close.notna()
        if bool(mask.any()):
            vwap = float((close[mask] * vol[mask]).sum() / vol[mask].sum())
    except Exception:
        vwap = 0.0
    vwap_gap = ((px - vwap) / vwap * 100.0) if vwap > 0 else 0.0
    latest_dt = str(x["datetime"].max()) if "datetime" in x.columns and not x.empty else ""

    metrics = {
        "symbol": symbol,
        "price": round(px, 6),
        "day_open": round(day_open, 6),
        "day_high": round(day_high, 6),
        "day_low": round(day_low, 6),
        "day_vwap": round(vwap, 6),
        "day_position_pct": round(float(max(0.0, min(100.0, day_position))), 3),
        "from_open_pct": round(float(from_open), 3),
        "vwap_gap_pct": round(float(vwap_gap), 3),
        "latest_1m_dt": latest_dt,
        "rows_1m_today": int(len(x)),
    }
    _CACHE[symbol] = (now, metrics)
    return metrics


def _source_enabled(entry: dict[str, Any]) -> bool:
    sources = _env_csv("COMMON_ENTRY_DAY_POSITION_SOURCES", "TONOSAMA,RANKING,SUMMARY,SUMMARY_AI,AI,PUSH")
    src = str(entry.get("source") or entry.get("entry_type") or "").strip().upper()
    typ = str(entry.get("entry_type") or "").strip().upper()
    if not src and not typ:
        return True
    return src in sources or typ in sources or any(x in src for x in sources if x)


def _extract_price_chg(entry: dict[str, Any]) -> float:
    cond = entry.get("entry_conditions") or {}
    if isinstance(cond, dict):
        for k in ("max_price_change_pct", "price_change_pct_3m", "price_change_pct_5m", "price_change_5s_pct"):
            if k in cond:
                return _safe_float(cond.get(k), 0.0)
    for k in ("_max_price_change_pct", "price_change_pct", "price_change", "change_pct"):
        if k in entry:
            return _safe_float(entry.get(k), 0.0)
    return 0.0


def _reject_reason(entry: dict[str, Any], metrics: dict[str, Any], pm: Any = None) -> str | None:
    side = _entry_side(entry, pm)
    day_pos = _safe_float(metrics.get("day_position_pct"), 50.0)
    from_open = _safe_float(metrics.get("from_open_pct"), 0.0)
    vwap_gap = _safe_float(metrics.get("vwap_gap_pct"), 0.0)
    price_chg = _extract_price_chg(entry)
    low_zone = _env_float("COMMON_ENTRY_DAY_POSITION_LOW_ZONE_PCT", 20.0)
    high_zone = _env_float("COMMON_ENTRY_DAY_POSITION_HIGH_ZONE_PCT", 80.0)
    open_extreme = abs(_env_float("COMMON_ENTRY_DAY_POSITION_FROM_OPEN_EXTREME_PCT", 3.0))
    vwap_extreme = abs(_env_float("COMMON_ENTRY_DAY_POSITION_VWAP_EXTREME_PCT", 2.0))
    short_chg_extreme = abs(_env_float("COMMON_ENTRY_DAY_POSITION_SHORT_CHG_EXTREME_PCT", 0.50))
    strict_zone_only = _env_bool("COMMON_ENTRY_DAY_POSITION_STRICT_ZONE_ONLY", False)

    if side == "SELL" and day_pos <= low_zone:
        if strict_zone_only or from_open <= -open_extreme or vwap_gap <= -vwap_extreme or price_chg <= -short_chg_extreme:
            return "day_low_zone_selling_climax_guard"
    if side == "BUY" and day_pos >= high_zone:
        if strict_zone_only or from_open >= open_extreme or vwap_gap >= vwap_extreme or price_chg >= short_chg_extreme:
            return "day_high_zone_buying_climax_guard"
    return None


def _attach_metrics(entry: dict[str, Any], metrics: dict[str, Any], *, reject_reason: str | None = None) -> None:
    try:
        cond = entry.get("entry_conditions")
        if not isinstance(cond, dict):
            cond = {}
            entry["entry_conditions"] = cond
        cond.update({
            "day_position_pct": metrics.get("day_position_pct"),
            "from_open_pct": metrics.get("from_open_pct"),
            "vwap_gap_pct": metrics.get("vwap_gap_pct"),
            "day_open": metrics.get("day_open"),
            "day_high": metrics.get("day_high"),
            "day_low": metrics.get("day_low"),
            "day_vwap": metrics.get("day_vwap"),
            "day_position_latest_1m_dt": metrics.get("latest_1m_dt"),
            "day_position_rows_1m_today": metrics.get("rows_1m_today"),
        })
        if reject_reason:
            cond["day_position_reject_reason"] = reject_reason
        day_text = (
            f"当日位置 {float(metrics.get('day_position_pct', 50.0)):.1f}%"
            f" / 始値比 {float(metrics.get('from_open_pct', 0.0)):.2f}%"
            f" / VWAP乖離 {float(metrics.get('vwap_gap_pct', 0.0)):.2f}%"
        )
        reason = str(cond.get("reason") or "")
        if "当日位置" not in reason:
            cond["reason"] = f"{reason} / {day_text}" if reason else day_text
    except Exception:
        logger.debug("[COMMON DAY POSITION GUARD] attach metrics failed", exc_info=True)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("COMMON_ENTRY_DAY_POSITION_GUARD", True):
        logger.warning("[COMMON DAY POSITION GUARD] disabled by env")
        return False
    try:
        import trading.entry.pending_manager as pm

        orig_add = getattr(pm.add_pending, "_common_day_position_original", pm.add_pending)
        if getattr(pm.add_pending, "_common_day_position_guard_v1", False):
            _INSTALLED = True
            return True

        def _patched_add_pending(entry: dict) -> bool:
            try:
                if isinstance(entry, dict) and _source_enabled(entry):
                    symbol = _norm_symbol(entry.get("symbol"))
                    price = _entry_price(entry)
                    metrics = _calc_day_metrics(symbol, price)
                    if metrics:
                        reject = _reject_reason(entry, metrics, pm)
                        _attach_metrics(entry, metrics, reject_reason=reject)
                        if reject:
                            logger.warning(
                                "[COMMON DAY POSITION GUARD] reject symbol=%s source=%s type=%s side=%s reason=%s day_pos=%.1f from_open=%.2f vwap_gap=%.2f price=%.1f open=%.1f high=%.1f low=%.1f vwap=%.1f latest_1m=%s",
                                symbol,
                                entry.get("source"),
                                entry.get("entry_type"),
                                _entry_side(entry, pm),
                                reject,
                                _safe_float(metrics.get("day_position_pct"), 50.0),
                                _safe_float(metrics.get("from_open_pct"), 0.0),
                                _safe_float(metrics.get("vwap_gap_pct"), 0.0),
                                _safe_float(metrics.get("price"), 0.0),
                                _safe_float(metrics.get("day_open"), 0.0),
                                _safe_float(metrics.get("day_high"), 0.0),
                                _safe_float(metrics.get("day_low"), 0.0),
                                _safe_float(metrics.get("day_vwap"), 0.0),
                                metrics.get("latest_1m_dt"),
                            )
                            return False
            except Exception:
                logger.exception("[COMMON DAY POSITION GUARD] pre-add check failed; fail-open entry=%s", entry)
            return bool(orig_add(entry))

        _patched_add_pending._common_day_position_guard_v1 = True  # type: ignore[attr-defined]
        _patched_add_pending._common_day_position_original = orig_add  # type: ignore[attr-defined]
        pm.add_pending = _patched_add_pending
        _INSTALLED = True
        logger.warning(
            "[COMMON DAY POSITION GUARD] installed version=%s low_zone=%s high_zone=%s from_open_extreme=%s vwap_extreme=%s sources=%s",
            VERSION,
            os.getenv("COMMON_ENTRY_DAY_POSITION_LOW_ZONE_PCT", "20"),
            os.getenv("COMMON_ENTRY_DAY_POSITION_HIGH_ZONE_PCT", "80"),
            os.getenv("COMMON_ENTRY_DAY_POSITION_FROM_OPEN_EXTREME_PCT", "3"),
            os.getenv("COMMON_ENTRY_DAY_POSITION_VWAP_EXTREME_PCT", "2"),
            os.getenv("COMMON_ENTRY_DAY_POSITION_SOURCES", "TONOSAMA,RANKING,SUMMARY,SUMMARY_AI,AI,PUSH"),
        )
        return True
    except Exception:
        logger.exception("[COMMON DAY POSITION GUARD] install failed")
        return False


__all__ = ["install", "VERSION"]
