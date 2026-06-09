# ============================================================
# File   : core/startup/ranking_entry_snapshot_technical_bridge_patch.py
# Version: V1-RANKING-SNAPSHOT-TECH-BRIDGE
# ------------------------------------------------------------
# 目的:
#   PUSH summary history が main.py 側で空の場合でも、ranking_snapshot_1min
#   に保存済みの ma/atr/slope/macd/rsi 等の technical 列を Ranking entry の
#   pending / entry_row に直接流す。
#
# 背景ログ:
#   [SUMMARY HISTORY GET] tf=1 source=push rows=0
#   [RANKING ENTRY FIX] technical fallback none base=0 symbols=20 missing=20
#   -> ATR_1M_FILTER_NG / no_momentum / ma_missing で全落ち。
#
# 方針:
#   - trading.ranking.entry_from_ranking.attach_ranking_technicals をwrap。
#   - row内の ma5_1m / atr_1m / ma5_3m / ma5_5m などを unsuffixed alias
#     ma5 / atr / slope / macd ... に反映。
#   - build_entry_row もwrapし、出力entry_rowにtechnical列をコピー。
#   - ranking_entry_fast_runtime_patch が後からwrapしても再install可能。
# ============================================================
from __future__ import annotations

import logging
import math
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False

_BASE_TECH = (
    "ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "atr",
    "slope", "slope_pct", "slope_atr_scaled", "price_change_pct",
    "volume_sma5", "volume_sma25", "volume_ratio5", "technical_ready",
)
_EXTRA_COPY = (
    "open", "high", "low", "close", "price", "current_price", "volume", "turnover",
    "ranking_tech_source", "ranking_tech_reason", "ranking_tech_datetime",
)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip().replace(",", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return float(default)
        x = float(s)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _has_value(v: Any) -> bool:
    try:
        if v is None:
            return False
        if str(v).strip() == "":
            return False
        return math.isfinite(float(str(v).replace(",", ""))) if str(v).replace(",", "").replace(".", "", 1).replace("-", "", 1).isdigit() else True
    except Exception:
        return True


def _first(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        try:
            if k in row and _has_value(row.get(k)):
                return row.get(k)
        except Exception:
            pass
    return None


def _choose_tf(row: dict[str, Any]) -> int:
    """Entry controller の1分ATR/MAガードには1mを優先する。なければ3m/5m。"""
    for tf in (1, 3, 5):
        if _sf(row.get(f"atr_{tf}m"), 0.0) > 0 or _sf(row.get(f"ma5_{tf}m"), 0.0) > 0:
            return tf
    return 1


def _copy_snapshot_tech(row: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    if not isinstance(row, dict) or not row:
        return row, 0, 1
    out = dict(row)
    tf = _choose_tf(out)
    copied = 0

    # unsuffixed aliases: downstream guards often read ma5/atr/slope directly.
    for base in _BASE_TECH:
        src = f"{base}_{tf}m"
        if src in out and _has_value(out.get(src)):
            # 既存値が0/Noneならsnapshot値で補完。既存値が有効なら尊重。
            if base not in out or not _has_value(out.get(base)) or _sf(out.get(base), 0.0) == 0.0:
                out[base] = out.get(src)
                copied += 1
        # 1m/3m/5mの元列も保持
        for t in (1, 3, 5):
            k = f"{base}_{t}m"
            if k in out and _has_value(out.get(k)):
                out[k] = out.get(k)

    # price aliases
    close = _first(out, "close", "close_1m", "price", "current_price", "close_price")
    if close is not None:
        for k in ("close", "price", "current_price", "close_price"):
            if k not in out or not _has_value(out.get(k)) or _sf(out.get(k), 0.0) == 0.0:
                out[k] = close
                copied += 1

    # high/low/open はランキング snapshot の high/low patch や technical列から補完。
    for base in ("open", "high", "low"):
        v = _first(out, base, f"{base}_1m", f"{base}_{tf}m", f"{base}_price")
        if v is not None and (base not in out or not _has_value(out.get(base)) or _sf(out.get(base), 0.0) == 0.0):
            out[base] = v
            copied += 1

    # MTF/score系の明示。AI gate failopenや低変動ガードで参照されることがある。
    slope1 = _sf(out.get("slope_1m") or out.get("slope"), 0.0)
    slope3 = _sf(out.get("slope_3m"), 0.0)
    slope5 = _sf(out.get("slope_5m"), 0.0)
    side = str(out.get("side") or out.get("entry_decision") or "").upper()
    aligned = 0
    if side == "SELL":
        aligned = int(slope1 < 0) + int(slope3 < 0) + int(slope5 < 0)
    elif side == "BUY":
        aligned = int(slope1 > 0) + int(slope3 > 0) + int(slope5 > 0)
    if aligned > 0:
        out.setdefault("mtf", float(aligned))
        out.setdefault("score_mtf", float(aligned))
        out.setdefault("mtf_score", float(aligned))
        copied += 1

    ready = any(_sf(out.get(f"atr_{t}m"), 0.0) > 0 or _sf(out.get(f"ma5_{t}m"), 0.0) > 0 for t in (1, 3, 5))
    if ready:
        out["ranking_tech_ready"] = True
        out["technical_ready"] = True
        out["ranking_tech_source"] = out.get("ranking_tech_source") or "ranking_snapshot_1min"
        out["ranking_tech_reason"] = out.get("ranking_tech_reason") or f"snapshot_technical_bridge_tf={tf}m"
        copied += 1

    return out, copied, tf


def _patch_entry_from_ranking() -> bool:
    import trading.ranking.entry_from_ranking as efr
    patched = False

    cur_attach = getattr(efr, "attach_ranking_technicals", None)
    if callable(cur_attach) and not getattr(cur_attach, "_snapshot_technical_bridge_v1", False):
        orig_attach = cur_attach

        @wraps(orig_attach)
        def attach_wrapper(row: dict[str, Any], tech_map: dict[str, dict[str, Any]] | None = None):
            try:
                ret = orig_attach(row, tech_map)
            except Exception:
                logger.debug("[RANKING SNAPSHOT TECH BRIDGE] original attach failed; use raw row", exc_info=True)
                ret = row
            try:
                out, copied, tf = _copy_snapshot_tech(ret if isinstance(ret, dict) else dict(ret or {}))
                if copied > 0:
                    logger.info("[RANKING SNAPSHOT TECH BRIDGE] attached symbol=%s copied=%s tf=%sm atr=%s ma5=%s slope=%s", out.get("symbol"), copied, tf, out.get("atr"), out.get("ma5"), out.get("slope"))
                return out
            except Exception:
                logger.exception("[RANKING SNAPSHOT TECH BRIDGE] attach bridge failed")
                return ret

        attach_wrapper._snapshot_technical_bridge_v1 = True  # type: ignore[attr-defined]
        attach_wrapper._original = orig_attach  # type: ignore[attr-defined]
        efr.attach_ranking_technicals = attach_wrapper
        patched = True

    cur_builder = getattr(efr, "build_entry_row", None)
    if callable(cur_builder) and not getattr(cur_builder, "_snapshot_technical_bridge_v1", False):
        orig_builder = cur_builder

        @wraps(orig_builder)
        def build_entry_row_wrapper(row: dict[str, Any], *args: Any, **kwargs: Any):
            bridged, copied, tf = _copy_snapshot_tech(row if isinstance(row, dict) else dict(row or {}))
            entry = orig_builder(bridged, *args, **kwargs)
            if isinstance(entry, dict):
                # downstream guards read both unsuffixed and suffixed technical columns.
                for base in _BASE_TECH:
                    for key in (base, f"{base}_1m", f"{base}_3m", f"{base}_5m"):
                        if key in bridged and _has_value(bridged.get(key)):
                            entry[key] = bridged.get(key)
                for key in _EXTRA_COPY:
                    if key in bridged and _has_value(bridged.get(key)):
                        entry[key] = bridged.get(key)
                entry.setdefault("ranking_tech_ready", bridged.get("ranking_tech_ready", False))
                entry.setdefault("technical_ready", bridged.get("technical_ready", False))
                entry.setdefault("ranking_tech_source", bridged.get("ranking_tech_source", "ranking_snapshot_1min" if copied else ""))
                entry.setdefault("ranking_tech_reason", bridged.get("ranking_tech_reason", f"snapshot_technical_bridge_tf={tf}m" if copied else ""))
                if copied > 0:
                    logger.info("[RANKING SNAPSHOT TECH BRIDGE] build_entry_row copied symbol=%s copied=%s tf=%sm", entry.get("symbol") or bridged.get("symbol"), copied, tf)
            return entry

        build_entry_row_wrapper._snapshot_technical_bridge_v1 = True  # type: ignore[attr-defined]
        build_entry_row_wrapper._original = orig_builder  # type: ignore[attr-defined]
        efr.build_entry_row = build_entry_row_wrapper
        patched = True

    return patched or bool(getattr(getattr(efr, "attach_ranking_technicals", None), "_snapshot_technical_bridge_v1", False))


def install() -> bool:
    global _INSTALLED
    if not _env_bool("RANKING_SNAPSHOT_TECH_BRIDGE_ENABLED", True):
        logger.warning("[RANKING SNAPSHOT TECH BRIDGE] disabled by env")
        return False
    try:
        os.environ.setdefault("RANKING_SNAPSHOT_TECH_BRIDGE_ENABLED", "1")
        ok = _patch_entry_from_ranking()
        _INSTALLED = bool(ok)
        logger.warning("[RANKING SNAPSHOT TECH BRIDGE] installed v1 ok=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[RANKING SNAPSHOT TECH BRIDGE] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[RANKING SNAPSHOT TECH BRIDGE] auto install failed")

__all__ = ["install"]
