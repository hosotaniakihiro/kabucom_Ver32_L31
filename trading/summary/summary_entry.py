# ==========================================================
# File   : trading/summary/summary_entry.py
# Version: Ver1.9-PRODUCTION-RETURN-EXECUTION-RESULT
# ----------------------------------------------------------
# ✔ summary entry実行責務
# ✔ approved_rows: list[dict] / DataFrame / Series / dict 両対応
# ✔ entry row build
# ✔ pending登録
# ✔ entry pipeline実行
# ✔ summary vs entry verification
# ✔ DataFrame truth-value ambiguous 対策
# ✔ 列名ループ事故対策
# ✔ NaN / None 防御
# ✔ entry_type="SUMMARY_AI" 明示
# ✔ entry_controller.py の ENTRY_TYPE_PRIORITY に対応
# ✔ pending登録の戻り値を厳密判定
# ✔ pending reject / duplicate を明示ログ化
# ✔ pending root snapshot 連携
# ✔ pipeline_source / interval を entry_controller に伝搬
# ✔ interval 正規化
# ✔ price/current_price/atr系の引き継ぎ強化
# ✔ ai_side を entry_decision より優先
# ✔ SELL候補をBUYに誤変換しない
# ✔ SUMMARY_AI SELL候補はSELLとしてpending化する
# ✔ Ver1.9: execute_entry_pipeline / run_summary_entry_executor が dict 結果を返す
# ✔ Ver1.9: 上位 summary_ai.executor の entry_pipeline_no_order 誤判定を防止
# ==========================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List

import pandas as pd

from AI.entry_row_builder import build_entry_row

from trading.entry.pending_manager import add_pending, snapshot_root
from trading.handlers.entry_controller import run_entry_pipeline
from trading.summary.summary_analysis_logger import verify_summary_vs_entry

logger = logging.getLogger(__name__)

DEFAULT_ENTRY_TYPE = "SUMMARY_AI"
DEFAULT_SOURCE = "SUMMARY"
DEFAULT_SIDE = "BUY"

_TRUE_VALUES = {"1", "true", "yes", "on", "y"}
_FALSE_VALUES = {"0", "false", "no", "off", "n"}


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE_VALUES:
            return True
        if s in _FALSE_VALUES:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _allow_summary_ai_sell_entry() -> bool:
    return _env_bool("SUMMARY_AI_ALLOW_SELL_ENTRY", True)


def _safe_dict(d: Any) -> Dict[str, Any]:
    try:
        if d is None:
            return {}
        if isinstance(d, dict):
            return dict(d)
        if isinstance(d, pd.Series):
            return d.to_dict()
        if hasattr(d, "to_dict"):
            v = d.to_dict()
            if isinstance(v, dict):
                return dict(v)
        return {}
    except Exception:
        logger.exception("[SUMMARY_ENTRY] _safe_dict failed")
        return {}


def _clean_nan_values(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        for k, v in dict(row).items():
            try:
                if pd.isna(v):
                    out[k] = None
                else:
                    out[k] = v
            except Exception:
                out[k] = v
    except Exception:
        logger.exception("[SUMMARY_ENTRY] _clean_nan_values failed")
        return row
    return out


def _norm_side_value(v: Any) -> str:
    try:
        side = str(v or "").strip().upper()
        return side if side in ("BUY", "SELL") else ""
    except Exception:
        return ""


def _normalize_entry_type(raw: Dict[str, Any]) -> str:
    try:
        entry_type = str(raw.get("entry_type") or "").strip()
        if entry_type:
            return entry_type
        source = str(raw.get("source") or "").strip().upper()
        if source == "EARLY_SCALP":
            return "EARLY_SCALP"
        if source == "TONOSAMA":
            return "TONOSAMA"
        if source == "RANKING":
            return "RANKING_5S"
        return DEFAULT_ENTRY_TYPE
    except Exception:
        return DEFAULT_ENTRY_TYPE


def _normalize_source(raw: Dict[str, Any]) -> str:
    try:
        source = str(raw.get("source") or "").strip().upper()
        if source:
            return source
        return DEFAULT_SOURCE
    except Exception:
        return DEFAULT_SOURCE


def _normalize_side(raw: Dict[str, Any]) -> str:
    try:
        for key in ("ai_side", "ai_entry_decision", "ai_decision", "entry_decision", "side", "decision"):
            side = _norm_side_value(raw.get(key))
            if side:
                return side
        return DEFAULT_SIDE
    except Exception:
        return DEFAULT_SIDE


def _safe_interval(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except Exception:
        return None


def normalize_approved_rows(approved_rows: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        if approved_rows is None:
            return rows
        if isinstance(approved_rows, pd.DataFrame):
            if approved_rows.empty:
                return rows
            records = approved_rows.to_dict(orient="records")
            for r in records:
                rr = _clean_nan_values(_safe_dict(r))
                if rr:
                    rows.append(rr)
            logger.info("[SUMMARY_ENTRY] normalize approved_rows DataFrame rows=%s normalized=%s", len(approved_rows), len(rows))
            return rows
        if isinstance(approved_rows, pd.Series):
            rr = _clean_nan_values(_safe_dict(approved_rows))
            if rr:
                rows.append(rr)
            return rows
        if isinstance(approved_rows, dict):
            rr = _clean_nan_values(_safe_dict(approved_rows))
            if rr:
                rows.append(rr)
            return rows
        if isinstance(approved_rows, (list, tuple)):
            for item in approved_rows:
                rr = _clean_nan_values(_safe_dict(item))
                if rr:
                    rows.append(rr)
            logger.info("[SUMMARY_ENTRY] normalize approved_rows sequence raw=%s normalized=%s", len(approved_rows), len(rows))
            return rows
        if isinstance(approved_rows, Iterable):
            for item in approved_rows:
                rr = _clean_nan_values(_safe_dict(item))
                if rr:
                    rows.append(rr)
            logger.info("[SUMMARY_ENTRY] normalize approved_rows iterable normalized=%s", len(rows))
            return rows
        return rows
    except Exception:
        logger.exception("[SUMMARY_ENTRY] normalize_approved_rows failed")
        return rows


def _rows_empty(rows: List[Dict[str, Any]]) -> bool:
    try:
        return not isinstance(rows, list) or len(rows) == 0
    except Exception:
        return True


def _safe_symbol(row: Dict[str, Any]) -> str:
    try:
        s = str(row.get("symbol") or "").strip()
        if s.endswith(".0"):
            ss = s[:-2]
            if ss.isdigit():
                return ss
        return s
    except Exception:
        return ""


def _copy_if_present(*, src: Dict[str, Any], dst: Dict[str, Any], keys: Iterable[str], overwrite: bool = False) -> None:
    try:
        for key in keys:
            if key not in src:
                continue
            if overwrite or key not in dst:
                dst[key] = src.get(key)
    except Exception:
        logger.debug("[SUMMARY_ENTRY] _copy_if_present failed", exc_info=True)


def _result_executed(result: Any) -> bool:
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            if bool(result.get("executed")):
                return True
            for key in ("executed_count", "approved_count", "order_count"):
                try:
                    if int(result.get(key) or 0) > 0:
                        return True
                except Exception:
                    pass
            for key in ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols"):
                v = result.get(key)
                if isinstance(v, (list, tuple, set, dict)):
                    if len(v) > 0:
                        return True
                elif v:
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return len(result) > 0
        return bool(result)
    except Exception:
        return False


def build_entry_rows(approved_rows: Any) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    try:
        rows = normalize_approved_rows(approved_rows)
        if _rows_empty(rows):
            logger.info("[SUMMARY_ENTRY] build_entry_rows skipped reason=no_approved_rows")
            return entries
        allow_sell_entry = _allow_summary_ai_sell_entry()

        for raw in rows:
            try:
                if not isinstance(raw, dict):
                    continue
                symbol = _safe_symbol(raw)
                if not symbol:
                    logger.warning("[SUMMARY_ENTRY] skip row reason=no_symbol keys=%s", sorted(raw.keys()))
                    continue

                raw["symbol"] = symbol
                raw["entry_type"] = _normalize_entry_type(raw)
                raw["source"] = _normalize_source(raw)
                side = _normalize_side(raw)
                raw["entry_decision"] = side
                raw["side"] = side
                raw["ai_side"] = raw.get("ai_side") or side
                raw["interval"] = _safe_interval(raw.get("interval"))

                if raw.get("entry_type") == DEFAULT_ENTRY_TYPE and side == "SELL" and not allow_sell_entry:
                    logger.info(
                        "[SUMMARY_ENTRY] skip summary AI SELL row by env symbol=%s side=%s ai_side=%s buy_score=%s sell_score=%s",
                        symbol,
                        side,
                        raw.get("ai_side"),
                        raw.get("score_buy", raw.get("buy_score")),
                        raw.get("score_sell", raw.get("sell_score")),
                    )
                    continue

                entry = build_entry_row(raw)
                if not entry:
                    logger.warning("[SUMMARY_ENTRY] build_entry_row returned empty symbol=%s keys=%s side=%s", symbol, sorted(raw.keys()), side)
                    continue
                if not isinstance(entry, dict):
                    logger.warning("[SUMMARY_ENTRY] build_entry_row returned non-dict symbol=%s type=%s", symbol, type(entry).__name__)
                    continue

                entry["symbol"] = _safe_symbol(entry) or symbol
                entry["entry_type"] = raw.get("entry_type") or DEFAULT_ENTRY_TYPE
                entry["side"] = side
                entry["entry_decision"] = side
                entry["ai_side"] = raw.get("ai_side") or side
                entry["confidence"] = raw.get("confidence", raw.get("ai_confidence", 0.0))
                entry["ai_confidence"] = raw.get("ai_confidence", raw.get("confidence", 0.0))
                entry["lot_multiplier"] = raw.get("lot_multiplier", entry.get("lot_multiplier", 1.0))
                entry["reason"] = raw.get("ai_reason", raw.get("reason", entry.get("reason", "")))
                entry["ai_reason"] = raw.get("ai_reason", raw.get("reason", entry.get("reason", "")))
                entry["model_used"] = raw.get("model_used", entry.get("model_used", ""))
                entry["source"] = raw.get("source", entry.get("source", DEFAULT_SOURCE))
                entry["interval"] = _safe_interval(raw.get("interval", entry.get("interval")))
                entry["created_at"] = pd.Timestamp.now()
                entry["immediate_entry"] = True

                _copy_if_present(
                    src=raw,
                    dst=entry,
                    keys=(
                        "score",
                        "score_total",
                        "final_score",
                        "buy_score",
                        "sell_score",
                        "score_buy",
                        "score_sell",
                        "turnover",
                        "close_price",
                        "price",
                        "current_price",
                        "datetime",
                        "ai_gate_allow",
                        "ai_confidence",
                        "ai_side",
                        "lot_multiplier",
                        "score_base",
                        "score_trend",
                        "score_momentum",
                        "score_velocity",
                        "score_penalty",
                        "atr",
                        "atr_1m",
                        "atr_5m",
                        "symbolname",
                        "source",
                        "interval",
                    ),
                    overwrite=False,
                )

                entries.append(entry)
                logger.info(
                    "[SUMMARY_ENTRY] entry row built symbol=%s side=%s ai_side=%s entry_type=%s confidence=%s source=%s interval=%s score_buy=%s score_sell=%s",
                    entry.get("symbol"),
                    entry.get("side"),
                    entry.get("ai_side"),
                    entry.get("entry_type"),
                    entry.get("confidence"),
                    entry.get("source"),
                    entry.get("interval"),
                    entry.get("score_buy", entry.get("buy_score")),
                    entry.get("score_sell", entry.get("sell_score")),
                )
            except Exception:
                logger.exception("[SUMMARY_ENTRY] build one entry failed raw=%s", raw)

        logger.info("[SUMMARY_ENTRY] build_entry_rows done approved=%s entries=%s allow_sell_entry=%s", len(rows), len(entries), allow_sell_entry)
        return entries
    except Exception:
        logger.exception("[SUMMARY_ENTRY] build_entry_rows failed")
        return entries


def register_pending_entries(entries: List[Dict[str, Any]]) -> int:
    registered = 0
    rejected = 0
    try:
        if not entries:
            logger.info("[SUMMARY_ENTRY] pending registration skipped reason=no_entries")
            return registered
        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    rejected += 1
                    continue
                symbol = _safe_symbol(entry)
                if not symbol:
                    rejected += 1
                    logger.warning("[SUMMARY_ENTRY] pending skip reason=no_symbol")
                    continue
                entry["symbol"] = symbol
                entry["entry_type"] = entry.get("entry_type") or DEFAULT_ENTRY_TYPE
                entry["source"] = entry.get("source") or DEFAULT_SOURCE
                side = _normalize_side(entry)
                entry["side"] = side
                entry["entry_decision"] = side
                entry["interval"] = _safe_interval(entry.get("interval"))

                logger.info(
                    "[SUMMARY_ENTRY] pending add request symbol=%s side=%s ai_side=%s entry_type=%s source=%s interval=%s",
                    entry.get("symbol"),
                    entry.get("side"),
                    entry.get("ai_side"),
                    entry.get("entry_type"),
                    entry.get("source"),
                    entry.get("interval"),
                )
                ok = add_pending(entry)
                if not ok:
                    rejected += 1
                    logger.warning(
                        "[SUMMARY_ENTRY] pending rejected symbol=%s side=%s entry_type=%s source=%s interval=%s",
                        entry.get("symbol"),
                        entry.get("side"),
                        entry.get("entry_type"),
                        entry.get("source"),
                        entry.get("interval"),
                    )
                    continue
                registered += 1
                logger.info("[SUMMARY_ENTRY] pending added symbol=%s side=%s entry_type=%s confidence=%s", entry.get("symbol"), entry.get("side"), entry.get("entry_type"), entry.get("confidence"))
            except Exception:
                rejected += 1
                logger.exception("[SUMMARY_ENTRY] pending add failed symbol=%s", _safe_symbol(entry) if isinstance(entry, dict) else "")

        logger.info("[SUMMARY_ENTRY] pending registration done entries=%s registered=%s rejected=%s root=%s", len(entries), registered, rejected, snapshot_root())
        return registered
    except Exception:
        logger.exception("[SUMMARY_ENTRY] pending registration failed")
        return registered


def execute_entry_pipeline(entries: List[Dict[str, Any]], *, pipeline_source: str | None = None, interval: int | None = None) -> Dict[str, Any]:
    try:
        if not entries:
            logger.info("[SUMMARY_ENTRY] entry pipeline skipped reason=no_entries")
            return {"executed": False, "skip_reason": "no_entries", "result": None}

        logger.info(
            "[SUMMARY_ENTRY] entry pipeline start entries=%s symbols=%s entry_types=%s pipeline_source=%s interval=%s root=%s",
            len(entries),
            [str(e.get("symbol")) for e in entries if isinstance(e, dict)][:20],
            [str(e.get("entry_type")) for e in entries if isinstance(e, dict)][:20],
            pipeline_source,
            interval,
            snapshot_root(),
        )

        result = run_entry_pipeline(pipeline_source=pipeline_source, interval=interval)
        executed = _result_executed(result)

        logger.info(
            "[SUMMARY_ENTRY] entry pipeline done entries=%s pipeline_source=%s interval=%s executed=%s result=%s root_after=%s",
            len(entries),
            pipeline_source,
            interval,
            executed,
            result,
            snapshot_root(),
        )

        return {
            "executed": executed,
            "entries": len(entries),
            "result": result,
            "pipeline_source": pipeline_source,
            "interval": interval,
            "skip_reason": None if executed else "entry_controller_no_order",
        }
    except Exception:
        logger.exception("[SUMMARY_ENTRY] entry pipeline failed")
        return {"executed": False, "skip_reason": "entry_pipeline_exception", "result": None}


def verify_summary_entries(df_summary: pd.DataFrame, entries: List[Dict[str, Any]], interval: int) -> bool:
    try:
        if not entries:
            logger.info("[SUMMARY_ENTRY] verification skipped reason=no_entries")
            return False
        verify_summary_vs_entry(df_summary, entries, interval)
        logger.info("[SUMMARY_ENTRY] verification done entries=%s interval=%s", len(entries), interval)
        return True
    except Exception:
        logger.exception("[SUMMARY_ENTRY] verification failed")
        return False


def run_summary_entry_executor(approved_rows: Any, df_summary: pd.DataFrame, interval: int) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    try:
        rows = normalize_approved_rows(approved_rows)
        if _rows_empty(rows):
            logger.info("[SUMMARY_ENTRY] executor skipped interval=%s reason=no_approved_rows", interval)
            return {
                "executed": False,
                "approved": 0,
                "entries": [],
                "registered": 0,
                "interval": interval,
                "skip_reason": "no_approved_rows",
            }

        logger.info("[SUMMARY_ENTRY] executor start interval=%s approved_rows=%s df_summary_rows=%s", interval, len(rows), len(df_summary) if isinstance(df_summary, pd.DataFrame) else 0)

        entries = build_entry_rows(rows)
        if not entries:
            logger.warning("[SUMMARY_ENTRY] executor stopped interval=%s reason=no_built_entries approved_rows=%s", interval, len(rows))
            return {
                "executed": False,
                "approved": len(rows),
                "entries": [],
                "registered": 0,
                "interval": interval,
                "skip_reason": "no_built_entries",
            }

        for entry in entries:
            if isinstance(entry, dict):
                if not entry.get("source"):
                    entry["source"] = DEFAULT_SOURCE
                if _safe_interval(entry.get("interval")) is None:
                    entry["interval"] = interval
                else:
                    entry["interval"] = _safe_interval(entry.get("interval"))

        registered = register_pending_entries(entries)
        if registered <= 0:
            logger.warning("[SUMMARY_ENTRY] executor stopped interval=%s reason=no_pending_registered entries=%s root=%s", interval, len(entries), snapshot_root())
            return {
                "executed": False,
                "approved": len(rows),
                "entries": entries,
                "registered": registered,
                "interval": interval,
                "skip_reason": "no_pending_registered",
            }

        pipeline_result = execute_entry_pipeline(entries, pipeline_source=DEFAULT_SOURCE, interval=interval)
        executed = _result_executed(pipeline_result)

        verify_summary_entries(df_summary, entries, interval)

        out = {
            "executed": executed,
            "approved": len(rows),
            "entries": entries,
            "registered": registered,
            "interval": interval,
            "pipeline_result": pipeline_result,
            "skip_reason": None if executed else "entry_controller_no_order",
        }

        logger.info(
            "[SUMMARY_ENTRY] executor done interval=%s approved=%s entries=%s registered=%s executed=%s root=%s result=%s",
            interval,
            len(rows),
            len(entries),
            registered,
            executed,
            snapshot_root(),
            out,
        )
        return out
    except Exception:
        logger.exception("[SUMMARY_ENTRY] fatal interval=%s", interval)
        return {
            "executed": False,
            "approved": 0,
            "entries": entries,
            "registered": 0,
            "interval": interval,
            "skip_reason": "summary_entry_exception",
        }
