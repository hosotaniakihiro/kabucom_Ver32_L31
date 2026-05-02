# ============================================================
# File: trading/handlers/exit_controller_tonosama.py
# ------------------------------------------------------------
# 殿様イナゴ（BUY）専用 EXIT コントローラ
#
# ✔ 秒殺・裁量ゼロ
# ✔ 利確は刻む／損切は即死
# ✔ 最大保持時間を絶対に超えない
# ✔ ENTRY 側ロジックと完全独立
# ✔ ★ EARLY_SCALP 時間制限 EXIT を安全に統合（STEP②）
# ============================================================

from __future__ import annotations

import datetime as dt

from trading.exit.exit_common import safe_float


# ============================================================
# 固定パラメータ（絶対に変更しない）
# ============================================================

# 利確
TAKE_PROFIT_HALF = 0.006   # +0.6%
TAKE_PROFIT_FULL = 0.010   # +1.0%

# 損切
STOP_LOSS = -0.004         # -0.4%

# 時間
MAX_HOLD_SECONDS = 180     # 3分

# 勢い消失
MIN_VOLUME_SPEED = 1.2


# ============================================================
# メイン API
# ============================================================

def tonosama_exit(ctx) -> None:
    """
    殿様イナゴ BUY 用 EXIT 判定

    Parameters
    ----------
    ctx : ExitContext 互換オブジェクト
        必須属性:
          - ret : float
              現在のリターン（+ 利益 / - 損失）
          - hold_seconds : int
              保持時間（秒）
          - volume_speed : float
              現在の出来高スピード
          - entry_source : str (optional)
              ENTRY の source（EARLY_SCALP 等）
          - score_5m : float (optional)
              5分足スコア（EARLY_SCALP 時間制限に使用）
        必須メソッド:
          - exit_half()
          - exit_all()
          - force_exit(reason: str)（存在すれば使用）
    """

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------
    if ctx is None:
        return

    ret = safe_float(getattr(ctx, "ret", 0.0))
    hold = int(getattr(ctx, "hold_seconds", 0))
    volume_speed = safe_float(getattr(ctx, "volume_speed", 0.0))
    entry_source = str(getattr(ctx, "entry_source", "") or "")

    # ========================================================
    # ★ STEP②：EARLY_SCALP 時間制限 EXIT（最優先ではない）
    # --------------------------------------------------------
    # ・既存殿様ロジックを壊さない
    # ・EARLY_SCALP のみ適用
    # ・含み益がある場合のみ発動
    # ========================================================
    if entry_source == "EARLY_SCALP":
        score_5m = safe_float(getattr(ctx, "score_5m", 0.0))
        max_hold = 40 if score_5m >= 1.5 else 20

        if hold >= max_hold and ret >= 0:
            if hasattr(ctx, "force_exit"):
                ctx.force_exit(f"EARLY_SCALP_TIME_EXIT_{max_hold}s")
            else:
                ctx.exit_all()
            return

    # --------------------------------------------------------
    # ① 即損切（最優先）
    # --------------------------------------------------------
    if ret <= STOP_LOSS:
        ctx.exit_all()
        return

    # --------------------------------------------------------
    # ② 利確（段階）
    # --------------------------------------------------------
    if ret >= TAKE_PROFIT_FULL:
        ctx.exit_all()
        return

    if ret >= TAKE_PROFIT_HALF:
        ctx.exit_half()
        # 半分利確後も下の条件は評価し続ける

    # --------------------------------------------------------
    # ③ 時間切れ（殿様の命）
    # --------------------------------------------------------
    if hold >= MAX_HOLD_SECONDS:
        ctx.exit_all()
        return

    # --------------------------------------------------------
    # ④ 勢い消失
    # --------------------------------------------------------
    if volume_speed < MIN_VOLUME_SPEED:
        ctx.exit_all()
        return

    # --------------------------------------------------------
    # ⑤ 何もしない（継続）
    # --------------------------------------------------------
    return