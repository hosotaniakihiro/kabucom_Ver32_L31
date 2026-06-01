# ============================================================
# File   : core/startup/tonosama_runtime_budget_patch.py
# Version: Ver1.2-TONOSAMA-RUNTIME-BUDGET-PRACTICAL
# ------------------------------------------------------------
# Purpose:
#   TONOSAMA runner can continue in a daemon thread even after the
#   scheduler timeout wrapper returns. That caused old worker threads
#   to keep running for 100s+ and made every next 30s schedule skip as
#   previous_timeout_thread_still_alive.
#
# Ver1.2:
#   - 10秒/5候補や18秒/8候補では、候補 ready rows=11 の時に
#     AI確認・板/MA確認だけで時間切れし、registered=0 のまま終了することがある。
#   - 既定を 25秒 / 12候補へ拡張。
#   - 候補を score/volume/range/surge で優先ソートして、登録可能性の高い銘柄を先に処理。
#   - registered=0 の間は時間切れ直後でも最低1件を最後まで評価する。
#   - 通知で時間超過しても pending 登録を優先する。
#
# Defaults:
#   TONOSAMA_LOOP_TIME_BUDGET_SEC=25
#   TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES=12
#   TONOSAMA_RUNTIME_MIN_REMAIN_SEC=1.5
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
        if candidates is None or candidates.empty:
            return candidates
        df = candidates.copy()
        df["__prio_score"] = df.get("_tonosama_score", 0).map(lambda x: abs(_safe_float(x, 0.0)))
        df["__prio_volume"] = df.get("_latest_volume", 0).map(lambda x: _safe_float(x, 0.0))
        df["__prio_range"] = df.get("_intrabar_range_pct", 0).map(lambda x: _safe_float(x, 0.0))
        df["__prio_surge"] = df.get("_max_volume_surge_ratio", 0).map(lambda x: _safe_float(x, 0.0))
        df = df.sort_values(
            ["__prio_score", "__prio_volume", "__prio_range", "__prio_surge"],
            ascending=[False, False, False, False],
            kind="mergesort",
        )
        return df.drop(columns=[c for c in ("__prio_score", "__prio_volume", "__prio_range", "__prio_surge") if c in df.columns])
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
        if getattr(runner, "_TONOSAMA_RUNTIME_BUDGET_PATCHED_V12", False):
            _INSTALLED = True
            return True

        original = getattr(runner, "build_tonosama_entries", None)
        if not callable(original):
            logger.warning("[TONOSAMA RUNTIME BUDGET PATCH] original build_tonosama_entries missing")
            return False

        def _budgeted_build_tonosama_entries() -> int:
            started = time.perf_counter()
            budget_sec = max(5.0, _env_float("TONOSAMA_LOOP_TIME_BUDGET_SEC", 25.0))
            max_eval = max(1, _env_int("TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES", 12))
            min_remain = max(0.0, _env_float("TONOSAMA_RUNTIME_MIN_REMAIN_SEC", 1.5))
            deadline = started + budget_sec

            def _remaining() -> float:
                return deadline - time.perf_counter()

            try:
                candidates = runner.iter_tonosama_candidate_rows()
            except Exception:
                logger.exception("[TONOSAMA RUNTIME BUDGET] iter_tonosama_candidate_rows failed")
                return 0

            try:
                empty = bool(candidates is None or candidates.empty)
            except Exception:
                empty = True
            if empty:
                logger.info("[TONOSAMA RUNTIME BUDGET] build done candidates=0 registered=0 elapsed=%.3fs", time.perf_counter() - started)
                return 0

            total_candidates = 0
            try:
                total_candidates = int(len(candidates))
                candidates = _prioritize_candidates(candidates).head(max_eval).reset_index(drop=True)
            except Exception:
                pass

            registered = 0
            ai_ng = 0
            duplicate = 0
            low_score = 0
            no_symbol = 0
            evaluated = 0
            time_budget_stop = False
            final_low_samples: list[dict[str, Any]] = []

            for idx, row in candidates.iterrows():
                now = time.perf_counter()
                # registered=0 の場合は、最初の候補だけは最後まで評価する。
                if now >= deadline and registered > 0:
                    time_budget_stop = True
                    logger.warning(
                        "[TONOSAMA RUNTIME BUDGET] stop before candidate budget_sec=%.1f elapsed=%.3fs registered=%s idx=%s evaluated_limit=%s total_candidates=%s",
                        budget_sec, now - started, registered, idx, max_eval, total_candidates,
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
                    if runner.add_tonosama_pending(entry):
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
                except Exception:
                    logger.warning("[TONOSAMA RUNTIME BUDGET] add failed symbol=%s", symbol, exc_info=True)
                    continue

            logger.info(
                "[TONOSAMA RUNTIME BUDGET] build done total_candidates=%s candidates=%s evaluated=%s evaluated_max=%s registered=%s duplicate=%s ai_ng=%s low_score=%s no_symbol=%s stopped=%s budget_sec=%.1f elapsed=%.3fs low_score_samples=%s",
                total_candidates, len(candidates), evaluated, max_eval, registered, duplicate, ai_ng, low_score, no_symbol, time_budget_stop, budget_sec, time.perf_counter() - started, final_low_samples[:10],
            )
            return registered

        setattr(runner, "_TONOSAMA_ORIGINAL_BUILD_TONOSAMA_ENTRIES", original)
        setattr(runner, "build_tonosama_entries", _budgeted_build_tonosama_entries)
        setattr(runner, "_TONOSAMA_RUNTIME_BUDGET_PATCHED", True)
        setattr(runner, "_TONOSAMA_RUNTIME_BUDGET_PATCHED_V12", True)
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA RUNTIME BUDGET PATCH] installed v1.2 budget_sec=%s max_eval=%s min_remain=%s",
            os.getenv("TONOSAMA_LOOP_TIME_BUDGET_SEC", "25"),
            os.getenv("TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES", "12"),
            os.getenv("TONOSAMA_RUNTIME_MIN_REMAIN_SEC", "1.5"),
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
