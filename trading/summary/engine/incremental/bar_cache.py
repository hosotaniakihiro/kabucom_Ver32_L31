# ============================================================
# File   : trading/summary/engine/incremental/bar_cache.py
# ------------------------------------------------------------
# 1分足 (および3分/5分足) の差分キャッシュ。
#
# 【なぜ必要か】
#   summary_database は push_receiver とは別プロセスなので
#   global_data.push_df がメモリ上に無く、毎 tick
#     [PUSH SUMMARY DB SOURCE] use db because memory push_df empty rows=30000
#   のように PUSH DB から直近20分ぶんの tick を読み直し、
#   その 20,000〜30,000 行を毎回すべて足に組み直している。
#   実測 (2026-08-17):
#       入力 tick        20,000〜30,000 行 / 分
#       中間 1分足        4,257 行 x 46列 / 243銘柄
#       実際の新規行      1分あたり 4〜7 行
#       job elapsed      51〜58 秒 (tick 間隔 60 秒 / timeout 25 秒)
#   さらに rows=30000 は PUSH_SUMMARY_DB_MAX_ROWS の上限に張り付いており、
#   ORDER BY datetime DESC LIMIT 30000 で 20分窓の古い側が切り捨てられて
#   ma25/ma75 の履歴が銘柄によって欠ける状態でもあった。
#
# 【方針】
#   完成済みの足をプロセス内に保持し、毎 tick は「最後にキャッシュした足以降」
#   だけを組み直してマージする。指標はマージ後のフレーム全体に掛けるので、
#   ma25/ma75 の履歴はキャッシュ側が持つ。
#
# 【期間出来高の注意】
#   足の volume は「symbol+当日で累計出来高を shift(1) して差分」した値なので、
#   tick を切り出すと各 symbol の先頭行が period=0 になり出来高が過少になる。
#   キャッシュしている1つ前の足の cumulative_volume を prev_cumulative として
#   渡して補う (summary_incremental_engine._seed_period_volume_from_prev)。
#
# 【切り替え】
#   SUMMARY_BAR_CACHE_MODE
#     off    : 従来どおり毎回フル再構築 (既定)
#     shadow : 両方計算して差分をログに出し、返すのは従来の結果
#     on     : キャッシュ側の結果を返す
# ============================================================

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_CACHE: dict[int, pd.DataFrame] = {}

_KEY = ["symbol", "datetime"]


def get_mode() -> str:
    try:
        v = str(os.getenv("SUMMARY_BAR_CACHE_MODE", "off")).strip().lower()
        return v if v in ("off", "shadow", "on") else "off"
    except Exception:
        return "off"


_SHADOW_TICK: dict[int, int] = {}


def shadow_should_compare(interval: int) -> bool:
    """shadow は両方計算するぶん tick が重くなるので、間引けるようにする。

    SUMMARY_BAR_CACHE_SHADOW_EVERY=1 なら毎 tick 比較 (既定)。
    5 なら 5 tick に 1 回だけ比較し、それ以外の tick は従来経路だけを通す。
    """
    try:
        every = max(1, int(float(os.getenv("SUMMARY_BAR_CACHE_SHADOW_EVERY", "1"))))
    except Exception:
        every = 1
    with _LOCK:
        n = _SHADOW_TICK.get(int(interval), 0) + 1
        _SHADOW_TICK[int(interval)] = n
    return (n % every) == 0


def _max_bars_per_symbol() -> int:
    try:
        return max(1, int(float(os.getenv("SUMMARY_BAR_CACHE_MAX_BARS", "240"))))
    except Exception:
        return 240


def reset(interval: int | None = None) -> None:
    """キャッシュを捨てる (日付が変わったとき等)。"""
    with _LOCK:
        if interval is None:
            _CACHE.clear()
            _SHADOW_TICK.clear()
        else:
            _CACHE.pop(int(interval), None)
            _SHADOW_TICK.pop(int(interval), None)


def snapshot(interval: int) -> pd.DataFrame | None:
    """キャッシュしている素の足のコピー (無ければ None)。DB への補完書き込み用。"""
    with _LOCK:
        df = _CACHE.get(int(interval))
        return None if df is None else df.copy()


def cached_rows(interval: int) -> int:
    with _LOCK:
        df = _CACHE.get(int(interval))
        return 0 if df is None else len(df)


def _trim(bars: pd.DataFrame) -> pd.DataFrame:
    n = _max_bars_per_symbol()
    if bars.empty or "symbol" not in bars.columns:
        return bars
    return (
        bars.sort_values(_KEY, kind="stable")
        .groupby("symbol", as_index=False, sort=False)
        .tail(n)
        .reset_index(drop=True)
    )


def _prev_cumulative_map(bars: pd.DataFrame, before) -> dict[str, float]:
    """before より前の足のうち、symbol ごとに最新の cumulative_volume。"""
    if bars is None or bars.empty or "cumulative_volume" not in bars.columns:
        return {}
    try:
        older = bars[pd.to_datetime(bars["datetime"], errors="coerce") < before]
        if older.empty:
            return {}
        latest = older.sort_values(_KEY, kind="stable").groupby("symbol", sort=False).tail(1)
        out: dict[str, float] = {}
        for sym, cum in zip(latest["symbol"].astype(str), latest["cumulative_volume"]):
            v = pd.to_numeric(pd.Series([cum]), errors="coerce").iloc[0]
            if pd.notna(v):
                out[sym] = float(v)
        return out
    except Exception:
        logger.debug("[BAR CACHE] prev cumulative map failed", exc_info=True)
        return {}


def build_raw_bars_cached(
    *,
    interval: int,
    ticks: pd.DataFrame,
    raw_builder: Callable[..., pd.DataFrame],
) -> pd.DataFrame:
    """キャッシュを使って素の足 (指標前) を返す。

    ticks は _normalize_push_df 済みのもの。
    raw_builder(interval=..., ticks=..., prev_cumulative=...) を呼ぶ。
    """
    interval = int(interval)

    with _LOCK:
        cached = _CACHE.get(interval)

    if cached is None or cached.empty:
        bars = raw_builder(interval=interval, ticks=ticks, prev_cumulative=None)
        bars = _trim(bars)
        with _LOCK:
            _CACHE[interval] = bars.copy()
        logger.info("[BAR CACHE] cold build interval=%s bars=%s ticks=%s", interval, len(bars), len(ticks))
        return bars

    dt_cached = pd.to_datetime(cached["datetime"], errors="coerce")
    last_slot = dt_cached.max()
    if pd.isna(last_slot):
        with _LOCK:
            _CACHE.pop(interval, None)
        return build_raw_bars_cached(interval=interval, ticks=ticks, raw_builder=raw_builder)

    # 最後の足はまだ確定していない可能性があるので作り直す
    tick_dt = pd.to_datetime(ticks["datetime"], errors="coerce")
    fresh_ticks = ticks[tick_dt >= last_slot]

    prev_map = _prev_cumulative_map(cached, last_slot)
    new_bars = raw_builder(interval=interval, ticks=fresh_ticks, prev_cumulative=prev_map)

    keep = cached[dt_cached < last_slot]
    if new_bars is None or new_bars.empty:
        merged = keep
    elif keep.empty:
        merged = new_bars
    else:
        merged = pd.concat([keep, new_bars], ignore_index=True, sort=False)

    merged = merged.drop_duplicates(subset=_KEY, keep="last")
    merged = _trim(merged)

    with _LOCK:
        _CACHE[interval] = merged.copy()

    logger.info(
        "[BAR CACHE] incremental interval=%s ticks_in=%s ticks_used=%s bars=%s (kept=%s new=%s) last_slot=%s",
        interval, len(ticks), len(fresh_ticks), len(merged), len(keep),
        0 if new_bars is None else len(new_bars), last_slot,
    )
    return merged


def compare_for_shadow(full: pd.DataFrame, cached: pd.DataFrame, interval: int) -> dict[str, Any]:
    """shadow 実行用: フル再構築とキャッシュ版の差を要約する。"""
    info: dict[str, Any] = {
        "interval": int(interval),
        "rows_full": 0 if full is None else len(full),
        "rows_cached": 0 if cached is None else len(cached),
    }
    try:
        if full is None or cached is None or full.empty or cached.empty:
            info["status"] = "empty"
            return info

        cols = [c for c in full.columns if c in cached.columns]
        info["cols_only_full"] = sorted(set(full.columns) - set(cached.columns))[:10]
        info["cols_only_cached"] = sorted(set(cached.columns) - set(full.columns))[:10]

        a = full[cols].sort_values(_KEY, kind="stable").reset_index(drop=True)
        b = cached[cols].sort_values(_KEY, kind="stable").reset_index(drop=True)

        # フル再構築は「直近 N 分の tick」しか見ていないため、窓の先頭にかかる足は
        # tick が途中から始まっており open / volume / tick_count が本来と違う。
        # キャッシュ側はその足を完全な状態で持っているので、ここを比べると
        # 「キャッシュの方が正しいのに different」になる。
        # 各 symbol の最古の足を比較対象から外す。
        try:
            a_dt = pd.to_datetime(a["datetime"], errors="coerce")
            first_per_symbol = a_dt.groupby(a["symbol"]).transform("min")
            boundary = a_dt <= first_per_symbol
            info["excluded_boundary_rows"] = int(boundary.sum())
            a = a[~boundary]
        except Exception:
            logger.debug("[BAR CACHE] boundary exclusion failed", exc_info=True)

        # キャッシュ側は古い足を保持しているぶん行数が多くなりうるので、
        # 共通の (symbol, datetime) だけを突き合わせる
        merged = a.merge(b, on=_KEY, how="inner", suffixes=("__f", "__c"))
        info["rows_compared"] = len(merged)

        diffs: list[str] = []
        for c in cols:
            if c in _KEY:
                continue
            fa, fb = merged.get(c + "__f"), merged.get(c + "__c")
            if fa is None or fb is None:
                continue
            if fa.equals(fb):
                continue
            try:
                na = pd.to_numeric(fa, errors="coerce")
                nb = pd.to_numeric(fb, errors="coerce")
                if na.notna().any() or nb.notna().any():
                    gap = (na - nb).abs()
                    if bool((gap.fillna(0.0) <= 1e-9).all()):
                        continue
                    diffs.append("%s(max_gap=%.6g)" % (c, float(gap.max() or 0.0)))
                    continue
            except Exception:
                pass
            diffs.append(c)

        info["diff_cols"] = diffs[:15]
        info["diff_count"] = len(diffs)
        info["status"] = "identical" if not diffs else "different"
    except Exception:
        logger.exception("[BAR CACHE] shadow compare failed interval=%s", interval)
        info["status"] = "compare_failed"
    return info
