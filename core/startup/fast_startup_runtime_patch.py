# ============================================================
# File   : core/startup/fast_startup_runtime_patch.py
# Version: PRODUCTION-FAST-STARTUP-PATCH-V1
# ------------------------------------------------------------
# 目的:
#   main.py 起動直後の重い初回ランキングサマリー実行を軽くする。
#
# ログ上の問題:
#   - ranking_summary_all の初回/定時実行が 232〜458秒かかっている
#   - 64233 rows の DataFrame が job thread done の戻り値としてログに出る
#   - 起動直後に summary / ranking の重い初回tickを同期実行すると、
#     realtime開始・entry開始まで遅れる
#
# 方針:
#   1) scheduler_bootstrap のランキング lookback を環境変数で短縮可能にする
#   2) ranking summary job の戻り値を None にして巨大DataFrameログを防ぐ
#   3) main.py の initial ranking tick once を no-op 化可能にする
#      デフォルトは SKIP=1。ランキングは定時ジョブに任せる。
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


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import core.startup.scheduler_bootstrap as sb
    except Exception:
        logger.exception("[FAST STARTUP PATCH] scheduler_bootstrap import failed")
        return False

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
    #    起動中に重い ranking summary を同期実行しない。定時ジョブは残す。
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
