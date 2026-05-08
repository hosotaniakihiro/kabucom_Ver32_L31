# ============================================================
# File   : core/startup/summary_startup.py
# Version: FINAL-PRODUCTION-REV23.2-SUMMARY-STARTUP-PUSH-INCREMENTAL-MA75-TAIL120
# ------------------------------------------------------------
# 【概要】
#   startup summary restore / push incremental MA75 / summary fast boot / MTF history を担当
#
# 【機能】
#   ✔ startup_summary_restore
#   ✔ 保存済み1/3/5分足summary最新以降のPUSHを読み込みMA75を継続作成
#   ✔ 各銘柄75MA計算用に最低75本以上、標準120本のsummary tailを読む
#   ✔ orchestrator が run_startup_summary_restore_safe() だけ呼ぶ構成でもMA75処理を必ず実行
#   ✔ summary fast boot async
#   ✔ MTF history bootstrap
# ============================================================

from __future__ import annotations

import logging
from typing import Any

from global_state import global_data

from core.startup.summary_runtime import run_bootstrap_summary_fast_boot
from core.startup.mtf_history_bootstrap_runner import run_mtf_history_bootstrap_safe
from core.startup.startup_config import resolve_attr

logger = logging.getLogger(__name__)

VERSION = "FINAL-PRODUCTION-REV23.2-SUMMARY-STARTUP-PUSH-INCREMENTAL-MA75-TAIL120"


# ============================================================
# push incremental MA75
# ============================================================

def run_push_incremental_ma75_startup_safe(*, force: bool = False) -> Any:
    """
    保存済み1分/3分/5分足サマリーの最新以降のPUSHだけを読み込み、
    既存tailと結合して MA75 を含む指標を作る。

    重要:
      - 起動を止めない
      - DB保存はここでは必須にしない
      - global cache を更新して、起動直後の表示/ENTRYで75MAが使えるようにする
      - 既に成功済みの場合は重複実行しない
    """
    try:
        if not force and bool(getattr(global_data, "push_incremental_ma75_done", False)):
            logger.info("📈 [PUSH INCR MA75] startup skip because already done")
            return getattr(global_data, "push_incremental_ma75_result", None)
    except Exception:
        pass

    logger.info("📈 [PUSH INCR MA75] startup begin")

    try:
        global_data.push_incremental_ma75_started = True
        global_data.push_incremental_ma75_done = False
        global_data.push_incremental_ma75_failed = False
        global_data.push_incremental_ma75_result = None
    except Exception:
        pass

    try:
        from core.startup.startup_push_incremental_ma75 import build_push_incremental_ma75_on_startup

        result = build_push_incremental_ma75_on_startup(
            intervals=(1, 3, 5),
            update_global_cache=True,
        )

        ok = bool(getattr(result, "ok", False))
        try:
            global_data.push_incremental_ma75_done = ok
            global_data.push_incremental_ma75_failed = not ok
            global_data.push_incremental_ma75_result = result
        except Exception:
            pass

        logger.info(
            "✅ [PUSH INCR MA75] startup result ok=%s msg=%s "
            "summary_db=%s push_db=%s push_rows=%s loaded_summary_rows=%s "
            "new_rows=%s cache_rows=%s ma75_nonnull=%s latest=%s",
            ok,
            getattr(result, "message", ""),
            getattr(result, "summary_db", None),
            getattr(result, "push_db", None),
            getattr(result, "push_rows", None),
            getattr(result, "loaded_summary_rows", None),
            getattr(result, "new_rows", None),
            getattr(result, "cache_rows", None),
            getattr(result, "ma75_nonnull", None),
            getattr(result, "latest", None),
        )

        return result

    except Exception as e:
        try:
            global_data.push_incremental_ma75_done = False
            global_data.push_incremental_ma75_failed = True
            global_data.push_incremental_ma75_result = {
                "ok": False,
                "message": str(e),
            }
        except Exception:
            pass
        logger.exception("❌ [PUSH INCR MA75] startup failed")
        return None

    finally:
        try:
            global_data.push_incremental_ma75_started = False
        except Exception:
            pass


# ============================================================
# startup summary restore
# ============================================================

def run_startup_summary_restore_safe() -> Any:
    """
    起動時に summary DB / PUSH DB から必要最小限のデータを復元する。

    重要:
      orchestrator はこの関数だけを直接呼ぶため、ここから必ず
      push incremental MA75 を続けて実行する。
    """
    logger.info("📊 startup summary restore start")

    try:
        global_data.startup_summary_restore_started = True
        global_data.startup_summary_restore_done = False
        global_data.startup_summary_restore_failed = False
        global_data.startup_summary_restore_result = None
    except Exception:
        pass

    result = None

    try:
        restore_fn = resolve_attr(
            "core.startup.startup_summary_restore",
            "restore_startup_summary_minimal_tail",
        )

        if not callable(restore_fn):
            logger.warning(
                "⚠ startup summary restore function not found. "
                "Create core/startup/startup_summary_restore.py with "
                "restore_startup_summary_minimal_tail()."
            )

            try:
                global_data.startup_summary_restore_failed = True
                global_data.startup_summary_restore_result = {
                    "ok": False,
                    "message": "restore_startup_summary_minimal_tail not found",
                }
            except Exception:
                pass

            return None

        result = restore_fn(
            intervals=(1, 3, 5),
            display=True,
            save_missing=True,
            # 75MA用。75本ちょうどでは欠損/途中足/重複除去で足りなくなるため120本読む。
            tail_rows=120,
            one_min_lookback_minutes=15,
        )

        ok = bool(getattr(result, "ok", False))
        msg = str(getattr(result, "message", ""))

        try:
            global_data.startup_summary_restore_done = ok
            global_data.startup_summary_restore_failed = not ok
            global_data.startup_summary_restore_result = result
        except Exception:
            pass

        logger.info(
            "✅ startup summary restore result "
            "ok=%s msg=%s "
            "summary_db=%s push_db=%s "
            "1min_rows=%s push_rows=%s "
            "existing3=%s existing5=%s "
            "new3=%s new5=%s "
            "saved3=%s saved5=%s "
            "load_from=%s tail_rows=%s",
            ok,
            msg,
            getattr(result, "summary_db", None),
            getattr(result, "push_db", None),
            getattr(result, "loaded_1min_rows", None),
            getattr(result, "loaded_push_rows", None),
            getattr(result, "existing_3min_rows", None),
            getattr(result, "existing_5min_rows", None),
            getattr(result, "new_3min_rows", None),
            getattr(result, "new_5min_rows", None),
            getattr(result, "saved_3min_rows", None),
            getattr(result, "saved_5min_rows", None),
            getattr(result, "one_min_load_from", None),
            120,
        )

        if not ok:
            logger.warning(
                "⚠ startup summary restore completed but ok=False. "
                "summary async bootstrap will continue later."
            )

        return result

    except Exception as e:
        try:
            global_data.startup_summary_restore_done = False
            global_data.startup_summary_restore_failed = True
            global_data.startup_summary_restore_result = {
                "ok": False,
                "message": str(e),
            }
        except Exception:
            pass

        logger.exception("❌ startup summary restore failed")
        return None

    finally:
        # orchestrator は run_startup_summary_restore_safe() しか直接呼ばないため、
        # ここで必ずMA75用の銘柄別tail読み込み・PUSH差分結合を実行する。
        try:
            logger.info("📈 [PUSH INCR MA75] chained after startup summary restore")
            run_push_incremental_ma75_startup_safe()
        except Exception:
            logger.exception("❌ [PUSH INCR MA75] chained startup call failed")


# ============================================================
# summary fast boot / MTF
# ============================================================

def start_summary_fast_boot_safe() -> None:
    """
    summary bootstrap は起動を止めず background で進める。
    """
    try:
        run_bootstrap_summary_fast_boot(force_sync=False)
    except Exception:
        logger.exception("❌ Summary bootstrap fast-boot start failed")


def run_mtf_history_bootstrap_startup_safe(*, market_open_now: bool) -> None:
    """
    scheduler / realtime entry より前後で使う MTF history bootstrap。
    """
    try:
        run_mtf_history_bootstrap_safe(market_open_now=market_open_now)
    except Exception:
        logger.exception("❌ MTF history bootstrap failed")


def start_summary_stack_after_scheduler(*, market_open_now: bool) -> None:
    """
    startup summary restore / push incremental MA75 / summary fast boot / MTF history を実行。
    """
    run_startup_summary_restore_safe()
    run_push_incremental_ma75_startup_safe()
    start_summary_fast_boot_safe()
    run_mtf_history_bootstrap_startup_safe(market_open_now=market_open_now)


__all__ = [
    "VERSION",
    "run_startup_summary_restore_safe",
    "run_push_incremental_ma75_startup_safe",
    "start_summary_fast_boot_safe",
    "run_mtf_history_bootstrap_startup_safe",
    "start_summary_stack_after_scheduler",
]
