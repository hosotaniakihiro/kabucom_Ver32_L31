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
from typing import Any, Dict, Iterable, List, Tuple

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


# ------------------------------------------------------------
# 旧 core/startup/summary_ai_entry_controller_bridge_patch.py の
# _strict_order_executed をここへ本文化 (V1.5)。
# 旧パッチは install 時にこの関数をまるごと差し替えていた (合成ではない)。
# approved/registered/entries や executed フラグだけでは成功扱いせず、
# 実注文の根拠 (件数キー / order 系キー) が取れたときだけ True を返す。
# ------------------------------------------------------------
_STRICT_ORDER_KEYS = ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols")
_STRICT_EXECUTED_COUNT_KEYS = ("executed_count", "order_count", "submitted_count", "sent_count")


def _strict_has_payload(v: Any) -> bool:
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) > 0
    return bool(v)


def _result_executed(result: Any) -> bool:
    """実注文が確認できた場合だけ True。approved/registered/entries/pending減少だけでは成功扱いしない。"""
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            # 既存の真偽戻り値は尊重。ただし wrapper が作った dict の executed は下で order根拠付きだけを見る。
            return bool(result)
        if isinstance(result, dict):
            for key in _STRICT_EXECUTED_COUNT_KEYS:
                try:
                    if int(result.get(key) or 0) > 0:
                        return True
                except Exception:
                    pass
            for key in _STRICT_ORDER_KEYS:
                if _strict_has_payload(result.get(key)):
                    return True
            for key in ("result", "pipeline_result", "order_result"):
                child = result.get(key)
                if child is not result and _result_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_result_executed(x) for x in result)
        return False
    except Exception:
        logger.exception("[SUMMARY AI ENTRY BRIDGE] strict order result judgement failed result=%s", result)
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


def _register_pending_entries_core(entries: List[Dict[str, Any]]) -> int:
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


# ============================================================
# register_pending_entries の2層ガード
# ------------------------------------------------------------
# 旧 core/startup の2パッチを本文化。実行時の合成は
#   native <- summary_entry_pending_existing_fix <- summary_ai_direct_timeout_continue
# の2層 (実測)。3件目の summary_entry_pending_duplicate_registered_patch は
# direct_timeout_continue に剥がされる死に層だったので f2276d1 で削除済み。
#
#   1層目 _pef_*: 既存 pending の refresh / stale 判定 (fresh dispatch 救済込み)
#   2層目 _dtc_*: timeout した内側スレッドが遅れて登録しても期限切れなら拒否
#
# スコープは旧 install 導線 (いずれも main.py 経由) を再現する。
# ============================================================

import datetime as dt
import functools
import time

_PEF_VERSION = "V3-SUMMARY-ENTRY-PENDING-EXISTING-FIX"
_DTC_VERSION = "V15-SUMMARY-AI-DIRECT-TIMEOUT-CONTINUE"
_RPE_SCOPE = None
# 旧パッチ内で誰も書き込まなくなったが、判定の形を変えないため残す。
_DTC_FRESHNESS_BY_SYMBOL: dict = {}

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}
_STALE_RESCUE_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_STALE_RESCUE_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}

def _pef_env_bool(name: str, default: bool) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_SET:
            return True
        if s in _FALSE_SET:
            return False
    except Exception:
        pass
    return bool(default)


def _pef_env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


def _pef_norm(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip().upper()
    except Exception:
        return ""


def _pef_norm_interval(v: Any) -> str:
    try:
        if v is None or v == "":
            return ""
        s = str(v).strip()
        return s[:-2] if s.endswith(".0") else s
    except Exception:
        return ""


def _pef_norm_side(v: Any) -> str:
    s = _pef_norm(v)
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _pef_identity(e: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        _pef_norm(e.get("source")),
        _pef_norm(e.get("entry_type")),
        _pef_norm_side(e.get("side") or e.get("entry_decision") or e.get("ai_side")),
        _pef_norm_interval(e.get("interval")),
    )


def _pef_symbol(e: Dict[str, Any]) -> str:
    s = str(e.get("symbol") or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _pef_stamp(entry: Dict[str, Any]) -> None:
    try:
        now = dt.datetime.now()
        entry["created_at"] = entry.get("created_at") or now
        entry["pending_refreshed_at"] = now
        entry["pending_duplicate_refreshed"] = True
    except Exception:
        pass


def _pef_as_datetime(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    try:
        # pandas.Timestamp / datetime.datetime compatible path
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=None) if value.tzinfo is not None else value
        if isinstance(value, dt.date):
            return dt.datetime.combine(value, dt.time.min)
    except Exception:
        pass

    try:
        s = str(value).strip()
        if not s or s.lower() in {"nat", "nan", "none", "null"}:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(s)
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except Exception:
        pass

    # Last resort for common pandas-like strings with slash date or seconds suffix.
    try:
        import pandas as pd  # type: ignore

        parsed = pd.to_datetime(value, errors="coerce")
        if parsed is None or getattr(parsed, "tzinfo", None) is None and str(parsed) == "NaT":
            return None
        if hasattr(parsed, "to_pydatetime"):
            parsed = parsed.to_pydatetime()
        if isinstance(parsed, dt.datetime):
            return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except Exception:
        pass

    return None


def _pef_entry_datetime(entry: Dict[str, Any]) -> dt.datetime | None:
    for key in (
        "datetime",
        "summary_datetime",
        "entry_datetime",
        "signal_datetime",
        "latest_dt",
        "latest_datetime",
        "end_time",
        "timestamp",
    ):
        try:
            parsed = _pef_as_datetime(entry.get(key))
            if parsed is not None:
                return parsed
        except Exception:
            continue
    return None


def _pef_summary_max_age_sec() -> float:
    # エントリー最終ガードのデフォルトと合わせる。0以下なら無効化。
    for name in (
        "SUMMARY_ENTRY_PENDING_MAX_AGE_SEC",
        "SUMMARY_AI_PENDING_MAX_AGE_SEC",
        "SUMMARY_AI_MAX_CANDIDATE_AGE_SEC",
        "ENTRY_CANDIDATE_MAX_AGE_SEC",
    ):
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return _pef_env_float(name, 180.0)
    return 180.0


def _pef_is_summary_ai_entry(entry: Dict[str, Any]) -> bool:
    source = _pef_norm(entry.get("source"))
    entry_type = _pef_norm(entry.get("entry_type"))
    if source in {"SUMMARY", "SUMMARY_AI", "PUSH", "AI"} and "SUMMARY" in entry_type:
        return True
    if entry_type in {"SUMMARY_AI", "SUMMARY", "AI_SUMMARY"}:
        return True
    return False


def _pef_stale_summary_reason_base(entry: Dict[str, Any]) -> str | None:
    if not _pef_env_bool("SUMMARY_SKIP_STALE_PENDING_ENABLED", True):
        return None
    if not _pef_is_summary_ai_entry(entry):
        return None

    max_age = _pef_summary_max_age_sec()
    if max_age <= 0:
        return None

    signal_dt = _pef_entry_datetime(entry)
    if signal_dt is None:
        if _pef_env_bool("SUMMARY_SKIP_MISSING_DATETIME_PENDING", True):
            return f"missing_datetime max_age={max_age:.1f}s"
        return None

    age = (dt.datetime.now() - signal_dt).total_seconds()
    if age > max_age:
        return f"stale age={age:.1f}s max_age={max_age:.1f}s datetime={signal_dt}"
    if age < -60:
        return f"future_datetime age={age:.1f}s datetime={signal_dt}"
    return None


def _pef_same_pending_rows(symbol: str, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from trading.entry.pending_manager import get_bucket

        target = _pef_identity(entry)
        return [old for old in get_bucket(symbol) if isinstance(old, dict) and _pef_identity(old) == target]
    except Exception:
        logger.debug("[SUMMARY ENTRY PENDING FIX] same pending rows check failed", exc_info=True)
        return []


def _pef_refresh_same_pending(symbol: str, entry: Dict[str, Any]) -> bool:
    """
    add_pending() が同一 identity duplicate で False を返した場合、
    古いpendingを最新entryで置換する。
    """
    if not _pef_env_bool("SUMMARY_PENDING_DUPLICATE_REFRESH_ENABLED", True):
        return False
    try:
        stale_reason = _pef_stale_summary_reason(entry)
        if stale_reason:
            logger.warning(
                "[SUMMARY ENTRY PENDING FIX] duplicate refresh skipped stale symbol=%s identity=%s reason=%s score=%s",
                symbol,
                _pef_identity(entry),
                stale_reason,
                entry.get("score", entry.get("final_score")),
            )
            return False

        from trading.entry.pending_manager import get_bucket, replace_bucket

        target = _pef_identity(entry)
        bucket = get_bucket(symbol)
        if not any(isinstance(old, dict) and _pef_identity(old) == target for old in bucket):
            return False

        kept: List[Dict[str, Any]] = []
        removed = 0
        old_samples: List[Dict[str, Any]] = []
        for old in bucket:
            if isinstance(old, dict) and _pef_identity(old) == target:
                removed += 1
                if len(old_samples) < 3:
                    old_samples.append({
                        "score": old.get("score"),
                        "score_buy": old.get("score_buy"),
                        "score_sell": old.get("score_sell"),
                        "side": old.get("side"),
                        "datetime": old.get("datetime"),
                        "created_at": old.get("created_at"),
                    })
                continue
            kept.append(old)

        _pef_stamp(entry)
        replace_bucket(symbol, kept + [entry])
        logger.warning(
            "[SUMMARY ENTRY PENDING FIX] duplicate pending refreshed symbol=%s identity=%s removed=%s old_samples=%s new_score=%s new_side=%s new_datetime=%s",
            symbol,
            target,
            removed,
            old_samples,
            entry.get("score", entry.get("final_score")),
            entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"),
            entry.get("datetime"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY ENTRY PENDING FIX] duplicate refresh failed symbol=%s identity=%s", symbol, _pef_identity(entry))
        return False


def _pef_patched_register_pending_entries(entries: List[Dict[str, Any]]) -> int:
    try:
        from trading.entry.pending_manager import add_pending, snapshot_root

        registered = 0
        rejected = 0
        refreshed = 0
        existing = 0
        stale_skipped = 0

        if not entries:
            logger.info("[SUMMARY ENTRY PENDING FIX] pending registration skipped reason=no_entries")
            return 0

        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    rejected += 1
                    continue
                sym = _pef_symbol(entry)
                if not sym:
                    rejected += 1
                    logger.warning("[SUMMARY ENTRY PENDING FIX] pending skip reason=no_symbol entry=%s", entry)
                    continue
                entry["symbol"] = sym

                stale_reason = _pef_stale_summary_reason(entry)
                if stale_reason:
                    stale_skipped += 1
                    logger.warning(
                        "[SUMMARY ENTRY PENDING FIX] stale pending skipped symbol=%s identity=%s reason=%s score=%s side=%s",
                        sym,
                        _pef_identity(entry),
                        stale_reason,
                        entry.get("score", entry.get("final_score")),
                        entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"),
                    )
                    continue

                ok = bool(add_pending(entry))
                if ok:
                    registered += 1
                    continue

                same_rows = _pef_same_pending_rows(sym, entry)
                if same_rows:
                    existing += 1
                    if _pef_refresh_same_pending(sym, entry):
                        refreshed += 1
                        registered += 1
                    else:
                        # stale refreshなどで置換しなかった場合も、既存pendingはあるので
                        # no_pending_registered 誤判定だけは避ける。ただし新規staleは上で除外済み。
                        registered += 1
                    logger.warning(
                        "[SUMMARY ENTRY PENDING FIX] duplicate existing pending treated as registered symbol=%s identity=%s refreshed=%s",
                        sym,
                        _pef_identity(entry),
                        bool(same_rows),
                    )
                    continue

                rejected += 1
                logger.warning(
                    "[SUMMARY ENTRY PENDING FIX] pending rejected symbol=%s identity=%s entry=%s",
                    sym,
                    _pef_identity(entry),
                    entry,
                )
            except Exception:
                rejected += 1
                logger.exception("[SUMMARY ENTRY PENDING FIX] pending add failed entry=%s", entry)

        logger.warning(
            "[SUMMARY ENTRY PENDING FIX] pending registration done entries=%s registered=%s existing=%s refreshed=%s stale_skipped=%s rejected=%s root=%s",
            len(entries),
            registered,
            existing,
            refreshed,
            stale_skipped,
            rejected,
            snapshot_root(),
        )
        return registered
    except Exception as e:
        logger.exception("[SUMMARY ENTRY PENDING FIX] patched register failed err=%s", e)
        if callable(_register_pending_entries_core):
            return int(_register_pending_entries_core(entries) or 0)
        return 0


def _pef_stale_rescue_env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _STALE_RESCUE_TRUE:
            return True
        if s in _STALE_RESCUE_FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _pef_stale_rescue_env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


def _pef_stale_rescue_as_dt(v: Any) -> dt.datetime | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        if hasattr(v, "to_pydatetime"):
            v = v.to_pydatetime()
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        s = str(v).strip()
        if s.lower() in {"nat", "nan", "none", "null"}:
            return None
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        try:
            import pandas as pd  # type: ignore
            p = pd.to_datetime(v, errors="coerce")
            if str(p) == "NaT":
                return None
            if hasattr(p, "to_pydatetime"):
                p = p.to_pydatetime()
            if isinstance(p, dt.datetime):
                return p.replace(tzinfo=None)
        except Exception:
            pass
    except Exception:
        pass
    return None


def _pef_stale_rescue_fresh_runtime_age(entry: dict[str, Any]) -> tuple[float | None, str]:
    for key in ("pending_refreshed_at", "created_at", "queued_at", "approved_at", "built_at", "updated_at"):
        ts = _pef_stale_rescue_as_dt(entry.get(key))
        if ts is None:
            continue
        try:
            return max(0.0, (dt.datetime.now() - ts).total_seconds()), key
        except Exception:
            continue
    return None, ""


def _pef_stale_summary_reason(entry: Dict[str, Any]) -> str | None:
    try:
        if not _pef_stale_rescue_env_bool("SUMMARY_ENTRY_STALE_RESCUE_ENABLED", True):
            return _pef_stale_summary_reason_base(entry)

        is_summary = False
        try:
            fn = globals().get("_pef_is_summary_ai_entry")
            is_summary = bool(fn(entry)) if callable(fn) else False
        except Exception:
            is_summary = False

        if is_summary:
            runtime_age, runtime_key = _pef_stale_rescue_fresh_runtime_age(entry)
            fresh_limit = _pef_stale_rescue_env_float("SUMMARY_ENTRY_FRESH_DISPATCH_MAX_AGE_SEC", 45.0)
            if runtime_age is not None and runtime_age <= fresh_limit:
                _identity_fn = globals().get("_pef_identity")
                logger.warning(
                    "[SUMMARY ENTRY STALE RESCUE] allow fresh runtime candidate symbol=%s identity=%s runtime_key=%s runtime_age=%.1fs limit=%.1fs bar_datetime=%s score=%s side=%s",
                    entry.get("symbol"),
                    _identity_fn(entry) if callable(_identity_fn) else "",
                    runtime_key,
                    runtime_age,
                    fresh_limit,
                    entry.get("datetime"),
                    entry.get("score", entry.get("final_score")),
                    entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"),
                )
                return None

        return _pef_stale_summary_reason_base(entry)
    except Exception:
        logger.debug("[SUMMARY ENTRY STALE RESCUE] rescued stale check failed", exc_info=True)
        return _pef_stale_summary_reason_base(entry)


def _rpe_scope_enabled() -> bool:
    global _RPE_SCOPE
    if _RPE_SCOPE is not None:
        return _RPE_SCOPE
    enabled = False
    try:
        import sys as _sys

        argv_text = " ".join(str(x) for x in _sys.argv).replace("\\", "/").lower()
        db_names = (
            "main_database.py", "data_collectors_runner.py", "db_prepare_runner.py",
            "push_receiver_runner.py", "yahoo_complement_runner.py",
            "summary_database_runner.py", "ranking_collector_runner.py",
        )
        if not any(n in argv_text for n in db_names):
            enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _RPE_SCOPE = enabled
    return enabled


def register_pending_entries(entries: List[Dict[str, Any]], *args: Any, **kwargs: Any) -> int:
    if not _rpe_scope_enabled():
        return _register_pending_entries_core(entries)

    def _layer1(ents, *a, **k):
        return _pef_patched_register_pending_entries(ents)

    try:
        if not entries:
            return _layer1(entries, *args, **kwargs)
        now = time.time()
        kept = []
        skipped = []
        for entry in list(entries or []):
            try:
                if not isinstance(entry, dict):
                    kept.append(entry)
                    continue
                sym = str(entry.get("symbol") or "").strip()
                meta = _DTC_FRESHNESS_BY_SYMBOL.get(sym) if sym else None
                valid_until = entry.get("summary_ai_valid_until_ts")
                if valid_until is None and meta:
                    valid_until = meta.get("valid_until_ts")
                if valid_until is not None and now > float(valid_until):
                    skipped.append(sym or "?")
                    continue
                kept.append(entry)
            except Exception:
                kept.append(entry)
        if skipped:
            logger.warning(
                "[SUMMARY AI DIRECT FRESHNESS] pending skip expired symbols=%s kept=%s skipped=%s version=%s",
                skipped, len(kept), len(skipped), _DTC_VERSION,
            )
        if not kept:
            return 0
        return _layer1(kept, *args, **kwargs)
    except Exception:
        logger.exception("[SUMMARY AI DIRECT FRESHNESS] pending guard failed; use original")
        return _layer1(entries, *args, **kwargs)
