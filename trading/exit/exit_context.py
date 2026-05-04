# ============================================================
# trading/exit/exit_context.py
# Ver1.2.0-FINAL-EXIT-CONTEXT-AI-READY
# ------------------------------------------------------------
# ✔ EXIT 状態の単一正本
# ✔ dataclass 化（型安全）
# ✔ MFE / MAE / pct 内包（学習対応）
# ✔ state machine 完全互換
# ✔ 副作用なし（DB / API / global_state 非依存）
# ✔ AI / 学習 / 可視化すべてに使用可能
# ✔ ★ FINAL / EXIT AI 用特徴量生成を追加
# ============================================================

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional, Dict


# ============================================================
# ExitContext
# ============================================================

@dataclass
class ExitContext:
    """
    EXIT 管理の単一正本

    - entry 直後に生成される
    - 5秒足ごとに update_price()
    - EXIT 確定時に学習データとして永続化
    """

    # --------------------------------------------------------
    # 固定情報（不変）
    # --------------------------------------------------------
    symbol: str
    side: str                      # BUY / SELL
    entry_price: float
    atr_1min: float
    entry_time: dt.datetime

    # --------------------------------------------------------
    # 状態
    # --------------------------------------------------------
    state: str = "ENTERED"          # ENTERED / BREAKEVEN / TRAILING

    # --------------------------------------------------------
    # 価格追跡
    # --------------------------------------------------------
    highest: float = field(init=False)
    lowest: float = field(init=False)

    # --------------------------------------------------------
    # 学習用（価格ベース）
    # --------------------------------------------------------
    mfe: float = 0.0               # 最大含み益（正値）
    mae: float = 0.0               # 最大含み損（負値）

    # --------------------------------------------------------
    # ストップ管理
    # --------------------------------------------------------
    stop_price: Optional[float] = None

    # --------------------------------------------------------
    # AI / 可視化用
    # --------------------------------------------------------
    last_price: float = 0.0        # 最新価格スナップショット

    # --------------------------------------------------------
    # 初期化
    # --------------------------------------------------------
    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {self.side}")

        if self.entry_price <= 0:
            raise ValueError("entry_price must be > 0")

        if self.atr_1min < 0:
            raise ValueError("atr_1min must be >= 0")

        self.highest = self.entry_price
        self.lowest = self.entry_price
        self.last_price = self.entry_price

    # --------------------------------------------------------
    # 価格更新（5秒足ごとに必ず呼ばれる）
    # --------------------------------------------------------
    def update_price(self, price: float) -> None:
        """
        MFE / MAE / high / low を更新する唯一の場所
        """
        if price <= 0:
            return

        self.last_price = price

        if self.side == "BUY":
            self.highest = max(self.highest, price)
            self.lowest = min(self.lowest, price)

            diff = price - self.entry_price
            self.mfe = max(self.mfe, diff)
            self.mae = min(self.mae, diff)

        else:  # SELL
            self.lowest = min(self.lowest, price)
            self.highest = max(self.highest, price)

            diff = self.entry_price - price
            self.mfe = max(self.mfe, diff)
            self.mae = min(self.mae, diff)

    # --------------------------------------------------------
    # 経過秒
    # --------------------------------------------------------
    def holding_seconds(self, now: dt.datetime) -> int:
        return max(0, int((now - self.entry_time).total_seconds()))

    # --------------------------------------------------------
    # 利益率（%）
    # --------------------------------------------------------
    def profit_pct(self, price: float) -> float:
        if price <= 0:
            return 0.0

        if self.side == "BUY":
            return (price - self.entry_price) / self.entry_price * 100.0
        else:
            return (self.entry_price - price) / self.entry_price * 100.0

    # --------------------------------------------------------
    # MFE / MAE（%）
    # --------------------------------------------------------
    @property
    def mfe_pct(self) -> float:
        return (self.mfe / self.entry_price * 100.0) if self.entry_price else 0.0

    @property
    def mae_pct(self) -> float:
        return (self.mae / self.entry_price * 100.0) if self.entry_price else 0.0

    # --------------------------------------------------------
    # state 遷移（安全）
    # --------------------------------------------------------
    def set_state(self, new_state: str) -> None:
        if new_state not in ("ENTERED", "BREAKEVEN", "TRAILING"):
            raise ValueError(f"Invalid state: {new_state}")
        self.state = new_state

    # --------------------------------------------------------
    # ★ FINAL / EXIT AI 用特徴量（唯一の正本）
    # --------------------------------------------------------
    def build_ai_features(
        self,
        *,
        current_price: Optional[float] = None,
        now: Optional[dt.datetime] = None,
    ) -> Dict[str, float]:
        """
        FINAL_DECISION_AI / EXIT_AI 用特徴量
        train / infer / log 完全一致
        """

        price = current_price or self.last_price
        now = now or dt.datetime.now()

        # ------------------------------
        # 利益率 / DD
        # ------------------------------
        if price <= 0 or self.entry_price <= 0:
            profit_rate = 0.0
            drawdown_rate = 0.0
        else:
            pnl = price - self.entry_price
            if self.side == "SELL":
                pnl = -pnl

            profit_rate = pnl / self.entry_price
            drawdown_rate = abs(self.mae) / self.entry_price

        # ------------------------------
        # 保有時間
        # ------------------------------
        hold_seconds = self.holding_seconds(now)

        # ------------------------------
        # ボラティリティ（ATR 正規化）
        # ------------------------------
        volatility = (
            self.atr_1min / self.entry_price
            if self.atr_1min > 0 and self.entry_price > 0
            else 0.0
        )

        # ------------------------------
        # トレンド強度
        # ------------------------------
        trend_strength = (
            self.mfe / self.atr_1min
            if self.atr_1min > 0
            else 0.0
        )

        return {
            # FINAL_DECISION_AI FEATURE_ORDER と完全一致
            "profit_rate": float(profit_rate),
            "drawdown_rate": float(drawdown_rate),
            "hold_seconds": int(hold_seconds),
            "volume_speed": 0.0,        # EXIT時は固定（将来拡張可）
            "volatility": float(volatility),
            "trend_strength": float(trend_strength),
        }

    # --------------------------------------------------------
    # state machine / AI / 学習 互換 dict
    # --------------------------------------------------------
    def to_dict(self) -> Dict:
        """
        dict 化（state_machine / AI / logging 用）
        """
        return {
            "symbol": self.symbol,
            "side": self.side,
            "state": self.state,
            "entry_price": self.entry_price,
            "atr_1min": self.atr_1min,
            "entry_time": self.entry_time,
            "highest": self.highest,
            "lowest": self.lowest,
            "last_price": self.last_price,
            "mfe": self.mfe,
            "mae": self.mae,
            "mfe_pct": self.mfe_pct,
            "mae_pct": self.mae_pct,
            "stop_price": self.stop_price,
        }
