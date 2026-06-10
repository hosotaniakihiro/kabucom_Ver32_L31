# ============================================================
# File   : core/startup/ranking_entry_budget_hard_stop_v6_patch.py
# Version: V1-RANKING-BUDGET-HARD-STOP-LIGHTWEIGHT-SCORING
# ------------------------------------------------------------
# Purpose:
#   The existing ranking_entry_fast_runtime_patch checks the runtime
#   budget only between major operations.  When technical attach/filter
#   or ranking filters become slow under SQLite/NAS lock pressure, the
#   job can run for 100s+ and still create 0 pending entries.
#
# Fix:
#   - Force a tiny prefilter universe while the system is busy.
#   - Replace technical attach with a pure in-memory merge from tech_map.
#   - Replace ranking-only score/filter with a deterministic lightweight
#     ranking score that does not require previous snapshot/history.
#   - Keep final safety/board/liquidity guards downstream intact.
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_ATTACH = None
_ORIG_SCORE = None
_ORIG_FILTER = None


def _b(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enabled"}
    except Exception:
        return bool(default)


def _f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "").replace("%", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 999999) -> int:
    try:
        return int(_safe_float(v, float(default)))
    except Exception:
        return default


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _rank_type_weight(rt: str) -> float:
    s = str(rt or "")
    if "値上" in s or "値下" in s:
        return 1.20
    if "売買代金" in s:
        return 1.12
    if "TICK" in s.upper() or "ティック" in s:
        return 1.05
    if "出来高" in s or "売買高" in s:
        return 1.00
    return 0.90


def _light_attach(row: Dict[str, Any], tech_map: Dict[str, Dict[str, Any]] | None = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Never touch DB here; merge only the supplied tech_map row."""
    try:
        out = dict(row or {})
        sym = str(out.get("symbol") or "").strip()
        item = (tech_map or {}).get(sym) if isinstance(tech_map, dict) else None
        if isinstance(item, dict):
            for k, v in item.items():
                if k not in out or out.get(k) in (None, ""):
                    out[k] = v
        if "ranking_tech_ready" not in out:
            out["ranking_tech_ready"] = 0
        out["ranking_light_attach"] = True
        return out
    except Exception:
        return dict(row or {})


def _infer_side(row: Dict[str, Any]) -> str:
    s = str(row.get("side") or row.get("entry_decision") or "").upper().strip()
    if s in {"BUY", "SELL"}:
        return s
    rt = str(row.get("rank_type") or "")
    if "値下" in rt or "下落" in rt:
        return "SELL"
    return "SELL" if _safe_float(row.get("day_change_pct"), 0.0) < 0 else "BUY"


def _light_score(row: Dict[str, Any], side: str, prev_price: float = 0.0, prev_rank: int = 999999, consecutive: int = 1) -> Tuple[float, Dict[str, float]]:
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    price = _safe_float(row.get("current_price") or row.get("price") or row.get("close"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume
        row["turnover"] = turnover
        row["trading_value"] = turnover
    day = _safe_float(row.get("day_change_pct"), 0.0)
    max_rank = max(1.0, _f("RANKING_ENTRY_LIGHT_MAX_RANK", 30.0))
    min_vol = max(1.0, _f("RANKING_ENTRY_LIGHT_MIN_VOLUME", 30000.0))
    min_turn = max(1.0, _f("RANKING_ENTRY_LIGHT_MIN_TURNOVER", 50000000.0))
    rank_score = _clip((max_rank - rank + 1.0) / max_rank, 0.0, 1.0) * 35.0
    turnover_score = _clip(turnover / (min_turn * 3.0), 0.0, 1.0) * 25.0
    volume_score = _clip(volume / (min_vol * 5.0), 0.0, 1.0) * 15.0
    dir_day = day if side == "BUY" else -day
    day_score = _clip(dir_day / 5.0, 0.0, 1.0) * 20.0
    type_score = _rank_type_weight(str(row.get("rank_type") or ""))
    tech_raw = _safe_float(row.get("ranking_tech_score"), 0.0)
    tech_bonus = max(0.0, tech_raw if side == "BUY" else -tech_raw) * 1.5
    score = _clip((rank_score + turnover_score + volume_score + day_score + tech_bonus) * type_score, 0.0, 100.0)
    parts = {
        "rank_score": rank_score,
        "turnover_score": turnover_score,
        "volume_score": volume_score,
        "day_direction_score": day_score,
        "ranking_tech_score": tech_raw,
        "ranking_tech_bonus": tech_bonus,
        "rank_type_weight": type_score,
        "rank_improve": float(prev_rank - rank) if prev_rank and prev_rank < 999999 else 0.0,
        "step_pct": 0.0,
        "light_scoring": 1.0,
    }
    return score, parts


def _light_filter(row: Dict[str, Any], side: str, prev_h: Dict[str, Any] | None, score: float, parts: Dict[str, float]) -> Tuple[bool, str]:
    symbol = str(row.get("symbol") or "").strip()
    price = _safe_float(row.get("current_price") or row.get("price") or row.get("close"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume
        row["turnover"] = turnover
        row["trading_value"] = turnover
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    day = _safe_float(row.get("day_change_pct"), 0.0)
    if not symbol:
        return False, "NO_SYMBOL"
    if price < _f("RANKING_ENTRY_LIGHT_MIN_PRICE", 300.0) or price > _f("RANKING_ENTRY_LIGHT_MAX_PRICE", 7000.0):
        return False, f"PRICE_RANGE_NG price={price}"
    if rank > int(_f("RANKING_ENTRY_LIGHT_MAX_RANK", 30.0)):
        return False, f"RANK_POSITION_NG rank={rank}"
    if volume < _f("RANKING_ENTRY_LIGHT_MIN_VOLUME", 30000.0):
        return False, f"VOLUME_NG volume={volume}"
    if turnover < _f("RANKING_ENTRY_LIGHT_MIN_TURNOVER", 50000000.0):
        return False, f"TURNOVER_NG turnover={turnover}"
    if side == "BUY" and day <= _f("RANKING_ENTRY_LIGHT_BUY_MIN_DAY", 0.0):
        return False, f"BUY_DAY_DIRECTION_NG day_change_pct={day:.3f}"
    if side == "SELL" and day >= _f("RANKING_ENTRY_LIGHT_SELL_MAX_DAY", 0.0):
        return False, f"SELL_DAY_DIRECTION_NG day_change_pct={day:.3f}"
    min_score = _f("RANKING_ENTRY_LIGHT_MIN_SCORE", 50.0)
    if score < min_score:
        return False, f"SCORE_NG score={score:.2f} min={min_score:.2f}"
    # Do not require previous snapshot or slope here. Final entry safety, board,
    # direction, low-move and liquidity guards still run after pending creation.
    return True, "OK_LIGHT_FAILOPEN"


def install() -> bool:
    global _INSTALLED, _ORIG_ATTACH, _ORIG_SCORE, _ORIG_FILTER
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", "12")
        os.environ.setdefault("RANKING_ENTRY_FAST_MAX_SYMBOLS", "12")
        os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PER_SIDE", "8")
        os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PER_TYPE", "6")
        os.environ.setdefault("RANKING_ENTRY_RUNTIME_BUDGET_SEC", "12")
        os.environ.setdefault("RANKING_ENTRY_MAX_PENDING_PER_RUN", "3")
        os.environ.setdefault("RANKING_ENTRY_SKIP_TECH_SAVE", "1")
        os.environ.setdefault("RANKING_ENTRY_TECH_READONLY", "1")
        os.environ.setdefault("RANKING_ENTRY_TECH_READ_BATCH_SIZE", "12")
        os.environ.setdefault("RANKING_ENTRY_LIGHT_MIN_SCORE", "50")
        os.environ.setdefault("RANKING_ENTRY_LIGHT_MIN_TURNOVER", "50000000")

        import trading.ranking.entry_from_ranking as efr
        patched = []
        cur_attach = getattr(efr, "attach_ranking_technicals", None)
        if callable(cur_attach) and not getattr(cur_attach, "_ranking_budget_hard_stop_v1", False):
            _ORIG_ATTACH = cur_attach
            _light_attach._ranking_budget_hard_stop_v1 = True  # type: ignore[attr-defined]
            efr.attach_ranking_technicals = _light_attach
            patched.append("entry_from_ranking.attach_ranking_technicals_light")
        cur_score = getattr(efr, "_calc_ranking_only_score", None)
        if callable(cur_score) and not getattr(cur_score, "_ranking_budget_hard_stop_v1", False):
            _ORIG_SCORE = cur_score
            _light_score._ranking_budget_hard_stop_v1 = True  # type: ignore[attr-defined]
            efr._calc_ranking_only_score = _light_score
            patched.append("entry_from_ranking._calc_ranking_only_score_light")
        cur_filter = getattr(efr, "_passes_ranking_only_filters", None)
        if callable(cur_filter) and not getattr(cur_filter, "_ranking_budget_hard_stop_v1", False):
            _ORIG_FILTER = cur_filter
            _light_filter._ranking_budget_hard_stop_v1 = True  # type: ignore[attr-defined]
            efr._passes_ranking_only_filters = _light_filter
            patched.append("entry_from_ranking._passes_ranking_only_filters_light")
        try:
            import trading.ranking.ranking_technical_store as store
            cur_store_attach = getattr(store, "attach_ranking_technicals", None)
            if callable(cur_store_attach) and not getattr(cur_store_attach, "_ranking_budget_hard_stop_v1", False):
                store.attach_ranking_technicals = _light_attach
                patched.append("ranking_technical_store.attach_ranking_technicals_light")
        except Exception:
            pass
        _INSTALLED = True
        logger.warning(
            "[RANKING BUDGET HARD STOP] installed v1 patched=%s rows=%s budget=%s min_score=%s min_turnover=%s",
            patched,
            os.getenv("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS"),
            os.getenv("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            os.getenv("RANKING_ENTRY_LIGHT_MIN_SCORE"),
            os.getenv("RANKING_ENTRY_LIGHT_MIN_TURNOVER"),
        )
        return True
    except Exception:
        logger.exception("[RANKING BUDGET HARD STOP] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING BUDGET HARD STOP] auto install failed")

__all__ = ["install"]
