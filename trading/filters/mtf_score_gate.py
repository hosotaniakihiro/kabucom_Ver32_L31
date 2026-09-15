# ============================================================
# trading/filters/mtf_score_gate.py
# ------------------------------------------------------------
# score_config.ini の点数を 3 分足 / 5 分足の指標で計算し直すゲート。
#
# 2026-09-15 ユーザー指示: 「ini 側の点数を 3 分・5 分足の指標で計算し直したい」
#
# 現状:
#   score_config.ini ([buy_entry]/[buy_bonus]/[sell_entry]/[sell_bonus]) の
#   フラグ点数は 1 分足の summary フレームにだけ付く (main.py は 1m-only
#   スコープで 3m/5m の summary ジョブを回さない。3m/5m のフレーム自体は
#   GC.get_summary_history(3/5) に ma5/ma25/rsi/macd/signal 付きで存在する)。
#
# ここでやること:
#   候補ごとに 3m と 5m の最新足を取り、1m と同じ flag builder
#   (trading/indicators/flag_builder_buy / _sell) でフラグを立て、
#   ini の点数表 (candidates._load_score_tables) で合計する。
#   BUY  : score_3m >= MIN かつ score_5m >= MIN   (buy_entry + buy_bonus)
#   SELL : 同様に sell_entry + sell_bonus
#   MIN は env MTF_SCORE_MIN、無ければ SUMMARY_ENTRY_MIN_SCORE (既定 3.0)。
#   3m/5m の足が無い・フラグが立たない場合は fail-closed (却下)。
#
# 一発 OFF: env MTF_SCORE_GATE_ENABLED=0
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.0-MTF-SCORE-GATE-3M5M-INI"

_TFS = (3, 5)
_CACHE_TTL_SEC = 5.0
_lock = threading.Lock()
_frame_cache: Dict[int, Tuple[float, pd.DataFrame]] = {}
_tables_cache: Optional[Dict[str, Dict[str, int]]] = None


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def gate_enabled() -> bool:
    return _env_bool("MTF_SCORE_GATE_ENABLED", True)


def min_score() -> float:
    v = os.getenv("MTF_SCORE_MIN")
    if v not in (None, ""):
        return _env_float("MTF_SCORE_MIN", 3.0)
    return _env_float("SUMMARY_ENTRY_MIN_SCORE", 3.0)


def _latest_frame(tf: int) -> pd.DataFrame:
    """tf 分足の銘柄ごと最新行 (全列)。5 秒キャッシュ。"""
    now = dt.datetime.now().timestamp()
    with _lock:
        hit = _frame_cache.get(tf)
        if hit and now - hit[0] <= _CACHE_TTL_SEC:
            return hit[1]
    df = pd.DataFrame()
    try:
        from core.global_context.context import global_context as GC
        try:
            src = GC.get_summary_history(tf, source="push")
        except TypeError:
            src = GC.get_summary_history(tf)
        if isinstance(src, pd.DataFrame) and not src.empty and "symbol" in src.columns:
            work = src.copy()
            work["symbol"] = work["symbol"].map(_norm_symbol)
            if "datetime" in work.columns:
                work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
                work = work.sort_values(["symbol", "datetime"], kind="stable")
            df = work.groupby("symbol", as_index=False).tail(1).set_index("symbol", drop=False)
    except Exception:
        logger.debug("[MTF SCORE GATE] history fetch failed tf=%s", tf, exc_info=True)
        df = pd.DataFrame()
    with _lock:
        _frame_cache[tf] = (now, df)
    return df


def _tables() -> Dict[str, Dict[str, int]]:
    global _tables_cache
    if _tables_cache is not None:
        return _tables_cache
    try:
        from trading.entry.summary_ai.candidates import _load_score_tables
        _tables_cache = _load_score_tables() or {}
    except Exception:
        logger.exception("[MTF SCORE GATE] score_config.ini の点数表が読めない")
        _tables_cache = {}
    return _tables_cache


def _score_row(row: pd.Series, side: str) -> Tuple[Optional[float], List[str]]:
    """1 行にフラグを立てて ini の点数で合計する。None = 評価できない。"""
    try:
        from trading.indicators.flag_builder_buy import build_buy_flags
        from trading.indicators.flag_builder_sell import build_sell_flags
        from trading.entry.summary_ai.candidates import _score_flags_for_row
    except Exception:
        logger.exception("[MTF SCORE GATE] flag builder / scorer import failed")
        return None, []
    one = pd.DataFrame([row])
    try:
        built = build_buy_flags(one.copy())
        built = build_sell_flags(built)
    except Exception:
        logger.exception("[MTF SCORE GATE] flag build failed symbol=%s", row.get("symbol"))
        return None, []
    if not isinstance(built, pd.DataFrame) or built.empty:
        return None, []
    r = built.iloc[0]
    t = _tables()
    if side == "BUY":
        entry_t, bonus_t = t.get("buy_entry", {}), t.get("buy_bonus", {})
    else:
        entry_t, bonus_t = t.get("sell_entry", {}), t.get("sell_bonus", {})
    if not entry_t and not bonus_t:
        return None, []
    s1, h1 = _score_flags_for_row(r, entry_t)
    s2, h2 = _score_flags_for_row(r, bonus_t)
    return float(s1) + float(s2), list(h1) + list(h2)


def mtf_score_gate(symbol: Any, side: Any) -> Tuple[bool, str]:
    """3m/5m の ini 点数ゲート。戻り値: (許可, 理由)。"""
    if not gate_enabled():
        return True, "MTF_SCORE_GATE_DISABLED"
    sym = _norm_symbol(symbol)
    side_u = str(side or "").strip().upper()
    if side_u in ("LONG", "2"):
        side_u = "BUY"
    elif side_u in ("SHORT", "1"):
        side_u = "SELL"
    if side_u not in ("BUY", "SELL"):
        return True, "MTF_SCORE_GATE_SIDE_NONE"

    need = min_score()
    parts: List[str] = []
    ok_all = True
    for tf in _TFS:
        df = _latest_frame(tf)
        if not isinstance(df, pd.DataFrame) or df.empty or sym not in df.index:
            ok_all = False
            parts.append(f"{tf}m:no_bar")
            continue
        score, hits = _score_row(df.loc[sym], side_u)
        if score is None:
            ok_all = False
            parts.append(f"{tf}m:no_score")
            continue
        flag = "ok" if score >= need else "NG"
        parts.append(f"{tf}m:{flag} score={score:.1f} hits={hits[:8]}")
        if score < need:
            ok_all = False
    detail = f"min={need:.1f} " + " | ".join(parts)
    if ok_all:
        return True, f"MTF_SCORE_OK {detail}"
    return False, f"MTF_SCORE_NG {detail}"
