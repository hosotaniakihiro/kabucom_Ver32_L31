# ============================================================
# trading/entry/fill_handler.py
# Ver1.2.0-FINAL-ENTRY-FILL-WITH-EXIT-AI-SHAP
# ------------------------------------------------------------
# ✔ ENTRY 約定確定ハンドラ
# ✔ ExitContext を生成して global_state に登録
# ✔ EXIT AI（mode 予測 + SHAP）を ENTRY 直後に実行
# ✔ EXIT の最終判断は行わない（LOG ONLY）
# ✔ SHAP ログ永続化（学習・検証用）
# ============================================================

from __future__ import annotations

import logging
import datetime as dt

from global_state import global_data
from database import Session_position
from database.models import Position

# ------------------------------------------------------------
# ExitContext
# ------------------------------------------------------------
from trading.exit.exit_context import ExitContext

# ------------------------------------------------------------
# EXIT AI（推論 + SHAP）
# ------------------------------------------------------------
from AI.inference.exit_predictor import predict_exit_mode_with_shap
from AI.logs.exit_shap_logger import save_exit_shap_log
from AI.inference.model_loader import load_model

# ------------------------------------------------------------
# AI 特徴量
# ------------------------------------------------------------
from AI.features.feature_builder import build_exit_ai_features

logger = logging.getLogger("entry_fill_handler")


# ============================================================
# ENTRY 約定時ハンドラ（唯一の入口）
# ============================================================

def on_entry_filled(
    *,
    position_id: int,
    symbol: str,
    side: str,
    entry_price: float,
    qty: int,
    atr_1min: float,
):
    """
    ENTRY 約定確定後に呼ばれる

    - Position 確定
    - ExitContext 生成
    - EXIT AI 推論（SHAP 付き）
    """

    now = dt.datetime.now()

    session = Session_position()

    try:
        # ----------------------------------------------------
        # Position 取得
        # ----------------------------------------------------
        pos: Position | None = session.get(Position, position_id)
        if not pos:
            logger.error("❌ Position not found id=%s", position_id)
            return

        # ----------------------------------------------------
        # ExitContext 作成（唯一の正本）
        # ----------------------------------------------------
        ctx = ExitContext(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            atr_1min=atr_1min,
            entry_time=now,
        )

        # global_state に登録
        global_data.exit_ctx = getattr(global_data, "exit_ctx", {})
        global_data.exit_ctx[symbol] = ctx

        # ----------------------------------------------------
        # EXIT AI 用特徴量生成
        # ----------------------------------------------------
        try:
            features_df = build_exit_ai_features(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                atr_1min=atr_1min,
                entry_time=now,
            )
        except Exception:
            logger.exception("❌ build_exit_ai_features failed")
            features_df = None

        # ----------------------------------------------------
        # EXIT AI 推論（SHAP 付き）
        # ----------------------------------------------------
        if features_df is not None and not features_df.empty:
            try:
                exit_ai_model = load_model("exit_ai")

                shap_result = predict_exit_mode_with_shap(
                    model=exit_ai_model,
                    features_df=features_df,
                )

                # ExitContext に保持（判断には使わない）
                ctx.exit_mode_pred = shap_result.get("exit_mode")
                ctx.exit_confidence = shap_result.get("confidence")

                # ------------------------------------------------
                # SHAP ログ保存（学習・分析用）
                # ------------------------------------------------
                save_exit_shap_log(
                    trade_id=pos.hold_id,
                    symbol=symbol,
                    shap_result=shap_result,
                )

                logger.info(
                    "📊 EXIT_AI predicted mode=%s conf=%.3f (%s)",
                    ctx.exit_mode_pred,
                    ctx.exit_confidence or 0.0,
                    symbol,
                )

            except Exception:
                logger.exception("❌ EXIT AI inference failed")

        else:
            logger.info("ℹ EXIT AI skipped (no features) %s", symbol)

        # ----------------------------------------------------
        # Position 更新（ENTRY 確定）
        # ----------------------------------------------------
        pos.status = "OPEN"
        pos.avg_price = entry_price
        pos.qty = qty
        pos.entry_time = now

        session.commit()

        logger.info(
            "✅ ENTRY FILLED %s side=%s price=%.2f qty=%d",
            symbol,
            side,
            entry_price,
            qty,
        )

    except Exception:
        session.rollback()
        logger.exception("❌ on_entry_filled fatal error")

    finally:
        session.close()
