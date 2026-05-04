# ============================================================
# exit_trailing.py
# Ver25-FINAL — STOP LOSS + TRAILING TAKE PROFIT（OPEN_POSITIONS統一）
# ------------------------------------------------------------
# ✔ 即時損切り : -0.3%
# ✔ トレーリング利確 : 高値/安値から -0.2%
# ✔ BUY / SELL 両対応
# ✔ position_side 完全廃止
# ============================================================

from global_state import global_data


# ============================================================
# パラメータ
# ============================================================
STOP_LOSS_PCT   = -0.3     # %
TRAIL_BACK_PCT  = 0.2      # %


# ============================================================
# トレーリング用ランタイム保持
# symbol -> dict
# ============================================================
def _get_runtime(symbol: str):
    rt = global_data.position_runtime.get(symbol)
    if rt is None:
        rt = {
            "max_price": None,
            "min_price": None,
        }
        global_data.position_runtime[symbol] = rt
    return rt


# ============================================================
# ポジション side を安全取得
# ============================================================
def _get_position_side(symbol: str) -> str | None:
    pos = global_data.open_positions.get(symbol)
    if not pos:
        return None

    # dict / ORM 両対応
    side = pos.get("side") if isinstance(pos, dict) else getattr(pos, "side", None)
    if not side:
        return None

    return "BUY" if "BUY" in side else "SELL"


# ============================================================
# メイン判定
# ============================================================
def check_trailing_exit(symbol: str, price: float) -> str | None:
    """
    戻り値:
        None            → EXIT しない
        str (reason)    → EXIT 理由
    """

    symbol = str(symbol)

    # 建玉が無ければ対象外
    if symbol not in global_data.open_positions:
        return None

    entry_price = global_data.position_entry_price.get(symbol)
    side = _get_position_side(symbol)

    if not side or not entry_price or price <= 0:
        return None

    rt = _get_runtime(symbol)

    # ========================================================
    # BUY ポジション
    # ========================================================
    if side == "BUY":

        # ---- 即時損切り
        pnl_pct = (price - entry_price) / entry_price * 100
        if pnl_pct <= STOP_LOSS_PCT:
            return f"STOP_LOSS_BUY {pnl_pct:.2f}%"

        # ---- トレーリング
        if rt["max_price"] is None or price > rt["max_price"]:
            rt["max_price"] = price
            return None

        drop_pct = (price - rt["max_price"]) / rt["max_price"] * 100
        if drop_pct <= -TRAIL_BACK_PCT:
            return f"TRAILING_TP_BUY {drop_pct:.2f}%"

    # ========================================================
    # SELL ポジション
    # ========================================================
    elif side == "SELL":

        pnl_pct = (entry_price - price) / entry_price * 100
        if pnl_pct <= STOP_LOSS_PCT:
            return f"STOP_LOSS_SELL {pnl_pct:.2f}%"

        if rt["min_price"] is None or price < rt["min_price"]:
            rt["min_price"] = price
            return None

        rebound_pct = (rt["min_price"] - price) / rt["min_price"] * 100
        if rebound_pct <= -TRAIL_BACK_PCT:
            return f"TRAILING_TP_SELL {rebound_pct:.2f}%"

    return None
