# ============================================================
# trading/filters/mtf_ma_gate.py
# ------------------------------------------------------------
# 3 分足 / 5 分足の 5MA・25MA を使ったエントリーゲート (全経路共通)。
#
# 2026-09-15 ユーザー指示:
#   「3分足5分足の 5MA 25MA を使っていない。使うように変更して。
#     それまでエントリーしないようにして」
#
# 背景:
#   ランキング経路には f864193 (9/15 00:54) で MTF_MA ゲートが入ったが、
#   前日のエントリー (7013 / 8001 / 9984) は SUMMARY 経路で、そちらは
#   3m/5m から score_3m / score_5m しか付けておらず MA を見ていなかった。
#   経路ごとに入れると漏れるので、entry_controller の共通パイプライン
#   (atr / range_5m の直後、AI ゲートの前) に 1 箇所で入れる。
#
# 判定 (V1.1, 2026-09-15 08:5x のユーザー指示
#        「テクニカル指標は 3 分足・5 分足の 5MA・25MA を使い、これをもとにエントリー」):
#   BUY  : 終値 > 5MA > 25MA   (終値が両 MA の上、かつ 5MA が 25MA の上)
#   SELL : 終値 < 5MA < 25MA
#   3 分足 と 5 分足の **両方** で成立 (ALL)。1 分足は見ない。
#   env: MTF_MA_GATE_CONFIRM_MODE=ANY / MTF_MA_GATE_REQUIRE_ORDER=0 / MTF_MA_GATE_BASE_TF=1m
#
# データ:
#   GC.get_summary_history(tf, source="push") の各銘柄の最新足。
#   summary_incremental_engine が ma5/ma25/ma75 を rolling(min_periods=1)
#   で付けているので、足が 1 本でもあれば値は入る (寄り直後は短い窓の平均)。
#
# データ無しの扱い (ランキング側と違う点):
#   ユーザー要件は「MA を見るまでエントリーしない」なので、確認足 (3m/5m)
#   のデータが両方無ければ **fail-closed** (却下)。
#   env MTF_MA_GATE_ALLOW_NO_DATA=1 でランキング側と同じ fail-open に戻せる。
#
# 一発 OFF: env MTF_MA_GATE_ENABLED=0
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from typing import Any, Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.2-MTF-GATE-3M5M-MA-RSI-MACD"

_TF_MAP = {1: "1m", 3: "3m", 5: "5m"}
_CACHE_TTL_SEC = 5.0

_cache_lock = threading.Lock()
_cache: Dict[int, Tuple[float, pd.DataFrame]] = {}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if x != x:  # NaN
            return default
        return x
    except Exception:
        return default


def _env_f(name: str, default: float) -> float:
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


def _cfg() -> Dict[str, Any]:
    try:
        from config.ranking_entry_config import RANKING_ENTRY_CONFIG
        return dict(RANKING_ENTRY_CONFIG.get("MTF_MA", {}) or {})
    except Exception:
        return {}


def gate_enabled() -> bool:
    return _env_bool("MTF_MA_GATE_ENABLED", True)


def _latest_rows(tf: int) -> pd.DataFrame:
    """tf 分足の銘柄ごと最新行 (symbol, close, ma5, ma25, ma75, datetime)。5 秒キャッシュ。"""
    now = dt.datetime.now().timestamp()
    with _cache_lock:
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
        if isinstance(src, pd.DataFrame) and not src.empty and "symbol" in src.columns:
            cols = [c for c in ("symbol", "datetime", "close", "current_price", "ma5", "ma25", "ma75", "rsi", "macd", "signal", "hist") if c in src.columns]
            work = src[cols].copy()
            work["symbol"] = work["symbol"].map(_norm_symbol)
            if "datetime" in work.columns:
                work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
                work = work.sort_values(["symbol", "datetime"], kind="stable")
            df = work.groupby("symbol", as_index=False).tail(1).set_index("symbol", drop=False)
    except Exception:
        logger.debug("[MTF MA GATE] history fetch failed tf=%s", tf, exc_info=True)
        df = pd.DataFrame()
    with _cache_lock:
        _cache[tf] = (now, df)
    return df


def _tf_values(symbol: str, tf: int, entry_row: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """指定足の close / ma5 / ma25 / ma75 を返す。無い値は 0.0。"""
    out: Dict[str, float] = {}
    tf_s = _TF_MAP[tf]
    # entry_row にサフィックス付きで既に載っていればそれを優先 (ランキング経路)
    if isinstance(entry_row, dict):
        c = _f(entry_row.get(f"close_{tf_s}"))
        if c > 0:
            out["close"] = c
            for ma in ("ma5", "ma25", "ma75", "rsi", "macd", "signal", "hist"):
                out[ma] = _f(entry_row.get(f"{ma}_{tf_s}"), float("nan"))
            return out
    df = _latest_rows(tf)
    if isinstance(df, pd.DataFrame) and not df.empty and symbol in df.index:
        r = df.loc[symbol]
        c = _f(r.get("close"))
        if c <= 0:
            c = _f(r.get("current_price"))
        out["close"] = c
        for ma in ("ma5", "ma25", "ma75"):
            out[ma] = _f(r.get(ma))
        for k in ("rsi", "macd", "signal", "hist"):
            out[k] = _f(r.get(k), float("nan"))
        return out
    # 1 分足だけは entry_row 自身の値でも代用できる
    if tf == 1 and isinstance(entry_row, dict):
        c = _f(entry_row.get("close") or entry_row.get("current_price") or entry_row.get("price"))
        if c > 0:
            out["close"] = c
            for ma in ("ma5", "ma25", "ma75"):
                out[ma] = _f(entry_row.get(ma))
    return out


def _side_ok(vals: Dict[str, float], side: str, mas, tf_s: str, *, require_order: bool = True) -> Tuple[Optional[bool], str]:
    """(True/False/None, detail)。None = データ不足。

    BUY : close > 5MA かつ close > 25MA、さらに require_order なら 5MA > 25MA
          (上昇の並び: 終値 > 5MA > 25MA)
    SELL: close < 5MA かつ close < 25MA、さらに require_order なら 5MA < 25MA
    MA が 1 本も無ければ None。require_order の判定は ma5/ma25 が両方ある時だけ。
    """
    close = _f(vals.get("close"))
    if close <= 0:
        return None, f"{tf_s}:no_close"
    checked = 0
    for ma in mas:
        mav = _f(vals.get(ma))
        if mav <= 0:
            continue
        checked += 1
        if side == "BUY" and close <= mav:
            return False, f"{tf_s}:{ma} c={close:.1f}<={mav:.1f}"
        if side == "SELL" and close >= mav:
            return False, f"{tf_s}:{ma} c={close:.1f}>={mav:.1f}"
    if checked == 0:
        return None, f"{tf_s}:no_ma"
    if require_order and "ma5" in mas and "ma25" in mas:
        m5, m25 = _f(vals.get("ma5")), _f(vals.get("ma25"))
        if m5 > 0 and m25 > 0:
            if side == "BUY" and m5 <= m25:
                return False, f"{tf_s}:order ma5={m5:.1f}<=ma25={m25:.1f}"
            if side == "SELL" and m5 >= m25:
                return False, f"{tf_s}:order ma5={m5:.1f}>=ma25={m25:.1f}"
    # ---- RSI / MACD (2026-09-15 09:0x のユーザー指示: RSI・MACD も 3m/5m で計算して根拠にする)
    #   BUY : RSI が [RSI_BUY_MIN, RSI_BUY_MAX] (既定 50〜75: 上向きだが買われ過ぎでない)
    #         MACD > signal (ヒストグラム > 0)。MACD_REQUIRE_POSITIVE=1 なら MACD > 0 も要求
    #   SELL: RSI が [RSI_SELL_MIN, RSI_SELL_MAX] (既定 25〜50)、MACD < signal
    #   値が NaN (未計算) なら fail-closed で却下。env で個別に OFF にできる。
    if _env_bool("MTF_GATE_RSI_ENABLED", True):
        rsi = _f(vals.get("rsi"), float("nan"))
        if rsi != rsi:
            return False, f"{tf_s}:rsi_nan"
        if side == "BUY":
            lo, hi = _env_f("MTF_GATE_RSI_BUY_MIN", 50.0), _env_f("MTF_GATE_RSI_BUY_MAX", 75.0)
        else:
            lo, hi = _env_f("MTF_GATE_RSI_SELL_MIN", 25.0), _env_f("MTF_GATE_RSI_SELL_MAX", 50.0)
        if not (lo <= rsi <= hi):
            return False, f"{tf_s}:rsi {rsi:.1f} not in [{lo:.0f},{hi:.0f}]"
    if _env_bool("MTF_GATE_MACD_ENABLED", True):
        macd, sig = _f(vals.get("macd"), float("nan")), _f(vals.get("signal"), float("nan"))
        if macd != macd or sig != sig:
            return False, f"{tf_s}:macd_nan"
        if side == "BUY" and macd <= sig:
            return False, f"{tf_s}:macd {macd:.2f}<=signal {sig:.2f}"
        if side == "SELL" and macd >= sig:
            return False, f"{tf_s}:macd {macd:.2f}>=signal {sig:.2f}"
        if _env_bool("MTF_GATE_MACD_REQUIRE_SIGN", False):
            if side == "BUY" and macd <= 0:
                return False, f"{tf_s}:macd {macd:.2f}<=0"
            if side == "SELL" and macd >= 0:
                return False, f"{tf_s}:macd {macd:.2f}>=0"
    return True, (
        f"{tf_s}:ok c={close:.1f} ma5={_f(vals.get('ma5')):.1f} ma25={_f(vals.get('ma25')):.1f}"
        f" rsi={_f(vals.get('rsi'), float('nan')):.1f} macd={_f(vals.get('macd'), float('nan')):.2f}/{_f(vals.get('signal'), float('nan')):.2f}"
    )


def mtf_ma_gate(symbol: Any, side: Any, entry_row: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """全経路共通の MTF MA ゲート。戻り値: (許可, 理由)。"""
    if not gate_enabled():
        return True, "MTF_MA_GATE_DISABLED"
    sym = _norm_symbol(symbol)
    side_u = str(side or "").strip().upper()
    if side_u in ("LONG", "2"):
        side_u = "BUY"
    elif side_u in ("SHORT", "1"):
        side_u = "SELL"
    if side_u not in ("BUY", "SELL"):
        return True, "MTF_MA_GATE_SIDE_NONE"

    # ユーザー要件 (2026-09-15 08:5x):
    #   「テクニカル指標は 3 分足・5 分足の 5MA・25MA を使い、これをもとにエントリーする」
    #   -> 判定は 3 分足と 5 分足の両方 (ALL)。1 分足は見ない。
    #      各足で 終値 > 5MA > 25MA (BUY) / 終値 < 5MA < 25MA (SELL)。
    #   env で緩められる: MTF_MA_GATE_CONFIRM_MODE=ANY / MTF_MA_GATE_REQUIRE_ORDER=0 /
    #   MTF_MA_GATE_BASE_TF=1m (1 分足も要求する)。
    mas = tuple(x.strip() for x in os.getenv("MTF_MA_GATE_MAS", "ma5,ma25").split(",") if x.strip())
    base_tf_s = os.getenv("MTF_MA_GATE_BASE_TF", "").strip()
    base_tf = int(base_tf_s.rstrip("m")) if base_tf_s else 0
    confirm_tfs = tuple(int(str(x).rstrip("m")) for x in os.getenv("MTF_MA_GATE_CONFIRM_TFS", "3m,5m").split(",") if x.strip())
    confirm_mode = os.getenv("MTF_MA_GATE_CONFIRM_MODE", "ALL").strip().upper()
    require_order = _env_bool("MTF_MA_GATE_REQUIRE_ORDER", True)
    allow_no_data = _env_bool("MTF_MA_GATE_ALLOW_NO_DATA", False)

    base_detail = "base:none"
    if base_tf in _TF_MAP:
        base_ok, base_detail = _side_ok(_tf_values(sym, base_tf, entry_row), side_u, mas, _TF_MAP[base_tf], require_order=require_order)
        if base_ok is False:
            return False, f"MTF_MA_BASE_NG {base_detail}"
        if base_ok is None and not allow_no_data:
            return False, f"MTF_MA_BASE_NODATA {base_detail}"

    results = []
    details = []
    for tf in confirm_tfs:
        ok, det = _side_ok(_tf_values(sym, tf, entry_row), side_u, mas, _TF_MAP[tf], require_order=require_order)
        results.append(ok)
        details.append(det)
    detail_s = ";".join(details)

    if not any(r is not None for r in results):
        if allow_no_data:
            return True, f"MTF_MA_OK_CONFIRM_NODATA base={base_detail}"
        return False, f"MTF_MA_CONFIRM_NODATA {detail_s}"

    if confirm_mode == "ALL":
        if any(r is False for r in results) or any(r is None for r in results):
            return False, f"MTF_MA_CONFIRM_ALL_NG {detail_s}"
        return True, f"MTF_MA_OK_ALL base={base_detail} {detail_s}"
    if any(r is True for r in results):
        return True, f"MTF_MA_OK_ANY base={base_detail} {detail_s}"
    return False, f"MTF_MA_CONFIRM_ANY_NG {detail_s}"
