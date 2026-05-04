# ============================================================
# trading/exit/exit_context_factory.py
# Ver1.0.0-FINAL-EXIT-CONTEXT-FACTORY
# ------------------------------------------------------------
# ✔ ExitContext 生成専用
# ✔ 初期値の正規化
# ✔ 副作用ゼロ
# ============================================================

import datetime as dt
from trading.exit.exit_context import ExitContext


def create_exit_context(
    *,
    symbol: str,
    side: str,
    entry_price: float,
    atr_1min: float,
    entry_time: dt.datetime | None = None,
) -> ExitContext:
    """
    ExitContext を正規生成する唯一の関数
    """

    if entry_time is None:
        entry_time = dt.datetime.now()

    ctx = ExitContext(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        atr_1min=atr_1min,
        entry_time=entry_time,
    )

    # state / stop_price / mfe / mae は
    # ExitContext.__post_init__ で正規化済み

    return ctx
