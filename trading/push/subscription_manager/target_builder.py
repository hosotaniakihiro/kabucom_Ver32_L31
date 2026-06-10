# ============================================================
# File   : trading/push/subscription_manager/target_builder.py
# Version: V1.2-RUNTIME-FALLBACK-WHEN-RANKING-EMPTY
# ------------------------------------------------------------
# Purpose:
#   - ranking_selector で最大100銘柄候補を作る
#   - 保有中/発注中/直近エントリー銘柄を優先追加
#   - filters を適用
#   - rotation.py で50銘柄に制限
#   - core.py に最終登録対象50銘柄を返す
#
# Fix V1.1:
#   - rotation_A / rotation_B では、rotation_core から渡された明示symbolsを
#     そのまま登録対象にする fast path を追加。
#   - これにより、50銘柄register直前に ranking DB / filters を毎回再計算して
#     60秒timeoutになる問題を避ける。
#
# Fix V1.2:
#   - ranking DB が空/未使用状態でも、runtime.monitor_symbols / push_symbols 等の
#     直近100銘柄を fallback として使う。
#   - on_open / reconnect 直後に ranking_selector empty -> target=0 となり、
#     登録済み銘柄を全解除する事故を防ぐ。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

from .filters import (
    SYMBOL_FLAGS_DB_PATH,
    apply_common_stock_filter,
    apply_freshness_filter,
    filter_by_symbol_flags_targets,
)
from .global_store import save_symbol_lists_to_global_data
from .globals_access import safe_get_global_data, safe_getattr
from .priority_symbols import (
    collect_open_position_symbols,
    collect_priority_push_symbols,
)
from .ranking_source import load_ranking_symbols
from .rotation import (
    REGISTER_CHUNK_SIZE,
    enforce_register_limit,
    normalize_symbols,
    select_target_by_reason,
    split_rotation_targets,
)
from .symbols import collect_symbols_from_explicit, dedupe_keep_order, limit_symbols

logger = logging.getLogger(__name__)

REGISTER_MAX_SYMBOLS = 100


def _is_rotation_reason(reason: Any) -> bool:
    try:
        s = str(reason or "").strip().lower()
        return s.startswith("rotation_") or s.startswith("push_rotation_") or s in {"rotation", "rotate"}
    except Exception:
        return False


def _merge_priority_symbols(
    priority_symbols,
    candidate_symbols,
    *,
    max_symbols: int,
) -> list[str]:
    priority = dedupe_keep_order(normalize_symbols(priority_symbols))
    candidates = dedupe_keep_order(normalize_symbols(candidate_symbols))

    merged = dedupe_keep_order(priority + candidates)

    if max_symbols and max_symbols > 0:
        merged = merged[: int(max_symbols)]

    return merged


def _runtime_symbol_attr_names() -> tuple[str, ...]:
    return (
        # active_symbol_manager / push_stream runtime seed 系
        "runtime_monitor_symbols",
        "monitor_symbols",
        "push_register_symbols",
        "register_symbols",
        "active_symbols",
        "active_push_symbols",
        # subscription/global_store 系
        "push_symbols",
        "subscription_targets",
        "ats_register_targets",
        "ats_targets",
        "should_register_symbols",
        "filtered_candidate_symbols",
        "buy_candidate_symbols",
    )


def _collect_runtime_symbols(max_symbols: int = REGISTER_MAX_SYMBOLS) -> list[str]:
    """
    ranking DB が空の瞬間でも、直近に構築済みの100銘柄を再利用する。

    ログ例:
      [PUSH RUNTIME] seeded push/register symbols ... count=100
    のように runtime 側には候補が残っていることがあるため、on_open / reconnect の
    fallback としてここを優先する。
    """
    gd = safe_get_global_data()
    if gd is None:
        return []

    for name in _runtime_symbol_attr_names():
        try:
            raw = safe_getattr(gd, name, None)
            symbols = collect_symbols_from_explicit(raw)
            symbols = dedupe_keep_order(normalize_symbols(symbols))
            if symbols:
                if max_symbols and max_symbols > 0:
                    symbols = symbols[: int(max_symbols)]
                logger.info(
                    "[SUB MANAGER TARGET] runtime fallback selected attr=%s count=%d head=%s",
                    name,
                    len(symbols),
                    symbols[:30],
                )
                return symbols
        except Exception:
            logger.debug("[SUB MANAGER TARGET] runtime fallback attr failed name=%s", name, exc_info=True)
            continue

    return []


def load_selected_ranking_symbols(max_symbols: int = REGISTER_MAX_SYMBOLS) -> list[str]:
    """
    ranking_selector を優先。
    失敗時だけ従来 ranking_source.load_ranking_symbols() へfallback。
    """
    try:
        from .ranking_selector import build_push_ranking_symbols

        selected = build_push_ranking_symbols(max_symbols=max_symbols)
        selected = dedupe_keep_order(normalize_symbols(selected))

        if selected:
            logger.info(
                "[SUB MANAGER TARGET] ranking_selector selected count=%d head=%s",
                len(selected),
                selected[:30],
            )
            return selected
        logger.warning(
            "[SUB MANAGER TARGET] ranking_selector returned empty -> fallback"
        )

    except Exception:
        logger.exception(
            "[SUB MANAGER TARGET] ranking_selector failed -> fallback"
        )

    try:
        fallback = load_ranking_symbols(limit=max_symbols)
        fallback = dedupe_keep_order(normalize_symbols(fallback))
        logger.info(
            "[SUB MANAGER TARGET] fallback ranking symbols count=%d head=%s",
            len(fallback),
            fallback[:30],
        )
        return fallback
    except Exception:
        logger.exception("[SUB MANAGER TARGET] fallback load_ranking_symbols failed")
        return []


def build_target_symbols(
    symbols: Any = None,
    *,
    max_symbols: int = REGISTER_MAX_SYMBOLS,
    reason: str = "manual",
) -> list[str]:
    """
    株ステーションPUSH登録対象を作る。

    戻り値:
      必ず50件以内の銘柄リスト。
    """
    explicit = collect_symbols_from_explicit(symbols)
    explicit = dedupe_keep_order(normalize_symbols(explicit))

    # rotation_core はすでに100銘柄候補からA/B各50銘柄を切り出して渡す。
    # ここでランキングDBやfreshness filterを再実行すると、登録APIに到達する前に
    # register timeout を消費するため、rotation時は明示symbolsをそのまま使う。
    if _is_rotation_reason(reason) and explicit:
        selected_push_symbols = enforce_register_limit(
            explicit,
            register_chunk_size=REGISTER_CHUNK_SIZE,
            reason=reason,
        )
        logger.info(
            "[SUB MANAGER TARGET] rotation explicit fastpath reason=%s selected=%d head=%s",
            reason,
            len(selected_push_symbols),
            selected_push_symbols[:30],
        )
        save_symbol_lists_to_global_data(
            raw_symbols=selected_push_symbols,
            buy_symbols=selected_push_symbols,
            sell_symbols=[],
            filtered_symbols=selected_push_symbols,
            ranking_symbols=[],
            rotation_a_symbols=selected_push_symbols if str(reason).lower().endswith("_a") else [],
            rotation_b_symbols=selected_push_symbols if str(reason).lower().endswith("_b") else [],
            priority_symbols=[],
            position_symbols=[],
        )
        return selected_push_symbols

    runtime_symbols = _collect_runtime_symbols(max_symbols=max_symbols)

    ranking_symbols = load_selected_ranking_symbols(max_symbols=max_symbols)
    ranking_symbols = apply_common_stock_filter(
        ranking_symbols,
        db_path=SYMBOL_FLAGS_DB_PATH,
    )
    ranking_symbols = apply_freshness_filter(ranking_symbols)
    ranking_symbols = dedupe_keep_order(normalize_symbols(ranking_symbols))

    priority_symbols = collect_priority_push_symbols()
    position_symbols = collect_open_position_symbols()

    priority_filtered = apply_common_stock_filter(
        priority_symbols,
        db_path=SYMBOL_FLAGS_DB_PATH,
    )
    priority_filtered = apply_freshness_filter(priority_filtered)

    if priority_symbols and not priority_filtered:
        logger.warning(
            "[SUB MANAGER TARGET] priority symbols were fully filtered out. fallback raw priority=%s",
            priority_symbols[:50],
        )
        priority_filtered = list(priority_symbols)

    priority_filtered = dedupe_keep_order(normalize_symbols(priority_filtered))

    if ranking_symbols:
        raw_symbols = dedupe_keep_order(list(priority_filtered) + list(ranking_symbols))
        source_name = "priority_plus_selected_ranking"
    else:
        fallback = dedupe_keep_order(list(priority_filtered) + list(explicit) + list(runtime_symbols))
        raw_symbols = apply_common_stock_filter(fallback, db_path=SYMBOL_FLAGS_DB_PATH)
        raw_symbols = apply_freshness_filter(raw_symbols)
        if priority_filtered:
            raw_symbols = dedupe_keep_order(list(priority_filtered) + list(raw_symbols))
        if not raw_symbols and runtime_symbols:
            # reconnect直後はfreshness/summary側が未復元のことがある。
            # runtime側は直近ACTIVE/PUSHで作った候補なので、ここでは最後の砦として採用する。
            raw_symbols = list(runtime_symbols)
            source_name = "priority_plus_runtime_fallback_unfiltered"
            logger.warning(
                "[SUB MANAGER TARGET] runtime fallback used without filters reason=%s count=%d head=%s",
                reason,
                len(raw_symbols),
                raw_symbols[:30],
            )
        elif runtime_symbols:
            source_name = "priority_plus_fallback_explicit_runtime"
        else:
            source_name = "priority_plus_fallback_explicit"

    raw_symbols = dedupe_keep_order(normalize_symbols(raw_symbols))

    buy_symbols, sell_symbols = filter_by_symbol_flags_targets(
        raw_symbols,
        db_path=SYMBOL_FLAGS_DB_PATH,
    )

    buy_symbols = dedupe_keep_order(normalize_symbols(buy_symbols))
    sell_symbols = dedupe_keep_order(normalize_symbols(sell_symbols))

    if not buy_symbols and runtime_symbols and not ranking_symbols:
        # symbol_flags が一時的に読めない場合でも reconnect でtarget=0にしない。
        buy_symbols = dedupe_keep_order(normalize_symbols(runtime_symbols))
        logger.warning(
            "[SUB MANAGER TARGET] buy_symbols empty -> runtime fallback promoted reason=%s count=%d head=%s",
            reason,
            len(buy_symbols),
            buy_symbols[:30],
        )

    push_symbols_all = _merge_priority_symbols(
        priority_filtered,
        buy_symbols,
        max_symbols=max_symbols,
    )

    sell_symbols = limit_symbols(sell_symbols, max_symbols=max_symbols)

    rotation_a_symbols, rotation_b_symbols = split_rotation_targets(
        push_symbols_all,
        priority_symbols=priority_filtered,
        register_chunk_size=REGISTER_CHUNK_SIZE,
    )

    selected_push_symbols = select_target_by_reason(
        push_symbols_all,
        reason=reason,
        priority_symbols=priority_filtered,
        register_chunk_size=REGISTER_CHUNK_SIZE,
    )

    selected_push_symbols = enforce_register_limit(
        selected_push_symbols,
        register_chunk_size=REGISTER_CHUNK_SIZE,
        reason=reason,
    )

    excluded = [
        s for s in raw_symbols
        if s not in set(push_symbols_all) and s not in set(sell_symbols)
    ]

    priority_missing_from_selected = [
        s for s in priority_filtered
        if s not in set(selected_push_symbols)
    ]

    if priority_missing_from_selected:
        logger.warning(
            "[SUB MANAGER TARGET] priority missing from selected reason=%s missing=%s selected_size=%d",
            reason,
            priority_missing_from_selected[:50],
            len(selected_push_symbols),
        )

    save_symbol_lists_to_global_data(
        raw_symbols=raw_symbols,
        buy_symbols=push_symbols_all,
        sell_symbols=sell_symbols,
        filtered_symbols=selected_push_symbols,
        ranking_symbols=ranking_symbols,
        rotation_a_symbols=rotation_a_symbols,
        rotation_b_symbols=rotation_b_symbols,
        priority_symbols=priority_filtered,
        position_symbols=position_symbols,
    )

    logger.info("[SUB MANAGER TARGET] source=%s reason=%s", source_name, reason)
    logger.info(
        "[SUB MANAGER TARGET] runtime fallback count=%d symbols=%s",
        len(runtime_symbols),
        runtime_symbols[:50],
    )
    logger.info(
        "[SUB MANAGER TARGET] priority count=%d symbols=%s",
        len(priority_filtered),
        priority_filtered[:50],
    )
    logger.info("[SUB MANAGER TARGET] raw candidate count=%d", len(raw_symbols))
    logger.info("[SUB MANAGER TARGET] selected ranking candidate count=%d", len(ranking_symbols))
    logger.info(
        "[SUB MANAGER TARGET] final counts buy_all=%d sell=%d rotation_A=%d rotation_B=%d selected=%d",
        len(push_symbols_all),
        len(sell_symbols),
        len(rotation_a_symbols),
        len(rotation_b_symbols),
        len(selected_push_symbols),
    )
    logger.info("[SUB MANAGER TARGET] excluded count=%d symbols=%s", len(excluded), excluded[:50])
    logger.info("[SUB MANAGER TARGET] selected target symbols=%s", selected_push_symbols[:50])

    return selected_push_symbols


__all__ = [
    "REGISTER_MAX_SYMBOLS",
    "build_target_symbols",
    "load_selected_ranking_symbols",
]
