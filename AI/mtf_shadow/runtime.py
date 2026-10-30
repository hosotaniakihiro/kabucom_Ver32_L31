# ============================================================
# AI/mtf_shadow/runtime.py
# ------------------------------------------------------------
# 場中の記録 (判定には使わない)。
#
# entry_gate.ai_final_entry_check から record(row, result) を呼ぶ。
#   - 呼び出し側は queue に積むだけ (足の取得・推論・DB 書き込みは裏スレッド)。
#   - 同じ銘柄・同じ向きは 60 秒に 1 回だけ。
#   - 1 / 3 / 5 分足の確定足で各モデルの p_down / p_flat / p_up を出す。
#   - 15 分後に 1 分足の終値で実際の騰落 (fwd_ret) を埋める。
#   - 何が起きても例外は外へ出さない。モデルが無ければ何もしない。
#
# 記録先: %LOCALAPPDATA%\autostock\ai_shadow\mtf_shadow_YYYYMMDD.db (records)
# 集計:   python -m AI.mtf_shadow.evaluate
# 止める: MTF_SHADOW_ENABLED=0
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

import pandas as pd

from AI.mtf_shadow.features import FEATURES, HORIZON_MIN, build_features

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = _REPO / "AI" / "models_shadow"
TFS = (1, 3, 5)
THROTTLE_SEC = 60.0

_q: "queue.Queue[dict]" = queue.Queue(maxsize=2000)
_last_sent: dict[tuple, float] = {}
_rate = [0, 0]  # [分, その分の件数]
_started = False
_start_lock = threading.Lock()
_models: dict[int, dict] | None = None
_stats = {"queued": 0, "dropped": 0, "written": 0, "resolved": 0, "errors": 0}


def enabled() -> bool:
    return str(os.getenv("MTF_SHADOW_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _max_per_min() -> int:
    try:
        return int(float(os.getenv("MTF_SHADOW_MAX_PER_MIN", "120")))
    except Exception:
        return 120


def shadow_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "autostock" / "ai_shadow"


def _load_models() -> dict[int, dict]:
    import joblib

    out: dict[int, dict] = {}
    for tf in TFS:
        p = MODEL_DIR / f"mtf_{tf}m.pkl"
        if not p.exists():
            continue
        try:
            d = joblib.load(p)
            try:
                d["model"].set_params(n_jobs=1)  # 1 行ずつなのでスレッドを立てない
            except Exception:
                pass
            out[tf] = d
        except Exception:
            logger.exception("[MTF SHADOW] model load failed %s", p)
    logger.info("[MTF SHADOW] models=%s dir=%s", sorted(out), MODEL_DIR)
    return out


def record(row: dict, result: dict) -> None:
    """判定 1 件を記録へ回す。判定の結果は変えない。"""
    try:
        if not enabled() or not isinstance(row, dict):
            return
        symbol = str(row.get("symbol") or "").strip()
        side = str(row.get("entry_decision") or row.get("side") or "").strip().upper()
        if not symbol or side not in {"BUY", "SELL"}:
            return
        now = time.time()
        key = (symbol, side)
        if now - _last_sent.get(key, 0.0) < THROTTLE_SEC:
            return
        # 足の読み込みが summary と GIL を取り合うので、1 分あたりの件数にも上限を置く
        minute = int(now // 60)
        if _rate[0] != minute:
            _rate[0], _rate[1] = minute, 0
        if _rate[1] >= _max_per_min():
            _stats["dropped"] += 1
            return
        _rate[1] += 1
        _last_sent[key] = now
        _ensure_worker()
        item = {
            "ts": dt.datetime.now(),
            "symbol": symbol,
            "side": side,
            "source": str(row.get("source") or ""),
            "interval": int(row.get("interval") or 1),
            "allow": 1 if (isinstance(result, dict) and result.get("allow")) else 0,
            "reason": str((result or {}).get("reason") or "")[:200],
        }
        try:
            _q.put_nowait(item)
            _stats["queued"] += 1
        except queue.Full:
            _stats["dropped"] += 1
    except Exception:
        logger.debug("[MTF SHADOW] record failed", exc_info=True)


def _ensure_worker() -> None:
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_worker, name="mtf_shadow", daemon=True).start()
        _started = True


# ------------------------------------------------------------
# 裏スレッド
# ------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, symbol TEXT, side TEXT, source TEXT, interval INTEGER,
    allow INTEGER, reason TEXT,
    bar1_dt TEXT, close1 REAL,
    p1_down REAL, p1_flat REAL, p1_up REAL,
    p3_down REAL, p3_flat REAL, p3_up REAL,
    p5_down REAL, p5_flat REAL, p5_up REAL,
    fwd_dt TEXT, fwd_close REAL, fwd_ret REAL
)
"""


def _db(day: dt.date) -> sqlite3.Connection:
    d = shadow_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(d / f"mtf_shadow_{day:%Y%m%d}.db"), timeout=5)
    conn.execute(_SCHEMA)
    return conn


def _predict(symbol: str, tf: int, model: dict, now: dt.datetime):
    from trading.summary.mtf_local_bars import get_bars, is_stale

    bars = get_bars(symbol, tf, now)
    if bars is None or bars.empty or is_stale(bars, tf, now):
        return None, None
    bars = bars[bars["completed"].astype(bool)].reset_index(drop=True)
    if bars.empty:
        return None, None
    feats = build_features(bars).iloc[[-1]]
    p = model["model"].predict_proba(feats[model.get("features") or FEATURES])[0]
    return [float(x) for x in p], bars.iloc[-1]


def _handle(conn: sqlite3.Connection, item: dict) -> None:
    now = item["ts"]
    vals = {k: item[k] for k in ("symbol", "side", "source", "interval", "allow", "reason")}
    vals["ts"] = now.isoformat(timespec="seconds")
    got = False
    for tf, m in _models.items():
        p, last = _predict(item["symbol"], tf, m, now)
        if p is None:
            continue
        got = True
        vals[f"p{tf}_down"], vals[f"p{tf}_flat"], vals[f"p{tf}_up"] = p
        if tf == 1:
            vals["bar1_dt"] = pd.Timestamp(last["datetime"]).isoformat()
            vals["close1"] = float(last["close"])
    if not got:
        return
    cols = ",".join(vals)
    conn.execute(f"INSERT INTO records ({cols}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
    conn.commit()
    _stats["written"] += 1


def _resolve(conn: sqlite3.Connection, now: dt.datetime) -> None:
    """bar1_dt + 15 分の 1 分足が確定した行に fwd_ret を埋める。"""
    from trading.summary.mtf_local_bars import get_bars

    cutoff = (now - dt.timedelta(minutes=HORIZON_MIN + 1)).isoformat()
    rows = conn.execute(
        "SELECT id, symbol, bar1_dt, close1 FROM records "
        "WHERE fwd_ret IS NULL AND bar1_dt IS NOT NULL AND bar1_dt <= ? LIMIT 300", (cutoff,)).fetchall()
    by_sym: dict[str, list] = {}
    for r in rows:
        by_sym.setdefault(r[1], []).append(r)
    for sym, rs in by_sym.items():
        bars = get_bars(sym, 1, now)
        if bars is None or bars.empty:
            continue
        done = bars[bars["completed"].astype(bool)]
        idx = {pd.Timestamp(t): float(c) for t, c in zip(done["datetime"], done["close"])}
        for rid, _, b_dt, c0 in rs:
            t = pd.Timestamp(b_dt) + pd.Timedelta(minutes=HORIZON_MIN)
            c1 = idx.get(t)
            if c1 is None or not c0:
                continue
            conn.execute("UPDATE records SET fwd_dt=?, fwd_close=?, fwd_ret=? WHERE id=?",
                         (t.isoformat(), c1, c1 / c0 - 1, rid))
            _stats["resolved"] += 1
    conn.commit()


def _worker() -> None:
    global _models
    try:
        _models = _load_models()
    except Exception:
        logger.exception("[MTF SHADOW] init failed")
        _models = {}
    if not _models:
        return
    day = dt.date.today()
    conn = _db(day)
    last_resolve = last_report = time.time()
    while True:
        try:
            try:
                item = _q.get(timeout=5)
            except queue.Empty:
                item = None
            if dt.date.today() != day:
                conn.close()
                day = dt.date.today()
                conn = _db(day)
            if item is not None:
                _handle(conn, item)
            t = time.time()
            if t - last_resolve >= 30:
                last_resolve = t
                _resolve(conn, dt.datetime.now())
            if t - last_report >= 300:
                last_report = t
                logger.info("[MTF SHADOW] stats %s qsize=%d", _stats, _q.qsize())
        except Exception:
            _stats["errors"] += 1
            if _stats["errors"] <= 20 or _stats["errors"] % 500 == 0:
                logger.warning("[MTF SHADOW] worker error (n=%d)", _stats["errors"], exc_info=True)
            time.sleep(1)
