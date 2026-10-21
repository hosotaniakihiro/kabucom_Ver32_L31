# ============================================================
# File   : trading/handlers/short_mtf_direction_guard.py
# ------------------------------------------------------------
# 発注直前の短期MTF方向ガード (1分/3分/5分の 2 of 3 判定)。
#
# 旧 core/startup/entry_mtf_short_required_daily_optional_patch.py V1.4 が
#   trading/handlers/entry_order_builder._summary_mtf_direction_guard
#   core/startup/entry_limit_passive_runtime_patch._summary_ai_strict_guard
# を差し替えて行っていた判定を本文側へ移設したもの。
#
# 方針 (旧パッチのまま):
#   - 発注直前の方向ガードでは日足込みの mtf / score_mtf を必須判定に使わない
#   - 1分/3分/5分の slope_atr_scaled_* を確認し、2本以上が方向一致ならOK
#   - slope=0.0 は中立 (方向一致にも逆行にも数えない)
#   - 日足MTFはスコア加点・参考情報として残すが単独では発注停止しない
#
# 旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 = main.py 専用
# だったため、short_mtf_guard_enabled() で同じスコープを保つ。
# ============================================================

from __future__ import annotations

import logging
import math
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_DB_CACHE: dict[tuple[str, int, str], Optional[float]] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


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


def _safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.endswith(".T"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _get(row: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return None


def _ng(reason: str, **detail: Any) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


def _latest_symbol_row(df: Any, symbol: str) -> dict:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return {}
        sym = _norm_symbol(symbol)
        work = df.copy()
        work["__sym__"] = work["symbol"].map(_norm_symbol)
        work = work[work["__sym__"] == sym].copy()
        if work.empty:
            return {}
        time_col = None
        for c in ("datetime", "end_time", "start_time", "time"):
            if c in work.columns:
                time_col = c
                break
        if time_col:
            work["__dt__"] = pd.to_datetime(work[time_col], errors="coerce")
            work = work.sort_values("__dt__", kind="stable")
        return dict(work.iloc[-1].to_dict())
    except Exception:
        logger.debug("[SHORT MTF GUARD] latest row lookup failed symbol=%s", symbol, exc_info=False)
        return {}


def _slope_from_summary_row(row: dict) -> Optional[float]:
    return _safe_float_or_none(_get(row, "slope_atr_scaled", "slope", "score_slope", "disp_slope"))


def _get_gc_summary_row(symbol: str, tf: int) -> dict:
    try:
        from core.global_context.context import global_context as GC
    except Exception:
        return {}

    for getter_name in ("get_push_merged_summary", "get_merged_summary", "get_summary_history", "get_push_summary"):
        try:
            getter = getattr(GC, getter_name, None)
            if not callable(getter):
                continue
            if getter_name == "get_merged_summary":
                df = getter(tf, source="push")
            else:
                df = getter(tf)
            got = _latest_symbol_row(df, symbol)
            if got:
                return got
        except Exception:
            logger.debug("[SHORT MTF GUARD] GC getter failed getter=%s tf=%s symbol=%s", getter_name, tf, symbol, exc_info=False)
    return {}


def _summary_db_path() -> str:
    ymd = os.getenv("KABU_TODAY") or os.getenv("TARGET_DATE") or datetime.now().strftime("%Y%m%d")
    root = os.getenv("SUMMARY_DB_DIR") or nas_path(r"raw_data\kabu_station\summary")
    return os.path.join(root, f"summary{ymd}.db")


def _table_for_tf(tf: int) -> str:
    return f"stock_summary_{int(tf)}min"


def _get_db_short_slope(symbol: str, tf: int) -> Optional[float]:
    if not _env_bool("ENTRY_SHORT_MTF_DB_BACKFILL", True):
        return None

    sym = _norm_symbol(symbol)
    today = datetime.now().strftime("%Y%m%d")
    key = (sym, int(tf), today)
    if key in _DB_CACHE:
        return _DB_CACHE[key]

    db = _summary_db_path()
    table = _table_for_tf(tf)
    val: Optional[float] = None

    try:
        if not os.path.exists(db):
            _DB_CACHE[key] = None
            return None

        sql = f"""
            SELECT slope_atr_scaled, slope, score_slope, datetime
              FROM {table}
             WHERE CAST(symbol AS TEXT)=?
             ORDER BY datetime DESC
             LIMIT 1
        """
        with committing_connect(db, timeout=1.0) as conn:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute(sql, (sym,)).fetchone()

        if row:
            for x in (row[0], row[1], row[2]):
                val = _safe_float_or_none(x)
                if val is not None:
                    break
            logger.warning(
                "[SHORT MTF GUARD] DB backfill symbol=%s tf=%s slope=%s dt=%s db=%s",
                sym, tf, val, row[3], db,
            )
    except Exception as e:
        logger.debug("[SHORT MTF GUARD] DB backfill failed symbol=%s tf=%s err=%s", sym, tf, e, exc_info=False)

    _DB_CACHE[key] = val
    return val


def _resolve_short_slope(row: Dict[str, Any], *, symbol: str, tf: int) -> Optional[float]:
    tf_s = str(tf)
    explicit = _safe_float_or_none(_get(row, f"slope_atr_scaled_{tf_s}m", f"slope_{tf_s}m", f"slope{tf_s}m", f"score_slope_{tf_s}m"))
    if explicit is not None:
        return explicit
    row_interval = _safe_int(_get(row, "interval"), 0)
    if row_interval == tf:
        cur = _safe_float_or_none(_get(row, "slope_atr_scaled", "slope", "score_slope", "disp_slope"))
        if cur is not None:
            return cur
    gc_row = _get_gc_summary_row(symbol, tf)
    val = _slope_from_summary_row(gc_row)
    if val is not None:
        return val
    db_val = _get_db_short_slope(symbol, tf)
    if db_val is not None:
        return db_val
    return None


def _short_mtf_direction_guard(entry_row: Dict[str, Any], *, symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not _env_bool("ENTRY_SHORT_MTF_REQUIRED", True):
        return None
    if str(source or "").upper() != "SUMMARY_AI":
        return None

    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None

    row = entry_row or {}
    sym = _norm_symbol(symbol or row.get("symbol"))
    eps = abs(_env_float("ENTRY_SHORT_MTF_SLOPE_EPS", 0.0))
    zero_neutral = _env_bool("ENTRY_SHORT_MTF_ZERO_NEUTRAL", True)
    min_aligned = max(1, min(3, _env_int("ENTRY_SHORT_MTF_MIN_ALIGNED", 2)))
    min_available = max(min_aligned, min(3, _env_int("ENTRY_SHORT_MTF_MIN_AVAILABLE", 2)))

    slopes = {
        "slope_1m": _resolve_short_slope(row, symbol=sym, tf=1),
        "slope_3m": _resolve_short_slope(row, symbol=sym, tf=3),
        "slope_5m": _resolve_short_slope(row, symbol=sym, tf=5),
    }

    missing = [k for k, v in slopes.items() if v is None]
    available = {k: v for k, v in slopes.items() if v is not None}
    if len(available) < min_available:
        return _ng(
            "SHORT_MTF_SLOPE_MISSING",
            symbol=sym,
            side=side_u,
            missing=missing,
            slopes=slopes,
            min_available=min_available,
            min_aligned=min_aligned,
            daily_mtf_optional=_env_bool("ENTRY_DAILY_MTF_OPTIONAL", True),
            db_backfill=_env_bool("ENTRY_SHORT_MTF_DB_BACKFILL", True),
            note="not enough short slopes were found in entry_row, global_context, or summary DB",
        )

    if side_u == "BUY":
        aligned = {k: v for k, v in available.items() if v > eps}
        opposite = {k: v for k, v in available.items() if (v < -eps if zero_neutral else v <= eps)}
        reason = "SHORT_MTF_NOT_BUY_ALIGNED"
    else:
        aligned = {k: v for k, v in available.items() if v < -eps}
        opposite = {k: v for k, v in available.items() if (v > eps if zero_neutral else v >= -eps)}
        reason = "SHORT_MTF_NOT_SELL_ALIGNED"

    neutral = {k: v for k, v in available.items() if k not in aligned and k not in opposite}

    if len(aligned) < min_aligned:
        return _ng(
            reason,
            symbol=sym,
            side=side_u,
            aligned=aligned,
            opposite=opposite,
            neutral=neutral,
            slopes=slopes,
            eps=eps,
            min_aligned=min_aligned,
            min_available=min_available,
            zero_neutral=zero_neutral,
            note="daily_mtf_is_optional_not_blocking; relaxed rule requires 2 of 3 short slopes aligned by default",
        )

    logger.warning(
        "[SHORT MTF GUARD] OK symbol=%s side=%s slopes=%s aligned=%s opposite=%s neutral=%s min_aligned=%s min_available=%s daily_mtf_optional=%s db_backfill=%s zero_neutral=%s",
        sym, side_u, slopes, aligned, opposite, neutral, min_aligned, min_available,
        _env_bool("ENTRY_DAILY_MTF_OPTIONAL", True), _env_bool("ENTRY_SHORT_MTF_DB_BACKFILL", True), zero_neutral,
    )
    return None


def _strict_guard_short_mtf_only(*, symbol: str, side: str, row: dict, detail: dict) -> Optional[Dict[str, Any]]:
    return _short_mtf_direction_guard(row, symbol=symbol, side=side, source="SUMMARY_AI")

# ============================================================
# スコープ判定 + env 既定値 + 公開入口
# ============================================================

import sys  # noqa: E402
from sqlite_cm import committing_connect
from nas_root import nas_path, nas_root

_SHORT_MTF_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)

_ENV_APPLIED = False


def apply_env_defaults() -> None:
    """旧 install() が設定していた env 既定値。

    sitecustomize から呼ばれる install() 相当のタイミングで一度だけ適用する。
    """
    global _ENV_APPLIED
    if _ENV_APPLIED:
        return
    _ENV_APPLIED = True
    os.environ.setdefault("ENTRY_SHORT_MTF_REQUIRED", "1")
    os.environ["ENTRY_SHORT_MTF_REQUIRE_ALL"] = os.environ.get("ENTRY_SHORT_MTF_REQUIRE_ALL", "0")
    if os.environ.get("ENTRY_SHORT_MTF_REQUIRE_ALL") == "1" and _env_bool("ENTRY_SHORT_MTF_FORCE_2OF3", True):
        os.environ["ENTRY_SHORT_MTF_REQUIRE_ALL"] = "0"
    os.environ.setdefault("ENTRY_SHORT_MTF_MIN_ALIGNED", "2")
    os.environ.setdefault("ENTRY_SHORT_MTF_MIN_AVAILABLE", "2")
    os.environ.setdefault("ENTRY_SHORT_MTF_SLOPE_EPS", "0.0")
    os.environ.setdefault("ENTRY_DAILY_MTF_OPTIONAL", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_DB_BACKFILL", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_ZERO_NEUTRAL", "1")


def short_mtf_guard_enabled() -> bool:
    """この短期MTFガードをこのプロセスで使うか。

    旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 = main.py 専用。
    ENTRY_SHORT_MTF_GUARD_SCOPE_ENABLED=1/0 で明示的に上書きできる。
    """
    try:
        raw = os.getenv("ENTRY_SHORT_MTF_GUARD_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _env_bool("ENTRY_SHORT_MTF_GUARD_SCOPE_ENABLED", True)
        if _env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv_text for name in _SHORT_MTF_DB_PROCESS_NAMES)
    except Exception:
        return False


def short_mtf_direction_guard(entry_row, *, symbol: str, side: str, source: str):
    """2 of 3 判定版の方向ガード。NG のとき dict を返す。"""
    return _short_mtf_direction_guard(entry_row, symbol=symbol, side=side, source=source)


def summary_ai_strict_guard(*, symbol: str, side: str, row: dict, detail: dict):
    """SUMMARY_AI の strict guard を短期MTF判定だけで行う版。"""
    return _strict_guard_short_mtf_only(symbol=symbol, side=side, row=row, detail=detail)


def install() -> bool:
    """sitecustomize から呼ばれる互換入口。env 既定値の適用だけ行う。

    判定ロジック本体は entry_order_builder / entry_limit_passive_runtime_patch
    から直接呼ばれるため monkeypatch は不要。
    """
    apply_env_defaults()
    logger.warning(
        "[SHORT MTF GUARD] env defaults applied scope=%s required=%s require_all=%s min_aligned=%s eps=%s",
        short_mtf_guard_enabled(),
        os.environ.get("ENTRY_SHORT_MTF_REQUIRED"),
        os.environ.get("ENTRY_SHORT_MTF_REQUIRE_ALL"),
        os.environ.get("ENTRY_SHORT_MTF_MIN_ALIGNED"),
        os.environ.get("ENTRY_SHORT_MTF_SLOPE_EPS"),
    )
    return True


__all__ = [
    "install",
    "apply_env_defaults",
    "short_mtf_guard_enabled",
    "short_mtf_direction_guard",
    "summary_ai_strict_guard",
]
