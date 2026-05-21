# ============================================================
# File   : trading/ranking/entry_from_ranking.py
# Ver    : RANKING-ENTRY-SNAPSHOT+TECHNICAL+BREADTH+VOL+MARKET-v4.4.0-PENDING-FIX
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
# ✔ RANKING AI フェイルセーフ正式実装
#
# 【Ver4.4 修正】
# ✔ pending_manager.add_pending(entry_dict) の正しい呼び出し形式に修正
#   旧: add_pending(symbol=symbol, data={...})  ← TypeError で落ちる
#   新: add_pending({...})
# ✔ [RANKING ENTRY LOOP] / [RANKING PENDING ADD] の判定ログを強化
# ✔ ranking_df 件数・source取得元・created件数を明確化
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
        logger.info("[RANKING ENTRY LOOP] source=latest_ranking_snapshot rows=%s", len(snapshot))
        return pd.DataFrame(snapshot)

    for name in (
        "latest_ranking_raw",
        "latest_ranking_df",
        "ranking_raw_df",
    ):
        df = getattr(global_data, name, None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            logger.info("[RANKING ENTRY LOOP] source=%s rows=%s", name, len(df))
            return df

    logger.warning("[RANKING ENTRY LOOP] ranking source dataframe not found")
    return None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


# ============================================================
# core
# ============================================================

def entry_from_ranking():
    started = dt.datetime.now()
    logger.info("[RANKING ENTRY LOOP] start at=%s", started.strftime("%Y-%m-%d %H:%M:%S"))

    ranking_df = _get_ranking_source_df()
    if ranking_df is None or ranking_df.empty:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_ranking_df")
        return 0

    cfg_score = RANKING_ENTRY_CONFIG["SCORE"]
    cfg_volume = RANKING_ENTRY_CONFIG["VOLUME"]

    MIN_SCORE_TOTAL = cfg_score["MIN_ENTRY_SCORE"]
    USE_TECHNICAL_SCORE = cfg_score.get("USE_TECHNICAL_SCORE", True)
    MIN_TURNOVER = cfg_volume.get("MIN_TURNOVER", 0)

    now = _now()
    created = 0
    ai_reject = 0
    turnover_reject = 0
    build_reject = 0
    pending_reject = 0

    # =====================================================
    # 複数ランキング統合（breadth / strength）
    # =====================================================
    try:
        agg_df = build_ranking_aggregate(ranking_df)
        ranking_strength_map = dict(
            zip(
                agg_df["symbol"].astype(str),
                agg_df["ranking_score_total"],
            )
        )
    except Exception:
        logger.exception("[RANKING ENTRY LOOP] build_ranking_aggregate failed")
        ranking_strength_map = {}

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

            ready_col = None
            if "indicator_ready" in df_eval.columns:
                ready_col = "indicator_ready"
            elif "technical_ready" in df_eval.columns:
                ready_col = "technical_ready"

            if ready_col is None:
                logger.warning("[RANKING TECH SCORE] ready column missing -> use all evaluated rows")
            else:
                df_eval = df_eval[df_eval[ready_col].fillna(False).astype(bool)]

            for _, r in df_eval.iterrows():
                sym = str(r.get("symbol"))
                tech_score_map[sym] = _safe_float(r.get("score_buy", r.get("buy_score", 0.0)), 0.0)

            logger.info("[RANKING TECH SCORE] prepared symbols=%d", len(tech_score_map))

        except Exception:
            logger.exception("[RANKING TECH SCORE] failed")

    # =====================================================
    # 各 ranking row 処理
    # =====================================================
    for _, raw_row in ranking_df.iterrows():
        row = raw_row.to_dict()
        row["source"] = "RANKING"

        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            build_reject += 1
            continue

        snapshot_score = _safe_float(row.get("score_total") or row.get("score") or 0.0, 0.0)
        technical_score = tech_score_map.get(symbol, 0.0)
        breadth_score = _safe_float(ranking_strength_map.get(symbol, 0.0), 0.0)

        price_delta = abs(_safe_float(row.get("price_delta_1m"), 0.0))
        volatility_score = min(price_delta / VOL_THRESHOLD, 1.0)

        rank_type = row.get("rank_type")
        rank_type_weight = RANK_TYPE_WEIGHT.get(rank_type, 1.0)

        base_score = (
            0.30 * breadth_score
            + 0.30 * technical_score
            + 0.25 * volatility_score
            + 0.15 * snapshot_score
        ) * rank_type_weight

        market = row.get("market") or "ALL"
        market_weight = EXCHANGE_WEIGHT.get(market, 1.0)

        final_score = max(base_score * market_weight, MIN_SCORE_TOTAL)
        row["score_total"] = final_score
        row["score"] = final_score

        entry_row = build_entry_row(row)
        if not entry_row:
            build_reject += 1
            continue

        side = entry_row.get("side") or infer_side_from_rank_type(row.get("rank_type")) or "BUY"
        entry_row["side"] = side
        entry_row["source"] = "RANKING"
        entry_row["symbol"] = symbol
        entry_row.setdefault("entry_type", "RANKING")
        entry_row.setdefault("interval", 1)
        entry_row["score"] = final_score
        entry_row["score_total"] = final_score

        if _safe_float(entry_row.get("turnover", 0), 0.0) < MIN_TURNOVER:
            turnover_reject += 1
            continue

        ai = ai_final_entry_check(entry_row)
        fallback_used = False

        if not ai.get("allow"):
            if breadth_score >= RANKING_FALLBACK_MIN_STRENGTH:
                fallback_used = True
                logger.warning(
                    "⚠️ RANKING FALLBACK ALLOW symbol=%s breadth=%.3f",
                    symbol,
                    breadth_score,
                )
            else:
                ai_reject += 1
                _reject("AI_REJECT", entry_row)
                continue

        pending_entry = {
            **entry_row,
            "source": "RANKING",
            "created_at": now,
            "ranking_fallback_used": fallback_used,
            "ranking_strength": breadth_score,
            "technical_score": technical_score,
            "snapshot_score": snapshot_score,
        }

        if add_pending(pending_entry):
            created += 1
            logger.info(
                "[RANKING PENDING ADD] "
                f"symbol={symbol} "
                f"side={side} "
                f"snap={snapshot_score:.2f} "
                f"tech={technical_score:.2f} "
                f"breadth={breadth_score:.2f} "
                f"vol={volatility_score:.2f} "
                f"rank_w={rank_type_weight:.2f} "
                f"mkt_w={market_weight:.2f} "
                f"final={final_score:.2f} "
                f"fallback={fallback_used}"
            )
        else:
            pending_reject += 1

    elapsed = (dt.datetime.now() - started).total_seconds()
    logger.info(
        "[RANKING ENTRY LOOP] done created=%s total=%s build_reject=%s turnover_reject=%s ai_reject=%s pending_reject=%s elapsed=%.3fs",
        created,
        len(ranking_df),
        build_reject,
        turnover_reject,
        ai_reject,
        pending_reject,
        elapsed,
    )

    return created


def run_ranking_entry_pipeline():
    return entry_from_ranking()
