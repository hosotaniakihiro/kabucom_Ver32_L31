# ============================================================
# File   : trading/ranking/entry_from_ranking.py
# Ver    : RANKING-ENTRY-SNAPSHOT+TECHNICAL+BREADTH+VOL+MARKET-v4.3.0-FINAL
# ------------------------------------------------------------
# ✔ ranking_snapshot / ranking_raw → pending 生成の唯一の入口
# ✔ snapshot / technical / breadth / volatility / market の完全HYBRID
# ✔ SUMMARY と同等のテクニカル評価を RANKING に導入
# ✔ 複数ランキング同時出現（breadth）を正式評価
# ✔ 変動率（price_delta_1m）を明示的に加点
# ✔ 市場区分（TP/TS/TG）による最終補正
# ✔ pending_manager 以外を直接操作しない
# ✔ AI 最終判断は既存設計を完全維持
# ✔ price 欠損 / indicator_ready 欠損 完全耐性
# ✔ ★ RANKING AI フェイルセーフ（②）正式実装
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from typing import Dict, Any, Optional

import pandas as pd

from global_state import global_data
from trading.entry.pending_manager import add_pending
from trading.ranking.side_infer import infer_side_from_rank_type
from AI.entry_gate import ai_final_entry_check
from AI.entry_row_builder import build_entry_row
from config.ranking_entry_config import RANKING_ENTRY_CONFIG

# ranking → summary → technical
from trading.ranking.ranking_summary_adapter import (
    build_ranking_like_summary_1min,
)
from trading.summary.calculator import calculate_summary

# 複数ランキング統合（breadth / strength）
from trading.ranking.ranking_aggregate_builder import (
    build_ranking_aggregate,
)

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

VOL_THRESHOLD = 0.01  # 1分変動率 1%

# ★ フェイルセーフ用（②）
RANKING_FALLBACK_MIN_STRENGTH = 0.80

RANK_TYPE_WEIGHT = {
    "値上がり率": 1.0,
    "値下がり率": 1.0,
    "売買高上位": 0.7,
    "売買代金": 0.8,
    "TICK回数": 0.6,
    "売買高急増": 1.3,
    "売買代金急増": 1.4,
}

EXCHANGE_WEIGHT = {
    "TP": 1.05,
    "TS": 1.00,
    "TG": 1.10,
    "ALL": 1.00,
}

# ============================================================
# util
# ============================================================

def _now():
    return dt.datetime.now()


def _reject(reason: str, row: Dict[str, Any]):
    logger.info(
        "[RANKING DROP] "
        f"symbol={row.get('symbol')} "
        f"side={row.get('side')} "
        f"score_total={row.get('score_total')} "
        f"reason={reason}"
    )


def _get_ranking_source_df() -> Optional[pd.DataFrame]:
    snapshot = getattr(global_data, "latest_ranking_snapshot", None)
    if isinstance(snapshot, list) and snapshot:
        return pd.DataFrame(snapshot)

    for name in (
        "latest_ranking_raw",
        "latest_ranking_df",
        "ranking_raw_df",
    ):
        df = getattr(global_data, name, None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df

    return None


# ============================================================
# core
# ============================================================

def entry_from_ranking():

    logger.info("[RANKING ENTRY PIPELINE] START")

    ranking_df = _get_ranking_source_df()
    if ranking_df is None or ranking_df.empty:
        return 0

    cfg_score = RANKING_ENTRY_CONFIG["SCORE"]
    cfg_volume = RANKING_ENTRY_CONFIG["VOLUME"]

    MIN_SCORE_TOTAL = cfg_score["MIN_ENTRY_SCORE"]
    USE_TECHNICAL_SCORE = cfg_score.get("USE_TECHNICAL_SCORE", True)
    MIN_TURNOVER = cfg_volume.get("MIN_TURNOVER", 0)

    now = _now()
    created = 0

    # =====================================================
    # A️⃣ 複数ランキング統合（breadth / strength）
    # =====================================================
    agg_df = build_ranking_aggregate(ranking_df)

    ranking_strength_map = dict(
        zip(
            agg_df["symbol"],
            agg_df["ranking_score_total"],
        )
    )

    # =====================================================
    # TECHNICAL SCORE（SUMMARY 同等）
    # =====================================================
    tech_score_map: Dict[str, float] = {}

    if USE_TECHNICAL_SCORE:
        try:
            symbols = ranking_df["symbol"].astype(str).unique().tolist()

            df_rank_summary = build_ranking_like_summary_1min(
                symbols=symbols,
                end_time=now,
                bars=80,
            )

            # price 補完
            if (
                "price" not in df_rank_summary.columns
                and "close_price" in df_rank_summary.columns
            ):
                df_rank_summary["price"] = df_rank_summary["close_price"]

            df_eval = calculate_summary(
                df_push=df_rank_summary,
                df_summary=None,
                symbols=symbols,
                start_time=None,
                end_time=now,
            )

            if "indicator_ready" not in df_eval.columns:
                logger.warning(
                    "[RANKING TECH SCORE] indicator_ready missing → skip"
                )
                df_eval = pd.DataFrame()
            else:
                df_eval = df_eval[df_eval["indicator_ready"]]

            for _, r in df_eval.iterrows():
                tech_score_map[str(r["symbol"])] = float(
                    r.get("score_buy", 0.0)
                )

            logger.info(
                "[RANKING TECH SCORE] prepared symbols=%d",
                len(tech_score_map),
            )

        except Exception:
            logger.exception("[RANKING TECH SCORE] failed")

    # =====================================================
    # 各 ranking row 処理
    # =====================================================
    for _, raw_row in ranking_df.iterrows():
        row = raw_row.to_dict()
        row["source"] = "RANKING"

        symbol = str(row.get("symbol"))

        snapshot_score = float(
            row.get("score_total") or row.get("score") or 0.0
        )
        technical_score = tech_score_map.get(symbol, 0.0)
        breadth_score = ranking_strength_map.get(symbol, 0.0)

        price_delta = abs(row.get("price_delta_1m") or 0.0)
        volatility_score = min(price_delta / VOL_THRESHOLD, 1.0)

        rank_type = row.get("rank_type")
        rank_type_weight = RANK_TYPE_WEIGHT.get(rank_type, 1.0)

        base_score = (
            0.30 * breadth_score +
            0.30 * technical_score +
            0.25 * volatility_score +
            0.15 * snapshot_score
        ) * rank_type_weight

        market = row.get("market") or "ALL"
        market_weight = EXCHANGE_WEIGHT.get(market, 1.0)

        final_score = max(
            base_score * market_weight,
            MIN_SCORE_TOTAL,
        )

        row["score_total"] = final_score

        entry_row = build_entry_row(row)
        if not entry_row:
            continue

        side = entry_row.get("side") or infer_side_from_rank_type(
            row.get("rank_type")
        ) or "BUY"
        entry_row["side"] = side

        if entry_row.get("turnover", 0) < MIN_TURNOVER:
            continue

        # =================================================
        # AI 最終判断 + フェイルセーフ（②）
        # =================================================
        ai = ai_final_entry_check(entry_row)

        fallback_used = False

        if not ai.get("allow"):
            # ★ フェイルセーフ条件
            if breadth_score >= RANKING_FALLBACK_MIN_STRENGTH:
                fallback_used = True
                logger.warning(
                    "⚠️ RANKING FALLBACK ALLOW "
                    f"symbol={symbol} breadth={breadth_score:.3f}"
                )
            else:
                _reject("AI_REJECT", entry_row)
                continue

        add_pending(
            symbol=symbol,
            data={
                **entry_row,
                "source": "RANKING",
                "created_at": now,
                # --- 学習・検証用 ---
                "ranking_fallback_used": fallback_used,
                "ranking_strength": breadth_score,
                "technical_score": technical_score,
                "snapshot_score": snapshot_score,
            },
        )

        created += 1

        logger.info(
            "[RANKING PENDING ADD] "
            f"symbol={symbol} "
            f"snap={snapshot_score:.2f} "
            f"tech={technical_score:.2f} "
            f"breadth={breadth_score:.2f} "
            f"vol={volatility_score:.2f} "
            f"rank_w={rank_type_weight:.2f} "
            f"mkt_w={market_weight:.2f} "
            f"final={final_score:.2f} "
            f"fallback={fallback_used}"
        )

    logger.info(
        "[RANKING ENTRY PIPELINE] END "
        f"created={created} total={len(ranking_df)}"
    )

    return created


def run_ranking_entry_pipeline():
    return entry_from_ranking()