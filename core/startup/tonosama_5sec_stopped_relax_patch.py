# ============================================================
# File   : core/startup/tonosama_5sec_stopped_relax_patch.py
# Version: V1.0-TONOSAMA-5SEC-NO-DT-FALLBACK
# ------------------------------------------------------------
# 目的:
#   TONOSAMA pending 登録直前で five_sec_stopped_final_guard が出るが、
#   five_sec_dt=不明 の場合は「5秒足で止まった」のではなく
#   5秒足時刻が取れていないだけのケースがある。
#
# 方針:
#   - five_sec_stopped_final_guard かつ five_sec_dt が 不明/なし/空 の場合だけ緩和
#   - 3m/5m の向き、slope、ranking MA がエントリー方向と矛盾しない場合のみ通す
#   - 既存の climax / 逆方向 / 出来高不足 / MA逆行ガードは維持
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
    min_move = abs(_env_float("TONOSAMA_5SEC_FALLBACK_MIN_3M5M_CHANGE", 0.0))

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

    # ranking MA が明示的にNGを返している場合は通さない。
    if ma_reason and ma_reason not in {"ok", ""}:
        return False, {**diag, "ng": "ranking_ma_not_ok"}

    if side_u == "BUY":
        # 3m/5m/slope/ランキングMAのいずれかが買い方向、かつ明確な逆行がない。
        against = (chg3 < -min_move) or (chg5 < -min_move) or (slope < -eps) or (ma3_slope < -eps and ma5_slope < -eps)
        aligned = (chg3 > min_move) or (chg5 > min_move) or (slope > eps) or (ma3_slope > eps) or (ma5_slope > eps) or (price_chg > min_move)
        return bool(aligned and not against), {**diag, "aligned": aligned, "against": against}

    if side_u == "SELL":
        against = (chg3 > min_move) or (chg5 > min_move) or (slope > eps) or (ma3_slope > eps and ma5_slope > eps)
        aligned = (chg3 < -min_move) or (chg5 < -min_move) or (slope < -eps) or (ma3_slope < -eps) or (ma5_slope < -eps) or (price_chg < -min_move)
        return bool(aligned and not against), {**diag, "aligned": aligned, "against": against}

    return False, {**diag, "ng": "unknown_side"}


def _patched_climax_reject_reason(entry: dict[str, Any]):
    reject, ma_info = _ORIG(entry)  # type: ignore[misc]
    try:
        if reject != "five_sec_stopped_final_guard":
            return reject, ma_info
        if not _env_bool("TONOSAMA_5SEC_STOPPED_NO_DT_FALLBACK", True):
            return reject, ma_info

        cond = entry.get("entry_conditions") or {}
        if not isinstance(cond, dict):
            return reject, ma_info

        five_sec_dt = cond.get("five_sec_dt")
        # 5秒足時刻が取れているなら、既存の five_sec stopped 判定を尊重する。
        if not _unknown_dt(five_sec_dt):
            return reject, ma_info

        side = str(entry.get("side") or cond.get("side") or "").upper()
        ok, diag = _side_ok_by_3m5m(cond, side, ma_info if isinstance(ma_info, dict) else {})
        if ok:
            logger.warning(
                "[TONOSAMA 5SEC RELAX] allow five_sec_stopped because five_sec_dt missing and 3m/5m/ranking_ma aligned symbol=%s side=%s diag=%s",
                entry.get("symbol"), side, diag,
            )
            return None, ma_info

        logger.warning(
            "[TONOSAMA 5SEC RELAX] keep reject symbol=%s side=%s reason=%s diag=%s",
            entry.get("symbol"), side, reject, diag,
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
        if getattr(cur, "_tonosama_5sec_relax_v1", False):
            _INSTALLED = True
            return True
        _ORIG = cur
        _patched_climax_reject_reason._tonosama_5sec_relax_v1 = True  # type: ignore[attr-defined]
        _patched_climax_reject_reason._original = cur  # type: ignore[attr-defined]
        pw._climax_reject_reason = _patched_climax_reject_reason
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA 5SEC RELAX] installed enabled=%s eps=%s min_move=%s",
            _env_bool("TONOSAMA_5SEC_STOPPED_NO_DT_FALLBACK", True),
            _env_float("TONOSAMA_5SEC_FALLBACK_EPS", 0.000001),
            _env_float("TONOSAMA_5SEC_FALLBACK_MIN_3M5M_CHANGE", 0.0),
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
