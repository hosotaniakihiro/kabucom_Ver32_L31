# ============================================================
# pj/trading/exit/exit_handler.py
# Ver26.0-PRO-EXIT-SCHEDULER-GC-POSITIONS-NORMAL-EXIT
# Updated: 2026-05-12
# ------------------------------------------------------------
# ✔ 通常 EXIT ロジック完全温存方針
# ✔ ranking + TONOSAMA は即逃げ専用
# ✔ 初動成行 → トレーリング即逃げ
# ✔ 学習ログ連携（存在チェック付き）
# ✔ push 欠損・属性欠損完全耐性
# ✔ EXIT 経路を一本化（事故防止）
# ✔ EXIT加速（含み益 × 時間 × 3m/5m）統合
# ✔ GC.positions.open_positions も EXIT 対象に追加
# ✔ 通常 SUMMARY_AI / RANKING / EARLY_SCALP 用の最低限 EXIT 判定を有効化
# ✔ Session_trade 未定義バグを解消し Session_position に統一
# ✔ EXIT pipeline heartbeat / scan ログ追加
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import os
import sys
from types import SimpleNamespace
from typing import Any, Iterable

import numpy as np

# ============================================================
# numba は「あれば速い」だけの任意依存にする
# ------------------------------------------------------------
# 2026-09-02 08:42:55 の起動失敗:
#   from numba import njit
#     -> ImportError: DLL load failed while importing _devicearray:
#        アプリケーション制御ポリシーによってこのファイルがブロックされました。
#   sitecustomize 経由の先行 import でも同じ例外が出たあと、
#   main.py の top-level import で未処理になり終了コード 1 で落ちた。
#
# このモジュールで numba を使うのは calc_ohlc(prices) 1 箇所だけで、
# 中身は prices[0] / max / min / prices[-1] を返すだけ。
# JIT が無くても結果は同じなので、import できない環境では
# 同じ decorator の書き方を保てる identity decorator に落とす。
# EXIT / 発注 / 取消 / 返済のロジックは一切変えない。
#
# 判定と fallback の実体は core/compat/numba_compat.py が 1 箇所で持つ。
# ここでは従来の名前 (njit / NUMBA_AVAILABLE) を再エクスポートするだけにして、
# 「numba をどう扱うか」の分岐がプロジェクト内で二重にならないようにする。
try:
    from core.compat.numba_compat import (  # noqa: F401
        NUMBA_AVAILABLE,
        NUMBA_IMPORT_ERROR as _NUMBA_IMPORT_ERROR,
        njit,
    )
except Exception as _numba_compat_exc:  # compat 層自体が読めない最悪ケースの保険
    _NUMBA_IMPORT_ERROR = f"numba_compat unavailable: {type(_numba_compat_exc).__name__}: {_numba_compat_exc}"
    NUMBA_AVAILABLE = False

    def njit(*args, **kwargs):  # type: ignore[misc]
        """identity decorator (`@njit` / `@njit(...)` の両方に対応)。"""
        if len(args) == 1 and not kwargs and callable(args[0]):
            return args[0]

        def _identity(func):
            return func

        return _identity


from global_state import global_data
from database import Session_position
from database.models import Position, TradeHistory
from kabu_api.close import process_exit

try:
    from trading.entry.tonosama_logger import log_tonosama_trade
except Exception:
    log_tonosama_trade = None

logger = logging.getLogger("exit_ultra")

if not NUMBA_AVAILABLE:
    # import 時に 1 回だけ。traceback は出さない (起動ログを潰さないため)。
    # モジュールは sys.modules にキャッシュされるので、
    # sitecustomize 経由と main.py 経由で二度 numba を試すことはない。
    # 失敗理由そのものは core.compat.numba_compat が [NUMBA COMPAT] で
    # 1 回だけ出しているので、ここでは EXIT 側への影響だけを書く。
    logger.warning(
        "[EXIT HANDLER] JIT なしで動作します "
        "(calc_ohlc は通常の Python 関数として実行。EXIT 判定・発注・取消・返済のロジックは不変)"
    )

# ============================================================
# TONOSAMA フォールバックパラメータ（最後の保険）
# ============================================================
TONO_STOP_LOSS = -0.002      # -0.20%
TONO_TAKE_PROFIT = 0.003    # +0.30%
TONO_TRAIL_GAP = 0.0015     # -0.15%
TONO_MAX_HOLD_SEC = 90      # AI 未指定時の最大保持秒数


def _tonosama_exit_infer_env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _tonosama_exit_infer_scope_enabled() -> bool:
    """tonosama_exit_source_infer_patch (native化) の旧scope判定を再現する。

    旧パッチは core/startup/runtime_env/installer.py の
    TRADING_CONTEXTS={"main"} 経由でのみ install() されていた
    (main.py プロセス以外では常に無効)。
    """
    try:
        if str(os.environ.get("DISABLE_TONOSAMA_EXIT_SOURCE_INFER_PATCH", "")).strip() == "1":
            return False
        raw = os.getenv("TONOSAMA_EXIT_SOURCE_INFER_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
        argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(a.endswith("/main.py") or a == "main.py" for a in argv_list)
    except Exception:
        return False


if _tonosama_exit_infer_scope_enabled():
    # Scalp-oriented TONOSAMA defaults (native化, 元 tonosama_exit_source_infer_patch.py)。
    # 既存の env 値があれば優先する。DISABLE_TONOSAMA_EXIT_SOURCE_INFER_PATCH=1 の場合や
    # main.py プロセス以外では、このファイル本来のデフォルト値のまま変更しない。
    TONO_STOP_LOSS = -abs(_tonosama_exit_infer_env_float("TONOSAMA_EXIT_STOP_LOSS_PCT", 0.0030))       # -0.30%
    TONO_TAKE_PROFIT = abs(_tonosama_exit_infer_env_float("TONOSAMA_EXIT_TAKE_PROFIT_PCT", 0.0020))   # +0.20%
    TONO_TRAIL_GAP = abs(_tonosama_exit_infer_env_float("TONOSAMA_EXIT_TRAIL_GAP_PCT", 0.0020))       # high/low from best -0.20%
    TONO_MAX_HOLD_SEC = int(_tonosama_exit_infer_env_float("TONOSAMA_EXIT_MAX_HOLD_SEC", 60.0))

# ============================================================
# 通常 EXIT フォールバックパラメータ
# ============================================================
NORMAL_STOP_LOSS = -0.006     # -0.60%
NORMAL_TAKE_PROFIT = 0.008    # +0.80%
NORMAL_TRAIL_START = 0.006    # +0.60% からトレーリング開始
NORMAL_TRAIL_GAP = 0.003      # 高値/安値から -0.30%
NORMAL_MAX_HOLD_SEC = 300     # 5分

_EXIT_HEARTBEAT_EVERY_SEC = 10
_last_exit_heartbeat_at: dt.datetime | None = None


# ============================================================
# util
# ============================================================

def safe_float(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _safe_int(x, default=0):
    try:
        if x is None or x == "":
            return default
        return int(float(x))
    except Exception:
        return default


def _get_core(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


# --- tonosama_exit_source_infer_patch (native化, 元 core/startup/tonosama_exit_source_infer_patch.py) ---
# Position ORM モデルに entry_source/entry_mode 列が無く、DB建玉が TONOSAMA (殿様イナゴ)
# 由来であっても遅いNORMAL exitへ落ちてしまう問題への対処。既存のexit pipelineは変えず、
# _get() だけに reason/source/history からTONOSAMAメタデータを推測するfallbackを持たせる。
_TONOSAMA_EXIT_INFER_HINTS = (
    "tonosama",
    "殿様",
    "イナゴ",
    "出来高急増",
    "volume_surge",
    "volume surge",
    "tonosama_entry",
    "tonosama entry",
)


def _tonosama_exit_infer_text(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v)
    except Exception:
        return ""


def _tonosama_exit_infer_iter_history_text(pos: Any) -> list[str]:
    texts: list[str] = []
    try:
        histories = _get_core(pos, "histories", None)
        if histories:
            for h in list(histories)[-5:]:
                for name in ("reason", "action", "side", "symbolname"):
                    texts.append(_tonosama_exit_infer_text(_get_core(h, name, "")))
    except Exception:
        pass
    return texts


def _tonosama_exit_infer_looks_tonosama_position(pos: Any) -> bool:
    texts: list[str] = []
    for name in (
        "entry_source",
        "source",
        "entry_mode",
        "entry_type",
        "trigger_type",
        "reason",
        "entry_reason",
        "entry_comment",
        "source_reason",
        "mode",
    ):
        texts.append(_tonosama_exit_infer_text(_get_core(pos, name, "")))
    texts.extend(_tonosama_exit_infer_iter_history_text(pos))

    joined = " ".join(t for t in texts if t).lower()
    if not joined:
        return False
    return any(h.lower() in joined for h in _TONOSAMA_EXIT_INFER_HINTS)


def _get(obj: Any, name: str, default=None):
    if not _tonosama_exit_infer_scope_enabled():
        return _get_core(obj, name, default)

    # First keep the original behavior 100% intact.
    v = _get_core(obj, name, default)
    if v not in (None, ""):
        return v

    # Only synthesize TONOSAMA metadata for fields used by run_exit_pipeline.
    try:
        lname = str(name or "").lower()
        if lname in {"entry_source", "source"} and _tonosama_exit_infer_looks_tonosama_position(obj):
            return "ranking"
        if lname in {"entry_mode", "entry_type", "trigger_type"} and _tonosama_exit_infer_looks_tonosama_position(obj):
            return "TONOSAMA"
        if lname == "hold_limit_sec" and _tonosama_exit_infer_looks_tonosama_position(obj):
            return _tonosama_exit_infer_env_float("TONOSAMA_EXIT_MAX_HOLD_SEC", 60.0)
    except Exception:
        pass

    return default


def _set(obj: Any, name: str, value: Any) -> None:
    try:
        if isinstance(obj, dict):
            obj[name] = value
        else:
            setattr(obj, name, value)
    except Exception:
        pass


def _is_buy_side(side: Any) -> bool:
    return str(side or "").upper().startswith("BUY")


def _is_sell_side(side: Any) -> bool:
    s = str(side or "").upper()
    return s.startswith("SELL") or s.startswith("SHORT")


def _is_dict_position(pos: Any) -> bool:
    return isinstance(pos, dict)


def _as_position_object(pos: Any):
    """
    process_exit は Position ORM 風の属性アクセスを想定する可能性があるため、
    dict の場合は SimpleNamespace に変換して渡す。
    """
    if isinstance(pos, dict):
        return SimpleNamespace(**pos)
    return pos


def _position_symbol(pos: Any) -> str:
    return str(
        _get(pos, "symbol")
        or _get(pos, "Symbol")
        or _get(pos, "stock_code")
        or ""
    ).strip()


def _position_side(pos: Any) -> str:
    return str(_get(pos, "side") or _get(pos, "Side") or "BUY").upper()


def _position_qty(pos: Any) -> int:
    return _safe_int(_get(pos, "qty") or _get(pos, "quantity") or _get(pos, "Qty"), 0)


def _position_entry_price(pos: Any) -> float:
    return safe_float(
        _get(pos, "avg_price")
        or _get(pos, "entry_price")
        or _get(pos, "price")
        or _get(pos, "current_price"),
        0.0,
    )


def _position_entry_time(pos: Any) -> dt.datetime:
    v = _get(pos, "entry_time")
    if isinstance(v, dt.datetime):
        return v
    try:
        if v:
            return dt.datetime.fromisoformat(str(v))
    except Exception:
        pass
    return dt.datetime.now()


def _pnl_rate(pos: Any, price: float) -> float:
    entry_price = _position_entry_price(pos)
    if entry_price <= 0:
        return 0.0

    rate = (price - entry_price) / entry_price
    if _is_sell_side(_position_side(pos)):
        rate = -rate
    return rate


def _heartbeat(msg: str, *args):
    global _last_exit_heartbeat_at
    now = dt.datetime.now()
    if _last_exit_heartbeat_at is None or (now - _last_exit_heartbeat_at).total_seconds() >= _EXIT_HEARTBEAT_EVERY_SEC:
        logger.info(msg, *args)
        _last_exit_heartbeat_at = now


# ============================================================
# 上位足方向判定（3分 / 5分）
# ============================================================

def judge_direction_from_summary(row, side):
    """
    return: ALIGN / AGAINST / FLAT
    """
    if not row:
        return "FLAT"

    close = safe_float(row.get("close_price") or row.get("close"))
    ma25 = safe_float(row.get("ma25"))
    ma75 = safe_float(row.get("ma75"))

    if close > ma25 > ma75:
        trend = "UP"
    elif close < ma25 < ma75:
        trend = "DOWN"
    else:
        return "FLAT"

    if str(side).upper().startswith("BUY") and trend == "UP":
        return "ALIGN"
    if str(side).upper().startswith("SELL") and trend == "DOWN":
        return "ALIGN"

    return "AGAINST"


# ============================================================
# TONOSAMA EXIT 加速係数
# ============================================================

def calc_exit_accel_for_tonosama(pos: Position, price: float, now: dt.datetime) -> float:
    """
    含み益 × 経過時間 × 上位足環境 から EXIT 加速係数を算出
    """

    symbol = _position_symbol(pos)
    side = _position_side(pos)

    # ---------- 上位足 ----------
    summary_by_interval = getattr(global_data, "summary_by_interval", {}) or {}
    s3 = summary_by_interval.get("3min", {}).get(symbol)
    s5 = summary_by_interval.get("5min", {}).get(symbol)

    env3 = judge_direction_from_summary(s3, side)
    env5 = judge_direction_from_summary(s5, side)

    if env3 == env5 == "ALIGN":
        base = 0.8
    elif "AGAINST" in (env3, env5):
        base = 1.8
    else:
        base = 1.0

    # ---------- 含み益 ----------
    pnl_rate = _pnl_rate(pos, price)

    if pnl_rate <= 0:
        pnl_factor = 1.3
    elif pnl_rate < TONO_TAKE_PROFIT * 0.3:
        pnl_factor = 1.0
    else:
        pnl_factor = 0.6

    # ---------- 経過時間 ----------
    entry_time = _position_entry_time(pos)
    hold_sec = (now - entry_time).total_seconds()
    if hold_sec < 30:
        time_factor = 1.0
    elif hold_sec < 90:
        time_factor = 1.1
    else:
        time_factor = 1.4

    accel = base * pnl_factor * time_factor
    accel = max(0.6, min(accel, 2.5))

    return accel


# ============================================================
# 高速 5 秒足（PUSH 依存・安全）
# ============================================================
@njit
def calc_ohlc(prices):
    return prices[0], prices.max(), prices.min(), prices[-1]


def build_5s_bar_fast(symbol):
    if not hasattr(global_data, "get_push_df"):
        return None

    df = global_data.get_push_df()
    if df is None or df.empty:
        return None

    try:
        sym = str(symbol)
        if "symbol" not in df.columns:
            return None
        d = df[df["symbol"].astype(str) == sym]
    except Exception:
        return None

    if d.empty:
        return None

    try:
        d = d.copy()
        time_col = "time" if "time" in d.columns else "datetime" if "datetime" in d.columns else None
        if not time_col:
            return None

        d[time_col] = d[time_col].apply(
            lambda x: x if isinstance(x, dt.datetime)
            else dt.datetime.fromisoformat(str(x))
        )
    except Exception:
        return None

    last_dt = d[time_col].iloc[-1]
    w = d[d[time_col] >= last_dt - dt.timedelta(seconds=5)]
    if w.empty:
        return None

    price_col = "price" if "price" in w.columns else "current_price" if "current_price" in w.columns else "close" if "close" in w.columns else None
    if not price_col:
        return None

    prices = w[price_col].astype(float).to_numpy()
    if len(prices) <= 0:
        return None

    vol = float(w["volume"].sum()) if "volume" in w.columns else 0.0

    o, h, l, c = calc_ohlc(prices)
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": vol,
        "dt": last_dt,
    }


# ============================================================
# TONOSAMA EXIT 判定（唯一）
# ============================================================
def _check_tonosama_exit_core(pos: Position, price: float, now: dt.datetime):
    accel = calc_exit_accel_for_tonosama(pos, price, now)

    entry_price = _position_entry_price(pos)
    if entry_price <= 0:
        return None

    pnl_rate = _pnl_rate(pos, price)
    hold_sec = (now - _position_entry_time(pos)).total_seconds()

    # --------------------------------------------
    # 高値更新（属性欠損耐性）
    # --------------------------------------------
    high_since_entry = _get(pos, "high_since_entry")
    if high_since_entry is None:
        _set(pos, "high_since_entry", price)
        high_since_entry = price
    else:
        high_since_entry = max(safe_float(high_since_entry, price), price)
        _set(pos, "high_since_entry", high_since_entry)

    max_profit = (high_since_entry - entry_price) / entry_price
    if _is_sell_side(_position_side(pos)):
        # SELL は安値方向が利益。既存属性名を維持しつつ、pnl_rateベースで扱う。
        max_profit = max(safe_float(_get(pos, "max_profit_rate"), 0.0), pnl_rate)
        _set(pos, "max_profit_rate", max_profit)

    # --------------------------------------------
    # ① 即損切（加速）
    # --------------------------------------------
    if pnl_rate <= TONO_STOP_LOSS * accel:
        return "TONOSAMA_STOP"

    # --------------------------------------------
    # ② 利確後トレーリング（加速）
    # --------------------------------------------
    if max_profit >= TONO_TAKE_PROFIT:
        if pnl_rate <= max_profit - (TONO_TRAIL_GAP * accel):
            return "TONOSAMA_TRAIL"

    # --------------------------------------------
    # ③ 保持時間（短縮）
    # --------------------------------------------
    hold_limit = _get(pos, "hold_limit_sec")
    if hold_limit is None:
        hold_limit = TONO_MAX_HOLD_SEC

    if hold_sec >= safe_float(hold_limit, TONO_MAX_HOLD_SEC) / accel:
        return "TONOSAMA_TIMEOUT"

    return None


# ============================================================
# 通常 EXIT 判定
# ============================================================
def _check_normal_exit_core(pos: Any, price: float, now: dt.datetime):
    entry_price = _position_entry_price(pos)
    if entry_price <= 0 or price <= 0:
        return None

    pnl_rate = _pnl_rate(pos, price)
    hold_sec = (now - _position_entry_time(pos)).total_seconds()

    symbol = _position_symbol(pos)
    side = _position_side(pos)

    # 損切り
    if pnl_rate <= NORMAL_STOP_LOSS:
        return "NORMAL_STOP"

    # 利確
    if pnl_rate >= NORMAL_TAKE_PROFIT:
        return "NORMAL_TAKE_PROFIT"

    # トレーリング
    max_profit_rate = safe_float(_get(pos, "max_profit_rate"), 0.0)
    max_profit_rate = max(max_profit_rate, pnl_rate)
    _set(pos, "max_profit_rate", max_profit_rate)

    if max_profit_rate >= NORMAL_TRAIL_START and pnl_rate <= max_profit_rate - NORMAL_TRAIL_GAP:
        return "NORMAL_TRAIL"

    # 時間切れ
    if hold_sec >= NORMAL_MAX_HOLD_SEC:
        return "NORMAL_TIMEOUT"

    logger.debug(
        "[EXIT HOLD] symbol=%s side=%s price=%s entry=%s pnl_rate=%.4f hold_sec=%.1f max_profit=%.4f",
        symbol,
        side,
        price,
        entry_price,
        pnl_rate,
        hold_sec,
        max_profit_rate,
    )

    return None


# ============================================================
# EXIT 対象収集
# ============================================================
def _load_db_open_positions(sp) -> list[Any]:
    try:
        return list(sp.query(Position).filter_by(status="OPEN").all())
    except Exception:
        logger.exception("[EXIT] DB open positions load failed")
        return []


def _load_gc_open_positions() -> list[Any]:
    try:
        snap = {}
        try:
            from core.global_context.context import global_context as GC
            if hasattr(GC.positions, "snapshot_open"):
                snap = GC.positions.snapshot_open()
            elif hasattr(GC.positions, "snapshot_dict"):
                snap = GC.positions.snapshot_dict()
        except Exception:
            snap = getattr(global_data, "open_positions", {}) or {}

        if isinstance(snap, dict):
            return [dict(v) for v in snap.values() if isinstance(v, dict)]
        if isinstance(snap, list):
            return snap
        return []

    except Exception:
        logger.exception("[EXIT] GC open positions load failed")
        return []


def _merge_positions(db_positions: list[Any], gc_positions: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()

    for pos in db_positions + gc_positions:
        sym = _position_symbol(pos)
        if not sym:
            continue
        key = f"{sym}:{_position_side(pos)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(pos)

    return out


def _remove_open_position(symbol: str) -> None:
    try:
        from core.global_context.context import global_context as GC
        if hasattr(GC.positions, "remove"):
            GC.positions.remove(str(symbol))
    except Exception:
        pass

    try:
        op = getattr(global_data, "open_positions", None)
        if isinstance(op, dict):
            op.pop(str(symbol), None)
    except Exception:
        pass


# ============================================================
# EXIT 実行（共通）
# ============================================================
def execute_exit(pos: Position, exit_price: float, reason: str):
    symbol = _position_symbol(pos)
    side = _position_side(pos)
    qty = _position_qty(pos)
    avg_price = _position_entry_price(pos)

    logger.warning("🔥 EXIT %s side=%s qty=%s price=%s reason=%s", symbol, side, qty, exit_price, reason)

    api = process_exit(_as_position_object(pos), exit_price, reason)
    if not api or not api.get("order_id"):
        logger.error("❌ EXIT 注文失敗 symbol=%s reason=%s api=%s", symbol, reason, api)
        return False

    exec_price = safe_float(api.get("exec_price", exit_price), exit_price)

    pnl = (
        (exec_price - avg_price) * qty
        if _is_buy_side(side)
        else (avg_price - exec_price) * qty
    )

    # DB ORM position の場合のみ DB を更新する。
    if not _is_dict_position(pos):
        sp = Session_position()
        try:
            try:
                db_pos = sp.merge(pos)
            except Exception:
                db_pos = pos

            db_pos.status = "CLOSED"
            db_pos.exit_price = exec_price
            db_pos.exit_time = dt.datetime.now()

            try:
                sp.add(
                    TradeHistory(
                        symbol=symbol,
                        side=side,
                        action="EXIT",
                        qty=qty,
                        price=exec_price,
                        pnl=pnl,
                        realized_pnl=pnl,
                        order_id=api["order_id"],
                        position_id=getattr(db_pos, "id", None),
                        trade_time=dt.datetime.now(),
                        reason=reason,
                    )
                )
            except Exception:
                logger.exception("[EXIT] TradeHistory add failed symbol=%s", symbol)

            sp.commit()

            # TONOSAMA 学習ログ（安全）
            if (
                log_tonosama_trade
                and getattr(db_pos, "entry_source", None) == "ranking"
                and getattr(db_pos, "entry_mode", None) == "TONOSAMA"
            ):
                log_tonosama_trade(db_pos, exec_price, reason)

        except Exception:
            sp.rollback()
            logger.exception("[EXIT] DB close update failed symbol=%s", symbol)
        finally:
            sp.close()

    _remove_open_position(symbol)

    logger.warning(
        "✅ EXIT_ORDER_SENT symbol=%s side=%s qty=%s exec_price=%s pnl=%s reason=%s order_id=%s",
        symbol,
        side,
        qty,
        exec_price,
        pnl,
        reason,
        api.get("order_id"),
    )
    return True


# ============================================================
# EXIT パイプライン（唯一の入口）
# ============================================================
def run_exit_pipeline_core():
    sp = Session_position()
    try:
        db_positions = _load_db_open_positions(sp)
        gc_positions = _load_gc_open_positions()
        positions = _merge_positions(db_positions, gc_positions)

        _heartbeat(
            "[EXIT PIPELINE HEARTBEAT] db_positions=%s gc_positions=%s merged=%s",
            len(db_positions),
            len(gc_positions),
            len(positions),
        )

        if not positions:
            return

        now = dt.datetime.now()

        for pos in positions:
            symbol = _position_symbol(pos)
            if not symbol:
                continue

            bar = build_5s_bar_fast(symbol)
            if not bar:
                logger.debug("[EXIT SKIP] no 5s bar symbol=%s", symbol)
                continue

            price = safe_float(bar.get("close"), 0.0)
            if price <= 0:
                logger.debug("[EXIT SKIP] invalid price symbol=%s bar=%s", symbol, bar)
                continue

            entry_source = str(_get(pos, "entry_source") or _get(pos, "source") or "").lower()
            entry_mode = str(_get(pos, "entry_mode") or _get(pos, "entry_type") or "").upper()

            # --------------------------------------------------
            # TONOSAMA 専用 EXIT
            # --------------------------------------------------
            if entry_source == "ranking" and entry_mode == "TONOSAMA":
                reason = check_tonosama_exit(pos, price, now)
                if reason:
                    execute_exit(pos, price, reason)
                continue

            # --------------------------------------------------
            # 通常 EXIT
            # --------------------------------------------------
            reason = check_normal_exit(pos, price, now)
            if reason:
                execute_exit(pos, price, reason)
                continue

        try:
            sp.commit()
        except Exception:
            sp.rollback()
            logger.exception("[EXIT] session commit failed")

    except Exception as e:
        logger.error("❌ EXIT pipeline error %s", e, exc_info=True)
    finally:
        sp.close()


# ============================================================
# check_normal_exit / check_tonosama_exit の利確保護レイヤ
# ------------------------------------------------------------
# 旧 core/startup の2パッチを本文化。実行時の合成は (実 install 順で実測)
#   check_normal_exit  : native <- normal_exit_scalp_align <- exit_profit_protect
#   check_tonosama_exit: native <- exit_profit_protect
# の順だった。同じ順序で重ねている。
#
# 3つ目の board_wall_stall_exit_patch も同じ2関数を包むが、
# `AUTOSTOCK_TEMP_DISABLE_EXIT` (既定 True) による早期 return で
# **既定では install されない**運用上の一時停止スイッチ配下にある。
# 同パッチはそのまま残してあるので、スイッチを解除すれば従来どおり
# この本文の外側に乗る。
#
# normal_exit_scalp_align が上書きしていた NORMAL_* 定数5件は、
# 呼び出し元 (runtime_env/installer) で env 経由に移してある。
# ============================================================

_NES_VERSION = "V1-NORMAL-EXIT-PROFIT-FLOOR"
_EPP_VERSION = "V1-EXIT-PROFIT-PROTECT"
_CNE_SCOPE = None

def _nes_env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _nes_get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _nes_set(obj: Any, name: str, value: Any) -> None:
    try:
        if isinstance(obj, dict):
            obj[name] = value
        else:
            setattr(obj, name, value)
    except Exception:
        pass


def _nes_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _nes_entry_price(pos: Any) -> float:
    return _nes_safe_float(
        _nes_get(pos, "avg_price")
        or _nes_get(pos, "entry_price")
        or _nes_get(pos, "price")
        or _nes_get(pos, "current_price"),
        0.0,
    )


def _nes_pnl_rate(pos: Any, price: float) -> float:
    entry = _nes_entry_price(pos)
    if entry <= 0 or price <= 0:
        return 0.0
    rate = (float(price) - entry) / entry
    if _is_sell_side(pos):
        rate = -rate
    return rate


def _epp_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _epp_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _epp_get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _epp_set(obj: Any, name: str, value: Any) -> None:
    try:
        if isinstance(obj, dict):
            obj[name] = value
        else:
            setattr(obj, name, value)
    except Exception:
        pass


def _epp_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _epp_symbol(pos: Any) -> str:
    try:
        return str(_epp_get(pos, "symbol") or _epp_get(pos, "Symbol") or _epp_get(pos, "stock_code") or "").strip()
    except Exception:
        return ""


def _epp_side(pos: Any) -> str:
    try:
        return str(_epp_get(pos, "side") or _epp_get(pos, "Side") or "BUY").upper()
    except Exception:
        return "BUY"


def _epp_entry_price(pos: Any) -> float:
    return _epp_safe_float(
        _epp_get(pos, "avg_price")
        or _epp_get(pos, "entry_price")
        or _epp_get(pos, "price")
        or _epp_get(pos, "current_price"),
        0.0,
    )


def _epp_entry_time(pos: Any) -> dt.datetime:
    v = _epp_get(pos, "entry_time")
    if isinstance(v, dt.datetime):
        return v
    try:
        if v:
            return dt.datetime.fromisoformat(str(v))
    except Exception:
        pass
    return dt.datetime.now()


def _epp_pnl_rate(pos: Any, price: float) -> float:
    entry = _epp_entry_price(pos)
    if entry <= 0 or price <= 0:
        return 0.0
    r = (price - entry) / entry
    side = _epp_side(pos)
    if side.startswith("SELL") or side.startswith("SHORT"):
        r = -r
    return r


def _epp_update_max_profit(pos: Any, pnl_rate: float) -> float:
    max_profit = _epp_safe_float(_epp_get(pos, "max_profit_rate"), 0.0)
    max_profit = max(max_profit, pnl_rate)
    _epp_set(pos, "max_profit_rate", max_profit)
    return max_profit


def _epp_profit_protect_reason(pos: Any, price: float, now: dt.datetime, *, label: str) -> str | None:
    if not _epp_env_bool("EXIT_PROFIT_PROTECT_ENABLED", True):
        return None

    entry = _epp_entry_price(pos)
    if entry <= 0 or price <= 0:
        return None

    hold_sec = (now - _epp_entry_time(pos)).total_seconds()
    min_hold = _epp_env_float("EXIT_PROFIT_MIN_HOLD_SEC", 0.0)
    if hold_sec < min_hold:
        return None

    pnl = _epp_pnl_rate(pos, price)
    max_profit = _epp_update_max_profit(pos, pnl)

    take_pct = _epp_env_float("EXIT_PROFIT_TAKE_PCT", 0.0030)
    start_pct = _epp_env_float("EXIT_PROFIT_PROTECT_START_PCT", 0.0020)
    floor_pct = _epp_env_float("EXIT_PROFIT_PROTECT_FLOOR_PCT", 0.0010)
    giveback_pct = _epp_env_float("EXIT_PROFIT_GIVEBACK_PCT", 0.0010)

    symbol = _epp_symbol(pos)
    side = _epp_side(pos)

    # 1) +0.30% 到達なら即利確
    if pnl >= take_pct:
        logger.warning(
            "[EXIT PROFIT PROTECT] TAKE symbol=%s side=%s label=%s price=%.4f entry=%.4f pnl=%.4f max=%.4f take=%.4f hold=%.1fs",
            symbol, side, label, price, entry, pnl, max_profit, take_pct, hold_sec,
        )
        return "PROFIT_TAKE_FAST"

    # 2) 一度 +0.20% 以上乗ったら、+0.10%まで落ちた時点で逃げる
    if max_profit >= start_pct and pnl <= floor_pct:
        logger.warning(
            "[EXIT PROFIT PROTECT] FLOOR symbol=%s side=%s label=%s price=%.4f entry=%.4f pnl=%.4f max=%.4f start=%.4f floor=%.4f hold=%.1fs",
            symbol, side, label, price, entry, pnl, max_profit, start_pct, floor_pct, hold_sec,
        )
        return "PROFIT_PROTECT_FLOOR"

    # 3) 高値利益から -0.10% 戻したら逃げる
    if max_profit >= start_pct and (max_profit - pnl) >= giveback_pct:
        logger.warning(
            "[EXIT PROFIT PROTECT] GIVEBACK symbol=%s side=%s label=%s price=%.4f entry=%.4f pnl=%.4f max=%.4f giveback=%.4f hold=%.1fs",
            symbol, side, label, price, entry, pnl, max_profit, giveback_pct, hold_sec,
        )
        return "PROFIT_PROTECT_GIVEBACK"

    logger.debug(
        "[EXIT PROFIT PROTECT] HOLD symbol=%s side=%s label=%s pnl=%.4f max=%.4f price=%.4f entry=%.4f hold=%.1fs",
        symbol, side, label, pnl, max_profit, price, entry, hold_sec,
    )
    return None


def _cne_scope_enabled() -> bool:
    global _CNE_SCOPE
    if _CNE_SCOPE is not None:
        return _CNE_SCOPE
    enabled = False
    try:
        import sys as _sys

        argv_text = " ".join(str(x) for x in _sys.argv).replace("\\", "/").lower()
        db_names = (
            "main_database.py", "data_collectors_runner.py", "db_prepare_runner.py",
            "push_receiver_runner.py", "yahoo_complement_runner.py",
            "summary_database_runner.py", "ranking_collector_runner.py",
        )
        if not any(n in argv_text for n in db_names):
            enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _CNE_SCOPE = enabled
    return enabled


def _check_normal_exit_scalp_align(pos: Any, price: float, now: dt.datetime):
    """旧 normal_exit_scalp_align_patch._patched_check_normal_exit。"""
    try:
        pnl_rate = _nes_pnl_rate(pos, float(price or 0.0))
        max_profit_rate = max(_nes_safe_float(_nes_get(pos, "max_profit_rate"), 0.0), pnl_rate)
        _nes_set(pos, "max_profit_rate", max_profit_rate)

        breakeven_trigger = abs(_nes_env_float("NORMAL_PROFIT_FLOOR_BREAKEVEN_TRIGGER_PCT", 0.0020))
        breakeven_floor = _nes_env_float("NORMAL_PROFIT_FLOOR_BREAKEVEN_PCT", 0.0005)
        floor1_trigger = abs(_nes_env_float("NORMAL_PROFIT_FLOOR_1_TRIGGER_PCT", 0.0040))
        floor1_rate = _nes_env_float("NORMAL_PROFIT_FLOOR_1_PCT", 0.0010)
        floor2_trigger = abs(_nes_env_float("NORMAL_PROFIT_FLOOR_2_TRIGGER_PCT", 0.0060))
        floor2_rate = _nes_env_float("NORMAL_PROFIT_FLOOR_2_PCT", 0.0025)

        if max_profit_rate >= floor2_trigger and pnl_rate <= floor2_rate:
            return "NORMAL_PROFIT_FLOOR_025"
        if max_profit_rate >= floor1_trigger and pnl_rate <= floor1_rate:
            return "NORMAL_PROFIT_FLOOR_010"
        if max_profit_rate >= breakeven_trigger and pnl_rate <= breakeven_floor:
            return "NORMAL_BREAKEVEN_STOP"
    except Exception:
        logger.exception("[NORMAL EXIT PROFIT FLOOR] precheck failed; fallback original")

    try:
        return _check_normal_exit_core(pos, price, now)
    except Exception:
        logger.exception("[NORMAL EXIT PROFIT FLOOR] original check failed")
    return None


def check_normal_exit_core(pos: Any, price: float, now: dt.datetime):
    if not _cne_scope_enabled():
        return _check_normal_exit_core(pos, price, now)
    reason = _epp_profit_protect_reason(pos, price, now, label="NORMAL")
    if reason:
        return reason
    return _check_normal_exit_scalp_align(pos, price, now)


def check_tonosama_exit_core(pos, price: float, now: dt.datetime):
    if not _cne_scope_enabled():
        return _check_tonosama_exit_core(pos, price, now)
    reason = _epp_profit_protect_reason(pos, price, now, label="TONOSAMA")
    if reason:
        return reason
    return _check_tonosama_exit_core(pos, price, now)


# ============================================================
# 旧 core/startup/board_wall_stall_exit_patch.py を本文化
# ------------------------------------------------------------
# 旧パッチ install() は3つのことをしていた:
#
#  1) _install_entry_board_imbalance_guard()
#     -> final_entry_safety_guard._board_guard は本文化済みで存在せず、
#        常に False を返すだけの no-op だった。_patched_board_guard は
#        一度も呼ばれない死にコードなので移していない。
#        板ガード本体は trading/handlers/entry_controller._final_guard_board_ok。
#
#  2) _install_exit_pipeline_temp_stop()
#     -> _bfe_exit_temp_disabled() が真なら run_exit_pipeline を no-op 化。
#        AUTOSTOCK_TEMP_DISABLE_EXIT は既定 True なので通常はこちらが有効。
#
#  3) check_normal_exit / check_tonosama_exit の板崩れ / 板食い停滞 EXIT
#     -> _bfe_exit_temp_disabled() が偽のときだけ install されていた。
#        つまり 2) と 3) は排他。本文でも同じ排他条件で分岐する。
#
# 旧パッチは main.py:276 の patches リストから install され、この2関数の
# 最外周ラッパーだった (他に同じ属性を包むパッチは残っていない)。
# ここでもネイティブを *_core へ改名し最外周として被せている。
#
# 意図的な差分: 旧パッチは main.py が from-import 済みの
# __main__.run_exit_pipeline を setattr で差し替えていた。本文化後は
# from-import 自体がガード付きの関数を束縛するので setattr は不要。
# 副次的に trading/experimental/scheduler.py の from-import にも
# 同じガードが効く (EXIT 停止という運用意図に沿う向きの差分)。
# ============================================================

def _bfe_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _bfe_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _bfe_exit_temp_stop_flag(name: str) -> bool:
    """EXIT 一時停止フラグ。明示的に真と書かれた時だけ True。

    P0-12: 旧実装は _bfe_env_bool("AUTOSTOCK_TEMP_DISABLE_EXIT", True) で
    「未設定 = 停止」だった。env が未設定 (None) の本番環境で EXIT が
    恒久的に無効化され、2026-08-27 のログでは
      [EXIT TEMP STOP] run_exit_pipeline skipped
      AUTOSTOCK_TEMP_DISABLE_EXIT=None EXIT_TEMP_STOP=None
    が 3,831 行出ていた。返済が一度も走らない状態。

    停止するのは 1/true/on/yes/y と明示された時だけ。
    None / '' / 0 / false / off / no は False (= EXIT 実行) とする。
    未知の値も安全側 (EXIT を止めない) に倒す。
    """
    try:
        v = os.getenv(name)
        if v is None:
            return False
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"", "0", "false", "no", "n", "off", "none", "null"}:
            return False
        logger.warning(
            "[EXIT TEMP STOP] %s=%r を解釈できない -> EXIT は止めない (False)",
            name, v,
        )
        return False
    except Exception:
        logger.exception("[EXIT TEMP STOP] %s の判定に失敗 -> False", name)
        return False


def _bfe_exit_temp_disabled() -> bool:
    # EXIT を止めるのは明示指定があるときだけ。既定は「止めない」。
    return (
        _bfe_exit_temp_stop_flag("AUTOSTOCK_TEMP_DISABLE_EXIT")
        or _bfe_exit_temp_stop_flag("EXIT_TEMP_STOP")
    )


def _bfe_get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _bfe_position_symbol(pos: Any) -> str:
    return str(
        _bfe_get(pos, "symbol")
        or _bfe_get(pos, "Symbol")
        or _bfe_get(pos, "stock_code")
        or ""
    ).strip()


def _bfe_position_side(pos: Any) -> str:
    return str(_bfe_get(pos, "side") or _bfe_get(pos, "Side") or "BUY").upper()


def _bfe_board_fast_exit_reason(pos: Any, price: float) -> str | None:
    """支え板崩壊EXIT → 壁食われ失速EXIT の順で確認する。"""
    if _bfe_exit_temp_disabled():
        return None

    symbol = _bfe_position_symbol(pos)
    side = _bfe_position_side(pos)
    if not symbol or price <= 0:
        return None

    try:
        from trading.board.board_signal import analyze_exit_board_collapse

        detail = analyze_exit_board_collapse(
            symbol,
            position_side=side,
            current_price=float(price),
            exchange=_bfe_env_int("EXIT_BOARD_WALL_EXCHANGE", 1),
        )
        if detail:
            reason = str(detail.get("reason") or "BOARD_SUPPORT_COLLAPSE_EXIT")
            logger.warning(
                "[BOARD FAST EXIT PATCH] EXIT symbol=%s side=%s price=%s reason=%s detail=%s",
                symbol,
                side,
                price,
                reason,
                detail,
            )
            return reason
    except Exception:
        logger.debug("[BOARD FAST EXIT PATCH] collapse analyze failed symbol=%s", symbol, exc_info=True)

    if not _bfe_env_bool("EXIT_BOARD_WALL_STALL_ENABLED", True):
        return None
    try:
        from trading.board.board_state import analyze_wall_eaten_stall

        detail = analyze_wall_eaten_stall(
            symbol,
            position_side=side,
            current_price=float(price),
            exchange=_bfe_env_int("EXIT_BOARD_WALL_EXCHANGE", 1),
        )
        if not detail:
            return None

        reason = str(detail.get("reason") or "BOARD_WALL_EATEN_STALL_EXIT")
        logger.warning(
            "[BOARD WALL STALL EXIT PATCH] EXIT symbol=%s side=%s price=%s reason=%s detail=%s",
            symbol,
            side,
            price,
            reason,
            detail,
        )
        return reason
    except Exception:
        logger.debug("[BOARD WALL STALL EXIT PATCH] analyze failed symbol=%s", symbol, exc_info=True)
        return None


def run_exit_pipeline():
    if _bfe_exit_temp_disabled():
        logger.warning(
            "[EXIT TEMP STOP] run_exit_pipeline skipped AUTOSTOCK_TEMP_DISABLE_EXIT=%s EXIT_TEMP_STOP=%s",
            os.getenv("AUTOSTOCK_TEMP_DISABLE_EXIT"),
            os.getenv("EXIT_TEMP_STOP"),
        )
        return None
    return run_exit_pipeline_core()


def check_normal_exit(pos: Any, price: float, now: dt.datetime):
    if _bfe_exit_temp_disabled():
        return None
    reason = _bfe_board_fast_exit_reason(pos, price)
    if reason:
        return reason
    return check_normal_exit_core(pos, price, now)


def check_tonosama_exit(pos, price: float, now: dt.datetime):
    if _bfe_exit_temp_disabled():
        return None
    reason = _bfe_board_fast_exit_reason(pos, price)
    if reason:
        return reason
    return check_tonosama_exit_core(pos, price, now)
