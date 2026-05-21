# ============================================================
# File   : trading/exit/blowoff_profit_take.py
# Version: V2.0-BLOWOFF-PROFIT-TAKE-VOLUME-SLOPE
# ------------------------------------------------------------
# 株価が吹いた瞬間に通常EXIT判定より前で利確する。
#
# V2.0:
#   - 「吹いた」の判定に、含み益率だけでなく
#       1) 5秒足の出来高急増
#       2) 5秒足の slope / close-open 急変
#     を追加。
#   - 小ロット全利確・一部利確は、原則として
#     profit 条件 + volume 条件 + slope 条件が揃ったときだけ実行。
#   - 大きく利益が乗った場合の全利確は利益保護を優先し、確認条件なしでも実行可能。
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Tuple

from trading.exit.exit_price_source import get_latest_exit_price, get_five_sec_bar_safe
from trading.exit.exit_finalize import finalize_exit
from trading.exit.partial_profit_executor import execute_partial_profit

logger = logging.getLogger(__name__)

BLOWOFF_PROFIT_TAKE_ENABLED = str(os.getenv("BLOWOFF_PROFIT_TAKE_ENABLED", "1")).lower() not in {"0", "false", "no", "off"}
BLOWOFF_SMALL_QTY_FULL_TAKE_PCT = float(os.getenv("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT", "0.20"))
BLOWOFF_PARTIAL_TAKE_PCT = float(os.getenv("BLOWOFF_PARTIAL_TAKE_PCT", "0.25"))
BLOWOFF_FULL_TAKE_PCT = float(os.getenv("BLOWOFF_FULL_TAKE_PCT", "0.45"))
BLOWOFF_SMALL_QTY_MAX = int(float(os.getenv("BLOWOFF_SMALL_QTY_MAX", "199")))
BLOWOFF_PARTIAL_RATIO = float(os.getenv("BLOWOFF_PARTIAL_RATIO", "0.50"))

# 「吹いた」確認条件。小ロット全利確/一部利確に使う。
BLOWOFF_CONFIRM_ENABLED = str(os.getenv("BLOWOFF_CONFIRM_ENABLED", "1")).lower() not in {"0", "false", "no", "off"}
BLOWOFF_REQUIRE_VOLUME_AND_SLOPE = str(os.getenv("BLOWOFF_REQUIRE_VOLUME_AND_SLOPE", "1")).lower() not in {"0", "false", "no", "off"}
BLOWOFF_CONFIRM_FAIL_OPEN = str(os.getenv("BLOWOFF_CONFIRM_FAIL_OPEN", "0")).lower() in {"1", "true", "yes", "on"}

# 5秒足出来高。volume / avg_volume が取れる場合は倍率も見る。
BLOWOFF_MIN_5S_VOLUME = float(os.getenv("BLOWOFF_MIN_5S_VOLUME", "1000"))
BLOWOFF_MIN_VOLUME_SPIKE_RATIO = float(os.getenv("BLOWOFF_MIN_VOLUME_SPIKE_RATIO", "2.0"))

# 5秒足の傾き。barに slope 系があればそれを使用、無ければ close/open から算出。
# 0.10 = 0.10%。5秒で0.1%以上動いたら急変扱い。
BLOWOFF_MIN_5S_SLOPE_PCT = float(os.getenv("BLOWOFF_MIN_5S_SLOPE_PCT", "0.10"))


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


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _get(pos: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(pos, dict):
        return default
    for k in keys:
        v = pos.get(k)
        if v not in (None, ""):
            return v
    # 大文字小文字違いも吸収
    try:
        lower_map = {str(k).lower(): k for k in pos.keys()}
        for k in keys:
            real_key = lower_map.get(str(k).lower())
            if real_key is not None:
                v = pos.get(real_key)
                if v not in (None, ""):
                    return v
    except Exception:
        pass
    return default


def _normalize_side(v: Any) -> str:
    s = str(v or "").upper().strip()
    if s in {"BUY", "BUY_CREDIT", "LONG", "2", "信用買", "買", "買建"}:
        return "BUY"
    if s in {"SELL", "SELL_CREDIT", "SHORT", "1", "信用売", "売", "売建"}:
        return "SELL"
    return s


def _entry_price(pos: Dict[str, Any]) -> float:
    for k in ("avg_price", "entry_price", "AveragePrice", "average_price", "AvgPrice", "ExecutionPrice", "execution_price", "filled_price", "contract_price", "hold_price"):
        x = _safe_float(_get(pos, k), 0.0)
        if x > 0:
            return x
    src = str(_get(pos, "_position_source", default="") or "").upper()
    if "DB" in src:
        x = _safe_float(_get(pos, "Price", "price"), 0.0)
        if x > 0:
            return x
    return 0.0


def _profit_pct(side: str, entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    if side == "BUY":
        return (price - entry) / entry * 100.0
    if side == "SELL":
        return (entry - price) / entry * 100.0
    return 0.0


def _pnl(side: str, entry: float, price: float) -> float:
    if side == "BUY":
        return price - entry
    if side == "SELL":
        return entry - price
    return 0.0


def _bar_get(bar: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(bar, dict):
        return default
    for k in keys:
        v = bar.get(k)
        if v not in (None, ""):
            return v
    try:
        lower_map = {str(k).lower(): k for k in bar.keys()}
        for k in keys:
            real_key = lower_map.get(str(k).lower())
            if real_key is not None:
                v = bar.get(real_key)
                if v not in (None, ""):
                    return v
    except Exception:
        pass
    return default


def _calc_slope_pct_from_bar(bar: Dict[str, Any]) -> Tuple[float, str]:
    """5秒足の方向付き slope_pct を返す。

    barに slope / slope_pct / slope_atr_scaled があれば使う。
    値が 1 未満なら比率扱いとして *100、1以上なら pct とみなす。
    無ければ close/open で算出する。
    """
    raw = _bar_get(
        bar,
        "slope_pct", "slope_percent", "price_change_pct", "change_pct",
        "slope", "slope_atr_scaled", "score_slope",
        default=None,
    )
    if raw not in (None, ""):
        v = _safe_float(raw, 0.0)
        # 既存summaryでは slope=0.005 のような比率値が多いので、小さい値は%換算する。
        if abs(v) <= 1.0:
            return v * 100.0, "bar_slope"
        return v, "bar_slope_pct"

    open_px = _safe_float(_bar_get(bar, "open", "Open", "open_price", default=0.0), 0.0)
    close_px = _safe_float(_bar_get(bar, "close", "Close", "close_price", "price", "current_price", default=0.0), 0.0)
    if open_px > 0 and close_px > 0:
        return (close_px - open_px) / open_px * 100.0, "close_open_pct"

    high_px = _safe_float(_bar_get(bar, "high", "High", "high_price", default=0.0), 0.0)
    low_px = _safe_float(_bar_get(bar, "low", "Low", "low_price", default=0.0), 0.0)
    if high_px > 0 and low_px > 0:
        # 方向は分からないので正の値。方向条件では fail しやすい。
        return (high_px - low_px) / low_px * 100.0, "high_low_pct_no_direction"

    return 0.0, "missing"


def _volume_spike_from_bar(bar: Dict[str, Any]) -> Tuple[bool, float, float, float, str]:
    volume = _safe_float(
        _bar_get(bar, "volume", "Volume", "qty", "quantity", "tick_volume", "vol", default=0.0),
        0.0,
    )
    avg_volume = _safe_float(
        _bar_get(
            bar,
            "avg_volume", "volume_avg", "volume_ma", "volume_ma5", "avg_5s_volume", "avg_volume_5s",
            "prev_avg_volume", "baseline_volume", "volume_baseline",
            default=0.0,
        ),
        0.0,
    )
    if avg_volume > 0:
        ratio = volume / avg_volume if avg_volume > 0 else 0.0
        ok = volume >= BLOWOFF_MIN_5S_VOLUME and ratio >= BLOWOFF_MIN_VOLUME_SPIKE_RATIO
        return ok, volume, avg_volume, ratio, "volume_ratio"

    # 平均が無い場合は絶対出来高だけを見る。倍率は0表示。
    ok = volume >= BLOWOFF_MIN_5S_VOLUME
    return ok, volume, avg_volume, 0.0, "volume_abs_only"


def _slope_ok(side: str, slope_pct: float) -> bool:
    if side == "BUY":
        return slope_pct >= BLOWOFF_MIN_5S_SLOPE_PCT
    if side == "SELL":
        return slope_pct <= -BLOWOFF_MIN_5S_SLOPE_PCT
    return False


def _confirm_blowoff(symbol: str, side: str, bar: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    if not BLOWOFF_CONFIRM_ENABLED:
        return True, {"confirm_enabled": False, "reason": "disabled"}

    if not isinstance(bar, dict) or not bar:
        # price取得時のbarが空の場合、直接5秒足を再取得する。
        bar = get_five_sec_bar_safe(symbol)

    if not isinstance(bar, dict) or not bar:
        return bool(BLOWOFF_CONFIRM_FAIL_OPEN), {
            "confirm_enabled": True,
            "reason": "no_5sec_bar",
            "volume_ok": False,
            "slope_ok": False,
        }

    volume_ok, volume, avg_volume, volume_ratio, volume_reason = _volume_spike_from_bar(bar)
    slope_pct, slope_reason = _calc_slope_pct_from_bar(bar)
    slope_ok = _slope_ok(side, slope_pct)

    if BLOWOFF_REQUIRE_VOLUME_AND_SLOPE:
        ok = volume_ok and slope_ok
        reason = "volume_and_slope"
    else:
        ok = volume_ok or slope_ok
        reason = "volume_or_slope"

    detail = {
        "confirm_enabled": True,
        "reason": reason,
        "ok": bool(ok),
        "volume_ok": bool(volume_ok),
        "volume": float(volume),
        "avg_volume": float(avg_volume),
        "volume_ratio": float(volume_ratio),
        "volume_reason": volume_reason,
        "slope_ok": bool(slope_ok),
        "slope_pct": float(slope_pct),
        "slope_reason": slope_reason,
        "min_5s_volume": float(BLOWOFF_MIN_5S_VOLUME),
        "min_volume_ratio": float(BLOWOFF_MIN_VOLUME_SPIKE_RATIO),
        "min_slope_pct": float(BLOWOFF_MIN_5S_SLOPE_PCT),
    }
    return bool(ok), detail


def apply_blowoff_profit_take(*, symbol: str, pos: Dict[str, Any], regime: int = 0) -> bool:
    if not BLOWOFF_PROFIT_TAKE_ENABLED:
        return False
    try:
        side = _normalize_side(_get(pos, "side", "Side", "trade_side", "position_side", "order_side"))
        qty = _safe_int(_get(pos, "qty", "quantity", "LeavesQty", "HoldQty"), 0)
        entry = _entry_price(pos)
        price, bar = get_latest_exit_price(symbol)
        price = _safe_float(price, 0.0)
        profit = _profit_pct(side, entry, price)

        if side not in {"BUY", "SELL"} or qty <= 0 or entry <= 0 or price <= 0:
            return False

        blowoff_ok, blowoff_detail = _confirm_blowoff(symbol, side, bar if isinstance(bar, dict) else {})

        # 大きく利益が乗った場合は、出来高/slope確認を待たずに利益保護を優先する。
        if profit >= BLOWOFF_FULL_TAKE_PCT:
            reason = f"BLOWOFF_FULL_TAKE profit={profit:.3f}%>=trigger={BLOWOFF_FULL_TAKE_PCT:.3f}% qty={qty} blowoff_confirm={blowoff_ok} detail={blowoff_detail}"
            logger.warning(
                "[BLOWOFF PROFIT TAKE] FULL symbol=%s side=%s qty=%s entry=%.4f price=%.4f profit=%.3f%% confirm=%s detail=%s",
                symbol, side, qty, entry, price, profit, blowoff_ok, blowoff_detail,
            )
            finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=0, regime=regime, inago_state=0, pnl=_pnl(side, entry, price), collapse_prob=0.0, ctx=None)
            return True

        # 100株など小ロットは、利益条件 + 吹いた確認で全利確。
        if qty <= BLOWOFF_SMALL_QTY_MAX and profit >= BLOWOFF_SMALL_QTY_FULL_TAKE_PCT:
            if not blowoff_ok:
                logger.info(
                    "[BLOWOFF PROFIT TAKE] skip small confirm_ng symbol=%s side=%s qty=%s entry=%.4f price=%.4f profit=%.3f%% detail=%s",
                    symbol, side, qty, entry, price, profit, blowoff_detail,
                )
                return False
            reason = f"BLOWOFF_SMALL_QTY_FULL_TAKE profit={profit:.3f}%>=trigger={BLOWOFF_SMALL_QTY_FULL_TAKE_PCT:.3f}% qty={qty} detail={blowoff_detail}"
            logger.warning(
                "[BLOWOFF PROFIT TAKE] FULL small symbol=%s side=%s qty=%s entry=%.4f price=%.4f profit=%.3f%% detail=%s",
                symbol, side, qty, entry, price, profit, blowoff_detail,
            )
            finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=0, regime=regime, inago_state=0, pnl=_pnl(side, entry, price), collapse_prob=0.0, ctx=None)
            return True

        # 200株以上は、利益条件 + 吹いた確認で一部利確。
        if qty > BLOWOFF_SMALL_QTY_MAX and profit >= BLOWOFF_PARTIAL_TAKE_PCT:
            if not blowoff_ok:
                logger.info(
                    "[BLOWOFF PROFIT TAKE] skip partial confirm_ng symbol=%s side=%s qty=%s entry=%.4f price=%.4f profit=%.3f%% detail=%s",
                    symbol, side, qty, entry, price, profit, blowoff_detail,
                )
                return False
            reason = f"BLOWOFF_PARTIAL_TAKE profit={profit:.3f}%>=trigger={BLOWOFF_PARTIAL_TAKE_PCT:.3f}% qty={qty} detail={blowoff_detail}"
            logger.warning(
                "[BLOWOFF PROFIT TAKE] PARTIAL symbol=%s side=%s qty=%s entry=%.4f price=%.4f profit=%.3f%% detail=%s",
                symbol, side, qty, entry, price, profit, blowoff_detail,
            )
            return bool(execute_partial_profit(symbol=symbol, pos=pos, reason=reason, exit_price=price, ratio=BLOWOFF_PARTIAL_RATIO))

        return False
    except Exception:
        logger.exception("[BLOWOFF PROFIT TAKE] failed symbol=%s", symbol)
        return False


__all__ = ["apply_blowoff_profit_take"]
