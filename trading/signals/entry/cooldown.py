# ============================================================
# trading/signals/entry/cooldown.py
# ------------------------------------------------------------
# ✔ ENTRY を抑制するためのクールダウン管理
# ✔ 時間帯 / 連続トレード / 連敗 / 手動停止に対応
# ✔ signal_state を操作する専用レイヤ
# ✔ entry_checker の外側で使う想定
# ============================================================

from datetime import datetime, time, timedelta
from typing import Optional, Dict

from trading.signals.state.signal_state import SignalState
from trading.signals.state.prev_state import PrevSignalState
from trading.signals.state.position_state import PositionState


# ============================================================
# 時間帯ルール
# ============================================================

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 0)

DEFAULT_OPEN_SKIP_MINUTES = 5      # 寄り付き直後
DEFAULT_CLOSE_SKIP_MINUTES = 5     # 引け前


def apply_time_based_cooldown(
    *,
    now: Optional[datetime],
    signal_state: SignalState,
    open_skip_minutes: int = DEFAULT_OPEN_SKIP_MINUTES,
    close_skip_minutes: int = DEFAULT_CLOSE_SKIP_MINUTES,
) -> None:
    """
    時間帯による ENTRY 抑制
    - 寄り直後
    - 引け前
    """
    now = now or datetime.now()
    t = now.time()

    # --- 寄り付き直後 ---
    open_until = (datetime.combine(now.date(), MARKET_OPEN)
                  + timedelta(minutes=open_skip_minutes))

    if MARKET_OPEN <= t < open_until.time():
        signal_state.set_skip(
            until=open_until,
            reason="寄り付き直後SKIP",
        )
        return

    # --- 引け前 ---
    close_from = (datetime.combine(now.date(), MARKET_CLOSE)
                  - timedelta(minutes=close_skip_minutes))

    if close_from.time() <= t < MARKET_CLOSE:
        signal_state.set_skip(
            until=datetime.combine(now.date(), MARKET_CLOSE),
            reason="引け前SKIP",
        )
        return


# ============================================================
# 連続トレード抑制
# ============================================================

def apply_consecutive_cooldown(
    *,
    prev_state: PrevSignalState,
    signal_state: SignalState,
    max_consecutive: int = 1,
    cooldown_seconds: int = 60,
) -> None:
    """
    同一方向の連続トレード抑制
    """
    if prev_state.consecutive_count > max_consecutive:
        signal_state.set_cooldown(
            seconds=cooldown_seconds,
            reason="連続トレード抑制",
        )


# ============================================================
# 連敗抑制（損失ベース）
# ============================================================

def apply_loss_cooldown(
    *,
    recent_realized_pnl: Optional[float],
    signal_state: SignalState,
    loss_threshold: float = -5000.0,
    cooldown_minutes: int = 10,
) -> None:
    """
    一定以上の損失後にクールダウン
    """
    if recent_realized_pnl is None:
        return

    if recent_realized_pnl <= loss_threshold:
        signal_state.set_cooldown(
            seconds=cooldown_minutes * 60,
            reason="損失クールダウン",
        )


# ============================================================
# ポジション状態連動
# ============================================================

def apply_position_cooldown(
    *,
    position_state: PositionState,
    signal_state: SignalState,
    seconds_after_exit: int = 30,
) -> None:
    """
    EXIT 直後のクールダウン
    """
    if position_state.exit_time:
        until = position_state.exit_time + timedelta(seconds=seconds_after_exit)
        if datetime.now() < until:
            signal_state.set_cooldown(
                seconds=(until - datetime.now()).seconds,
                reason="EXIT直後クールダウン",
            )


# ============================================================
# 統合ラッパ
# ============================================================

def apply_all_cooldowns(
    *,
    now: Optional[datetime],
    signal_state: SignalState,
    prev_state: PrevSignalState,
    position_state: PositionState,
    recent_realized_pnl: Optional[float] = None,
) -> Dict[str, Optional[str]]:
    """
    すべてのクールダウンルールを適用
    戻り値：現在の signal_state snapshot
    """

    # 時間帯
    apply_time_based_cooldown(
        now=now,
        signal_state=signal_state,
    )

    # 連続トレード
    apply_consecutive_cooldown(
        prev_state=prev_state,
        signal_state=signal_state,
    )

    # 連敗
    apply_loss_cooldown(
        recent_realized_pnl=recent_realized_pnl,
        signal_state=signal_state,
    )

    # EXIT直後
    apply_position_cooldown(
        position_state=position_state,
        signal_state=signal_state,
    )

    return signal_state.snapshot()
