# ============================================================
# File   : trading/exit/tonosama_exit_runner.py
# Version: V1.0-SPLIT-TONOSAMA-RUNNER
# ------------------------------------------------------------
# 【概要】
#   殿様イナゴEXITの実行ランナー。
#
# 【役割】
#   - judge_tonosama_exit() 呼び出し
#   - 5秒足特徴量を policy へ渡す
#   - EXIT_ALL の場合 finalize_exit() へ接続
#   - partial 利確は現 executor が全決済のみのためログのみ
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, Optional

from trading.exit.exit_features import build_5sec_exit_features_safe
from trading.exit.exit_finalize import finalize_exit
from trading.exit.exit_utils import (
    get_feature_value,
    get_holding_seconds_safe,
    safe_float,
)

logger = logging.getLogger(__name__)

try:
    from trading.exit.tonosama_exit_policy import judge_tonosama_exit
except Exception:
    judge_tonosama_exit = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


TONOSAMA_EXIT_ENABLED = _env_bool("TONOSAMA_EXIT_ENABLED", True)

# 実売却がデフォルト。
# テスト時だけ TONOSAMA_EXIT_DRY_RUN=1
TONOSAMA_EXIT_DRY_RUN = _env_bool("TONOSAMA_EXIT_DRY_RUN", False)


def apply_tonosama_exit_if_needed(
    *,
    symbol: str,
    pos: Dict[str, Any],
    side: str,
    price: float,
    entry_price: float,
    features: Dict[str, Any],
    ctx: Any,
    now: dt.datetime,
    cluster_id: int,
    regime: int,
    inago_state: int,
    pnl: float,
    collapse_prob: float,
    bar5s: Optional[Dict[str, Any]] = None,
) -> bool:
    if not TONOSAMA_EXIT_ENABLED:
        return False

    if judge_tonosama_exit is None:
        logger.warning("[TONOSAMA EXIT] policy unavailable")
        return False

    if str(side or "").upper() != "BUY":
        return False

    try:
        holding_seconds = get_holding_seconds_safe(ctx, now)

        high_after_entry = get_feature_value(
            features,
            ctx,
            "highest",
            "high_after_entry",
            "mfe_high",
            default=0.0,
        )

        vwap = get_feature_value(
            features,
            ctx,
            "vwap",
            "VWAP",
            default=0.0,
        )

        ranking_lost_minutes = None
        try:
            if "ranking_lost_minutes" in features:
                ranking_lost_minutes = int(safe_float(features.get("ranking_lost_minutes"), 0))
            elif "ranking_lost_minutes" in pos:
                ranking_lost_minutes = int(safe_float(pos.get("ranking_lost_minutes"), 0))
        except Exception:
            ranking_lost_minutes = None

        bar5s_features = build_5sec_exit_features_safe(
            symbol=symbol,
            features=features,
            ctx=ctx,
            pos=pos,
            bar5s=bar5s,
        )

        decision = judge_tonosama_exit(
            symbol=symbol,
            entry_price=entry_price,
            current_price=price,
            high_after_entry=high_after_entry,
            vwap=vwap,
            hold_seconds=holding_seconds,
            ranking_lost_minutes=ranking_lost_minutes,
            already_first_tp=bool(pos.get("tonosama_first_tp_done", False)),
            already_second_tp=bool(pos.get("tonosama_second_tp_done", False)),

            # optional: 5秒足
            bar5s_drop_pct=bar5s_features.get("bar5s_drop_pct"),
            bar5s_consecutive_down=bar5s_features.get("bar5s_consecutive_down"),
            bar5s_volume_ratio=bar5s_features.get("bar5s_volume_ratio"),
            bar5s_vwap_break=bar5s_features.get("bar5s_vwap_break"),
            bar5s_high_after_entry=bar5s_features.get("bar5s_high_after_entry"),
        )

        if not decision.should_exit:
            logger.debug(
                "[TONOSAMA EXIT HOLD] symbol=%s price=%.4f entry=%.4f pnl_pct=%.2f "
                "reason=%s 5s_drop=%s 5s_down=%s 5s_vol=%s 5s_vwap_break=%s",
                symbol,
                price,
                entry_price,
                safe_float(getattr(decision, "pnl_pct", 0.0)),
                getattr(decision, "reason", "HOLD"),
                bar5s_features.get("bar5s_drop_pct"),
                bar5s_features.get("bar5s_consecutive_down"),
                bar5s_features.get("bar5s_volume_ratio"),
                bar5s_features.get("bar5s_vwap_break"),
            )
            return False

        action = str(getattr(decision, "action", "EXIT_ALL") or "EXIT_ALL")
        reason = str(getattr(decision, "reason", "TONOSAMA_EXIT") or "TONOSAMA_EXIT")
        sell_ratio = safe_float(getattr(decision, "sell_ratio", 1.0), 1.0)
        pnl_pct = safe_float(getattr(decision, "pnl_pct", 0.0), 0.0)

        logger.warning(
            "[TONOSAMA EXIT] symbol=%s action=%s ratio=%.2f price=%.4f entry=%.4f "
            "pnl=%.4f pnl_pct=%.2f high=%.4f vwap=%.4f hold_sec=%s "
            "5s_drop=%s 5s_down=%s 5s_vol=%s 5s_vwap_break=%s 5s_high=%s "
            "reason=%s dry_run=%s",
            symbol,
            action,
            sell_ratio,
            price,
            entry_price,
            pnl,
            pnl_pct,
            high_after_entry,
            vwap,
            holding_seconds,
            bar5s_features.get("bar5s_drop_pct"),
            bar5s_features.get("bar5s_consecutive_down"),
            bar5s_features.get("bar5s_volume_ratio"),
            bar5s_features.get("bar5s_vwap_break"),
            bar5s_features.get("bar5s_high_after_entry"),
            reason,
            TONOSAMA_EXIT_DRY_RUN,
        )

        if action == "EXIT_ALL":
            if TONOSAMA_EXIT_DRY_RUN:
                logger.warning(
                    "[TONOSAMA EXIT DRY_RUN] would execute full exit symbol=%s price=%.4f reason=%s",
                    symbol,
                    price,
                    reason,
                )
                return True

            logger.warning(
                "[TONOSAMA EXIT EXECUTE] symbol=%s price=%.4f reason=%s",
                symbol,
                price,
                reason,
            )

            finalize_exit(
                symbol=symbol,
                price=price,
                reason=f"TONOSAMA:{reason}",
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
            )
            return True

        logger.info(
            "[TONOSAMA EXIT] partial skipped symbol=%s action=%s ratio=%.2f "
            "reason=executor_full_exit_only original_reason=%s",
            symbol,
            action,
            sell_ratio,
            reason,
        )
        return False

    except TypeError:
        logger.exception(
            "[TONOSAMA_EXIT_ERROR] symbol=%s possible policy signature mismatch. "
            "tonosama_exit_policy.py may not be updated to 5sec version.",
            symbol,
        )
        return False

    except Exception:
        logger.exception("[TONOSAMA_EXIT_ERROR] symbol=%s", symbol)
        return False


__all__ = [
    "apply_tonosama_exit_if_needed",
]