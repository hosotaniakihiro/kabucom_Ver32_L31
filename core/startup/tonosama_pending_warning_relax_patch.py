# ============================================================
# File   : core/startup/tonosama_pending_warning_relax_patch.py
# Version: V1-WARNING-ONLY-CLIMAX-ALLOW
# ------------------------------------------------------------
# 目的:
#   TONOSAMA pending_writer の buying_climax_upper_wick_warning /
#   selling_climax_lower_wick_warning は「warning」名だが、現状は即reject扱い。
#
#   2026-05-29 12:42〜12:44ログでは、7014が
#     price_chg=0.271% / surge=3.00 / upper_wick=53.8 / close_pos=46.2
#   で候補化されたが、価格変化は急騰追いかけ基準0.50%未満にもかかわらず
#   buying_climax_upper_wick_warning でpending登録前に落ちていた。
#
# 方針:
#   - ranking MA guard のrejectは維持。
#   - *_warning のみ、価格変化が小さく、出来高とMA方向がOKなら許可する。
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


def _patched_climax_reject_reason(entry: dict[str, Any]):
    reject, info = _ORIG_CLIMAX(entry)
    try:
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
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA PENDING WARNING RELAX] installed v1 enabled=%s max_price_chg=%s",
            _env_on("TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX", True),
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
