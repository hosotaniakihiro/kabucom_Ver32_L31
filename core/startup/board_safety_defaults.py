# ============================================================
# File   : core/startup/board_safety_defaults.py
# ------------------------------------------------------------
# 旧 core/startup/board_missing_failopen_runtime_patch.py。
# 関数のモンキーパッチはもう1つも残っておらず、実体は
#   - 板なし発注を復活させないための env ハード代入 (安全側)
#   - entry_order_builder.ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY = True
#   - controller busy 時のリトライ env (旧 summary_ai_lock_retry_runtime_patch)
#   - trading/entry/summary_ai/current_df_context の連鎖install
#     (旧 final_board_guard_signature_compat_patch)
# だけなので、パッチではなく通常モジュールへ改名した (ロジックは無変更)。
#
# **実行位置は変えていない**: 同じ env/定数を
# summary_ai_strict_board_rest_patch / core/startup/strict_entry_defaults も
# 書くため後勝ちになる。main.py の patches リスト内の同じ位置で install() が
# 呼ばれ続けるようにしてある。
# ============================================================
# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/board_missing_failopen_runtime_patch.py
# Version: V1.8-DISABLED-SAFE-BOARD-REQUIRED-SIG-COMPAT
# ------------------------------------------------------------
# 以前の V1.6 は SUMMARY_AI の板なし発注を fail-open していた。
# 2026-07-01 の 429/API実行回数エラー・orig=wrapped 対策後は、
# final_entry_safety_guard v10 / board_retry v17 を優先し、このpatchは
# 板なし発注を再有効化しない安全no-opとして残す。
#
# V1.8:
#   - 後段patchで _board_guard が3引数版に戻り、
#     final_entry_safety_guard が4引数呼びで TypeError になる問題を防ぐため、
#     旧 final_board_guard_signature_compat_patch を常時installしていた。
#     同パッチは解体済みで、いまは current_df_context.install() を呼ぶ。
# ============================================================
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

VERSION = "V1.8-DISABLED-SAFE-BOARD-REQUIRED-SIG-COMPAT"
_INSTALLED = False


def _set_safe_defaults() -> None:
    """他patchから呼ばれても板なし発注を復活させない。"""
    os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK_FORCE", "1")
    os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD_FORCE", "0")
    os.environ["ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD"] = os.getenv("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_FORCE", "0")
    os.environ["ENTRY_SUMMARY_AI_FAST_BOARDLESS_ORDER"] = os.getenv("ENTRY_SUMMARY_AI_FAST_BOARDLESS_ORDER_FORCE", "0")
    os.environ.setdefault("ENTRY_BOARD_MISSING_POP_PENDING", "0")
    os.environ.setdefault("ENTRY_BOARD_MISSING_RETRYABLE", "1")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_COUNT", "0")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_COUNT", "0")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_WAIT_SEC", "0.05")


def _restore_final_guard_board_guard() -> bool:
    """
    既に旧V1.6が同一プロセス内で board guard を上書き済みの場合だけ、
    final_entry_safety_guard v10 の _board_guard へ戻す。
    新規プロセスでは通常何もしない。
    """
    # final_entry_safety_guard_patch は本文化済みで _board_guard を持たなくなったため、
    # ここでの復元対象は存在しない (旧V1.6が同一プロセス内で上書きしていた場合のみの処理)。
    # 板ガード本体は trading/handlers/entry_controller._final_guard_board_ok が担当する。
    return True


def _install_board_guard_signature_compat() -> bool:
    """旧 final_board_guard_signature_compat_patch の install 導線。

    同パッチは解体済み。中身の行方:
      - _apply() の _board_guard 署名互換化
          final_entry_safety_guard_patch が本文化済みで _board_guard を持たず、
          「target missing」で常に False を返す死にコードだったので移していない。
      - _install_summary_ai_low_move_prefilter_patch
          -> trading/summary/pipeline/entry_pipeline._allow_summary_ai_liquidity 本体
      - _install_entry_pipeline_exec_cap_patch
          -> trading/summary/pipeline/entry_pipeline._cap_summary_ai_exec_rows()
      - _install_current_df_freshness_patch
          -> runner のラップは trading/entry/summary_ai/current_df_context.py
             (読み手が candidate_refill 層 = ネイティブより外側なので畳めない)
             unsafe_reason 側は candidate_refill._current_df_freshness_ok() へ本文化
      - _install_summary_ai_selection_pool_patch
          -> **移していない**。summary_ai_runtime_consistency_patch の watcher が
             毎秒 _select_ai_ok_items を "protected" 版 (= ネイティブと同じ
             _selected_pool[:cap]) へ書き戻すため、実運用では一度も効いていない。
             実測でも定常状態の選択件数は cap どおり (pool 全量にならない)。
    """
    try:
        from trading.entry.summary_ai import current_df_context as sig

        ok = bool(sig.install())
        logger.warning("[BOARD MISSING FAILOPEN] board_guard_signature_compat installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] board_guard_signature_compat install failed")
        return False


def _patch_entry_order_builder_safe() -> bool:
    """entry_order_builder側も板なしfast fallbackへ変更しない。"""
    try:
        import trading.handlers.entry_order_builder as eob
        setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", True)
        return True
    except Exception:
        logger.debug("[BOARD MISSING FAILOPEN] entry_order_builder safe patch skipped", exc_info=True)
        return False


def _install_summary_ai_lock_retry() -> bool:
    """controller busy 時のリトライ設定を確定させる。

    旧 core/startup/summary_ai_lock_retry_runtime_patch.py を本文化。
    同パッチの関数差し替え
    (summary_ai_async_entry_patch._is_retryable_controller_busy) は読み手が
    1つも無く死んでいた (実際の判定は
    trading/entry/summary_ai/executor.py:_async_is_retryable_controller_busy)。
    実効は下記の env ハード代入だけなので、呼び出し位置を変えずにここへ移した。

    summary_ai_async_entry_patch は :41-45 で同じキーを setdefault し、
    install() 内 (:316-317) で STALE_SEC を 20 以下へクランプするが、
    この関数はその後に走るため 35 / 0.8 が最終値になる。
    実際の install 順をスタブ module で再現して確認済み。
    """
    try:
        os.environ["SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK"] = "1"
        os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY"] = "1"
        os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX"] = os.getenv("SUMMARY_AI_LOCK_RETRY_MAX", "3")
        os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC"] = os.getenv("SUMMARY_AI_LOCK_RETRY_SLEEP", "0.8")
        os.environ["SUMMARY_AI_ASYNC_ENTRY_STALE_SEC"] = os.getenv("SUMMARY_AI_LOCK_RETRY_STALE", "35")
        logger.warning(
            "[BOARD MISSING FAILOPEN] summary_ai_lock_retry env applied retry_max=%s sleep=%s stale=%s reentrant=%s",
            os.environ.get("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX"),
            os.environ.get("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC"),
            os.environ.get("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC"),
            os.environ.get("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK"),
        )
        return True
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] summary_ai_lock_retry env apply failed")
        return False


def _install_summary_entry_stale_rescue() -> bool:
    """fresh dispatch 直後の pending を stale で捨てない救済。

    旧 core/startup/summary_entry_stale_pending_rescue_patch.py は
    core/startup/summary_entry_pending_existing_fix_patch.py の
    _stale_summary_reason 本体へ統合済み (ヘルパは同ファイルの
    _stale_rescue_* が旧パッチの実装を保持)。事前installは不要になった。
    """
    return True


def install() -> bool:
    global _INSTALLED
    _set_safe_defaults()
    guard_ok = _restore_final_guard_board_guard()
    sig_ok = _install_board_guard_signature_compat()
    order_ok = _patch_entry_order_builder_safe()
    lock_retry_ok = _install_summary_ai_lock_retry()
    stale_rescue_ok = _install_summary_entry_stale_rescue()
    _INSTALLED = True
    logger.warning(
        "[BOARD MISSING FAILOPEN] disabled safe install=%s guard_ok=%s sig_ok=%s order_safe=%s lock_retry_ok=%s stale_rescue_ok=%s hard_block=%s allow_without_board=%s summary_ai_allow=%s fast_boardless=%s version=%s",
        _INSTALLED,
        guard_ok,
        sig_ok,
        order_ok,
        lock_retry_ok,
        stale_rescue_ok,
        os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK"),
        os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
        os.getenv("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD"),
        os.getenv("ENTRY_SUMMARY_AI_FAST_BOARDLESS_ORDER"),
        VERSION,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD MISSING FAILOPEN] auto install failed")


__all__ = ["VERSION", "install"]
