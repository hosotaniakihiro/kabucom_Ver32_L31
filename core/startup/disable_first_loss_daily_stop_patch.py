# ============================================================
# File   : core/startup/disable_first_loss_daily_stop_patch.py
# Version: Ver01-DISABLE-FIRST-LOSS-DAILY-STOP
# ------------------------------------------------------------
# ユーザー要望:
#   「全敗対策の日次停止ガード要らない」
#
# 対策:
#   1) SUMMARY AI executor の daily risk 事前除外をデフォルト無効化
#   2) entry_daily_risk_runtime_patch の SYMBOL_STOP_AFTER_FIRST_LOSS を無効化
#
# 残すもの:
#   - 価格帯フィルタ
#   - trade_restricted
#   - SELL reject cache
#   - 発注失敗時の一時クールダウン
#
# ENV:
#   DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS=1
#   SUMMARY_AI_PRE_FILTER_DAILY_RISK=0
#   ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS=0
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_RISK_BLOCK_REASON = None
_ORIG_EXECUTOR_DAILY_RISK_BLOCK_REASON = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _norm_reason(reason: Any) -> str:
    return str(reason or "").strip().upper()


def _patched_risk_block_reason(symbol: str, side: str) -> Tuple[bool, str, Dict[str, Any]]:
    if callable(_ORIG_RISK_BLOCK_REASON):
        blocked, reason, detail = _ORIG_RISK_BLOCK_REASON(symbol, side)
    else:
        blocked, reason, detail = False, "", {}

    if blocked and _norm_reason(reason) == "SYMBOL_STOP_AFTER_FIRST_LOSS" and _env_bool("DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS", True):
        if not isinstance(detail, dict):
            detail = {"detail": str(detail)}
        logger.warning(
            "[DISABLE FIRST LOSS DAILY STOP] allow symbol=%s side=%s original_reason=%s detail=%s",
            symbol,
            side,
            reason,
            detail,
        )
        return False, "", {
            "symbol": symbol,
            "side": side,
            "disabled_reason": reason,
            "original_detail": detail,
        }

    return bool(blocked), str(reason or ""), dict(detail or {}) if isinstance(detail, dict) else {"detail": str(detail)}


def _patched_executor_daily_risk_block_reason(symbol: str, side: str):
    # SUMMARY_AI_PRE_FILTER_DAILY_RISK=0 をデフォルトとし、候補選抜前の daily risk 除外を止める。
    if not _env_bool("SUMMARY_AI_PRE_FILTER_DAILY_RISK", False):
        return False, "", {}
    if callable(_ORIG_EXECUTOR_DAILY_RISK_BLOCK_REASON):
        blocked, reason, detail = _ORIG_EXECUTOR_DAILY_RISK_BLOCK_REASON(symbol, side)
        if blocked and _norm_reason(reason) == "SYMBOL_STOP_AFTER_FIRST_LOSS" and _env_bool("DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS", True):
            logger.warning(
                "[DISABLE FIRST LOSS DAILY STOP] executor prefilter allow symbol=%s side=%s original_reason=%s detail=%s",
                symbol,
                side,
                reason,
                detail,
            )
            return False, "", {}
        return blocked, reason, detail
    return False, "", {}


def install() -> bool:
    global _INSTALLED, _ORIG_RISK_BLOCK_REASON, _ORIG_EXECUTOR_DAILY_RISK_BLOCK_REASON
    if _INSTALLED:
        return True

    os.environ.setdefault("DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS", "1")
    os.environ.setdefault("SUMMARY_AI_PRE_FILTER_DAILY_RISK", "0")
    os.environ.setdefault("ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS", "0")

    ok_any = False

    try:
        from core.startup import entry_daily_risk_runtime_patch as daily_risk

        cur = getattr(daily_risk, "_risk_block_reason", None)
        if callable(cur) and not getattr(cur, "_disable_first_loss_daily_stop_patch", False):
            _ORIG_RISK_BLOCK_REASON = cur
            _patched_risk_block_reason._disable_first_loss_daily_stop_patch = True  # type: ignore[attr-defined]
            _patched_risk_block_reason._original = cur  # type: ignore[attr-defined]
            daily_risk._risk_block_reason = _patched_risk_block_reason
            ok_any = True
            logger.warning("[DISABLE FIRST LOSS DAILY STOP] patched entry_daily_risk_runtime_patch._risk_block_reason")
    except Exception:
        logger.exception("[DISABLE FIRST LOSS DAILY STOP] daily risk patch failed")

    try:
        import trading.entry.summary_ai.executor as executor

        cur = getattr(executor, "_daily_risk_block_reason", None)
        if callable(cur) and not getattr(cur, "_disable_first_loss_daily_stop_patch", False):
            _ORIG_EXECUTOR_DAILY_RISK_BLOCK_REASON = cur
            _patched_executor_daily_risk_block_reason._disable_first_loss_daily_stop_patch = True  # type: ignore[attr-defined]
            _patched_executor_daily_risk_block_reason._original = cur  # type: ignore[attr-defined]
            executor._daily_risk_block_reason = _patched_executor_daily_risk_block_reason
            ok_any = True
            logger.warning("[DISABLE FIRST LOSS DAILY STOP] patched summary_ai.executor._daily_risk_block_reason")
    except Exception:
        logger.exception("[DISABLE FIRST LOSS DAILY STOP] executor patch failed")

    _INSTALLED = bool(ok_any)
    logger.warning(
        "[DISABLE FIRST LOSS DAILY STOP] installed=%s disable_first_loss=%s summary_prefilter=%s entry_stop_after_first_loss=%s",
        _INSTALLED,
        _env_bool("DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS", True),
        _env_bool("SUMMARY_AI_PRE_FILTER_DAILY_RISK", False),
        _env_bool("ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS", False),
    )
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[DISABLE FIRST LOSS DAILY STOP] auto install failed")


__all__ = ["install"]
