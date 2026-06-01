# ============================================================
# File   : core/startup/tonosama_pending_warning_relax_patch.py
# Version: V2-WARNING-ONLY-CLIMAX-AND-OPTIONAL-5SEC
# ------------------------------------------------------------
# 目的:
#   TONOSAMA pending_writer の buying_climax_upper_wick_warning /
#   selling_climax_lower_wick_warning は「warning」名だが、現状は即reject扱い。
#
#   さらに、5秒足が実質取得できていないケースで
#     five_sec_stopped_final_guard
#   により pending 登録前に落ちる問題を緩和する。
#
# 背景:
#   ユーザー方針では「5秒足は必須にしない」。
#   ログ上も five_sec_dt=不明 / chg5s=0.000 のまま、
#   3m/5m価格変化・出来高急増・ランキングMAは見えている候補が
#   five_sec_stopped_final_guard で全落ちしていた。
#
# 方針:
#   - ranking MA guard のrejectは維持。
#   - *_warning のみ、価格変化が小さく、出来高とMA方向がOKなら許可する。
#   - five_sec_stopped_final_guard は、5秒足時刻が不明/なしなら許可する。
#   - 5秒足が本当に取得済みで止まっている場合だけ従来拒否を維持。
#   - reversal / high_chase / low_chase など強い拒否理由は従来通りreject。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_CLIMAX = None

_WARNING_REASONS = {
    "buying_climax_upper_wick_warning",
    "selling_climax_lower_wick_warning",
}


_DISABLE_OR_UNKNOWN_5SEC_REASONS = {
    "",
    "なし",
    "不明",
    "nan",
    "none",
    "nat",
    "<na>",
}


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


def _five_sec_unknown(cond: dict[str, Any]) -> bool:
    try:
        five_dt = str(cond.get("five_sec_dt") or "").strip().lower()
        if five_dt in _DISABLE_OR_UNKNOWN_5SEC_REASONS:
            return True
        latest_close = _sf(cond.get("latest_5sec_close"), 0.0)
        latest_volume = _sf(cond.get("latest_5sec_volume"), 0.0)
        # has_5sec_bar=True でも、時刻・close・volume が無いなら未取得扱い。
        if latest_close <= 0 and latest_volume <= 0:
            return True
        return False
    except Exception:
        return False


def _allow_five_sec_stopped_if_optional(entry: dict[str, Any], info: dict[str, Any]) -> bool:
    if not _env_on("TONOSAMA_FINAL_5SEC_OPTIONAL", True):
        return False

    cond = entry.get("entry_conditions") or {}
    if not isinstance(cond, dict):
        return False

    if not _five_sec_unknown(cond):
        return False

    side = str(entry.get("side") or cond.get("side") or "").upper()
    symbol = entry.get("symbol")
    price_chg = _sf(cond.get("max_price_change_pct"), 0.0)
    chg_3m = _sf(cond.get("price_change_pct_3m"), 0.0)
    chg_5m = _sf(cond.get("price_change_pct_5m"), 0.0)
    surge = _sf(cond.get("max_volume_surge_ratio"), 0.0)
    latest_volume = _sf(cond.get("latest_volume"), 0.0)
    volume_3m = _sf(cond.get("volume_3m"), 0.0)
    volume_5m = _sf(cond.get("volume_5m"), 0.0)
    slope = _sf(cond.get("slope"), 0.0)

    min_volume = _env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_MIN_VOLUME", 50000.0)
    min_surge = _env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_MIN_SURGE", 3.0)

    volume_ok = max(latest_volume, volume_3m, volume_5m) >= min_volume
    surge_ok = surge >= min_surge
    direction_ok = False
    if side == "BUY":
        direction_ok = price_chg >= 0 or chg_3m >= 0 or chg_5m >= 0 or slope >= 0
    elif side == "SELL":
        direction_ok = price_chg <= 0 or chg_3m <= 0 or chg_5m <= 0 or slope <= 0

    ma_ok = not isinstance(info, dict) or str(info.get("reason") or "").lower() in {"", "ok", "none"}

    if volume_ok and surge_ok and direction_ok and ma_ok:
        logger.warning(
            "[TONOSAMA PENDING 5SEC OPTIONAL] allow because 5sec unknown symbol=%s side=%s five_sec_dt=%s price_chg=%.3f chg3m=%.3f chg5m=%.3f surge=%.2f latest_volume=%.0f volume_3m=%.0f volume_5m=%.0f slope=%.6f ma=%s",
            symbol,
            side,
            cond.get("five_sec_dt"),
            price_chg,
            chg_3m,
            chg_5m,
            surge,
            latest_volume,
            volume_3m,
            volume_5m,
            slope,
            info,
        )
        return True

    logger.warning(
        "[TONOSAMA PENDING 5SEC OPTIONAL] keep reject symbol=%s side=%s volume_ok=%s surge_ok=%s direction_ok=%s ma_ok=%s five_sec_dt=%s price_chg=%.3f chg3m=%.3f chg5m=%.3f",
        symbol,
        side,
        volume_ok,
        surge_ok,
        direction_ok,
        ma_ok,
        cond.get("five_sec_dt"),
        price_chg,
        chg_3m,
        chg_5m,
    )
    return False


def _patched_climax_reject_reason(entry: dict[str, Any]):
    reject, info = _ORIG_CLIMAX(entry)
    try:
        if reject == "five_sec_stopped_final_guard" and _allow_five_sec_stopped_if_optional(entry, info if isinstance(info, dict) else {}):
            return None, info

        if not _env_on("TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX", True):
            return reject, info
        if reject not in _WARNING_REASONS:
            return reject, info

        cond = entry.get("entry_conditions") or {}
        side = str(entry.get("side") or cond.get("side") or "").upper()
        symbol = entry.get("symbol")
        price_chg = _sf(cond.get("max_price_change_pct"), 0.0)
        abs_chg = abs(price_chg)
        latest_volume = _sf(cond.get("latest_volume"), 0.0)
        volume_3m = _sf(cond.get("volume_3m"), 0.0)
        volume_5m = _sf(cond.get("volume_5m"), 0.0)
        surge = _sf(cond.get("max_volume_surge_ratio"), 0.0)
        slope = _sf(cond.get("slope"), 0.0)

        max_warning_chg = _env_float("TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT", 0.50)
        min_volume = _env_float("TONOSAMA_WARNING_ONLY_MIN_VOLUME", 50000.0)
        min_surge = _env_float("TONOSAMA_WARNING_ONLY_MIN_SURGE", 3.0)

        ma_ok = not isinstance(info, dict) or str(info.get("reason") or "").lower() in {"", "ok", "none"}
        volume_ok = latest_volume >= min_volume or volume_3m >= min_volume or volume_5m >= min_volume
        surge_ok = surge >= min_surge
        direction_ok = (side == "BUY" and slope >= -0.003) or (side == "SELL" and slope <= 0.003) or side not in {"BUY", "SELL"}

        if abs_chg < max_warning_chg and volume_ok and surge_ok and ma_ok and direction_ok:
            logger.warning(
                "[TONOSAMA PENDING WARNING RELAX] allow warning-only symbol=%s side=%s original=%s price_chg=%.3f max_warning_chg=%.3f latest_volume=%.0f volume_3m=%.0f volume_5m=%.0f surge=%.2f slope=%.6f ma=%s",
                symbol,
                side,
                reject,
                price_chg,
                max_warning_chg,
                latest_volume,
                volume_3m,
                volume_5m,
                surge,
                slope,
                info,
            )
            return None, info

        logger.warning(
            "[TONOSAMA PENDING WARNING RELAX] keep reject symbol=%s side=%s reason=%s price_chg=%.3f volume_ok=%s surge_ok=%s ma_ok=%s direction_ok=%s",
            symbol,
            side,
            reject,
            price_chg,
            volume_ok,
            surge_ok,
            ma_ok,
            direction_ok,
        )
    except Exception:
        logger.exception("[TONOSAMA PENDING WARNING RELAX] wrapper failed")
    return reject, info


def install() -> bool:
    global _INSTALLED, _ORIG_CLIMAX
    if _INSTALLED:
        return True
    try:
        import trading.entry.tonosama.pending_writer as pw
        cur = getattr(pw, "_climax_reject_reason", None)
        if not callable(cur):
            logger.warning("[TONOSAMA PENDING WARNING RELAX] target missing")
            return False
        if getattr(cur, "_tonosama_pending_warning_relax", False):
            _INSTALLED = True
            return True
        _ORIG_CLIMAX = cur
        _patched_climax_reject_reason._tonosama_pending_warning_relax = True  # type: ignore[attr-defined]
        _patched_climax_reject_reason._original = cur  # type: ignore[attr-defined]
        pw._climax_reject_reason = _patched_climax_reject_reason

        # 5秒足は必須にしない。実5秒足がある場合の強制拒否は wrapper 側で制御する。
        try:
            if _env_on("TONOSAMA_FINAL_5SEC_OPTIONAL", True):
                setattr(pw, "REJECT_ZERO_5SEC_FINAL", True)
        except Exception:
            pass

        _INSTALLED = True
        logger.warning(
            "[TONOSAMA PENDING WARNING RELAX] installed v2 warning_enabled=%s optional_5sec=%s max_price_chg=%s",
            _env_on("TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX", True),
            _env_on("TONOSAMA_FINAL_5SEC_OPTIONAL", True),
            os.getenv("TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT", "0.50"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA PENDING WARNING RELAX] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA PENDING WARNING RELAX] auto install failed")


__all__ = ["install"]
