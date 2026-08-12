# ============================================================
# File   : core/startup/summary_ai_entry_hook_dataframe_truth_patch.py
# Version: V1.7-BOARD-GUARD-COMPAT-REMOVED
# ------------------------------------------------------------
# 【目的】
#   scheduler_jobs.summary.summary_ai_entry_hook_v20.run_summary_ai_entry_safe で
#   DataFrame truth value ambiguous が出る問題を防止する。
#
# V1.7:
#   - FINAL ENTRY BOARD GUARD COMPAT 一式 (_install_final_entry_board_guard_compat /
#     _call_board_guard_flexible / _unwrap_board_guard / _is_our_board_compat /
#     watcher スレッド) を撤去した。ラップ対象だった
#     final_entry_safety_guard_patch._board_guard は 46ddd54 の本文化で消滅しており、
#     関数は冒頭の `cur = None` → `if not callable(cur): return False` で
#     即 return する到達不能コードになっていた。watcher も 120 回 × 0.5 秒
#     ただ sleep するだけのスレッドだった。
#     板ガード本体は trading/handlers/entry_controller._final_guard_board_ok。
#   - これに伴い install() が常に False を返す問題も解消した
#     (副作用は効いているのに戻り値だけ False という状態だった)。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1.7-BOARD-GUARD-COMPAT-REMOVED"

_PATCHED = False
_WEAK_SUMMARY_AI_PATCHED = False
_ORIGINAL_RUN_AI_GATE_FOR_CANDIDATES = None
_PUSH_LIQUIDITY_CAP_PATCHED = False
_SUMMARY_AI_APPROVAL_DIAG_PATCHED = False
_ORIGINAL_ENTRY_PRICE_BOUNDS = None

# -----------------------------
# small helpers
# -----------------------------
def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _append_reason(base: Any, extra: str) -> str:
    b = str(base or "").strip()
    return f"{b}|{extra}" if b else str(extra)


# -----------------------------
# weak SUMMARY AI filter
# -----------------------------
def _weak_summary_ai_ng(item: dict[str, Any]) -> tuple[bool, str]:
    if not _env_bool("SUMMARY_AI_WEAK_SIGNAL_FILTER_ENABLED", True):
        return False, "disabled"
    try:
        source = str(item.get("source") or item.get("source_name") or "").upper()
        reason = str(item.get("reason") or "")
        ai_row = item.get("ai_row") if isinstance(item.get("ai_row"), dict) else {}
        source_row = item.get("source_row") if isinstance(item.get("source_row"), dict) else {}
        src_from_reason = "src=SUMMARY" in reason or "src=PUSH" in reason
        is_summary_like = src_from_reason or source in {"SUMMARY", "PUSH", "PUSH_SUMMARY", "SUMMARY_AI"}
        if not is_summary_like:
            return False, "not_summary_like"
        rank_score = _safe_float(ai_row.get("rankScore", ai_row.get("rank_score", source_row.get("rankScore", source_row.get("rank_score", 0.0)))), 0.0)
        score_mtf = max(
            abs(_safe_float(ai_row.get("score_mtf", source_row.get("score_mtf", 0.0)), 0.0)),
            abs(_safe_float(ai_row.get("mtf_score", source_row.get("mtf_score", 0.0)), 0.0)),
            abs(_safe_float(ai_row.get("mtf", source_row.get("mtf", 0.0)), 0.0)),
        )
        reason_has_zero_3m5m = ("3m=0.00" in reason or "3m=0" in reason) and ("5m=0.00" in reason or "5m=0" in reason)
        if reason_has_zero_3m5m and abs(rank_score) <= 1e-9:
            return True, "weak_summary_ai_zero_rank_and_zero_3m5m"
        v3 = max(abs(_safe_float(ai_row.get(k, source_row.get(k, 0.0)), 0.0)) for k in ("score_3m", "score_total_3m", "final_score_3m"))
        v5 = max(abs(_safe_float(ai_row.get(k, source_row.get(k, 0.0)), 0.0)) for k in ("score_5m", "score_total_5m", "final_score_5m"))
        if abs(rank_score) <= 1e-9 and score_mtf <= 1e-9 and v3 <= 1e-9 and v5 <= 1e-9:
            return True, "weak_summary_ai_no_rank_mtf_3m_5m"
        return False, "ok"
    except Exception:
        logger.exception("[SUMMARY AI WEAK FILTER] check failed")
        return False, "exception_fail_open"


def _install_summary_ai_weak_filter() -> bool:
    global _WEAK_SUMMARY_AI_PATCHED, _ORIGINAL_RUN_AI_GATE_FOR_CANDIDATES
    if _WEAK_SUMMARY_AI_PATCHED:
        return True
    try:
        import trading.entry.summary_ai.ai_gate_runner as target
        cur = getattr(target, "run_ai_gate_for_candidates", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI WEAK FILTER] target run_ai_gate_for_candidates not callable")
            return False
        if getattr(cur, "_summary_ai_weak_filter_v1", False):
            _WEAK_SUMMARY_AI_PATCHED = True
            return True
        _ORIGINAL_RUN_AI_GATE_FOR_CANDIDATES = cur

        def _patched_run_ai_gate_for_candidates(*args, **kwargs):
            results = _ORIGINAL_RUN_AI_GATE_FOR_CANDIDATES(*args, **kwargs)
            if not isinstance(results, list):
                return results
            blocked = 0
            for item in results:
                if not isinstance(item, dict) or not bool(item.get("allow")):
                    continue
                ng, why = _weak_summary_ai_ng(item)
                if not ng:
                    continue
                item["allow"] = False
                item["reason"] = _append_reason(item.get("reason"), why)
                blocked += 1
                logger.warning("[SUMMARY AI WEAK FILTER] AI_OK -> AI_NG symbol=%s side=%s reason=%s", item.get("symbol"), item.get("side") or item.get("ai_side"), why)
            if blocked:
                logger.warning("[SUMMARY AI WEAK FILTER] blocked weak AI_OK count=%s", blocked)
            return results

        _patched_run_ai_gate_for_candidates._summary_ai_weak_filter_v1 = True  # type: ignore[attr-defined]
        _patched_run_ai_gate_for_candidates._original = cur  # type: ignore[attr-defined]
        target.run_ai_gate_for_candidates = _patched_run_ai_gate_for_candidates
        _WEAK_SUMMARY_AI_PATCHED = True
        logger.warning("[SUMMARY AI WEAK FILTER] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI WEAK FILTER] install failed")
        return False


# -----------------------------
# PUSH rotation cap
# -----------------------------
def _install_push_liquidity_rotation_cap() -> bool:
    # trading/push/subscription_manager/liquidity_guard.py 本体へ本文化した。
    #   MIN_SURVIVOR_COUNT / MIN_SURVIVOR_RATIO … min(env, 50) / min(env, 0.50) の cap
    #   ROTATION_* 3 件 … ソース既定を実運用値 (50 / 0.50 / False) へ揃えた
    # 旧構成では main.py だけがここでハード代入していたが、
    # runtime_env/defaults.py:90-92 の PUSH_DEFAULTS が全プロセスへ
    # 同じ env 既定値を入れているため既定構成では値が一致しており、
    # 本文化しても差分は出ない (プロセス別に実測確認済み)。
    return True


# -----------------------------
# SUMMARY AI approval diagnostics + price floor
# -----------------------------
def _install_summary_ai_approval_diag_and_price_floor() -> bool:
    global _SUMMARY_AI_APPROVAL_DIAG_PATCHED, _ORIGINAL_ENTRY_PRICE_BOUNDS
    if _SUMMARY_AI_APPROVAL_DIAG_PATCHED:
        return True
    try:
        import trading.entry.summary_ai.executor as exec_mod
        min_override = max(0.0, _env_float("SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE", 200.0))
        os.environ["SUMMARY_AI_ENTRY_MIN_PRICE"] = str(min_override)
        os.environ["ENTRY_MIN_PRICE"] = str(min_override)
        try:
            exec_mod.DEFAULT_MIN_PRICE_FOR_ENTRY = float(min_override)
        except Exception:
            pass

        cur_bounds = getattr(exec_mod, "_entry_price_bounds", None)
        if callable(cur_bounds) and not getattr(cur_bounds, "_summary_ai_price200_patch_v1", False):
            _ORIGINAL_ENTRY_PRICE_BOUNDS = cur_bounds
            def _patched_entry_price_bounds():
                min_price, max_price, diag = _ORIGINAL_ENTRY_PRICE_BOUNDS()
                try:
                    old_min = float(min_price or 0.0)
                    if min_override > 0 and (old_min <= 0 or old_min > min_override):
                        min_price = float(min_override)
                    if not isinstance(diag, dict):
                        diag = {"raw_diag": str(diag)}
                    diag = dict(diag)
                    diag["summary_ai_price200_patch"] = True
                    diag["old_min_price"] = old_min
                    diag["effective_min_price"] = float(min_price or 0.0)
                    diag["min_override"] = min_override
                except Exception:
                    pass
                return min_price, max_price, diag
            _patched_entry_price_bounds._summary_ai_price200_patch_v1 = True  # type: ignore[attr-defined]
            _patched_entry_price_bounds._original = cur_bounds  # type: ignore[attr-defined]
            exec_mod._entry_price_bounds = _patched_entry_price_bounds

        # executor._filter_blocked_ai_ok_items のラップ (承認前 diag フィルタ) は撤去した。
        # executor.py:1664 の core filter watcher が 0.5 秒ごとに _ensure_core_filter() で
        # モジュール属性を _base_filter_blocked_ai_ok_items へ戻し、一度戻すと誰も
        # 再 install しない。install 直後 0.5 秒未満しか生存できない死にラッパーだった
        # (実測確認済み)。price bound / daily risk / trade restricted / sell reject の
        # 各判定は executor 本体側にも実装があるので機能欠落はない。

        _SUMMARY_AI_APPROVAL_DIAG_PATCHED = True
        logger.warning("[SUMMARY AI APPROVAL DIAG] installed price_min_override=%.0f executor_default_min=%s", min_override, getattr(exec_mod, "DEFAULT_MIN_PRICE_FOR_ENTRY", None))
        return True
    except Exception:
        logger.exception("[SUMMARY AI APPROVAL DIAG] install failed")
        return False


# -----------------------------
# ranking precheck helper
# -----------------------------
def _install_ranking_precheck_pending_failopen() -> bool:
    try:
        import core.startup.ranking_precheck_pending_failopen_patch as patch
        fn = getattr(patch, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] ranking precheck pending failopen installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] ranking precheck pending failopen install failed")
        return False


def install() -> bool:
    global _PATCHED
    ok_rank_precheck = _install_ranking_precheck_pending_failopen()
    ok_weak_ai = _install_summary_ai_weak_filter()
    ok_liq_cap = _install_push_liquidity_rotation_cap()
    ok_approval_diag = _install_summary_ai_approval_diag_and_price_floor()
    # summary_ai_entry_hook_v20._result_to_dict の差し替えは撤去した。
    # DataFrame -> records 正規化と execution の dict 化は
    # scheduler_jobs/summary/summary_ai_entry_hook_v20.py 本体へ本文化済み
    # (_result_to_dict_core + _normalize_result_dict、
    #  スコープは同ファイルの _df_truth_scope_enabled() が再現)。
    if _PATCHED:
        return True and ok_rank_precheck and ok_weak_ai and ok_liq_cap and ok_approval_diag
    _PATCHED = True
    logger.warning(
        "[SUMMARY AI HOOK DF TRUTH PATCH] installed ranking_precheck_failopen=%s weak_ai=%s liq_cap=%s approval_diag=%s version=%s",
        ok_rank_precheck, ok_weak_ai, ok_liq_cap, ok_approval_diag, VERSION,
    )
    return True and ok_rank_precheck and ok_weak_ai and ok_liq_cap and ok_approval_diag


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] auto install failed")


__all__ = ["install", "VERSION"]
