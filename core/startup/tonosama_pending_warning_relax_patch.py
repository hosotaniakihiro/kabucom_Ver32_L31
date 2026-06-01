# ============================================================
# File   : core/startup/tonosama_pending_warning_relax_patch.py
# Version: V3-OPTIONAL-5SEC-FINAL-GUARD-RELAX
# ------------------------------------------------------------
# 目的:
#   TONOSAMA pending 登録直前の final guard で、5秒足が止まっている
#   だけの理由で出来高急増候補を全落ちさせない。
#
# 背景:
#   2026-06-01 ログで、4392/7220 などが
#     reason=five_sec_stopped_final_guard
#     chg5s=0.000
#     surge=3.00
#     chg3m/chg5m はプラス
#     ranking MA は上向き
#   にもかかわらず pending 登録前に reject されていた。
#
# 方針:
#   - ユーザー方針どおり「5秒足は必須にしない」。
#   - ranking MA guard / 5m逆行 / 3m逆行 / 高値追い / クライマックス
#     などの強い拒否理由は維持する。
#   - five_sec_stopped_final_guard は、3m/5m方向・出来高急増・ランキングMA
#     が良好なら許可する。
#   - pending_writer.REJECT_ZERO_5SEC_FINAL も False に落として、
#     元関数側の即時rejectを止める。
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


def _ma_ok(info: Any, *, side: str) -> bool:
    if not isinstance(info, dict):
        return True
    reason = str(info.get("reason") or "").strip().lower()
    if reason not in {"", "ok", "none"}:
        return False
    ma3_slope = _sf(info.get("ma3_slope"), 0.0)
    ma5_slope = _sf(info.get("ma5_slope"), 0.0)
    if side == "BUY":
        return ma3_slope >= 0 or ma5_slope >= 0 or (ma3_slope == 0 and ma5_slope == 0)
    if side == "SELL":
        return ma3_slope <= 0 or ma5_slope <= 0 or (ma3_slope == 0 and ma5_slope == 0)
    return True


def _primary_3m5m_ok(entry: dict[str, Any], info: Any) -> bool:
    cond = entry.get("entry_conditions") or {}
    if not isinstance(cond, dict):
        return False

    side = str(entry.get("side") or cond.get("side") or "").upper()
    symbol = entry.get("symbol")
    price_chg = _sf(cond.get("max_price_change_pct"), 0.0)
    chg_3m = _sf(cond.get("price_change_pct_3m"), 0.0)
    chg_5m = _sf(cond.get("price_change_pct_5m"), 0.0)
    chg_5s = _sf(cond.get("price_change_5s_pct"), 0.0)
    surge = _sf(cond.get("max_volume_surge_ratio"), 0.0)
    latest_volume = _sf(cond.get("latest_volume"), 0.0)
    volume_3m = _sf(cond.get("volume_3m"), 0.0)
    volume_5m = _sf(cond.get("volume_5m"), 0.0)
    slope = _sf(cond.get("slope"), 0.0)
    close_pos = _sf(cond.get("close_position_pct"), 50.0)

    min_volume = _env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_MIN_VOLUME", 50000.0)
    min_surge = _env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_MIN_SURGE", 3.0)
    min_abs_move = _env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_MIN_ABS_3M5M_MOVE", 0.0)

    volume_ok = max(latest_volume, volume_3m, volume_5m) >= min_volume
    surge_ok = surge >= min_surge
    ma_ok = _ma_ok(info, side=side)

    if side == "BUY":
        direction_ok = (
            chg_3m >= -0.001
            and chg_5m >= -0.001
            and (price_chg >= min_abs_move or chg_3m >= min_abs_move or chg_5m >= min_abs_move or slope >= 0)
        )
        # 高値圏でも、ここでは5秒停止だけを理由に落とさない。
        # buying_climax 系の強い拒否は元関数側で別理由として残る。
        close_ok = close_pos >= _env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_BUY_MIN_CLOSE_POS", 35.0)
    elif side == "SELL":
        direction_ok = (
            chg_3m <= 0.001
            and chg_5m <= 0.001
            and (price_chg <= -min_abs_move or chg_3m <= -min_abs_move or chg_5m <= -min_abs_move or slope <= 0)
        )
        close_ok = close_pos <= _env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_SELL_MAX_CLOSE_POS", 65.0)
    else:
        direction_ok = False
        close_ok = False

    ok = bool(volume_ok and surge_ok and ma_ok and direction_ok and close_ok)
    logger.warning(
        "[TONOSAMA PENDING 5SEC OPTIONAL V3] check symbol=%s side=%s ok=%s volume_ok=%s surge_ok=%s ma_ok=%s direction_ok=%s close_ok=%s five_sec_dt=%s chg5s=%.3f price_chg=%.3f chg3m=%.3f chg5m=%.3f surge=%.2f latest_volume=%.0f volume_3m=%.0f volume_5m=%.0f slope=%.6f close_pos=%.1f ma=%s",
        symbol,
        side,
        ok,
        volume_ok,
        surge_ok,
        ma_ok,
        direction_ok,
        close_ok,
        cond.get("five_sec_dt"),
        chg_5s,
        price_chg,
        chg_3m,
        chg_5m,
        surge,
        latest_volume,
        volume_3m,
        volume_5m,
        slope,
        close_pos,
        info,
    )
    return ok


def _patched_climax_reject_reason(entry: dict[str, Any]):
    reject, info = _ORIG_CLIMAX(entry)
    try:
        if reject == "five_sec_stopped_final_guard" and _env_on("TONOSAMA_FINAL_5SEC_OPTIONAL", True):
            if _primary_3m5m_ok(entry, info):
                logger.warning(
                    "[TONOSAMA PENDING 5SEC OPTIONAL V3] allow five_sec_stopped_final_guard symbol=%s side=%s",
                    entry.get("symbol"),
                    entry.get("side"),
                )
                return None, info
            return reject, info

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

        ma_ok = _ma_ok(info, side=side)
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

        # 5秒足は必須にしない。元関数側の即時 five_sec_stopped reject を止める。
        if _env_on("TONOSAMA_FINAL_5SEC_OPTIONAL", True):
            try:
                setattr(pw, "REJECT_ZERO_5SEC_FINAL", False)
                setattr(pw, "MIN_FINAL_5SEC_CHANGE_PCT", 0.0)
            except Exception:
                pass

        cur = getattr(pw, "_climax_reject_reason", None)
        if not callable(cur):
            logger.warning("[TONOSAMA PENDING WARNING RELAX] target missing")
            return False
        if getattr(cur, "_tonosama_pending_warning_relax_v3", False):
            _INSTALLED = True
            return True

        _ORIG_CLIMAX = getattr(cur, "_original", cur)
        _patched_climax_reject_reason._tonosama_pending_warning_relax_v3 = True  # type: ignore[attr-defined]
        _patched_climax_reject_reason._original = _ORIG_CLIMAX  # type: ignore[attr-defined]
        pw._climax_reject_reason = _patched_climax_reject_reason

        _INSTALLED = True
        logger.warning(
            "[TONOSAMA PENDING WARNING RELAX] installed V3 warning_enabled=%s optional_5sec=%s reject_zero_5sec=%s min_5sec=%s",
            _env_on("TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX", True),
            _env_on("TONOSAMA_FINAL_5SEC_OPTIONAL", True),
            getattr(pw, "REJECT_ZERO_5SEC_FINAL", None),
            getattr(pw, "MIN_FINAL_5SEC_CHANGE_PCT", None),
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
