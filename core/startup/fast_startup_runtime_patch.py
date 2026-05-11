# ============================================================
# File   : core/startup/fast_startup_runtime_patch.py
# Version: PRODUCTION-FAST-STARTUP-PATCH-V2-SKIP-SUMMARY-SCHEMA
# ------------------------------------------------------------
# 目的:
#   main.py 起動直後の重い処理を軽くする。
#
# ログ上の問題:
#   - ranking_summary_all の初回/定時実行が 232〜458秒かかる
#   - 64233 rows の DataFrame が job thread done の戻り値としてログに出る
#   - summary schema bootstrap が added_columns=0 なのに約5分かかる
#
# 方針:
#   1) scheduler_bootstrap のランキング lookback を環境変数で短縮可能にする
#   2) ranking summary job の戻り値を None にして巨大DataFrameログを防ぐ
#   3) main.py の initial ranking tick once を no-op 化可能にする
#   4) main.py 側の summary schema bootstrap をデフォルトskipする
#      DB作成・schema補完は main_database.py 側で実施する想定。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _patch_summary_schema_bootstrap() -> None:
    """
    database.session._bootstrap_summary_schema を main.py ではskipする。

    理由:
      database.session Ver43 は起動ごとに3テーブル全列を確認する。
      NAS上SQLiteでは added_columns=0 でも数分かかる。

    戻したい場合:
      set FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP=0
    """
    skip_schema = _env_bool("FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP", True)
    if not skip_schema:
        logger.warning(
            "[FAST STARTUP PATCH] summary schema bootstrap skip disabled env=FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP"
        )
        return

    try:
        import database.session as ds
    except Exception:
        logger.exception("[FAST STARTUP PATCH] database.session import failed for schema skip")
        return

    old_bootstrap = getattr(ds, "_bootstrap_summary_schema", None)
    if not callable(old_bootstrap):
        logger.warning("[FAST STARTUP PATCH] _bootstrap_summary_schema not callable")
        return

    if getattr(old_bootstrap, "_fast_startup_schema_skip", False):
        return

    def _skip_summary_schema_bootstrap(engine):
        logger.warning(
            "[FAST STARTUP PATCH] summary schema bootstrap skipped in main.py "
            "reason=main_database_handles_schema env=FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP"
        )
        return None

    _skip_summary_schema_bootstrap._fast_startup_schema_skip = True  # type: ignore[attr-defined]
    _skip_summary_schema_bootstrap._original_bootstrap = old_bootstrap  # type: ignore[attr-defined]
    ds._bootstrap_summary_schema = _skip_summary_schema_bootstrap

    logger.warning("[FAST STARTUP PATCH] database.session._bootstrap_summary_schema patched to no-op")


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import core.startup.scheduler_bootstrap as sb
    except Exception:
        logger.exception("[FAST STARTUP PATCH] scheduler_bootstrap import failed")
        return False

    # 0) summary schema bootstrap をmain側ではskip。
    try:
        _patch_summary_schema_bootstrap()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] summary schema skip patch failed")

    # 1) ranking lookback を短縮。デフォルト 240 -> 60。
    try:
        old_lookback = getattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", None)
        new_lookback = _env_int("FAST_STARTUP_RANKING_LOOKBACK_MIN", 60)
        if new_lookback > 0:
            setattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", int(new_lookback))
        logger.warning(
            "[FAST STARTUP PATCH] ranking lookback patched old=%s new=%s env=FAST_STARTUP_RANKING_LOOKBACK_MIN",
            old_lookback,
            getattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", None),
        )
    except Exception:
        logger.exception("[FAST STARTUP PATCH] lookback patch failed")

    # 2) ranking summary job の巨大戻り値ログを防ぐ。
    try:
        old_job = getattr(sb, "_run_ranking_summary_all_job_safe", None)
        if callable(old_job) and not getattr(old_job, "_fast_startup_wrapped", False):

            def _ranking_job_safe_no_return(*args: Any, **kwargs: Any):
                ret = old_job(*args, **kwargs)
                try:
                    sb._set_global_attr("last_ranking_summary_job_result_type", type(ret).__name__)
                    if isinstance(ret, dict):
                        sb._set_global_attr(
                            "last_ranking_summary_job_result_summary",
                            {
                                k: {
                                    "type": type(v).__name__,
                                    "rows": len(v) if hasattr(v, "__len__") else None,
                                }
                                for k, v in ret.items()
                            },
                        )
                except Exception:
                    pass
                return None

            _ranking_job_safe_no_return._fast_startup_wrapped = True  # type: ignore[attr-defined]
            sb._run_ranking_summary_all_job_safe = _ranking_job_safe_no_return
            logger.warning("[FAST STARTUP PATCH] ranking summary scheduled job return suppressed")
    except Exception:
        logger.exception("[FAST STARTUP PATCH] ranking return suppression failed")

    # 3) main.py の initial ranking tick をデフォルト no-op 化。
    try:
        skip_initial = _env_bool("FAST_STARTUP_SKIP_INITIAL_RANKING_TICK", True)
        if skip_initial:
            import __main__ as main_mod
            old_initial = getattr(main_mod, "_run_initial_ranking_tick_once", None)
            if callable(old_initial) and not getattr(old_initial, "_fast_startup_noop", False):

                def _skip_initial_ranking_tick_once():
                    logger.warning(
                        "[FAST STARTUP PATCH] initial ranking tick skipped env=FAST_STARTUP_SKIP_INITIAL_RANKING_TICK"
                    )
                    return None

                _skip_initial_ranking_tick_once._fast_startup_noop = True  # type: ignore[attr-defined]
                setattr(main_mod, "_run_initial_ranking_tick_once", _skip_initial_ranking_tick_once)
                logger.warning("[FAST STARTUP PATCH] main initial ranking tick patched to no-op")
    except Exception:
        logger.exception("[FAST STARTUP PATCH] initial ranking tick patch failed")

    _PATCHED = True
    logger.warning("[FAST STARTUP PATCH] installed")
    return True


try:
    install()
except Exception:
    logger.exception("[FAST STARTUP PATCH] auto install failed")
