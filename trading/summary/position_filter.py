# ============================================================
# File   : trading/summary/position_filter.py
# Version: Ver25.2-FINAL-ALLOW-ORDERS-RUNTIME-FALLBACK
# ------------------------------------------------------------
# ✔ SUMMARY / RANKING でルールを分離（思想維持）
# ✔ SUMMARY は STOP高安フィルタを緩和
# ✔ RANKING は従来どおり厳格
# ✔ 両建て・多重ENTRY完全防止
# ✔ open_positions 正統一本化
# ✔ allow_orders False の原因を必ずログ表示
# ✔ 起動時パッチが効かない場合でも env / dry-run を見て allow_orders を復旧
# ============================================================

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from global_state import global_data
from utils_common import safe_float

logger = logging.getLogger("position_filter")

_TRUE_VALUES = {"1", "true", "yes", "on", "y", "live", "real"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "dry", "dryrun", "paper"}


# ============================================================
# 共通ヘルパ
# ============================================================
def _norm(v: Any) -> str:
    try:
        return str(v).strip().lower()
    except Exception:
        return ""


def _env_bool(name: str, default: bool | None = None) -> bool | None:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    s = _norm(v)
    if s in _TRUE_VALUES:
        return True
    if s in _FALSE_VALUES:
        return False
    return default


def _dry_run_requested() -> tuple[bool, str]:
    for key in (
        "DRY_RUN",
        "ENTRY_DRY_RUN",
        "ORDER_DRY_RUN",
        "KABU_DRY_RUN",
        "SUMMARY_ENTRY_DRY_RUN",
        "PAPER_TRADE",
    ):
        v = _env_bool(key, None)
        if v is True:
            return True, key
    return False, ""


def _resolve_allow_orders_runtime() -> tuple[bool, str]:
    """
    position_filter 側の最終防衛。

    GlobalContext 初期値 allow_orders=False や起動時パッチ未適用で
    SUMMARY AI が全件 POSITION_FILTER_NG になるのを防ぐ。

    安全側:
      - dry-run 系 env が 1/true なら必ず False。
      - ALLOW_ORDERS 系 env が false なら必ず False。
      - ALLOW_ORDERS 系 env が true なら True。
      - 未設定時は実運用前提で True。
    """
    dry, dry_key = _dry_run_requested()
    if dry:
        return False, f"{dry_key}=true"

    for key in (
        "ALLOW_ORDERS",
        "ENTRY_ALLOW_ORDERS",
        "KABU_ALLOW_ORDERS",
        "LIVE_ORDERS",
        "ORDER_LIVE",
    ):
        v = _env_bool(key, None)
        if v is True:
            return True, f"{key}=true"
        if v is False:
            return False, f"{key}=false"

    return True, "default_live_true"


def _ensure_allow_orders_if_possible(symbol: str, side: str, source: str) -> bool:
    try:
        current = bool(getattr(global_data, "allow_orders", False))
        if current:
            return True

        allow, reason = _resolve_allow_orders_runtime()
        if allow:
            try:
                setattr(global_data, "allow_orders", True)
            except Exception:
                pass
            try:
                from core.global_context.context import global_context

                setattr(global_context, "allow_orders", True)
            except Exception:
                pass

            logger.warning(
                "✅ ENTRY許可へ復旧（allow_orders False -> True）: %s side=%s source=%s reason=%s",
                symbol,
                side,
                source,
                reason,
            )
            return True

        logger.info(
            "⛔ ENTRY禁止（allow_orders=False）: %s side=%s source=%s reason=%s env_ALLOW_ORDERS=%s env_DRY_RUN=%s env_ENTRY_DRY_RUN=%s env_ORDER_DRY_RUN=%s",
            symbol,
            side,
            source,
            reason,
            os.environ.get("ALLOW_ORDERS"),
            os.environ.get("DRY_RUN"),
            os.environ.get("ENTRY_DRY_RUN"),
            os.environ.get("ORDER_DRY_RUN"),
        )
        return False

    except Exception:
        logger.exception("[position_filter] allow_orders resolve failed symbol=%s side=%s source=%s", symbol, side, source)
        return False


def _get_latest_price(symbol: str):
    tick = global_data.get_latest_tick(symbol)
    if not tick:
        return None
    return safe_float(tick.get("price"))


def _get_limit_prices(symbol: str):
    tick = global_data.get_latest_tick(symbol)
    if not tick:
        return None, None
    return (
        safe_float(tick.get("limit_up")),
        safe_float(tick.get("limit_down")),
    )


def _is_stop_price_area(symbol: str, side: str) -> bool:
    """
    STOP高 / STOP安 の 2% 手前判定
    """
    price = _get_latest_price(symbol)
    limit_up, limit_down = _get_limit_prices(symbol)

    if not price:
        return False

    side = side.upper()

    if side == "BUY" and limit_up:
        return price >= limit_up * 0.98

    if side == "SELL" and limit_down:
        return price <= limit_down * 1.02

    return False


# ============================================================
# ENTRY 可否判定（★中枢）
# ============================================================
def can_entry_symbol(
    symbol: str,
    side: str,
    *,
    source: str = "SUMMARY",
) -> bool:
    """
    source:
        - "SUMMARY"  : サマリー由来（STOP高安は緩和）
        - "RANKING"  : ランキング由来（STOP高安は厳格）
    """

    symbol = str(symbol)
    side = side.upper()
    source = source.upper()

    # --------------------------------------------------------
    # システム停止中（★最大の詰まりポイント）
    # --------------------------------------------------------
    if not _ensure_allow_orders_if_possible(symbol, side, source):
        return False

    # --------------------------------------------------------
    # クールダウン（再ENTRY禁止）
    # --------------------------------------------------------
    cooldown_until = global_data.entry_cooldown_until.get(symbol)
    if cooldown_until:
        if datetime.now() < cooldown_until:
            logger.info(
                "⛔ ENTRY禁止（クールダウン中）: %s until=%s source=%s",
                symbol, cooldown_until, source,
            )
            return False
        else:
            global_data.entry_cooldown_until.pop(symbol, None)
            logger.info(
                "✅ クールダウン解除: %s source=%s",
                symbol, source,
            )

    # --------------------------------------------------------
    # 既存ポジション（同一・反対方向とも禁止）
    # --------------------------------------------------------
    if symbol in global_data.open_positions:
        logger.info(
            "⛔ ENTRY禁止（既存ポジあり）: %s source=%s",
            symbol, source,
        )
        return False

    # --------------------------------------------------------
    # inflight（二重発注防止）
    # --------------------------------------------------------
    if symbol in global_data.entry_inflight:
        logger.info(
            "⛔ ENTRY禁止（inflight）: %s source=%s",
            symbol, source,
        )
        return False

    # --------------------------------------------------------
    # STOP高安フィルタ
    # --------------------------------------------------------
    if source == "RANKING":
        # 🔥 ランキングは厳格
        if _is_stop_price_area(symbol, side):
            logger.info(
                "⛔ RANKING ENTRY禁止（STOP高安2%%手前）: %s side=%s",
                symbol, side,
            )
            return False
    else:
        # 🔥 SUMMARY はログのみ（許可）
        if _is_stop_price_area(symbol, side):
            logger.info(
                "⚠ SUMMARY STOP高安圏内だが許可: %s side=%s",
                symbol, side,
            )

    # --------------------------------------------------------
    # 最終 OK
    # --------------------------------------------------------
    logger.info(
        "✅ ENTRY許可: %s side=%s source=%s",
        symbol, side, source,
    )
    return True
