# ============================================================
# trading/signals/state/signal_state.py
# ------------------------------------------------------------
# ✔ ENTRY 判定の「扱い」を管理する状態
# ✔ SKIP / COOLDOWN / FORCE を明示的に制御
# ✔ prev_state（履歴）・position_state（実ポジ）と分離
# ✔ entry_checker の判断をシンプルに保つ
# ============================================================

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict


# ============================================================
# Signal Mode 定義
# ============================================================

SIGNAL_ALLOW = "ALLOW"       # 通常判定OK
SIGNAL_SKIP = "SKIP"         # 一時的に無視
SIGNAL_COOLDOWN = "COOLDOWN" # クールダウン中
SIGNAL_FORCE = "FORCE"       # 強制ENTRY（テスト・手動）


@dataclass
class SignalState:
    """
    ENTRY シグナルの扱いを制御する状態クラス

    - 市場状況・時間帯・イベントに応じて ON/OFF
    - prev_state / position_state の外側に位置
    """

    # --- 現在のモード ---
    mode: str = SIGNAL_ALLOW

    # --- モード期限 ---
    until: Optional[datetime] = None

    # --- 理由・メモ ---
    reason: Optional[str] = None

    # ============================================================
    # 状態判定
    # ============================================================

    def is_allow(self, now: Optional[datetime] = None) -> bool:
        """
        ENTRY を許可するか
        """
        now = now or datetime.now()

        if self.mode == SIGNAL_ALLOW:
            return True

        if self.until and now >= self.until:
            self.reset()
            return True

        return self.mode == SIGNAL_FORCE

    def is_force(self) -> bool:
        return self.mode == SIGNAL_FORCE

    def is_skip(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        if self.mode != SIGNAL_SKIP:
            return False

        if self.until and now >= self.until:
            self.reset()
            return False

        return True

    def is_cooldown(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        if self.mode != SIGNAL_COOLDOWN:
            return False

        if self.until and now >= self.until:
            self.reset()
            return False

        return True

    # ============================================================
    # 状態遷移
    # ============================================================

    def set_skip(
        self,
        *,
        seconds: Optional[int] = None,
        until: Optional[datetime] = None,
        reason: Optional[str] = None,
    ) -> None:
        """
        ENTRY を一時的に無視（寄り直後・指標前など）
        """
        self.mode = SIGNAL_SKIP
        self.reason = reason

        if until:
            self.until = until
        elif seconds:
            self.until = datetime.now() + timedelta(seconds=seconds)
        else:
            self.until = None

    def set_cooldown(
        self,
        *,
        seconds: int,
        reason: Optional[str] = None,
    ) -> None:
        """
        ENTRY クールダウン（約定直後・連敗後など）
        """
        self.mode = SIGNAL_COOLDOWN
        self.reason = reason
        self.until = datetime.now() + timedelta(seconds=seconds)

    def set_force(
        self,
        *,
        reason: Optional[str] = None,
        seconds: Optional[int] = None,
    ) -> None:
        """
        強制 ENTRY（テスト・裁量介入）
        """
        self.mode = SIGNAL_FORCE
        self.reason = reason

        if seconds:
            self.until = datetime.now() + timedelta(seconds=seconds)
        else:
            self.until = None

    def reset(self) -> None:
        """
        通常状態へ戻す
        """
        self.mode = SIGNAL_ALLOW
        self.until = None
        self.reason = None

    # ============================================================
    # 補助
    # ============================================================

    def snapshot(self) -> Dict[str, Optional[str]]:
        """
        状態を dict 化（ログ・Discord・DB）
        """
        return {
            "mode": self.mode,
            "until": self.until.isoformat() if self.until else None,
            "reason": self.reason,
        }

    def __str__(self) -> str:
        return (
            f"SignalState(mode={self.mode}, "
            f"until={self.until}, "
            f"reason={self.reason})"
        )
