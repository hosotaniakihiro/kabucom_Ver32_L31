# ============================================================
# File   : core/startup/entry_limit_passive_runtime_patch.py
# Version: V1.2-STRICT-BOARD-MTF-SLOPE-GUARD
# ------------------------------------------------------------
# SUMMARY_AI エントリー指値を board touch に変更し、同時に
# 「売ったら上がる / 買ったら下がる」対策として発注直前ガードを強化する。
#
# 重要:
#   BUY  : ask 指値
#   SELL : bid 指値
#
# 追加防衛:
#   1. 板が取れない場合は summary_fallback_touch で発注しない
#   2. technical_ready=False の SUMMARY_AI は止める
#   3. BUY は slope が正方向、SELL は slope が負方向のときだけ通す
#   4. MTF / score_mtf が逆方向なら止める
#   5. SELL で positive MTF を無視しない
#
# 環境変数:
#   ENTRY_STRICT_BOARD_REQUIRED=1
#   ENTRY_STRICT_SUMMARY_REQUIRE_TECHNICAL_READY=1
#   ENTRY_STRICT_SUMMARY_SLOPE_EPS=0.001
#   ENTRY_STRICT_SUMMARY_MTF_EPS=0.0
#   ENTRY_STRICT_SUMMARY_REQUIRE_MTF_DATA=1
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
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
        if v is None or v == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _first(row: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _ng(reason: str, **detail: Any) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


def _summary_ai_strict_guard(*, symbol: str, side: str, row: dict, detail: dict) -> Optional[Dict[str, Any]]:
    side_u = str(side or "").upper()

    if _env_bool("ENTRY_STRICT_BOARD_REQUIRED", True):
        board_ok = bool(detail.get("board"))
        price_source = str(detail.get("price_source") or "")
        if not board_ok or "fallback" in price_source.lower():
            return _ng(
                "STRICT_BOARD_MISSING",
                symbol=symbol,
                side=side_u,
                board=detail.get("board"),
                price_source=price_source,
                message="板が取れないためSUMMARY_AI新規エントリーを停止",
            )

    if _env_bool("ENTRY_STRICT_SUMMARY_REQUIRE_TECHNICAL_READY", True):
        technical_ready = row.get("technical_ready")
        if str(technical_ready).strip().lower() not in {"1", "true", "yes", "y", "on"}:
            return _ng(
                "STRICT_TECHNICAL_NOT_READY",
                symbol=symbol,
                side=side_u,
                technical_ready=technical_ready,
                message="technical_ready=False のためSUMMARY_AI新規エントリーを停止",
            )

    slope = _safe_float_or_none(_first(row, ("slope_atr_scaled", "slope", "score_slope", "disp_slope"), None))
    slope_eps = abs(_env_float("ENTRY_STRICT_SUMMARY_SLOPE_EPS", 0.001))
    if slope is None:
        return _ng("STRICT_SLOPE_MISSING", symbol=symbol, side=side_u, slope=slope)
    if side_u == "BUY" and slope <= slope_eps:
        return _ng("STRICT_BUY_SLOPE_NOT_POSITIVE", symbol=symbol, side=side_u, slope=slope, min_slope=slope_eps)
    if side_u == "SELL" and slope >= -slope_eps:
        return _ng("STRICT_SELL_SLOPE_NOT_NEGATIVE", symbol=symbol, side=side_u, slope=slope, max_slope=-slope_eps)

    mtf_values = {
        "mtf": _safe_float_or_none(_first(row, ("mtf", "score_mtf", "mtf_score", "score_mtf_merged"), None)),
        "slope_1m": _safe_float_or_none(_first(row, ("slope_atr_scaled_1m", "slope_1m", "slope1m"), None)),
        "slope_3m": _safe_float_or_none(_first(row, ("slope_atr_scaled_3m", "slope_3m", "slope3m"), None)),
        "slope_5m": _safe_float_or_none(_first(row, ("slope_atr_scaled_5m", "slope_5m", "slope5m"), None)),
    }
    available = {k: v for k, v in mtf_values.items() if v is not None}
    require_mtf = _env_bool("ENTRY_STRICT_SUMMARY_REQUIRE_MTF_DATA", True)
    mtf_eps = abs(_env_float("ENTRY_STRICT_SUMMARY_MTF_EPS", 0.0))
    if require_mtf and not available:
        return _ng("STRICT_MTF_MISSING", symbol=symbol, side=side_u, values=mtf_values)
    if side_u == "BUY":
        bad = {k: v for k, v in available.items() if v < -mtf_eps}
        if bad:
            return _ng("STRICT_MTF_NOT_BUY_ALIGNED", symbol=symbol, side=side_u, bad=bad, values=mtf_values)
    if side_u == "SELL":
        bad = {k: v for k, v in available.items() if v > mtf_eps}
        if bad:
            return _ng("STRICT_MTF_NOT_SELL_ALIGNED", symbol=symbol, side=side_u, bad=bad, values=mtf_values)

    return None


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import trading.handlers.entry_controller as ec
        import trading.handlers.entry_order_builder as eob
    except Exception:
        logger.exception("[ENTRY LIMIT BOARD TOUCH] import failed")
        return False

    try:
        old_build = getattr(eob, "build_entry_order", None)
        round_price = getattr(eob, "_round_price", None)
        if not callable(old_build) or not callable(round_price):
            logger.warning("[ENTRY LIMIT BOARD TOUCH] required functions not callable")
            return False

        # 0 = board base price そのまま。BUYはask、SELLはbid。
        setattr(eob, "SUMMARY_AGGRESSIVE_LIMIT_TICKS", 0)

        # fail-open しない方向へデフォルトを補正する。
        setattr(eob, "ENTRY_ORDER_REQUIRE_MTF_DATA", _env_bool("ENTRY_STRICT_SUMMARY_REQUIRE_MTF_DATA", True))
        setattr(eob, "ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF", False)

        def _board_touch_limit_price(base_price: float, side: str, ticks: int = 0) -> float:
            p = _safe_float(base_price, 0.0)
            if p <= 0:
                return p
            side_u = str(side or "").upper()
            return round_price(p, side_u)

        _board_touch_limit_price._entry_board_touch = True  # type: ignore[attr-defined]
        setattr(eob, "_aggressive_limit_price", _board_touch_limit_price)

        if not getattr(old_build, "_entry_board_touch_wrapped", False):
            def build_entry_order_board_touch(*args: Any, **kwargs: Any) -> Dict[str, Any]:
                ret = old_build(*args, **kwargs)
                try:
                    src = str(kwargs.get("source") or "").upper()
                    side = str(kwargs.get("side") or "").upper()
                    symbol = str(kwargs.get("symbol") or "").strip()
                    row = _row_to_dict(kwargs.get("entry_row"))

                    if isinstance(ret, dict) and ret.get("ok"):
                        detail = ret.get("detail")
                        if isinstance(detail, dict) and src == "SUMMARY_AI" and detail.get("order_type") == "LIMIT":
                            detail["aggressive_limit_ticks"] = 0
                            detail["entry_limit_mode"] = "BOARD_TOUCH_STRICT"
                            detail["price_rule"] = "BUY=ask / SELL=bid"
                            if detail.get("board"):
                                detail["price_source"] = "board_bid_ask_touch"
                            else:
                                detail["price_source"] = "summary_fallback_touch"

                            strict_ng = _summary_ai_strict_guard(symbol=symbol, side=side, row=row, detail=detail)
                            if strict_ng is not None:
                                logger.warning(
                                    "[ENTRY LIMIT BOARD TOUCH] NG symbol=%s side=%s reason=%s detail=%s",
                                    symbol,
                                    side,
                                    strict_ng.get("reason"),
                                    strict_ng.get("detail"),
                                )
                                return strict_ng

                            logger.warning(
                                "[ENTRY LIMIT BOARD TOUCH] symbol=%s side=%s price=%s base=%s rule=%s strict=OK",
                                symbol,
                                side,
                                detail.get("price"),
                                detail.get("base_price"),
                                detail.get("price_rule"),
                            )
                except Exception:
                    logger.debug("[ENTRY LIMIT BOARD TOUCH] detail patch failed", exc_info=True)
                return ret

            build_entry_order_board_touch._entry_board_touch_wrapped = True  # type: ignore[attr-defined]
            build_entry_order_board_touch._original = old_build  # type: ignore[attr-defined]
            setattr(eob, "build_entry_order", build_entry_order_board_touch)
            # entry_controller は from import で参照を保持しているため、同時に差し替える。
            try:
                setattr(ec, "build_entry_order", build_entry_order_board_touch)
            except Exception:
                logger.debug("[ENTRY LIMIT BOARD TOUCH] entry_controller patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[ENTRY LIMIT BOARD TOUCH] installed rule=BUY:ask SELL:bid strict_board=%s require_tech=%s slope_eps=%.6f require_mtf=%s sell_ignore_positive_mtf=False",
            _env_bool("ENTRY_STRICT_BOARD_REQUIRED", True),
            _env_bool("ENTRY_STRICT_SUMMARY_REQUIRE_TECHNICAL_READY", True),
            _env_float("ENTRY_STRICT_SUMMARY_SLOPE_EPS", 0.001),
            _env_bool("ENTRY_STRICT_SUMMARY_REQUIRE_MTF_DATA", True),
        )
        return True

    except Exception:
        logger.exception("[ENTRY LIMIT BOARD TOUCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY LIMIT BOARD TOUCH] auto install failed")


__all__ = ["install"]
