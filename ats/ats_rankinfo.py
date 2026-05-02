# ============================================================
# File   : ats/ats_rankinfo.py
# Version: Ver1.0-ATS-RANKINFO
# ------------------------------------------------------------
# ranking snapshot 情報の取得 / cache / 表示整形
# ============================================================

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Dict, List

import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)

_ATS_REGISTER_RANKINFO_CACHE: Dict[str, Dict[str, Any]] = {}
_ATS_REGISTER_RANKINFO_CACHE_TS: float = 0.0
_ATS_REGISTER_RANKINFO_CACHE_SEC: float = 5.0


def normalize_symbol_for_rankinfo(x: Any) -> str:
    try:
        s = str(x).strip()
    except Exception:
        return ""
    if not s:
        return ""
    if s.endswith(".0"):
        s = s[:-2].strip()
    return s


def safe_scalar(v: Any, default: Any = "") -> Any:
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except Exception:
        pass
    return v


def pick_first_nonempty(row: Dict[str, Any], keys: List[str], default: Any = "") -> Any:
    for k in keys:
        try:
            v = row.get(k, None)
        except Exception:
            v = None

        v = safe_scalar(v, default=None)
        if v is None:
            continue

        if isinstance(v, str):
            if v.strip() != "":
                return v.strip()
        else:
            return v

    return default


def normalize_rankinfo_record(info: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(info, dict):
        return {}

    rank_type = pick_first_nonempty(info, ["rank_type", "type", "ranking_type"], "")
    market = pick_first_nonempty(info, ["market", "market_type"], "")
    rank_position = pick_first_nonempty(info, ["rank_position", "rank", "順位"], "")
    current_price = pick_first_nonempty(info, ["current_price", "price", "close", "現在値"], "")
    trading_volume = pick_first_nonempty(info, ["trading_volume", "volume", "売買高"], "")
    volume_speed = pick_first_nonempty(info, ["volume_speed", "volume_spike", "出来高急増"], "")

    return {
        "rank_type": rank_type,
        "market": market,
        "market_type": market,
        "rank_position": rank_position,
        "rank": rank_position,
        "current_price": current_price,
        "price": current_price,
        "trading_volume": trading_volume,
        "volume": trading_volume,
        "volume_speed": volume_speed,
        "volume_spike": volume_speed,
    }


def get_ranking_snapshot_map_from_global() -> Dict[str, Dict[str, Any]]:
    candidates = [
        "ranking_snapshot_map",
        "ats_ranking_snapshot_map",
        "ranking_info_map",
        "ats_ranking_info_map",
    ]

    for name in candidates:
        try:
            m = getattr(global_data, name, None)
            if not isinstance(m, dict) or not m:
                continue

            out: Dict[str, Dict[str, Any]] = {}
            for k, v in m.items():
                sym = normalize_symbol_for_rankinfo(k)
                if not sym:
                    continue
                info = normalize_rankinfo_record(v if isinstance(v, dict) else {})
                if info:
                    out[sym] = info

            if out:
                logger.info("[ATS RANK INFO] using global_data map=%s symbols=%d", name, len(out))
                return out
        except Exception:
            logger.exception("ranking snapshot map get failed: %s", name)

    return {}


def build_rankinfo_map_from_ats_ranking() -> Dict[str, Dict[str, Any]]:
    try:
        from ats import ats_ranking

        if hasattr(ats_ranking, "_prepare_base_df"):
            df = ats_ranking._prepare_base_df()
        else:
            logger.warning("[ATS RANK INFO] ats_ranking._prepare_base_df not found")
            df = None

        if df is None or df.empty:
            logger.warning("[ATS RANK INFO] ats_ranking base df empty")
            return {}

        work = df.copy()
        if "symbol" not in work.columns:
            logger.warning("[ATS RANK INFO] base df missing symbol column")
            return {}

        work["symbol"] = work["symbol"].map(normalize_symbol_for_rankinfo)
        work = work[work["symbol"] != ""].copy()
        if work.empty:
            return {}

        if "snapshot_time" in work.columns:
            try:
                work["__dt__"] = pd.to_datetime(work["snapshot_time"], errors="coerce")
                work = work.sort_values("__dt__", ascending=False, kind="mergesort")
            except Exception:
                logger.exception("[ATS RANK INFO] snapshot_time sort failed")

        if "rank_position" not in work.columns and "rank" in work.columns:
            work["rank_position"] = pd.to_numeric(work["rank"], errors="coerce")
        elif "rank_position" in work.columns:
            work["rank_position"] = pd.to_numeric(work["rank_position"], errors="coerce")
        else:
            work["rank_position"] = 999999

        if "rank_type" not in work.columns:
            work["rank_type"] = ""

        if "market_type" not in work.columns and "market" in work.columns:
            work["market_type"] = work["market"]
        elif "market_type" not in work.columns:
            work["market_type"] = ""

        if "current_price" not in work.columns and "price" in work.columns:
            work["current_price"] = work["price"]
        elif "current_price" not in work.columns:
            work["current_price"] = ""

        if "trading_volume" not in work.columns and "volume" in work.columns:
            work["trading_volume"] = work["volume"]
        elif "trading_volume" not in work.columns:
            work["trading_volume"] = ""

        if "volume_speed" not in work.columns and "volume_spike" in work.columns:
            work["volume_speed"] = work["volume_spike"]
        elif "volume_speed" not in work.columns:
            work["volume_speed"] = ""

        try:
            work["__rankpos__"] = pd.to_numeric(work["rank_position"], errors="coerce").fillna(999999)
            sort_cols = ["__rankpos__"]
            ascending = [True]
            if "__dt__" in work.columns:
                sort_cols.append("__dt__")
                ascending.append(False)
            work = work.sort_values(sort_cols, ascending=ascending, kind="mergesort")
        except Exception:
            logger.exception("[ATS RANK INFO] sort for best row failed")

        work = work.drop_duplicates(subset=["symbol"], keep="first").copy()

        out: Dict[str, Dict[str, Any]] = {}
        for _, row in work.iterrows():
            sym = normalize_symbol_for_rankinfo(row.get("symbol", ""))
            if not sym:
                continue

            out[sym] = {
                "rank_type": safe_scalar(row.get("rank_type", ""), ""),
                "market": safe_scalar(row.get("market_type", row.get("market", "")), ""),
                "market_type": safe_scalar(row.get("market_type", row.get("market", "")), ""),
                "rank_position": safe_scalar(row.get("rank_position", row.get("rank", "")), ""),
                "rank": safe_scalar(row.get("rank_position", row.get("rank", "")), ""),
                "current_price": safe_scalar(row.get("current_price", row.get("price", "")), ""),
                "price": safe_scalar(row.get("current_price", row.get("price", "")), ""),
                "trading_volume": safe_scalar(row.get("trading_volume", row.get("volume", "")), ""),
                "volume": safe_scalar(row.get("trading_volume", row.get("volume", "")), ""),
                "volume_speed": safe_scalar(row.get("volume_speed", row.get("volume_spike", "")), ""),
                "volume_spike": safe_scalar(row.get("volume_speed", row.get("volume_spike", "")), ""),
            }

        logger.info("[ATS RANK INFO] rebuilt symbols=%d", len(out))
        return out

    except Exception:
        logger.exception("[ATS RANK INFO] rebuild from ats_ranking failed")
        return {}


def get_ranking_snapshot_map() -> Dict[str, Dict[str, Any]]:
    global _ATS_REGISTER_RANKINFO_CACHE, _ATS_REGISTER_RANKINFO_CACHE_TS

    now = time.time()
    if (
        isinstance(_ATS_REGISTER_RANKINFO_CACHE, dict)
        and _ATS_REGISTER_RANKINFO_CACHE
        and (now - _ATS_REGISTER_RANKINFO_CACHE_TS) < _ATS_REGISTER_RANKINFO_CACHE_SEC
    ):
        try:
            return dict(_ATS_REGISTER_RANKINFO_CACHE)
        except Exception:
            logger.exception("[ATS RANK INFO] cache copy failed")

    info_map = get_ranking_snapshot_map_from_global()
    if info_map:
        _ATS_REGISTER_RANKINFO_CACHE = dict(info_map)
        _ATS_REGISTER_RANKINFO_CACHE_TS = now
        return info_map

    info_map = build_rankinfo_map_from_ats_ranking()
    if info_map:
        _ATS_REGISTER_RANKINFO_CACHE = dict(info_map)
        _ATS_REGISTER_RANKINFO_CACHE_TS = now
        try:
            global_data.ats_ranking_info_map = dict(info_map)
        except Exception:
            logger.debug("[ATS RANK INFO] global_data reflect failed", exc_info=True)
        return info_map

    return {}


def get_symbol_ranking_info(symbol: str) -> Dict[str, Any]:
    symbol = normalize_symbol_for_rankinfo(symbol)
    if not symbol:
        return {}

    info_map = get_ranking_snapshot_map()
    if not info_map:
        logger.debug("[ATS RANK INFO] info_map empty symbol=%s", symbol)
        return {}

    try:
        info = info_map.get(symbol, {})
        if isinstance(info, dict) and info:
            return info
    except Exception:
        logger.exception("symbol ranking info lookup failed: %s", symbol)

    logger.debug("[ATS RANK INFO] symbol miss symbol=%s map_size=%d", symbol, len(info_map))
    return {}


def format_symbol_ranking_info(symbol: str) -> str:
    info = get_symbol_ranking_info(symbol)

    if not info:
        return "rank_type=? market=? rank=? price=? vol=? vspd=?"

    rank_type = safe_scalar(info.get("rank_type", ""), "")
    market = safe_scalar(info.get("market", info.get("market_type", "")), "")
    rank_position = safe_scalar(info.get("rank_position", info.get("rank", "")), "")
    current_price = safe_scalar(info.get("current_price", info.get("price", "")), "")
    trading_volume = safe_scalar(info.get("trading_volume", info.get("volume", "")), "")
    volume_speed = safe_scalar(info.get("volume_speed", info.get("volume_spike", "")), "")

    return (
        f"rank_type={rank_type if rank_type not in (None, '') else '?'} "
        f"market={market if market not in (None, '') else '?'} "
        f"rank={rank_position if rank_position not in (None, '') else '?'} "
        f"price={current_price if current_price not in (None, '') else '?'} "
        f"vol={trading_volume if trading_volume not in (None, '') else '?'} "
        f"vspd={volume_speed if volume_speed not in (None, '') else '?'}"
    )