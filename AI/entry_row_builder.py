# ============================================================
# AI/entry_row_builder.py
# ------------------------------------------------------------
# ✔ AI/entry_gate に渡す row を正規化生成
# ✔ summary / ranking / pending 共通
# ✔ 欠損・型ブレ完全耐性
# ✔ 判断ロジック一切なし
# ✔ ★ pending / ranking の score_total を絶対に破壊しない（最重要FIX）
# ✔ ★ pandas NaN を score_total=未設定として正しく扱う
# ============================================================

import logging
import math
import pandas as pd
from typing import Dict, Any

logger = logging.getLogger(__name__)


# ============================================================
# util
# ============================================================

def _safe_int(v, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, float) and math.isnan(v):
            return default
        return int(v)
    except Exception:
        return default


def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, float) and math.isnan(v):
            return default
        return float(v)
    except Exception:
        return default


# ============================================================
def build_entry_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    AI/entry_gate に渡す row を生成する唯一の関数
    - 正規化のみ
    - 値の意味変更・判断は一切しない
    """

    if row is None:
        return {}

    # pandas Series → dict
    if hasattr(row, "to_dict"):
        row = row.to_dict()

    # ========================================================
    # 基本情報
    # ========================================================
    symbol = row.get("symbol")
    if not symbol:
        return {}

    source = row.get("source", "UNKNOWN")

    entry_row: Dict[str, Any] = {
        "symbol": symbol,
        "symbolname": row.get("symbolname", ""),
        "source": source,
    }

    # ========================================================
    # 時間・価格・出来高
    # ========================================================
    entry_row["datetime"] = row.get("datetime")
    entry_row["interval"] = _safe_int(row.get("interval", 1), 1)

    close_price = _safe_float(
        row.get("close_price") or row.get("current_price"),
        0.0,
    )
    entry_row["close_price"] = close_price

    volume = _safe_int(
        row.get("volume") or row.get("trading_volume"),
        0,
    )
    entry_row["volume"] = volume

    # ========================================================
    # ★ turnover（売買代金）完全吸収
    # ========================================================
    turnover = (
        row.get("turnover")
        or row.get("trading_value")
        or row.get("TradingValue")
        or row.get("value")
        or row.get("Value")
    )

    # 最終 fallback（値が無い場合のみ）
    if turnover is None:
        turnover = close_price * volume

    entry_row["turnover"] = _safe_float(turnover, 0.0)

    # ========================================================
    # decision
    # ========================================================
    entry_row["entry_decision"] = row.get("entry_decision", "NONE")

    # ========================================================
    # ★ score_total 正規化（最重要・破壊禁止）
    # ========================================================
    raw_score_total = row.get("score_total")

    # NaN / None / 未設定のみ 0 扱い
    score_total = _safe_int(raw_score_total, 0)

    if score_total == 0:
        # ----------------------------------------------------
        # RANKING / PENDING は upstream で保証されている
        # → build_entry_row では「0 にしない」
        # ----------------------------------------------------
        if source in ("RANKING", "PENDING"):
            score_total = _safe_int(raw_score_total, 0)

        # ----------------------------------------------------
        # SUMMARY / 後方互換補完のみ許可
        # ----------------------------------------------------
        else:
            if "score" in row:
                score_total = _safe_int(row.get("score"), 0)
            elif "buy_score" in row:
                score_total = _safe_int(row.get("buy_score"), 0)
            elif "rank_strength" in row:
                score_total = _safe_int(row.get("rank_strength"), 0)

    entry_row["score_total"] = score_total

    # ========================================================
    # 補助スコア
    # ========================================================
    entry_row["buy_score"] = _safe_int(row.get("buy_score"), 0)
    entry_row["sell_score"] = _safe_int(row.get("sell_score"), 0)

    entry_row["dominant_side"] = row.get("dominant_side", "NONE")
    entry_row["dominant_ratio"] = _safe_float(row.get("dominant_ratio"), 0.0)

    # ========================================================
    # ranking / optional
    # ========================================================
    entry_row["volume_speed"] = _safe_float(row.get("volume_speed"), 0.0)
    entry_row["rank_type"] = row.get("rank_type")
    entry_row["rank_position"] = row.get("rank_position")
    entry_row["market"] = row.get("market")

    # ========================================================
    # debug（判断には使わない）
    # ========================================================
    entry_row["_raw"] = {
        "score_total_raw": raw_score_total,
        "pending_score": row.get("score"),
        "rank_strength": row.get("rank_strength"),
        "turnover_raw": row.get("turnover"),
    }

    return entry_row


# ============================================================
def build_entry_rows(df: pd.DataFrame):
    """
    DataFrame → list[entry_row]

    - None / empty 完全耐性
    - 不正 row は自動スキップ
    """

    if df is None or df.empty:
        return []

    rows = []

    for _, r in df.iterrows():
        try:
            er = build_entry_row(r)
            if er:
                rows.append(er)
        except Exception:
            logger.exception("❌ entry_row build failed")

    return rows