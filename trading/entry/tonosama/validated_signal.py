# ============================================================
# File   : trading/entry/tonosama/validated_signal.py
# 2026-10-13: 検証どおりの殿様イナゴ (出来高急増の買い) を出す。
# ------------------------------------------------------------
# 旧来の runner は出来高に「当日の累計」を 1 本分として使っていた (倍率の中央値 114 倍、
# 最大 5,331 倍) うえ、AI ゲート・3m/5m ゲートなど検証に無い条件で全部落ちていた
# (10/1〜10/13 の発注 0 件)。ここでは backtest で +0.33% (n=66, 8/17〜9/28, 往復 0.1% 控除後、
# 損切り 1.5% + 60 分) だった定義をそのまま実装する:
#   - PUSH の 1 分足 (累計出来高の差分)。前の足と 1 分ずつ連続している足だけ有効。
#   - 3 分足 / 5 分足 (時計の区切り)。出来高 / 直前 5 本平均 (3 本以上) >= 10 倍。
#   - 足の騰落 (直前 1 分足の終値から) が +0.2〜+1.2%。
#   - 足の形: 終値位置 <= 90%、上ヒゲ <= 45%。
#   - 足の終わり時刻が 09:05〜11:30 / 12:40〜14:30。
#   - 直近 1 分足の売買代金 >= 2,000 万円。株価 >= TONOSAMA_MIN_PRICE。
#   - 1 銘柄 1 日 1 回 (最初の 1 回)。買いのみ。
# 出た候補は pending に source=TONOSAMA / tonosama_validated=True で入れる。
# entry_controller はこの印のある候補だけ、検証に無い汎用ゲートを通さない
# (日次リスク・時間帯・板・発注直前の当日騰落/スプレッド/板ガードは残す)。
# 決済は trading.strategies.tonosama_exit (損切り 1.5% / 60 分)。
# 止め方: TONOSAMA_VALIDATED_SIGNAL=0 (旧 runner に戻る)。ログの目印: [TONOSAMA VALIDATED]
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {"minute": None}


def _env_f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def enabled() -> bool:
    return str(os.getenv("TONOSAMA_VALIDATED_SIGNAL", "1")).strip().lower() not in ("0", "false", "no", "off")


def _fired_path(day: str) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "autostock" / "runtime" / f"tonosama_signal_{day}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_fired(day: str) -> Dict[str, Any]:
    try:
        p = _fired_path(day)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[TONOSAMA VALIDATED] fired state read failed", exc_info=True)
    return {}


def _save_fired(day: str, st: Dict[str, Any]) -> None:
    p = _fired_path(day)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[TONOSAMA VALIDATED] fired state write failed", exc_info=True)


def _push_db_path() -> str:
    from trading.entry.tonosama.history_missing_guard import _push_db_path as _p
    return _p()


def load_ticks(path: str, since: dt.datetime, until: dt.datetime) -> pd.DataFrame:
    """[since, until) の PUSH ティック (symbol, ts, price, volume=当日累計)。"""
    if not Path(path).exists():
        return pd.DataFrame()
    c = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=1.0)
    try:
        c.execute("PRAGMA busy_timeout=500;")
        t = pd.read_sql(
            "select symbol, substr(datetime,1,19) dt, price, volume from stream_data "
            "where datetime >= ? and datetime < ? and price > 0",
            c, params=(since.strftime("%Y-%m-%dT%H:%M:%S"), until.strftime("%Y-%m-%dT%H:%M:%S")),
        )
    finally:
        c.close()
    if t.empty:
        return t
    t["ts"] = pd.to_datetime(t["dt"], errors="coerce")
    return t.dropna(subset=["ts"])


def build_m1(t: pd.DataFrame) -> pd.DataFrame:
    """1 分足 (終わり時刻ラベル)。volume は累計の差分、前の足と連続していない足は無効。"""
    if t is None or t.empty:
        return pd.DataFrame(columns=["symbol", "t", "close", "high", "low", "volume", "valid"])
    hm = t.ts.dt.hour * 60 + t.ts.dt.minute
    t = t[((hm >= 540) & (hm < 690)) | ((hm >= 750) & (hm < 930))].sort_values(["symbol", "ts"]).copy()
    t["t"] = t.ts.dt.floor("min") + pd.Timedelta(minutes=1)
    m = t.groupby(["symbol", "t"]).agg(close=("price", "last"), high=("price", "max"), low=("price", "min"),
                                       cum=("volume", "max")).reset_index()
    m["prev_t"] = m.groupby("symbol").t.shift(1)
    m["prev_cum"] = m.groupby("symbol").cum.shift(1)
    m["volume"] = m.cum - m.prev_cum
    gap = (m.t - m.prev_t) != pd.Timedelta(minutes=1)
    lunch = m.t.dt.strftime("%H:%M") == "12:31"
    m["valid"] = ~gap & m.volume.notna() & (m.volume >= 0) & ~lunch
    m["volume"] = m.volume.where(m["valid"], 0)
    return m[["symbol", "t", "close", "high", "low", "volume", "valid"]]


def _mk_bars(m1: pd.DataFrame, n: int) -> pd.DataFrame:
    x = m1.copy()
    mins = x.t.dt.hour * 60 + x.t.dt.minute
    end = np.ceil(mins / n) * n
    x["bend"] = x.t.dt.normalize() + pd.to_timedelta(end, unit="m")
    x["prevc"] = x.groupby("symbol").close.shift(1)
    g = x.groupby(["symbol", "bend"], sort=True)
    b = g.agg(close=("close", "last"), high=("high", "max"), low=("low", "min"), volume=("volume", "sum"),
              open=("prevc", "first"), cnt=("close", "size"), valid=("valid", "all")).reset_index()
    b = b[b.cnt == n] if n > 1 else b
    b = b.sort_values(["symbol", "bend"])
    vv = b.volume.where(b.valid)
    b["avg"] = vv.groupby(b.symbol).transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    b["ratio"] = (b.volume / b.avg.replace(0, np.nan)).where(b.valid)
    b["pchg"] = (b.close / b.open - 1) * 100
    rng = (b.high - b.low).replace(0, np.nan)
    b["cpos"] = ((b.close - b.low) / rng * 100).fillna(50)
    b["uwick"] = ((b.high - b.close) / rng * 100).fillna(0)
    b["tf"] = n
    return b


def signals(m1: pd.DataFrame) -> pd.DataFrame:
    """検証 (%TEMP%\\cw\\tonosama\\common.py) と同じ定義。しきい値は env で読む。"""
    if m1 is None or m1.empty:
        return pd.DataFrame()
    ratio_min = _env_f("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 10.0)
    turn_min = _env_f("TONOSAMA_MIN_LATEST_TURNOVER", 20_000_000.0)
    price_min = _env_f("TONOSAMA_MIN_PRICE", 300.0)
    s = pd.concat([_mk_bars(m1, 3), _mk_bars(m1, 5)])
    s = s[(s.ratio >= ratio_min) & (s.pchg >= 0.2) & (s.pchg <= 1.2)]
    s = s[(s.cpos <= 90) & (s.uwick <= 45) & (s.close >= price_min)]
    s = s.rename(columns={"bend": "t"})
    hm = s.t.dt.hour * 100 + s.t.dt.minute
    s = s[((hm >= 905) & (hm <= 1130)) | ((hm >= 1240) & (hm <= 1430))]
    last = m1[["symbol", "t", "close", "volume"]].rename(columns={"close": "c1", "volume": "v1"})
    s = s.merge(last, on=["symbol", "t"], how="left")
    s["turn1"] = s.c1 * s.v1
    s = s[s.turn1 >= turn_min]
    s = s.sort_values("ratio", ascending=False).drop_duplicates(["symbol", "t"]).sort_values(["t", "symbol"])
    return s


def _pending_entry(r: Any, now: dt.datetime) -> Dict[str, Any]:
    expire_at = now + dt.timedelta(seconds=_env_f("TONOSAMA_VALIDATED_EXPIRE_SEC", 60.0))
    reason = (f"TONOSAMA_VALIDATED {int(r.tf)}m ratio={r.ratio:.1f} chg={r.pchg:+.2f}% "
              f"turn1={r.turn1 / 1e6:.0f}M bar={pd.Timestamp(r.t):%H:%M}")
    cond = {"expire_at": expire_at, "decision_at": now, "feature_dt": f"{pd.Timestamp(r.t):%Y-%m-%d %H:%M:%S}",
            "reason": reason, "reason_code": "tonosama_validated", "side": "BUY",
            "max_volume_surge_ratio": float(r.ratio), "max_price_change_pct": float(r.pchg),
            "close_position_pct": float(r.cpos), "upper_wick_pct": float(r.uwick),
            "latest_volume": float(r.v1), "surge_tf": f"{int(r.tf)}m"}
    return {
        "symbol": str(r.symbol), "symbolname": "", "side": "BUY", "source": "TONOSAMA", "entry_type": "TONOSAMA",
        "price": float(r.close), "raw_score": float(r.ratio), "final_score": float(r.ratio),
        "display_score": float(r.ratio), "score": float(r.ratio), "score_buy": float(r.ratio), "score_sell": 0.0,
        "ai_prob": 1.0, "expire_at": expire_at, "entry_conditions": cond, "created_at": now,
        "tonosama_validated": True, "tonosama_validated_reason": reason,
    }


def run_once(now: Optional[dt.datetime] = None, *, path: Optional[str] = None, add=None) -> int:
    """新しく確定した 3m/5m 足に検証どおりの急増があれば pending に入れる。入れた件数を返す。"""
    now = now or dt.datetime.now()
    cur_min = now.replace(second=0, microsecond=0)
    with _LOCK:
        if _STATE.get("minute") == cur_min:
            return 0
        _STATE["minute"] = cur_min
    started = time.perf_counter()
    try:
        path = path or _push_db_path()
        # 直前 5 本平均には前場の足も入る (検証は 1 日分を通しで計算)。後場の始めは昼休みの 60 分を足して遡る。
        since = cur_min - dt.timedelta(minutes=40)
        if dt.time(12, 30) <= cur_min.time() <= dt.time(13, 15):
            since -= dt.timedelta(minutes=60)
        t = load_ticks(path, since, cur_min)
        m1 = build_m1(t)
        s = signals(m1)
        # 今確定した足 (終わり時刻 = 今の分) と、取りこぼし用に 1 分前まで
        if not s.empty:
            s = s[(s.t <= pd.Timestamp(cur_min)) & (s.t >= pd.Timestamp(cur_min - dt.timedelta(minutes=1)))]
        day = now.strftime("%Y%m%d")
        n_added = 0
        if not s.empty:
            if add is None:
                from trading.entry.pending_manager import add_pending as add
            with _LOCK:
                fired = _load_fired(day)
                for r in s.itertuples():
                    if str(r.symbol) in fired:
                        continue
                    entry = _pending_entry(r, now)
                    ok = bool(add(entry))
                    fired[str(r.symbol)] = {"ts": now.strftime("%H:%M:%S"), "added": ok,
                                            "reason": entry["tonosama_validated_reason"]}
                    n_added += int(ok)
                    logger.warning("[TONOSAMA VALIDATED] signal symbol=%s added=%s %s", r.symbol, ok,
                                   entry["tonosama_validated_reason"])
                _save_fired(day, fired)
        logger.info("[TONOSAMA VALIDATED] scan minute=%s ticks=%s symbols=%s signals=%s added=%s elapsed=%.3fs",
                    cur_min.strftime("%H:%M"), len(t), m1.symbol.nunique() if not m1.empty else 0,
                    len(s), n_added, time.perf_counter() - started)
        return n_added
    except Exception:
        logger.exception("[TONOSAMA VALIDATED] scan failed")
        return 0


def is_validated_entry(entry: Any, entry_row: Any = None) -> bool:
    for d in (entry, entry_row):
        if isinstance(d, dict) and d.get("tonosama_validated"):
            return True
    return False


def bypass_enabled() -> bool:
    return str(os.getenv("TONOSAMA_BYPASS_GENERIC_GATES", "1")).strip().lower() not in ("0", "false", "no", "off")


__all__ = ["run_once", "signals", "build_m1", "load_ticks", "is_validated_entry", "bypass_enabled", "enabled"]
