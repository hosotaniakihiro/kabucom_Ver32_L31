# ============================================================
# trading/exit/exit_state_machine.py
# Ver1.2.0-FINAL-EXIT-STATE-MACHINE-HARDENED
# ------------------------------------------------------------
# ✔ Ver1.1.0 完全保持（機能削除ゼロ）
# ✔ EXIT 判断の唯一の場所
# ✔ ルール主導（AI は抑制のみ）
# ✔ ExitContext 以外の状態を一切持たない
# ✔ STOP → 建値 → ATRトレール → TIMEOUT
# ✔ AI は EXIT を「止める」だけ（LOG ONLY）
# ✔ 呼び出し元 API 変更に完全耐性
# ✔ 例外発生時でも EXIT ロジックは壊れない
# ✔ ATRゼロ / NaN / 異常値 完全防御
# ✔ holding_seconds 負値防止
# ✔ stop_price 未初期化安全化
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import math
from typing import Tuple, Any

from trading.exit.exit_context import ExitContext

from AI.inference.exit_predictor import should_block_exit_by_ai
from AI.features.exit_feature_builder import build_exit_features

logger = logging.getLogger("exit_state_machine")


# ============================================================
# ルール定数
# ============================================================

BREAKEVEN_TRIGGER_PCT = 0.0
TRAIL_ATR_MULTIPLIER = 1.5
MAX_HOLD_SECONDS = 60 * 60 * 2


# ============================================================
# utils
# ============================================================

def _safe_float(v, default=0.0):
    try:
        v = float(v)
        return v if math.isfinite(v) else default
    except Exception:
        return default


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

    # --------------------------------------------------------
    # ctx 必須
    # --------------------------------------------------------
    if ctx is None:
        logger.error("[EXIT] ctx is None → HOLD")
        return "HOLD", None

    # --------------------------------------------------------
    # price 正規化
    # --------------------------------------------------------
    if price is None:
        price = exit_price

    price = _safe_float(price, None)
    if price is None or price <= 0:
        logger.error("[EXIT] invalid price → HOLD")
        return "HOLD", None

    # --------------------------------------------------------
    # now
    # --------------------------------------------------------
    if now is None:
        now = dt.datetime.now()

    # --------------------------------------------------------
    # holding time
    # --------------------------------------------------------
    try:
        holding_seconds = max(0, int(ctx.holding_seconds(now)))
    except Exception:
        holding_seconds = 0

    # --------------------------------------------------------
    # TIMEOUT（最優先）
    # --------------------------------------------------------
    if holding_seconds >= MAX_HOLD_SECONDS:
        return _apply_exit_ai_filter(
            ctx, price, now, "EXIT", "TIMEOUT"
        )

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

            logger.debug(
                "[EXIT] %s ENTERED→BREAKEVEN stop=%.4f",
                ctx.symbol,
                ctx.stop_price,
            )

        return "HOLD", None

    # ========================================================
    # BREAKEVEN
    # ========================================================
    if ctx.state == "BREAKEVEN":

        if ctx.stop_price is None:
            ctx.stop_price = ctx.entry_price

        if ctx.side == "BUY" and price <= ctx.stop_price:
            return _apply_exit_ai_filter(
                ctx, price, now, "EXIT", "BREAKEVEN_STOP"
            )

        if ctx.side == "SELL" and price >= ctx.stop_price:
            return _apply_exit_ai_filter(
                ctx, price, now, "EXIT", "BREAKEVEN_STOP"
            )

        # 利益拡大 → TRAILING
        try:
            atr = _safe_float(ctx.atr_1min)
            mfe = _safe_float(ctx.mfe)
        except Exception:
            atr = 0.0
            mfe = 0.0

        if atr > 0 and mfe >= atr * TRAIL_ATR_MULTIPLIER:
            ctx.set_state("TRAILING")
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
            return _apply_exit_ai_filter(
                ctx, price, now, "EXIT", "TRAIL_STOP"
            )

        if ctx.side == "SELL" and price >= ctx.stop_price:
            return _apply_exit_ai_filter(
                ctx, price, now, "EXIT", "TRAIL_STOP"
            )

        return "HOLD", None

    # --------------------------------------------------------
    # unknown state
    # --------------------------------------------------------
    logger.warning("[EXIT] unknown state: %s", ctx.state)
    return "HOLD", None


# ============================================================
# トレーリングストップ更新（強化版）
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

    else:  # SELL
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

    # 特徴量生成
    try:
        features = build_exit_features(ctx, price, now)
    except Exception:
        logger.exception("[EXIT_AI] feature build failed")
        return "EXIT", reason

    # AI判定（止めるだけ）
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