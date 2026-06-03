# ============================================================
# File   : trading/exit/tonosama_exit_runner.py
# Version: V1.1-MIRROR-SELL-TONOSAMA-EXIT
# ------------------------------------------------------------
# 【概要】
#   殿様イナゴEXITの実行ランナー。
#
# 【役割】
#   - judge_tonosama_exit() 呼び出し
#   - 5秒足特徴量を policy へ渡す
#   - EXIT_ALL の場合 finalize_exit() へ接続
#   - partial 利確は現 executor が全決済のみのためログのみ
#
# V1.1:
#   - BUY専用だったTonosama EXITをSELLにも適用。
#   - SELLは価格軸を反転してBUY用policyへ通す。
#     SELLの「VWAP上抜け」「安値から反発」「5秒足陽線」を、
#     BUY側の「VWAP割れ」「高値から下落」「5秒足陰線」にミラー変換する。
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
TONOSAMA_EXIT_SELL_ENABLED = _env_bool("TONOSAMA_EXIT_SELL_ENABLED", True)

# 実売却がデフォルト。
# テスト時だけ TONOSAMA_EXIT_DRY_RUN=1
TONOSAMA_EXIT_DRY_RUN = _env_bool("TONOSAMA_EXIT_DRY_RUN", False)


def _mirror_price(entry_price: float, price: float) -> float:
    """SELLをBUY policyへ通すため、建値を中心に価格を反転する。"""
    try:
        entry = float(entry_price)
        px = float(price)
        mirrored = (2.0 * entry) - px
        return mirrored if mirrored > 0 else 0.0
    except Exception:
        return 0.0


def _safe_side(side: Any) -> str:
    s = str(side or "").strip().upper()
    if s in {"BUY", "BUY_CREDIT", "LONG", "2", "02", "20", "信用買", "買", "買建"}:
        return "BUY"
    if s in {"SELL", "SELL_CREDIT", "SHORT", "1", "01", "10", "信用売", "売", "売建"}:
        return "SELL"
    return s


def _decision_inputs_for_side(
    *,
    side: str,
    entry_price: float,
    price: float,
    high_after_entry: float,
    low_after_entry: float,
    vwap: float,
    bar5s_features: Dict[str, Any],
) -> Dict[str, Any]:
    """BUY policy 用の入力へ変換する。BUYはそのまま、SELLは反転。"""
    if side == "SELL":
        mirrored_price = _mirror_price(entry_price, price)
        mirrored_vwap = _mirror_price(entry_price, vwap) if vwap and vwap > 0 else 0.0
        # SELLの有利方向は安値更新なので、反転後はhigh_after_entryとして扱う。
        mirrored_high = _mirror_price(entry_price, low_after_entry) if low_after_entry and low_after_entry > 0 else 0.0

        # SELLで価格が上がる5秒足は、BUY policyでは下落として扱う。
        raw_drop = bar5s_features.get("bar5s_drop_pct")
        try:
            mirrored_drop = -float(raw_drop) if raw_drop is not None else None
        except Exception:
            mirrored_drop = None

        return {
            "policy_side": "SELL_MIRROR",
            "policy_price": mirrored_price,
            "policy_high_after_entry": mirrored_high,
            "policy_vwap": mirrored_vwap,
            "bar5s_drop_pct": mirrored_drop,
            "bar5s_consecutive_down": bar5s_features.get("bar5s_consecutive_up"),
            "bar5s_volume_ratio": bar5s_features.get("bar5s_volume_ratio"),
            "bar5s_vwap_break": bar5s_features.get("bar5s_vwap_above"),
            "bar5s_high_after_entry": _mirror_price(entry_price, bar5s_features.get("bar5s_low_after_entry") or low_after_entry) if (bar5s_features.get("bar5s_low_after_entry") or low_after_entry) else mirrored_high,
            "real_price": price,
            "real_high_after_entry": high_after_entry,
            "real_low_after_entry": low_after_entry,
            "real_vwap": vwap,
        }

    return {
        "policy_side": "BUY",
        "policy_price": price,
        "policy_high_after_entry": high_after_entry,
        "policy_vwap": vwap,
        "bar5s_drop_pct": bar5s_features.get("bar5s_drop_pct"),
        "bar5s_consecutive_down": bar5s_features.get("bar5s_consecutive_down"),
        "bar5s_volume_ratio": bar5s_features.get("bar5s_volume_ratio"),
        "bar5s_vwap_break": bar5s_features.get("bar5s_vwap_break"),
        "bar5s_high_after_entry": bar5s_features.get("bar5s_high_after_entry"),
        "real_price": price,
        "real_high_after_entry": high_after_entry,
        "real_low_after_entry": low_after_entry,
        "real_vwap": vwap,
    }


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

    side_norm = _safe_side(side)
    if side_norm == "SELL" and not TONOSAMA_EXIT_SELL_ENABLED:
        return False
    if side_norm not in {"BUY", "SELL"}:
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
        low_after_entry = get_feature_value(
            features,
            ctx,
            "lowest",
            "low_after_entry",
            "mfe_low",
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

        inp = _decision_inputs_for_side(
            side=side_norm,
            entry_price=float(entry_price),
            price=float(price),
            high_after_entry=safe_float(high_after_entry, 0.0),
            low_after_entry=safe_float(low_after_entry, 0.0),
            vwap=safe_float(vwap, 0.0),
            bar5s_features=bar5s_features,
        )
        policy_price = safe_float(inp.get("policy_price"), 0.0)
        if policy_price <= 0:
            logger.debug("[TONOSAMA EXIT] skip invalid mirrored price symbol=%s side=%s entry=%.4f price=%.4f", symbol, side_norm, entry_price, price)
            return False

        decision = judge_tonosama_exit(
            symbol=symbol,
            entry_price=entry_price,
            current_price=policy_price,
            high_after_entry=inp.get("policy_high_after_entry"),
            vwap=inp.get("policy_vwap"),
            hold_seconds=holding_seconds,
            ranking_lost_minutes=ranking_lost_minutes,
            already_first_tp=bool(pos.get("tonosama_first_tp_done", False)),
            already_second_tp=bool(pos.get("tonosama_second_tp_done", False)),

            # optional: 5秒足。SELLは上昇/上抜けをBUY側の下落/割れに反転済み。
            bar5s_drop_pct=inp.get("bar5s_drop_pct"),
            bar5s_consecutive_down=inp.get("bar5s_consecutive_down"),
            bar5s_volume_ratio=inp.get("bar5s_volume_ratio"),
            bar5s_vwap_break=inp.get("bar5s_vwap_break"),
            bar5s_high_after_entry=inp.get("bar5s_high_after_entry"),
        )

        if not decision.should_exit:
            logger.debug(
                "[TONOSAMA EXIT HOLD] symbol=%s side=%s policy_side=%s price=%.4f policy_price=%.4f entry=%.4f pnl_pct=%.2f "
                "reason=%s 5s_drop=%s 5s_down=%s 5s_up=%s 5s_vol=%s 5s_vwap_break=%s 5s_vwap_above=%s",
                symbol,
                side_norm,
                inp.get("policy_side"),
                price,
                policy_price,
                entry_price,
                safe_float(getattr(decision, "pnl_pct", 0.0)),
                getattr(decision, "reason", "HOLD"),
                bar5s_features.get("bar5s_drop_pct"),
                bar5s_features.get("bar5s_consecutive_down"),
                bar5s_features.get("bar5s_consecutive_up"),
                bar5s_features.get("bar5s_volume_ratio"),
                bar5s_features.get("bar5s_vwap_break"),
                bar5s_features.get("bar5s_vwap_above"),
            )
            return False

        action = str(getattr(decision, "action", "EXIT_ALL") or "EXIT_ALL")
        reason = str(getattr(decision, "reason", "TONOSAMA_EXIT") or "TONOSAMA_EXIT")
        sell_ratio = safe_float(getattr(decision, "sell_ratio", 1.0), 1.0)
        pnl_pct = safe_float(getattr(decision, "pnl_pct", 0.0), 0.0)

        logger.warning(
            "[TONOSAMA EXIT] symbol=%s side=%s policy_side=%s action=%s ratio=%.2f price=%.4f policy_price=%.4f entry=%.4f "
            "pnl=%.4f policy_pnl_pct=%.2f real_high=%.4f real_low=%.4f real_vwap=%.4f policy_high=%.4f policy_vwap=%.4f hold_sec=%s "
            "5s_drop=%s 5s_down=%s 5s_up=%s 5s_vol=%s 5s_vwap_break=%s 5s_vwap_above=%s 5s_high=%s 5s_low=%s "
            "reason=%s dry_run=%s",
            symbol,
            side_norm,
            inp.get("policy_side"),
            action,
            sell_ratio,
            price,
            policy_price,
            entry_price,
            pnl,
            pnl_pct,
            high_after_entry,
            low_after_entry,
            vwap,
            safe_float(inp.get("policy_high_after_entry"), 0.0),
            safe_float(inp.get("policy_vwap"), 0.0),
            holding_seconds,
            bar5s_features.get("bar5s_drop_pct"),
            bar5s_features.get("bar5s_consecutive_down"),
            bar5s_features.get("bar5s_consecutive_up"),
            bar5s_features.get("bar5s_volume_ratio"),
            bar5s_features.get("bar5s_vwap_break"),
            bar5s_features.get("bar5s_vwap_above"),
            bar5s_features.get("bar5s_high_after_entry"),
            bar5s_features.get("bar5s_low_after_entry"),
            reason,
            TONOSAMA_EXIT_DRY_RUN,
        )

        if action == "EXIT_ALL":
            if TONOSAMA_EXIT_DRY_RUN:
                logger.warning(
                    "[TONOSAMA EXIT DRY_RUN] would execute full exit symbol=%s side=%s price=%.4f reason=%s",
                    symbol,
                    side_norm,
                    price,
                    reason,
                )
                return True

            logger.warning(
                "[TONOSAMA EXIT EXECUTE] symbol=%s side=%s price=%.4f reason=%s",
                symbol,
                side_norm,
                price,
                reason,
            )

            finalize_exit(
                symbol=symbol,
                price=price,
                reason=f"TONOSAMA:{side_norm}:{reason}",
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
            )
            return True

        logger.info(
            "[TONOSAMA EXIT] partial skipped symbol=%s side=%s action=%s ratio=%.2f "
            "reason=executor_full_exit_only original_reason=%s",
            symbol,
            side_norm,
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
