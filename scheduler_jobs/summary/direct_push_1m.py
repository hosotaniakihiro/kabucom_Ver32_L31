# -*- coding: utf-8 -*-
# ============================================================
# File   : scheduler_jobs/summary/direct_push_1m.py
# Version: V10-JOB-SUMMARY-WRAP-REMOVED  (VERSION 定数と一致させること。ログに出る)
# ------------------------------------------------------------
# main.py の1分PUSH summary tickを軽量経路に固定する。
#
# 移設 (2026-08-12):
#   - core/startup/summary_main_direct_push_force_patch.py から移設した。
#     V10 で job_summary のラップを撤去した時点でモンキーパッチは無くなり、
#     runner_core.job_summary が tier1 として直接呼ぶライブラリ +
#     env 既定値 5 件の install() だけになっていたので、
#     利用側と同じ scheduler_jobs/summary/ 配下へ置く。
#   - 末尾の auto install は残す。monkeypatch を伴わず env の setdefault のみで
#     冪等なため、import されたプロセスで必ず既定値が入る従来の挙動を保つ。
#     (sitecustomize / fast_startup_runtime_patch からの install も従来どおり)
#
# V10:
#   - job_summary/run_push_summary_job/job_1m への再代入 + 180秒間2秒毎に
#     再適用し続ける watcher スレッドを撤去した。
#
#   発見した不具合: _patch_once() は _ORIGINAL_JOB_SUMMARY をモジュール変数として
#   初回成功時に1度だけキャッシュし、以降の watcher 呼び出しではそのまま
#   古いキャッシュを使い回していた。このパッチは sitecustomize.py の
#   SYNC_MAIN_PATCHES で最も早く (main.py 起動前に) install されるため、
#   light_tick_1m / memory_latest_1m が
#   後から重ね書きしても、次の watcher tick (最大2秒後、確実に main.py の
#   起動シーケンス中に発生する) で「まだ自分の目印が付いていない」と判定し、
#   古いキャッシュ原本を土台に自分自身を re-install していた。結果、
#   後続2パッチの job_summary ロジックは毎回確実に奪われ、
#   light_tick_1m の「本物のrunnerパイプラインを使う、
#   より正確な高速経路」は事実上一度も選ばれなかった。
#
#   本文化にあたり、_build_direct / _store / _submit_ai 等はライブラリ関数として
#   維持し、scheduler_jobs/summary/runner_core.py の job_summary から
#   tier1 (memory直接+このファイルのhardening) として直接呼ぶ形にした。
#   tier2 として light_tick_1m.build_light_1m_summary
#   (本物のrunnerパイプライン) を明示的に重ね、tier1が空/失敗の場合だけ
#   フォールバックするようにして、3本の奪い合いを解消した。
#
# V9:
#   - V8 fallback が global_data.summary_1m_df をPUSHメモリとして拾い、
#     11:02に10:59の古いsummaryを再利用してAIを回していた。
#   - fallback対象を raw/current PUSH系フレームだけに限定する。
#   - robust/process memory の出力も latest_dt freshness で検証し、古い1分足は
#     AIへ渡さない。
#   - timeout/empty/stale時は raw count / original fallback を呼ばず即returnする。
#   - ガード/発注条件は緩和しない。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V10-JOB-SUMMARY-WRAP-REMOVED"
_PATCHED = False
_AI_LOCK = threading.RLock()
_AI_RUNNING: dict[str, float] = {}

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _dt_key(now: Any) -> str:
    return now.strftime("%Y%m%d%H%M%S") if isinstance(now, dt.datetime) else str(now)


def _latest_dt(df: Any) -> pd.Timestamp | None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "received_at", "event_time", "time", "timestamp", "created_at", "updated_at", "inserted_at"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                mx = s.max()
                if pd.notna(mx):
                    ts = pd.Timestamp(mx)
                    return ts.tz_localize(None) if getattr(ts, "tzinfo", None) else ts
    except Exception:
        pass
    return None


def _is_fresh_df(df: Any, now: dt.datetime, *, label: str) -> bool:
    latest = _latest_dt(df)
    if latest is None:
        return False
    max_age = max(30.0, _env_float("SUMMARY_FORCE_DIRECT_MAX_OUTPUT_AGE_SEC", 90.0))
    age = (pd.Timestamp(now) - latest).total_seconds()
    if age > max_age:
        try:
            logger.warning(
                "[SUMMARY FORCE DIRECT 1M] reject stale frame label=%s latest_dt=%s age=%.1fs max_age=%.1fs rows=%s version=%s",
                label, latest, age, max_age, len(df) if isinstance(df, pd.DataFrame) else None, VERSION,
            )
        except Exception:
            pass
        return False
    return True


def _candidate_push_frames() -> list[tuple[str, pd.DataFrame]]:
    frames: list[tuple[str, pd.DataFrame]] = []
    # V9: summary_1m_df / push_summary_1m_df は完成済みsummaryなので除外する。
    # fallbackでは、現在受信中のraw PUSH/streamフレームだけを見る。
    attr_names = (
        "push_df", "push_memory_df", "push_stream_df", "stream_data_df", "stream_data_raw",
        "raw_push_df", "push_history_df", "push_data_df", "latest_push_df", "realtime_push_df",
    )
    targets: list[tuple[str, Any]] = []
    try:
        from global_state import global_data
        targets.append(("global_data", global_data))
    except Exception:
        pass
    try:
        from core.global_context.context import global_context
        targets.append(("global_context", global_context))
    except Exception:
        pass

    seen: set[int] = set()
    for prefix, obj in targets:
        if obj is None:
            continue
        for name in attr_names:
            try:
                v = getattr(obj, name, None)
            except Exception:
                continue
            if isinstance(v, pd.DataFrame) and not v.empty and id(v) not in seen:
                seen.add(id(v))
                frames.append((f"{prefix}.{name}", v))
        # summary getters are intentionally excluded to avoid stale self-feeding.
        for meth in ("get_push_df", "get_push_memory_df", "get_stream_data_df"):
            fn = getattr(obj, meth, None)
            if not callable(fn):
                continue
            for call in (lambda fn=fn: fn(), lambda fn=fn: fn(1)):
                try:
                    v = call()
                    if isinstance(v, pd.DataFrame) and not v.empty and id(v) not in seen:
                        seen.add(id(v))
                        frames.append((f"{prefix}.{meth}", v))
                        break
                except TypeError:
                    continue
                except Exception:
                    break

    # WebSocket の生バッファ (メモリのみ。NAS は読まない)。
    # main.py は memory_only 運用で global_data.push_df を更新する flush が
    # 動かないため、ここだけが新鮮なことがある
    # (実測: global_data.push_df=13:08:20 のまま / live バッファは 6280 行と増加中)。
    # memory_latest_1m._load_push_memory_df_core() と同じソース。
    try:
        from trading.push.push_stream import get_push_dataframe

        v = get_push_dataframe()
        if isinstance(v, pd.DataFrame) and not v.empty and id(v) not in seen:
            seen.add(id(v))
            frames.append(("push_stream.get_push_dataframe", v))
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] push_stream live buffer unavailable", exc_info=True)

    # ------------------------------------------------------------
    # P0-10 FAST LANE: メモリ候補が全部 stale なら PUSH DB を読む。
    #
    # 2026-08-27 15:24 の実測:
    #   global_data.push_df              latest=14:08:10 rows=644
    #   push_stream.get_push_dataframe   latest=14:08:10 rows=2488
    #   push{YYYYMMDD}.db stream_data    latest=15:24:04  (最終的に 15:30:52)
    # main.py 自身の WS バッファは last_recv が新しいのに datetime が
    # 14:08:10 で止まっていた (WS が張り直しのたびに初期スナップショット
    # だけ受けて live 更新を掴めていない。connection_generation 335->392)。
    # 一方 push_receiver が書く PUSH DB は datetime == received_at で常に新鮮。
    #
    # そのため「メモリが古いときは DB を正とする」。
    # heavy な historical worker は待たない。読むのは tail だけ。
    # ------------------------------------------------------------
    if _env_bool("SUMMARY_FORCE_DIRECT_PUSH_DB_FASTLANE", True):
        st = _FastLaneStages("push_db_fastlane")
        try:
            max_age = max(30.0, _env_float("SUMMARY_FORCE_DIRECT_MAX_OUTPUT_AGE_SEC", 90.0))
            now_ts = pd.Timestamp(dt.datetime.now())
            fresh_enough = False
            for _name, _df in frames:
                latest = _latest_dt(_df)
                if latest is None:
                    continue
                if float((now_ts - pd.Timestamp(latest)).total_seconds()) <= max_age:
                    fresh_enough = True
                    break
            st.mark("freshness_scan", n=len(frames), fresh=fresh_enough)

            if not fresh_enough:
                from scheduler_jobs.summary.memory_latest_1m import _read_push_db_recent

                db_df = _read_push_db_recent()
                st.mark("push_db_read",
                        rows=0 if not isinstance(db_df, pd.DataFrame) else len(db_df))
                if isinstance(db_df, pd.DataFrame) and not db_df.empty:
                    db_latest = _latest_dt(db_df)
                    st.mark("latest_dt")
                    logger.warning(
                        "[SUMMARY FORCE DIRECT 1M] FAST LANE: メモリ候補が全部 stale なので "
                        "PUSH DB を採用 db_rows=%s db_latest=%s memory=[%s] version=%s",
                        len(db_df), db_latest,
                        ", ".join("%s(latest=%s,rows=%s)" % (n, _latest_dt(d), len(d))
                                  for n, d in frames[:4]),
                        VERSION,
                    )
                    st.mark("log_memory_summary", n=min(4, len(frames)))
                    # DB を先頭へ入れる。以降の best 選択で確実に候補に入る。
                    frames.insert(0, ("push_db.stream_data.fastlane", db_df))
                    # 直読みしている経路のために global_data.push_df も更新する。
                    # 単調ガード (global_state.push_df setter) があるので
                    # 古いフレームで潰されることはない。
                    try:
                        if not _worker_generation_is_current():
                            logger.warning(
                                "[SUMMARY FORCE DIRECT 1M][STALE WRITEBACK BLOCKED] "
                                "古い世代の worker からの global_data.push_df 書き戻しを止めた "
                                "rows=%s db_latest=%s",
                                len(db_df), db_latest,
                            )
                        else:
                            from global_state import global_data

                            global_data.push_df = db_df
                    except Exception:
                        logger.debug("[SUMMARY FORCE DIRECT 1M] push_df writeback failed",
                                     exc_info=True)
                    st.mark("push_df_writeback")
            st.emit(fresh_enough=fresh_enough, frames=len(frames))
        except Exception:
            st.emit(result="exception")
            logger.exception("[SUMMARY FORCE DIRECT 1M] FAST LANE push DB read failed")

    return frames


def _normalize_push_ticks(df: pd.DataFrame, now: dt.datetime, *, source_name: str) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        x = df.copy(deep=False)
        x = x.loc[:, ~pd.Index(x.columns).duplicated()].copy()
        sym_col = next((c for c in ("symbol", "Symbol", "code", "銘柄コード", "stock_code") if c in x.columns), None)
        price_col = next((c for c in ("price", "current_price", "CurrentPrice", "現在値", "close", "close_price") if c in x.columns), None)
        dt_col = next((c for c in ("datetime", "received_at", "event_time", "time", "timestamp", "created_at", "updated_at", "inserted_at") if c in x.columns), None)
        vol_col = next((c for c in ("volume", "trading_volume", "vol", "Volume", "出来高") if c in x.columns), None)
        if sym_col is None or price_col is None or dt_col is None:
            return pd.DataFrame()
        out = pd.DataFrame()
        out["symbol"] = x[sym_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        out["price"] = pd.to_numeric(x[price_col], errors="coerce")
        out["datetime"] = pd.to_datetime(x[dt_col], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        if vol_col is not None:
            out["volume"] = pd.to_numeric(x[vol_col], errors="coerce").fillna(0.0)
        else:
            out["volume"] = 0.0
        out = out.dropna(subset=["symbol", "price", "datetime"])
        out = out[(out["symbol"] != "") & (out["price"] > 0)]
        if out.empty:
            return pd.DataFrame()
        latest = out["datetime"].max()
        max_age = max(30, _env_int("SUMMARY_FORCE_DIRECT_MEMORY_LOOKBACK_SEC", 90))
        age = (pd.Timestamp(now) - pd.Timestamp(latest)).total_seconds()
        if age > max_age:
            logger.warning(
                "[SUMMARY FORCE DIRECT 1M] reject stale push memory source=%s latest=%s age=%.1fs max_age=%ss rows=%s version=%s",
                source_name, latest, age, max_age, len(out), VERSION,
            )
            return pd.DataFrame()
        min_dt = pd.Timestamp(now) - pd.Timedelta(seconds=max_age)
        out = out[out["datetime"] >= min_dt]
        if out.empty:
            return pd.DataFrame()
        out["minute"] = out["datetime"].dt.floor("min")
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] normalize push ticks failed source=%s", source_name, exc_info=True)
        return pd.DataFrame()


# ============================================================
# P0-2: FAST LANE の段別計測
# ------------------------------------------------------------
# 2026-08-28 08:51 は入力 983 行に対して
#   stream_data.fastlane  elapsed=41.202s
#   SUMMARY FORCE DIRECT 1M elapsed=49.588s
# だった。行数から見てアルゴリズムではないので、どの段で
# 時間を使っているかを必ず名指しできるようにする。
# 1 段 1 秒以上で WARNING。
# ============================================================

_FASTLANE_STAGE_WARN_SEC = 1.0


class _FastLaneStages:
    def __init__(self, label: str) -> None:
        self.label = label
        self._t0 = time.perf_counter()
        self._last = self._t0
        self.marks: list = []
        self.slow: list = []

    def mark(self, name: str, **info) -> None:
        now = time.perf_counter()
        el = now - self._last
        self._last = now
        self.marks.append((name, el, info))
        if el >= _FASTLANE_STAGE_WARN_SEC:
            self.slow.append(name)
            logger.warning(
                "[FASTLANE STAGE] slow label=%s name=%s elapsed=%.3fs %s "
                "(1 段で 1 秒以上かかっている)",
                self.label, name, el,
                " ".join("%s=%s" % kv for kv in info.items()),
            )

    @property
    def total(self) -> float:
        return time.perf_counter() - self._t0

    def emit(self, **info) -> None:
        parts = " ".join(
            "%s=%.3fs%s" % (
                n, e,
                ("(" + ",".join("%s=%s" % kv for kv in i.items()) + ")") if i else "",
            )
            for n, e, i in self.marks
        )
        log = logger.warning if (self.total >= _FASTLANE_STAGE_WARN_SEC or self.slow) else logger.info
        log(
            "[FASTLANE STAGE] label=%s total=%.3fs stages=[%s] %s",
            self.label, self.total, parts,
            " ".join("%s=%s" % kv for kv in info.items()),
        )


def _build_from_process_memory(now: dt.datetime) -> pd.DataFrame:
    t0 = time.perf_counter()
    best_name = ""
    best_ticks = pd.DataFrame()
    # P0-2: 段ごとに計測する。1 段 1 秒以上なら WARNING。
    # 983 行の入力で 41 秒かかっていたのがどこかを名指しできるようにする。
    st = _FastLaneStages("build_from_process_memory")
    try:
        cands = _candidate_push_frames()
        st.mark("candidate_frames", n=len(cands),
                rows=sum(len(d) for _, d in cands))
        for name, df in cands:
            n_before = len(df)
            ticks = _normalize_push_ticks(df, now, source_name=name)
            st.mark("normalize_ticks", src=name, rows_before=n_before, rows_after=len(ticks))
            if len(ticks) > len(best_ticks):
                best_name = name
                best_ticks = ticks
        if best_ticks.empty:
            st.emit(result="no_ticks")
            return pd.DataFrame()
        rows = []
        for (sym, minute), g in best_ticks.sort_values("datetime").groupby(["symbol", "minute"], sort=False):
            prices = pd.to_numeric(g["price"], errors="coerce").dropna()
            if prices.empty:
                continue
            vol = float(pd.to_numeric(g.get("volume", 0.0), errors="coerce").fillna(0.0).sum()) if "volume" in g.columns else 0.0
            close = float(prices.iloc[-1])
            rows.append({
                "symbol": str(sym),
                "datetime": pd.Timestamp(minute).to_pydatetime().replace(tzinfo=None),
                "interval": 1,
                "open": float(prices.iloc[0]),
                "high": float(prices.max()),
                "low": float(prices.min()),
                "close": close,
                "price": close,
                "current_price": close,
                "close_price": close,
                "volume": vol,
                "turnover": vol * close if vol > 0 else 0.0,
                "source": "SUMMARY",
            })
        st.mark("ohlc_aggregate", tick_rows=len(best_ticks), bar_rows=len(rows))
        df = pd.DataFrame(rows)
        if df.empty:
            st.emit(result="empty_bars")
            return pd.DataFrame()
        latest = df.sort_values(["symbol", "datetime"]).groupby("symbol", as_index=False).tail(1).reset_index(drop=True)
        st.mark("latest_projection", rows_before=len(df), rows_after=len(latest),
                symbols=int(latest["symbol"].nunique()) if "symbol" in latest.columns else -1)
        if not _is_fresh_df(latest, now, label=f"process_memory:{best_name}"):
            st.emit(result="stale_output")
            return pd.DataFrame()
        st.emit(result="ok", source=best_name, rows=len(latest))
        logger.warning(
            "[SUMMARY FORCE DIRECT 1M] built via fresh raw push memory fallback source=%s tick_rows=%s summary_rows=%s latest_dt=%s elapsed=%.3fs version=%s",
            best_name, len(best_ticks), len(latest), latest["datetime"].max() if "datetime" in latest.columns else None,
            time.perf_counter() - t0, VERSION,
        )
        return latest
    except Exception:
        st.emit(result="exception")
        logger.exception("[SUMMARY FORCE DIRECT 1M] process memory fallback failed")
        return pd.DataFrame()


def _build_direct_inner(now: dt.datetime) -> pd.DataFrame:
    try:
        from .memory_latest_1m import _build_memory_1m_summary
        df = _build_memory_1m_summary(now=now)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] robust memory builder failed", exc_info=True)
    return pd.DataFrame()


# 自分が最後に publish した latest_dt。fast path でのエコー採用を防ぐ。
_LAST_PUBLISHED_LATEST: Any = None


def _fresh_summary_candidates() -> list[tuple[str, pd.DataFrame]]:
    """完成済み 1m summary の候補 (メモリ/GC のみ、NAS なし)。"""
    out: list[tuple[str, pd.DataFrame]] = []
    try:
        from trading.entry.tonosama.fresh_summary_wait import _context_candidate_dfs

        for name, df in _context_candidate_dfs():
            if isinstance(df, pd.DataFrame) and not df.empty:
                out.append(("ctx." + str(name), df))
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] fresh summary enumerator unavailable", exc_info=True)

    # 上記が使えない環境向けの最小限の直接参照。
    for mod_name, obj_names in (("global_state", ("global_data",)),):
        try:
            mod = __import__(mod_name, fromlist=list(obj_names))
            for on in obj_names:
                obj = getattr(mod, on, None)
                if obj is None:
                    continue
                for attr in ("latest_summary_1m_df", "summary_1m_latest_df", "summary_1m_df",
                             "push_summary_1m_df", "push_merged_summary_1", "merged_summary_1"):
                    try:
                        v = getattr(obj, attr, None)
                    except Exception:
                        continue
                    if isinstance(v, pd.DataFrame) and not v.empty:
                        out.append(("%s.%s.%s" % (mod_name, on, attr), v))
        except Exception:
            continue
    return out


def _fresh_summary_fast_path(now: dt.datetime) -> pd.DataFrame:
    """再構築する前に、既に fresh な完成済み summary があればそれを使う。

    _LAST_PUBLISHED_LATEST は読むだけ (代入は _store 側)。
    """

    if not _env_bool("SUMMARY_FORCE_DIRECT_FRESH_SUMMARY_FASTPATH", True):
        return pd.DataFrame()
    try:
        max_age = max(30.0, _env_float("SUMMARY_FORCE_DIRECT_FRESH_SUMMARY_MAX_AGE_SEC", 90.0))
        min_rows = max(1, _env_int("SUMMARY_FORCE_DIRECT_FRESH_SUMMARY_MIN_ROWS", 1))
        now_ts = pd.Timestamp(now)

        rows_report: list[str] = []
        best = None
        best_name = ""
        best_dt = None

        for name, df in _fresh_summary_candidates():
            latest = _latest_dt(df)
            if latest is None:
                rows_report.append("%s(latest=None)" % name)
                continue
            age = float((now_ts - latest).total_seconds())
            try:
                syms = int(df["symbol"].nunique()) if "symbol" in df.columns else 0
            except Exception:
                syms = 0
            rows_report.append("%s(latest=%s age=%.1fs rows=%s symbols=%s)" % (name, latest, age, len(df), syms))
            if age > max_age or len(df) < min_rows:
                continue
            # 自分が直前に出したものを食い戻さない。
            if _LAST_PUBLISHED_LATEST is not None and latest <= _LAST_PUBLISHED_LATEST:
                continue
            if best_dt is None or latest > best_dt:
                best, best_name, best_dt = df, name, latest

        if best is None:
            if rows_report:
                logger.info(
                    "[SUMMARY FORCE DIRECT 1M] fresh summary fastpath miss max_age=%.0fs candidates=%s version=%s",
                    max_age, " | ".join(rows_report[:12]), VERSION,
                )
            return pd.DataFrame()

        logger.warning(
            "[SUMMARY FORCE DIRECT 1M] fresh summary fastpath hit source=%s latest=%s rows=%s "
            "(skipped %.0fs rebuild) candidates=%s version=%s",
            best_name, best_dt, len(best),
            _env_float("SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC", 6.0),
            " | ".join(rows_report[:12]), VERSION,
        )
        return best.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY FORCE DIRECT 1M] fresh summary fastpath failed")
        return pd.DataFrame()


# ============================================================
# 2026-09-02 P0: force-direct worker の世代管理
# ------------------------------------------------------------
# 旧実装:
#     th = threading.Thread(target=_target, name="summary-force-direct-build")
#     th.start(); th.join(timeout)
#     if th.is_alive(): return pd.DataFrame()      # <-- スレッドを放置
#
# 放置された daemon スレッドは _read_push_db_recent() の
# pd.read_sql_query の中で NAS SQLite を掴んだまま生き続ける。
# 本番実測 (2026-09-02):
#     [PUSH DB STAGE] query_fetch=880.344s
#     threads_top={'summary-force-direct': 10, ...}
# 毎分 2 本ずつ増え、10 本が同時に読み取りトランザクションを開いたままになる。
# その結果 wal_checkpoint(PASSIVE) が 1 フレームも進めず
# (log_frames=41055 checkpointed=0)、WAL が 196MB まで膨らみ、
# 読み書き双方が更に遅くなるという正のフィードバックになっていた。
# しかも古い worker は完走時に global_data.push_df を書き戻していた。
#
# 対策 (timeout 値は伸ばさない):
#   - 種類ごとに worker は最大 1 本。走っている間は新規を作らない。
#   - worker には generation を持たせ、世代が変わった結果は必ず捨てる。
#   - timeout 時は cancel event を立て、SQLite 実行中でも中断させる
#     (memory_latest_1m.push_db_set_cancel -> set_progress_handler)。
#   - 古い世代からの global_data 書き戻しを禁止する。
# ============================================================

_WORKER_CTX = threading.local()


class _SingleFlightWorker:
    """同じ種類の force-direct worker を 1 本に制限する。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._lock = threading.Lock()
        self.thread = None
        self.generation = 0
        self.started_at = 0.0
        self.owner = ""
        self.cancel = None

    # -- 診断用 --------------------------------------------------
    def snapshot(self) -> dict:
        th = self.thread
        alive = bool(th is not None and th.is_alive())
        return {
            "kind": self.kind,
            "generation": self.generation,
            "alive": alive,
            "age": round(time.perf_counter() - self.started_at, 1) if (alive and self.started_at) else 0.0,
            "owner": self.owner,
            "name": getattr(th, "name", None) if th is not None else None,
        }

    def is_busy(self) -> bool:
        th = self.thread
        return bool(th is not None and th.is_alive())

    def run(self, fn, timeout: float, owner: str):
        """fn() を worker で実行する。

        戻り値: (結果 or None, status)
          status: "done" / "timeout" / "busy"
        """
        with self._lock:
            if self.is_busy():
                snap = self.snapshot()
                logger.error(
                    "[SUMMARY FORCE DIRECT 1M][WORKER BUSY] kind=%s 前世代の worker がまだ生きている "
                    "generation=%s age=%.1fs owner=%s thread=%s owner_now=%s "
                    "-> 新しい worker を作らない (同種 worker は最大 1 本)。"
                    "空を返すので Entry は fresh 無しで fail-closed のまま。",
                    self.kind, snap["generation"], snap["age"], snap["owner"],
                    snap["name"], owner, VERSION,
                )
                # 前世代へ中断要求を出しておく (SQLite 実行中でも効く)。
                try:
                    if self.cancel is not None:
                        self.cancel.set()
                except Exception:
                    pass
                return None, "busy"

            self.generation += 1
            gen = self.generation
            self.owner = owner
            self.started_at = time.perf_counter()
            cancel = threading.Event()
            self.cancel = cancel
            box = {"v": None, "err": None}

            def _target() -> None:
                _WORKER_CTX.registry = self
                _WORKER_CTX.generation = gen
                try:
                    from scheduler_jobs.summary.memory_latest_1m import (
                        push_db_set_cancel, push_db_clear_cancel,
                    )
                except Exception:
                    push_db_set_cancel = None
                    push_db_clear_cancel = None
                try:
                    if push_db_set_cancel is not None:
                        push_db_set_cancel(cancel, deadline_sec=timeout)
                    box["v"] = fn()
                except Exception as exc:
                    box["err"] = exc
                    logger.exception(
                        "[SUMMARY FORCE DIRECT 1M] worker failed kind=%s generation=%s",
                        self.kind, gen,
                    )
                finally:
                    if push_db_clear_cancel is not None:
                        try:
                            push_db_clear_cancel()
                        except Exception:
                            pass
                    _WORKER_CTX.registry = None
                    _WORKER_CTX.generation = 0
                    if gen != self.generation:
                        logger.warning(
                            "[SUMMARY FORCE DIRECT 1M][STALE WORKER DONE] kind=%s generation=%s "
                            "current=%s elapsed=%.1fs -> 結果を破棄した "
                            "(世代を跨いだ結果は使わない)",
                            self.kind, gen, self.generation,
                            time.perf_counter() - self.started_at,
                        )
                        box["v"] = None
                    else:
                        logger.info(
                            "[SUMMARY FORCE DIRECT 1M][WORKER DONE] kind=%s generation=%s elapsed=%.1fs",
                            self.kind, gen, time.perf_counter() - self.started_at,
                        )

            th = threading.Thread(
                target=_target,
                name="summary-force-direct-%s-g%d" % (self.kind, gen),
                daemon=True,
            )
            self.thread = th
            logger.info(
                "[SUMMARY FORCE DIRECT 1M][WORKER START] kind=%s generation=%s owner=%s "
                "timeout=%.3fs thread=%s",
                self.kind, gen, owner, timeout, th.name,
            )
            th.start()

        th.join(timeout)
        if th.is_alive():
            # 世代を進めて結果を無効化し、SQLite 実行中でも中断させる。
            with self._lock:
                if self.generation == gen:
                    self.generation += 1
            try:
                cancel.set()
            except Exception:
                pass
            logger.error(
                "[SUMMARY FORCE DIRECT 1M][WORKER TIMEOUT] kind=%s generation=%s owner=%s "
                "timeout=%.3fs -> cancel 要求済み。結果は破棄する。"
                "timeout は伸ばさない。次 tick は worker が終わるまで新規を作らない。",
                self.kind, gen, owner, timeout,
            )
            return None, "timeout"

        if box["err"] is not None:
            return None, "done"
        return box["v"], "done"


_FORCE_DIRECT_WORKERS: dict = {
    "build": _SingleFlightWorker("build"),
    "fallback": _SingleFlightWorker("fallback"),
}


def force_direct_worker_stats() -> list:
    """診断用。E2E 1 行ログが読む。"""
    try:
        return [w.snapshot() for w in _FORCE_DIRECT_WORKERS.values()]
    except Exception:
        return []


def _worker_generation_is_current() -> bool:
    """今のスレッドが「現行世代の worker」か。

    worker 以外 (通常の同期経路) から呼ばれた場合は True。
    古い世代の worker からの global_data 書き戻しを止めるために使う。
    """
    try:
        reg = getattr(_WORKER_CTX, "registry", None)
        if reg is None:
            return True
        return int(getattr(_WORKER_CTX, "generation", 0)) == int(reg.generation)
    except Exception:
        return True


def _build_direct(now: dt.datetime) -> pd.DataFrame:
    t0 = time.perf_counter()

    # 既に fresh な summary があるなら 6 秒の再構築を待たない。
    fast = _fresh_summary_fast_path(now)
    if isinstance(fast, pd.DataFrame) and not fast.empty:
        return fast

    timeout = max(0.0, _env_float("SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC", 6.0))
    df = pd.DataFrame()
    if timeout <= 0:
        df = _build_direct_inner(now)
    else:
        res, status = _FORCE_DIRECT_WORKERS["build"].run(
            lambda: _build_direct_inner(now), timeout, owner="_build_direct"
        )
        if status != "done" or not isinstance(res, pd.DataFrame):
            if status == "timeout":
                logger.error(
                    "[SUMMARY FORCE DIRECT 1M] build timeout timeout=%.3fs elapsed=%.3fs now=%s version=%s "
                    "note=worker_cancelled_and_discarded",
                    timeout, time.perf_counter() - t0, now, VERSION,
                )
            df = pd.DataFrame()
        else:
            df = res
    if isinstance(df, pd.DataFrame) and not df.empty and not _is_fresh_df(df, now, label="robust_builder"):
        df = pd.DataFrame()
    if not isinstance(df, pd.DataFrame) or df.empty:
        # ------------------------------------------------------------
        # P0-7: fallback も必ず時間で打ち切る。
        #
        # 旧実装では上の timeout は _build_direct_inner だけを縛っており、
        # ここから先の _build_from_process_memory (FAST LANE の PUSH DB 読み)
        # は**一切縛られていなかった**。そのため 2026-08-28 08:51 は
        #     build timeout timeout=6.000s elapsed=8.380s
        #     -> stream_data.fastlane elapsed=41.202s
        #     -> SUMMARY FORCE DIRECT 1M elapsed=49.588s
        # となり、6 秒の timeout が実質何も止めていなかった。
        #
        # timeout 値は増やさない。予算を使い切ったら
        #   - fallback を打ち切る
        #   - 前回の fresh projection を保持したまま空を返す
        #   - 次 tick で再試行する
        # とする。空を返しても呼び出し側は既存 merged を潰さない
        # (stale なデータで Entry を fail-open させない)。
        # ------------------------------------------------------------
        total_budget = max(
            timeout,
            _env_float("SUMMARY_FORCE_DIRECT_TOTAL_BUDGET_SEC", max(2.0, timeout * 1.5)),
        )
        spent = time.perf_counter() - t0
        remain = total_budget - spent
        if remain <= 0.2:
            logger.error(
                "[SUMMARY FORCE DIRECT 1M] fallback 打ち切り reason=budget_exhausted "
                "spent=%.3fs total_budget=%.3fs timeout=%.3fs now=%s version=%s "
                "(前回の fresh projection を保持し、次 tick で再試行する)",
                spent, total_budget, timeout, now, VERSION,
            )
            return pd.DataFrame()

        res2, status2 = _FORCE_DIRECT_WORKERS["fallback"].run(
            lambda: _build_from_process_memory(now), remain, owner="_build_direct.fallback"
        )
        if status2 != "done" or not isinstance(res2, pd.DataFrame):
            if status2 == "timeout":
                logger.error(
                    "[SUMMARY FORCE DIRECT 1M] fallback timeout budget=%.3fs remain=%.3fs "
                    "elapsed=%.3fs now=%s version=%s "
                    "(process memory / PUSH DB fallback を打ち切り、worker を cancel した。"
                    "timeout は伸ばさない。次 tick で再試行する)",
                    total_budget, remain, time.perf_counter() - t0, now, VERSION,
                )
            return pd.DataFrame()
        df = res2
    if isinstance(df, pd.DataFrame) and not df.empty:
        logger.warning(
            "[SUMMARY FORCE DIRECT 1M] built via fresh robust/process memory rows=%s symbols=%s latest_dt=%s elapsed=%.3fs timeout=%.3fs version=%s",
            len(df), int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
            df["datetime"].max() if "datetime" in df.columns else None,
            time.perf_counter() - t0, timeout, VERSION,
        )
        return df.reset_index(drop=True)
    return pd.DataFrame()


def _normalize_1m_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        out = df.copy()
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            out = out.dropna(subset=["datetime"])
        if "close" not in out.columns:
            for c in ("close_price", "current_price", "price"):
                if c in out.columns:
                    out["close"] = out[c]
                    break
        if "close" not in out.columns:
            return pd.DataFrame()
        for c in ("open", "high", "low"):
            if c not in out.columns:
                out[c] = out["close"]
        if "volume" not in out.columns:
            for c in ("trading_volume", "vol", "Volume", "出来高"):
                if c in out.columns:
                    out["volume"] = out[c]
                    break
        if "volume" not in out.columns:
            out["volume"] = 0.0
        for c in ("open", "high", "low", "close", "volume"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["symbol", "datetime", "close"])
        out["interval"] = 1
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] normalize 1m df failed", exc_info=True)
        return pd.DataFrame()


def _assign_attrs(obj: Any, hist: pd.DataFrame, latest: pd.DataFrame) -> None:
    for name, value in (
        ("summary_1m_df", hist), ("latest_summary_1m_df", latest), ("summary_1m_latest_df", latest),
        ("push_summary_1", latest), ("push_summary_1min", latest), ("push_summary_1m_df", hist),
        ("push_merged_summary_1", latest), ("push_merged_summary_1min", latest), ("push_merged_summary_1m_df", latest),
        ("merged_summary_1", latest), ("merged_summary_1min", latest),
    ):
        try:
            setattr(obj, name, value.copy(deep=False))
        except Exception:
            pass


def _store(df: pd.DataFrame) -> None:
    hist = _normalize_1m_df(df)
    if hist.empty:
        return
    try:
        latest = hist.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1).reset_index(drop=True)
    except Exception:
        latest = hist.copy()
    t0 = time.perf_counter()
    try:
        from global_state import global_data
        _assign_attrs(global_data, hist, latest)
    except Exception:
        pass
    try:
        from core.global_context.context import global_context as GC
        _assign_attrs(GC, hist, latest)
    except Exception:
        pass
    global _LAST_PUBLISHED_LATEST
    try:
        _LAST_PUBLISHED_LATEST = _latest_dt(hist)
    except Exception:
        pass
    logger.warning(
        "[SUMMARY FORCE DIRECT 1M] fast attrs only tf=1 hist_rows=%s latest_rows=%s latest_dt=%s elapsed=%.3fs version=%s",
        len(hist), len(latest), hist["datetime"].max() if "datetime" in hist.columns else None, time.perf_counter() - t0, VERSION,
    )


def _cleanup_ai_running(now_ts: float) -> None:
    max_age = max(30.0, _env_float("SUMMARY_FORCE_DIRECT_AI_RUNNING_MAX_AGE_SEC", 90.0))
    for k, ts in list(_AI_RUNNING.items()):
        age = now_ts - float(ts or 0.0)
        if age > max_age:
            _AI_RUNNING.pop(k, None)
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI stale running key cleared key=%s age_sec=%.1f version=%s", k, age, VERSION)


def _submit_ai(df: pd.DataFrame, now: dt.datetime, run_entry: bool) -> None:
    if not run_entry or df is None or df.empty or not _env_bool("SUMMARY_FORCE_DIRECT_ASYNC_AI", True):
        return
    key = "force-summary-ai:1:" + _dt_key(now)
    now_ts = time.time()
    with _AI_LOCK:
        _cleanup_ai_running(now_ts)
        if key in _AI_RUNNING:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI skipped already_running key=%s rows=%s", key, len(df))
            return
        active_cap = max(1, _env_int("SUMMARY_FORCE_DIRECT_AI_MAX_ACTIVE", 3))
        if len(_AI_RUNNING) >= active_cap:
            oldest = sorted(_AI_RUNNING.items(), key=lambda kv: kv[1])[:3]
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI skipped active_cap=%s active=%s oldest=%s key=%s", active_cap, len(_AI_RUNNING), oldest, key)
            return
        _AI_RUNNING[key] = now_ts
    df_copy = df.copy(deep=False)

    def _task() -> None:
        started = time.time()
        try:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI start key=%s rows=%s version=%s", key, len(df_copy), VERSION)
            try:
                from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
                run_summary_ai_entry_safe(interval=1, now=now, df=df_copy, source="SUMMARY")
            except Exception:
                logger.exception("[SUMMARY FORCE DIRECT 1M] async AI failed key=%s", key)
        finally:
            with _AI_LOCK:
                _AI_RUNNING.pop(key, None)
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI done key=%s elapsed=%.3fs version=%s", key, time.time() - started, VERSION)

    try:
        threading.Thread(target=_task, name=f"summary-force-direct-ai-{_dt_key(now)}", daemon=True).start()
        logger.warning("[SUMMARY FORCE DIRECT 1M] async AI thread started key=%s rows=%s active=%s version=%s", key, len(df_copy), len(_AI_RUNNING), VERSION)
    except Exception:
        with _AI_LOCK:
            _AI_RUNNING.pop(key, None)
        logger.exception("[SUMMARY FORCE DIRECT 1M] async AI thread start failed key=%s", key)


def tier1_build_and_publish(now: dt.datetime, *, run_entry: bool) -> pd.DataFrame:
    """scheduler_jobs/summary/runner_core.py の job_summary (tier1) から直接呼ばれる。
    memory直接ビルド (_build_direct、内部で memory_latest_1m を使う) を
    タイムアウト付きで試し、成功時のみ _store/_submit_ai を行う。呼び出し元の事前条件
    (_is_main_py() 相当・interval==1) は runner_core.py 側で確認済みの前提。"""
    if not _env_bool("SUMMARY_FORCE_DIRECT_PATCH_ENABLED", True):
        return pd.DataFrame()
    now_i = (now or dt.datetime.now()).replace(microsecond=0)
    t0 = time.perf_counter()
    try:
        df = _build_direct(now_i)
        if df is not None and not df.empty:
            _store(df)
            _submit_ai(df, now_i, run_entry)
            logger.warning("[SUMMARY FORCE DIRECT 1M] tier1 hit rows=%s elapsed=%.3fs version=%s", len(df), time.perf_counter() - t0, VERSION)
            return df
        logger.warning("[SUMMARY FORCE DIRECT 1M] tier1 direct empty/timeout/stale elapsed=%.3fs version=%s", time.perf_counter() - t0, VERSION)
    except Exception:
        logger.exception("[SUMMARY FORCE DIRECT 1M] tier1 build failed now=%s", now_i)
    return pd.DataFrame()


def install() -> bool:
    global _PATCHED
    # ------------------------------------------------------------
    # 二重 install を防ぐ (R17)
    #
    # _PATCHED を立てるだけで **判定していなかった** ため、
    # モジュールが別名で 2 回 import されると install が 2 回走り、
    # 「installed」ログも 2 回出ていた。
    # light_tick_1m / memory_latest_1m と同じ形に揃える。
    # ------------------------------------------------------------
    if _PATCHED:
        logger.info(
            "[SUMMARY FORCE DIRECT 1M] already installed -> skip (version=%s)",
            VERSION,
        )
        return False
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC", "6")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_AI_MAX_ACTIVE", "3")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_MEMORY_LOOKBACK_SEC", "90")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_MAX_OUTPUT_AGE_SEC", "90")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_NO_ORIGINAL_FALLBACK_WHEN_RAW_EXISTS", "1")
    _PATCHED = True
    logger.warning("[SUMMARY FORCE DIRECT 1M] installed version=%s main=%s (job_summary wrap removed; called as tier1 from runner_core.job_summary)", VERSION, _is_main_py())
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY FORCE DIRECT 1M] auto install failed")

__all__ = ["VERSION", "install", "tier1_build_and_publish", "_is_main_py", "_env_bool"]
