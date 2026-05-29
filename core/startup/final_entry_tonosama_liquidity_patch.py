# ============================================================
# File   : core/startup/final_entry_tonosama_liquidity_patch.py
# Version: V1-TONOSAMA-FINAL-LIQUIDITY-FALLBACK
# ------------------------------------------------------------
# 目的:
#   TONOSAMA候補は entry_controller に入る時点で top-level の volume/turnover が
#   0 または欠損になることがある。
#
#   その結果、専用ゲートOK・AI_GATE_OK後に final_entry_safety_guard_patch の
#     reason=volume_missing
#   で停止していた。
#
# 方針:
#   - TONOSAMAのみ、_raw / *_raw / volume_speed / dominant_ratio を使って
#     最終流動性判定を補完する。
#   - 通常SUMMARY/RANKINGは既存ガードそのまま。
#   - 実volumeが無い場合でも volume_speed>=1 かつ score>=0.01 のTONOSAMAは、
#     既にTONOSAMA候補生成側で出来高急増を確認済みとして通す。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

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
        return float(v)
    except Exception:
        return float(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _su(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_tonosama(row: Any) -> bool:
    try:
        if not isinstance(row, dict):
            return False
        return _su(row.get("source")) == "TONOSAMA" or _su(row.get("entry_type")) == "TONOSAMA"
    except Exception:
        return False


def _flatten(row: dict) -> dict:
    d = dict(row or {})
    raw = d.get("_raw")
    if hasattr(raw, "to_dict"):
        try:
            raw = raw.to_dict()
        except Exception:
            raw = None
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k not in d or d.get(k) in (None, "", 0, 0.0):
                d[k] = v
            rk = f"{k}_raw"
            if rk not in d:
                d[rk] = v
    return d


def _first_num(row: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
    for k in keys:
        if k in row:
            v = _sf(row.get(k), None)  # type: ignore[arg-type]
            if v is not None and v != 0:
                return float(v)
    return float(default)


def _score(row: dict) -> float:
    return max(abs(_sf(row.get(k), 0.0)) for k in ("_tonosama_score", "pending_score", "score", "score_buy", "score_sell", "final_score"))


def _patched_liquidity_guard(row: dict, symbol: str, side: str) -> bool:
    try:
        if not (_env_on("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK", True) and _is_tonosama(row)):
            return _ORIG_LIQUIDITY_GUARD(row, symbol, side)

        d = _flatten(row)
        close = _first_num(d, ("close", "close_price", "price", "current_price"), 0.0)
        volume = _first_num(d, ("volume", "volume_raw", "_latest_volume", "latest_volume", "Volume", "出来高"), 0.0)
        turnover = _first_num(d, ("turnover", "turnover_raw", "trading_value", "売買代金"), 0.0)
        if turnover <= 0 and close > 0 and volume > 0:
            turnover = close * volume

        volume_speed = _first_num(d, ("volume_speed", "volume_surge_ratio", "_max_volume_surge_ratio", "dominant_ratio"), 0.0)
        score = _score(d)

        # 実volumeがある場合は、TONOSAMA専用の緩め下限で判定する。
        min_volume = _env_float("FINAL_ENTRY_TONOSAMA_MIN_VOLUME", 10000.0)
        min_turnover = _env_float("FINAL_ENTRY_TONOSAMA_MIN_TURNOVER", 3000000.0)
        if volume > 0:
            if volume < min_volume:
                logger.warning(
                    "[FINAL TONOSAMA LIQ] NG symbol=%s side=%s reason=low_volume volume=%.0f min_volume=%.0f turnover=%.0f close=%.1f",
                    symbol, side, volume, min_volume, turnover, close,
                )
                return False
            if turnover > 0 and turnover < min_turnover:
                logger.warning(
                    "[FINAL TONOSAMA LIQ] NG symbol=%s side=%s reason=low_turnover turnover=%.0f min_turnover=%.0f volume=%.0f close=%.1f",
                    symbol, side, turnover, min_turnover, volume, close,
                )
                return False
            logger.warning(
                "[FINAL TONOSAMA LIQ] OK actual volume symbol=%s side=%s volume=%.0f turnover=%.0f close=%.1f score=%.4f",
                symbol, side, volume, turnover, close, score,
            )
            return True

        # 実volumeが消えている場合は、候補生成済みの出来高急増シグナルで補完。
        min_speed = _env_float("FINAL_ENTRY_TONOSAMA_MIN_VOLUME_SPEED", 1.0)
        min_score = _env_float("FINAL_ENTRY_TONOSAMA_MIN_SCORE", 0.01)
        if volume_speed >= min_speed and score >= min_score:
            logger.warning(
                "[FINAL TONOSAMA LIQ] OK fallback symbol=%s side=%s volume_missing volume_speed=%.4f score=%.4f close=%.1f",
                symbol, side, volume_speed, score, close,
            )
            return True

        logger.warning(
            "[FINAL TONOSAMA LIQ] NG symbol=%s side=%s reason=no_volume_signal volume=%.0f turnover=%.0f volume_speed=%.4f score=%.4f close=%.1f",
            symbol, side, volume, turnover, volume_speed, score, close,
        )
        return False
    except Exception:
        logger.exception("[FINAL TONOSAMA LIQ] patched guard failed -> original")
        return _ORIG_LIQUIDITY_GUARD(row, symbol, side)


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
            "[FINAL TONOSAMA LIQ] installed v1 enabled=%s min_volume=%s min_turnover=%s min_speed=%s",
            _env_on("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK", True),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_VOLUME", "10000"),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_TURNOVER", "3000000"),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_VOLUME_SPEED", "1.0"),
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
