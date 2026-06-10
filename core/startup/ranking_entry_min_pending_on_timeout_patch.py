# ============================================================
# File   : core/startup/ranking_entry_min_pending_on_timeout_patch.py
# Version: V1-RANKING-MIN-PENDING-ON-TIMEOUT
# ------------------------------------------------------------
# Purpose:
#   ranking_entry_fast_runtime_patch can find a strong candidate but return
#   created=0 when the runtime deadline is reached before pending_add.
#   This patch keeps the fast path but guarantees at least one strong candidate
#   is submitted when candidates exist near timeout.
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from collections import Counter
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)
_DONE = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(raw))
    except Exception:
        return int(default)


def _patched_entry_from_ranking() -> int:
    import trading.ranking.entry_from_ranking as efr
    import core.startup.ranking_entry_fast_runtime_patch as fast

    started_dt = dt.datetime.now()
    started = time.perf_counter()
    budget_sec = max(5.0, _env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 18.0))
    # Small grace is only for pending_add after at least one candidate exists.
    add_grace = max(0.0, min(_env_float("RANKING_ENTRY_MIN_PENDING_GRACE_SEC", 8.0), 15.0))
    deadline = started + budget_sec
    add_deadline = deadline + add_grace
    max_pending = max(1, _env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", 5))
    min_force_score = _env_float("RANKING_ENTRY_MIN_PENDING_FORCE_SCORE", 70.0)

    logger.info(
        "[RANKING ENTRY MIN PENDING] start at=%s budget_sec=%.1f add_grace=%.1f max_pending=%s",
        started_dt.strftime("%Y-%m-%d %H:%M:%S"), budget_sec, add_grace, max_pending,
    )

    try:
        if not efr.is_time_allowed(started_dt):
            logger.info("[RANKING ENTRY MIN PENDING] skip reason=TIME_GUARD now=%s", started_dt.strftime("%H:%M:%S"))
            return 0

        ranking_df = efr._get_ranking_source_df()
        if ranking_df is None or ranking_df.empty:
            logger.info("[RANKING ENTRY MIN PENDING] skip reason=no_ranking_df")
            return 0

        rows_all = efr._prepare_rows(ranking_df)
        if not rows_all:
            logger.info("[RANKING ENTRY MIN PENDING] skip reason=no_normalized_rows")
            return 0

        rows = fast._ultra_prefilter_rows(rows_all)
        if not rows:
            logger.info("[RANKING ENTRY MIN PENDING] skip reason=no_prefilter_rows raw_rows=%s", len(rows_all))
            return 0

        tech_map: Dict[str, Dict[str, Any]] = {}
        cfg_tech = efr.RANKING_ENTRY_CONFIG.get("TECHNICAL", {}) or {}
        if bool(cfg_tech.get("ENABLED", True)) and callable(getattr(efr, "save_ranking_pseudo_technicals", None)):
            # Do not let slow/missing tech block all entries. The called function is already readonly/capped by fast patch.
            t0 = time.perf_counter()
            try:
                tech_map = efr.save_ranking_pseudo_technicals(rows)
            except Exception:
                logger.exception("[RANKING ENTRY MIN PENDING] technical attach failed -> continue without tech")
                tech_map = {}
            logger.info(
                "[RANKING ENTRY MIN PENDING] technical attached symbols=%s prefiltered_rows=%s raw_rows=%s elapsed=%.3fs",
                len(tech_map), len(rows), len(rows_all), time.perf_counter() - t0,
            )

        created = 0
        build_reject = 0
        filter_reject = 0
        pending_reject = 0
        reject_counts = Counter()
        reject_samples: List[Dict[str, Any]] = []
        current_keys: set[str] = set()
        best_by_symbol_side: Dict[Tuple[str, str], Dict[str, Any]] = {}
        now = efr._now()

        for row in rows:
            if time.perf_counter() >= deadline and best_by_symbol_side:
                logger.warning(
                    "[RANKING ENTRY MIN PENDING] stop scoring with candidates elapsed=%.3fs candidates=%s",
                    time.perf_counter() - started, len(best_by_symbol_side),
                )
                break
            if time.perf_counter() >= add_deadline:
                logger.warning(
                    "[RANKING ENTRY MIN PENDING] hard stop scoring elapsed=%.3fs candidates=%s",
                    time.perf_counter() - started, len(best_by_symbol_side),
                )
                break

            if callable(getattr(efr, "attach_ranking_technicals", None)):
                row = efr.attach_ranking_technicals(row, tech_map)
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            side = efr._infer_side(row)
            row["side"] = side
            current_keys.add(efr._history_key(symbol, side))
            prev_h = efr._get_prev_history(symbol, side)
            prev_price = efr._safe_float(prev_h.get("last_price"), 0.0)
            prev_rank = efr._safe_int(prev_h.get("last_rank_position"), 999999)
            consecutive = int(prev_h.get("consecutive", 0)) + 1 if prev_h else 1
            score, parts = efr._calc_ranking_only_score(row, side, prev_price, prev_rank, consecutive)
            row["score"] = row["score_total"] = row["ranking_only_score"] = score
            row["ranking_score_parts"] = parts
            ok, reason = efr._passes_ranking_only_filters(row, side, prev_h, score, parts)
            efr._update_history(
                symbol,
                side,
                efr._safe_float(row.get("price") or row.get("current_price"), 0.0),
                efr._safe_int(row.get("rank_position"), 999999),
                str(row.get("rank_type") or ""),
                now,
            )
            if not ok:
                filter_reject += 1
                reason_key = str(reason).split()[0].split("=")[0]
                reject_counts[reason_key] += 1
                if len(reject_samples) < 10:
                    reject_samples.append({
                        "symbol": symbol,
                        "side": side,
                        "rank_type": row.get("rank_type"),
                        "rank": row.get("rank_position"),
                        "score": round(score, 2),
                        "reason": reason,
                    })
                continue
            key = (symbol, side)
            old = best_by_symbol_side.get(key)
            old_score = efr._safe_float(old["row"].get("score_total"), 0.0) if isinstance(old, dict) and isinstance(old.get("row"), dict) else 0.0
            if old is None or score > old_score:
                best_by_symbol_side[key] = {
                    "row": row,
                    "parts": parts,
                    "prev_price": prev_price,
                    "prev_rank": prev_rank,
                    "consecutive": consecutive,
                }

        try:
            efr._reset_missing_histories(current_keys)
        except Exception:
            logger.debug("[RANKING ENTRY MIN PENDING] reset histories failed", exc_info=True)

        packs = list(best_by_symbol_side.items())
        packs.sort(key=lambda kv: efr._safe_float(kv[1]["row"].get("score_total"), 0.0), reverse=True)

        for (symbol, side), pack in packs:
            if created >= max_pending:
                break
            final_score = efr._safe_float(pack["row"].get("score_total"), 0.0)
            if time.perf_counter() >= deadline and created > 0:
                logger.warning(
                    "[RANKING ENTRY MIN PENDING] stop pending_add after created elapsed=%.3fs created=%s",
                    time.perf_counter() - started, created,
                )
                break
            if time.perf_counter() >= add_deadline and final_score < min_force_score:
                logger.warning(
                    "[RANKING ENTRY MIN PENDING] hard stop pending_add elapsed=%.3fs created=%s score=%.2f",
                    time.perf_counter() - started, created, final_score,
                )
                break

            row = pack["row"]
            parts = pack["parts"]
            prev_price = pack["prev_price"]
            prev_rank = pack["prev_rank"]
            consecutive = pack["consecutive"]
            entry_row = efr.build_entry_row(row)
            if not entry_row:
                build_reject += 1
                continue
            entry_row.update({
                "side": side,
                "source": "RANKING",
                "symbol": symbol,
                "entry_type": entry_row.get("entry_type") or "RANKING",
                "interval": entry_row.get("interval") or 1,
                "score": final_score,
                "score_total": final_score,
                "ranking_only_score": final_score,
                "ranking_entry_mode": "RANKING_MIN_PENDING_ON_TIMEOUT_V1",
                "ranking_prev_price": prev_price,
                "ranking_prev_rank": prev_rank,
                "ranking_consecutive": consecutive,
                "ranking_step_pct": parts.get("step_pct"),
                "ranking_rank_improve": parts.get("rank_improve"),
                "ranking_score_parts": parts,
                "ranking_min_pending_timeout_rescue": bool(time.perf_counter() >= deadline),
            })
            for k in (
                "ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "atr", "slope",
                "slope_atr_scaled", "vwap", "ranking_tech_score", "ranking_tech_ready", "ranking_tech_reason",
                "ranking_tech_datetime", "ranking_tech_db",
            ):
                if k in row:
                    entry_row[k] = row.get(k)
            pending_entry = {
                **entry_row,
                "created_at": now,
                "ranking_fallback_used": False,
                "ranking_strength": final_score,
                "technical_score": efr._safe_float(row.get("ranking_tech_score"), 0.0),
                "snapshot_score": final_score,
            }
            if efr.add_pending(pending_entry):
                created += 1
                logger.info(
                    "[RANKING MIN PENDING ADD] mode=TIMEOUT_RESCUE_V1 symbol=%s side=%s rank_type=%s rank=%s price=%.2f prev_price=%.2f step=%.3f%% day=%.3f%% volume=%.0f turnover=%.0f consecutive=%s rank_improve=%.1f score=%.2f tech=%.2f timeout_rescue=%s",
                    symbol,
                    side,
                    row.get("rank_type"),
                    row.get("rank_position"),
                    efr._safe_float(row.get("price") or row.get("current_price"), 0.0),
                    prev_price,
                    efr._safe_float(parts.get("step_pct"), 0.0),
                    efr._safe_float(row.get("day_change_pct"), 0.0),
                    efr._safe_float(row.get("volume"), 0.0),
                    efr._safe_float(row.get("turnover"), 0.0),
                    consecutive,
                    efr._safe_float(parts.get("rank_improve"), 0.0),
                    final_score,
                    efr._safe_float(row.get("ranking_tech_score"), 0.0),
                    bool(time.perf_counter() >= deadline),
                )
            else:
                pending_reject += 1

        elapsed = time.perf_counter() - started
        if reject_samples:
            logger.warning("[RANKING ENTRY MIN PENDING] reject_counts=%s samples=%s", dict(reject_counts), reject_samples)
        logger.info(
            "[RANKING ENTRY MIN PENDING] done created=%s raw_total=%s prefiltered=%s candidates=%s filter_reject=%s build_reject=%s pending_reject=%s budget_sec=%.1f add_grace=%.1f elapsed=%.3fs",
            created, len(rows_all), len(rows), len(best_by_symbol_side), filter_reject, build_reject, pending_reject,
            budget_sec, add_grace, elapsed,
        )
        return created
    except Exception:
        logger.exception("[RANKING ENTRY MIN PENDING] failed")
        return 0


def _patched_run_ranking_entry_pipeline() -> int:
    return _patched_entry_from_ranking()


def install() -> bool:
    global _DONE
    if _DONE:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        os.environ.setdefault("RANKING_ENTRY_MIN_PENDING_GRACE_SEC", "8")
        os.environ.setdefault("RANKING_ENTRY_MIN_PENDING_FORCE_SCORE", "70")
        os.environ.setdefault("RANKING_ENTRY_RUNTIME_BUDGET_SEC", "18")
        _patched_entry_from_ranking._ranking_min_pending_timeout_v1 = True  # type: ignore[attr-defined]
        _patched_run_ranking_entry_pipeline._ranking_min_pending_timeout_v1 = True  # type: ignore[attr-defined]
        efr.entry_from_ranking = _patched_entry_from_ranking
        efr.run_ranking_entry_pipeline = _patched_run_ranking_entry_pipeline
        _DONE = True
        logger.warning(
            "[RANKING ENTRY MIN PENDING] installed v1 budget=%s grace=%s force_score=%s",
            os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            os.environ.get("RANKING_ENTRY_MIN_PENDING_GRACE_SEC"),
            os.environ.get("RANKING_ENTRY_MIN_PENDING_FORCE_SCORE"),
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY MIN PENDING] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY MIN PENDING] auto install failed")

__all__ = ["install"]
