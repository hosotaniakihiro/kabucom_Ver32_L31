# ============================================================
# backtest16/short_sale_rules.py
# ------------------------------------------------------------
# 空売り価格規制(§6.2)と空売り固有の約定妥当性(§6.4)。
#
#  §6.2 空売り価格規制(JPX):
#   - 基準値段から10%以上下落で価格規制が発動し、翌営業日の立会終了まで適用。
#   - ただし一律に空売り禁止ではない。売付価格/直近公表価格との関係・注文種別・
#     適用除外で可否が変わる。規制状態や必要価格データが不明なら「確定しない」。
#   - シグナルは勝手に削除せず、規制で注文不可/不確定になった件数を別集計する。
#
#  本システムの制約: 当時の価格規制の実データ(基準値段・発動フラグ)が履歴に無い。
#   → 当日の安値が基準からの-10%に到達したかを push/1分足で近似推定し、
#     到達後の空売りは「規制の可能性あり=不確定(UNDETERMINED)」として扱う。
#     到達前は NOT_TRIGGERED(発注可の見込み)。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

# 価格規制のトリガ(基準値段からの下落率)
PRICE_LIMIT_REG_DROP_PCT = 10.0


@dataclass
class ShortRegStatus:
    state: str          # "NOT_TRIGGERED" / "UNDETERMINED" / "UNKNOWN"
    reason: str = ""
    trigger_ts: Optional[pd.Timestamp] = None


def price_regulation_status(
    *,
    signal_ts: pd.Timestamp,
    base_price: Optional[float],   # 基準値段(前日終値で近似)
    intraday_low_before_signal: Optional[float],
    low_time_before_signal: Optional[pd.Timestamp] = None,
) -> ShortRegStatus:
    """シグナル時点で空売り価格規制が発動している可能性を近似判定する。
    - base_price 不明 → UNKNOWN(確定しない)
    - シグナル時点までの当日安値が base から -10% 以上 → UNDETERMINED(規制の可能性)
    - それ未満 → NOT_TRIGGERED
    一律禁止にはしない(§6.2)。"""
    if base_price is None or base_price <= 0 or intraday_low_before_signal is None:
        return ShortRegStatus("UNKNOWN", "基準値段/当日安値が不明のため規制状態を確定できない")
    drop_pct = (base_price - intraday_low_before_signal) / base_price * 100.0
    if drop_pct >= PRICE_LIMIT_REG_DROP_PCT:
        return ShortRegStatus("UNDETERMINED",
                              f"当日安値が基準から-{drop_pct:.1f}%(≥10%)で価格規制の可能性",
                              trigger_ts=low_time_before_signal)
    return ShortRegStatus("NOT_TRIGGERED", f"当日下落-{drop_pct:.1f}%(<10%)")


# ------------------------------------------------------------
# §6.4 約定妥当性(空売りは買気配基準、買い戻しは売気配基準)
# ------------------------------------------------------------
def short_fill_price_from_board(row: pd.Series) -> Optional[float]:
    """信用新規売りは利用可能な買気配(bid)で約定判定する(§6.4)。"""
    for c in ("bid1", "bid2"):
        if c in row and pd.notna(row[c]) and row[c] > 0:
            return float(row[c])
    return None


def cover_fill_price_from_board(row: pd.Series) -> Optional[float]:
    """買い戻しは利用可能な売気配(ask)で約定判定する(§6.4)。"""
    for c in ("ask1", "ask2"):
        if c in row and pd.notna(row[c]) and row[c] > 0:
            return float(row[c])
    return None


def long_buy_fill_price_from_board(row: pd.Series) -> Optional[float]:
    """買い新規は売気配(ask)で約定判定する。"""
    return cover_fill_price_from_board(row)


def long_sell_fill_price_from_board(row: pd.Series) -> Optional[float]:
    """買い建ての手仕舞い売りは買気配(bid)で約定判定する。"""
    return short_fill_price_from_board(row)


def available_qty_from_board(row: pd.Series, side_price_col: str) -> Optional[float]:
    """対応する気配数量。板数量不足は部分約定/未約定判定に使う(§6.4)。"""
    qty_col = side_price_col + "_qty"
    if qty_col in row and pd.notna(row[qty_col]):
        return float(row[qty_col])
    return None
