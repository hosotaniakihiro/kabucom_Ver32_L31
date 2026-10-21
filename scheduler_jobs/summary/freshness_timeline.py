# -*- coding: utf-8 -*-
"""Summary 鮮度タイムライン (P0-6)。

これまで「merged tf=1 latest=14:09」「push_summary_engine latest=14:30」のように
別々のログに散っていたため、どこで鮮度が落ちたのか一目で分からなかった。

この module は 1 行で全段の最新時刻を並べる:

    [FRESHNESS] now=15:25:30
        websocket_latest=15:25:28 (age=2.0s)
        memory_push_latest=15:25:27 (age=3.0s)
        push_db_latest=15:25:00 (age=30.0s)
        raw_df_latest=15:25:00 (age=30.0s)
        1m_ohlc_latest=15:25:00 (age=30.0s)
        summary_history_latest=15:24:00 (age=90.0s)
        merged_summary_latest=15:24:00 (age=90.0s)
        -> worst=summary_history/merged_summary

目標 (完成条件):
    websocket_latest      age < 5 秒
    memory_push_latest    age < 5 秒
    1m_ohlc_latest        age < 90 秒
    merged_summary_latest age < 90 秒

閾値を超えた段には ! が付き、行全体が WARNING になる。
どの段で落ちたかを示すだけで、鮮度判定そのもの (STALE DROP 等) は変更しない。

env:
    SUMMARY_FRESHNESS_TIMELINE_ENABLED=0  -> 無効化
    SUMMARY_FRESHNESS_TIMELINE_MIN_SEC=20 -> 出力間隔の下限 (既定 20 秒)
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LAST_EMIT = [0.0]
_LOCK = threading.Lock()

# (キー, 目標 age 秒)。None は目標なし (参考表示)。
STAGES = (
    ("websocket_latest", 5.0),
    ("memory_push_latest", 5.0),
    ("push_db_latest", None),
    ("raw_df_latest", None),
    ("1m_ohlc_latest", 90.0),
    ("summary_history_latest", None),
    ("merged_summary_latest", 90.0),
)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return default
        return float(str(v).strip())
    except Exception:
        return default


def enabled() -> bool:
    return _env_bool("SUMMARY_FRESHNESS_TIMELINE_ENABLED", True)


def _to_dt(v: Any) -> Optional[dt.datetime]:
    """いろいろな形 (datetime / str / pandas Timestamp / epoch) を datetime にする。"""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    if isinstance(v, (int, float)):
        try:
            if v <= 0:
                return None
            return dt.datetime.fromtimestamp(float(v))
        except Exception:
            return None
    # 高速パス: よくある文字列形式は pandas を使わずに読む。
    # tick 走査で銘柄数ぶん pandas.to_datetime を呼ぶと診断自体が重くなる。
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%H:%M:%S"):
            try:
                got = dt.datetime.strptime(s, fmt)
                if fmt == "%H:%M:%S":
                    today = dt.date.today()
                    got = got.replace(year=today.year, month=today.month, day=today.day)
                return got
            except ValueError:
                continue
    try:
        import pandas as pd
        ts = pd.to_datetime(v, errors="coerce")
        if ts is None or pd.isna(ts):
            return None
        return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


def _latest_of_df(df: Any) -> Optional[dt.datetime]:
    """DataFrame から datetime 列の最大値を取る。"""
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for col in ("datetime", "timestamp", "dt", "time", "snapshot_time"):
            if col in df.columns:
                s = pd.to_datetime(df[col], errors="coerce").dropna()
                if not s.empty:
                    return s.max().to_pydatetime().replace(tzinfo=None)
        return None
    except Exception:
        return None


def _safe(fn, label: str):
    try:
        return fn()
    except Exception:
        logger.debug("[FRESHNESS] probe failed stage=%s", label, exc_info=True)
        return None


# どの accessor が当たったかを 1 回だけ出す (推測を残さないため)
_ACCESSOR_LOGGED: set = set()


def _resolve_value(obj: Any, name: str) -> Any:
    """属性でもメソッドでも値を取り出す。

    global_data は push_df を属性ではなく get_push_df() として持つ。
    属性だけ見ていたため束縛メソッドが返り、黙って None になっていた。
    """
    if obj is None:
        return None
    for attr, call in ((name, False), (name, True), ("get_" + name, True)):
        if not hasattr(obj, attr):
            continue
        try:
            v = getattr(obj, attr)
        except Exception:
            continue
        if callable(v):
            if not call:
                continue
            try:
                v = v()
            except Exception:
                continue
        if v is not None:
            return v
    return None


def _first_df(obj: Any, names, stage: str):
    """候補名を順に試して、最初に取れた DataFrame の最新時刻を返す。"""
    for name in names:
        v = _resolve_value(obj, name)
        ts = _latest_of_df(v)
        if ts is not None:
            key = "%s:%s" % (stage, name)
            if key not in _ACCESSOR_LOGGED:
                _ACCESSOR_LOGGED.add(key)
                logger.info("[FRESHNESS] accessor resolved stage=%s via=%s", stage, name)
            return ts
    return None


def _first_ts(obj: Any, names, stage: str):
    for name in names:
        ts = _to_dt(_resolve_value(obj, name))
        if ts is not None:
            key = "%s:%s" % (stage, name)
            if key not in _ACCESSOR_LOGGED:
                _ACCESSOR_LOGGED.add(key)
                logger.info("[FRESHNESS] accessor resolved stage=%s via=%s", stage, name)
            return ts
    return None


def _pushdb_enabled() -> bool:
    return _env_bool("SUMMARY_FRESHNESS_PUSHDB_ENABLED", True)


def _pushdb_ttl() -> float:
    return _env_float("SUMMARY_FRESHNESS_PUSHDB_TTL_SEC", 60.0)


def _collect_budget() -> float:
    """診断全体の上限。

    1.0 秒だと 7 段中 3 段で打ち切られ、残りを None と誤表示していた
    (2026-08-27 実測)。tick 予算 25 秒に対して 2 秒は十分小さいので、
    「途中で切って誤診する」より「最後まで取る」を選ぶ。
    """
    return max(0.1, _env_float("SUMMARY_FRESHNESS_COLLECT_BUDGET_SEC", 2.0))


def _ws_ttl() -> float:
    return _env_float("SUMMARY_FRESHNESS_WS_TTL_SEC", 5.0)


def _ws_scan_limit() -> int:
    try:
        return int(float(_env_float("SUMMARY_FRESHNESS_WS_SCAN_LIMIT", 300)))
    except Exception:
        return 300


# websocket 最新時刻のキャッシュ (値, 取得時刻)
_WS_CACHE: list = [None, 0.0]


# push DB probe のキャッシュ (値, 取得時刻) と background single-flight
_PUSHDB_CACHE: list = [None, 0.0]
_PUSHDB_THREAD: list = [None]
_PUSHDB_LOCK = threading.Lock()


def _pushdb_read_now() -> Optional[dt.datetime]:
    """push DB から最新時刻を読む。background からだけ呼ぶ。

    2026-09-05: 置き場所は push_db_location が決める (既定はローカル)。
    ここを NAS 固定にすると、鮮度チェックが前日の複製を見て
    「PUSH が止まっている」と誤判定する。
    """
    import sqlite3
    path = os.path.join(r"X:\raw_data\kabu_station\push",
                        "push%s.db" % dt.datetime.now().strftime("%Y%m%d"))
    try:
        from trading.push import push_db_location

        path = str(push_db_location.push_db_path())
    except Exception:
        pass
    path = os.getenv("PUSH_DB_PATH", path)
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1.0)
    try:
        conn.execute("PRAGMA busy_timeout=800")
        deadline = time.perf_counter() + 2.0
        conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 2000)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'push%' LIMIT 1").fetchone()
        if not row:
            return None
        t = row[0]
        cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % t).fetchall()]
        dtc = next((c for c in ("datetime", "timestamp", "recv_at", "created_at")
                    if c in cols), None)
        if not dtc:
            return None
        r = conn.execute('SELECT MAX("%s") FROM "%s"' % (dtc, t)).fetchone()
        return _to_dt(r[0]) if r else None
    finally:
        try:
            conn.set_progress_handler(None, 0)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _pushdb_start_refresh() -> bool:
    with _PUSHDB_LOCK:
        th = _PUSHDB_THREAD[0]
        if th is not None and th.is_alive():
            return False

        def _work() -> None:
            t0 = time.perf_counter()
            try:
                v = _pushdb_read_now()
                _PUSHDB_CACHE[0] = v
                _PUSHDB_CACHE[1] = time.monotonic()
                logger.debug("[FRESHNESS] push_db refresh done latest=%s elapsed=%.3fs",
                             v, time.perf_counter() - t0)
            except Exception:
                # 失敗しても既存の値は残す。診断のために本処理を止めない。
                _PUSHDB_CACHE[1] = time.monotonic()
                logger.debug("[FRESHNESS] push_db refresh failed elapsed=%.3fs",
                             time.perf_counter() - t0, exc_info=True)

        t = threading.Thread(target=_work, name="freshness-pushdb", daemon=True)
        _PUSHDB_THREAD[0] = t
    t.start()
    return True


def _pushdb_cached() -> Optional[dt.datetime]:
    """hot path 用。NAS は待たない。"""
    if not _pushdb_enabled():
        return None
    age = time.monotonic() - _PUSHDB_CACHE[1]
    if _PUSHDB_CACHE[1] <= 0.0 or age > _pushdb_ttl():
        _pushdb_start_refresh()
    return _PUSHDB_CACHE[0]


def collect() -> dict:
    """各段の最新時刻を集める。1 つ失敗しても他は出す。

    診断のために本処理を遅らせないよう、全体に実時間の上限を掛ける。
    """
    out: dict[str, Optional[dt.datetime]] = {}
    _t_collect = time.perf_counter()
    _budget = _collect_budget()

    def _over() -> bool:
        return (time.perf_counter() - _t_collect) > _budget

    def _global_data():
        import global_state
        return getattr(global_state, "global_data", None)

    gd = _safe(_global_data, "global_data")

    # 1. WebSocket が最後に受けた時刻
    def _ws():
        v = _first_ts(gd, ("push_last_recv_at", "last_push_recv_at",
                           "push_stream_last_recv", "last_websocket_recv_at",
                           "last_recv_at", "last_tick_at"), "websocket_latest")
        if v is not None:
            return v
        try:
            from trading.push.push_stream import monitor as _mon
            v = _first_ts(_mon, ("last_recv_at", "last_received_at", "LAST_RECV_AT"),
                          "websocket_latest")
            if v is not None:
                return v
        except Exception:
            pass
        # PUSH tick スナップショットの最新時刻から拾う。
        # 全銘柄を毎回走査すると診断自体が重くなるので、
        # 結果をキャッシュし、走査数にも上限を掛ける。
        age = time.monotonic() - _WS_CACHE[1]
        if _WS_CACHE[1] > 0.0 and age <= _ws_ttl():
            return _WS_CACHE[0]
        try:
            from core.global_context import context as _ctx
            snap = _ctx.GC.push.snapshot()
            if isinstance(snap, dict) and snap:
                best = None
                limit = _ws_scan_limit()
                seen = 0
                for t in snap.values():
                    if limit and seen >= limit:
                        break
                    seen += 1
                    if not isinstance(t, dict):
                        continue
                    for k in ("recv_at", "received_at", "timestamp", "datetime"):
                        raw = t.get(k)
                        if raw is None:
                            continue
                        ts = _to_dt(raw)
                        if ts is not None and (best is None or ts > best):
                            best = ts
                        break
                _WS_CACHE[0] = best
                _WS_CACHE[1] = time.monotonic()
                if best is not None:
                    key = "websocket_latest:push_snapshot"
                    if key not in _ACCESSOR_LOGGED:
                        _ACCESSOR_LOGGED.add(key)
                        logger.info(
                            "[FRESHNESS] accessor resolved stage=%s via=%s scanned=%d",
                            "websocket_latest", "GC.push.snapshot()", seen)
                    return best
        except Exception:
            pass
        return None

    out["websocket_latest"] = _safe(_ws, "websocket_latest")

    # 2. メモリ上の PUSH 最新 (Entry が実際に見るもの)
    def _mem():
        v = _first_df(gd, ("push_df", "latest_push_df", "push_merged_df"),
                      "memory_push_latest")
        if v is not None:
            return v
        # context 側のモジュール関数も試す
        try:
            from core.global_context import context as _ctx
            return _latest_of_df(_ctx.get_push_df())
        except Exception:
            return None

    out["memory_push_latest"] = _safe(_mem, "memory_push_latest")

    # 3. PUSH DB の最新 (別プロセスが書いている)
    #    NAS を開くので hot path では読まない。キャッシュを返し背景で更新する。
    out["push_db_latest"] = _safe(_pushdb_cached, "push_db_latest")

    # 4. runner が返した raw df
    def _raw():
        return _first_df(gd, ("last_raw_summary_df", "raw_df", "latest_raw_df",
                              "raw_summary_df"), "raw_df_latest")

    if _over():
        logger.debug("[FRESHNESS] collect budget hit before raw_df_latest")
        return out
    out["raw_df_latest"] = _safe(_raw, "raw_df_latest")

    # 5. 1 分足 OHLC
    def _ohlc():
        v = _first_df(gd, ("push_1m_df", "ohlc_1m_df", "latest_1m_df",
                           "push_ohlc_1m_df", "summary_1m_df"), "1m_ohlc_latest")
        if v is not None:
            return v
        # 1m OHLC の代わりに push merged summary(tf=1) を見る
        try:
            from core.global_context import context as _ctx
            return _latest_of_df(_ctx.get_merged_summary(1, source="push"))
        except Exception:
            return None

    out["1m_ohlc_latest"] = _safe(_ohlc, "1m_ohlc_latest")

    # 6. summary history
    def _hist():
        from core.global_context import context as _ctx
        for src in ("push", "legacy", None):
            try:
                df = _ctx.get_summary_history(1) if src is None else _ctx.get_summary_history(1, source=src)
            except Exception:
                continue
            ts = _latest_of_df(df)
            if ts is not None:
                return ts
        return None

    if _over():
        logger.debug("[FRESHNESS] collect budget hit before summary_history_latest")
        return out
    out["summary_history_latest"] = _safe(_hist, "summary_history_latest")

    # 7. merged summary (Entry が最終的に見るもの)
    def _merged():
        from core.global_context import context as _ctx
        best = None
        for src in (None, "push", "legacy"):
            try:
                df = _ctx.get_merged_summary(1) if src is None else _ctx.get_merged_summary(1, source=src)
            except Exception:
                continue
            ts = _latest_of_df(df)
            if ts is not None and (best is None or ts > best):
                best = ts
        return best

    out["merged_summary_latest"] = _safe(_merged, "merged_summary_latest")

    return out


def log_timeline(reason: str = "", *, force: bool = False, now: Optional[dt.datetime] = None) -> dict:
    """全段の最新時刻を 1 行で出す。

    出しすぎないよう既定 20 秒に 1 回へ間引く (force=True で必ず出す)。
    """
    if not enabled():
        return {}
    min_sec = _env_float("SUMMARY_FRESHNESS_TIMELINE_MIN_SEC", 20.0)
    if not force:
        with _LOCK:
            if (time.time() - _LAST_EMIT[0]) < min_sec:
                return {}
            _LAST_EMIT[0] = time.time()
    else:
        with _LOCK:
            _LAST_EMIT[0] = time.time()

    n = now or dt.datetime.now()
    _t0 = time.perf_counter()
    data = collect()
    _collect_el = time.perf_counter() - _t0

    parts = []
    worst = []
    for key, target in STAGES:
        if key not in data:
            # 予算で打ち切られて「まだ見ていない」段。
            # None (取れなかった) と混同すると誤診の元になる。
            parts.append("%s=skipped" % key)
            continue
        v = data.get(key)
        if v is None:
            parts.append("%s=None" % key)
            continue
        age = (n - v).total_seconds()
        mark = ""
        if target is not None and age > target:
            mark = "!"
            worst.append("%s(age=%.1fs>%.0fs)" % (key, age, target))
        parts.append("%s=%s(age=%.1fs)%s" % (key, v.strftime("%H:%M:%S"), age, mark))

    msg = "[FRESHNESS] now=%s %s reason=%s collect=%.3fs" % (
        n.strftime("%H:%M:%S"), " ".join(parts), reason or "-", _collect_el)
    if _collect_el >= 1.0:
        logger.warning("[FRESHNESS] 診断自体が遅い collect=%.3fs (budget=%.1fs)",
                       _collect_el, _collect_budget())
    if worst:
        logger.warning("%s -> OVER_TARGET=%s", msg, ",".join(worst))
    else:
        logger.info("%s -> all within target", msg)
    return data
