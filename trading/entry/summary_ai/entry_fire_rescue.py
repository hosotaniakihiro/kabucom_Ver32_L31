# -*- coding: utf-8 -*-
# ============================================================
# File   : trading/entry/summary_ai/entry_fire_rescue.py
# Version: V4-ENTRY-FIRE-RESCUE-PRE-AI-SELL-CREDIT-GUARD-CALL
# ------------------------------------------------------------
# 旧 core/startup/entry_fire_rescue_runtime_patch.py を移設した
# (パッチではなく summary_ai 側の共有ヘルパ)。
#
# 旧 install() のラップは各ホストへ本文化済み:
#   trading/entry/summary_ai/candidates.py   … スコア/傾きしきい値の緩和
#   trading/entry/summary_ai/runner.py       … run_summary_ai_entry_from_df の
#                                              kwargs 既定値 / DEFAULT_MIN_TOP10_SLOPE
#   trading/entry/summary_ai/ai_gate_runner.py … SELL 信用の事前フィルタと
#                                              score_low 救済
# ここに残しているのは、それらが共有するヘルパと env 既定値だけ。
#
# 旧 install() の _watch_loop() (最大24回の再install) は、差し替える
# モジュール属性が無くなったため不要になり撤去した。
#
# 適用スコープ: 旧 install 元は core/startup/runtime_env/defaults_patch.py:216 の
# _safe_install(..., TRADING_CONTEXTS, "DISABLE_ENTRY_FIRE_RESCUE_PATCH", ...) で、
# TRADING_CONTEXTS は {"main"} のみ。scope_enabled() がそれを再現する。
# ============================================================

from __future__ import annotations
import logging
import os
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V4-ENTRY-FIRE-RESCUE-PRE-AI-SELL-CREDIT-GUARD-CALL"


_EFR_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)

_SCOPE: bool | None = None


def scope_enabled() -> bool:
    """旧 install スコープ (main.py かつ DISABLE/ENABLED env が許す) の再現。"""
    global _SCOPE
    if _SCOPE is not None:
        return _SCOPE
    enabled = False
    try:
        if str(os.environ.get("DISABLE_ENTRY_FIRE_RESCUE_PATCH", "")).strip() == "1":
            enabled = False
        elif not _env_bool("ENTRY_FIRE_RESCUE_ENABLED", True):
            enabled = False
        else:
            argv_parts = [str(x).replace("\\", "/").lower() for x in sys.argv]
            argv = " ".join(argv_parts)
            if any(n in argv for n in _EFR_DB_PROCESS_NAMES):
                enabled = False
            else:
                enabled = any(a.endswith("/main.py") or a == "main.py" for a in argv_parts)
    except Exception:
        enabled = False
    _SCOPE = enabled
    return enabled


def apply_env_defaults() -> None:
    """旧 _patch_once() 冒頭の _set_default_envs() を再現する。"""
    if not scope_enabled():
        return
    _set_default_envs()


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(str(v).replace(",", ""))
    except Exception:
        return default


def _set_default_envs() -> None:
    """Set operator-overridable defaults for the current low-score scale."""
    defaults = {
        "SUMMARY_ENTRY_MIN_SCORE": "0.12",
        "SUMMARY_AI_MIN_SCORE": "0.12",
        "ENTRY_GATE_MIN_SCORE": "0.12",
        "SUMMARY_AI_MIN_BUY_SCORE": "0.12",
        "SUMMARY_AI_MIN_SELL_SCORE": "0.12",
        "SUMMARY_AI_MAX_SELL_SCORE_FOR_BUY": "0.35",
        "SUMMARY_AI_MAX_BUY_SCORE_FOR_SELL": "0.35",
        "SUMMARY_ENTRY_ALLOW_1MIN_FALLBACK": "1",
        "SUMMARY_ENTRY_REQUIRE_3MIN_READY": "0",
        "SUMMARY_ENTRY_REQUIRE_MTF": "0",
        "SUMMARY_AI_REQUIRE_MTF": "0",
        "ENTRY_BYPASS_SLOPE_FILTER": "1",
        "SUMMARY_AI_DIRECT_LIQ_REQUIRE_DATA": "0",
        "SUMMARY_AI_SELL_CREDIT_PREFILTER": "1",
        "SUMMARY_AI_SELL_CREDIT_PREFILTER_BEFORE_AI": "1",
        "SUMMARY_AI_SELL_CREDIT_PREFILTER_CALL_GUARD": "1",
        "RANKING_ENTRY_BUILD_TIMEOUT_SEC": "30",
        "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC": "18",
        "RANKING_ENTRY_RUNTIME_BUDGET_SEC": "30",
        "RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC": "10",
        "RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC": "10",
        "RANKING_ENTRY_FAST_MAX_SYMBOLS": "30",
        "RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS": "30",
        "RANKING_ENTRY_FAST_MAX_PER_SIDE": "15",
        "RANKING_ENTRY_FAST_MAX_PER_TYPE": "8",
        "RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS": "200",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)


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


def _norm_side(v: Any, default: str = "BUY") -> str:
    try:
        s = str(v or default).strip().upper()
        return s if s in {"BUY", "SELL"} else default
    except Exception:
        return default


def _score_for_side(item: dict[str, Any]) -> float:
    side = _norm_side(item.get("side") or item.get("ai_side"), "BUY")
    if side == "SELL":
        return max(
            _safe_float(item.get("sell_score")),
            abs(_safe_float(item.get("score_total"))),
            abs(_safe_float(item.get("final_score"))),
        )
    return max(
        _safe_float(item.get("buy_score")),
        _safe_float(item.get("score_total")),
        _safe_float(item.get("final_score")),
    )


def _as_plain_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return dict(v)
    try:
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            if isinstance(d, dict):
                return dict(d)
    except Exception:
        pass
    return {}


def _merged_item_dicts(item: Any) -> list[dict[str, Any]]:
    root = _as_plain_dict(item)
    dicts: list[dict[str, Any]] = []
    if root:
        dicts.append(root)
        for k in ("ai_row", "source_row", "row", "summary_row"):
            v = root.get(k)
            if isinstance(v, dict):
                dicts.append(dict(v))
    return dicts


def _pick_from_item(item: Any, *keys: str) -> Any:
    for d in _merged_item_dicts(item):
        for k in keys:
            if k in d:
                return d.get(k)
    return None


def _flag_is_explicit_false(v: Any) -> bool:
    if v is None:
        return False
    try:
        if isinstance(v, bool):
            return not v
        s = str(v).strip().lower()
        if s in {"", "none", "nan", "null"}:
            return False
        return s in {"0", "false", "no", "n", "ng", "不可", "×", "x"}
    except Exception:
        return False


def _row_has_any_credit_flag(item: Any) -> bool:
    for key in (
        "short_ok",
        "shortable",
        "short_sale_ok",
        "is_short_ok",
        "short_sellable",
        "sell_target",
        "is_sell_target",
        "can_sell",
        "sellable",
        "margin_sellable",
        "credit_sellable",
        "can_margin_sell",
    ):
        if _pick_from_item(item, key) is not None:
            return True
    return False


def _call_sell_credit_guard(symbol: str) -> tuple[bool, str, dict[str, Any]]:
    """Call central sell credit guard only when enabled and symbol exists."""
    if not symbol or not _env_bool("SUMMARY_AI_SELL_CREDIT_PREFILTER_CALL_GUARD", True):
        return False, "", {}
    try:
        from AI.sell_credit_guard import can_sell_symbol

        ok = bool(can_sell_symbol(symbol))
        if ok:
            return False, "", {}
        return True, "sell_credit_guard_ng", {"symbol": symbol, "side": "SELL", "guard_call": "can_sell_symbol_false"}
    except Exception as e:
        # Fail-open: do not drop candidates if the guard itself is unavailable.
        logger.debug("[ENTRY FIRE RESCUE] can_sell_symbol pre-AI check failed symbol=%s err=%s", symbol, e, exc_info=True)
        return False, "", {}


def _sell_credit_block_reason(item: Any) -> tuple[bool, str, dict[str, Any]]:
    """Return block reason when a SELL row is definitely not short-sellable."""
    side = _norm_side(
        _pick_from_item(item, "side", "ai_side", "entry_decision", "signal"),
        "BUY",
    )
    if side != "SELL":
        return False, "", {}

    symbol = _norm_symbol(_pick_from_item(item, "symbol", "Symbol", "code", "銘柄コード"))
    checks = {
        "short_ok": _pick_from_item(item, "short_ok", "shortable", "short_sale_ok", "is_short_ok", "short_sellable"),
        "sell_target": _pick_from_item(item, "sell_target", "is_sell_target", "can_sell", "sellable"),
        "margin_sellable": _pick_from_item(item, "margin_sellable", "credit_sellable", "can_margin_sell"),
    }
    explicit_ng = {k: v for k, v in checks.items() if _flag_is_explicit_false(v)}
    if explicit_ng:
        detail = {"symbol": symbol, "side": side, **explicit_ng}
        return True, "sell_credit_guard_ng", detail

    # Many SUMMARY candidate rows do not carry short_ok/sell_target yet.
    # In that case, call the canonical guard before AI gate so non-shortable
    # SELL rows never become AI_OK.
    if not _row_has_any_credit_flag(item):
        return _call_sell_credit_guard(symbol)

    return False, "", {}


def _prefilter_sell_credit_ng_candidates(candidates: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Filter SELL credit NG rows while preserving DataFrame/list shape."""
    if not _env_bool("SUMMARY_AI_SELL_CREDIT_PREFILTER_BEFORE_AI", True):
        return candidates, []
    if candidates is None:
        return candidates, []

    try:
        import pandas as pd
        if isinstance(candidates, pd.DataFrame):
            if candidates.empty:
                return candidates, []
            keep_idx = []
            skipped: list[dict[str, Any]] = []
            for idx, row in candidates.iterrows():
                item = row.to_dict()
                blocked, reason, detail = _sell_credit_block_reason(item)
                if blocked:
                    skipped.append({"reason": reason, **detail})
                    continue
                keep_idx.append(idx)
            if skipped:
                filtered = candidates.loc[keep_idx].copy()
                return filtered, skipped
            return candidates, []
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] dataframe SELL credit prefilter skipped", exc_info=True)

    try:
        seq = list(candidates or [])
    except Exception:
        return candidates, []
    kept = []
    skipped = []
    for item in seq:
        blocked, reason, detail = _sell_credit_block_reason(item)
        if blocked:
            skipped.append({"reason": reason, **detail})
            continue
        kept.append(item)
    return kept, skipped


def rescue_score_low(results):
    """AI_NG のうち score_low だが実スコアが閾値以上のものを救済する。

    旧 patched_run_ai_gate_for_candidates の後段そのもの。
    """
    if not _env_bool("SUMMARY_AI_RESCUE_SCORE_LOW", True):
        return results
    threshold = _env_float("SUMMARY_AI_MIN_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", 0.12))
    rescued = []
    try:
        for item in list(results or []):
            if not isinstance(item, dict):
                continue
            if bool(item.get("allow")):
                continue
            reason = str(item.get("reason") or "")
            if "score_low" not in reason:
                continue
            score = _score_for_side(item)
            if score < threshold:
                continue
            item["allow"] = True
            item["confidence"] = max(_safe_float(item.get("confidence"), 0.0), _env_float("SUMMARY_AI_SCORE_LOW_RESCUE_CONFIDENCE", 0.66))
            item["reason"] = f"{reason}|score_low_rescued:{score:.4f}>={threshold:.4f}"
            item["model_used"] = str(item.get("model_used") or "SCORE_LOW_RESCUE")
            rescued.append(str(item.get("symbol") or ""))
        if rescued:
            logger.warning("[ENTRY FIRE RESCUE] SUMMARY_AI score_low rescued threshold=%.4f symbols=%s", threshold, rescued[:30])
    except Exception:
        logger.exception("[ENTRY FIRE RESCUE] score_low rescue failed")
    return results


def prefilter_sell_credit(candidate_obj):
    """SELL 信用が組めない候補を AI 前に落とす。戻り値は (filtered, skipped)。"""
    return _prefilter_sell_credit_ng_candidates(candidate_obj)


apply_env_defaults()
