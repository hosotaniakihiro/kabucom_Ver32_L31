# ============================================================
# trading/exit/mtf_trend_exit.py
# ------------------------------------------------------------
# 3 分足のトレンドで手仕舞う EXIT ルール。
#
# 2026-09-15 ユーザー指示 (エントリーを 3 分足・5 分足の MA/RSI/MACD と
# ini 点数で判定するよう変えたのに合わせ、出口も上位足に揃える):
#
#   BUY  : 3 分足の **確定した** 最新足で 終値 < 5MA、または MACD < シグナル
#   SELL : 終値 > 5MA、または MACD > シグナル
#
#   - 判定に使う足は「足の開始 + 3 分 <= now」の確定足だけ。作りかけの足では
#     判定しない (1 ティックの上下で降りないため)。
#   - エントリー後に確定した足だけを見る (建てた直後の、エントリー前から
#     形成中だった足では判定しない)。
#   - 損切り (ABSOLUTE_ENTRY_STOP_LOSS 0.30%) はこれより先に評価され、そのまま残る。
#   - 利確 (トレール 0.20% / ブローオフ 0.45%) もそのまま残る。
#   - このルールが有効な間、時間基準の
#       THREE_MIN_PROFIT_ESCAPE / THREE_MIN_FLAT_EXIT /
#       EARLY_NO_PROGRESS / EARLY_STAGNATION
#     は評価しない (env EXIT_TIME_RULES_ENABLED=1 で併用に戻せる)。
#
# env:
#   MTF_TREND_EXIT_ENABLED   (1)   一発 OFF は 0
#   MTF_TREND_EXIT_TF        (3)   判定に使う足 (3 or 5)
#   MTF_TREND_EXIT_MA        (ma5) 割れを見る MA
#   MTF_TREND_EXIT_USE_MACD  (1)   MACD とシグナルの逆転でも手仕舞う
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from typing import Any, Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.0-MTF-TREND-EXIT-3M"

_CACHE_TTL_SEC = 3.0
_lock = threading.Lock()
_cache: Dict[int, Tuple[float, pd.DataFrame]] = {}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _f(v: Any, default: float = float("nan")) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        return x if x == x else default
    except Exception:
        return default


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def enabled() -> bool:
    return _env_bool("MTF_TREND_EXIT_ENABLED", True)


def time_rules_enabled() -> bool:
    """時間基準の EXIT ルールを評価するか。trend exit 有効時は既定で止める。"""
    if not enabled():
        return True
    return _env_bool("EXIT_TIME_RULES_ENABLED", False)


def _tf() -> int:
    try:
        v = int(str(os.getenv("MTF_TREND_EXIT_TF", "3")).rstrip("m"))
        return v if v in (3, 5) else 3
    except Exception:
        return 3


def _frame(tf: int) -> pd.DataFrame:
    """tf 分足の全銘柄フレーム (symbol, datetime, close, ma5, ma25, macd, signal)。3 秒キャッシュ。"""
    now = dt.datetime.now().timestamp()
    with _lock:
        hit = _cache.get(tf)
        if hit and now - hit[0] <= _CACHE_TTL_SEC:
            return hit[1]
    df = pd.DataFrame()
    try:
        from core.global_context.context import global_context as GC
        try:
            src = GC.get_summary_history(tf, source="push")
        except TypeError:
            src = GC.get_summary_history(tf)
        if isinstance(src, pd.DataFrame) and not src.empty and "symbol" in src.columns and "datetime" in src.columns:
            cols = [c for c in ("symbol", "datetime", "close", "current_price", "ma5", "ma25", "macd", "signal") if c in src.columns]
            work = src[cols].copy()
            work["symbol"] = work["symbol"].map(_norm_symbol)
            work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
            try:
                work["datetime"] = work["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            work = work.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"], kind="stable")
            df = work.groupby("symbol", as_index=False).tail(3)
    except Exception:
        logger.debug("[MTF TREND EXIT] history fetch failed tf=%s", tf, exc_info=True)
        df = pd.DataFrame()
    with _lock:
        _cache[tf] = (now, df)
    return df


def _completed_bar(symbol: str, tf: int, now: dt.datetime, entry_time: Optional[dt.datetime]) -> Optional[pd.Series]:
    """エントリー後に確定した最新の足。無ければ None。"""
    df = _frame(tf)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    rows = df[df["symbol"] == symbol]
    if rows.empty:
        return None
    bar_len = dt.timedelta(minutes=tf)
    for _, r in rows.sort_values("datetime", ascending=False).iterrows():
        start = r["datetime"].to_pydatetime() if hasattr(r["datetime"], "to_pydatetime") else r["datetime"]
        if start + bar_len > now:
            continue  # 作りかけの足
        if entry_time is not None and start < entry_time:
            return None  # エントリー前から形成中だった足 → まだ判定しない
        return r
    return None


def judge_mtf_trend_exit(
    *, symbol: Any, side: Any, entry_time: Optional[dt.datetime], now: dt.datetime
) -> Tuple[bool, str, Dict[str, Any]]:
    """戻り値: (手仕舞うか, 理由, 詳細)。"""
    if not enabled():
        return False, "", {"enabled": False}
    sym = _norm_symbol(symbol)
    side_u = str(side or "").strip().upper()
    if side_u not in ("BUY", "SELL"):
        return False, "", {"side": side_u}
    tf = _tf()
    ma_name = os.getenv("MTF_TREND_EXIT_MA", "ma5").strip() or "ma5"
    use_macd = _env_bool("MTF_TREND_EXIT_USE_MACD", True)

    bar = _completed_bar(sym, tf, now, entry_time)
    if bar is None:
        return False, "", {"tf": tf, "reason": "no_completed_bar_after_entry"}
    close = _f(bar.get("close"))
    if close != close or close <= 0:
        close = _f(bar.get("current_price"))
    ma = _f(bar.get(ma_name))
    macd, sig = _f(bar.get("macd")), _f(bar.get("signal"))
    bar_dt = str(bar.get("datetime"))
    detail = {"tf": tf, "bar": bar_dt, "close": close, ma_name: ma, "macd": macd, "signal": sig}
    if close != close or close <= 0 or ma != ma or ma <= 0:
        return False, "", {**detail, "reason": "no_close_or_ma"}

    if side_u == "BUY":
        if close < ma:
            return True, f"MTF_TREND_EXIT_BUY {tf}m close={close:.1f}<{ma_name}={ma:.1f} bar={bar_dt}", detail
        if use_macd and macd == macd and sig == sig and macd < sig:
            return True, f"MTF_TREND_EXIT_BUY {tf}m macd={macd:.2f}<signal={sig:.2f} bar={bar_dt}", detail
    else:
        if close > ma:
            return True, f"MTF_TREND_EXIT_SELL {tf}m close={close:.1f}>{ma_name}={ma:.1f} bar={bar_dt}", detail
        if use_macd and macd == macd and sig == sig and macd > sig:
            return True, f"MTF_TREND_EXIT_SELL {tf}m macd={macd:.2f}>signal={sig:.2f} bar={bar_dt}", detail
    return False, "", detail
