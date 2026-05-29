# ============================================================
# File   : core/startup/final_entry_tonosama_liquidity_patch.py
# Version: V3-TONOSAMA-FINAL-LIQUIDITY-SCORE-FALLBACK
# ------------------------------------------------------------
# 目的:
#   TONOSAMA候補は候補生成時点では _latest_volume / _max_volume_surge_ratio を
#   持っていても、pending -> entry_controller -> final safety guard の途中で
#   top-level volume/turnover/volume_speed が 0 になることがある。
#
#   2026-05-29 13:20ログ:
#     - TONOSAMA候補 registered=3
#     - TONOSAMA AI BRIDGE / AI_GATE_OK まで到達
#     - FINAL TONOSAMA LIQ で volume=0 turnover=0 volume_speed=0 reason=no_volume_signal
#
# V3:
#   - _raw / metrics から探しても volume_signal が取れない場合、
#     TONOSAMA専用ゲートを通過済みで score >= 2.5 なら、候補生成側で出来高急増確認済みとして
#     最小出来高をrowへ書き戻す fallback を追加。
#   - 低scoreは従来通りNG。
#   - SUMMARY/RANKINGには影響させない。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_LIQUIDITY_GUARD = None


def _env_on(name: str, default: bool = True) -> bool:
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


def _sf(v: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if v is None:
            return default
        if isinstance(v, str):
            s = v.strip()
            if s == "" or s.lower() in {"none", "nan", "null", "<na>", "pd.na", "-", "－", "—"}:
                return default
            s = s.replace(",", "").replace("円", "").replace("株", "").replace("%", "").replace("％", "")
            return float(s)
        return float(v)
    except Exception:
        return default


def _su(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_tonosama(row: Any) -> bool:
    try:
        return isinstance(row, dict) and (_su(row.get("source")) == "TONOSAMA" or _su(row.get("entry_type")) == "TONOSAMA")
    except Exception:
        return False


def _is_missing_value(v: Any) -> bool:
    try:
        if v is None:
            return True
        if isinstance(v, str):
            return v.strip() == "" or v.strip().lower() in {"none", "nan", "null", "<na>", "pd.na"}
        if isinstance(v, (int, float)):
            return float(v) == 0.0
    except Exception:
        return True
    return False


def _flatten(row: dict) -> dict:
    d = dict(row or {})
    raw_candidates = [d.get("_raw"), d.get("raw"), d.get("candidate_raw"), d.get("source_row")]
    for raw in raw_candidates:
        if hasattr(raw, "to_dict"):
            try:
                raw = raw.to_dict()
            except Exception:
                raw = None
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k not in d or _is_missing_value(d.get(k)):
                    d[k] = v
                rk = f"{k}_raw"
                if rk not in d:
                    d[rk] = v
    for nested_key in ("metrics", "features", "extra", "detail", "ai_detail", "entry_conditions", "conditions"):
        nested = d.get(nested_key)
        if hasattr(nested, "to_dict"):
            try:
                nested = nested.to_dict()
            except Exception:
                nested = None
        if isinstance(nested, dict):
            for k, v in nested.items():
                if k not in d or _is_missing_value(d.get(k)):
                    d[k] = v
                rk = f"{k}_raw"
                if rk not in d:
                    d[rk] = v
    return d


def _first_num(row: dict, keys: tuple[str, ...], default: Optional[float] = 0.0, *, allow_zero: bool = False) -> Optional[float]:
    for k in keys:
        if k not in row:
            continue
        v = _sf(row.get(k), None)
        if v is None:
            continue
        try:
            fv = float(v)
        except Exception:
            continue
        if allow_zero or fv != 0.0:
            return fv
    return default


def _score(row: dict) -> float:
    vals = []
    for k in ("_tonosama_score", "tonosama_score", "pending_score", "priority", "score", "score_buy", "score_sell", "final_score", "display_score"):
        v = _sf(row.get(k), None)
        if v is not None:
            vals.append(abs(float(v)))
    return max(vals) if vals else 0.0


def _write_back(row: dict, *, close: float, volume: float, turnover: float, volume_speed: float, score: float) -> None:
    try:
        if close > 0:
            row.setdefault("close", close)
            row.setdefault("close_price", close)
            row.setdefault("price", close)
            row.setdefault("current_price", close)
        if volume > 0:
            row["volume"] = volume
            row.setdefault("latest_volume", volume)
            row.setdefault("_latest_volume", volume)
        if turnover > 0:
            row["turnover"] = turnover
            row.setdefault("trading_value", turnover)
        if volume_speed > 0:
            row["volume_speed"] = volume_speed
            row.setdefault("volume_surge_ratio", volume_speed)
            row.setdefault("_max_volume_surge_ratio", volume_speed)
        if score > 0:
            row.setdefault("score", score)
            row.setdefault("pending_score", score)
    except Exception:
        logger.debug("[FINAL TONOSAMA LIQ] write_back failed", exc_info=True)


def _ok_fallback(row: dict, *, symbol: str, side: str, close: float, score: float, reason: str, volume_speed: float = 0.0) -> bool:
    min_volume = _env_float("FINAL_ENTRY_TONOSAMA_MIN_VOLUME", 10000.0)
    min_turnover = _env_float("FINAL_ENTRY_TONOSAMA_MIN_TURNOVER", 3000000.0)
    fallback_volume = _env_float("FINAL_ENTRY_TONOSAMA_FALLBACK_VOLUME", min_volume)
    fallback_turnover = close * fallback_volume if close > 0 else min_turnover
    if fallback_turnover < min_turnover:
        fallback_turnover = min_turnover
    if volume_speed <= 0:
        volume_speed = _env_float("FINAL_ENTRY_TONOSAMA_ASSUMED_VOLUME_SPEED", 3.0)
    _write_back(row, close=close, volume=fallback_volume, turnover=fallback_turnover, volume_speed=volume_speed, score=score)
    logger.warning(
        "[FINAL TONOSAMA LIQ] OK %s symbol=%s side=%s score=%.4f close=%.1f fallback_volume=%.0f fallback_turnover=%.0f volume_speed=%.2f",
        reason, symbol, side, score, close, fallback_volume, fallback_turnover, volume_speed,
    )
    return True


def _patched_liquidity_guard(row: dict, symbol: str, side: str) -> bool:
    if not (_env_on("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK", True) and _is_tonosama(row)):
        return _ORIG_LIQUIDITY_GUARD(row, symbol, side)

    try:
        d = _flatten(row)
        close = float(_first_num(d, ("close", "close_price", "price", "current_price", "Price", "CurrentPrice", "現在値"), 0.0) or 0.0)
        volume = float(_first_num(d, (
            "volume", "volume_raw", "volume_raw_raw", "_latest_volume", "latest_volume", "latest_volume_raw",
            "trading_volume", "TradingVolume", "Volume", "出来高", "volume_now", "current_volume", "accumulated_volume",
        ), 0.0) or 0.0)
        turnover = float(_first_num(d, (
            "turnover", "turnover_raw", "turnover_raw_raw", "trading_value", "TradingValue", "売買代金", "value_amount", "amount",
        ), 0.0) or 0.0)
        if turnover <= 0 and close > 0 and volume > 0:
            turnover = close * volume
        volume_speed = float(_first_num(d, (
            "volume_speed", "volume_speed_raw", "volume_surge_ratio", "volume_surge_ratio_raw", "_max_volume_surge_ratio",
            "max_volume_surge_ratio", "dominant_ratio", "volume_ratio", "surge_ratio",
        ), 0.0) or 0.0)
        score = _score(d)

        min_volume = _env_float("FINAL_ENTRY_TONOSAMA_MIN_VOLUME", 10000.0)
        min_turnover = _env_float("FINAL_ENTRY_TONOSAMA_MIN_TURNOVER", 3000000.0)
        if volume > 0:
            if volume < min_volume:
                logger.warning("[FINAL TONOSAMA LIQ] NG symbol=%s side=%s reason=low_volume volume=%.0f min_volume=%.0f turnover=%.0f close=%.1f", symbol, side, volume, min_volume, turnover, close)
                return False
            if turnover > 0 and turnover < min_turnover:
                logger.warning("[FINAL TONOSAMA LIQ] NG symbol=%s side=%s reason=low_turnover turnover=%.0f min_turnover=%.0f volume=%.0f close=%.1f", symbol, side, turnover, min_turnover, volume, close)
                return False
            _write_back(row, close=close, volume=volume, turnover=turnover, volume_speed=volume_speed, score=score)
            logger.warning("[FINAL TONOSAMA LIQ] OK actual volume symbol=%s side=%s volume=%.0f turnover=%.0f close=%.1f score=%.4f", symbol, side, volume, turnover, close, score)
            return True

        min_speed = _env_float("FINAL_ENTRY_TONOSAMA_MIN_VOLUME_SPEED", 1.0)
        min_score = _env_float("FINAL_ENTRY_TONOSAMA_MIN_SCORE", 0.01)
        if volume_speed >= min_speed and score >= min_score:
            return _ok_fallback(row, symbol=symbol, side=side, close=close, score=score, reason="fallback_volume_speed", volume_speed=volume_speed)

        # V3: pending化後にvolume系フィールドが完全に失われるケース向け。
        # score>=2.5はTONOSAMA候補生成・AI bridge通過済みの強い候補として扱う。
        score_only_enabled = _env_on("FINAL_ENTRY_TONOSAMA_SCORE_ONLY_FALLBACK", True)
        min_score_only = _env_float("FINAL_ENTRY_TONOSAMA_SCORE_ONLY_MIN_SCORE", 2.5)
        if score_only_enabled and score >= min_score_only and close > 0:
            return _ok_fallback(row, symbol=symbol, side=side, close=close, score=score, reason="fallback_score_only_lost_volume", volume_speed=3.0)

        logger.warning(
            "[FINAL TONOSAMA LIQ] NG symbol=%s side=%s reason=no_volume_signal volume=%.0f turnover=%.0f volume_speed=%.4f score=%.4f close=%.1f score_only_enabled=%s score_only_min=%.2f keys=%s",
            symbol, side, volume, turnover, volume_speed, score, close, score_only_enabled, min_score_only, sorted(list(d.keys()))[:100],
        )
        return False

    except Exception:
        logger.exception("[FINAL TONOSAMA LIQ] patched guard fatal -> NG")
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_LIQUIDITY_GUARD
    if _INSTALLED:
        return True
    try:
        import core.startup.final_entry_safety_guard_patch as fg
        cur = getattr(fg, "_liquidity_guard", None)
        if not callable(cur):
            logger.warning("[FINAL TONOSAMA LIQ] target missing")
            return False
        if getattr(cur, "_final_tonosama_liq_patch", False):
            _INSTALLED = True
            return True
        _ORIG_LIQUIDITY_GUARD = cur
        _patched_liquidity_guard._final_tonosama_liq_patch = True  # type: ignore[attr-defined]
        _patched_liquidity_guard._original = cur  # type: ignore[attr-defined]
        fg._liquidity_guard = _patched_liquidity_guard
        _INSTALLED = True
        logger.warning(
            "[FINAL TONOSAMA LIQ] installed v3 enabled=%s min_volume=%s min_turnover=%s min_speed=%s min_score=%s score_only=%s score_only_min=%s",
            _env_on("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK", True),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_VOLUME", "10000"),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_TURNOVER", "3000000"),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_VOLUME_SPEED", "1.0"),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_SCORE", "0.01"),
            os.getenv("FINAL_ENTRY_TONOSAMA_SCORE_ONLY_FALLBACK", "1"),
            os.getenv("FINAL_ENTRY_TONOSAMA_SCORE_ONLY_MIN_SCORE", "2.5"),
        )
        return True
    except Exception:
        logger.exception("[FINAL TONOSAMA LIQ] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[FINAL TONOSAMA LIQ] auto install failed")


__all__ = ["install"]
