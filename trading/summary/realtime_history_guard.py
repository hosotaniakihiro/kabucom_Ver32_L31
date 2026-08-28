# ============================================================
# File   : trading/summary/realtime_history_guard.py
# Version: V1.0-REALTIME-HISTORY-GUARD
# ------------------------------------------------------------
# P0-8 / P0-9: 履歴フレームを realtime 経路へ流さない。
#
# 2026-08-27 のログでは 1 tick のなかで
#     rebuild_display_ready         rows=22851
#     GC SUMMARY REPAIR             rows=22851
#     DAILY MTF                     rows=22851
#     SUMMARY PUBLISH MTF MERGED    rows=22851
#     attach_display_ready          rows=22851 cols=281 display_symbols=349
# が繰り返されていた。実体は 349 銘柄 x 平均 65 本の履歴フレームで、
# realtime に必要なのは「銘柄あたり最新の完成足 1 行」= 349 行だけ。
# 22,851 行 x 281 列を毎 tick 通したせいで
#   - light_push_runner が 390.9 秒 (予算 25 秒)
#   - cancel 要求から実停止まで 330 秒
#   - Summary が 14:08 で止まったまま
# になっていた。
#
# このモジュールは realtime publish 境界での「最後の砦」。
#   rows > threshold なら
#     1. 当日のみに絞る (過去日は indicator seed 用であって現在値ではない)
#     2. datetime でソートし symbol ごとに最新 1 行へ畳む
#     3. WARNING [REALTIME_HISTORY_LEAK] を caller 付きで出す
#
# 履歴フレーム自体は storage / indicator seed には必要なので消さない。
# ここは「realtime consumer へ渡す前」だけを守る。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# ENV
# ============================================================

def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def enabled() -> bool:
    return _env_bool("REALTIME_HISTORY_GUARD_ENABLED", True)


def threshold_rows() -> int:
    """この行数を超えたら履歴フレームとみなす。

    銘柄数は実測 349、監視上限は 500 程度なので 2000 で十分な余裕がある。
    """
    return max(100, _env_int("REALTIME_HISTORY_GUARD_MAX_ROWS", 2000))


# ============================================================
# STATS
# ============================================================

_LEAK_COUNTS: Counter = Counter()
_LEAK_LOCK = threading.Lock()
_LAST_LEAK: dict = {}


def leak_stats() -> dict:
    with _LEAK_LOCK:
        return {
            "total": sum(_LEAK_COUNTS.values()),
            "by_caller": dict(_LEAK_COUNTS.most_common(20)),
            "last": dict(_LAST_LEAK),
        }


def reset_stats() -> None:
    with _LEAK_LOCK:
        _LEAK_COUNTS.clear()
        _LAST_LEAK.clear()


# ============================================================
# CORE
# ============================================================

def _latest_dt(df: pd.DataFrame) -> Any:
    try:
        if "datetime" not in df.columns:
            return None
        ts = pd.to_datetime(df["datetime"], errors="coerce").max()
        return None if pd.isna(ts) else ts
    except Exception:
        return None


def _nsym(df: pd.DataFrame) -> int:
    try:
        return int(df["symbol"].nunique()) if "symbol" in df.columns else -1
    except Exception:
        return -1


def to_latest_projection(
    df: Any,
    *,
    caller: str,
    interval: Any = 1,
    today_only: bool = True,
) -> pd.DataFrame:
    """realtime consumer へ渡す前に latest projection へ落とす。

    - rows <= threshold_rows() なら何もしない (既に projection 済み)
    - それ以外は当日 filter -> sort datetime -> groupby(symbol).tail(1)
    - 変換したら必ず WARNING [REALTIME_HISTORY_LEAK] を出す

    返り値は必ず DataFrame。判定不能なら入力をそのまま返す
    (勝手に空にしない)。
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    if not enabled():
        return df

    n_in = len(df)
    limit = threshold_rows()
    if n_in <= limit:
        return df

    if "symbol" not in df.columns or "datetime" not in df.columns:
        # 畳めない形。黙って通さず、必ず記録する。
        logger.warning(
            "[REALTIME_HISTORY_LEAK] caller=%s interval=%s rows=%s > %s だが "
            "symbol/datetime 列が無く projection できない cols=%s",
            caller, interval, n_in, limit, list(df.columns)[:12],
        )
        return df

    t0 = time.perf_counter()
    latest_before = _latest_dt(df)
    nsym_before = _nsym(df)

    out = df
    try:
        dtc = pd.to_datetime(out["datetime"], errors="coerce")
    except Exception:
        logger.exception("[REALTIME_HISTORY_LEAK] datetime 変換に失敗 caller=%s", caller)
        return df

    n_today = n_in
    if today_only:
        try:
            today = pd.Timestamp.now().normalize()
            mask = dtc.dt.normalize() == today
            if bool(mask.any()):
                out = out[mask]
                dtc = dtc[mask]
                n_today = len(out)
            else:
                # 当日行が 1 行も無い。過去日フレームなので realtime へは渡さない。
                # ただし空を返すと consumer が「建玉/銘柄が消えた」と誤解するので、
                # 当日 filter は諦めて最新 1 行へ畳むだけにする。
                logger.warning(
                    "[REALTIME_HISTORY_LEAK] caller=%s interval=%s 当日行なし "
                    "rows=%s latest=%s -> today filter を諦めて latest 1 行へ畳む",
                    caller, interval, n_in, latest_before,
                )
        except Exception:
            logger.exception("[REALTIME_HISTORY_LEAK] today filter 失敗 caller=%s", caller)

    try:
        work = out.assign(_rhg_dt=dtc.values)
        work = work.sort_values("_rhg_dt", kind="stable")
        work = work.drop_duplicates("symbol", keep="last")
        out = work.drop(columns=["_rhg_dt"], errors="ignore").reset_index(drop=True)
    except Exception:
        logger.exception("[REALTIME_HISTORY_LEAK] latest projection 失敗 caller=%s", caller)
        return df

    el = time.perf_counter() - t0
    latest_after = _latest_dt(out)
    nsym_after = _nsym(out)

    with _LEAK_LOCK:
        _LEAK_COUNTS[str(caller)] += 1
        _LAST_LEAK.clear()
        _LAST_LEAK.update({
            "caller": str(caller),
            "interval": interval,
            "rows_before": n_in,
            "rows_after": len(out),
            "latest_before": str(latest_before),
            "latest_after": str(latest_after),
        })

    logger.warning(
        "[REALTIME_HISTORY_LEAK] caller=%s interval=%s "
        "rows_before=%s rows_after=%s (today filter %s -> %s, non_today=%s) "
        "latest_before=%s latest_after=%s symbols %s -> %s elapsed=%.3fs "
        "(履歴フレームを realtime 経路へ渡そうとしたので latest projection へ落とした。"
        "履歴は storage / indicator seed 専用)",
        caller, interval, n_in, len(out), n_in, n_today, n_in - n_today,
        latest_before, latest_after, nsym_before, nsym_after, el,
    )
    return out


def is_history_frame(df: Any) -> bool:
    """realtime へ渡してはいけない履歴フレームか。"""
    try:
        return isinstance(df, pd.DataFrame) and len(df) > threshold_rows()
    except Exception:
        return False


def guard(caller: str, interval: Any = 1):
    """デコレータ版。第 1 引数の DataFrame を projection してから呼ぶ。"""

    def _wrap(fn):
        def _inner(df, *args, **kwargs):
            return fn(to_latest_projection(df, caller=caller, interval=interval), *args, **kwargs)

        _inner.__name__ = getattr(fn, "__name__", "guarded")
        _inner.__doc__ = getattr(fn, "__doc__", None)
        return _inner

    return _wrap


__all__ = [
    "to_latest_projection",
    "is_history_frame",
    "guard",
    "leak_stats",
    "reset_stats",
    "enabled",
    "threshold_rows",
]
