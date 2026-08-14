# ============================================================
# File   : trading/entry/tonosama/pending_writer.py
# Version: Ver2.1-TONOSAMA-OPTIONAL-5SEC-FINAL-GUARD
# ------------------------------------------------------------
# 目的:
#   殿様イナゴの pending 登録と Discord 通知直前の最終安全ガード。
#
# Ver2.1:
#   - 5秒足は必須にしない方針に合わせる。
#   - has_5sec_bar=True でも five_sec_dt が不明、または latest_5sec_volume=0 の場合は
#     five_sec_stopped_final_guard を発動しない。
#   - 実5秒足が存在する時だけ、5s変化0近辺を停止判定に使う。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from typing import Any

import pandas as pd

from trading.entry.pending_manager import add_pending, get_bucket, replace_bucket, snapshot_root
from .config import TONOSAMA_EXPIRE_SEC
from .utils import normalize_symbol, safe_float

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


MAX_BUY_PRICE_CHANGE_PCT = _env_float("TONOSAMA_MAX_BUY_PRICE_CHANGE_PCT", 0.80)
MAX_BUY_CLOSE_POSITION_PCT = _env_float("TONOSAMA_MAX_BUY_CLOSE_POSITION_PCT", 90.0)
MAX_BUY_UPPER_WICK_PCT = _env_float("TONOSAMA_MAX_BUY_UPPER_WICK_PCT", 45.0)
BUY_REJECTED_CLOSE_POSITION_PCT = _env_float("TONOSAMA_BUY_REJECTED_CLOSE_POSITION_PCT", 35.0)
BUYING_CLIMAX_MIN_SURGE_RATIO = _env_float("TONOSAMA_BUYING_CLIMAX_MIN_SURGE_RATIO", 3.0)
BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT = _env_float("TONOSAMA_BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT", 0.50)

MAX_SELL_PRICE_DROP_PCT = _env_float("TONOSAMA_MAX_SELL_PRICE_DROP_PCT", 0.80)
MIN_SELL_CLOSE_POSITION_PCT = _env_float("TONOSAMA_MIN_SELL_CLOSE_POSITION_PCT", 10.0)
MAX_SELL_LOWER_WICK_PCT = _env_float("TONOSAMA_MAX_SELL_LOWER_WICK_PCT", 45.0)
SELL_REJECTED_CLOSE_POSITION_PCT = _env_float("TONOSAMA_SELL_REJECTED_CLOSE_POSITION_PCT", 65.0)
SELLING_CLIMAX_MIN_SURGE_RATIO = _env_float("TONOSAMA_SELLING_CLIMAX_MIN_SURGE_RATIO", 3.0)
SELLING_CLIMAX_MIN_PRICE_DROP_PCT = _env_float("TONOSAMA_SELLING_CLIMAX_MIN_PRICE_DROP_PCT", 0.50)

MAX_BUY_PREV_3M5M_UP_STREAK = _env_int("TONOSAMA_MAX_BUY_PREV_3M5M_UP_STREAK", 2)
MAX_SELL_PREV_3M5M_DOWN_STREAK = _env_int("TONOSAMA_MAX_SELL_PREV_3M5M_DOWN_STREAK", 2)
MIN_FINAL_LATEST_VOLUME = _env_float("TONOSAMA_MIN_FINAL_LATEST_VOLUME", _env_float("TONOSAMA_MIN_LATEST_VOLUME", 50000.0))

# final direction / optional 5sec guard
MIN_FINAL_5SEC_CHANGE_PCT = _env_float("TONOSAMA_FINAL_MIN_5SEC_CHANGE_PCT", _env_float("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.01))
REJECT_ZERO_5SEC_FINAL = _env_bool("TONOSAMA_FINAL_REJECT_ZERO_5SEC", True)
REJECT_ZERO_5SEC_ONLY_WHEN_VALID = _env_bool("TONOSAMA_FINAL_REJECT_ZERO_5SEC_ONLY_WHEN_VALID", True)
MIN_BUY_5M_CHANGE_FINAL = _env_float("TONOSAMA_FINAL_MIN_BUY_5M_CHANGE", 0.0)
MAX_SELL_5M_CHANGE_FINAL = _env_float("TONOSAMA_FINAL_MAX_SELL_5M_CHANGE", 0.0)


def _norm_source(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_tonosama_entry(entry: Any) -> bool:
    return isinstance(entry, dict) and _norm_source(entry.get("source")) == "TONOSAMA"


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if v is None or v == "":
            return None
        if isinstance(v, pd.Timestamp):
            if pd.isna(v):
                return None
            py = v.to_pydatetime()
            return py.replace(tzinfo=None) if py.tzinfo else py
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None) if v.tzinfo else v
        if isinstance(v, dt.date):
            return dt.datetime.combine(v, dt.time.min)
        s = str(v).strip()
        if not s or s in {"不明", "なし", "None", "nan", "NaT"}:
            return None
        parsed = pd.to_datetime(s, errors="coerce")
        if pd.isna(parsed):
            return None
        py = parsed.to_pydatetime()
        return py.replace(tzinfo=None) if py.tzinfo else py
    except Exception:
        return None


def _fmt_dt(v: Any) -> str:
    d = _parse_dt(v)
    if d is None:
        return "不明"
    try:
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(d)


def _first_dt_from_row(row: pd.Series, names: list[str]) -> Any:
    try:
        for n in names:
            if n in row.index:
                v = row.get(n)
                if v is not None and str(v).strip() != "":
                    return v
    except Exception:
        pass
    return None


def _expire_at(entry: dict[str, Any]) -> dt.datetime | None:
    try:
        ec = entry.get("entry_conditions") or {}
        if isinstance(ec, dict):
            exp = _parse_dt(ec.get("expire_at"))
            if exp is not None:
                return exp
        exp = _parse_dt(entry.get("expire_at"))
        if exp is not None:
            return exp
        created_at = _parse_dt(entry.get("created_at"))
        if created_at is not None:
            return created_at + dt.timedelta(seconds=int(TONOSAMA_EXPIRE_SEC or 180))
    except Exception:
        return None
    return None


def _is_expired_tonosama_entry(entry: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    try:
        if not _is_tonosama_entry(entry):
            return False
        exp = _expire_at(entry)
        if exp is None:
            return False
        return (now or dt.datetime.now()) >= exp
    except Exception:
        return False


def _prune_symbol_expired_tonosama(symbol: str, *, reason: str, now: dt.datetime | None = None) -> int:
    sym = normalize_symbol(symbol)
    if not sym:
        return 0
    removed = 0
    now = now or dt.datetime.now()
    bucket = get_bucket(sym)
    kept: list[dict[str, Any]] = []
    for entry in bucket:
        if isinstance(entry, dict) and _is_expired_tonosama_entry(entry, now=now):
            removed += 1
            logger.warning(
                "[TONOSAMA PENDING] expired pruned symbol=%s reason=%s expire_at=%s created_at=%s side=%s score=%s",
                sym, reason, _expire_at(entry), entry.get("created_at"), entry.get("side"), entry.get("final_score") or entry.get("score"),
            )
            continue
        if isinstance(entry, dict):
            kept.append(entry)
    if removed:
        replace_bucket(sym, kept)
    return removed


def prune_expired_tonosama_pending(symbol: str | None = None, *, reason: str = "TONOSAMA_EXPIRED") -> int:
    now = dt.datetime.now()
    try:
        if symbol:
            return _prune_symbol_expired_tonosama(symbol, reason=reason, now=now)
        removed = 0
        for sym in list(snapshot_root().keys()):
            removed += _prune_symbol_expired_tonosama(sym, reason=reason, now=now)
        if removed:
            logger.warning("[TONOSAMA PENDING] expired pending prune done removed=%s reason=%s root=%s", removed, reason, snapshot_root())
        return int(removed or 0)
    except Exception:
        logger.exception("[TONOSAMA PENDING] prune expired failed symbol=%s reason=%s", symbol, reason)
        return 0


def has_tonosama_pending(symbol: str) -> bool:
    sym = normalize_symbol(symbol)
    if not sym:
        return False
    prune_expired_tonosama_pending(sym, reason="TONOSAMA_DUP_CHECK_EXPIRED")
    bucket = get_bucket(sym)
    return any(_is_tonosama_entry(e) for e in bucket if isinstance(e, dict))


def _infer_side_from_row(row: pd.Series) -> str:
    max_chg = safe_float(row.get("_max_price_change_pct"), 0.0)
    signed_body = safe_float(row.get("_signed_body_change_pct"), max_chg)
    slope = safe_float(row.get("_slope"), 0.0)
    score_sell = safe_float(row.get("score_sell"), 0.0)
    score_buy = safe_float(row.get("score_buy"), 0.0)
    if max_chg < 0:
        return "SELL"
    if max_chg > 0:
        return "BUY"
    if signed_body < 0:
        return "SELL"
    if signed_body > 0:
        return "BUY"
    if slope < 0:
        return "SELL"
    if slope > 0:
        return "BUY"
    if score_sell > score_buy:
        return "SELL"
    return "BUY"


def _build_reason_ja(row: pd.Series, *, ai_reason: str, side: str) -> str:
    max_surge = safe_float(row.get("_max_volume_surge_ratio"), 0.0)
    max_chg = safe_float(row.get("_max_price_change_pct"), 0.0)
    chg_3m = safe_float(row.get("price_change_pct_3m"), 0.0)
    chg_5m = safe_float(row.get("price_change_pct_5m"), 0.0)
    chg_5s = safe_float(row.get("price_change_5s_pct"), 0.0)
    has_5s = bool(row.get("has_5sec_bar", False))
    slope = safe_float(row.get("_slope"), 0.0)
    latest_volume = safe_float(row.get("_latest_volume"), 0.0)
    volume_3m = safe_float(row.get("volume_3m"), 0.0)
    volume_5m = safe_float(row.get("volume_5m"), 0.0)
    tf = str(row.get("_surge_tf", ""))
    up3 = int(safe_float(row.get("prev_3m_up_streak"), 0.0))
    up5 = int(safe_float(row.get("prev_5m_up_streak"), 0.0))
    dn3 = int(safe_float(row.get("prev_3m_down_streak"), 0.0))
    dn5 = int(safe_float(row.get("prev_5m_down_streak"), 0.0))

    decision_at = dt.datetime.now()
    feature_dt = _first_dt_from_row(row, ["datetime", "dt", "summary_dt", "bar_dt", "latest_dt", "_latest_dt"])
    five_sec_dt = _first_dt_from_row(row, ["latest_5sec_dt", "five_sec_dt", "bar_5s_dt", "dt_5s", "timestamp_5s"])

    parts = [
        f"判定時刻 {decision_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"特徴量時刻 {_fmt_dt(feature_dt)}",
        f"5秒足時刻 {_fmt_dt(five_sec_dt) if has_5s else 'なし'}",
        f"方向 {side}",
        f"{tf or '3m/5m'}で出来高急増 {max_surge:.2f}倍",
        f"価格変化 max={max_chg:.2f}% / 3m={chg_3m:.2f}% / 5m={chg_5m:.2f}%",
        f"傾き {slope:.4f}",
        f"出来高 1m={latest_volume:.0f} / 3m={volume_3m:.0f} / 5m={volume_5m:.0f}",
        f"3分連続上昇 {up3}本 / 5分連続上昇 {up5}本 / 3分連続下落 {dn3}本 / 5分連続下落 {dn5}本",
        "ランキングスナップショットMA方向は通知直前に判定",
    ]
    parts.append(f"5秒変化 {chg_5s:.3f}%" if has_5s else "5秒足なしのため3m/5m条件で判定")
    if ai_reason:
        parts.append(f"AI判定: {ai_reason}")
    return " / ".join(parts)


def _entry_conditions_from_row(row: pd.Series, *, ai_reason: str, side: str, expire_at: dt.datetime) -> dict[str, Any]:
    feature_dt = _first_dt_from_row(row, ["datetime", "dt", "summary_dt", "bar_dt", "latest_dt", "_latest_dt"])
    five_sec_dt_raw = _first_dt_from_row(row, ["latest_5sec_dt", "five_sec_dt", "bar_5s_dt", "dt_5s", "timestamp_5s"])
    five_sec_dt = _fmt_dt(five_sec_dt_raw) if bool(row.get("has_5sec_bar", False)) else "なし"
    return {
        "expire_at": expire_at,
        "decision_at": dt.datetime.now(),
        "feature_dt": _fmt_dt(feature_dt),
        "five_sec_dt": five_sec_dt,
        "five_sec_dt_valid": _parse_dt(five_sec_dt_raw) is not None,
        "reason": _build_reason_ja(row, ai_reason=ai_reason, side=side),
        "reason_code": "tonosama_volume_surge_price_change_5sec_ai",
        "ai_reason": ai_reason,
        "side": side,
        "volume_surge_ratio_3m": safe_float(row.get("volume_surge_ratio_3m"), 0.0),
        "volume_surge_ratio_5m": safe_float(row.get("volume_surge_ratio_5m"), 0.0),
        "max_volume_surge_ratio": safe_float(row.get("_max_volume_surge_ratio"), 0.0),
        "price_change_pct_3m": safe_float(row.get("price_change_pct_3m"), 0.0),
        "price_change_pct_5m": safe_float(row.get("price_change_pct_5m"), 0.0),
        "max_price_change_pct": safe_float(row.get("_max_price_change_pct"), 0.0),
        "signed_body_change_pct": safe_float(row.get("_signed_body_change_pct"), safe_float(row.get("_max_price_change_pct"), 0.0)),
        "body_change_pct": safe_float(row.get("_body_change_pct"), 0.0),
        "intrabar_range_pct": safe_float(row.get("_intrabar_range_pct"), 0.0),
        "close_position_pct": safe_float(row.get("_close_position_pct"), 50.0),
        "upper_wick_pct": safe_float(row.get("_upper_wick_pct"), 0.0),
        "lower_wick_pct": safe_float(row.get("_lower_wick_pct"), 0.0),
        "prev_3m_up_streak": int(safe_float(row.get("prev_3m_up_streak"), 0.0)),
        "prev_5m_up_streak": int(safe_float(row.get("prev_5m_up_streak"), 0.0)),
        "prev_3m_down_streak": int(safe_float(row.get("prev_3m_down_streak"), 0.0)),
        "prev_5m_down_streak": int(safe_float(row.get("prev_5m_down_streak"), 0.0)),
        "prev_3m_last_delta_pct": safe_float(row.get("prev_3m_last_delta_pct"), 0.0),
        "prev_5m_last_delta_pct": safe_float(row.get("prev_5m_last_delta_pct"), 0.0),
        "latest_volume": safe_float(row.get("_latest_volume"), 0.0),
        "volume_3m": safe_float(row.get("volume_3m"), 0.0),
        "volume_5m": safe_float(row.get("volume_5m"), 0.0),
        "min_final_latest_volume": MIN_FINAL_LATEST_VOLUME,
        "has_5sec_bar": bool(row.get("has_5sec_bar", False)),
        "latest_5sec_close": safe_float(row.get("latest_5sec_close"), 0.0),
        "latest_5sec_volume": safe_float(row.get("latest_5sec_volume"), 0.0),
        "price_change_5s_pct": safe_float(row.get("price_change_5s_pct"), 0.0),
        "volume_surge_ratio_5s": safe_float(row.get("volume_surge_ratio_5s"), 0.0),
        "is_5sec_confirm_ok": bool(row.get("is_5sec_confirm_ok", False)),
        "surge_tf": str(row.get("_surge_tf", "")),
        "slope": safe_float(row.get("_slope"), 0.0),
        "rsi": safe_float(row.get("rsi"), 0.0),
        "macd": safe_float(row.get("macd"), 0.0),
        "signal": safe_float(row.get("signal"), 0.0),
        "mtf": safe_float(row.get("mtf"), 0.0),
        "score_mtf": safe_float(row.get("score_mtf"), 0.0),
    }


def _ranking_ma_reject_reason(symbol: str, side: str) -> tuple[str | None, dict[str, Any]]:
    try:
        from .ranking_snapshot_ma_guard import reject_reason_for_side
        return reject_reason_for_side(symbol, side)
    except Exception:
        logger.warning("[TONOSAMA PENDING GUARD] ranking snapshot MA guard failed symbol=%s side=%s", symbol, side, exc_info=True)
        return None, {"reason": "ranking_ma_guard_exception"}


def _has_valid_5sec_for_zero_guard(cond: dict[str, Any]) -> bool:
    if not bool(cond.get("has_5sec_bar", False)):
        return False
    if REJECT_ZERO_5SEC_ONLY_WHEN_VALID:
        if not bool(cond.get("five_sec_dt_valid", False)):
            return False
        if safe_float(cond.get("latest_5sec_volume"), 0.0) <= 0:
            return False
    return True


# ============================================================
# pending 登録直前 final guard の緩和 2 層
#   旧 core/startup/tonosama_5sec_stopped_relax_patch.py       (V1.2)
#   旧 core/startup/tonosama_pending_warning_relax_patch.py    (V3)
# ------------------------------------------------------------
# 2 本とも pw._climax_reject_reason を差し替えており、しかも両方が
# _ORIG = getattr(cur, "_original", cur) で「既存ラッパーを外して捨てる」
# 実装だった。マーカーも別なので、後に install した側が相手を丸ごと無効化する。
#   - 5sec relax : main.py:219 から既定 ON
#   - warning relax: sitecustomize の TONOSAMA_EXTRA_RESCUE_PATCHES (既定 OFF)
# 既定構成では 5sec relax だけが生きていた。本文化では両方を段階として
# 直列に合成し、それぞれ本来のスコープでだけ効くようにした。
# (両方 ON にしたときの「後勝ちで片方消える」レースはここで解消する)
# ============================================================
_RELAX_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)
_RELAX_DB_PROCESS_ENVS = (
    "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
    "AUTOSTOCK_SUMMARY_DB_WRITER",
    "AUTOSTOCK_MAIN_DATABASE_PROCESS",
    "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
)

_WARNING_RELAX_REASONS = {
    "buying_climax_upper_wick_warning",
    "selling_climax_lower_wick_warning",
}


def _relax_env_bool(name: str, default: bool = True) -> bool:
    """旧パッチ 2 本に共通の _env_on / _env_bool。未知の値は False。

    本ファイルの _env_bool は未知の値で default を返すので別実装にしてある。
    """
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _relax_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _relax_sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _relax_not_db_process() -> bool:
    argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
    argv = " ".join(argv_list)
    if any(name in argv for name in _RELAX_DB_PROCESS_NAMES):
        return False
    if any(_relax_env_bool(name, False) for name in _RELAX_DB_PROCESS_ENVS):
        return False
    return True


def _five_sec_relax_scope_enabled() -> bool:
    """5sec relax は main.py:219 の _install_main_runtime_patches から既定 ON。

    DISABLE_* env は無かった。TONOSAMA_5SEC_RELAX_SCOPE_ENABLED で明示上書き可。
    """
    try:
        raw = os.getenv("TONOSAMA_5SEC_RELAX_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _relax_env_bool("TONOSAMA_5SEC_RELAX_SCOPE_ENABLED", False)
        if not _relax_not_db_process():
            return False
        argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(a.endswith("/main.py") or a == "main.py" for a in argv_list)
    except Exception:
        return False


def _warning_relax_scope_enabled() -> bool:
    """warning relax は TONOSAMA_EXTRA_RESCUE_PATCHES = opt-in グループ (既定 OFF)。"""
    try:
        raw = os.getenv("TONOSAMA_PENDING_WARNING_RELAX_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _relax_env_bool("TONOSAMA_PENDING_WARNING_RELAX_SCOPE_ENABLED", False)
        if str(os.environ.get("DISABLE_TONOSAMA_PENDING_WARNING_RELAX_PATCH", "")).strip() == "1":
            return False
        if not (
            _relax_env_bool("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES", False)
            or _relax_env_bool("SITECUSTOMIZE_ENABLE_TONOSAMA_EXTRA_RESCUE_PATCHES", False)
            or _relax_env_bool("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES", False)
        ):
            return False
        if not _relax_not_db_process():
            return False
        argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
        if any(a.endswith("/main.py") or a == "main.py" for a in argv_list) and _relax_env_bool("SITECUSTOMIZE_MAIN_LITE", True):
            return True
        return _relax_env_bool("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False)
    except Exception:
        return False


def _effective_zero_5sec_guard() -> tuple[bool, float]:
    """5秒足ゼロ guard の実効値。

    旧 warning relax の install() は TONOSAMA_FINAL_5SEC_OPTIONAL が真のとき
    pw.REJECT_ZERO_5SEC_FINAL=False / pw.MIN_FINAL_5SEC_CHANGE_PCT=0.0 と
    モジュール定数を書き換えていた。読み手はこの guard 1 箇所だけなので、
    定数を破壊せずここで実効値に落とす。
    """
    if _warning_relax_scope_enabled() and _relax_env_bool("TONOSAMA_FINAL_5SEC_OPTIONAL", True):
        return False, 0.0
    return REJECT_ZERO_5SEC_FINAL, MIN_FINAL_5SEC_CHANGE_PCT


def _relax_unknown_dt(v: Any) -> bool:
    s = str(v or "").strip().lower()
    return s in {"", "none", "nan", "nat", "不明", "なし"}


def _one_min_direction_ok(cond: dict[str, Any], side: str) -> tuple[bool, dict[str, Any]]:
    side_u = str(side or "").upper()
    price_chg = _relax_sf(cond.get("max_price_change_pct"), 0.0)
    signed_body = _relax_sf(cond.get("signed_body_change_pct"), price_chg)
    slope = _relax_sf(cond.get("slope"), 0.0)
    rng = _relax_sf(cond.get("intrabar_range_pct"), 0.0)
    close_pos = _relax_sf(cond.get("close_position_pct"), 50.0)
    upper_wick = _relax_sf(cond.get("upper_wick_pct"), 0.0)
    lower_wick = _relax_sf(cond.get("lower_wick_pct"), 0.0)
    surge = _relax_sf(cond.get("max_volume_surge_ratio"), 0.0)
    latest_volume = _relax_sf(cond.get("latest_volume"), 0.0)

    min_body = abs(_relax_env_float("TONOSAMA_5SEC_RELAX_MIN_1M_BODY_PCT", 0.10))
    min_slope = abs(_relax_env_float("TONOSAMA_5SEC_RELAX_MIN_1M_SLOPE", 0.0010))
    min_range = _relax_env_float("TONOSAMA_5SEC_RELAX_MIN_RANGE_PCT", 3.0)
    min_volume = _relax_env_float("TONOSAMA_5SEC_RELAX_MIN_VOLUME", 50000.0)
    min_surge = _relax_env_float("TONOSAMA_5SEC_RELAX_MIN_SURGE", 3.0)
    climax_range = _relax_env_float("TONOSAMA_5SEC_RELAX_CLIMAX_RANGE_PCT", 6.0)

    diag = {
        "side": side_u, "price_chg": price_chg, "signed_body": signed_body, "slope": slope,
        "range": rng, "close_pos": close_pos, "upper_wick": upper_wick, "lower_wick": lower_wick,
        "surge": surge, "latest_volume": latest_volume, "min_body": min_body,
        "min_slope": min_slope, "min_range": min_range, "min_volume": min_volume, "min_surge": min_surge,
    }

    if latest_volume < min_volume:
        return False, {**diag, "ng": "volume_low"}
    if rng < min_range:
        return False, {**diag, "ng": "range_low"}
    if surge < min_surge:
        return False, {**diag, "ng": "surge_low"}

    if side_u == "SELL":
        direction_ok = signed_body <= -min_body and slope <= -min_slope
        # SELLでは上ヒゲ・安値引けは弱気材料。下ヒゲ反転だけをクライマックス扱いする。
        climax_like = rng >= climax_range and close_pos <= 35.0 and lower_wick >= 45.0
        if direction_ok and not climax_like:
            return True, {**diag, "ok": "sell_1m_body_slope"}
        return False, {**diag, "ng": "sell_1m_direction_or_climax_ng", "direction_ok": direction_ok, "climax_like": climax_like}

    if side_u == "BUY":
        direction_ok = signed_body >= min_body and slope >= min_slope
        # BUYでは下ヒゲ・高値引けは強気材料。上ヒゲ反転だけをクライマックス扱いする。
        climax_like = rng >= climax_range and close_pos >= 65.0 and upper_wick >= 45.0
        if direction_ok and not climax_like:
            return True, {**diag, "ok": "buy_1m_body_slope"}
        return False, {**diag, "ng": "buy_1m_direction_or_climax_ng", "direction_ok": direction_ok, "climax_like": climax_like}

    return False, {**diag, "ng": "unknown_side"}


def _soft_rescue_final_reject(cond: dict[str, Any], side: str) -> tuple[bool, dict[str, Any]]:
    ai_reason = str(cond.get("ai_reason") or "")
    side_u = str(side or "").upper()
    if "soft_rescue" not in ai_reason and "range_rescue" not in ai_reason:
        return False, {"reason": "not_soft_rescue"}

    if _relax_env_bool("TONOSAMA_SOFT_RESCUE_ALLOW_1M_DIRECTION", True):
        ok1, diag1 = _one_min_direction_ok(cond, side_u)
        if ok1:
            return False, {**diag1, "ai_reason": ai_reason[:180], "soft_rescue_1m_allow": True}

    chg3 = _relax_sf(cond.get("price_change_pct_3m"), 0.0)
    chg5 = _relax_sf(cond.get("price_change_pct_5m"), 0.0)
    price_chg = _relax_sf(cond.get("max_price_change_pct"), 0.0)
    signed_body = _relax_sf(cond.get("signed_body_change_pct"), price_chg)
    slope = _relax_sf(cond.get("slope"), 0.0)
    rng = _relax_sf(cond.get("intrabar_range_pct"), 0.0)
    close_pos = _relax_sf(cond.get("close_position_pct"), 50.0)
    upper_wick = _relax_sf(cond.get("upper_wick_pct"), 0.0)
    lower_wick = _relax_sf(cond.get("lower_wick_pct"), 0.0)
    surge = _relax_sf(cond.get("max_volume_surge_ratio"), 0.0)
    latest_volume = _relax_sf(cond.get("latest_volume"), 0.0)

    min_move = abs(_relax_env_float("TONOSAMA_SOFT_RESCUE_MIN_DIRECTION_MOVE", 0.10))
    min_range = _relax_env_float("TONOSAMA_SOFT_RESCUE_CLIMAX_RANGE_PCT", 6.0)
    min_surge = _relax_env_float("TONOSAMA_SOFT_RESCUE_CLIMAX_SURGE", 3.0)

    diag = {
        "side": side_u, "chg3": chg3, "chg5": chg5, "price_chg": price_chg,
        "signed_body": signed_body, "slope": slope, "range": rng, "close_pos": close_pos,
        "upper_wick": upper_wick, "lower_wick": lower_wick, "surge": surge,
        "latest_volume": latest_volume, "min_move": min_move, "ai_reason": ai_reason[:180],
    }

    if side_u == "BUY":
        if price_chg < min_move or chg3 < min_move or chg5 < min_move or slope <= 0:
            return True, {**diag, "ng": "soft_rescue_buy_direction_too_weak"}
        if surge >= min_surge and rng >= min_range and (close_pos >= 65.0 or upper_wick >= 35.0):
            return True, {**diag, "ng": "soft_rescue_buying_climax_like_volume"}

    if side_u == "SELL":
        if price_chg > -min_move or chg3 > -min_move or chg5 > -min_move or slope >= 0:
            return True, {**diag, "ng": "soft_rescue_sell_direction_too_weak"}
        if surge >= min_surge and rng >= min_range and (close_pos <= 35.0 or lower_wick >= 35.0):
            return True, {**diag, "ng": "soft_rescue_selling_climax_like_volume"}

    return False, diag


def _side_ok_by_3m5m(cond: dict[str, Any], side: str, ma_info: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    side_u = str(side or "").upper()
    chg3 = _relax_sf(cond.get("price_change_pct_3m"), 0.0)
    chg5 = _relax_sf(cond.get("price_change_pct_5m"), 0.0)
    slope = _relax_sf(cond.get("slope"), 0.0)
    price_chg = _relax_sf(cond.get("max_price_change_pct"), 0.0)
    latest_volume = _relax_sf(cond.get("latest_volume"), 0.0)
    min_volume = _relax_sf(cond.get("min_final_latest_volume"), _relax_env_float("TONOSAMA_MIN_FINAL_LATEST_VOLUME", 50000.0))
    ma3_slope = _relax_sf(ma_info.get("ma3_slope"), 0.0)
    ma5_slope = _relax_sf(ma_info.get("ma5_slope"), 0.0)
    ma_reason = str(ma_info.get("reason") or "")
    rows = _relax_sf(ma_info.get("rows"), 0.0)

    eps = abs(_relax_env_float("TONOSAMA_5SEC_FALLBACK_EPS", 0.000001))
    min_move = abs(_relax_env_float("TONOSAMA_5SEC_FALLBACK_MIN_3M5M_CHANGE", 0.10))

    diag = {
        "side": side_u, "chg3": chg3, "chg5": chg5, "slope": slope, "price_chg": price_chg,
        "latest_volume": latest_volume, "min_volume": min_volume, "ma3_slope": ma3_slope,
        "ma5_slope": ma5_slope, "ma_reason": ma_reason, "rows": rows, "eps": eps, "min_move": min_move,
    }

    if latest_volume < min_volume:
        return False, {**diag, "ng": "latest_volume_low"}
    if ma_reason and ma_reason not in {"ok", ""}:
        return False, {**diag, "ng": "ranking_ma_not_ok"}

    if side_u == "BUY":
        aligned = (chg3 >= min_move) and (chg5 >= min_move) and (slope > eps)
        against = (price_chg < min_move) or (ma3_slope < -eps and ma5_slope < -eps)
        return bool(aligned and not against), {**diag, "aligned": aligned, "against": against}

    if side_u == "SELL":
        aligned = (chg3 <= -min_move) and (chg5 <= -min_move) and (slope < -eps)
        against = (price_chg > -min_move) or (ma3_slope > eps and ma5_slope > eps)
        return bool(aligned and not against), {**diag, "aligned": aligned, "against": against}

    return False, {**diag, "ng": "unknown_side"}


def _five_sec_relax_stage(entry: dict[str, Any], reject, ma_info):
    """旧 tonosama_5sec_stopped_relax_patch._patched_climax_reject_reason の後段。"""
    try:
        cond = entry.get("entry_conditions") or {}
        if not isinstance(cond, dict):
            return reject, ma_info
        side = str(entry.get("side") or cond.get("side") or "").upper()

        soft_reject, soft_diag = _soft_rescue_final_reject(cond, side)
        if soft_reject:
            logger.warning(
                "[TONOSAMA 5SEC RELAX] reject soft/range rescue symbol=%s side=%s diag=%s",
                entry.get("symbol"), side, soft_diag,
            )
            return soft_diag.get("ng", "soft_rescue_final_guard"), ma_info

        if reject in {"sell_ranking_ma3_ma5_up_guard", "buy_ranking_ma3_ma5_down_guard"} and _relax_env_bool("TONOSAMA_RANKING_MA_GUARD_ALLOW_STRONG_1M", True):
            ok1, diag1 = _one_min_direction_ok(cond, side)
            if ok1:
                logger.warning(
                    "[TONOSAMA 5SEC RELAX] warn-only ranking MA guard because 1m direction strong symbol=%s side=%s reject=%s diag=%s ma_info=%s",
                    entry.get("symbol"), side, reject, diag1, ma_info,
                )
                return None, ma_info

        if reject != "five_sec_stopped_final_guard":
            return reject, ma_info
        if not _relax_env_bool("TONOSAMA_5SEC_STOPPED_NO_DT_FALLBACK", True):
            return reject, ma_info

        five_sec_dt = cond.get("five_sec_dt")
        if not _relax_unknown_dt(five_sec_dt):
            return reject, ma_info

        ok1, diag1 = _one_min_direction_ok(cond, side)
        if ok1:
            logger.warning(
                "[TONOSAMA 5SEC RELAX] allow five_sec_stopped because five_sec_dt missing and 1m direction strong symbol=%s side=%s diag=%s",
                entry.get("symbol"), side, diag1,
            )
            return None, ma_info

        ok, diag = _side_ok_by_3m5m(cond, side, ma_info if isinstance(ma_info, dict) else {})
        if ok:
            logger.warning(
                "[TONOSAMA 5SEC RELAX] allow five_sec_stopped because five_sec_dt missing and strict 3m/5m/ranking_ma aligned symbol=%s side=%s diag=%s",
                entry.get("symbol"), side, diag,
            )
            return None, ma_info

        logger.warning(
            "[TONOSAMA 5SEC RELAX] keep reject symbol=%s side=%s reason=%s diag=%s",
            entry.get("symbol"), side, reject, {"one_min": diag1, "mtf": diag},
        )
        return reject, ma_info
    except Exception:
        logger.exception("[TONOSAMA 5SEC RELAX] wrapper failed symbol=%s", entry.get("symbol") if isinstance(entry, dict) else None)
        return reject, ma_info


def _relax_ma_ok(info: Any, *, side: str) -> bool:
    if not isinstance(info, dict):
        return True
    reason = str(info.get("reason") or "").strip().lower()
    if reason not in {"", "ok", "none"}:
        return False
    ma3_slope = _relax_sf(info.get("ma3_slope"), 0.0)
    ma5_slope = _relax_sf(info.get("ma5_slope"), 0.0)
    if side == "BUY":
        return ma3_slope >= 0 or ma5_slope >= 0 or (ma3_slope == 0 and ma5_slope == 0)
    if side == "SELL":
        return ma3_slope <= 0 or ma5_slope <= 0 or (ma3_slope == 0 and ma5_slope == 0)
    return True


def _relax_primary_3m5m_ok(entry: dict[str, Any], info: Any) -> bool:
    cond = entry.get("entry_conditions") or {}
    if not isinstance(cond, dict):
        return False

    side = str(entry.get("side") or cond.get("side") or "").upper()
    symbol = entry.get("symbol")
    price_chg = _relax_sf(cond.get("max_price_change_pct"), 0.0)
    chg_3m = _relax_sf(cond.get("price_change_pct_3m"), 0.0)
    chg_5m = _relax_sf(cond.get("price_change_pct_5m"), 0.0)
    chg_5s = _relax_sf(cond.get("price_change_5s_pct"), 0.0)
    surge = _relax_sf(cond.get("max_volume_surge_ratio"), 0.0)
    latest_volume = _relax_sf(cond.get("latest_volume"), 0.0)
    volume_3m = _relax_sf(cond.get("volume_3m"), 0.0)
    volume_5m = _relax_sf(cond.get("volume_5m"), 0.0)
    slope = _relax_sf(cond.get("slope"), 0.0)
    close_pos = _relax_sf(cond.get("close_position_pct"), 50.0)

    min_volume = _relax_env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_MIN_VOLUME", 50000.0)
    min_surge = _relax_env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_MIN_SURGE", 3.0)
    min_abs_move = _relax_env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_MIN_ABS_3M5M_MOVE", 0.0)

    volume_ok = max(latest_volume, volume_3m, volume_5m) >= min_volume
    surge_ok = surge >= min_surge
    ma_ok = _relax_ma_ok(info, side=side)

    if side == "BUY":
        direction_ok = (
            chg_3m >= -0.001
            and chg_5m >= -0.001
            and (price_chg >= min_abs_move or chg_3m >= min_abs_move or chg_5m >= min_abs_move or slope >= 0)
        )
        # 高値圏でも、ここでは5秒停止だけを理由に落とさない。
        # buying_climax 系の強い拒否は元関数側で別理由として残る。
        close_ok = close_pos >= _relax_env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_BUY_MIN_CLOSE_POS", 35.0)
    elif side == "SELL":
        direction_ok = (
            chg_3m <= 0.001
            and chg_5m <= 0.001
            and (price_chg <= -min_abs_move or chg_3m <= -min_abs_move or chg_5m <= -min_abs_move or slope <= 0)
        )
        close_ok = close_pos <= _relax_env_float("TONOSAMA_FINAL_5SEC_OPTIONAL_SELL_MAX_CLOSE_POS", 65.0)
    else:
        direction_ok = False
        close_ok = False

    ok = bool(volume_ok and surge_ok and ma_ok and direction_ok and close_ok)
    logger.warning(
        "[TONOSAMA PENDING 5SEC OPTIONAL V3] check symbol=%s side=%s ok=%s volume_ok=%s surge_ok=%s ma_ok=%s direction_ok=%s close_ok=%s five_sec_dt=%s chg5s=%.3f price_chg=%.3f chg3m=%.3f chg5m=%.3f surge=%.2f latest_volume=%.0f volume_3m=%.0f volume_5m=%.0f slope=%.6f close_pos=%.1f ma=%s",
        symbol, side, ok, volume_ok, surge_ok, ma_ok, direction_ok, close_ok,
        cond.get("five_sec_dt"), chg_5s, price_chg, chg_3m, chg_5m, surge,
        latest_volume, volume_3m, volume_5m, slope, close_pos, info,
    )
    return ok


def _warning_relax_stage(entry: dict[str, Any], reject, info):
    """旧 tonosama_pending_warning_relax_patch._patched_climax_reject_reason の後段。"""
    try:
        if reject == "five_sec_stopped_final_guard" and _relax_env_bool("TONOSAMA_FINAL_5SEC_OPTIONAL", True):
            if _relax_primary_3m5m_ok(entry, info):
                logger.warning(
                    "[TONOSAMA PENDING 5SEC OPTIONAL V3] allow five_sec_stopped_final_guard symbol=%s side=%s",
                    entry.get("symbol"), entry.get("side"),
                )
                return None, info
            return reject, info

        if not _relax_env_bool("TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX", True):
            return reject, info
        if reject not in _WARNING_RELAX_REASONS:
            return reject, info

        cond = entry.get("entry_conditions") or {}
        side = str(entry.get("side") or cond.get("side") or "").upper()
        symbol = entry.get("symbol")
        price_chg = _relax_sf(cond.get("max_price_change_pct"), 0.0)
        abs_chg = abs(price_chg)
        latest_volume = _relax_sf(cond.get("latest_volume"), 0.0)
        volume_3m = _relax_sf(cond.get("volume_3m"), 0.0)
        volume_5m = _relax_sf(cond.get("volume_5m"), 0.0)
        surge = _relax_sf(cond.get("max_volume_surge_ratio"), 0.0)
        slope = _relax_sf(cond.get("slope"), 0.0)

        max_warning_chg = _relax_env_float("TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT", 0.50)
        min_volume = _relax_env_float("TONOSAMA_WARNING_ONLY_MIN_VOLUME", 50000.0)
        min_surge = _relax_env_float("TONOSAMA_WARNING_ONLY_MIN_SURGE", 3.0)

        ma_ok = _relax_ma_ok(info, side=side)
        volume_ok = latest_volume >= min_volume or volume_3m >= min_volume or volume_5m >= min_volume
        surge_ok = surge >= min_surge
        direction_ok = (side == "BUY" and slope >= -0.003) or (side == "SELL" and slope <= 0.003) or side not in {"BUY", "SELL"}

        if abs_chg < max_warning_chg and volume_ok and surge_ok and ma_ok and direction_ok:
            logger.warning(
                "[TONOSAMA PENDING WARNING RELAX] allow warning-only symbol=%s side=%s original=%s price_chg=%.3f max_warning_chg=%.3f latest_volume=%.0f volume_3m=%.0f volume_5m=%.0f surge=%.2f slope=%.6f ma=%s",
                symbol, side, reject, price_chg, max_warning_chg, latest_volume,
                volume_3m, volume_5m, surge, slope, info,
            )
            return None, info

        logger.warning(
            "[TONOSAMA PENDING WARNING RELAX] keep reject symbol=%s side=%s reason=%s price_chg=%.3f volume_ok=%s surge_ok=%s ma_ok=%s direction_ok=%s",
            symbol, side, reject, price_chg, volume_ok, surge_ok, ma_ok, direction_ok,
        )
    except Exception:
        logger.exception("[TONOSAMA PENDING WARNING RELAX] wrapper failed")
    return reject, info


def _climax_reject_reason(entry: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    reject, ma_info = _climax_reject_reason_core(entry)
    if _five_sec_relax_scope_enabled():
        reject, ma_info = _five_sec_relax_stage(entry, reject, ma_info)
    if _warning_relax_scope_enabled():
        reject, ma_info = _warning_relax_stage(entry, reject, ma_info)
    return reject, ma_info


def _climax_reject_reason_core(entry: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    ma_info: dict[str, Any] = {}
    try:
        cond = entry.get("entry_conditions") or {}
        side = str(entry.get("side") or cond.get("side") or "BUY").upper()
        symbol = normalize_symbol(entry.get("symbol"))
        ma_reject, ma_info = _ranking_ma_reject_reason(symbol, side)
        if ma_reject:
            return ma_reject, ma_info

        surge = safe_float(cond.get("max_volume_surge_ratio"), 0.0)
        price_chg = safe_float(cond.get("max_price_change_pct"), 0.0)
        chg_3m = safe_float(cond.get("price_change_pct_3m"), 0.0)
        chg_5m = safe_float(cond.get("price_change_pct_5m"), 0.0)
        chg_5s = safe_float(cond.get("price_change_5s_pct"), 0.0)
        signed_body = safe_float(cond.get("signed_body_change_pct"), price_chg)
        slope = safe_float(cond.get("slope"), 0.0)
        close_pos = safe_float(cond.get("close_position_pct"), 50.0)
        upper_wick = safe_float(cond.get("upper_wick_pct"), 0.0)
        lower_wick = safe_float(cond.get("lower_wick_pct"), 0.0)
        latest_volume = safe_float(cond.get("latest_volume"), 0.0)
        up3 = int(safe_float(cond.get("prev_3m_up_streak"), 0.0))
        up5 = int(safe_float(cond.get("prev_5m_up_streak"), 0.0))
        dn3 = int(safe_float(cond.get("prev_3m_down_streak"), 0.0))
        dn5 = int(safe_float(cond.get("prev_5m_down_streak"), 0.0))

        if latest_volume < MIN_FINAL_LATEST_VOLUME:
            return "latest_volume_low_final_guard", ma_info

        reject_zero_5sec, min_final_5sec = _effective_zero_5sec_guard()
        if reject_zero_5sec and _has_valid_5sec_for_zero_guard(cond) and abs(chg_5s) < min_final_5sec:
            return "five_sec_stopped_final_guard", ma_info

        if side == "BUY":
            buy_like = (price_chg > 0) or (signed_body > 0) or (slope > 0)
            if chg_5m < MIN_BUY_5M_CHANGE_FINAL:
                return "buy_5m_reverse_final_guard", ma_info
            if chg_3m < 0:
                return "buy_3m_reverse_final_guard", ma_info
            if max(up3, up5) > MAX_BUY_PREV_3M5M_UP_STREAK:
                return "buy_after_3m5m_up_streak_guard", ma_info
            if buy_like and price_chg >= MAX_BUY_PRICE_CHANGE_PCT:
                return "buy_price_chase_too_late", ma_info
            if buy_like and close_pos >= MAX_BUY_CLOSE_POSITION_PCT and price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT:
                return "buying_climax_high_zone", ma_info
            if buy_like and upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT:
                return "buying_climax_upper_wick_reversal", ma_info
            if buy_like and surge >= BUYING_CLIMAX_MIN_SURGE_RATIO and upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= 60.0:
                return "buying_climax_upper_wick_warning", ma_info
            if buy_like and surge >= BUYING_CLIMAX_MIN_SURGE_RATIO and (
                (price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT and close_pos >= MAX_BUY_CLOSE_POSITION_PCT)
                or (upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT)
            ):
                return "buying_climax_or_high_chase_guard", ma_info

        if side == "SELL":
            drop_abs = abs(price_chg)
            sell_like = (price_chg < 0) or (signed_body < 0) or (slope < 0)
            if chg_5m > MAX_SELL_5M_CHANGE_FINAL:
                return "sell_5m_reverse_final_guard", ma_info
            if chg_3m > 0:
                return "sell_3m_reverse_final_guard", ma_info
            if max(dn3, dn5) > MAX_SELL_PREV_3M5M_DOWN_STREAK:
                return "sell_after_3m5m_down_streak_guard", ma_info
            if sell_like and drop_abs >= MAX_SELL_PRICE_DROP_PCT:
                return "sell_price_chase_too_late", ma_info
            if sell_like and close_pos <= MIN_SELL_CLOSE_POSITION_PCT and drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT:
                return "selling_climax_low_zone", ma_info
            if sell_like and lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT:
                return "selling_climax_lower_wick_reversal", ma_info
            if sell_like and surge >= SELLING_CLIMAX_MIN_SURGE_RATIO and lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= 40.0:
                return "selling_climax_lower_wick_warning", ma_info
            if sell_like and surge >= SELLING_CLIMAX_MIN_SURGE_RATIO and (
                (drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT and close_pos <= MIN_SELL_CLOSE_POSITION_PCT)
                or (lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT)
            ):
                return "selling_climax_or_low_chase_guard", ma_info
    except Exception:
        logger.debug("[TONOSAMA PENDING GUARD] climax/liquidity/ma check failed", exc_info=True)
    return None, ma_info


def build_pending_entry(row: pd.Series, *, final_score: float, ai_prob: float, ai_reason: str) -> dict[str, Any]:
    now = dt.datetime.now()
    expire_at = now + dt.timedelta(seconds=TONOSAMA_EXPIRE_SEC)
    symbol = normalize_symbol(row.get("symbol"))
    side = _infer_side_from_row(row)
    conditions = _entry_conditions_from_row(row, ai_reason=ai_reason, side=side, expire_at=expire_at)
    final = safe_float(final_score, 0.0)
    return {
        "symbol": symbol,
        "symbolname": str(row.get("symbolname", "")),
        "side": side,
        "source": "TONOSAMA",
        "entry_type": "TONOSAMA",
        "price": safe_float(row.get("close"), 0.0),
        "raw_score": safe_float(row.get("_tonosama_score"), 0.0),
        "final_score": final,
        "display_score": final,
        "score": final if side == "BUY" else -abs(final),
        "score_buy": final if side == "BUY" else 0.0,
        "score_sell": final if side == "SELL" else 0.0,
        "ai_prob": safe_float(ai_prob, 0.0),
        "expire_at": expire_at,
        "entry_conditions": conditions,
        "created_at": now,
    }


# ============================================================
# PENDING/Discordアラート直前の最終流動性ガード
# (旧 core/startup/tonosama_alert_liquidity_guard_patch.py)
# ------------------------------------------------------------
# 発注最終ガードとは別に、出来高・売買代金が少ない銘柄の通知だけ先に
# 出てしまう問題を防ぐ。add_tonosama_pending() の最初に判定し、
# reject の場合は元の add_pending 処理 (climax reject 等) を一切
# 実行せずに False を返す。
# ============================================================
def _alert_liq_max_num(*values: Any) -> float:
    m = 0.0
    for v in values:
        try:
            m = max(m, safe_float(v, 0.0))
        except Exception:
            pass
    return float(m)


def _alert_liq_should_reject(entry: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Return (reject, reason, detail)."""
    if not _env_bool("TONOSAMA_ALERT_LIQ_GUARD", True):
        return False, "disabled", {}
    if not _is_tonosama_entry(entry):
        return False, "not_tonosama", {}

    cond = entry.get("entry_conditions") or {}
    if not isinstance(cond, dict):
        cond = {}

    min_latest_volume = _env_float("TONOSAMA_ALERT_MIN_LATEST_VOLUME", _env_float("ENTRY_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("TONOSAMA_ALERT_MIN_TURNOVER", _env_float("ENTRY_MIN_TURNOVER", 10000000.0))
    min_recent_bar_volume = _env_float("TONOSAMA_ALERT_MIN_RECENT_BAR_VOLUME", min_latest_volume)
    reject_missing = _env_bool("TONOSAMA_ALERT_REJECT_VOLUME_MISSING", True)

    price = _alert_liq_max_num(entry.get("price"), cond.get("close"), cond.get("close_price"), cond.get("price"))
    latest_volume = _alert_liq_max_num(
        entry.get("volume"),
        entry.get("latest_volume"),
        cond.get("latest_volume"),
        cond.get("volume"),
        cond.get("_latest_volume"),
    )
    volume_3m = _alert_liq_max_num(cond.get("volume_3m"), entry.get("volume_3m"))
    volume_5m = _alert_liq_max_num(cond.get("volume_5m"), entry.get("volume_5m"))
    recent_bar_volume = max(latest_volume, volume_3m, volume_5m)
    turnover = _alert_liq_max_num(
        entry.get("turnover"),
        entry.get("trading_value"),
        cond.get("turnover"),
        cond.get("turnover_raw"),
        cond.get("trading_value"),
        cond.get("売買代金"),
    )
    if turnover <= 0 and price > 0 and recent_bar_volume > 0:
        turnover = price * recent_bar_volume

    detail = {
        "symbol": entry.get("symbol"),
        "side": entry.get("side"),
        "price": price,
        "latest_volume": latest_volume,
        "volume_3m": volume_3m,
        "volume_5m": volume_5m,
        "recent_bar_volume": recent_bar_volume,
        "turnover": turnover,
        "min_latest_volume": min_latest_volume,
        "min_recent_bar_volume": min_recent_bar_volume,
        "min_turnover": min_turnover,
    }

    if reject_missing and latest_volume <= 0 and volume_3m <= 0 and volume_5m <= 0:
        return True, "alert_volume_missing", detail
    if latest_volume < min_latest_volume:
        return True, "alert_latest_volume_low", detail
    if recent_bar_volume < min_recent_bar_volume:
        return True, "alert_recent_bar_volume_low", detail
    if turnover < min_turnover:
        return True, "alert_turnover_low", detail
    return False, "ok", detail


def _add_tonosama_pending_core(entry: dict[str, Any]) -> bool:
    reject, reason, detail = _alert_liq_should_reject(entry)
    if reject:
        logger.warning(
            "[TONOSAMA ALERT LIQ GUARD] reject symbol=%s side=%s reason=%s latest_volume=%.0f volume_3m=%.0f volume_5m=%.0f turnover=%.0f min_latest=%.0f min_recent=%.0f min_turnover=%.0f price=%.1f",
            detail.get("symbol"),
            detail.get("side"),
            reason,
            safe_float(detail.get("latest_volume"), 0.0),
            safe_float(detail.get("volume_3m"), 0.0),
            safe_float(detail.get("volume_5m"), 0.0),
            safe_float(detail.get("turnover"), 0.0),
            safe_float(detail.get("min_latest_volume"), 0.0),
            safe_float(detail.get("min_recent_bar_volume"), 0.0),
            safe_float(detail.get("min_turnover"), 0.0),
            safe_float(detail.get("price"), 0.0),
        )
        return False
    try:
        prune_expired_tonosama_pending(entry.get("symbol"), reason="TONOSAMA_BEFORE_ADD_EXPIRED")
        reject, ma_info = _climax_reject_reason(entry)
        if reject:
            cond = entry.get("entry_conditions") or {}
            logger.warning(
                "[TONOSAMA PENDING GUARD] reject symbol=%s side=%s reason=%s feature_dt=%s five_sec_dt=%s five_sec_dt_valid=%s price_chg=%.3f chg3m=%.3f chg5m=%.3f chg5s=%.3f surge=%.2f latest_volume=%.0f min_volume=%.0f volume_3m=%.0f volume_5m=%.0f close_pos=%.1f upper_wick=%.1f lower_wick=%.1f up3=%s up5=%s dn3=%s dn5=%s slope=%.6f ranking_ma=%s",
                entry.get("symbol"), entry.get("side"), reject,
                cond.get("feature_dt"), cond.get("five_sec_dt"), cond.get("five_sec_dt_valid"),
                safe_float(cond.get("max_price_change_pct"), 0.0),
                safe_float(cond.get("price_change_pct_3m"), 0.0),
                safe_float(cond.get("price_change_pct_5m"), 0.0),
                safe_float(cond.get("price_change_5s_pct"), 0.0),
                safe_float(cond.get("max_volume_surge_ratio"), 0.0),
                safe_float(cond.get("latest_volume"), 0.0),
                MIN_FINAL_LATEST_VOLUME,
                safe_float(cond.get("volume_3m"), 0.0),
                safe_float(cond.get("volume_5m"), 0.0),
                safe_float(cond.get("close_position_pct"), 50.0),
                safe_float(cond.get("upper_wick_pct"), 0.0),
                safe_float(cond.get("lower_wick_pct"), 0.0),
                int(safe_float(cond.get("prev_3m_up_streak"), 0.0)),
                int(safe_float(cond.get("prev_5m_up_streak"), 0.0)),
                int(safe_float(cond.get("prev_3m_down_streak"), 0.0)),
                int(safe_float(cond.get("prev_5m_down_streak"), 0.0)),
                safe_float(cond.get("slope"), 0.0),
                ma_info,
            )
            return False
        return bool(add_pending(entry))
    except Exception:
        logger.exception("[TONOSAMA ENTRY] add_pending failed symbol=%s", entry.get("symbol"))
        return False


# --- tonosama_pending_candidate_audit_patch (native化, 元 core/startup/tonosama_pending_candidate_audit_patch.py) ---
# TONOSAMA pending entry が受理された時に、entry_conditions を含むフルスナップショットを
# audit.candidate_history へ記録する (fail-safe: 監査ログ書き込み失敗は発注を止めない)。
_PENDING_AUDIT_VERSION = "V1-TONOSAMA-PENDING-CANDIDATE-AUDIT"
_PENDING_AUDIT_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _pending_audit_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _PENDING_AUDIT_TRUE
    except Exception:
        return bool(default)


def _pending_audit_norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _pending_audit_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _pending_audit_json_default(v: Any):
    try:
        if isinstance(v, pd.Timestamp):
            if pd.isna(v):
                return None
            return v.to_pydatetime().isoformat()
    except Exception:
        pass
    try:
        import numpy as np  # type: ignore
        if isinstance(v, np.generic):
            return v.item()
    except Exception:
        pass
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    return str(v)


def _pending_audit_snapshot_for_entry(entry: dict[str, Any]) -> str:
    cond = entry.get("entry_conditions") or {}
    if not isinstance(cond, dict):
        cond = {"entry_conditions_raw": str(cond)}

    payload: dict[str, Any] = {
        "audit_kind": "TONOSAMA_PENDING_CANDIDATE",
        "version": _PENDING_AUDIT_VERSION,
        "source": "TONOSAMA",
        "entry_type": entry.get("entry_type") or "TONOSAMA",
        "symbol": _pending_audit_norm_symbol(entry.get("symbol")),
        "symbolname": entry.get("symbolname"),
        "side": str(entry.get("side") or cond.get("side") or "").upper(),
        "price": _pending_audit_safe_float(entry.get("price"), 0.0),
        "raw_score": _pending_audit_safe_float(entry.get("raw_score"), 0.0),
        "final_score": _pending_audit_safe_float(entry.get("final_score") or entry.get("score"), 0.0),
        "display_score": _pending_audit_safe_float(entry.get("display_score") or entry.get("final_score"), 0.0),
        "score": _pending_audit_safe_float(entry.get("score"), 0.0),
        "score_buy": _pending_audit_safe_float(entry.get("score_buy"), 0.0),
        "score_sell": _pending_audit_safe_float(entry.get("score_sell"), 0.0),
        "ai_prob": _pending_audit_safe_float(entry.get("ai_prob"), 0.0),
        "ai_reason": cond.get("ai_reason"),
        "reason": cond.get("reason"),
        "reason_code": cond.get("reason_code"),
        "created_at": entry.get("created_at"),
        "expire_at": entry.get("expire_at") or cond.get("expire_at"),
        "entry_conditions": cond,
        # Flat aliases for easy SQL/JSON inspection.
        "feature_dt": cond.get("feature_dt"),
        "five_sec_dt": cond.get("five_sec_dt"),
        "five_sec_dt_valid": cond.get("five_sec_dt_valid"),
        "surge_tf": cond.get("surge_tf"),
        "volume_surge_ratio_3m": cond.get("volume_surge_ratio_3m"),
        "volume_surge_ratio_5m": cond.get("volume_surge_ratio_5m"),
        "max_volume_surge_ratio": cond.get("max_volume_surge_ratio"),
        "price_change_pct_3m": cond.get("price_change_pct_3m"),
        "price_change_pct_5m": cond.get("price_change_pct_5m"),
        "max_price_change_pct": cond.get("max_price_change_pct"),
        "price_change_5s_pct": cond.get("price_change_5s_pct"),
        "latest_volume": cond.get("latest_volume"),
        "volume_3m": cond.get("volume_3m"),
        "volume_5m": cond.get("volume_5m"),
        "has_5sec_bar": cond.get("has_5sec_bar"),
        "latest_5sec_volume": cond.get("latest_5sec_volume"),
        "volume_surge_ratio_5s": cond.get("volume_surge_ratio_5s"),
        "is_5sec_confirm_ok": cond.get("is_5sec_confirm_ok"),
        "prev_3m_up_streak": cond.get("prev_3m_up_streak"),
        "prev_5m_up_streak": cond.get("prev_5m_up_streak"),
        "prev_3m_down_streak": cond.get("prev_3m_down_streak"),
        "prev_5m_down_streak": cond.get("prev_5m_down_streak"),
        "close_position_pct": cond.get("close_position_pct"),
        "upper_wick_pct": cond.get("upper_wick_pct"),
        "lower_wick_pct": cond.get("lower_wick_pct"),
        "slope": cond.get("slope"),
        "rsi": cond.get("rsi"),
        "macd": cond.get("macd"),
        "signal": cond.get("signal"),
        "mtf": cond.get("mtf"),
        "score_mtf": cond.get("score_mtf"),
    }
    text = json.dumps(payload, ensure_ascii=False, default=_pending_audit_json_default, separators=(",", ":"))
    max_len = int(float(os.environ.get("TONOSAMA_AUDIT_TECHNICAL_SNAPSHOT_MAX_CHARS", "20000")))
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len] + "...TRUNCATED"
    return text


def _pending_audit_record_tonosama_candidate(entry: dict[str, Any]) -> None:
    try:
        if not isinstance(entry, dict):
            return
        source = str(entry.get("source") or "").strip().upper()
        entry_type = str(entry.get("entry_type") or "").strip().upper()
        if source != "TONOSAMA" and entry_type != "TONOSAMA":
            return
        cond = entry.get("entry_conditions") or {}
        if not isinstance(cond, dict):
            cond = {}
        from trading.audit_logging.recorder import record_candidate_event

        side = str(entry.get("side") or cond.get("side") or "").upper()
        final_score = _pending_audit_safe_float(entry.get("final_score") or entry.get("display_score") or entry.get("score"), 0.0)
        reason = str(cond.get("reason") or cond.get("ai_reason") or "TONOSAMA pending accepted")
        ai_reason = cond.get("ai_reason")
        ai_result = json.dumps(
            {
                "kind": "TONOSAMA",
                "ai_prob": _pending_audit_safe_float(entry.get("ai_prob"), 0.0),
                "ai_reason": ai_reason,
                "reason_code": cond.get("reason_code"),
            },
            ensure_ascii=False,
            default=_pending_audit_json_default,
            separators=(",", ":"),
        )
        record_candidate_event(
            datetime=dt.datetime.now().isoformat(timespec="seconds"),
            symbol=_pending_audit_norm_symbol(entry.get("symbol")),
            side=side,
            source="TONOSAMA",
            interval_min=0,
            score_buy=_pending_audit_safe_float(entry.get("score_buy"), 0.0),
            score_sell=_pending_audit_safe_float(entry.get("score_sell"), 0.0),
            score_total=final_score,
            final_score=final_score,
            ai_result=ai_result,
            reason=reason,
            technical_snapshot=_pending_audit_snapshot_for_entry(entry),
        )
        logger.info(
            "[TONOSAMA AUDIT] candidate_history recorded symbol=%s side=%s score=%s reason_code=%s",
            _pending_audit_norm_symbol(entry.get("symbol")), side, final_score, cond.get("reason_code"),
        )
    except Exception:
        logger.debug("[TONOSAMA AUDIT] record candidate failed", exc_info=True)


def _pending_audit_scope_enabled() -> bool:
    """tonosama_pending_candidate_audit_patch は core/startup/runtime_env/installer.py の
    TRADING_CONTEXTS ({"main"}) 経由でのみ install されていた (main.py プロセス以外では
    常に無効だった)。TONOSAMA_PENDING_CANDIDATE_AUDIT_SCOPE_ENABLED で明示上書き可。
    """
    try:
        if str(os.environ.get("DISABLE_TONOSAMA_PENDING_CANDIDATE_AUDIT_PATCH", "")).strip() == "1":
            return False
        raw = os.getenv("TONOSAMA_PENDING_CANDIDATE_AUDIT_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _pending_audit_env_bool("TONOSAMA_PENDING_CANDIDATE_AUDIT_SCOPE_ENABLED", False)
        argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(a.endswith("/main.py") or a == "main.py" for a in argv_list)
    except Exception:
        return False


def add_tonosama_pending(entry: dict[str, Any]) -> bool:
    if not _pending_audit_scope_enabled():
        return _add_tonosama_pending_core(entry)
    ok = False
    try:
        ok = bool(_add_tonosama_pending_core(entry))
        return ok
    finally:
        # Record only accepted pending entries, not rejected final-guard rows.
        if ok and _pending_audit_env_bool("TONOSAMA_AUDIT_RECORD_PENDING_ACCEPTED", True):
            _pending_audit_record_tonosama_candidate(entry)


__all__ = ["has_tonosama_pending", "build_pending_entry", "add_tonosama_pending", "prune_expired_tonosama_pending"]
