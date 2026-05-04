# ============================================================
# trading/scoring/patterns/pattern_dispatcher.py
# Ver24-FINAL-PATTERN-DICT
# ------------------------------------------------------------
# ・BUY / SELL の ENTRY + BONUS を統合
# ・score_total（符号付き）に集約
# ・score_reasons は dict[str, int] に統一
# ・reentry 条件を自動適用（ini 定義ベース）
# ・ini 定義 BONUS も必ず加算（★重要）
# ============================================================

import pandas as pd
import logging

from scoring.config.score_table import build_score_tables
from scoring.utils.state_tracker import signal_state

# BUY / SELL logic bonus
from .buy.buy_patterns_bonus import get_buy_bonus_score
from .sell.sell_patterns_bonus import get_sell_bonus_score

logger = logging.getLogger(__name__)


# ============================================================
# 🔧 util
# ============================================================

def _b(v) -> bool:
    """安全 bool 判定"""
    try:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() in ("1", "true", "t", "yes", "y")
    except Exception:
        return False


def _add_reason(row, key: str, score: int):
    """reason dict に安全加算"""
    if "score_reasons" not in row or not isinstance(row["score_reasons"], dict):
        row["score_reasons"] = {}
    row["score_reasons"][key] = row["score_reasons"].get(key, 0) + int(score)


# ============================================================
# 🔧 ini スコアテーブル（唯一の定義源）
# ============================================================

TABLES = build_score_tables()

BUY_ENTRY_TABLE  = TABLES.get("buy_entry", {})
BUY_BONUS_TABLE  = TABLES.get("buy_bonus", {})
SELL_ENTRY_TABLE = TABLES.get("sell_entry", {})
SELL_BONUS_TABLE = TABLES.get("sell_bonus", {})
REENTRY_TABLE    = TABLES.get("reentry", {})


# ============================================================
# 🔥 Pattern Dispatcher
# ============================================================

def dispatch_patterns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # 列保証
    # --------------------------------------------------------
    if "score_total" not in df.columns:
        df["score_total"] = 0
    df["score_total"] = pd.to_numeric(df["score_total"], errors="coerce").fillna(0)

    if "score_reasons" not in df.columns:
        df["score_reasons"] = [{} for _ in range(len(df))]
    else:
        df["score_reasons"] = df["score_reasons"].apply(
            lambda x: dict(x) if isinstance(x, dict) else {}
        )

    # ========================================================
    # 🔥 行ごと処理
    # ========================================================
    for idx, row in df.iterrows():
        symbol = str(row.get("symbol", ""))

        # ================================
        # 🔵 BUY ENTRY（イベント）
        # ================================
        for key, score in BUY_ENTRY_TABLE.items():
            if not _b(row.get(key)):
                continue

            allow_reentry = key in REENTRY_TABLE
            re_cond = _b(row.get(REENTRY_TABLE.get(key))) if allow_reentry else False

            if not signal_state.is_first(
                symbol,
                key,
                allow_reentry=allow_reentry,
                reentry_condition=re_cond,
            ):
                continue

            df.at[idx, "score_total"] += score
            _add_reason(df.at[idx], key, score)

        # ================================
        # 🔵 BUY BONUS（ini 直結）
        # ================================
        for key, score in BUY_BONUS_TABLE.items():
            if _b(row.get(key)):
                df.at[idx, "score_total"] += score
                _add_reason(df.at[idx], key, score)

        # ================================
        # 🔴 SELL ENTRY
        # ================================
        for key, score in SELL_ENTRY_TABLE.items():
            if not _b(row.get(key)):
                continue

            allow_reentry = key in REENTRY_TABLE
            re_cond = _b(row.get(REENTRY_TABLE.get(key))) if allow_reentry else False

            if not signal_state.is_first(
                symbol,
                key,
                allow_reentry=allow_reentry,
                reentry_condition=re_cond,
            ):
                continue

            df.at[idx, "score_total"] += score
            _add_reason(df.at[idx], key, score)

        # ================================
        # 🔴 SELL BONUS（ini 直結）
        # ================================
        for key, score in SELL_BONUS_TABLE.items():
            if _b(row.get(key)):
                df.at[idx, "score_total"] += score
                _add_reason(df.at[idx], key, score)

        # ================================
        # 🔵 BUY / 🔴 SELL ロジック BONUS
        # ================================
        try:
            b_score, b_labels = get_buy_bonus_score(row)
            if b_score:
                df.at[idx, "score_total"] += int(b_score)
                for k in b_labels:
                    _add_reason(df.at[idx], k, int(b_score))
        except Exception:
            pass

        try:
            s_score, s_labels = get_sell_bonus_score(row)
            if s_score:
                df.at[idx, "score_total"] += int(s_score)
                for k in s_labels:
                    _add_reason(df.at[idx], k, int(s_score))
        except Exception:
            pass

    logger.debug(
        "[pattern_dispatcher] ENTRY+BONUS merged rows=%d",
        len(df)
    )

    return df
