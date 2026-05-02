# ============================================================
# File   : scoring/pattern_dispatcher.py
# Version: Ver25-FINAL-PATTERN-DISPATCHER-FLAG-COMPAT
# ------------------------------------------------------------
# ・BUY / SELL の ENTRY + BONUS を統合
# ・score_total（符号付き）に集約
# ・score_reasons は dict[str, int] に統一
# ・reentry 条件を自動適用（ini 定義ベース）
# ・ini 定義 BONUS も必ず加算
# ・flag_* 正式対応
# ・旧キー後方互換対応
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Iterable, List

import pandas as pd

from scoring.config.score_table import build_score_tables
from scoring.utils.state_tracker import signal_state

from .buy.buy_patterns_bonus import get_buy_bonus_score
from .buy.buy_patterns_entry import get_buy_entry_score
from .sell.sell_patterns_bonus import get_sell_bonus_score
from .sell.sell_patterns_entry import get_sell_entry_score

logger = logging.getLogger(__name__)


# ============================================================
# util
# ============================================================

def _b(v) -> bool:
    try:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on")
    except Exception:
        return False


def _add_reason(row, key: str, score: int):
    if "score_reasons" not in row or not isinstance(row["score_reasons"], dict):
        row["score_reasons"] = {}
    row["score_reasons"][key] = row["score_reasons"].get(key, 0) + int(score)


def _signal_variants(signal_key: str) -> List[str]:
    """
    flag_正式対応 + 旧キー互換
    例:
      flag_breakout_high -> [flag_breakout_high, breakout_high]
      breakout_high      -> [breakout_high, flag_breakout_high]
    """
    key = str(signal_key or "").strip()
    if not key:
        return []

    out = [key]
    if key.startswith("flag_"):
        out.append(key[5:])
    else:
        out.append(f"flag_{key}")
    # 重複除去
    seen = set()
    uniq: List[str] = []
    for k in out:
        if k not in seen:
            uniq.append(k)
            seen.add(k)
    return uniq


def _row_has_signal(row, signal_key: str) -> bool:
    for k in _signal_variants(signal_key):
        if _b(row.get(k)):
            return True
    return False


def _resolve_reentry_key(reentry_table: Dict[str, str], signal_key: str) -> str:
    """
    reentry テーブルも flag_/旧キー両対応で引く
    """
    for k in _signal_variants(signal_key):
        if k in reentry_table:
            return reentry_table[k]
    return ""


# ============================================================
# ini スコアテーブル（唯一の定義源）
# ============================================================

TABLES = build_score_tables()

BUY_ENTRY_TABLE = TABLES.get("buy_entry", {})
BUY_BONUS_TABLE = TABLES.get("buy_bonus", {})
SELL_ENTRY_TABLE = TABLES.get("sell_entry", {})
SELL_BONUS_TABLE = TABLES.get("sell_bonus", {})
REENTRY_TABLE = TABLES.get("reentry", {})


# ============================================================
# main
# ============================================================

def dispatch_patterns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    if "score_total" not in df.columns:
        df["score_total"] = 0
    df["score_total"] = pd.to_numeric(df["score_total"], errors="coerce").fillna(0)

    if "score_reasons" not in df.columns:
        df["score_reasons"] = [{} for _ in range(len(df))]
    else:
        df["score_reasons"] = df["score_reasons"].apply(
            lambda x: dict(x) if isinstance(x, dict) else {}
        )

    # 行ごと処理
    for idx, row in df.iterrows():
        symbol = str(row.get("symbol", "")).strip()

        # ----------------------------------------------------
        # BUY ENTRY
        # ----------------------------------------------------
        try:
            b_entry_score, b_entry_labels = get_buy_entry_score(
                row=row,
                symbol=symbol,
                state=None,
            )
            if b_entry_score:
                df.at[idx, "score_total"] += int(b_entry_score)
                for k in b_entry_labels:
                    signal_score = BUY_ENTRY_TABLE.get(
                        k,
                        BUY_ENTRY_TABLE.get(k[5:] if str(k).startswith("flag_") else f"flag_{k}", int(b_entry_score)),
                    )
                    _add_reason(df.at[idx], k, int(signal_score))
        except Exception:
            logger.debug("[pattern_dispatcher] buy entry failed idx=%s", idx, exc_info=True)

        # ----------------------------------------------------
        # BUY BONUS
        # ----------------------------------------------------
        try:
            b_bonus_score, b_bonus_labels = get_buy_bonus_score(row)
            if b_bonus_score:
                df.at[idx, "score_total"] += int(b_bonus_score)
                for k in b_bonus_labels:
                    signal_score = BUY_BONUS_TABLE.get(
                        k,
                        BUY_BONUS_TABLE.get(k[5:] if str(k).startswith("flag_") else f"flag_{k}", 0),
                    )
                    _add_reason(df.at[idx], k, int(signal_score))
        except Exception:
            logger.debug("[pattern_dispatcher] buy bonus failed idx=%s", idx, exc_info=True)

        # ----------------------------------------------------
        # SELL ENTRY
        # ----------------------------------------------------
        try:
            s_entry_score, s_entry_labels = get_sell_entry_score(
                row=row,
                symbol=symbol,
                state=None,
            )
            if s_entry_score:
                df.at[idx, "score_total"] += int(s_entry_score)
                for k in s_entry_labels:
                    signal_score = SELL_ENTRY_TABLE.get(
                        k,
                        SELL_ENTRY_TABLE.get(k[5:] if str(k).startswith("flag_") else f"flag_{k}", 0),
                    )
                    _add_reason(df.at[idx], k, int(signal_score))
        except Exception:
            logger.debug("[pattern_dispatcher] sell entry failed idx=%s", idx, exc_info=True)

        # ----------------------------------------------------
        # SELL BONUS
        # ----------------------------------------------------
        try:
            s_bonus_score, s_bonus_labels = get_sell_bonus_score(row)
            if s_bonus_score:
                df.at[idx, "score_total"] += int(s_bonus_score)
                for k in s_bonus_labels:
                    signal_score = SELL_BONUS_TABLE.get(
                        k,
                        SELL_BONUS_TABLE.get(k[5:] if str(k).startswith("flag_") else f"flag_{k}", 0),
                    )
                    _add_reason(df.at[idx], k, int(signal_score))
        except Exception:
            logger.debug("[pattern_dispatcher] sell bonus failed idx=%s", idx, exc_info=True)

    logger.debug("[pattern_dispatcher] ENTRY+BONUS merged rows=%d", len(df))
    return df