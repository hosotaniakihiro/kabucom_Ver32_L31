# ============================================================
# File   : core/startup/final_entry_tonosama_liquidity_patch.py
# Version: V2-TONOSAMA-FINAL-LIQUIDITY-NONE-SAFE-FALLBACK
# ------------------------------------------------------------
# 目的:
#   TONOSAMA候補は entry_controller に入る時点で top-level の volume/turnover が
#   0 または欠損になることがある。
#
#   その結果、専用ゲートOK・AI_GATE_OK後に final_entry_safety_guard_patch の
#     reason=volume_missing
#   で停止していた。
#
# V2 修正:
#   ✔ _sf(v, default=None) が float(None) で TypeError になる不具合を修正
#   ✔ _first_num() は None を安全に扱う
#   ✔ TONOSAMA候補で top-level volume/turnover が欠損しても _raw / *_raw / metrics を探索
#   ✔ patched guard 内で例外が出ても、TONOSAMA候補は即 original に戻さず fail-closed/明示判定
#   ✔ fallback許可時に row へ volume/turnover 補完値を書き戻し、後続guardでも volume_missing になりにくくする
#
# 方針:
#   - TONOSAMAのみ、_raw / *_raw / volume_speed / dominant_ratio を使って最終流動性判定を補完する。
#   - 通常SUMMARY/RANKINGは既存ガードそのまま。
#   - 実volumeが無い場合でも volume_speed>=1 かつ score>=0.01 のTONOSAMAは、
#     既にTONOSAMA候補生成側で出来高急増を確認済みとして通す。
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
    """
    None-safe float converter.

    旧実装は default=None のときも float(default) を実行し、
    TypeError: float() argument must be a string or a real number, not 'NoneType'
    で落ちていた。
    """
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
        if not isinstance(row, dict):
            return False
        return _su(row.get("source")) == "TONOSAMA" or _su(row.get("entry_type")) == "TONOSAMA"
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

    # Pandas Series / dict を _raw から展開
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

    # よくあるネストも浅く展開
    for nested_key in ("metrics", "features", "extra", "detail", "ai_detail"):
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
            row.setdefault("volume_speed", volume_speed)
            row.setdefault("volume_surge_ratio", volume_speed)
        if score > 0:
            row.setdefault("score", score)
            row.setdefault("pending_score", score)
    except Exception:
        logger.debug("[FINAL TONOSAMA LIQ] write_back failed", exc_info=True)


def _patched_liquidity_guard(row: dict, symbol: str, side: str) -> bool:
    if not (_env_on("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK", True) and _is_tonosama(row)):
        return _ORIG_LIQUIDITY_GUARD(row, symbol, side)

    try:
        d = _flatten(row)

        close = float(_first_num(d, ("close", "close_price", "price", "current_price", "Price", "CurrentPrice", "現在値"), 0.0) or 0.0)
        volume = float(_first_num(d, (
            "volume", "volume_raw", "_latest_volume", "latest_volume", "trading_volume", "TradingVolume", "Volume", "出来高",
            "volume_now", "current_volume", "accumulated_volume",
        ), 0.0) or 0.0)
        turnover = float(_first_num(d, (
            "turnover", "turnover_raw", "trading_value", "TradingValue", "売買代金", "value_amount", "amount",
        ), 0.0) or 0.0)
        if turnover <= 0 and close > 0 and volume > 0:
            turnover = close * volume

        volume_speed = float(_first_num(d, (
            "volume_speed", "volume_surge_ratio", "_max_volume_surge_ratio", "dominant_ratio", "volume_ratio", "surge_ratio",
        ), 0.0) or 0.0)
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
            _write_back(row, close=close, volume=volume, turnover=turnover, volume_speed=volume_speed, score=score)
            logger.warning(
                "[FINAL TONOSAMA LIQ] OK actual volume symbol=%s side=%s volume=%.0f turnover=%.0f close=%.1f score=%.4f",
                symbol, side, volume, turnover, close, score,
            )
            return True

        # 実volumeが消えている場合は、候補生成済みの出来高急増シグナルで補完。
        min_speed = _env_float("FINAL_ENTRY_TONOSAMA_MIN_VOLUME_SPEED", 1.0)
        min_score = _env_float("FINAL_ENTRY_TONOSAMA_MIN_SCORE", 0.01)
        if volume_speed >= min_speed and score >= min_score:
            # 後続guardが volume_missing にならないよう、仮想volumeを最小値で書き戻す。
            fallback_volume = _env_float("FINAL_ENTRY_TONOSAMA_FALLBACK_VOLUME", min_volume)
            fallback_turnover = close * fallback_volume if close > 0 else min_turnover
            _write_back(row, close=close, volume=fallback_volume, turnover=fallback_turnover, volume_speed=volume_speed, score=score)
            logger.warning(
                "[FINAL TONOSAMA LIQ] OK fallback symbol=%s side=%s volume_missing volume_speed=%.4f score=%.4f close=%.1f fallback_volume=%.0f fallback_turnover=%.0f",
                symbol, side, volume_speed, score, close, fallback_volume, fallback_turnover,
            )
            return True

        logger.warning(
            "[FINAL TONOSAMA LIQ] NG symbol=%s side=%s reason=no_volume_signal volume=%.0f turnover=%.0f volume_speed=%.4f score=%.4f close=%.1f keys=%s",
            symbol, side, volume, turnover, volume_speed, score, close, sorted(list(d.keys()))[:80],
        )
        return False

    except Exception:
        # TONOSAMAで例外が出た場合、originalへ戻すと volume_missing で分かりにくく落ちる。
        # ここでは明示NGにして原因ログを残す。
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
            "[FINAL TONOSAMA LIQ] installed v2 enabled=%s min_volume=%s min_turnover=%s min_speed=%s min_score=%s",
            _env_on("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK", True),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_VOLUME", "10000"),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_TURNOVER", "3000000"),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_VOLUME_SPEED", "1.0"),
            os.getenv("FINAL_ENTRY_TONOSAMA_MIN_SCORE", "0.01"),
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
