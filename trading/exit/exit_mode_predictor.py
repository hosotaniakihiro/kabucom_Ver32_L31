# ============================================================
# trading/exit/exit_mode_predictor.py
# Ver1.0.0-FINAL-EXIT-MODE-PREDICTOR
# ------------------------------------------------------------
# ✔ EXIT方式を「予測」するだけ
# ✔ 判断・実行には一切関与しない
# ✔ 失敗してもシステムに影響なし
# ✔ LOG / 特徴量生成専用
# ============================================================

from __future__ import annotations
from typing import Literal, Dict

from trading.exit.exit_context import ExitContext


ExitMode = Literal["STOP", "TRAIL", "TIMEOUT"]


def predict_exit_mode(
    *,
    ctx: ExitContext,
    price: float,
    features: Dict | None = None,
) -> ExitMode:
    """
    このトレードはどの EXIT で終わりやすいかを予測する

    ⚠ 予測結果は「参考情報」専用
    """

    # --- 最小ルール（安全 fallback） ---
    if ctx.mfe < ctx.atr_1min * 0.2:
        return "STOP"

    if ctx.mfe > ctx.atr_1min * 0.6:
        return "TRAIL"

    return "TIMEOUT"
