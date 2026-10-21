# ============================================================
# File   : trading/entry/gapup_breakout.py
# Version: Ver1.0-GAPUP-BREAKOUT
# ------------------------------------------------------------
# ギャップアップ＋初動5分足高値ブレイク エントリー戦略（本体）
#
#  要件:
#    1. 寄り付きが前日終値より gap_pct(%) 以上 高い（ギャップアップ）
#    2. 9:00〜9:05 の最初の5分足が確定するまで待つ
#    3. その最初の5分足の高値を記録
#    4. 9:05以降、その高値をティック単位で上抜けた瞬間に成行で買いエントリー
#       （5分足の確定を待たず、tick で即発注）
#
#  実行経路:
#    PUSH の全 tick が通る trading/push/push_stream/ws_callbacks.on_message から
#    on_tick(row) が呼ばれる。銘柄別のステートマシンで判定し、ブレイク成立時に発火。
#
#  安全設計:
#    - 既定はドライラン（config.is_live()==False）。検知→ログ/Discord通知のみで発注しない。
#      GAPUP_BREAKOUT_LIVE=1 で初めて place_entry_buy（成行）を直呼びする。
#    - 1銘柄1日1回ガード（in-memory ＋ fired JSON）。tonosama/validated_signal と同じ流儀。
#    - ライブ時は既存建玉があればスキップ。発注直前の流動性ガードは place_entry_buy 内で担保。
#    - 例外は握りつぶしてログ（PUSH 本流を絶対に止めない）。
#
#  前日終値:
#    PUSH 行には無いので、ランキングの PreviousClose を使う
#    （RankingSnapshotManager シングルトン優先、無ければ rankingYYYYMMDD.db をフォールバック読み）。
#    取得できない銘柄は対象外（ギャップ判定不可）。
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from config.gapup_breakout_config import get_config, is_enabled, is_live

logger = logging.getLogger(__name__)

# 銘柄別ステート。on_tick は PUSH コールバックスレッドから呼ばれるのでロックで保護する。
_STATE: Dict[str, Dict[str, Any]] = {}
_STATE_LOCK = threading.Lock()


def enabled() -> bool:
    return is_enabled()


# ------------------------------------------------------------
# 値変換ヘルパ
# ------------------------------------------------------------
def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if f > 0 else None
    except Exception:
        return None


def _parse_dt(v: Any) -> Optional[dt.datetime]:
    """row の current_price_time（ISO 文字列 / datetime）を datetime へ。"""
    if isinstance(v, dt.datetime):
        return v
    if not v:
        return None
    s = str(v).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s[:26], fmt)
        except Exception:
            continue
    try:
        return dt.datetime.fromisoformat(s[:26])
    except Exception:
        return None


# ------------------------------------------------------------
# 前日終値ルックアップ（差し替え可能: テストで monkeypatch）
# ------------------------------------------------------------
def _prev_close_from_snapshot(symbol: str) -> Optional[float]:
    try:
        from trading.ranking.scheduler_core import _snapshot_mgr
        snap = _snapshot_mgr.get_latest(symbol)
        if snap:
            return _to_float(snap.get("prev_price"))
    except Exception:
        logger.debug("[GAPUP] snapshot prev_close lookup failed symbol=%s", symbol, exc_info=True)
    return None


def _prev_close_from_db(symbol: str) -> Optional[float]:
    """rankingYYYYMMDD.db の ranking_snapshot_1min から prev_price をフォールバック読み。"""
    try:
        import sqlite3
        from trading.ranking.active_symbols.db_ranking_fallback import _iter_ranking_db_paths
        sym = str(symbol).strip()
        for path in _iter_ranking_db_paths():
            if not path or not os.path.exists(path):
                continue
            try:
                c = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=1.0)
                try:
                    c.execute("PRAGMA busy_timeout=500;")
                    cur = c.execute(
                        "select prev_price from ranking_snapshot_1min "
                        "where symbol = ? and prev_price is not null "
                        "order by rowid desc limit 1",
                        (sym,),
                    )
                    r = cur.fetchone()
                    if r:
                        return _to_float(r[0])
                finally:
                    c.close()
            except Exception:
                continue
    except Exception:
        logger.debug("[GAPUP] db prev_close lookup failed symbol=%s", symbol, exc_info=True)
    return None


def lookup_prev_close(symbol: str) -> Optional[float]:
    """前日終値を取得。snapshot シングルトン優先、無ければ ranking DB。両方 None なら対象外。"""
    v = _prev_close_from_snapshot(symbol)
    if v is not None:
        return v
    return _prev_close_from_db(symbol)


# ------------------------------------------------------------
# 1日1回ガード（fired JSON）
# ------------------------------------------------------------
def _fired_path(day: str) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "autostock" / "runtime" / f"gapup_breakout_{day}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_fired(day: str) -> Dict[str, Any]:
    try:
        p = _fired_path(day)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[GAPUP] fired state read failed", exc_info=True)
    return {}


def _save_fired_symbol(day: str, symbol: str, rec: Dict[str, Any]) -> None:
    try:
        st = _load_fired(day)
        st[str(symbol)] = rec
        p = _fired_path(day)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[GAPUP] fired state write failed", exc_info=True)


def _already_fired_today(day: str, symbol: str) -> bool:
    try:
        return str(symbol) in _load_fired(day)
    except Exception:
        return False


# ------------------------------------------------------------
# 通知
# ------------------------------------------------------------
def _notify(msg: str) -> None:
    try:
        from utils.alerts_util import send_discord_notify
        send_discord_notify(msg)
    except Exception:
        logger.debug("[GAPUP] discord notify failed", exc_info=True)


# ------------------------------------------------------------
# ステート
# ------------------------------------------------------------
def _fresh_state(day: str) -> Dict[str, Any]:
    return {
        "day": day,
        "opening_price": None,
        "prev_close": None,
        "gap_ok": None,        # None=未判定 / True / False
        "first5m_high": None,
        "locked": False,
        "fired": False,
        "skip": False,
        "skip_reason": "",
    }


def reset_state() -> None:
    """テスト／日跨ぎ用に全銘柄ステートをクリアする。"""
    with _STATE_LOCK:
        _STATE.clear()


def get_state(symbol: str) -> Optional[Dict[str, Any]]:
    with _STATE_LOCK:
        st = _STATE.get(str(symbol))
        return dict(st) if st else None


def _skip(st: Dict[str, Any], reason: str) -> None:
    st["skip"] = True
    st["skip_reason"] = reason


# ------------------------------------------------------------
# 発火（発注 or ドライラン通知）
# ------------------------------------------------------------
def _fire(symbol: str, price: float, st: Dict[str, Any], cfg: Dict[str, Any], t: dt.datetime) -> None:
    day = st["day"]
    # 1日1回ガード（in-memory ＋ 永続）
    if st["fired"] or _already_fired_today(day, symbol):
        st["fired"] = True
        return

    high = st["first5m_high"]
    opening = st["opening_price"]
    prev = st["prev_close"]
    gap_pct_actual = ((opening - prev) / prev * 100.0) if (opening and prev) else 0.0
    live = bool(cfg["live"])

    base_msg = (
        f"📈 GAPUP BREAKOUT {symbol} "
        f"寄り={opening} 前日終値={prev} ギャップ=+{gap_pct_actual:.2f}% "
        f"初動5分高値={high} ブレイク価格={price} @ {t.strftime('%H:%M:%S')}"
    )

    if not live:
        # ドライラン：検知のみ。発注しない。
        logger.warning("[GAPUP DRYRUN] %s （LIVE=0 のため発注しません）", base_msg)
        _notify("[DRYRUN] " + base_msg)
        st["fired"] = True
        _save_fired_symbol(day, symbol, {
            "ts": t.strftime("%H:%M:%S"), "mode": "dryrun",
            "opening": opening, "prev_close": prev, "first5m_high": high,
            "break_price": price, "gap_pct": round(gap_pct_actual, 3),
        })
        return

    # ---- ライブ発注 ----
    # 既存建玉があれば二重エントリーを避ける。
    try:
        from global_state import global_data
        if global_data.get_position(symbol):
            logger.warning("[GAPUP LIVE SKIP] %s 既存建玉あり → スキップ", symbol)
            st["fired"] = True
            _save_fired_symbol(day, symbol, {"ts": t.strftime("%H:%M:%S"), "mode": "skip_has_position"})
            return
    except Exception:
        logger.debug("[GAPUP] position check failed symbol=%s", symbol, exc_info=True)

    order_id = None
    try:
        from trading.handlers.entry_handler import place_entry_buy
        # price=None / qty=None → place_entry_buy 内で参照価格・株数(予算)を自動補完。
        # 発注直前の流動性ガード（_final_recent_liquidity_ok）も内部で担保される。
        order_id = place_entry_buy(symbol, "", None, "GAPUP_BREAKOUT", order_type="MARKET")
    except Exception:
        logger.exception("[GAPUP LIVE] place_entry_buy failed symbol=%s", symbol)

    st["fired"] = True
    _save_fired_symbol(day, symbol, {
        "ts": t.strftime("%H:%M:%S"), "mode": "live",
        "order_id": order_id, "opening": opening, "prev_close": prev,
        "first5m_high": high, "break_price": price, "gap_pct": round(gap_pct_actual, 3),
    })
    if order_id:
        logger.warning("[GAPUP LIVE ENTRY] %s oid=%s", base_msg, order_id)
        _notify(f"🚀 {base_msg} → 成行発注 oid={order_id}")
    else:
        logger.error("[GAPUP LIVE ENTRY FAILED] %s（発注されず）", base_msg)


# ------------------------------------------------------------
# tick ハンドラ（PUSH コールバックから毎 tick 呼ばれる）
# ------------------------------------------------------------
def on_tick(row: dict) -> None:
    try:
        if not is_enabled() or not isinstance(row, dict):
            return

        symbol = row.get("symbol") or row.get("Symbol") or row.get("code")
        if not symbol:
            return
        symbol = str(symbol)

        price = _to_float(row.get("current_price"))
        if price is None:
            return

        t = _parse_dt(row.get("current_price_time") or row.get("datetime"))
        if t is None:
            return

        cfg = get_config()
        day = t.date().isoformat()
        tod = t.time()

        # 寄り前は無視（最初の5分足の窓より前）
        if tod < cfg["window_start"]:
            return

        with _STATE_LOCK:
            st = _STATE.get(symbol)
            if st is None or st["day"] != day:
                st = _fresh_state(day)
                _STATE[symbol] = st

            if st["skip"] or st["fired"]:
                return

            # 寄り値（公式 OpeningPrice）を確定
            if st["opening_price"] is None:
                st["opening_price"] = _to_float(row.get("opening_price"))

            # 前日終値（ランキング）を取得（取れるまで毎 tick 試行）
            if st["prev_close"] is None:
                st["prev_close"] = lookup_prev_close(symbol)

            # ギャップアップ判定（寄り値と前日終値が揃い次第）
            if st["gap_ok"] is None and st["opening_price"] and st["prev_close"]:
                thr = st["prev_close"] * (1.0 + cfg["gap_pct"] / 100.0)
                st["gap_ok"] = bool(st["opening_price"] >= thr)
                if not st["gap_ok"]:
                    _skip(st, "GAP_NOT_MET")
                    return

            # 最初の5分足 [window_start, window_lock) の高値を集計
            if cfg["window_start"] <= tod < cfg["window_lock"]:
                cand = max(price, st["opening_price"] or 0.0)
                st["first5m_high"] = cand if st["first5m_high"] is None else max(st["first5m_high"], cand)
                return  # 窓の中ではまだブレイク判定しない

            # ここから tod >= window_lock（初動5分足は確定済み）
            if not st["locked"]:
                # 確定時点の最終判定
                if st["gap_ok"] is None:
                    _skip(st, "NO_PREV_CLOSE_OR_OPEN")  # 前日終値/寄り値が取れずギャップ判定不能
                    return
                if not st["gap_ok"]:
                    _skip(st, "GAP_NOT_MET")
                    return
                if st["first5m_high"] is None:
                    _skip(st, "NO_FIRST_BAR")  # 窓内に tick が無かった
                    return
                st["locked"] = True
                logger.info(
                    "[GAPUP ARMED] %s 寄り=%s 前日終値=%s 初動5分高値=%s（ブレイク待ち）",
                    symbol, st["opening_price"], st["prev_close"], st["first5m_high"],
                )

            # 打ち切り時刻を過ぎたら監視終了
            if tod >= cfg["entry_cutoff"]:
                _skip(st, "CUTOFF")
                return

            # ティック単位の厳密上抜けで発火
            if st["locked"] and not st["fired"] and price > st["first5m_high"]:
                _fire(symbol, price, st, cfg, t)

    except Exception:
        logger.exception("[GAPUP] on_tick failed")
