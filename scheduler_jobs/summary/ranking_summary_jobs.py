# ============================================================
# File   : scheduler_jobs/summary/ranking_summary_jobs.py
# Ver    : PRODUCTION-STABLE-REV1.1-RANKING-SUMMARY-JOBS-AI-HOOK-V20
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリー専用の定時ジョブ入口
#
# 【重要方針】
#   - PUSH由来 summary とは完全分離
#   - stock_summary_* は読まない・書かない
#   - ranking_snapshot_1min.current_price を close として扱う
#   - Yahoo 1分足 close はランキング価格系列補完として利用
#   - ranking_summary_1min / 3min / 5min に保存
#   - 保存/表示後、summary_ai_entry_hook_v20 へ通す
#
# REV1.1:
#   - ランキング専用定時ジョブからもAI hook v20を直接呼ぶ
#   - BUY TOP20 / SELL TOP20 をAIへ渡す
#   - ログで hook=v20 を明示
# ============================================================

from __future__ import annotations

import datetime as dt
import inspect
import logging
import os
import threading
import time
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


_LOCKS: dict[int, threading.Lock] = {
    1: threading.Lock(),
    3: threading.Lock(),
    5: threading.Lock(),
}

_LAST_RUN_AT: dict[int, float] = {
    1: 0.0,
    3: 0.0,
    5: 0.0,
}

_MIN_INTERVAL_SEC: dict[int, float] = {
    1: 20.0,
    3: 40.0,
    5: 40.0,
}


def _get_global_data():
    try:
        from global_state import global_data  # type: ignore
        return global_data
    except Exception:
        pass

    try:
        from core.global_context.context import global_data  # type: ignore
        return global_data
    except Exception:
        return None


def _set_global_attr(name: str, value) -> None:
    gd = _get_global_data()
    if gd is None:
        return

    try:
        setattr(gd, name, value)
    except Exception:
        pass


def _get_global_attr(name: str, default=None):
    gd = _get_global_data()
    if gd is None:
        return default

    try:
        return getattr(gd, name, default)
    except Exception:
        return default


def _store_ranking_summary_cache(interval: int, df: pd.DataFrame) -> None:
    if df is None:
        return

    cache_name = f"ranking_summary_{interval}min_df"
    _set_global_attr(cache_name, df)

    cache = _get_global_attr("ranking_summary_cache", None)
    if cache is None or not isinstance(cache, dict):
        cache = {}

    cache[int(interval)] = df
    _set_global_attr("ranking_summary_cache", cache)
    _set_global_attr("last_ranking_summary_updated_at", dt.datetime.now())


def _now() -> dt.datetime:
    return dt.datetime.now()


def _is_market_related_time(now: Optional[dt.datetime] = None) -> bool:
    now = now or _now()
    t = now.time()
    return dt.time(8, 30) <= t <= dt.time(16, 30)


def _should_run_by_clock(interval: int, now: Optional[dt.datetime] = None) -> bool:
    """この interval を今走らせてよいか。

    P0-5: ここも `now.minute % interval == 0` だった。
    _job_ranking_summary_all_core 側のゲートだけ直しても、
    こちらで二重に弾かれて 5m が作られないままだった
    (実測 2026-08-27 09:12: `long tf due interval=5` の直後に
     `skip by clock interval=5 now=09:12:16`)。

    外側と同じ「前回作った足より新しい完成足があるか」で揃える。
    _long_tf_due は force を見ないので、ここでは due 判定だけを使う。
    """
    now = now or _now()
    interval = int(interval)
    if interval <= 1:
        return True
    if not _long_tf_slot_gate_enabled():
        return now.minute % interval == 0
    due, why = _long_tf_due(now, interval, force=False)
    if not due:
        logger.info(
            "[RANKING SUMMARY JOB] clock gate skip interval=%s reason=%s now=%s",
            interval, why, now.strftime("%H:%M:%S"),
        )
    return due


def _guard_duplicate_run(interval: int) -> bool:
    interval = int(interval)
    now_ts = time.time()
    last = _LAST_RUN_AT.get(interval, 0.0)
    min_sec = _MIN_INTERVAL_SEC.get(interval, 20.0)

    if now_ts - last < min_sec:
        logger.info(
            "[RANKING SUMMARY JOB] skip duplicate interval=%s elapsed=%.1fs min=%.1fs",
            interval,
            now_ts - last,
            min_sec,
        )
        return False

    _LAST_RUN_AT[interval] = now_ts
    return True


def _run_ranking_ai_hook_v20(*, interval: int, now: dt.datetime, df: pd.DataFrame) -> bool:
    """
    ランキング由来サマリーをPUSH同様にAI判定へ渡す。
    ranking_summary_jobs.py は runner_core.py を経由しない起動ルートがあるため、ここで直接呼ぶ。
    """
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            logger.warning(
                "[RANKING SUMMARY JOB] AI hook=v20 skipped interval=%s reason=empty_df",
                interval,
            )
            return False

        from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe

        logger.warning(
            "[RANKING SUMMARY JOB] AI hook=v20 requested interval=%s rows=%s symbols=%s now=%s source=RANKING",
            interval,
            len(df),
            df["symbol"].nunique() if "symbol" in df.columns else 0,
            now,
        )

        ok = run_summary_ai_entry_safe(
            interval=int(interval),
            now=now,
            df=df,
            source="RANKING",
        )

        logger.warning(
            "[RANKING SUMMARY JOB] AI hook=v20 done interval=%s ok=%s source=RANKING",
            interval,
            ok,
        )
        return bool(ok)

    except Exception:
        logger.exception(
            "[RANKING SUMMARY JOB] AI hook=v20 failed interval=%s source=RANKING",
            interval,
        )
        return False


# ============================================================
# 2026-09-30: 3/5 分足の ranking summary を監視銘柄だけで作る
# ------------------------------------------------------------
# main.py の CPU の 68% が ranking summary だった (py-spy 20 秒:
# ranking_summary_jobs._task 43% / _ranking_bg_task 25%)。3 分足は
# ランキング全銘柄 (約 530 銘柄 / 10,447 行) の指標計算に 12-165 秒かかり、
# スロットごとにほぼ切れ目なく走って GIL を取り続け、PUSH の読み取りが
# 時間切れで止まっていた (エントリー 0 件の上流)。
#
# 3/5 分足は監視銘柄 (約 100) だけで作る。監視銘柄外のランキング候補は
# 3/5 分足の値が無くなり MTF ゲートで落ちる側に変わる (ユーザー判断で採用)。
# 1 分足は従来どおり全銘柄。監視銘柄が取れないときは従来どおり全銘柄。
# RANKING_SUMMARY_LONG_TF_ACTIVE_ONLY=0 で従来動作。
# ============================================================
def _long_tf_active_only_enabled() -> bool:
    v = os.getenv("RANKING_SUMMARY_LONG_TF_ACTIVE_ONLY")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _long_tf_symbols(interval: int):
    """3/5 分足の計算対象。None は全銘柄 (従来動作)。"""
    if int(interval) not in (3, 5) or not _long_tf_active_only_enabled():
        return None
    try:
        from trading.ranking.active_symbols.manager import get_active_symbols

        syms = [str(x).strip() for x in (get_active_symbols() or []) if str(x).strip()]
    except Exception:
        logger.warning("[RANKING SUMMARY JOB] active symbols 取得失敗 -> 全銘柄で計算", exc_info=True)
        return None
    if len(syms) < 10:
        logger.warning(
            "[RANKING SUMMARY JOB] active symbols が少ない n=%s -> 全銘柄で計算 interval=%s",
            len(syms), interval,
        )
        return None
    return syms


def _run_interval(
    *,
    interval: int,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 20,
    run_entry: bool = True,
) -> pd.DataFrame:
    interval = int(interval)
    if interval not in (1, 3, 5):
        raise ValueError(f"unsupported interval: {interval}")

    now = _now()

    if not force:
        if not _should_run_by_clock(interval, now):
            logger.info(
                "[RANKING SUMMARY JOB] skip by clock interval=%s now=%s",
                interval,
                now.strftime("%H:%M:%S"),
            )
            return pd.DataFrame()

        if not _guard_duplicate_run(interval):
            return pd.DataFrame()

    lock = _LOCKS[interval]

    if not lock.acquire(blocking=False):
        logger.warning(
            "[RANKING SUMMARY JOB] skip locked interval=%s",
            interval,
        )
        return pd.DataFrame()

    # R18: runner 本体と後段 (cache/persist/display/AI hook/entry) を切り分ける。
    # 後段には名前が付いておらず、1m で 16-22 秒が「どこにも出ない時間」だった。
    _t_leg = time.perf_counter()
    _el_runner_only = 0.0

    try:
        logger.info(
            "[RANKING SUMMARY JOB] start interval=%s force=%s lookback=%s yahoo_fill=%s persist=%s display=%s top_n=%s run_entry=%s",
            interval,
            force,
            lookback_minutes,
            use_yahoo_fill,
            persist,
            display,
            top_n,
            run_entry,
        )

        _set_global_attr("ranking_summary_running", True)
        _set_global_attr(f"ranking_summary_{interval}min_running", True)

        from trading.ranking.summary.runner import run_ranking_summary_once

        _symbols = _long_tf_symbols(interval)
        if _symbols is not None:
            logger.info(
                "[RANKING SUMMARY JOB] long tf active-only interval=%s symbols=%s",
                interval, len(_symbols),
            )

        _t_runner = time.perf_counter()
        df = run_ranking_summary_once(
            trade_date=trade_date,
            interval=interval,
            symbols=_symbols,
            lookback_minutes=lookback_minutes,
            use_yahoo_fill=use_yahoo_fill,
            persist=persist,
            display=display,
            top_n=max(20, int(top_n or 20)),
        )
        _el_runner_only = time.perf_counter() - _t_runner

        if df is not None and not df.empty:
            # R18: post_runner の内訳を割る (cache 格納 と AI hook)
            _t_cache = time.perf_counter()
            _store_ranking_summary_cache(interval, df)
            _el_cache = time.perf_counter() - _t_cache

            logger.info(
                "[RANKING SUMMARY JOB] done interval=%s rows=%s symbols=%s rsi_non_null=%s macd_non_null=%s",
                interval,
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else 0,
                int(df["rsi"].notna().sum()) if "rsi" in df.columns else 0,
                int(df["macd"].notna().sum()) if "macd" in df.columns else 0,
            )

            if bool(run_entry):
                _t_hook = time.perf_counter()
                _run_ranking_ai_hook_v20(interval=interval, now=now, df=df)
                _el_hook = time.perf_counter() - _t_hook
                _log_ph = logger.warning if (_el_cache + _el_hook) >= 3.0 else logger.info
                _log_ph(
                    "[RANKING SUMMARY STAGE] interval=%s post_cache=%.3fs "
                    "post_ai_hook=%.3fs rows=%s",
                    interval, _el_cache, _el_hook, len(df),
                )
            else:
                logger.info(
                    "[RANKING SUMMARY JOB] AI hook=v20 skipped interval=%s reason=run_entry_false",
                    interval,
                )

        else:
            logger.warning(
                "[RANKING SUMMARY JOB] done empty interval=%s",
                interval,
            )

        return df

    except Exception:
        logger.exception(
            "[RANKING SUMMARY JOB] failed interval=%s",
            interval,
        )
        return pd.DataFrame()

    finally:
        try:
            _el_leg = time.perf_counter() - _t_leg
            _el_post = max(0.0, _el_leg - _el_runner_only)
            _log = logger.warning if _el_leg >= 5.0 else logger.info
            _log(
                "[RANKING SUMMARY STAGE] interval=%s leg_total=%.3fs "
                "runner=%.3fs post_runner=%.3fs "
                "(post_runner = cache/persist/display/AI hook/entry)",
                interval, _el_leg, _el_runner_only, _el_post,
            )
        except Exception:
            logger.debug("[RANKING SUMMARY STAGE] 内訳ログに失敗", exc_info=True)

        _set_global_attr(f"ranking_summary_{interval}min_running", False)
        _set_global_attr("ranking_summary_running", False)
        try:
            lock.release()
        except Exception:
            pass


# 2026-09-30: 1 分足の ranking summary (全ランキング銘柄) を止める (ユーザー判断)。
#   毎分 30-70 秒かかり、その日の最初の足から毎回作り直すので朝は時間とともに
#   重くなる (09:03 29 秒 -> 09:11 51 秒)。main.py の CPU の約 25%。
#   ranking entry (entry_from_ranking) は ranking_snapshot から自前で指標を付け、
#   監視銘柄の ranking_score は ranking 順位から付くのでこれを使っていない。
#   止まるのは 1 分足 ranking summary の AI hook (source=RANKING, interval=1)。
#   RANKING_SUMMARY_1M_ENABLED=1 で従来どおり作る。
def _ranking_summary_1m_enabled() -> bool:
    v = os.getenv("RANKING_SUMMARY_1M_ENABLED")
    if v is None or str(v).strip() == "":
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def job_ranking_summary_1m(
    *,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 20,
    run_entry: bool = True,
) -> pd.DataFrame:
    if not _ranking_summary_1m_enabled():
        logger.info("[RANKING SUMMARY JOB] 1m skipped (RANKING_SUMMARY_1M_ENABLED=0)")
        return pd.DataFrame()
    return _run_interval(
        interval=1,
        force=force,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        use_yahoo_fill=use_yahoo_fill,
        persist=persist,
        display=display,
        top_n=top_n,
        run_entry=run_entry,
    )


def job_ranking_summary_3m(
    *,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 20,
    run_entry: bool = True,
) -> pd.DataFrame:
    return _run_interval(
        interval=3,
        force=force,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        use_yahoo_fill=use_yahoo_fill,
        persist=persist,
        display=display,
        top_n=top_n,
        run_entry=run_entry,
    )


def job_ranking_summary_5m(
    *,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 20,
    run_entry: bool = True,
) -> pd.DataFrame:
    return _run_interval(
        interval=5,
        force=force,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        use_yahoo_fill=use_yahoo_fill,
        persist=persist,
        display=display,
        top_n=top_n,
        run_entry=run_entry,
    )


# P0-5: 長い足を「最後に作った完成足」で管理する。
_LONG_TF_LAST_SLOT: dict = {}
_LONG_TF_SLOT_LOCK = threading.Lock()


def _long_tf_slot_gate_enabled() -> bool:
    v = os.getenv("RANKING_SUMMARY_LONG_TF_SLOT_GATE")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _latest_completed_slot(now: dt.datetime, interval: int) -> dt.datetime:
    """直近の「完成した」足の開始時刻。

    now=15:32:10, interval=5 -> 15:25 (15:30 の足はまだ確定していない)
    """
    iv = max(1, int(interval))
    floored = now.replace(second=0, microsecond=0)
    floored = floored - dt.timedelta(minutes=floored.minute % iv)
    return floored - dt.timedelta(minutes=iv)


def _long_tf_due(now: dt.datetime, interval: int, *, force: bool) -> tuple[bool, str]:
    """この interval を今作るべきか。

    戻り値: (作るか, 理由)
    """
    iv = int(interval)
    if force:
        return True, "force"
    if not _long_tf_slot_gate_enabled():
        due = (now.minute % iv == 0)
        return due, "legacy_minute_mod:%s%%%s=%s" % (now.minute, iv, now.minute % iv)

    slot = _latest_completed_slot(now, iv)
    with _LONG_TF_SLOT_LOCK:
        last = _LONG_TF_LAST_SLOT.get(iv)
    if last is not None and slot <= last:
        return False, "slot_unchanged:last=%s slot=%s" % (last, slot)
    return True, "new_slot:last=%s slot=%s" % (last, slot)


def _long_tf_mark_built(now: dt.datetime, interval: int) -> None:
    iv = int(interval)
    slot = _latest_completed_slot(now, iv)
    with _LONG_TF_SLOT_LOCK:
        _LONG_TF_LAST_SLOT[iv] = slot


# ============================================================
# 2026-09-02 P0: 1 分足の Ranking Summary を 3/5 分足から切り離す
# ------------------------------------------------------------
# 本番実測 (2026-09-02 15:29):
#   [RANKING SUMMARY ALL STAGE] total=74.579s
#       iv1=43.119s iv3=31.460s iv5=0.001s
#   -> iv5 は "cooperative stop before base_df (予算切れのため着手しない)"
# 1 分ジョブが 3 分・5 分の直列フル計算を待っており、60 秒 budget を
# 使い切っていた。timeout を伸ばすのではなく、待たせない構造にする。
#
#   - interval=1 は従来どおり同期でリアルタイム計算する
#   - interval=3 / 5 は専用の 1 本ワーカーへ投げて即座に戻る
#     (結果は _store_ranking_summary_cache 経由で publish される)
#   - 3/5 が走っている間に次の 3/5 が来たら投げない (重ならない)
#
# エントリー条件・閾値・fail-closed には触れていない。
# ============================================================

_LONG_TF_EXECUTOR = None
# RLock: _submit_long_tf() がこのロックを持ったまま _get_long_tf_executor() を
# 呼ぶため、非再入ロックだと自分自身で固まる (2026-09-02 のテストで検出)。
_LONG_TF_EXEC_LOCK = threading.RLock()
_LONG_TF_INFLIGHT: dict = {}


def _long_tf_async_enabled() -> bool:
    v = os.getenv("RANKING_SUMMARY_LONG_TF_ASYNC")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_long_tf_executor():
    global _LONG_TF_EXECUTOR
    with _LONG_TF_EXEC_LOCK:
        if _LONG_TF_EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor

            _LONG_TF_EXECUTOR = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ranking-longtf"
            )
        return _LONG_TF_EXECUTOR


def ranking_long_tf_inflight() -> dict:
    """診断用。3/5 分足ワーカーの在席状況。"""
    out = {}
    with _LONG_TF_EXEC_LOCK:
        for iv, fut in list(_LONG_TF_INFLIGHT.items()):
            try:
                out[iv] = "running" if not fut.done() else "done"
            except Exception:
                out[iv] = "unknown"
    return out


def _submit_long_tf(iv: int, fn, kwargs: dict, now) -> str:
    """3/5 分足を非同期で走らせる。既に走っていれば投げない。"""
    with _LONG_TF_EXEC_LOCK:
        fut = _LONG_TF_INFLIGHT.get(iv)
        if fut is not None and not fut.done():
            logger.warning(
                "[RANKING SUMMARY JOB][LONG TF BUSY] interval=%s 前回の実行がまだ終わっていない "
                "-> 今回は投げない (次回実行と重ねない)",
                iv,
            )
            return "busy"

        def _task():
            t0 = time.perf_counter()
            try:
                df = fn(**kwargs)
                rows = len(df) if isinstance(df, pd.DataFrame) else -1
                logger.warning(
                    "[RANKING SUMMARY JOB][LONG TF ASYNC DONE] interval=%s rows=%s elapsed=%.3fs "
                    "(1 分足はこれを待っていない)",
                    iv, rows, time.perf_counter() - t0,
                )
                if rows > 0:
                    _long_tf_mark_built(now, iv)
                return df
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY JOB][LONG TF ASYNC FAILED] interval=%s elapsed=%.3fs",
                    iv, time.perf_counter() - t0,
                )
                return pd.DataFrame()

        _LONG_TF_INFLIGHT[iv] = _get_long_tf_executor().submit(_task)
        logger.warning(
            "[RANKING SUMMARY JOB][LONG TF ASYNC SUBMIT] interval=%s now=%s "
            "(1 分足は 3/5 分足の完了を待たない)",
            iv, now.strftime("%H:%M:%S"),
        )
        return "submitted"


def _job_ranking_summary_all_core(
    *,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 20,
    run_entry: bool = True,
) -> dict[int, pd.DataFrame]:
    now = _now()
    results: dict[int, pd.DataFrame] = {}

    # P3: BG の stage は ranking_job_all の 1 段しか無く、
    # stage_elapsed=70.1s / deadline_remaining=-10.3s が出ても
    # その 70 秒が 1m / 3m / 5m のどれなのかが分からなかった。
    # interval=3/5 には既に "long tf built ... elapsed" があるが
    # interval=1 だけ足元の elapsed がどこにも出ていない。
    # 1 実行につき 1 行だけ内訳を出す (段ごとの詳細は既存の
    # [RANKING SUMMARY STAGE] / [RANKING DB READ STAGE] 側にある)。
    _t_all = time.perf_counter()
    _leg_sec: dict[int, float] = {}

    _t_iv = time.perf_counter()
    results[1] = job_ranking_summary_1m(
        force=force,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        use_yahoo_fill=use_yahoo_fill,
        persist=persist,
        display=display,
        top_n=top_n,
        run_entry=run_entry,
    )
    _leg_sec[1] = time.perf_counter() - _t_iv

    for iv, fn in ((3, job_ranking_summary_3m), (5, job_ranking_summary_5m)):
        due, why = _long_tf_due(now, iv, force=force)
        if not due:
            logger.info(
                "[RANKING SUMMARY JOB] long tf skipped interval=%s reason=%s now=%s",
                iv, why, now.strftime("%H:%M:%S"),
            )
            results[iv] = pd.DataFrame()
            _leg_sec[iv] = 0.0
            continue
        logger.warning(
            "[RANKING SUMMARY JOB] long tf due interval=%s reason=%s now=%s",
            iv, why, now.strftime("%H:%M:%S"),
        )
        _kwargs = dict(
            force=force,
            trade_date=trade_date,
            lookback_minutes=lookback_minutes,
            use_yahoo_fill=use_yahoo_fill,
            persist=persist,
            display=display,
            top_n=top_n,
            run_entry=run_entry,
        )
        _t0 = time.perf_counter()
        if _long_tf_async_enabled():
            # 1 分足のジョブを 3/5 分足で待たせない。
            # 結果は _store_ranking_summary_cache 経由で publish される。
            _status = _submit_long_tf(iv, fn, _kwargs, now)
            results[iv] = pd.DataFrame()
            _leg_sec[iv] = time.perf_counter() - _t0
            logger.warning(
                "[RANKING SUMMARY JOB] long tf dispatched interval=%s status=%s dispatch=%.3fs "
                "(同期の直列計算はしない)",
                iv, _status, _leg_sec[iv],
            )
            continue

        results[iv] = fn(**_kwargs)
        _leg_sec[iv] = time.perf_counter() - _t0
        _rows = len(results[iv]) if isinstance(results[iv], pd.DataFrame) else -1
        logger.warning(
            "[RANKING SUMMARY JOB] long tf built interval=%s rows=%s elapsed=%.3fs",
            iv, _rows, _leg_sec[iv],
        )
        if _rows > 0:
            _long_tf_mark_built(now, iv)

    try:
        _total = time.perf_counter() - _t_all
        _sum = sum(_leg_sec.values())
        _log = logger.warning if _total >= 10.0 else logger.info
        _log(
            "[RANKING SUMMARY ALL STAGE] total=%.3fs iv1=%.3fs iv3=%.3fs iv5=%.3fs "
            "leg_sum=%.3fs unaccounted=%.3fs "
            "(この total が BG の ranking_job_all stage_elapsed に対応する)",
            _total, _leg_sec.get(1, 0.0), _leg_sec.get(3, 0.0), _leg_sec.get(5, 0.0),
            _sum, max(0.0, _total - _sum),
        )
    except Exception:
        logger.debug("[RANKING SUMMARY ALL STAGE] 内訳ログに失敗", exc_info=True)

    return results


# ============================================================
# RANKING SUMMARY FAST ENTRY ROUTE
# ------------------------------------------------------------
# 旧 core/startup/ranking_summary_fast_entry_patch.py
# (V1-RANKING-SUMMARY-FAST-ENTRY-ROUTE) を本文化。
#
# main.py での ranking summary 構築が数分かかると BG timeout/cooldown により
# RANKING AI hook が走らなくなるため、スケジュール実行 (run_entry=True) の
# 場合だけ lookback を短く打ち切って経路を生かす。
# BG timeout/cooldown/stale 側の既定値は core/startup/scheduler/bootstrap.py
# のモジュール冒頭で設定している。
# ============================================================

_RANKING_SUMMARY_FAST_ENTRY_VERSION = "V1-RANKING-SUMMARY-FAST-ENTRY-ROUTE"

os.environ.setdefault("RANKING_SUMMARY_ENTRY_LOOKBACK_MIN", "20")

# 1分足 indicator が成立するのに必要な最低本数。
# trading/summary/indicators/indicator_calculator.py の実装に合わせている:
#   ema26 / macd     : min_periods=26
#   signal           : macd から span=9 -> 実質 35
#   ma25             : 25
#   ma75             : rolling(75, min_periods=75)
#   ma75_slope       : ma75 から 3 本 -> 実質 78
#   slope_atr_scaled : ma75_slope 由来 -> 実質 78
os.environ.setdefault("RANKING_SUMMARY_MIN_INDICATOR_LOOKBACK_MIN", "78")

# lookback は「分」で切るが、値の来ない分があるため
# 78 分 = 78 本にならない (実測: 78分で 67本)。
# 欠損分のマージンを乗せて「本数」を満たす。
os.environ.setdefault("RANKING_SUMMARY_INDICATOR_LOOKBACK_MARGIN_PCT", "35")


def _ranking_summary_fast_entry_env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return max(1, int(float(raw)))
    except Exception:
        return int(default)


def _filter_kwargs_for_core(fn, kwargs: dict) -> dict:
    """core 関数が実際に受け取れる keyword だけを残す。

    この wrapper は (*args, **kwargs) なので、呼び出し側の
    core/startup/scheduler/bootstrap._call_with_supported_kwargs は
    VAR_KEYWORD を見て "全部渡せる" と判断してしまう。
    そのため topn / use_discord / announce_bridge / discord_sender /
    return_details / sides などが core へそのまま到達して
    TypeError: unexpected keyword argument になっていた。
    フィルタはここ (実態を知っている側) で行う。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    accepted = {k: v for k, v in kwargs.items() if k in params}
    dropped = sorted(set(kwargs) - set(accepted))
    if dropped:
        logger.info(
            "[RANKING SUMMARY JOBS] dropped unsupported kwargs for %s: %s",
            getattr(fn, "__name__", str(fn)),
            dropped,
        )
    return accepted


def job_ranking_summary_all(*args, **kwargs):
    try:
        if kwargs.get("run_entry", True):
            cap = _ranking_summary_fast_entry_env_int("RANKING_SUMMARY_ENTRY_LOOKBACK_MIN", 20)
            # cap をさらに下回ると macd(26)/ma25/ma75(75)/slope(78) が
            # 構造的に全 NaN になるので、indicator 要件を床にする。
            # (実測: lookback=20 だと hist_max=20 / macd_nonnull=0 / signal_nonnull=0)
            bars_needed = _ranking_summary_fast_entry_env_int("RANKING_SUMMARY_MIN_INDICATOR_LOOKBACK_MIN", 78)
            margin_pct = _ranking_summary_fast_entry_env_int("RANKING_SUMMARY_INDICATOR_LOOKBACK_MARGIN_PCT", 35)
            # 欠損分を見込んで分へ換算 (78本 * 1.35 → 106分)
            floor = int(bars_needed * (100 + max(0, margin_pct)) / 100.0)
            if cap < floor:
                logger.warning(
                    "[RANKING SUMMARY FAST ENTRY] lookback cap raised to indicator minimum cap=%s floor=%s "
                    "(bars_needed=%s margin=%s%% ; macd=26 ma25=25 ma75=75 ma75_slope=78)",
                    cap,
                    floor,
                    bars_needed,
                    margin_pct,
                )
                cap = floor
            old = kwargs.get("lookback_minutes", 240)
            try:
                old_i = int(old)
            except Exception:
                old_i = 240
            # cap = 上限 (fast entry のための打ち切り)、
            # floor = 下限 (indicator が成立するのに必要な本数)。
            # 両方効かせないと、呼び出し側が既に floor 未満 (例: 60) のとき
            # cap だけでは引き上げられず macd/signal/slope が全 NaN のままになる。
            effective = max(int(floor), min(old_i, int(cap)))
            kwargs["lookback_minutes"] = effective
            logger.warning(
                "[RANKING SUMMARY FAST ENTRY] lookback capped old=%s new=%s indicator_floor=%s version=%s",
                old,
                kwargs.get("lookback_minutes"),
                floor,
                _RANKING_SUMMARY_FAST_ENTRY_VERSION,
            )
    except Exception:
        pass
    # 旧名 top_n/topn 両方が渡ってくることがあるので、core が受ける
    # top_n に寄せてから間引く (値は変えない)。
    if "top_n" not in kwargs and "topn" in kwargs:
        kwargs["top_n"] = kwargs["topn"]
    kwargs = _filter_kwargs_for_core(_job_ranking_summary_all_core, kwargs)
    return _job_ranking_summary_all_core(*args, **kwargs)


def run_ranking_summary_job(**kwargs):
    return job_ranking_summary_all(**kwargs)


def run_ranking_summary_1min_job(**kwargs):
    return job_ranking_summary_1m(**kwargs)


def run_ranking_summary_3min_job(**kwargs):
    return job_ranking_summary_3m(**kwargs)


def run_ranking_summary_5min_job(**kwargs):
    return job_ranking_summary_5m(**kwargs)


def job_summary_ranking(**kwargs):
    return job_ranking_summary_all(**kwargs)


__all__ = [
    "job_ranking_summary_1m",
    "job_ranking_summary_3m",
    "job_ranking_summary_5m",
    "job_ranking_summary_all",
    "run_ranking_summary_job",
    "run_ranking_summary_1min_job",
    "run_ranking_summary_3min_job",
    "run_ranking_summary_5min_job",
    "job_summary_ranking",
]
