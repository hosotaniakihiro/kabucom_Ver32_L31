# ============================================================
# trading/signals/state/position_state.py
# ------------------------------------------------------------
# ✔ 現在のポジション状態を管理
# ✔ ENTRY / EXIT 判定と分離
# ✔ 部分利確・全決済・反転に対応
# ✔ EXITロジック・AI連携の基盤
# ============================================================

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict


@dataclass
class PositionState:
    """
    現在のポジション状態を保持するクラス

    - ENTRY 成功時に open()
    - EXIT 成功時に close()
    - prev_state（シグナル履歴）とは完全分離
    """

    # --- 基本情報 ---
    symbol: Optional[str] = None
    side: Optional[str] = None        # "LONG" / "SHORT"
    quantity: int = 0

    entry_price: Optional[float] = None
    entry_time: Optional[datetime] = None

    # --- 現在値（更新される） ---
    current_price: Optional[float] = None
    last_update_time: Optional[datetime] = None

    # --- EXIT 情報 ---
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None

    # --- 評価損益 ---
    unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None

    # ============================================================
    # 状態判定
    # ============================================================

    def has_position(self) -> bool:
        """
        ポジションを保有しているか
        """
        return self.symbol is not None and self.quantity > 0

    def is_long(self) -> bool:
        return self.side == "LONG"

    def is_short(self) -> bool:
        return self.side == "SHORT"

    # ============================================================
    # ENTRY / EXIT
    # ============================================================

    def open(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        time: Optional[datetime] = None,
    ) -> None:
        """
        ENTRY 成功時に呼ぶ
        """
        time = time or datetime.now()

        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.entry_price = price
        self.entry_time = time

        self.current_price = price
        self.last_update_time = time

        self.exit_price = None
        self.exit_time = None
        self.exit_reason = None

        self.unrealized_pnl = 0.0
        self.realized_pnl = None

    def close(
        self,
        *,
        price: float,
        reason: str,
        time: Optional[datetime] = None,
    ) -> None:
        """
        EXIT 成功時に呼ぶ（全決済）
        """
        if not self.has_position():
            return

        time = time or datetime.now()

        self.exit_price = price
        self.exit_time = time
        self.exit_reason = reason

        # --- 実現損益 ---
        if self.side == "LONG":
            self.realized_pnl = (price - self.entry_price) * self.quantity
        elif self.side == "SHORT":
            self.realized_pnl = (self.entry_price - price) * self.quantity

        # --- ポジション解消 ---
        self.symbol = None
        self.side = None
        self.quantity = 0
        self.entry_price = None
        self.entry_time = None
        self.current_price = None
        self.last_update_time = None
        self.unrealized_pnl = None

    # ============================================================
    # 更新（PUSH / 板 / ティック）
    # ============================================================

    def update_price(
        self,
        *,
        price: float,
        time: Optional[datetime] = None,
    ) -> None:
        """
        PUSH / ティック到着時に現在値を更新
        """
        if not self.has_position():
            return

        time = time or datetime.now()

        self.current_price = price
        self.last_update_time = time

        if self.side == "LONG":
            self.unrealized_pnl = (price - self.entry_price) * self.quantity
        elif self.side == "SHORT":
            self.unrealized_pnl = (self.entry_price - price) * self.quantity

    # ============================================================
    # 補助
    # ============================================================

    def snapshot(self) -> Dict[str, Optional[float]]:
        """
        現在のポジション状態を dict で返す（ログ・DB・Discord用）
        """
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "current_price": self.current_price,
            "unrealized_pnl": self.unrealized_pnl,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_reason": self.exit_reason,
            "realized_pnl": self.realized_pnl,
        }

    def __str__(self) -> str:
        if not self.has_position():
            return "PositionState(NO POSITION)"

        return (
            f"PositionState("
            f"{self.side} {self.symbol} x{self.quantity} | "
            f"entry={self.entry_price} | "
            f"current={self.current_price} | "
            f"unrealized_pnl={self.unrealized_pnl})"
        )
