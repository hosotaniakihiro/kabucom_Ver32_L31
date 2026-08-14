# -*- coding: utf-8 -*-
# ============================================================
# File   : trading/entry/summary_ai/current_df_context.py
# Version: V7-SUMMARY-AI-LOW-MOVE-CURRENT-DF-RANGE-REPAIR
# ------------------------------------------------------------
# 旧 core/startup/final_board_guard_signature_compat_patch.py のうち、
# 「今 SUMMARY_AI へ渡されている df」をスレッドローカルで共有する部分を
# summary_ai 側へ移設した。
#
# 読み手は 2 つ:
#   trading/entry/summary_ai/candidate_refill._summary_ai_entry_unsafe_reason()
#       … global history が古くても、実際に渡された df が新鮮なら通す
#   trading/summary/pipeline/entry_pipeline._allow_summary_ai_liquidity()
#       … 行の high/low が close に潰れていても、AI 入力元の当日レンジで補修する
#
# 書き手 (install_runner_current_df_context) をネイティブへ畳まないのは、
# 読み手のひとつ _summary_ai_entry_unsafe_reason() が candidate_refill 層
# (= runner のネイティブより外側) から呼ばれるため。ネイティブの中で
# スレッドローカルを張ると、その時点ではもう判定が終わっている。
#
# 旧 install 元 (core/startup/board_safety_defaults._install_board_guard_signature_compat)
# はそのままで、呼び先だけこのモジュールへ差し替えている。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V7-SUMMARY-AI-LOW-MOVE-CURRENT-DF-RANGE-REPAIR"

CURRENT_AI_INPUT = threading.local()

_INSTALLED = False
_WATCHER_STARTED = False


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def pick_first_float(d: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
    for k in keys:
        x = safe_float(d.get(k), 0.0)
        if x > 0:
            return x
    return default


def current_df() -> Any:
    return getattr(CURRENT_AI_INPUT, "df", None)


def latest_age_sec(df: Any) -> tuple[bool, float | None, Any, int, int]:
    """旧 final_board_guard_signature_compat_patch._latest_age_sec()。

    戻り値: (判定できたか, 経過秒, 最新時刻, 行数, スコアが非ゼロの行数)
    """
    try:
        import pandas as pd

        if not isinstance(df, pd.DataFrame) or df.empty:
            return False, None, None, 0, 0
        dt_col = None
        for c in ("datetime", "end_time", "last_update", "updated_at", "inserted_at"):
            if c in df.columns:
                dt_col = c
                break
        if not dt_col:
            return False, None, None, len(df), 0
        latest = pd.to_datetime(df[dt_col], errors="coerce").max()
        if pd.isna(latest):
            return False, None, None, len(df), 0
        try:
            latest_py = latest.to_pydatetime().replace(tzinfo=None)
        except Exception:
            latest_py = latest
        age = (dt.datetime.now().replace(tzinfo=None) - latest_py).total_seconds()
        score_nonzero = 0
        for c in ("score", "score_total", "final_score", "display_score", "score_buy", "score_sell", "buy_score", "sell_score"):
            if c in df.columns:
                try:
                    score_nonzero = max(score_nonzero, int((pd.to_numeric(df[c], errors="coerce").fillna(0).abs() > 0).sum()))
                except Exception:
                    pass
        return True, float(age), latest_py, len(df), score_nonzero
    except Exception:
        return False, None, None, 0, 0


def current_df_symbol_range(symbol: str) -> tuple[float, float, float, str]:
    """旧 final_board_guard_signature_compat_patch._current_df_symbol_range()。

    戻り値: (close, high, low, method)。取れなければ (0,0,0,"none")。
    """
    try:
        import pandas as pd

        df = current_df()
        if not isinstance(df, pd.DataFrame) or df.empty or not symbol or "symbol" not in df.columns:
            return 0.0, 0.0, 0.0, "none"
        s = df[df["symbol"].astype(str) == str(symbol)]
        if s.empty:
            return 0.0, 0.0, 0.0, "none"
        if "datetime" in s.columns:
            try:
                s = s.assign(_dt=pd.to_datetime(s["datetime"], errors="coerce")).sort_values("_dt")
            except Exception:
                pass
        d = s.iloc[-1].to_dict()
        close = pick_first_float(d, ("close", "close_price", "current_price", "price", "last_price"), 0.0)
        high = pick_first_float(d, ("high", "high_price", "day_high", "today_high", "intraday_high", "session_high", "opening_price", "open_price", "open", "day_open"), 0.0)
        low = pick_first_float(d, ("low", "low_price", "day_low", "today_low", "intraday_low", "session_low", "opening_price", "open_price", "open", "day_open"), 0.0)
        # 片側に始値を使った場合は終値と組み合わせて実際に動いた幅にする。
        op = pick_first_float(d, ("opening_price", "open_price", "open", "day_open"), 0.0)
        if op > 0 and close > 0:
            high = max(high, close, op)
            low = min(low if low > 0 else op, close, op)
        if close > 0 and high > 0 and low > 0 and high >= low and high > low:
            return close, high, low, "current_ai_input"
    except Exception:
        logger.debug("[entry_pipeline] current AI df range lookup failed symbol=%s", symbol, exc_info=True)
    return 0.0, 0.0, 0.0, "none"


def _extract_df(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if args:
        return args[0]
    for key in ("summary_df", "df", "source_df", "base_df"):
        if key in kwargs:
            return kwargs.get(key)
    return None


def install_runner_current_df_context() -> bool:
    """run_summary_ai_entry_from_df を包んで CURRENT_AI_INPUT.df を張る。

    旧 _install_current_df_freshness_patch() の runner 側と同じ実装
    (マーカー名も互換のため据え置き)。
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry.summary_ai.runner as runner

        cur_run = getattr(runner, "run_summary_ai_entry_from_df", None)
        if not callable(cur_run):
            return False
        if getattr(cur_run, "_summary_ai_current_df_freshness_v7", False):
            _INSTALLED = True
            return True
        orig_run = cur_run

        def _patched_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            old = getattr(CURRENT_AI_INPUT, "df", None)
            CURRENT_AI_INPUT.df = _extract_df(args, kwargs)
            try:
                return orig_run(*args, **kwargs)
            finally:
                CURRENT_AI_INPUT.df = old

        _patched_run_summary_ai_entry_from_df._summary_ai_current_df_freshness_v7 = True  # type: ignore[attr-defined]
        _patched_run_summary_ai_entry_from_df._summary_ai_current_df_freshness_v1 = True  # type: ignore[attr-defined]
        _patched_run_summary_ai_entry_from_df._original = orig_run  # type: ignore[attr-defined]
        runner.run_summary_ai_entry_from_df = _patched_run_summary_ai_entry_from_df
        _INSTALLED = True
        logger.warning("[SUMMARY AI SAFETY GUARD] current df freshness patch installed version=%s", VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI SAFETY GUARD] current df freshness patch not ready", exc_info=True)
        return False


def _watcher() -> None:
    """install が早すぎて失敗した場合だけ再試行する (旧 install 側の watcher 相当)。"""
    for i in range(30):
        time.sleep(1.0)
        if install_runner_current_df_context():
            logger.warning("[SUMMARY AI SAFETY GUARD] current df context ready i=%s version=%s", i + 1, VERSION)
            return
    logger.warning("[SUMMARY AI SAFETY GUARD] current df context watcher done installed=%s version=%s", _INSTALLED, VERSION)


def install() -> bool:
    global _WATCHER_STARTED
    ok = install_runner_current_df_context()
    if not ok and not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-current-df-context", daemon=True).start()
    return ok


__all__ = [
    "VERSION",
    "CURRENT_AI_INPUT",
    "current_df",
    "current_df_symbol_range",
    "latest_age_sec",
    "install",
    "install_runner_current_df_context",
]
