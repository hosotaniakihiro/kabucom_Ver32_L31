# ============================================================
# File   : core/startup/summary_discord_always_notify_patch.py
# Version: V1-SUMMARY-DISCORD-EMPTY-FALLBACK-NOTIFY
# ------------------------------------------------------------
# 目的:
#   サマリー計算結果がDiscordへ送信されない問題の最終防衛。
#
# 背景:
#   scheduler_jobs.summary.safe_io.display_push_summary_safe() / display_ranking_summary_safe()
#   は、以下の場合に False を返して終了する。
#     - 入力DFが空
#     - liquidity filter 後に0件
#     - display universe guard 後に0件
#
#   その場合、Discord送信関数まで到達せず「何も送られない」ように見える。
#
# 方針:
#   - 1分足は従来通り送信しない
#   - 3分足/5分足は、候補が0件でもDiscordへ状態通知する
#   - 通常の送信が成功した場合は何もしない
#   - main_database.py 側の summary_database_runner から、time_locked_runner import 前に install() する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_PUSH = None
_ORIG_RANKING = None
_LAST_SENT: dict[tuple[str, int], str] = {}


def _env_on(name: str, default: bool = True) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if raw in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    except Exception:
        pass
    return bool(default)


def _safe_len(df: Any) -> int:
    try:
        return len(df) if isinstance(df, pd.DataFrame) else 0
    except Exception:
        return 0


def _latest_dt(df: Any) -> str:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return ""
        for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    return str(s.max())
    except Exception:
        pass
    return ""


def _symbol_sample(df: Any, n: int = 10) -> str:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return ""
        vals = [str(x) for x in df["symbol"].astype(str).head(n).tolist()]
        return ",".join(vals)
    except Exception:
        return ""


def _should_notify(interval: int) -> bool:
    try:
        return int(interval) != 1
    except Exception:
        return True


def _send_empty_notice(*, source: str, interval: int, now: Any, df: Any, reason: str) -> None:
    if not _env_on("SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY", True):
        return
    if not _should_notify(interval):
        return

    try:
        minute_key = ""
        try:
            minute_key = (now or dt.datetime.now()).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        except Exception:
            minute_key = dt.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

        key = (str(source).upper(), int(interval))
        if _LAST_SENT.get(key) == minute_key:
            logger.info(
                "[SUMMARY DISCORD EMPTY PATCH] duplicate notice skipped source=%s interval=%s minute=%s",
                source,
                interval,
                minute_key,
            )
            return
        _LAST_SENT[key] = minute_key

        from utils.alerts_util import send_discord_message

        rows = _safe_len(df)
        latest = _latest_dt(df)
        sample = _symbol_sample(df)

        title = f"📊 {str(source).upper()} SUMMARY {int(interval)}min"
        lines = [
            f"{title}",
            f"候補なし / Discord表示対象 0件",
            f"時刻={minute_key}",
            f"理由={reason}",
            f"入力rows={rows}",
        ]
        if latest:
            lines.append(f"latest_dt={latest}")
        if sample:
            lines.append(f"sample_symbols={sample}")
        lines.append("※ 1分足は通知抑止、3分/5分のみ状態通知")

        ok = send_discord_message(content="\n".join(lines))
        logger.warning(
            "[SUMMARY DISCORD EMPTY PATCH] sent empty notice ok=%s source=%s interval=%s rows=%s reason=%s",
            ok,
            source,
            interval,
            rows,
            reason,
        )
    except Exception:
        logger.exception(
            "[SUMMARY DISCORD EMPTY PATCH] empty notice failed source=%s interval=%s reason=%s",
            source,
            interval,
            reason,
        )


def install() -> bool:
    global _INSTALLED, _ORIG_PUSH, _ORIG_RANKING
    if _INSTALLED:
        return True

    try:
        import scheduler_jobs.summary.safe_io as safe_io

        cur_push = getattr(safe_io, "display_push_summary_safe", None)
        cur_ranking = getattr(safe_io, "display_ranking_summary_safe", None)
        if not callable(cur_push) or not callable(cur_ranking):
            logger.warning("[SUMMARY DISCORD EMPTY PATCH] target functions missing")
            return False

        if getattr(cur_push, "_summary_discord_empty_patch", False):
            _INSTALLED = True
            return True

        _ORIG_PUSH = cur_push
        _ORIG_RANKING = cur_ranking

        def _patched_push(df, interval: int, now):
            try:
                ok = bool(_ORIG_PUSH(df, interval, now))
                if not ok:
                    _send_empty_notice(source="PUSH", interval=int(interval), now=now, df=df, reason="display_push_summary_safe returned False")
                return ok
            except Exception:
                logger.exception("[SUMMARY DISCORD EMPTY PATCH] patched push display failed")
                _send_empty_notice(source="PUSH", interval=int(interval), now=now, df=df, reason="display_push_summary_safe exception")
                return False

        def _patched_ranking(df, interval: int, now):
            try:
                ok = bool(_ORIG_RANKING(df, interval, now))
                if not ok:
                    _send_empty_notice(source="RANKING", interval=int(interval), now=now, df=df, reason="display_ranking_summary_safe returned False")
                return ok
            except Exception:
                logger.exception("[SUMMARY DISCORD EMPTY PATCH] patched ranking display failed")
                _send_empty_notice(source="RANKING", interval=int(interval), now=now, df=df, reason="display_ranking_summary_safe exception")
                return False

        _patched_push._summary_discord_empty_patch = True  # type: ignore[attr-defined]
        _patched_push._original = cur_push  # type: ignore[attr-defined]
        _patched_ranking._summary_discord_empty_patch = True  # type: ignore[attr-defined]
        _patched_ranking._original = cur_ranking  # type: ignore[attr-defined]

        safe_io.display_push_summary_safe = _patched_push
        safe_io.display_ranking_summary_safe = _patched_ranking

        _INSTALLED = True
        logger.warning(
            "[SUMMARY DISCORD EMPTY PATCH] installed enabled=%s",
            _env_on("SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY", True),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY DISCORD EMPTY PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY DISCORD EMPTY PATCH] auto install failed")


__all__ = ["install"]
