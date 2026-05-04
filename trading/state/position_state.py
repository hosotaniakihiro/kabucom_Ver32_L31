# ============================================================
# trading/state/position_state.py
# ------------------------------------------------------------
# ✔ ポジション状態の一元管理
# ✔ Position API / WS 両対応
# ============================================================

import logging

logger = logging.getLogger(__name__)


class PositionState:
    def __init__(self):
        self._positions = {}  # symbol -> dict

    # -----------------------------
    # API からの再構築
    # -----------------------------
    def rebuild_from_api(self, positions: list[dict]):
        """
        kabu API の positions 結果から状態を再構築
        """
        self._positions.clear()

        for p in positions:
            try:
                symbol = str(p.get("Symbol"))
                side = "BUY" if p.get("Side") == "1" else "SELL"
                qty = int(p.get("LeavesQty", 0))
                price = float(p.get("Price", 0))

                if not symbol or qty <= 0:
                    continue

                self._positions[symbol] = {
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": price,
                }

            except Exception as e:
                logger.error(f"[PositionState] rebuild error: {p} {e}")

        logger.info(f"[PositionState] rebuilt {len(self._positions)} positions")

    # -----------------------------
    # 参照用
    # -----------------------------
    @property
    def symbols(self):
        return set(self._positions.keys())

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions


# ============================================================
# ★ シングルトン（重要）
# ============================================================
position_state = PositionState()
