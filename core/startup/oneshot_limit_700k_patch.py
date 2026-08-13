# ============================================================
# File   : core/startup/oneshot_limit_700k_patch.py
# Version: Ver25-ENTRY-BUDGET-700K-ENV-OVERRIDE
# ------------------------------------------------------------
# 起動時 runtime patches:
# - PUSH bootstrap fast restore: 起動時PUSH DB復元を軽量化
# - OPEN POSITION stale DB cleanup: broker側に無い古いDB建玉を自動CLOSED化
# - OPEN POSITION sync throttle: 建玉なし時のbroker同期/API/長文ログを間引く
# - ENTRY fresh quote guard: 発注直前の板鮮度を確認し古い価格注文を止める
# - 70万円ワンショット制限
# - ENTRY数量0株の最低100株フォールバック
# - BUYエントリー閾値を後場スコアに合わせて緩和
# - SUMMARY AI daily risk / executed判定 / 売建不可候補除外
# - SUMMARY AI pre-order dedupe/cooldown無効化
# - SUMMARY AI async queue: busy時に候補を捨てずキュー処理
# - SUMMARY AI strict liquidity: 直近1本/平均出来高も必須
# - WATCHLIST recent liquidity: 監視銘柄選定時点で直近1本/平均/売買代金を必須化
# - WATCHLIST recent liquidity bulk: NAS summary DBを銘柄ごとではなく一括読取
# - SUMMARY parallel intervals: 1m/3m/5mを並列実行
# - ENTRY pipeline bucket prefilter: interval/source違いpendingを事前除外
# - SUMMARY_ENTRY duplicate pending を registered 扱いにする
# - EXIT 利益保護 / 板対応
#
# Ver25:
# - settings.ini の [trade]/[ENTRY] が 500,000 円のままでも、ユーザー方針の
#   1銘柄70万円を runtime ENV で優先する。
# - entry_budget._cfg は ENV を settings.ini より優先するため、
#   ENTRY_LOW_PRICE_ONESHOT_YEN / ENTRY_HIGH_PRICE_ONESHOT_YEN / MAX_ENTRY_ONESHOT_YEN
#   を起動時に 700000 へ補正する。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


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


def _install_entry_budget_700k_env_override() -> bool:
    """1銘柄70万円を entry_budget のENV優先経路へ注入する。"""
    try:
        # 既に明示ENV指定がある場合は尊重する。settings.ini の古い50万円だけを上書きする目的。
        os.environ.setdefault("MAX_ENTRY_ONESHOT_YEN", "700000")
        os.environ.setdefault("ENTRY_LOW_PRICE_ONESHOT_YEN", "700000")
        os.environ.setdefault("ENTRY_HIGH_PRICE_ONESHOT_YEN", "700000")
        os.environ.setdefault("ENTRY_MAX_PRICE", "7000")
        os.environ.setdefault("ORDER_LOT_SIZE", "100")
        os.environ.setdefault("ENTRY_AFFORDABILITY_FILTER_ENABLED", "1")
        logger.warning(
            "[ONESHOT LIMIT PATCH] entry budget env ensured max=%s low=%s high=%s max_price=%s lot=%s",
            os.getenv("MAX_ENTRY_ONESHOT_YEN"),
            os.getenv("ENTRY_LOW_PRICE_ONESHOT_YEN"),
            os.getenv("ENTRY_HIGH_PRICE_ONESHOT_YEN"),
            os.getenv("ENTRY_MAX_PRICE"),
            os.getenv("ORDER_LOT_SIZE"),
        )
        try:
            from trading.entry.entry_budget import log_entry_budget_config
            log_entry_budget_config(prefix="[ENTRY AFFORDABILITY PATCH]")
        except Exception:
            logger.debug("[ONESHOT LIMIT PATCH] entry budget config log skipped", exc_info=True)
        return True
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] entry budget env override failed")
        return False


def _install_push_bootstrap_fast_restore_patch() -> bool:
    """PUSH bootstrap 高速復元の env 既定値を設定する。

    復元ロジック自体は core/startup/push/bootstrap.py._load_push_db へ
    本文化済み (スコープは同ファイルの _push_fast_scope_enabled() が再現)。
    ここは既定値の設定タイミングを従来どおりに保つために残している。
    """
    try:
        os.environ.setdefault("PUSH_BOOTSTRAP_FAST_RESTORE", "1")
        os.environ.setdefault("PUSH_BOOTSTRAP_FAST_MAX_ROWS", "3000")
        os.environ.setdefault("PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES", "45")
        return True
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] push bootstrap fast restore env defaults failed")
        return False


def _install_open_position_stale_db_cleanup_patch() -> bool:
    """open_position_stale_db_cleanup_patch は trading/position/open_position_sync.py へ本文化済み。

    native <- broker_merge <- stale_db_cleanup <- sync_throttle の3層を
    順序を保って畳んである (スコープは _ops_scope_enabled() が再現)。
    """
    return True


def _install_open_position_sync_throttle_patch() -> bool:
    """open_position_sync_throttle_patch は trading/position/open_position_sync.py へ本文化済み。

    native <- broker_merge <- stale_db_cleanup <- sync_throttle の3層を
    順序を保って畳んである (スコープは _ops_scope_enabled() が再現)。
    """
    return True


def _install_entry_fresh_quote_guard_patch() -> bool:
    try:
        os.environ.setdefault("ENTRY_FRESH_QUOTE_GUARD_ENABLED", "1")
        os.environ.setdefault("ENTRY_MAX_QUOTE_AGE_SEC", "8.0")
        os.environ.setdefault("ENTRY_ALLOW_QUOTE_WITHOUT_TIMESTAMP", "0")
        # entry_fresh_quote_guard_patch は kabu_api/buy_sell_entry.py の
        # get_latest_bid_ask へ本文化済み (_fqg_*)。
        # 上の env 既定値だけここで入れておけばよい
        # (有効/無効は同ファイルが ENTRY_FRESH_QUOTE_GUARD_ENABLED を見る)。
        logger.warning("[ONESHOT LIMIT PATCH] entry_fresh_quote_guard is inlined; env defaults applied")
        return True
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] entry_fresh_quote_guard_patch install failed")
        return False


def _install_entry_threshold_patch() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        old_buy_score = getattr(ec, "MIN_SUMMARY_SCORE_BUY", None)
        old_buy_comp = getattr(ec, "MIN_COMPOSITE_SCORE_BUY", None)
        old_sell_score = getattr(ec, "MIN_SUMMARY_SCORE_SELL", None)
        old_sell_comp = getattr(ec, "MIN_COMPOSITE_SCORE_SELL", None)
        ec.MIN_SUMMARY_SCORE_BUY = _env_float("MIN_SUMMARY_SCORE_BUY", 1.0)
        ec.MIN_COMPOSITE_SCORE_BUY = _env_float("MIN_COMPOSITE_SCORE_BUY", 0.8)
        ec.MIN_SUMMARY_SCORE_SELL = _env_float("MIN_SUMMARY_SCORE_SELL", 1.0)
        ec.MIN_COMPOSITE_SCORE_SELL = _env_float("MIN_COMPOSITE_SCORE_SELL", 1.0)
        logger.warning("[ENTRY THRESHOLD PATCH] installed BUY score %s->%s comp %s->%s SELL score %s->%s comp %s->%s", old_buy_score, ec.MIN_SUMMARY_SCORE_BUY, old_buy_comp, ec.MIN_COMPOSITE_SCORE_BUY, old_sell_score, ec.MIN_SUMMARY_SCORE_SELL, old_sell_comp, ec.MIN_COMPOSITE_SCORE_SELL)
        return True
    except Exception:
        logger.exception("[ENTRY THRESHOLD PATCH] install failed")
        return False


def _install_entry_qty_minlot_patch() -> bool:
    """最小単元フォールバックの env 既定値。

    entry_qty_min_lot_runtime_patch は3責務に分割して本文化済み
    (数量フォールバック -> trading/handlers/entry_controller.py、
     表示整形 -> core/global_context/context.py、
     しきい値 env -> main.py)。
    ここに残しているのは、この env 既定値を従来と同じ位置で設定するため。
    """
    try:
        os.environ.setdefault("ENTRY_MIN_LOT_FALLBACK_WHEN_AFFORDABLE", "1")
        return True
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] entry qty minlot env default failed")
        return False


def _install_aligned_summary_display_patch() -> bool:
    try:
        from core.startup import aligned_summary_display_patch as p  # type: ignore
        ok = bool(p.install()) if hasattr(p, "install") else False
        logger.warning("[ONESHOT LIMIT PATCH] aligned_summary_display_patch installed=%s", ok)
        return ok
    except Exception:
        logger.debug("[ONESHOT LIMIT PATCH] aligned_summary_display_patch module not found; skip", exc_info=False)
        return False


# push_flush_auto_recover_patch は削除した。実体は trading/push/push_stream/monitor.py
# ._monitor_worker の setattr 差し替えのみだったが、trading/push/push_stream/runner.py が
# 起動時に `from .monitor import _monitor_worker` でこの関数を名前解決していたため
# (このパッチのinstall()が走るより前に完了する)、setattr後もrunner.py側の束縛は
# 差し替え前の元関数を指したままだった。実際に import 順序をシミュレートし、
# monitor._monitor_worker が置き換わった後も runner._monitor_worker は
# 旧来のオリジナル (現在の trading/push/push_stream/monitor.py Ver1.5、
# WS stale watchdog・memory-only判定込みの上位互換) を指し続けることを確認済み
# (=このパッチは実運用で一度も効いていなかった)。

# summary_ai_daily_risk_symbol_only_patch は削除した。ラップ対象の置換後ロジックは
# 「旧 entry_daily_risk_runtime_patch._risk_block_reason がインライン化で消滅済みのため
# 常に fail-open (False, "", {}) を返すだけ」であり、これは
# trading/entry/summary_ai/executor.py の現行ネイティブ _daily_risk_block_reason と
# 入出力が完全に一致することを確認済み (実質的に無効化のみを目的とした空パッチ)。


def _install_summary_ai_executor_result_patch() -> bool:
    # summary_ai_executor_result_patch は削除した。
    # あのパッチは trading/entry/summary_ai/executor へ
    # _is_positive_order_result という「本体に存在しない」属性を新設していただけで、
    # executor.py もその他のモジュールもこの名前を一度も読まない (git grep 済み)。
    # 実際に使われている厳格判定は executor._dd_strict_result_executed (ネイティブ) と、
    # entry_pipeline / summary_entry の _result_executed
    # (summary_ai_entry_controller_bridge_patch が差し替え) の方。
    return True


# summary_ai_sell_credit_prefilter_patch は trading/entry/summary_ai/executor.py の
# _is_sell_reject_cached へ本文化済みのため削除 (SELL却下キャッシュ判定 + 信用売り
# 可否ガードを公開関数へ直接実装)。


def _install_summary_ai_async_entry_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", "0")
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", "20")
        from core.startup import summary_ai_async_entry_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_async_entry_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_async_entry_patch install failed")
        return False


# summary_ai_dedupe_fix_runtime_patch は trading/entry/summary_ai/runner.py の
# _apply_optional_entry_dedupe_guard へ本文化済みのため削除 (SUMMARY_AI_PREORDER_DEDUPE_ENABLED
# 既定OFF時はdedupe/cooldown判定を丸ごとスキップする挙動を公開関数へ直接実装)。


def _install_strict_liquidity_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_STRICT_LIQ_ENABLED", "1")
        os.environ.setdefault("SUMMARY_AI_LIQ_MIN_LATEST_VOLUME", "30000")
        os.environ.setdefault("SUMMARY_AI_LIQ_MIN_AVG_VOLUME", "30000")
        # summary_ai_strict_liquidity_extra_patch は削除した。
        # 実体は executor._filter_blocked_ai_ok_items のラップだけで、
        # core filter watcher に即座に剥がされる死にコードだった。
        # 上の env 3 件は他に読み手が無いが、運用値として残しておく。
        return True
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_strict_liquidity_patch install failed")
        return False


def _install_watchlist_recent_liquidity_patch() -> bool:
    try:
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_ENABLED", "1")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", "3000")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", "3000")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", "1000000")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_BARS", "5")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_PROTECT_BYPASS", "1")
        from core.startup import watchlist_recent_liquidity_guard_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] watchlist_recent_liquidity_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] watchlist_recent_liquidity_patch install failed")
        return False


def _install_watchlist_recent_liquidity_bulk_patch() -> bool:
    try:
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_CACHE_TTL_SEC", "10")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_BULK_LOG_SEC", "0.5")
        from core.startup import watchlist_recent_liquidity_bulk_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] watchlist_recent_liquidity_bulk_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] watchlist_recent_liquidity_bulk_patch install failed")
        return False


def _install_summary_parallel_intervals_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_PARALLEL_INTERVALS_ENABLED", "1")
        os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_WORKERS", "3")
        os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", "55")
        os.environ.setdefault("SUMMARY_PARALLEL_RANKING_ENABLED", "1")
        from core.startup import summary_parallel_intervals_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_parallel_intervals_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_parallel_intervals_patch install failed")
        return False


def _install_entry_pipeline_bucket_filter_patch() -> bool:
    # entry_controller_pipeline_bucket_filter_patch は本文化済みで空スタブになっており、
    # 実体は env 既定値の設定 + summary_ai_atr_failopen_patch の install だけの
    # 中継だったため、その中身をここへ移して直接呼ぶ。
    try:
        os.environ.setdefault("ENTRY_PIPELINE_BUCKET_PREFILTER", "1")
        os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_ENABLED", "1")
        os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_VOLUME", "30000")
        os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_TURNOVER", "10000000")
        os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_PRICE", "1500")
        # summary_ai_atr_failopen_patch は削除した。
        # 実 install 順で測定した結果、同パッチが包む
        # entry_controller.atr_1m_filter は main.py:275 の
        # volatility_filter_tonosama_entryrow_rescue_patch が
        # native まで巻き戻して被せ直すため、この層は捨てられていた
        # (main.py:250 -> ここ, main.py:275 -> tonosama の順)。
        # 復元する側も居ないので完全な死にラッパー。
        # env 既定値 (上の setdefault) は他所でも読まれるため残してある。
        return True
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] entry pipeline bucket filter defaults failed")
        return False


def _install_summary_entry_pending_existing_fix_patch() -> bool:
    """summary_entry_pending_existing_fix_patch は
    trading/summary/summary_entry.register_pending_entries へ本文化済み
    (native <- existing_fix <- direct_timeout_continue の2層。
     スコープは同ファイルの _rpe_scope_enabled() が再現)。
    """
    return True


def _install_entry_direction_failopen_patch() -> bool:
    # entry_direction_failopen_runtime_patch (Ver05) は
    # trading/entry/direction_confirm.check_entry_direction_confirm() へ本文化済み。
    # pure guard を _check_entry_direction_confirm_core() へ退避し、公開版が
    # fail-open / 逆トレンド反転レイヤになっている (旧 install 後と同じ合成)。
    # env 既定値だけは install 時に効いていたのでここへ残す。
    os.environ.setdefault("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI", "1")
    return True


def _install_exit_profit_protect_patch() -> bool:
    """exit_profit_protect_runtime_patch は
    trading/handlers/exit_handler.py へ本文化済み
    (check_normal_exit / check_tonosama_exit の利確保護レイヤ。
     順序は native <- normal_exit_scalp_align <- exit_profit_protect)。
    """
    return True


def _install_exit_board_profit_patch() -> bool:
    try:
        os.environ.setdefault("EXIT_BOARD_PROFIT_ENABLED", "1")
        os.environ.setdefault("EXIT_BOARD_MIN_PROFIT_PCT", "0.0010")
        os.environ.setdefault("EXIT_BOARD_THICK_QTY_MULT", "3.0")
        os.environ.setdefault("EXIT_BOARD_MIN_THICK_QTY", "1000")
        os.environ.setdefault("EXIT_BOARD_MAX_WAIT_SEC", "2.0")
        os.environ.setdefault("EXIT_BOARD_POLL_SEC", "0.25")
        os.environ.setdefault("EXIT_BOARD_REVERSE_GAP_PCT", "0.0010")
        from core.startup import exit_board_profit_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] exit_board_profit_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] exit_board_profit_patch install failed")
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    ok_budget_700k = _install_entry_budget_700k_env_override()

    ok_push_fast_restore = _install_push_bootstrap_fast_restore_patch()
    ok_stale_db_cleanup = _install_open_position_stale_db_cleanup_patch()
    ok_open_pos_throttle = _install_open_position_sync_throttle_patch()
    ok_fresh_quote = _install_entry_fresh_quote_guard_patch()

    ok_async_entry = _install_summary_ai_async_entry_patch()
    # summary_ai_dedupe_fix_runtime_patch は runner.py へ本文化済み (上記コメント参照)。
    ok_strict_liq = _install_strict_liquidity_patch()
    ok_watchlist_liq = _install_watchlist_recent_liquidity_patch()
    ok_watchlist_bulk = _install_watchlist_recent_liquidity_bulk_patch()
    ok_parallel = _install_summary_parallel_intervals_patch()
    ok_bucket_filter = _install_entry_pipeline_bucket_filter_patch()
    ok_pending_existing = _install_summary_entry_pending_existing_fix_patch()
    ok_direction_failopen = _install_entry_direction_failopen_patch()
    ok_exit_profit = _install_exit_profit_protect_patch()
    ok_exit_board = _install_exit_board_profit_patch()

    ok_display = _install_aligned_summary_display_patch()
    ok_threshold = _install_entry_threshold_patch()
    ok_qty_minlot = _install_entry_qty_minlot_patch()
    # push_flush_auto_recover_patch は削除済み (上記コメント参照。実運用で無効だったことを確認済み)。

    ok_main = False
    try:
        from kabu_api import buy_sell_entry as bse
        old_value = getattr(bse, "MAX_ONESHOT", None)
        bse.MAX_ONESHOT = 700_000
        ok_main = True
        logger.warning("[ONESHOT LIMIT PATCH] MAX_ONESHOT changed old=%s new=%s", old_value, bse.MAX_ONESHOT)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] install failed")

    # summary_ai_daily_risk_symbol_only_patch は削除済み (上記コメント参照。ネイティブ版と
    # 入出力完全一致を確認済み)。
    ok_executor_result = _install_summary_ai_executor_result_patch()
    # summary_ai_sell_credit_prefilter_patch は削除済み (上記コメント参照)。

    _INSTALLED = bool(
        ok_budget_700k
        or ok_push_fast_restore
        or ok_stale_db_cleanup
        or ok_open_pos_throttle
        or ok_fresh_quote
        or ok_async_entry
        or ok_strict_liq
        or ok_watchlist_liq
        or ok_watchlist_bulk
        or ok_parallel
        or ok_bucket_filter
        or ok_pending_existing
        or ok_direction_failopen
        or ok_exit_profit
        or ok_exit_board
        or ok_display
        or ok_threshold
        or ok_qty_minlot
        or ok_main
        or ok_executor_result
    )
    return _INSTALLED
