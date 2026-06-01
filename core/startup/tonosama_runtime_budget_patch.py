# ============================================================
# File   : core/startup/tonosama_runtime_budget_patch.py
# Version: Ver2.1-TONOSAMA-RUNTIME-BUDGET-FIRST-PENDING
# ------------------------------------------------------------
# Purpose:
#   TONOSAMA runner can continue in a daemon thread even after the
#   scheduler timeout wrapper returns.  This patch keeps the loop bounded,
#   but it must not finish with registered=0 only because candidate build
#   consumed the time budget.
#
# Ver2.1:
#   - Evaluation budget starts after candidates are ready.
#   - Environment values such as 10sec / 5 candidates are floored to
#     practical minimums: 25sec / 12 candidates.
#   - While registered=0, the loop does not stop solely on elapsed time;
#     it continues until at least one candidate reaches add_pending or the
#     evaluated candidate limit is exhausted.
#   - Add-fail samples are logged so the next blocker is visible.
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_on(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return float(default)
        return float(s.replace(",", ""))
    except Exception:
        return float(default)


def _prioritize_candidates(candidates):
    try:
        import pandas as pd
        if candidates is None or candidates.empty:
            return candidates
        df = candidates.copy()

        price = pd.to_numeric(df.get("close"), errors="coerce").fillna(0.0)
        min_price = _env_float("ENTRY_MIN_PRICE", _env_float("TONOSAMA_ENTRY_MIN_PRICE", 1500.0))
        max_price = _env_float("ENTRY_MAX_PRICE", _env_float("TONOSAMA_ENTRY_MAX_PRICE", 7000.0))
        budget = _env_float("ENTRY_BUDGET_YEN", _env_float("TRADE_BUDGET_YEN", 700000.0))
        lot_size = max(1.0, _env_float("ENTRY_LOT_SIZE", 100.0))
        affordable_max = min(max_price, budget / lot_size) if budget > 0 else max_price

        if _env_on("TONOSAMA_RUNTIME_PRIORITIZE_PRICE_ELIGIBLE", True):
            df["__prio_price_ok"] = (price >= min_price) & (price <= affordable_max)
        else:
            df["__prio_price_ok"] = True

        df["__prio_score"] = df.get("_tonosama_score", 0).map(lambda x: abs(_safe_float(x, 0.0)))
        df["__prio_volume"] = df.get("_latest_volume", 0).map(lambda x: _safe_float(x, 0.0))
        df["__prio_range"] = df.get("_intrabar_range_pct", 0).map(lambda x: _safe_float(x, 0.0))
        df["__prio_surge"] = df.get("_max_volume_surge_ratio", 0).map(lambda x: _safe_float(x, 0.0))
        eligible = int(df["__prio_price_ok"].sum())
        df = df.sort_values(
            ["__prio_price_ok", "__prio_score", "__prio_volume", "__prio_range", "__prio_surge"],
            ascending=[False, False, False, False, False],
            kind="mergesort",
        )
        logger.warning(
            "[TONOSAMA RUNTIME BUDGET] priority rows=%s price_eligible=%s min_price=%.1f max_price=%.1f affordable_max=%.1f",
            len(df), eligible, min_price, max_price, affordable_max,
        )
        return df.drop(columns=[c for c in ("__prio_price_ok", "__prio_score", "__prio_volume", "__prio_range", "__prio_surge") if c in df.columns])
    except Exception:
        logger.debug("[TONOSAMA RUNTIME BUDGET] prioritize failed", exc_info=True)
        return candidates


def _apply_patch() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import pandas as pd  # noqa: F401
        import trading.entry.tonosama.runner as runner
    except Exception:
        logger.debug("[TONOSAMA RUNTIME BUDGET PATCH] runner not ready", exc_info=True)
        return False

    try:
        cur = getattr(runner, "build_tonosama_entries", None)
        if getattr(cur, "_TONOSAMA_RUNTIME_BUDGET_PATCHED_V21", False):
            _INSTALLED = True
            return True

        original = getattr(runner, "_TONOSAMA_ORIGINAL_BUILD_TONOSAMA_ENTRIES", None)
        if not callable(original):
            original = cur
        if not callable(original):
            logger.warning("[TONOSAMA RUNTIME BUDGET PATCH] original build_tonosama_entries missing")
            return False

        def _budgeted_build_tonosama_entries() -> int:
            build_started = time.perf_counter()
            raw_budget = _env_float("TONOSAMA_LOOP_TIME_BUDGET_SEC", 25.0)
            raw_max_eval = _env_int("TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES", 12)
            budget_sec = max(25.0, raw_budget)
            max_eval = max(12, raw_max_eval)
            min_remain = max(1.5, _env_float("TONOSAMA_RUNTIME_MIN_REMAIN_SEC", 1.5))

            try:
                candidates = runner.iter_tonosama_candidate_rows()
            except Exception:
                logger.exception("[TONOSAMA RUNTIME BUDGET] iter_tonosama_candidate_rows failed")
                return 0

            candidate_elapsed = time.perf_counter() - build_started
            try:
                empty = bool(candidates is None or candidates.empty)
            except Exception:
                empty = True
            if empty:
                logger.info("[TONOSAMA RUNTIME BUDGET] build done candidates=0 registered=0 candidate_elapsed=%.3fs", candidate_elapsed)
                return 0

            try:
                total_candidates = int(len(candidates))
                candidates = _prioritize_candidates(candidates).head(max_eval).reset_index(drop=True)
            except Exception:
                total_candidates = 0
                try:
                    candidates = candidates.head(max_eval).reset_index(drop=True)
                except Exception:
                    pass

            eval_started = time.perf_counter()
            deadline = eval_started + budget_sec
            registered = 0
            ai_ng = 0
            duplicate = 0
            low_score = 0
            no_symbol = 0
            evaluated = 0
            time_budget_stop = False
            final_low_samples: list[dict[str, Any]] = []
            add_fail_samples: list[dict[str, Any]] = []

            def _remaining() -> float:
                return deadline - time.perf_counter()

            for idx, row in candidates.iterrows():
                now = time.perf_counter()
                # registered=0 の間は、時間切れだけでは止めない。
                if now >= deadline and registered > 0:
                    time_budget_stop = True
                    logger.warning(
                        "[TONOSAMA RUNTIME BUDGET] stop before candidate budget_sec=%.1f raw_budget=%.1f eval_elapsed=%.3fs registered=%s idx=%s evaluated_limit=%s total_candidates=%s",
                        budget_sec, raw_budget, now - eval_started, registered, idx, max_eval, total_candidates,
                    )
                    break

                if registered >= runner.MAX_PENDING_PER_LOOP:
                    break

                symbol = runner.normalize_symbol(row.get("symbol"))
                if not symbol:
                    no_symbol += 1
                    continue
                evaluated += 1

                try:
                    if runner.has_tonosama_pending(symbol):
                        duplicate += 1
                        continue
                except Exception:
                    logger.debug("[TONOSAMA RUNTIME BUDGET] has_pending failed symbol=%s", symbol, exc_info=True)

                raw_score = runner.safe_float(row.get("_tonosama_score"), 0.0)
                if raw_score <= 0:
                    low_score += 1
                    final_low_samples.append({"symbol": symbol, "reason": "raw_score_le_zero", "raw_score": raw_score})
                    continue

                if registered > 0 and _remaining() <= min_remain:
                    time_budget_stop = True
                    logger.warning(
                        "[TONOSAMA RUNTIME BUDGET] stop before AI due reserve symbol=%s remaining=%.3fs registered=%s min_remain=%.3fs",
                        symbol, _remaining(), registered, min_remain,
                    )
                    break

                try:
                    ai_ok, ai_prob, ai_reason = runner.ai_check_tonosama_entry(row)
                except Exception:
                    ai_ng += 1
                    logger.warning("[TONOSAMA RUNTIME BUDGET] ai_check failed symbol=%s", symbol, exc_info=True)
                    continue

                if not ai_ok:
                    ai_ng += 1
                    logger.info(
                        "[TONOSAMA ENTRY AI NG] symbol=%s prob=%.3f reason=%s surge=%.2f price_chg=%.2f body=%.3f range=%.3f close_pos=%.1f upper_wick=%.1f lower_wick=%.1f vol=%.0f 5s=%.3f slope=%.6f",
                        symbol, ai_prob, ai_reason,
                        runner.safe_float(row.get("_max_volume_surge_ratio"), 0.0),
                        runner.safe_float(row.get("_max_price_change_pct"), 0.0),
                        runner.safe_float(row.get("_body_change_pct"), 0.0),
                        runner.safe_float(row.get("_intrabar_range_pct"), 0.0),
                        runner.safe_float(row.get("_close_position_pct"), 50.0),
                        runner.safe_float(row.get("_upper_wick_pct"), 0.0),
                        runner.safe_float(row.get("_lower_wick_pct"), 0.0),
                        runner.safe_float(row.get("_latest_volume"), 0.0),
                        runner.safe_float(row.get("price_change_5s_pct"), 0.0),
                        runner.safe_float(row.get("_slope"), 0.0),
                    )
                    continue

                try:
                    final_score = runner.calc_final_score_safe(row, raw_score=raw_score, ai_prob=ai_prob)
                except Exception:
                    logger.warning("[TONOSAMA RUNTIME BUDGET] final_score failed symbol=%s", symbol, exc_info=True)
                    low_score += 1
                    continue

                if final_score < runner.MIN_FINAL_SCORE:
                    low_score += 1
                    final_low_samples.append({
                        "symbol": symbol,
                        "reason": "final_score_low",
                        "final_score": round(final_score, 4),
                        "min_final_score": runner.MIN_FINAL_SCORE,
                        "raw_score": round(raw_score, 4),
                        "ai_prob": round(ai_prob, 4),
                    })
                    continue

                try:
                    entry = runner.build_pending_entry(row, final_score=final_score, ai_prob=ai_prob, ai_reason=ai_reason)
                    ok = bool(runner.add_tonosama_pending(entry))
                    if ok:
                        registered += 1
                        logger.info(
                            "🔥 TONOSAMA PENDING %s score=%.2f price=%.1f vol=%.0f body=%.3f%% range=%.3f%% close_pos=%.1f%% upper_wick=%.1f%% lower_wick=%.1f%% surge=%.2fx price_chg=%.2f%% tf=%s 5s=%.3f%% slope=%.6f ai_prob=%.3f",
                            symbol, final_score,
                            runner.safe_float(row.get("close"), 0.0),
                            runner.safe_float(row.get("_latest_volume"), 0.0),
                            runner.safe_float(row.get("_body_change_pct"), 0.0),
                            runner.safe_float(row.get("_intrabar_range_pct"), 0.0),
                            runner.safe_float(row.get("_close_position_pct"), 50.0),
                            runner.safe_float(row.get("_upper_wick_pct"), 0.0),
                            runner.safe_float(row.get("_lower_wick_pct"), 0.0),
                            runner.safe_float(row.get("_max_volume_surge_ratio"), 0.0),
                            runner.safe_float(row.get("_max_price_change_pct"), 0.0),
                            str(row.get("_surge_tf", "")),
                            runner.safe_float(row.get("price_change_5s_pct"), 0.0),
                            runner.safe_float(row.get("_slope"), 0.0),
                            ai_prob,
                        )
                        try:
                            runner.notify_discord_tonosama_pending(entry)
                        except Exception:
                            logger.warning("[TONOSAMA RUNTIME BUDGET] notify failed symbol=%s", symbol, exc_info=True)
                    else:
                        add_fail_samples.append({"symbol": symbol, "side": entry.get("side"), "score": round(final_score, 4)})
                except Exception:
                    add_fail_samples.append({"symbol": symbol, "reason": "exception"})
                    logger.warning("[TONOSAMA RUNTIME BUDGET] add failed symbol=%s", symbol, exc_info=True)
                    continue

            logger.info(
                "[TONOSAMA RUNTIME BUDGET] build done total_candidates=%s candidates=%s evaluated=%s evaluated_max=%s registered=%s duplicate=%s ai_ng=%s low_score=%s no_symbol=%s stopped=%s budget_sec=%.1f raw_budget=%.1f max_eval=%s raw_max_eval=%s candidate_elapsed=%.3fs eval_elapsed=%.3fs total_elapsed=%.3fs low_score_samples=%s add_fail_samples=%s",
                total_candidates, len(candidates), evaluated, max_eval, registered, duplicate, ai_ng, low_score, no_symbol, time_budget_stop,
                budget_sec, raw_budget, max_eval, raw_max_eval, candidate_elapsed, time.perf_counter() - eval_started, time.perf_counter() - build_started,
                final_low_samples[:10], add_fail_samples[:10],
            )
            return registered

        setattr(runner, "_TONOSAMA_ORIGINAL_BUILD_TONOSAMA_ENTRIES", original)
        setattr(runner, "build_tonosama_entries", _budgeted_build_tonosama_entries)
        setattr(runner, "_TONOSAMA_RUNTIME_BUDGET_PATCHED", True)
        setattr(runner, "_TONOSAMA_RUNTIME_BUDGET_PATCHED_V20", True)
        setattr(runner, "_TONOSAMA_RUNTIME_BUDGET_PATCHED_V21", True)
        _budgeted_build_tonosama_entries._TONOSAMA_RUNTIME_BUDGET_PATCHED_V21 = True  # type: ignore[attr-defined]
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA RUNTIME BUDGET PATCH] installed V2.1 budget_sec_floor=25 raw_budget=%s max_eval_floor=12 raw_max_eval=%s price_priority=%s",
            os.getenv("TONOSAMA_LOOP_TIME_BUDGET_SEC", "25"),
            os.getenv("TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES", "12"),
            os.getenv("TONOSAMA_RUNTIME_PRIORITIZE_PRICE_ELIGIBLE", "1"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA RUNTIME BUDGET PATCH] install failed")
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLING
    if _apply_patch():
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _retry_loop() -> None:
            global _INSTALLING
            try:
                for _ in range(30):
                    if _apply_patch():
                        return
                    time.sleep(0.2)
                logger.warning("[TONOSAMA RUNTIME BUDGET PATCH] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_retry_loop, name="tonosama-runtime-budget-patch", daemon=True).start()
    return False


__all__ = ["install"]
