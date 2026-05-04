# ============================================================
# trading/exit/exit_state_machine.py
# Ver1.1.0-FINAL-EXIT-STATE-MACHINE
# ------------------------------------------------------------
# ✔ EXIT 判断の唯一の場所
# ✔ ルール主導（AI はブレーキのみ）
# ✔ ExitContext 以外の状態を持たない
# ✔ STOP → 建値 → ATRトレール → TIMEOUT
# ✔ AI は EXIT を「止める」だけ（LOG ONLY）
# ✔ 例外発生時は必ずルール EXIT
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from typing import Tuple, Optional

from trading.exit.exit_context import ExitContext

# EXIT AI（抑制のみ）
from AI.inference.exit_predictor import should_block_exit_by_ai
from AI.features.exit_feature_builder import build_exit_features

logger = logging.getLogger("exit_state_machine")


# ============================================================
# ルール定数
# ============================================================

BREAKEVEN_TRIGGER_PCT = 0.0        # 建値移行条件（%）
TRAIL_ATR_MULTIPLIER = 1.5         # ATR トレーリング倍率
MAX_HOLD_SECONDS = 60 * 60 * 2     # 最大保有時間（2時間）


# ============================================================
# メイン STATE MACHINE
# ============================================================

def manage_exit(
    ctx: ExitContext,
    price: float,
    now: dt.datetime,
) -> Tuple[str, Optional[str]]:
    """
    EXIT 判断の唯一の入口

    Returns
    -------
    ("HOLD", None)
    ("EXIT", reason)
    """

    # --------------------------------------------------------
    # 経過時間
    # --------------------------------------------------------
    holding_seconds = ctx.holding_seconds(now)

    # --------------------------------------------------------
    # TIMEOUT（最優先）
    # --------------------------------------------------------
    if holding_seconds >= MAX_HOLD_SECONDS:
        return _apply_exit_ai_filter(
            ctx=ctx,
            price=price,
            now=now,
            action="EXIT",
            reason="TIMEOUT",
        )

    # --------------------------------------------------------
    # ENTERED → BREAKEVEN
    # --------------------------------------------------------
    if ctx.state == "ENTERED":
        if ctx.profit_pct(price) >= BREAKEVEN_TRIGGER_PCT:
            ctx.set_state("BREAKEVEN")
            ctx.stop_price = ctx.entry_price

            logger.debug(
                "[EXIT] %s ENTERED→BREAKEVEN stop=%.2f",
                ctx.symbol,
                ctx.stop_price,
            )
        return "HOLD", None

    # --------------------------------------------------------
    # BREAKEVEN
    # --------------------------------------------------------
    if ctx.state == "BREAKEVEN":
        if ctx.side == "BUY" and price <= ctx.stop_price:
            return _apply_exit_ai_filter(
                ctx, price, now, "EXIT", "BREAKEVEN_STOP"
            )

        if ctx.side == "SELL" and price >= ctx.stop_price:
            return _apply_exit_ai_filter(
                ctx, price, now, "EXIT", "BREAKEVEN_STOP"
            )

        # 利益が伸びたら TRAILING へ
        if ctx.mfe >= ctx.atr_1min * TRAIL_ATR_MULTIPLIER:
            ctx.set_state("TRAILING")
            _update_trailing_stop(ctx)

            logger.debug(
                "[EXIT] %s BREAKEVEN→TRAILING stop=%.2f",
                ctx.symbol,
                ctx.stop_price,
            )

        return "HOLD", None

    # --------------------------------------------------------
    # TRAILING
    # --------------------------------------------------------
    if ctx.state == "TRAILING":
        _update_trailing_stop(ctx)

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
    # フォールバック（安全）
    # --------------------------------------------------------
    logger.warning(
        "[EXIT] unknown state symbol=%s state=%s",
        ctx.symbol,
        ctx.state,
    )
    return "HOLD", None


# ============================================================
# トレーリングストップ更新
# ============================================================

def _update_trailing_stop(ctx: ExitContext) -> None:
    """
    ATR ベース トレーリングストップ更新
    """
    atr = ctx.atr_1min * TRAIL_ATR_MULTIPLIER

    if ctx.side == "BUY":
        new_stop = ctx.highest - atr
        if ctx.stop_price is None or new_stop > ctx.stop_price:
            ctx.stop_price = new_stop

    else:  # SELL
        new_stop = ctx.lowest + atr
        if ctx.stop_price is None or new_stop < ctx.stop_price:
            ctx.stop_price = new_stop


# ============================================================
# EXIT AI フィルタ（LOG ONLY）
# ============================================================

def _apply_exit_ai_filter(
    ctx: ExitContext,
    price: float,
    now: dt.datetime,
    action: str,
    reason: str,
) -> Tuple[str, Optional[str]]:
    """
    EXIT AI による抑制フィルタ

    - AI は EXIT を「止める」だけ
    - 例外発生時は必ず EXIT を通す
    """

    if action != "EXIT":
        return action, reason

    # --------------------------------------------------------
    # 特徴量生成
    # --------------------------------------------------------
    try:
        features = build_exit_features(
            ctx=ctx,
            price=price,
            now=now,
        )
    except Exception:
        logger.exception("[EXIT_AI] feature build failed")
        return "EXIT", reason

    # --------------------------------------------------------
    # AI 判定（抑制のみ）
    # --------------------------------------------------------
    try:
        if should_block_exit_by_ai(features):
            logger.info(
                "[EXIT_AI_BLOCK][LOG_ONLY] symbol=%s state=%s rule=%s",
                ctx.symbol,
                ctx.state,
                reason,
            )
            return "HOLD", "AI_BLOCK_EXIT"

    except Exception:
        logger.exception("[EXIT_AI] inference failed")

    return "EXIT", reason
