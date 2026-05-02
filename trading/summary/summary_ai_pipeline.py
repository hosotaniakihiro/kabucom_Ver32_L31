# ==========================================================
# File   : trading/summary/summary_ai_pipeline.py
# Version: Ver1.0-PRODUCTION-AI-ENTRY-PIPELINE
# ----------------------------------------------------------
# ✔ summary_controller からAI処理を完全分離
# ✔ ATR percentile filter
# ✔ AI entry check
# ✔ entry row build
# ✔ pending登録
# ✔ entry pipeline実行
# ✔ summary vs entry検証
# ✔ 副作用最小設計
# ✔ 本番安定版
# ==========================================================

from __future__ import annotations

import logging
import pandas as pd
from typing import List, Dict

from AI.entry_gate import ai_final_entry_check
from AI.entry_row_builder import build_entry_row

from trading.entry.pending_manager import add_pending
from trading.handlers.entry_controller import run_entry_pipeline

from trading.summary.summary_analysis_logger import verify_summary_vs_entry

logger = logging.getLogger(__name__)

# ==========================================================
# 定数
# ==========================================================

TOP_N = 30
ATR_DROP_RATIO = 0.30


# ==========================================================
# ATR フィルタ
# ==========================================================

def apply_atr_percentile_filter(
        df: pd.DataFrame,
        atr_col: str = "atr_1m",
        drop_ratio: float = ATR_DROP_RATIO
):

    try:

        if df is None or df.empty:
            return df

        if atr_col not in df.columns:
            return df

        df_valid = df.dropna(subset=[atr_col]).copy()

        if df_valid.empty:
            return df

        threshold = df_valid[atr_col].quantile(drop_ratio)

        df_valid = df_valid[
            df_valid[atr_col] >= threshold
        ]

        return df_valid

    except Exception:

        logger.exception("[AI_PIPELINE] ATR filter failed")

        return df


# ==========================================================
# AI 승인
# ==========================================================

def run_ai_entry_checks(
        df: pd.DataFrame,
        interval: int
) -> List[Dict]:

    approved: List[Dict] = []

    try:

        if df is None or df.empty:
            return approved

        for i, (_, row) in enumerate(df.iterrows()):

            raw = row.to_dict()

            raw["interval"] = interval
            raw["source"] = "SUMMARY_AI"

            ai = ai_final_entry_check(raw)

            # 先頭は強制通過（fallback）
            if not ai.get("allow") and i != 0:
                continue

            entry = build_entry_row(raw)

            if not entry:
                continue

            entry["side"] = raw.get(
                "entry_decision",
                "BUY"
            )

            entry["confidence"] = ai.get(
                "confidence",
                0.0
            )

            entry["created_at"] = pd.Timestamp.now()

            entry["immediate_entry"] = True

            approved.append(entry)

        return approved

    except Exception:

        logger.exception("[AI_PIPELINE] ai checks failed")

        return approved


# ==========================================================
# pending登録
# ==========================================================

def register_pending_entries(entries: List[Dict[str, Any]]) -> int:
    registered = 0

    try:
        if not entries:
            logger.info("[SUMMARY_ENTRY] pending registration skipped reason=no_entries")
            return registered

        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    continue

                symbol = _safe_symbol(entry)
                if not symbol:
                    logger.warning("[SUMMARY_ENTRY] pending skip reason=no_symbol")
                    continue

                entry["symbol"] = symbol
                entry["entry_type"] = entry.get("entry_type") or DEFAULT_ENTRY_TYPE
                entry["source"] = entry.get("source") or DEFAULT_SOURCE
                entry["side"] = _normalize_side(entry)
                entry["entry_decision"] = entry["side"]

                ok = add_pending(entry)

                if not ok:
                    logger.warning(
                        "[SUMMARY_ENTRY] pending rejected symbol=%s source=%s side=%s",
                        entry.get("symbol"),
                        entry.get("source"),
                        entry.get("side"),
                    )
                    continue

                registered += 1

                logger.info(
                    "[SUMMARY_ENTRY] pending added symbol=%s side=%s entry_type=%s confidence=%s",
                    entry.get("symbol"),
                    entry.get("side"),
                    entry.get("entry_type"),
                    entry.get("confidence"),
                )

            except Exception:
                logger.exception(
                    "[SUMMARY_ENTRY] pending add failed symbol=%s",
                    _safe_symbol(entry) if isinstance(entry, dict) else "",
                )

        logger.info(
            "[SUMMARY_ENTRY] pending registration done entries=%s registered=%s",
            len(entries),
            registered,
        )
        return registered

    except Exception:
        logger.exception("[SUMMARY_ENTRY] pending registration failed")
        return registered

# ==========================================================
# entry pipeline実行
# ==========================================================

def run_entry_if_needed(
        entries: List[Dict]
):

    try:

        if not entries:
            return

        run_entry_pipeline()

    except Exception:

        logger.exception(
            "[AI_PIPELINE] entry pipeline failed"
        )


# ==========================================================
# AI ENTRY PIPELINE
# ==========================================================

def run_summary_ai_pipeline(
        df_entry: pd.DataFrame,
        df_summary: pd.DataFrame,
        interval: int
) -> List[Dict]:

    """
    summary ranking → AI entry pipeline
    """

    approved: List[Dict] = []

    try:

        if df_entry is None or df_entry.empty:
            return approved

        # ------------------------------------------
        # ATR filter
        # ------------------------------------------

        df_ai = apply_atr_percentile_filter(
            df_entry.head(TOP_N),
            atr_col="atr_1m",
            drop_ratio=ATR_DROP_RATIO
        )

        if df_ai is None or df_ai.empty:

            df_ai = df_entry.head(1).copy()

        # ------------------------------------------
        # AI decision
        # ------------------------------------------

        approved = run_ai_entry_checks(
            df_ai,
            interval
        )

        # ------------------------------------------
        # pending登録
        # ------------------------------------------

        register_pending_entries(
            approved
        )

        # ------------------------------------------
        # entry実行
        # ------------------------------------------

        run_entry_if_needed(
            approved
        )

        # ------------------------------------------
        # summary vs entry 検証
        # ------------------------------------------

        verify_summary_vs_entry(
            df_summary,
            approved,
            interval
        )

        return approved

    except Exception:

        logger.exception(
            "[AI_PIPELINE] fatal"
        )

        return approved