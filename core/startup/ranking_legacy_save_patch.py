# ============================================================
# File   : core/startup/ranking_legacy_save_patch.py
# Version: V1.1-FORCE-RANKING-LEGACY-SAVE-SNAPSHOT-FALLBACK
# ------------------------------------------------------------
# Purpose:
#   ranking_raw / ranking_snapshot に加えて、ランキング種別ごとの
#   legacy テーブル（例: 値上がり率_ALL, 売買高上位_TP 等）にも
#   確実に保存されるようにする起動時パッチ。
#
# Notes:
#   trading.ranking.ranking_db_writer は legacy table 保存機能を持つが、
#   旧実装は legacy_rows を raw_rows からだけ作る。
#   そのため呼び出し側が snapshot_rows のみ渡す経路では、save_legacy=True
#   でも legacy 側が 0 件になる。
#
# V1.1:
#   - raw_rows が空の場合は snapshot_rows から legacy 保存する。
#   - writer 本体の raw/snapshot 保存には影響させず、legacy_buffer にだけ追加する。
#   - 市場別（TP/TS/TG）行は、対応する市場テーブルと *_ALL の両方へ保存する。
#   - ranking_type / market が数値・名称混在でも、既存テーブル名へ寄せる。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y", "enable", "enabled"}


def _as_dict_rows(rows: Any) -> list[dict]:
    out: list[dict] = []
    for r in rows or []:
        if isinstance(r, dict):
            out.append(dict(r))
        else:
            try:
                out.append(dict(r))
            except Exception:
                pass
    return out


def _get_row_value(row: dict, *keys: str) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v not in (None, ""):
                return v
        except Exception:
            pass
    return None


def _normalize_type(value: Any, mod: Any) -> str:
    v = str(value or "").strip()
    if not v:
        return "UNKNOWN"

    try:
        type_map = getattr(mod, "TYPE_TO_TABLE", {}) or {}
        if v.isdigit():
            return str(type_map.get(int(v), v))
        try:
            f = float(v)
            if f.is_integer():
                return str(type_map.get(int(f), v))
        except Exception:
            pass
    except Exception:
        pass
    return v


def _normalize_market(value: Any, mod: Any) -> str:
    v = str(value or "ALL").strip() or "ALL"
    try:
        divisions = getattr(mod, "EXCHANGE_DIVISIONS", {}) or {}
        if v in divisions:
            return v
        reverse = {str(name): str(code) for code, name in divisions.items()}
        if v in reverse:
            return reverse[v]
    except Exception:
        pass
    return v


def _minute_str(value: Any = None) -> str:
    if value is None or value == "":
        return dt.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.datetime):
        return value.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    try:
        s = str(value).strip().replace("T", " ")
        if "+" in s:
            s = s.split("+", 1)[0].strip()
        if s.endswith("Z"):
            s = s[:-1].strip()
        if "." in s:
            s = s.split(".", 1)[0]
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M",
            "%Y%m%d %H:%M:%S",
            "%Y%m%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y%m%d",
        ):
            try:
                return dt.datetime.strptime(s, fmt).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return dt.datetime.fromisoformat(s).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _build_legacy_rows(rows: list[dict], *, now_dt: Any, source: str, mod: Any) -> list[dict]:
    """
    writer 本体の raw/snapshot 保存には触らず、legacy_buffer 用の行だけを作る。
    市場別行は market別と ALL の両方へ入れる。
    """
    out: list[dict] = []
    batch_id = uuid.uuid4().hex
    queued_at = dt.datetime.now().isoformat(timespec="seconds")
    target_minute = _minute_str(now_dt) if now_dt is not None else None

    for src in rows:
        try:
            base = dict(src)
            rt_value = _get_row_value(base, "ranking_type", "rank_type", "category", "type", "Type", "ランキング種別")
            mk_value = _get_row_value(base, "market", "exchange", "Market", "市場")

            ranking_type = _normalize_type(rt_value, mod)
            market = _normalize_market(mk_value, mod)

            base["ranking_type"] = ranking_type
            base["rank_type"] = ranking_type
            base["category"] = ranking_type
            base["market"] = market
            base["exchange"] = market
            base.setdefault("_ranking_writer_source", source)
            base.setdefault("_ranking_writer_batch_id", batch_id)
            base.setdefault("_ranking_writer_queued_at", queued_at)
            if target_minute is not None:
                base.setdefault("datetime", target_minute)
                base.setdefault("snapshot_time", target_minute)
                base.setdefault("inserted_at", target_minute)

            out.append(base)

            # TP/TS/TG など市場別データも、ランキング種別_ALL に同時保存する。
            if market != "ALL":
                all_row = dict(base)
                all_row["market"] = "ALL"
                all_row["exchange"] = "ALL"
                out.append(all_row)
        except Exception:
            logger.debug("[RANKING LEGACY SAVE PATCH] build legacy row skipped row=%r", src, exc_info=True)
    return out


def install() -> bool:
    """
    ランキング保存時に legacy/category table 保存を有効化する。

    無効化したい場合だけ環境変数で指定:
      RANKING_LEGACY_SAVE_ENABLED=0
    """
    global _PATCHED
    if _PATCHED:
        return True

    if not _env_bool("RANKING_LEGACY_SAVE_ENABLED", True):
        logger.warning("[RANKING LEGACY SAVE PATCH] skipped by RANKING_LEGACY_SAVE_ENABLED=0")
        return False

    try:
        import trading.ranking.ranking_db_writer as mod
    except Exception:
        logger.exception("[RANKING LEGACY SAVE PATCH] import ranking_db_writer failed")
        return False

    try:
        cls = mod.RankingDBWriter
        original_method = getattr(cls, "add_ranking_rows")

        if getattr(original_method, "_legacy_save_forced", False):
            _PATCHED = True
            return True

        @wraps(original_method)
        def patched_add_ranking_rows(self: Any, *args: Any, **kwargs: Any):
            raw_rows = _as_dict_rows(kwargs.get("raw_rows"))
            snapshot_rows = _as_dict_rows(kwargs.get("snapshot_rows"))
            source = str(kwargs.get("source") or "scheduler_core")
            now_dt = kwargs.get("now_dt")

            # writer本体の legacy 作成は raw_rows 限定なので止め、ここで raw/snapshot どちらからでも作る。
            kwargs["save_legacy"] = False
            ret = original_method(self, *args, **kwargs)

            if not getattr(self, "enable_legacy_save", True):
                return ret

            legacy_source = raw_rows if raw_rows else snapshot_rows
            legacy_rows = _build_legacy_rows(legacy_source, now_dt=now_dt, source=source, mod=mod)
            if not legacy_rows:
                return ret

            with self.lock:
                self.legacy_buffer.extend(legacy_rows)
                self.total_queued_legacy += len(legacy_rows)
                legacy_buffer_len = len(self.legacy_buffer)
                try:
                    self._mark_runtime()
                except Exception:
                    pass

            logger.warning(
                "[RANKING LEGACY SAVE PATCH] queued legacy rows=%d source=%s from=%s buffer_legacy=%d",
                len(legacy_rows),
                source,
                "raw_rows" if raw_rows else "snapshot_rows",
                legacy_buffer_len,
            )

            try:
                if legacy_buffer_len >= int(getattr(self, "buffer_size", 1)) and _env_bool("RANKING_WRITER_FLUSH_ON_THRESHOLD", False):
                    self.flush()
            except Exception:
                logger.debug("[RANKING LEGACY SAVE PATCH] flush after legacy queue failed", exc_info=True)

            return ret

        patched_add_ranking_rows._legacy_save_forced = True  # type: ignore[attr-defined]
        setattr(cls, "add_ranking_rows", patched_add_ranking_rows)
    except Exception:
        logger.exception("[RANKING LEGACY SAVE PATCH] patch RankingDBWriter.add_ranking_rows failed")
        return False

    try:
        original_func = getattr(mod, "add_ranking_rows_async", None)
        if original_func is not None and not getattr(original_func, "_legacy_save_forced", False):

            @wraps(original_func)
            def patched_add_ranking_rows_async(*args: Any, **kwargs: Any):
                # class method 側で legacy を作るため、ここでは単に呼び出しを通す。
                kwargs["save_legacy"] = False
                return original_func(*args, **kwargs)

            patched_add_ranking_rows_async._legacy_save_forced = True  # type: ignore[attr-defined]
            setattr(mod, "add_ranking_rows_async", patched_add_ranking_rows_async)
    except Exception:
        logger.exception("[RANKING LEGACY SAVE PATCH] patch add_ranking_rows_async failed")
        return False

    _PATCHED = True
    logger.warning(
        "[RANKING LEGACY SAVE PATCH] installed V1.1: legacy rows are built from raw_rows or snapshot_rows and saved to market + ALL tables"
    )
    return True


__all__ = ["install"]
