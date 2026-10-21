# ============================================================
# File   : trading/ranking/ranking_flat_price_guard.py
# ------------------------------------------------------------
# ランキング由来エントリーの「前回と同値 (flat price)」による過剰除外を救済する。
#
# 旧 core/startup/ranking_entry_flat_price_guard_patch.py V1.4 が
# trading/ranking/entry_from_ranking._passes_ranking_only_filters を
# ラップして行っていた処理を本文側へ移設した。
#
# 内容 (旧パッチのまま):
#   - volume/turnover の単位を先に補修してから本来のフィルタへ渡す
#   - NG かつ flat price が原因のときだけ、prev_h の last_price を
#     わずかにずらして再判定し、通ればランク根拠で許可する
#   - 再帰した場合は FLAT_PRICE_FILTER_RECURSION で打ち切る
#
# 旧パッチは _get_ranking_source_df も差し替えていたが、
# ranking_entry_source_db_fallback_patch が cur._original (本体) を掴んで
# バイパスしており実効ゼロだったため移設していない。
#
# 旧パッチは sitecustomize BACKGROUND / main.py 経由 = main.py 専用だったため、
# flat_price_guard_enabled() で同じスコープを保つ。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_IN_FILTER = False

_PATCHED = False

_ORIGINAL_FILTER = None

_ORIGINAL_GET_RANKING_SOURCE_DF = None

_IN_FILTER = False

_LOG_NORMALIZE_COUNT = 0

def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)

def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)

def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", "").replace("%", ""))
        return float(default) if x != x else x
    except Exception:
        return float(default)

def _i(v: Any, default: int = 999999) -> int:
    try:
        return int(float(v))
    except Exception:
        return int(default)

_NORMALIZE_CACHE: dict[tuple, dict] = {}
_LOGGED_NORMALIZE_KEYS: set[tuple] = set()

def _get_cfg() -> dict:
    try:
        from config.ranking_entry_config import RANKING_ENTRY_CONFIG
        return RANKING_ENTRY_CONFIG
    except Exception:
        return {}

def _normalize_key(row: Dict[str, Any], price: float, volume: float, turnover: float) -> tuple[str, float, float, float]:
    return (str(row.get("symbol") or "").strip(), round(float(price), 4), round(float(volume), 4), round(float(turnover), 4))

def _should_log_normalize(key: tuple[str, float, float, float]) -> bool:
    global _LOG_NORMALIZE_COUNT
    if _env_bool("RANKING_FLAT_PRICE_PATCH_LOG_NORMALIZE", False):
        return True
    limit = _env_int("RANKING_FLAT_PRICE_PATCH_LOG_FIRST_N", 0)
    if limit <= 0 or key in _LOGGED_NORMALIZE_KEYS:
        return False
    _LOGGED_NORMALIZE_KEYS.add(key)
    _LOG_NORMALIZE_COUNT += 1
    return _LOG_NORMALIZE_COUNT <= limit


def _repair_volume_units(row: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _get_cfg()
    vol_cfg = cfg.get("VOLUME", {}) if isinstance(cfg, dict) else {}
    min_volume = _f(vol_cfg.get("MIN_VOLUME", 30000), 30000)
    min_turnover = _f(vol_cfg.get("MIN_TURNOVER", 10000000), 10000000)
    mul = _env_float("RANKING_ENTRY_LAST_CHANCE_VOLUME_MULTIPLIER", 1000.0)

    price = _f(row.get("price") or row.get("current_price") or row.get("close"), 0.0)
    volume = _f(row.get("volume") or row.get("trading_volume"), 0.0)
    turnover = _f(row.get("turnover") or row.get("trading_value"), 0.0)
    raw_v = volume
    raw_t = turnover
    key = _normalize_key(row, price, raw_v, raw_t)

    cached = _NORMALIZE_CACHE.get(key)
    if cached is not None:
        row.update(cached)
        return row

    if 0 < volume < min_volume and mul > 1:
        volume = volume * mul
        row["volume"] = volume
        row["trading_volume"] = volume
        row["ranking_last_chance_volume_fixed"] = True
        row["ranking_last_chance_volume_raw"] = raw_v

    implied = price * volume if price > 0 and volume > 0 else 0.0
    if implied > turnover:
        turnover = implied
    if 0 < turnover < min_turnover and price > 0 and volume > 0:
        turnover = max(turnover, implied)
    if turnover > 0:
        row["turnover"] = turnover
        row["trading_value"] = turnover

    updates = {
        "volume": row.get("volume", volume),
        "trading_volume": row.get("trading_volume", volume),
        "turnover": row.get("turnover", turnover),
        "trading_value": row.get("trading_value", turnover),
    }
    if row.get("ranking_last_chance_volume_fixed"):
        updates["ranking_last_chance_volume_fixed"] = True
        updates["ranking_last_chance_volume_raw"] = raw_v

    if len(_NORMALIZE_CACHE) >= _env_int("RANKING_FLAT_PRICE_PATCH_CACHE_MAX", 20000):
        _NORMALIZE_CACHE.clear()
        _LOGGED_NORMALIZE_KEYS.clear()
    _NORMALIZE_CACHE[key] = dict(updates)

    if (raw_v != volume or raw_t != turnover) and _should_log_normalize(key):
        logger.info(
            "[RANKING FLAT PRICE PATCH] last-chance volume normalize symbol=%s price=%s volume %s->%s turnover %s->%s min_volume=%s",
            row.get("symbol"), price, raw_v, volume, raw_t, turnover, min_volume,
        )
    return row

def _flat_price_allowed(row: Dict[str, Any], prev_h: Dict[str, Any], reason: str) -> bool:
    if not _env_bool("RANKING_ENTRY_ALLOW_FLAT_PRICE_IF_RANK_STRONG", True):
        return False
    if not (str(reason).startswith("BUY_PRICE_NOT_UP") or str(reason).startswith("SELL_PRICE_NOT_DOWN")):
        return False
    cfg = _get_cfg()
    rank_cfg = cfg.get("RANKING", {}) if isinstance(cfg, dict) else {}
    max_rank = _i(rank_cfg.get("FLAT_PRICE_ALLOW_MAX_RANK", 12), 12)
    rank = _i(row.get("rank_position"), 999999)
    prev_rank = _i((prev_h or {}).get("last_rank_position"), 999999)
    consecutive = _i((prev_h or {}).get("consecutive"), 0) + 1 if prev_h else 1
    min_consecutive = _i(rank_cfg.get("MIN_CONSECUTIVE_APPEAR", 2), 2)
    rank_not_worse = prev_rank < 999999 and rank <= prev_rank
    rank_top = rank <= max_rank
    return consecutive >= min_consecutive and (rank_not_worse or rank_top)

# ============================================================
# スコープ判定 + 公開入口
# ============================================================

_FLAT_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def flat_price_guard_enabled() -> bool:
    """このプロセスで flat price 救済を使うか。

    RANKING_FLAT_PRICE_GUARD_SCOPE_ENABLED=1/0 で明示的に上書きできる。
    """
    try:
        raw = os.getenv("RANKING_FLAT_PRICE_GUARD_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _env_bool("RANKING_FLAT_PRICE_GUARD_SCOPE_ENABLED", True)
        if _env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv_text for name in _FLAT_DB_PROCESS_NAMES)
    except Exception:
        return False


def apply_flat_price_guard(
    original_filter,
    row: Dict[str, Any],
    side: str,
    prev_h: Dict[str, Any],
    score: float,
    parts: Dict[str, float],
) -> Tuple[bool, str]:
    """本来のフィルタを呼びつつ flat price 救済を挟む。"""
    global _IN_FILTER

    def _call(r, p):
        if not callable(original_filter):
            return False, "ORIGINAL_FILTER_NOT_AVAILABLE"
        try:
            return original_filter(r, side, p, score, parts)
        except RecursionError:
            logger.warning(
                "[RANKING FLAT PRICE] original filter recursion detected symbol=%s side=%s",
                r.get("symbol"), side,
            )
            return False, "FLAT_PRICE_FILTER_RECURSION"

    if _IN_FILTER:
        # filter_rescue_patch と循環した場合は、ここで止めて上位のrescueへ返す。
        return False, "FLAT_PRICE_FILTER_RECURSION"

    _IN_FILTER = True
    try:
        row = _repair_volume_units(row)
        ok, reason = _call(row, prev_h)
        if ok:
            return ok, reason

        if _flat_price_allowed(row, prev_h or {}, reason):
            price = _f(row.get("price") or row.get("current_price"), 0.0)
            patched_prev = dict(prev_h or {})
            if price > 0:
                if str(side).upper() == "BUY":
                    patched_prev["last_price"] = price * 0.999999
                else:
                    patched_prev["last_price"] = price * 1.000001
            ok2, reason2 = _call(row, patched_prev)
            if ok2:
                if _env_bool("RANKING_FLAT_PRICE_PATCH_LOG_PASS", False):
                    logger.info(
                        "[RANKING FLAT PRICE] pass flat price symbol=%s side=%s rank=%s prev_rank=%s reason=%s",
                        row.get("symbol"), side, row.get("rank_position"),
                        (prev_h or {}).get("last_rank_position"), reason,
                    )
                return True, "OK_FLAT_PRICE_RANK_STRONG"
            return False, reason2
        return ok, reason
    finally:
        _IN_FILTER = False


__all__ = ["flat_price_guard_enabled", "apply_flat_price_guard"]
