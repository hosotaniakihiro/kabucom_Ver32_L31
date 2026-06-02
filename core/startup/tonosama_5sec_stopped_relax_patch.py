# ============================================================
# File   : core/startup/tonosama_5sec_stopped_relax_patch.py
# Version: V1.2-TONOSAMA-5SEC-RELAX-1M-DIRECTION-AND-MA-WARN
# ------------------------------------------------------------
# 目的:
#   TONOSAMA pending 登録直前の過剰な拒否を、1分足の実体・slope・出来高が
#   明確な方向を示している時だけ限定緩和する。
#
# V1.2:
#   - V1.1の安全ガードは維持。
#   - ただし開場直後などで3m/5m変化が0でも、1m signed_body + slope + range + volume
#     が同方向なら soft_rescue_*_direction_too_weak を出さない。
#   - sell_ranking_ma3_ma5_up_guard / buy_ranking_ma3_ma5_down_guard も、
#     1m方向が十分強い場合だけ WARN扱いでpending登録を許可する。
#   - climax/reversal系の拒否はそのまま維持。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG = None


def _env_bool(name: str, default: bool = True) -> bool:
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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _unknown_dt(v: Any) -> bool:
    s = str(v or "").strip().lower()
    return s in {"", "none", "nan", "nat", "不明", "なし"}


def _one_min_direction_ok(cond: dict[str, Any], side: str) -> tuple[bool, dict[str, Any]]:
    side_u = str(side or "").upper()
    price_chg = _safe_float(cond.get("max_price_change_pct"), 0.0)
    signed_body = _safe_float(cond.get("signed_body_change_pct"), price_chg)
    slope = _safe_float(cond.get("slope"), 0.0)
    rng = _safe_float(cond.get("intrabar_range_pct"), 0.0)
    close_pos = _safe_float(cond.get("close_position_pct"), 50.0)
    upper_wick = _safe_float(cond.get("upper_wick_pct"), 0.0)
    lower_wick = _safe_float(cond.get("lower_wick_pct"), 0.0)
    surge = _safe_float(cond.get("max_volume_surge_ratio"), 0.0)
    latest_volume = _safe_float(cond.get("latest_volume"), 0.0)

    min_body = abs(_env_float("TONOSAMA_5SEC_RELAX_MIN_1M_BODY_PCT", 0.10))
    min_slope = abs(_env_float("TONOSAMA_5SEC_RELAX_MIN_1M_SLOPE", 0.0010))
    min_range = _env_float("TONOSAMA_5SEC_RELAX_MIN_RANGE_PCT", 3.0)
    min_volume = _env_float("TONOSAMA_5SEC_RELAX_MIN_VOLUME", 50000.0)
    min_surge = _env_float("TONOSAMA_5SEC_RELAX_MIN_SURGE", 3.0)
    climax_range = _env_float("TONOSAMA_5SEC_RELAX_CLIMAX_RANGE_PCT", 6.0)

    diag = {
        "side": side_u,
        "price_chg": price_chg,
        "signed_body": signed_body,
        "slope": slope,
        "range": rng,
        "close_pos": close_pos,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "surge": surge,
        "latest_volume": latest_volume,
        "min_body": min_body,
        "min_slope": min_slope,
        "min_range": min_range,
        "min_volume": min_volume,
        "min_surge": min_surge,
    }

    if latest_volume < min_volume:
        return False, {**diag, "ng": "volume_low"}
    if rng < min_range:
        return False, {**diag, "ng": "range_low"}
    if surge < min_surge:
        return False, {**diag, "ng": "surge_low"}

    if side_u == "SELL":
        direction_ok = signed_body <= -min_body and slope <= -min_slope
        # SELLでは上ヒゲ・安値引けは弱気材料。下ヒゲ反転だけをクライマックス扱いする。
        climax_like = rng >= climax_range and close_pos <= 35.0 and lower_wick >= 45.0
        if direction_ok and not climax_like:
            return True, {**diag, "ok": "sell_1m_body_slope"}
        return False, {**diag, "ng": "sell_1m_direction_or_climax_ng", "direction_ok": direction_ok, "climax_like": climax_like}

    if side_u == "BUY":
        direction_ok = signed_body >= min_body and slope >= min_slope
        # BUYでは下ヒゲ・高値引けは強気材料。上ヒゲ反転だけをクライマックス扱いする。
        climax_like = rng >= climax_range and close_pos >= 65.0 and upper_wick >= 45.0
        if direction_ok and not climax_like:
            return True, {**diag, "ok": "buy_1m_body_slope"}
        return False, {**diag, "ng": "buy_1m_direction_or_climax_ng", "direction_ok": direction_ok, "climax_like": climax_like}

    return False, {**diag, "ng": "unknown_side"}


def _soft_rescue_final_reject(cond: dict[str, Any], side: str) -> tuple[bool, dict[str, Any]]:
    ai_reason = str(cond.get("ai_reason") or "")
    side_u = str(side or "").upper()
    if "soft_rescue" not in ai_reason and "range_rescue" not in ai_reason:
        return False, {"reason": "not_soft_rescue"}

    if _env_bool("TONOSAMA_SOFT_RESCUE_ALLOW_1M_DIRECTION", True):
        ok1, diag1 = _one_min_direction_ok(cond, side_u)
        if ok1:
            return False, {**diag1, "ai_reason": ai_reason[:180], "soft_rescue_1m_allow": True}

    chg3 = _safe_float(cond.get("price_change_pct_3m"), 0.0)
    chg5 = _safe_float(cond.get("price_change_pct_5m"), 0.0)
    price_chg = _safe_float(cond.get("max_price_change_pct"), 0.0)
    signed_body = _safe_float(cond.get("signed_body_change_pct"), price_chg)
    slope = _safe_float(cond.get("slope"), 0.0)
    rng = _safe_float(cond.get("intrabar_range_pct"), 0.0)
    close_pos = _safe_float(cond.get("close_position_pct"), 50.0)
    upper_wick = _safe_float(cond.get("upper_wick_pct"), 0.0)
    lower_wick = _safe_float(cond.get("lower_wick_pct"), 0.0)
    surge = _safe_float(cond.get("max_volume_surge_ratio"), 0.0)
    latest_volume = _safe_float(cond.get("latest_volume"), 0.0)

    min_move = abs(_env_float("TONOSAMA_SOFT_RESCUE_MIN_DIRECTION_MOVE", 0.10))
    min_range = _env_float("TONOSAMA_SOFT_RESCUE_CLIMAX_RANGE_PCT", 6.0)
    min_surge = _env_float("TONOSAMA_SOFT_RESCUE_CLIMAX_SURGE", 3.0)

    diag = {
        "side": side_u,
        "chg3": chg3,
        "chg5": chg5,
        "price_chg": price_chg,
        "signed_body": signed_body,
        "slope": slope,
        "range": rng,
        "close_pos": close_pos,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "surge": surge,
        "latest_volume": latest_volume,
        "min_move": min_move,
        "ai_reason": ai_reason[:180],
    }

    if side_u == "BUY":
        if price_chg < min_move or chg3 < min_move or chg5 < min_move or slope <= 0:
            return True, {**diag, "ng": "soft_rescue_buy_direction_too_weak"}
        if surge >= min_surge and rng >= min_range and (close_pos >= 65.0 or upper_wick >= 35.0):
            return True, {**diag, "ng": "soft_rescue_buying_climax_like_volume"}

    if side_u == "SELL":
        if price_chg > -min_move or chg3 > -min_move or chg5 > -min_move or slope >= 0:
            return True, {**diag, "ng": "soft_rescue_sell_direction_too_weak"}
        if surge >= min_surge and rng >= min_range and (close_pos <= 35.0 or lower_wick >= 35.0):
            return True, {**diag, "ng": "soft_rescue_selling_climax_like_volume"}

    return False, diag


def _side_ok_by_3m5m(cond: dict[str, Any], side: str, ma_info: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    side_u = str(side or "").upper()
    chg3 = _safe_float(cond.get("price_change_pct_3m"), 0.0)
    chg5 = _safe_float(cond.get("price_change_pct_5m"), 0.0)
    slope = _safe_float(cond.get("slope"), 0.0)
    price_chg = _safe_float(cond.get("max_price_change_pct"), 0.0)
    latest_volume = _safe_float(cond.get("latest_volume"), 0.0)
    min_volume = _safe_float(cond.get("min_final_latest_volume"), _env_float("TONOSAMA_MIN_FINAL_LATEST_VOLUME", 50000.0))
    ma3_slope = _safe_float(ma_info.get("ma3_slope"), 0.0)
    ma5_slope = _safe_float(ma_info.get("ma5_slope"), 0.0)
    ma_reason = str(ma_info.get("reason") or "")
    rows = _safe_float(ma_info.get("rows"), 0.0)

    eps = abs(_env_float("TONOSAMA_5SEC_FALLBACK_EPS", 0.000001))
    min_move = abs(_env_float("TONOSAMA_5SEC_FALLBACK_MIN_3M5M_CHANGE", 0.10))

    diag = {
        "side": side_u,
        "chg3": chg3,
        "chg5": chg5,
        "slope": slope,
        "price_chg": price_chg,
        "latest_volume": latest_volume,
        "min_volume": min_volume,
        "ma3_slope": ma3_slope,
        "ma5_slope": ma5_slope,
        "ma_reason": ma_reason,
        "rows": rows,
        "eps": eps,
        "min_move": min_move,
    }

    if latest_volume < min_volume:
        return False, {**diag, "ng": "latest_volume_low"}
    if ma_reason and ma_reason not in {"ok", ""}:
        return False, {**diag, "ng": "ranking_ma_not_ok"}

    if side_u == "BUY":
        aligned = (chg3 >= min_move) and (chg5 >= min_move) and (slope > eps)
        against = (price_chg < min_move) or (ma3_slope < -eps and ma5_slope < -eps)
        return bool(aligned and not against), {**diag, "aligned": aligned, "against": against}

    if side_u == "SELL":
        aligned = (chg3 <= -min_move) and (chg5 <= -min_move) and (slope < -eps)
        against = (price_chg > -min_move) or (ma3_slope > eps and ma5_slope > eps)
        return bool(aligned and not against), {**diag, "aligned": aligned, "against": against}

    return False, {**diag, "ng": "unknown_side"}


def _patched_climax_reject_reason(entry: dict[str, Any]):
    reject, ma_info = _ORIG(entry)  # type: ignore[misc]
    try:
        cond = entry.get("entry_conditions") or {}
        if not isinstance(cond, dict):
            return reject, ma_info
        side = str(entry.get("side") or cond.get("side") or "").upper()

        soft_reject, soft_diag = _soft_rescue_final_reject(cond, side)
        if soft_reject:
            logger.warning(
                "[TONOSAMA 5SEC RELAX] reject soft/range rescue symbol=%s side=%s diag=%s",
                entry.get("symbol"), side, soft_diag,
            )
            return soft_diag.get("ng", "soft_rescue_final_guard"), ma_info

        if reject in {"sell_ranking_ma3_ma5_up_guard", "buy_ranking_ma3_ma5_down_guard"} and _env_bool("TONOSAMA_RANKING_MA_GUARD_ALLOW_STRONG_1M", True):
            ok1, diag1 = _one_min_direction_ok(cond, side)
            if ok1:
                logger.warning(
                    "[TONOSAMA 5SEC RELAX] warn-only ranking MA guard because 1m direction strong symbol=%s side=%s reject=%s diag=%s ma_info=%s",
                    entry.get("symbol"), side, reject, diag1, ma_info,
                )
                return None, ma_info

        if reject != "five_sec_stopped_final_guard":
            return reject, ma_info
        if not _env_bool("TONOSAMA_5SEC_STOPPED_NO_DT_FALLBACK", True):
            return reject, ma_info

        five_sec_dt = cond.get("five_sec_dt")
        if not _unknown_dt(five_sec_dt):
            return reject, ma_info

        ok1, diag1 = _one_min_direction_ok(cond, side)
        if ok1:
            logger.warning(
                "[TONOSAMA 5SEC RELAX] allow five_sec_stopped because five_sec_dt missing and 1m direction strong symbol=%s side=%s diag=%s",
                entry.get("symbol"), side, diag1,
            )
            return None, ma_info

        ok, diag = _side_ok_by_3m5m(cond, side, ma_info if isinstance(ma_info, dict) else {})
        if ok:
            logger.warning(
                "[TONOSAMA 5SEC RELAX] allow five_sec_stopped because five_sec_dt missing and strict 3m/5m/ranking_ma aligned symbol=%s side=%s diag=%s",
                entry.get("symbol"), side, diag,
            )
            return None, ma_info

        logger.warning(
            "[TONOSAMA 5SEC RELAX] keep reject symbol=%s side=%s reason=%s diag=%s",
            entry.get("symbol"), side, reject, {"one_min": diag1, "mtf": diag},
        )
        return reject, ma_info
    except Exception:
        logger.exception("[TONOSAMA 5SEC RELAX] wrapper failed symbol=%s", entry.get("symbol") if isinstance(entry, dict) else None)
        return reject, ma_info


def install() -> bool:
    global _INSTALLED, _ORIG
    if _INSTALLED:
        return True
    try:
        import trading.entry.tonosama.pending_writer as pw
        cur = getattr(pw, "_climax_reject_reason", None)
        if not callable(cur):
            logger.warning("[TONOSAMA 5SEC RELAX] target unavailable")
            return False
        if getattr(cur, "_tonosama_5sec_relax_v12", False):
            _INSTALLED = True
            return True
        _ORIG = getattr(cur, "_original", cur)
        _patched_climax_reject_reason._tonosama_5sec_relax_v12 = True  # type: ignore[attr-defined]
        _patched_climax_reject_reason._original = _ORIG  # type: ignore[attr-defined]
        pw._climax_reject_reason = _patched_climax_reject_reason
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA 5SEC RELAX] installed V1.2 enabled=%s eps=%s min_move=%s soft_rescue_guard=%s ma_guard_1m=%s",
            _env_bool("TONOSAMA_5SEC_STOPPED_NO_DT_FALLBACK", True),
            _env_float("TONOSAMA_5SEC_FALLBACK_EPS", 0.000001),
            _env_float("TONOSAMA_5SEC_FALLBACK_MIN_3M5M_CHANGE", 0.10),
            True,
            _env_bool("TONOSAMA_RANKING_MA_GUARD_ALLOW_STRONG_1M", True),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA 5SEC RELAX] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA 5SEC RELAX] auto install failed")

__all__ = ["install"]
