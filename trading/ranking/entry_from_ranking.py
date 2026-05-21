# ============================================================
# File   : trading/ranking/entry_from_ranking.py
# Ver    : RANKING-ENTRY-SNAPSHOT+TECHNICAL+BREADTH+VOL+MARKET-v4.5.0-TURNOVER-FIX
# ------------------------------------------------------------
# ✔ ranking_snapshot / ranking_raw → pending 生成の唯一の入口
# ✔ snapshot / technical / breadth / volatility / market の完全HYBRID
# ✔ SUMMARY と同等のテクニカル評価を RANKING に導入
# ✔ pending_manager.add_pending(entry_dict) の正しい呼び出し形式
# ✔ [RANKING ENTRY LOOP] / [RANKING PENDING ADD] の判定ログを強化
#
# 【Ver4.5 修正】
# ✔ ランキング行の価格・出来高・売買代金を entry_row 生成前に正規化
# ✔ turnover 欠損時は price × volume で補完
# ✔ turnover_reject=全件 になる原因を潰す
# ✔ turnover_reject の先頭サンプルをログ出力
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

from trading.ranking.ranking_summary_adapter import build_ranking_like_summary_1min
from trading.summary.calculator import calculate_summary
from trading.ranking.ranking_aggregate_builder import build_ranking_aggregate

logger = logging.getLogger(__name__)


VOL_THRESHOLD = 0.01
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


def _now():
    return dt.datetime.now()


def _reject(reason: str, row: Dict[str, Any]):
    logger.info(
        "[RANKING DROP] symbol=%s side=%s score_total=%s reason=%s",
        row.get("symbol"),
        row.get("side"),
        row.get("score_total"),
        reason,
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
            return df.copy()

    logger.warning("[RANKING ENTRY LOOP] ranking source dataframe not found")
    return None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s == "" or s.lower() == "nan":
            return default
        return float(s.replace(",", ""))
    except Exception:
        return default


def _first(row: Dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            if k in row:
                v = row.get(k)
                if v is not None and str(v).strip() != "":
                    return v
        except Exception:
            pass
    return default


def _normalize_ranking_row_for_entry(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    ranking_snapshot / ranking_raw のカラム名揺れを entry_row_builder が読める形に正規化する。
    """
    row = dict(row)

    price = _safe_float(
        _first(
            row,
            (
                "close_price",
                "current_price",
                "CurrentPrice",
                "price",
                "close",
                "現在値",
            ),
            0.0,
        ),
        0.0,
    )

    volume = _safe_float(
        _first(
            row,
            (
                "volume",
                "trading_volume",
                "TradingVolume",
                "出来高",
                "売買高",
            ),
            0.0,
        ),
        0.0,
    )

    turnover = _safe_float(
        _first(
            row,
            (
                "turnover",
                "trading_value",
                "TradingValue",
                "売買代金",
                "売買代金上位",
                "value",
                "Value",
            ),
            0.0,
        ),
        0.0,
    )

    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume

    if price > 0:
        for k in ("close_price", "current_price", "price", "close"):
            row[k] = price

    if volume > 0:
        row["volume"] = volume
        row["trading_volume"] = volume

    if turnover > 0:
        row["turnover"] = turnover
        row["trading_value"] = turnover

    # datetime も揺れを吸収
    if not row.get("datetime"):
        row["datetime"] = row.get("snapshot_time") or row.get("time") or row.get("created_at")

    return row


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
    MIN_TURNOVER = _safe_float(cfg_volume.get("MIN_TURNOVER", 0), 0.0)

    now = _now()
    created = 0
    ai_reject = 0
    turnover_reject = 0
    build_reject = 0
    pending_reject = 0
    turnover_reject_samples: list[dict[str, Any]] = []

    try:
        agg_df = build_ranking_aggregate(ranking_df)
        ranking_strength_map = dict(
            zip(agg_df["symbol"].astype(str), agg_df["ranking_score_total"])
        )
    except Exception:
        logger.exception("[RANKING ENTRY LOOP] build_ranking_aggregate failed")
        ranking_strength_map = {}

    tech_score_map: Dict[str, float] = {}

    if USE_TECHNICAL_SCORE:
        try:
            symbols = ranking_df["symbol"].astype(str).unique().tolist()

            df_rank_summary = build_ranking_like_summary_1min(
                symbols=symbols,
                end_time=now,
                bars=80,
            )

            if "price" not in df_rank_summary.columns and "close_price" in df_rank_summary.columns:
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

    for _, raw_row in ranking_df.iterrows():
        row = _normalize_ranking_row_for_entry(raw_row.to_dict())
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

        turnover = _safe_float(entry_row.get("turnover", 0), 0.0)
        if turnover < MIN_TURNOVER:
            turnover_reject += 1
            if len(turnover_reject_samples) < 10:
                turnover_reject_samples.append(
                    {
                        "symbol": symbol,
                        "price": entry_row.get("price"),
                        "volume": entry_row.get("volume"),
                        "turnover": turnover,
                        "min_turnover": MIN_TURNOVER,
                        "raw_keys": sorted(list(row.keys()))[:40],
                    }
                )
            continue

        ai = ai_final_entry_check(entry_row)
        fallback_used = False

        if not ai.get("allow"):
            if breadth_score >= RANKING_FALLBACK_MIN_STRENGTH:
                fallback_used = True
                logger.warning("⚠️ RANKING FALLBACK ALLOW symbol=%s breadth=%.3f", symbol, breadth_score)
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
                "[RANKING PENDING ADD] symbol=%s side=%s snap=%.2f tech=%.2f breadth=%.2f vol=%.2f rank_w=%.2f mkt_w=%.2f final=%.2f turnover=%.0f fallback=%s",
                symbol,
                side,
                snapshot_score,
                technical_score,
                breadth_score,
                volatility_score,
                rank_type_weight,
                market_weight,
                final_score,
                turnover,
                fallback_used,
            )
        else:
            pending_reject += 1

    elapsed = (dt.datetime.now() - started).total_seconds()
    if turnover_reject_samples:
        logger.warning("[RANKING ENTRY LOOP] turnover_reject_samples=%s", turnover_reject_samples)

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
