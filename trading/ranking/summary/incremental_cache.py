# ============================================================
# File   : trading/ranking/summary/incremental_cache.py
# Version: Ver1.0-RANKING-SNAPSHOT-INCREMENTAL-BOUNDED-CACHE
# ------------------------------------------------------------
# 【なぜ必要か】
#   ranking_snapshot_1min は場中ずっと増える。
#   2026-08-28 の実測:
#     lookback=240 でも 82,000〜84,000 行 / 1 回の読み込み
#     [RANKING SUMMARY BOOTSTRAP] rows=290,939 -> 113,789 (1,321 symbols)
#     [BOOTSTRAP] done elapsed=362.52s
#     RSS 338.6MB -> 1,717.0MB
#
#   snapshot cache (TTL=45s) は「1 tick 内の読み直し」しか防げないので、
#   毎分ごとに NAS から 80,000 行級を読み直していた。
#
# 【方針】
#   indicator に必要なのは銘柄あたり最大 105 bars 程度しかない。
#   ならば
#     - 初回だけ有界な full read
#     - 以降は watermark より新しい行だけ delta read (1 分ぶん = 銘柄数ぶん)
#     - 銘柄あたり max_bars 本で ring buffer 的に打ち切る
#   とすれば、毎分の NAS read は 80,000 行 -> 1,300 行級になり、
#   保持メモリも 銘柄数 x max_bars で上限が決まる。
#
# 【安全性】
#   - ここは read 経路のキャッシュであり、freshness/threshold の判定は一切変えない。
#   - 返す datetime は DB の値そのもの。古ければ呼び出し側が従来どおり弾く。
#   - trade_date が変われば key が変わるので日跨ぎで混ざらない。
#   - RANKING_INCREMENTAL_FULL_REFRESH_SEC ごとに watermark を
#     RANKING_INCREMENTAL_REFRESH_OVERLAP_MIN 分だけ巻き戻して読み直し、
#     後追い修正/backfill された行を取りこぼさない
#     (cold full read は NAS 上 66〜69 秒かかるので行わない)。
#   - delta が上限行数に張り付いたら full read へ降格する (取りこぼし回避)。
#
# ENV:
#   RANKING_INCREMENTAL_CACHE=1              # 既定 ON / 0 で従来動作
#   RANKING_INCREMENTAL_MAX_BARS=105         # 銘柄あたり保持本数
#   RANKING_INCREMENTAL_FULL_REFRESH_SEC=900 # 定期再同期の間隔
#   RANKING_INCREMENTAL_REFRESH_OVERLAP_MIN=15 # 再同期で巻き戻す分数
#   RANKING_INCREMENTAL_MAX_DELTA_ROWS=40000  # delta が大きすぎたら full へ降格
#   RANKING_INCREMENTAL_RESYNC_BACKGROUND=1  # 再同期を背景で行う
#   RANKING_INCREMENTAL_RESYNC_DEADLINE_SEC=180 # 背景 read の締切
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time as _time
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STATE: dict[Any, dict[str, Any]] = {}

_HITS = 0
_MISSES = 0
_FULL_READS = 0
_DELTA_READS = 0
_DELTA_ROWS = 0
_FULL_ROWS = 0


# ============================================================
# env
# ============================================================

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def enabled() -> bool:
    return _env_bool("RANKING_INCREMENTAL_CACHE", True)


def max_bars() -> int:
    return max(1, _env_int("RANKING_INCREMENTAL_MAX_BARS", 105))


def full_refresh_sec() -> float:
    return max(0.0, _env_float("RANKING_INCREMENTAL_FULL_REFRESH_SEC", 900.0))


def max_delta_rows() -> int:
    return max(0, _env_int("RANKING_INCREMENTAL_MAX_DELTA_ROWS", 40000))


def resync_deadline_sec() -> float:
    """背景再同期の read 締切。

    前景の RANKING_DB_READ_DEADLINE_SEC (20 秒) は
    「tick を止めないための締切」なので、前景を止めない背景処理には短すぎる。
    実測では前景 tick と NAS を取り合って 20 秒で打ち切られていた。
    """
    return max(20.0, _env_float("RANKING_INCREMENTAL_RESYNC_DEADLINE_SEC", 180.0))


def refresh_overlap_min() -> int:
    """定期再同期で watermark を巻き戻す分数。

    後追い backfill がこの窓より古い位置に入る場合は拾えないので、
    実運用の backfill 遅延より十分大きく取る。

    ただし大きすぎると再同期 1 回の delta が max_delta_rows に達して
    full read へ降格してしまう (= 避けたかった 66 秒が戻ってくる)。
    実測 (2026-08-28, 631 銘柄):
        overlap=30 分 -> delta 36,648 行 (上限 40,000 に対し余裕 8%)
        overlap=15 分 -> delta 17,634 行 (上限 40,000 に対し余裕 2.3 倍)
    上限を 120,000 へ広げる案は、SQLite の ORDER BY ソートが
    RANKING_DB_READ_DEADLINE_SEC (20 秒) を超えて
    cancelled=True / rows_fetched=0 になったため取り消した。
    overlap を 15 分にして余裕を作るのが正しい。
    """
    return max(1, _env_int("RANKING_INCREMENTAL_REFRESH_OVERLAP_MIN", 15))


# ============================================================
# helpers
# ============================================================

def _owner() -> str:
    """どのプロセス役割がこの entry を作ったか (診断用)。"""
    for name in (
        "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
        "AUTOSTOCK_SUMMARY_DB_WRITER",
        "AUTOSTOCK_MAIN_DATABASE_PROCESS",
    ):
        if str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}:
            return "%s/pid=%s" % (name, os.getpid())
    return "main/pid=%s" % os.getpid()


# memory_usage(deep=True) の全行走査を避けるためのサンプル行数 (R18)
_MEM_SAMPLE_ROWS = 2000


def _frame_mb(df) -> float:
    """概算メモリ (MB)。

    R18: deep=True の全行走査は object 列が多いと非常に重い。
    実測 (108,098 行 x 111 列):
        memory_usage(deep=True)   0.674s  294.06MB
        memory_usage(deep=False)  0.000s   92.37MB  (-68.6% / 使えない)
        2000 行サンプル外挿       0.035s  294.05MB  (誤差 0.0%)
    これはログ 1 項目のためのコストで、しかも try_delta の毎 tick 経路と
    _LOCK を保持したままの stats() から呼ばれていた。サンプル外挿へ変更する。
    """
    try:
        n = len(df)
        if n <= 0:
            return 0.0

        if n <= _MEM_SAMPLE_ROWS:
            return float(df.memory_usage(deep=True).sum()) / (1024.0 * 1024.0)

        step = max(1, n // _MEM_SAMPLE_ROWS)
        s = df.iloc[::step]

        if len(s) <= 0:
            return -1.0

        mb = float(s.memory_usage(deep=True).sum()) / (1024.0 * 1024.0)
        return mb * (float(n) / float(len(s)))
    except Exception:
        return -1.0


def _trim(df: pd.DataFrame, bars: int) -> pd.DataFrame:
    """銘柄あたり直近 bars 本へ切り詰める (ring buffer 相当)。

    R18: 旧実装は 111 列すべてを sort/drop_duplicates/groupby/sort と
    4 回並べ替えていた。キー 2 列 + 行位置だけの narrow frame で
    残す位置を決め、最後に 1 回だけ take する。
    実測 (121,764 行 x 111 列): 0.360s -> 0.199s、出力は .equals() で一致。
    """
    if df is None or df.empty:
        return df
    if "symbol" not in df.columns or "datetime" not in df.columns:
        return df

    try:
        k = pd.DataFrame({
            "symbol": np.asarray(df["symbol"]),
            "datetime": np.asarray(df["datetime"]),
            "_pos": np.arange(len(df), dtype=np.int64),
        })
        k = k.sort_values(["symbol", "datetime"], kind="stable")
        k = k.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        k = k.groupby("symbol", sort=False, group_keys=False).tail(bars)
        k = k.sort_values("datetime", kind="stable")
        return df.take(k["_pos"].to_numpy()).reset_index(drop=True)
    except Exception:
        # narrow 経路で落ちたら従来どおりの経路で必ず切り詰める
        logger.exception("[RANKING INCREMENTAL CACHE] _trim narrow 経路が失敗 -> 従来経路")
        out = df.sort_values(["symbol", "datetime"], kind="stable")
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        out = out.groupby("symbol", sort=False, group_keys=False).tail(bars)
        return out.sort_values("datetime", kind="stable").reset_index(drop=True)


def _watermark_of(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    if df is None or df.empty or "datetime" not in df.columns:
        return None
    try:
        v = pd.to_datetime(df["datetime"], errors="coerce").max()
        if pd.isna(v):
            return None
        return pd.Timestamp(v)
    except Exception:
        return None


def _key_repr(key: Any) -> str:
    try:
        return str(key)[:120]
    except Exception:
        return "?"


# ============================================================
# public
# ============================================================

# ============================================================
# 背景再同期 (R17)
# ============================================================

# read が打ち切られたと判断する閾値 (deadline 20 秒に対して余裕を見る)
def _resync_slow_warn_sec() -> float:
    # 締切近くまで掛かって 0 行なら打ち切りとみなす
    return max(5.0, resync_deadline_sec() * 0.75)

_BG_LOCK = threading.Lock()
_BG_INFLIGHT: set = set()
_BG_DONE = 0
_BG_FAILED = 0


def _submit_bg_resync(
    *, key, db_path, order_col, base_sql, has_where, params,
    reader, normalizer, cutoff, age, refresh, overlap,
) -> bool:
    """定期再同期を背景で 1 本だけ走らせる。tick は待たない。"""
    global _BG_DONE, _BG_FAILED

    with _BG_LOCK:
        if key in _BG_INFLIGHT:
            logger.info(
                "[RANKING INCREMENTAL CACHE] 背景再同期は実行中 -> 二重起動しない key=%s",
                _key_repr(key),
            )
            return False
        _BG_INFLIGHT.add(key)

    # 走っている間に何度も submit されないよう、先に時計を進める
    with _LOCK:
        st = _STATE.get(key)
        if st is not None:
            st["created_at_m"] = _time.monotonic()

    def _task() -> None:
        global _BG_DONE, _BG_FAILED
        t0 = _time.perf_counter()
        try:
            dp = dict(params)
            dp["inc_wm"] = cutoff.strftime("%Y-%m-%d %H:%M:%S")
            sql = base_sql + ("\nAND " if has_where else "\nWHERE ")
            sql += '"%s" > :inc_wm' % order_col
            sql += '\nORDER BY "%s" ASC' % order_col
            lim = max_delta_rows()
            if lim > 0:
                sql += "\nLIMIT %d" % int(lim)

            # 背景 read は前景 tick と NAS を取り合うので遅い。
            # 前景を止めていない以上、締切は長くてよい。
            # stage 停止にも巻き込まれないようにする。
            try:
                raw = reader(
                    db_path=db_path, sql=sql, params=dp,
                    deadline_sec=resync_deadline_sec(),
                    cooperative_stop=False,
                )
            except TypeError:
                # 古い reader (deadline 引数なし) でも動くようにしておく
                raw = reader(db_path=db_path, sql=sql, params=dp)
            n = 0 if raw is None else len(raw)
            el = _time.perf_counter() - t0
            if n == 0:
                # 0 行は「本当に差分が無い」場合と
                # 「read が deadline / stage 停止で cancelled された」場合の
                # 両方があり得る。後者を黙って成功扱いにすると
                # 再同期が効いていないことに気づけないので warning で出す。
                if el >= _resync_slow_warn_sec():
                    logger.error(
                        "[RANKING INCREMENTAL CACHE][BG RESYNC] rows=0 だが %.1fs かかった "
                        "-> read が打ち切られた可能性が高い "
                        "(RANKING_DB_READ_DEADLINE_SEC / RANKING_INCREMENTAL_MAX_DELTA_ROWS を確認)。"
                        "再同期は効いていない",
                        el,
                    )
                    _BG_FAILED += 1
                    return
                logger.warning(
                    "[RANKING INCREMENTAL CACHE][BG RESYNC] 差分なし rows=0 %.1fs "
                    "(overlap=%d分の範囲に変更なし)",
                    el, overlap,
                )
                _BG_DONE += 1
                return

            nd = normalizer(raw)
            # 生フレームはここでもう要らない。
            # base + delta + merged を同時に抱えると RSS が跳ねる
            # (実測: 背景再同期中に 491MB -> 535MB)。
            del raw
            if nd is None or nd.empty:
                _BG_DONE += 1
                return

            with _LOCK:
                st2 = _STATE.get(key)
                if st2 is None:
                    return
                cur = st2["df"]
            merged = _trim(pd.concat([cur, nd], ignore_index=True), max_bars())
            del nd, cur
            new_wm = _watermark_of(merged)

            with _LOCK:
                st2 = _STATE.get(key)
                if st2 is not None:
                    st2["df"] = merged
                    if new_wm is not None:
                        st2["watermark"] = new_wm
                    st2["generation"] = int(st2["generation"]) + 1
                    st2["rows"] = len(merged)
                    st2["created_at_m"] = _time.monotonic()
                    st2["updated_at_m"] = _time.monotonic()
            _BG_DONE += 1
            logger.warning(
                "[RANKING INCREMENTAL CACHE][BG RESYNC] 完了 %.1fs delta_rows=%d "
                "-> rows=%d symbols=%d mem=%.1fMB wm=%s overlap=%d分 "
                "(tick は待っていない)",
                _time.perf_counter() - t0, n, len(merged),
                merged["symbol"].nunique() if "symbol" in merged.columns else -1,
                _frame_mb(merged), new_wm, overlap,
            )
        except Exception:
            _BG_FAILED += 1
            logger.exception("[RANKING INCREMENTAL CACHE][BG RESYNC] 失敗")
        finally:
            with _BG_LOCK:
                _BG_INFLIGHT.discard(key)

    logger.warning(
        "[RANKING INCREMENTAL CACHE] 定期再同期 age=%.0fs >= %.0fs -> "
        "背景で %d 分ぶん読み直す (tick は待たない。"
        "前景実行だと実測 48〜59 秒かかる)",
        age, refresh, overlap,
    )
    threading.Thread(
        target=_task, name="ranking-inc-resync", daemon=True,
    ).start()
    return True


def bg_resync_stats() -> dict:
    with _BG_LOCK:
        return {
            "inflight": len(_BG_INFLIGHT),
            "done": _BG_DONE,
            "failed": _BG_FAILED,
            "enabled": _env_bool("RANKING_INCREMENTAL_RESYNC_BACKGROUND", True),
        }


def try_delta(
    *,
    key: Any,
    db_path: str,
    order_col: Optional[str],
    base_sql: str,
    has_where: bool,
    params: dict,
    reader: Callable[..., pd.DataFrame],
    normalizer: Callable[[pd.DataFrame], pd.DataFrame],
) -> Optional[pd.DataFrame]:
    """warm state があれば delta だけ読んで merge した frame を返す。

    使えない場合は None を返し、呼び出し側は従来どおり full read する。
    """
    if not enabled() or not order_col:
        return None

    with _LOCK:
        st = _STATE.get(key)
        if st is None:
            return None
        age = _time.monotonic() - float(st["created_at_m"])
        refresh = full_refresh_sec()
        base_df = st["df"]
        wm = st["watermark"]
        generation = int(st["generation"])

    # ------------------------------------------------------------
    # 定期再同期は「巻き戻し delta」で行う (R17)
    #
    # 当初は state を捨てて cold full read に戻していたが、
    # 30 分 dry-run の実測で full read は NAS 上 66〜69 秒かかっていた。
    # 15 分ごとに 69 秒固まるのは場中では許容できない。
    #
    # 再同期の目的は「後追いで backfill / 修正された行を拾う」ことなので、
    # watermark を overlap 分だけ巻き戻して delta を読めば足りる。
    # merge 側は (symbol, datetime) で keep="last" なので、
    # 修正行は上書きされる。
    # ------------------------------------------------------------
    rewound = False
    if refresh > 0.0 and age >= refresh and wm is not None:
        overlap = refresh_overlap_min()
        try:
            cutoff = pd.Timestamp(wm) - pd.Timedelta(minutes=overlap)
        except Exception:
            logger.debug("[RANKING INCREMENTAL CACHE] rewind failed", exc_info=True)
            with _LOCK:
                _STATE.pop(key, None)
            return None

        # --------------------------------------------------------
        # 再同期は **背景スレッド** で行い、tick は待たない (R17)
        #
        # 「COUNT(*) が同じなら省略」する案は却下した。
        # 後追い修正が in-place UPDATE だと行数が変わらず、
        # COUNT / MAX(rowid) では検知できない。
        # 実際にテストで close=777 への更新を見逃すことを確認済み。
        # 速度のために修正を取りこぼすことはできない。
        #
        # かといって前景で読むと止まる。実測 (631 銘柄):
        #   overlap=30 分 -> delta 36,648 行 / tick 59.4 秒
        #   overlap=15 分 -> delta 17,634 行 / tick 48.5 秒
        #   うち DB read は 3.9 秒。残りは normalize + merge の pandas コスト。
        # overlap を縮めても消えない = 前景から外すのが正しい。
        #
        # tick は「今持っている有界なキャッシュ」をそのまま返し、
        # 再同期は背景で完了させて次の tick から反映する。
        # 再同期が終わるまで古いままだが、
        # ・返す datetime は DB の値そのもの
        # ・freshness 判定は呼び出し側が従来どおり行う
        # ので、古ければ従来どおり弾かれる (fail-open にはならない)。
        #
        # RANKING_INCREMENTAL_RESYNC_BACKGROUND=0 で前景実行に戻せる。
        # --------------------------------------------------------
        if _env_bool("RANKING_INCREMENTAL_RESYNC_BACKGROUND", True):
            _submit_bg_resync(
                key=key, db_path=db_path, order_col=order_col,
                base_sql=base_sql, has_where=has_where, params=params,
                reader=reader, normalizer=normalizer,
                cutoff=cutoff, age=age, refresh=refresh, overlap=overlap,
            )
            # tick は待たない。通常の delta (watermark 以降) だけ読む。
        else:
            wm = cutoff
            rewound = True
            logger.warning(
                "[RANKING INCREMENTAL CACHE] 定期再同期 age=%.0fs >= %.0fs -> "
                "watermark を %d 分巻き戻して delta 再読込 (前景実行)",
                age, refresh, overlap,
            )

    if wm is None or base_df is None or base_df.empty:
        return None

    # ------------------------------------------------------------
    # watermark より新しい行だけ読む
    # ------------------------------------------------------------
    delta_params = dict(params)
    delta_params["inc_wm"] = pd.Timestamp(wm).strftime("%Y-%m-%d %H:%M:%S")
    sql = base_sql + ("\nAND " if has_where else "\nWHERE ") + '"%s" > :inc_wm' % order_col
    sql += '\nORDER BY "%s" ASC' % order_col
    lim = max_delta_rows()
    if lim > 0:
        sql += "\nLIMIT %d" % int(lim)

    t0 = _time.perf_counter()
    try:
        raw = reader(db_path=db_path, sql=sql, params=delta_params)
    except Exception:
        logger.exception(
            "[RANKING INCREMENTAL CACHE] delta read failed -> full read へ降格 key=%s",
            _key_repr(key),
        )
        with _LOCK:
            _STATE.pop(key, None)
        return None
    read_ms = (_time.perf_counter() - t0) * 1000.0

    n_delta = 0 if raw is None else len(raw)

    # delta が上限に張り付いた = 取りこぼしの可能性 -> full read へ降格
    if lim > 0 and n_delta >= lim:
        logger.warning(
            "[RANKING INCREMENTAL CACHE] delta rows=%d が上限 %d に達した -> full read へ降格 key=%s "
            "(取りこぼしを避ける)",
            n_delta, lim, _key_repr(key),
        )
        with _LOCK:
            _STATE.pop(key, None)
        return None

    global _HITS, _DELTA_READS, _DELTA_ROWS
    with _LOCK:
        _HITS += 1
        _DELTA_READS += 1
        _DELTA_ROWS += n_delta

    if n_delta == 0:
        if rewound:
            with _LOCK:
                st = _STATE.get(key)
                if st is not None:
                    st["created_at_m"] = _time.monotonic()
        logger.info(
            "[RANKING INCREMENTAL CACHE] delta なし rows=0 read=%.1fms wm=%s "
            "-> 保持中の %d 行をそのまま使う (NAS 大量 read を行わない)",
            read_ms, wm, len(base_df),
        )
        return base_df

    nd = normalizer(raw)
    if nd is None or nd.empty:
        # 巻き戻し再同期で読んだ行が normalize 後に空になった場合も
        # 時計を進める。進めないと age >= refresh のままなので
        # 毎 tick 巻き戻し read を撃ち続けてしまう。
        if rewound:
            with _LOCK:
                st = _STATE.get(key)
                if st is not None:
                    st["created_at_m"] = _time.monotonic()
        return base_df

    bars = max_bars()
    merged = _trim(pd.concat([base_df, nd], ignore_index=True), bars)
    new_wm = _watermark_of(merged) or wm

    with _LOCK:
        st = _STATE.get(key)
        if st is not None:
            st["df"] = merged
            st["watermark"] = new_wm
            st["generation"] = generation + 1
            st["rows"] = len(merged)
            st["updated_at_m"] = _time.monotonic()
            if rewound:
                # 再同期し終えたので次の再同期までの時計を進める
                st["created_at_m"] = _time.monotonic()

    logger.warning(
        "[RANKING INCREMENTAL CACHE] delta merged read=%.1fms delta_rows=%d "
        "-> total_rows=%d symbols=%d mem=%.1fMB gen=%d max_bars=%d wm=%s "
        "(full read %d 行ぶんの NAS read を回避)",
        read_ms, n_delta, len(merged),
        merged["symbol"].nunique() if "symbol" in merged.columns else -1,
        _frame_mb(merged), generation + 1, bars, new_wm,
        len(base_df),
    )
    return merged


def store_full(key: Any, df: pd.DataFrame) -> pd.DataFrame:
    """full read の結果を state として保持する。切り詰めた frame を返す。"""
    if not enabled():
        return df
    if df is None or df.empty:
        return df

    bars = max_bars()
    trimmed = _trim(df, bars)
    wm = _watermark_of(trimmed)

    global _MISSES, _FULL_READS, _FULL_ROWS
    now_m = _time.monotonic()
    with _LOCK:
        _MISSES += 1
        _FULL_READS += 1
        _FULL_ROWS += len(df)
        _STATE[key] = {
            "df": trimmed,
            "watermark": wm,
            "generation": 0,
            "rows": len(trimmed),
            "created_at_m": now_m,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at_m": now_m,
            "owner": _owner(),
        }
        while len(_STATE) > 8:
            oldest = min(_STATE, key=lambda k: _STATE[k]["created_at_m"])
            _STATE.pop(oldest, None)

    logger.warning(
        "[RANKING INCREMENTAL CACHE] full read stored rows=%d -> bounded=%d symbols=%d "
        "mem=%.1fMB max_bars=%d wm=%s owner=%s "
        "(以降は delta read のみ。RANKING_INCREMENTAL_CACHE=0 で従来動作)",
        len(df), len(trimmed),
        trimmed["symbol"].nunique() if "symbol" in trimmed.columns else -1,
        _frame_mb(trimmed), bars, wm, _owner(),
    )
    return trimmed


def stats() -> dict:
    """追加P0-2 で要求された cache 診断出力。

    R18: 旧実装は _LOCK を保持したまま nunique() / _frame_mb() を回していた。
    DataFrame は差し替え (st["df"] = merged) でしか更新されない不変参照なので、
    ロック内では参照とスカラだけを取り出し、重い集計はロックの外で行う。
    """
    with _LOCK:
        total = _HITS + _MISSES
        snapshot = [
            (
                _key_repr(k),
                st.get("df"),
                int(st.get("rows", 0)),
                int(st.get("generation", 0)),
                st.get("created_at"),
                float(st.get("created_at_m", 0.0)),
                st.get("owner"),
                str(st.get("watermark")),
            )
            for k, st in _STATE.items()
        ]
        hits, misses = _HITS, _MISSES
        full_reads, delta_reads = _FULL_READS, _DELTA_READS
        full_rows, delta_rows = _FULL_ROWS, _DELTA_ROWS

    entries = []
    for key, df, rows, generation, created_at, created_at_m, owner, watermark in snapshot:
        has_sym = (
            isinstance(df, pd.DataFrame)
            and not df.empty
            and "symbol" in df.columns
        )
        entries.append({
            "key": key,
            "rows": rows,
            "symbols": int(df["symbol"].nunique()) if has_sym else 0,
            "memory_mb": round(_frame_mb(df), 2) if isinstance(df, pd.DataFrame) else -1.0,
            "generation": generation,
            "created_at": created_at,
            "age_sec": round(_time.monotonic() - created_at_m, 1),
            "owner": owner,
            "watermark": watermark,
        })

    return {
        "enabled": enabled(),
        "max_bars": max_bars(),
        "full_refresh_sec": full_refresh_sec(),
        "hits": hits,
        "misses": misses,
        "hit_rate": (hits / total) if total else None,
        "full_reads": full_reads,
        "delta_reads": delta_reads,
        "full_rows_read": full_rows,
        "delta_rows_read": delta_rows,
        "total_memory_mb": round(sum(e["memory_mb"] for e in entries if e["memory_mb"] > 0), 2),
        "entries": entries,
    }


def log_stats(prefix: str = "") -> dict:
    s = stats()
    logger.warning(
        "[RANKING INCREMENTAL CACHE STATS] %sentries=%d rows=%d mem=%.1fMB "
        "hits=%d misses=%d hit_rate=%s full_reads=%d delta_reads=%d "
        "full_rows=%d delta_rows=%d max_bars=%d",
        (prefix + " ") if prefix else "",
        len(s["entries"]),
        sum(e["rows"] for e in s["entries"]),
        s["total_memory_mb"],
        s["hits"], s["misses"],
        ("%.2f" % s["hit_rate"]) if s["hit_rate"] is not None else "n/a",
        s["full_reads"], s["delta_reads"],
        s["full_rows_read"], s["delta_rows_read"],
        s["max_bars"],
    )
    for e in s["entries"]:
        logger.warning(
            "[RANKING INCREMENTAL CACHE ENTRY] rows=%d symbols=%d mem=%.2fMB gen=%d "
            "age=%.0fs created_at=%s owner=%s wm=%s key=%s",
            e["rows"], e["symbols"], e["memory_mb"], e["generation"],
            e["age_sec"], e["created_at"], e["owner"], e["watermark"], e["key"],
        )
    return s


def reset() -> None:
    global _HITS, _MISSES, _FULL_READS, _DELTA_READS, _DELTA_ROWS, _FULL_ROWS
    with _LOCK:
        _STATE.clear()
        _HITS = _MISSES = _FULL_READS = _DELTA_READS = _DELTA_ROWS = _FULL_ROWS = 0


__all__ = [
    "enabled", "max_bars", "full_refresh_sec", "max_delta_rows",
    "try_delta", "store_full", "stats", "log_stats", "reset",
    "refresh_overlap_min", "bg_resync_stats", "resync_deadline_sec",
]
