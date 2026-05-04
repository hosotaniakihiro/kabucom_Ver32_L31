# ============================================================
# trading/signals/state/prev_state.py
# ------------------------------------------------------------
# ✔ 前回のシグナル判定結果を保持
# ✔ 同一方向の連続エントリー防止
# ✔ フェイクブレイク回避
# ✔ 再エントリー / クールダウン制御の基盤
# ============================================================

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict


@dataclass
class PrevSignalState:
    """
    前回の ENTRY 判定状態を保持するクラス

    - entry_checker からのみ更新される想定
    - ポジション有無とは分離（position_state で管理）
    """

    # --- 最後に出たシグナル ---
    last_signal: Optional[str] = None   # "BUY" / "SELL" / None
    last_symbol: Optional[str] = None
    last_time: Optional[datetime] = None

    # --- 連続抑制用 ---
    consecutive_count: int = 0

    # --- クールダウン制御 ---
    cooldown_until: Optional[datetime] = None

    # --- デバッグ・解析用 ---
    last_reason: Optional[str] = None
    last_score: Optional[float] = None

    # --- 設定値（必要なら外から差し替え） ---
    cooldown_seconds: int = 30
    max_consecutive: int = 1

    # ============================================================
    # 状態判定
    # ============================================================

    def is_cooldown(self, now: Optional[datetime] = None) -> bool:
        """
        クールダウン中かどうか
        """
        if self.cooldown_until is None:
            return False

        now = now or datetime.now()
        return now < self.cooldown_until

    def can_emit(self, signal: str, symbol: str) -> bool:
        """
        同一シグナルの連続発火を防ぐ
        """
        if self.last_signal != signal:
            return True

        if self.last_symbol != symbol:
            return True

        if self.consecutive_count < self.max_consecutive:
            return True

        return False

    # ============================================================
    # 状態更新
    # ============================================================

    def update(
        self,
        *,
        signal: str,
        symbol: str,
        now: Optional[datetime] = None,
        reason: Optional[str] = None,
        score: Optional[float] = None,
        start_cooldown: bool = True,
    ) -> None:
        """
        シグナル確定時に呼ぶ
        """
        now = now or datetime.now()

        if self.last_signal == signal and self.last_symbol == symbol:
            self.consecutive_count += 1
        else:
            self.consecutive_count = 1

        self.last_signal = signal
        self.last_symbol = symbol
        self.last_time = now
        self.last_reason = reason
        self.last_score = score

        if start_cooldown:
            self.cooldown_until = now + timedelta(seconds=self.cooldown_seconds)
        else:
            self.cooldown_until = None

    def reset(self) -> None:
        """
        状態完全リセット（システム起動時・日付切替時など）
        """
        self.last_signal = None
        self.last_symbol = None
        self.last_time = None
        self.consecutive_count = 0
        self.cooldown_until = None
        self.last_reason = None
        self.last_score = None

    # ============================================================
    # 可視化・ログ用
    # ============================================================

    def snapshot(self) -> Dict[str, Optional[str]]:
        """
        現在状態を dict で返す（ログ・Discord用）
        """
        return {
            "last_signal": self.last_signal,
            "last_symbol": self.last_symbol,
            "last_time": self.last_time.isoformat() if self.last_time else None,
            "consecutive_count": self.consecutive_count,
            "cooldown_until": (
                self.cooldown_until.isoformat() if self.cooldown_until else None
            ),
            "last_reason": self.last_reason,
            "last_score": self.last_score,
        }

    def __str__(self) -> str:
        return (
            f"PrevSignalState("
            f"signal={self.last_signal}, "
            f"symbol={self.last_symbol}, "
            f"count={self.consecutive_count}, "
            f"cooldown_until={self.cooldown_until})"
        )
