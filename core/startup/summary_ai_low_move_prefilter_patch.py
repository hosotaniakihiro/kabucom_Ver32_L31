# ============================================================
# File   : core/startup/summary_ai_low_move_prefilter_patch.py
# Version: V2-LOW-MOVE-BEFORE-APPROVED-NONZERO-GUARD
# ------------------------------------------------------------
# 【目的】
#   SUMMARY_AI の AI_OK / Top3 / approved 後に、発注直前の order_builder で
#   LOW_MOVE_RANGE_TOO_SMALL になり snapshot_no_order になる問題を前倒しで防ぐ。
#
# 方針:
#   - 低変動ガードは緩和しない。
#   - order_builder と同等の range_pct 閾値を SUMMARY_AI 選定段階で適用する。
#   - low-move 候補は pending/entry_controller/order_builder まで進ませない。
#   - range 情報が完全に欠損している行は誤除外を避け、後段の既存ガードに任せる。
#   - V2: prefilter だけで approved=0 になり続けると、AI_OK があるのに
#         no_ai_ok で完全停止する。安全弁として top候補を最小限残し、
#         最終 order_builder / low move guard / board guard で止める。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V2-LOW-MOVE-BEFORE-APPROVED-NONZERO-GUARD"
_INSTALLED = False
_ORIG_SELECT_AI_OK_ITEMS: Callable[..., Any] | None = None
_ORIG_BUILD_AI_OK_APPROVED_ROWS: Callable[..., Any] | None = None

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
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


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return float(str(raw).replace(",", ""))
    except Exception:
        pass
    return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return int(float(str(raw).replace(",", "")))
    except Exception:
        pass
    return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if pd.isna(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _as_dict(v: Any) -> dict[str, Any]:
    try:
        if isinstance(v, dict):
            return dict(v)
        if isinstance(v, pd.Series):
            return v.to_dict()
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            if isinstance(d, dict):
                return dict(d)
    except Exception:
        pass
    return {}


def _first(d: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = d.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _merged_row(item: Any) -> dict[str, Any]:
    base = _as_dict(item)
    out: dict[str, Any] = {}
    for root in (base.get("source_row"), base.get("ai_row"), base):
        d = _as_dict(root)
        for k, v in d.items():
            if k not in out or out.get(k) in (None, ""):
                out[k] = v
            else:
                out[k] = v
    return out


def _pick_symbol(row: dict[str, Any]) -> str:
    return _norm_symbol(_first(row, ("symbol", "Symbol", "code", "stock_code", "銘柄コード"), ""))


def _pick_side(row: dict[str, Any]) -> str:
    side = str(_first(row, ("side", "ai_side", "entry_decision", "decision"), "") or "").strip().upper()
    if side in {"BUY", "SELL"}:
        return side
    buy = _safe_float(_first(row, ("score_buy", "buy_score"), 0.0), 0.0)
    sell = _safe_float(_first(row, ("score_sell", "sell_score"), 0.0), 0.0)
    score = _safe_float(_first(row, ("score", "score_total", "final_score", "display_score"), 0.0), 0.0)
    if sell > buy and sell > 0:
        return "SELL"
    if buy > sell and buy > 0:
        return "BUY"
    return "SELL" if score < 0 else "BUY" if score > 0 else ""


def _score(row: dict[str, Any]) -> float:
    side = _pick_side(row)
    if side == "BUY":
        return max(
            _safe_float(_first(row, ("score_buy", "buy_score"), 0.0), 0.0),
            _safe_float(_first(row, ("score", "score_total", "final_score", "display_score"), 0.0), 0.0),
        )
    if side == "SELL":
        return max(
            _safe_float(_first(row, ("score_sell", "sell_score"), 0.0), 0.0),
            abs(_safe_float(_first(row, ("score", "score_total", "final_score", "display_score"), 0.0), 0.0)),
        )
    return abs(_safe_float(_first(row, ("score", "score_total", "final_score", "display_score"), 0.0), 0.0))


def _range_pct(row: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    for k in (
        "range_pct",
        "range_5m_pct",
        "range_pct_5m",
        "entry_range_pct",
        "low_move_range_pct",
        "display_range_pct",
    ):
        if k in row and row.get(k) not in (None, ""):
            v = _safe_float(row.get(k), 0.0)
            return v, {"source": k, "range_pct": v}

    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high", "high_price", "HighPrice", "day_high"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low", "low_price", "LowPrice", "day_low"), 0.0), 0.0)
    if close > 0 and high > 0 and low > 0 and high >= low:
        v = (high - low) / close
        return v, {"source": "high_low_close_ratio", "range_pct": v, "high": high, "low": low, "close": close}
    return None, {"source": "missing", "close": close, "high": high, "low": low}


def _is_low_move_ng(item: Any) -> tuple[bool, dict[str, Any]]:
    row = _merged_row(item)
    symbol = _pick_symbol(row)
    side = _pick_side(row)
    rp, detail = _range_pct(row)
    min_range = _env_float("SUMMARY_AI_PREFILTER_MIN_RANGE_PCT", _env_float("ENTRY_ORDER_MIN_RANGE_PCT", 0.005))
    reject_missing = _env_bool("SUMMARY_AI_PREFILTER_REJECT_MISSING_RANGE", False)

    detail = dict(detail)
    detail.update({"symbol": symbol, "side": side, "min_range_pct": min_range, "score": _score(row)})

    if rp is None:
        detail["reason"] = "range_missing"
        return bool(reject_missing), detail

    detail["reason"] = "LOW_MOVE_RANGE_TOO_SMALL" if rp < min_range else "ok"
    return bool(rp < min_range), detail


def _best_rescue_items(items: list[dict[str, Any]], skipped: list[dict[str, Any]], *, stage: str) -> list[dict[str, Any]]:
    if not items:
        return []
    if not _env_bool("SUMMARY_AI_PREFILTER_KEEP_MIN_IF_ALL_SKIPPED", True):
        return []
    keep_n = max(1, _env_int("SUMMARY_AI_PREFILTER_KEEP_MIN_COUNT", 1))
    # 全落ち回避。LOW_MOVE自体を緩和するのではなく、最終 order_builder で再判定させる。
    ranked = []
    for item in items:
        row = _merged_row(item)
        ranked.append((_score(row), _pick_symbol(row), item))
    ranked.sort(key=lambda x: x[0], reverse=True)
    rescued = [x[2] for x in ranked[:keep_n]]
    logger.warning(
        "[SUMMARY AI LOW MOVE PREFILTER] all skipped safety rescue stage=%s keep=%s skipped_count=%s rescued=%s skipped_head=%s version=%s",
        stage,
        len(rescued),
        len(skipped),
        [{"symbol": x[1], "score": x[0]} for x in ranked[:keep_n]],
        skipped[:20],
        VERSION,
    )
    return rescued


def _filter_low_move_items(items: list[dict[str, Any]], *, stage: str) -> list[dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_LOW_MOVE_PREFILTER_ENABLED", True):
        return items
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in items or []:
        try:
            ng, detail = _is_low_move_ng(item)
            if ng:
                skipped.append(detail)
                continue
            if detail.get("reason") == "range_missing":
                missing.append(detail)
            kept.append(item)
        except Exception:
            logger.debug("[SUMMARY AI LOW MOVE PREFILTER] item check failed stage=%s", stage, exc_info=True)
            kept.append(item)
    if not kept and items:
        kept = _best_rescue_items(items, skipped, stage=stage)
    if skipped or missing:
        logger.warning(
            "[SUMMARY AI LOW MOVE PREFILTER] stage=%s before=%s after=%s skipped=%s missing=%s version=%s",
            stage,
            len(items or []),
            len(kept),
            skipped[:30],
            missing[:20],
            VERSION,
        )
    return kept


def _install_executor_prefilter() -> bool:
    global _ORIG_SELECT_AI_OK_ITEMS, _ORIG_BUILD_AI_OK_APPROVED_ROWS
    try:
        import trading.entry.summary_ai.executor as ex

        changed = []

        cur_select = getattr(ex, "_select_ai_ok_items", None)
        if callable(cur_select) and not getattr(cur_select, "_summary_ai_low_move_prefilter_v2", False):
            _ORIG_SELECT_AI_OK_ITEMS = getattr(cur_select, "_original", cur_select)

            def _patched_select_ai_ok_items(ok_items, *, max_entries: int):
                try:
                    filtered_items = _filter_low_move_items(list(ok_items or []), stage="before_top_selection")
                    selected = _ORIG_SELECT_AI_OK_ITEMS(filtered_items, max_entries=max_entries)
                    if not selected and ok_items and _env_bool("SUMMARY_AI_PREFILTER_KEEP_MIN_IF_ALL_SKIPPED", True):
                        logger.warning("[SUMMARY AI LOW MOVE PREFILTER] original selected=0 -> retry with top rescue items")
                        rescue = _best_rescue_items(list(ok_items or []), [], stage="select_retry")
                        return _ORIG_SELECT_AI_OK_ITEMS(rescue, max_entries=max_entries)
                    return selected
                except Exception:
                    logger.exception("[SUMMARY AI LOW MOVE PREFILTER] _select_ai_ok_items patch failed -> original")
                    return _ORIG_SELECT_AI_OK_ITEMS(ok_items, max_entries=max_entries)

            _patched_select_ai_ok_items._summary_ai_low_move_prefilter_v2 = True  # type: ignore[attr-defined]
            _patched_select_ai_ok_items._summary_ai_low_move_prefilter_v1 = True  # type: ignore[attr-defined]
            _patched_select_ai_ok_items._original = _ORIG_SELECT_AI_OK_ITEMS  # type: ignore[attr-defined]
            ex._select_ai_ok_items = _patched_select_ai_ok_items
            changed.append("_select_ai_ok_items")

        cur_build = getattr(ex, "build_ai_ok_approved_rows", None)
        if callable(cur_build) and not getattr(cur_build, "_summary_ai_low_move_prefilter_v2", False):
            _ORIG_BUILD_AI_OK_APPROVED_ROWS = getattr(cur_build, "_original", cur_build)

            def _patched_build_ai_ok_approved_rows(ai_results, *, max_entries: int = 3):
                rows = _ORIG_BUILD_AI_OK_APPROVED_ROWS(ai_results, max_entries=max_entries)
                if not isinstance(rows, list):
                    return rows
                filtered = _filter_low_move_items(rows, stage="after_approved_build")
                if not filtered and rows and _env_bool("SUMMARY_AI_PREFILTER_KEEP_MIN_IF_ALL_SKIPPED", True):
                    return _best_rescue_items(rows, [], stage="after_approved_rescue")
                return filtered

            _patched_build_ai_ok_approved_rows._summary_ai_low_move_prefilter_v2 = True  # type: ignore[attr-defined]
            _patched_build_ai_ok_approved_rows._summary_ai_low_move_prefilter_v1 = True  # type: ignore[attr-defined]
            _patched_build_ai_ok_approved_rows._original = _ORIG_BUILD_AI_OK_APPROVED_ROWS  # type: ignore[attr-defined]
            ex.build_ai_ok_approved_rows = _patched_build_ai_ok_approved_rows
            changed.append("build_ai_ok_approved_rows")

        logger.warning(
            "[SUMMARY AI LOW MOVE PREFILTER] installed changed=%s min_range_pct=%s keep_min_if_all_skipped=%s keep_min_count=%s reject_missing=%s version=%s",
            changed,
            _env_float("SUMMARY_AI_PREFILTER_MIN_RANGE_PCT", _env_float("ENTRY_ORDER_MIN_RANGE_PCT", 0.005)),
            _env_bool("SUMMARY_AI_PREFILTER_KEEP_MIN_IF_ALL_SKIPPED", True),
            _env_int("SUMMARY_AI_PREFILTER_KEEP_MIN_COUNT", 1),
            _env_bool("SUMMARY_AI_PREFILTER_REJECT_MISSING_RANGE", False),
            VERSION,
        )
        return bool(changed)
    except Exception:
        logger.exception("[SUMMARY AI LOW MOVE PREFILTER] install failed")
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("SUMMARY_AI_LOW_MOVE_PREFILTER_ENABLED", "1")
        os.environ.setdefault("SUMMARY_AI_PREFILTER_MIN_RANGE_PCT", "0.005")
        os.environ.setdefault("SUMMARY_AI_PREFILTER_REJECT_MISSING_RANGE", "0")
        os.environ.setdefault("SUMMARY_AI_PREFILTER_KEEP_MIN_IF_ALL_SKIPPED", "1")
        os.environ.setdefault("SUMMARY_AI_PREFILTER_KEEP_MIN_COUNT", "1")
        ok = _install_executor_prefilter()
        _INSTALLED = bool(ok)
        return bool(ok)
    except Exception:
        logger.exception("[SUMMARY AI LOW MOVE PREFILTER] install exception")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI LOW MOVE PREFILTER] auto install failed")


__all__ = ["install", "VERSION"]
