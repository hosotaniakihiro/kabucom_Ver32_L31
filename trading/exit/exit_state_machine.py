# ============================================================
# trading/exit/exit_state_machine.py
# Ver1.3.0-EXIT-CONFIRMATION-NO-ONE-TICK-CUT
# ------------------------------------------------------------
# ✔ EXIT 判断の唯一の場所
# ✔ STOP → 建値 → ATRトレール → TIMEOUT
# ✔ AI は EXIT を「止める」だけ（LOG ONLY）
#
# 【Ver1.3 追加】
# ✔ 一瞬の押し目・上ヒゲで切られないように EXIT 確認回数を追加
# ✔ 建値ストップ移行を profit>=0.00% から profit>=0.20% に変更可能
# ✔ BREAKEVEN_STOP / TRAIL_STOP は連続確認後にEXIT
# ✔ HARD系ではない通常STOPは 5秒ループ2回連続を既定にする
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import math
import os
from typing import Tuple, Any

from trading.exit.exit_context import ExitContext

from AI.inference.exit_predictor import should_block_exit_by_ai
from AI.features.exit_feature_builder import build_exit_features

logger = logging.getLogger("exit_state_machine")


# ============================================================
# ルール定数
# ============================================================

BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "0.20"))
TRAIL_ATR_MULTIPLIER = float(os.getenv("TRAIL_ATR_MULTIPLIER", "1.5"))
MAX_HOLD_SECONDS = int(float(os.getenv("EXIT_MAX_HOLD_SECONDS", str(60 * 60 * 2))))
EXIT_STATE_CONFIRM_TICKS = int(float(os.getenv("EXIT_STATE_CONFIRM_TICKS", "2")))
EXIT_STATE_CONFIRM_ENABLED = str(os.getenv("EXIT_STATE_CONFIRM_ENABLED", "1")).lower() not in {"0", "false", "no", "off"}


# ============================================================
# utils
# ============================================================

def _safe_float(v, default=0.0):
    try:
        v = float(v)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _confirm_exit(ctx: ExitContext, *, reason: str, price: float, required: int | None = None) -> bool:
    """
    同じEXIT理由が連続して出た場合のみ True。
    一瞬だけ stop_price を割った押し目で切られるのを防ぐ。
    """
    if not EXIT_STATE_CONFIRM_ENABLED:
        return True

    need = int(required if required is not None else EXIT_STATE_CONFIRM_TICKS)
    if need <= 1:
        return True

    key = f"{reason}"
    try:
        last_key = getattr(ctx, "_exit_confirm_key", None)
        count = int(getattr(ctx, "_exit_confirm_count", 0) or 0)
        if last_key == key:
            count += 1
        else:
            count = 1
        setattr(ctx, "_exit_confirm_key", key)
        setattr(ctx, "_exit_confirm_count", count)
        setattr(ctx, "_exit_confirm_last_price", price)
        logger.info(
            "[EXIT_CONFIRM] symbol=%s reason=%s count=%s/%s price=%.4f state=%s",
            getattr(ctx, "symbol", ""),
            reason,
            count,
            need,
            price,
            getattr(ctx, "state", ""),
        )
        return count >= need
    except Exception:
        logger.debug("[EXIT_CONFIRM] failed -> allow exit reason=%s", reason, exc_info=True)
        return True


def _reset_confirm(ctx: ExitContext) -> None:
    try:
        setattr(ctx, "_exit_confirm_key", None)
        setattr(ctx, "_exit_confirm_count", 0)
    except Exception:
        pass


# ============================================================
# メイン STATE MACHINE
# ============================================================

def manage_exit(
    ctx: ExitContext | None = None,
    price: float | None = None,
    now: dt.datetime | None = None,
    *,
    row: Any = None,
    symbol: str | None = None,
    exit_price: float | None = None,
    pos_list: Any = None,
    **_ignored,
) -> Tuple[str, str | None]:

    if ctx is None:
        logger.error("[EXIT] ctx is None → HOLD")
        return "HOLD", None

    if price is None:
        price = exit_price

    price = _safe_float(price, None)
    if price is None or price <= 0:
        logger.error("[EXIT] invalid price → HOLD")
        return "HOLD", None

    if now is None:
        now = dt.datetime.now()

    try:
        holding_seconds = max(0, int(ctx.holding_seconds(now)))
    except Exception:
        holding_seconds = 0

    if holding_seconds >= MAX_HOLD_SECONDS:
        return _apply_exit_ai_filter(ctx, price, now, "EXIT", "TIMEOUT")

    # ========================================================
    # ENTERED
    # ========================================================
    if ctx.state == "ENTERED":
        try:
            profit_pct = _safe_float(ctx.profit_pct(price))
        except Exception:
            profit_pct = 0.0

        if profit_pct >= BREAKEVEN_TRIGGER_PCT:
            ctx.set_state("BREAKEVEN")
            ctx.stop_price = ctx.entry_price
            _reset_confirm(ctx)
            logger.debug(
                "[EXIT] %s ENTERED→BREAKEVEN stop=%.4f profit_pct=%.3f trigger=%.3f",
                ctx.symbol,
                ctx.stop_price,
                profit_pct,
                BREAKEVEN_TRIGGER_PCT,
            )

        return "HOLD", None

    # ========================================================
    # BREAKEVEN
    # ========================================================
    if ctx.state == "BREAKEVEN":
        if ctx.stop_price is None:
            ctx.stop_price = ctx.entry_price

        if ctx.side == "BUY" and price <= ctx.stop_price:
            if _confirm_exit(ctx, reason="BREAKEVEN_STOP", price=price):
                return _apply_exit_ai_filter(ctx, price, now, "EXIT", "BREAKEVEN_STOP")
            return "HOLD", "BREAKEVEN_STOP_CONFIRMING"

        if ctx.side == "SELL" and price >= ctx.stop_price:
            if _confirm_exit(ctx, reason="BREAKEVEN_STOP", price=price):
                return _apply_exit_ai_filter(ctx, price, now, "EXIT", "BREAKEVEN_STOP")
            return "HOLD", "BREAKEVEN_STOP_CONFIRMING"

        _reset_confirm(ctx)

        try:
            atr = _safe_float(ctx.atr_1min)
            mfe = _safe_float(ctx.mfe)
        except Exception:
            atr = 0.0
            mfe = 0.0

        if atr > 0 and mfe >= atr * TRAIL_ATR_MULTIPLIER:
            ctx.set_state("TRAILING")
            _reset_confirm(ctx)
            _update_trailing_stop(ctx)
            logger.debug(
                "[EXIT] %s BREAKEVEN→TRAILING stop=%.4f",
                ctx.symbol,
                ctx.stop_price,
            )

        return "HOLD", None

    # ========================================================
    # TRAILING
    # ========================================================
    if ctx.state == "TRAILING":
        _update_trailing_stop(ctx)

        if ctx.stop_price is None:
            return "HOLD", None

        if ctx.side == "BUY" and price <= ctx.stop_price:
            if _confirm_exit(ctx, reason="TRAIL_STOP", price=price):
                return _apply_exit_ai_filter(ctx, price, now, "EXIT", "TRAIL_STOP")
            return "HOLD", "TRAIL_STOP_CONFIRMING"

        if ctx.side == "SELL" and price >= ctx.stop_price:
            if _confirm_exit(ctx, reason="TRAIL_STOP", price=price):
                return _apply_exit_ai_filter(ctx, price, now, "EXIT", "TRAIL_STOP")
            return "HOLD", "TRAIL_STOP_CONFIRMING"

        _reset_confirm(ctx)
        return "HOLD", None

    logger.warning("[EXIT] unknown state: %s", ctx.state)
    return "HOLD", None


# ============================================================
# トレーリングストップ更新
# ============================================================

def _update_trailing_stop(ctx: ExitContext) -> None:
    atr = _safe_float(ctx.atr_1min)
    if atr <= 0:
        return

    atr *= TRAIL_ATR_MULTIPLIER

    if ctx.side == "BUY":
        highest = _safe_float(ctx.highest)
        new_stop = highest - atr
        if ctx.stop_price is None or new_stop > ctx.stop_price:
            ctx.stop_price = new_stop
    else:
        lowest = _safe_float(ctx.lowest)
        new_stop = lowest + atr
        if ctx.stop_price is None or new_stop < ctx.stop_price:
            ctx.stop_price = new_stop


# ============================================================
# EXIT AI フィルタ（抑制のみ）
# ============================================================

def _apply_exit_ai_filter(
    ctx: ExitContext,
    price: float,
    now: dt.datetime,
    action: str,
    reason: str,
) -> Tuple[str, str | None]:
    if action != "EXIT":
        return action, reason

    try:
        features = build_exit_features(ctx, price, now)
    except Exception:
        logger.exception("[EXIT_AI] feature build failed")
        return "EXIT", reason

    try:
        block = should_block_exit_by_ai(features)
    except Exception:
        logger.exception("[EXIT_AI] inference failed")
        return "EXIT", reason

    if block:
        logger.info(
            "[EXIT_AI_BLOCK][LOG_ONLY] symbol=%s state=%s reason=%s",
            ctx.symbol,
            ctx.state,
            reason,
        )
        return "HOLD", "AI_BLOCK_EXIT"

    return "EXIT", reason
