# ============================================================
# File   : ats/ats_ranking/db_path.py
# Version: Ver1.1-ATS-RANKING-DB-PATH-TODAY-FIRST
# ------------------------------------------------------------
# 【概要】
#   ATSランキング候補生成で使用する ranking DB path 解決モジュール
#
# 【主な機能】
#   - 当日 rankingYYYYMMDD.db を最優先で返す
#   - 当日DBが存在する場合、空でも原則そのDBを返す
#   - マーケット時間中は古いランキングDBへ fallback しない
#   - 時間外・休場日・当日DB未作成時のみ過去DB fallback を許可
#   - DB内に usable table / rows があるか検査可能
#   - cache による過剰な filesystem / sqlite アクセスを抑制
#
# 【重要】
#   以前の実装では、
#
#       ranking20260420.db が存在するが空
#       -> ranking20260417.db へ fallback
#
#   となり、マーケット時間中にも過去DBを参照する可能性があった。
#
#   これにより、
#     - price_delta_1m
#     - volume_delta_1m
#     - volume_speed
#     - rank_delta
#     - score
#   が当日データと整合しなくなり、score=0 が大量発生しやすい。
#
# 【本版の方針】
#   - 当日DBが存在するなら、空でも当日DBを返す
#   - 場中は古いDBへ逃げない
#   - fallback は表示・時間外・休場日向けの安全策としてのみ使う
#
# 【互換】
#   - get_usable_ranking_db_path(force_refresh=False) は従来通り提供
#   - 引数を追加しても既存呼び出しは壊さない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .constants import RANKING_DB_ROOT, PRIMARY_TABLES, CATEGORY_TABLE_SPECS
from . import cache
from .normalizer import _list_tables, _table_row_count

logger = logging.getLogger(__name__)


# ============================================================
# market time helpers
# ============================================================

def _now_jst_naive() -> dt.datetime:
    """
    実行環境が日本時間前提の既存システムに合わせ、naive datetime を返す。
    """
    return dt.datetime.now().replace(microsecond=0)


def _is_weekday(d: dt.date) -> bool:
    """
    土日だけを簡易休場扱いにする。
    日本祝日判定はここでは持たない。
    祝日は ranking DB が作られない/更新されないため、
    当日DBが無い場合は fallback で過去DBを使える。
    """
    return d.weekday() < 5


def _is_market_session_like(now: Optional[dt.datetime] = None) -> bool:
    """
    ATSランキングDB解決用の簡易マーケット時間判定。

    厳密な祝日カレンダーは scheduler 側に委ねる。
    ここでは「場中に古いDBへ fallback しない」ことが目的。

    対象:
      - 09:00-11:30
      - 12:30-15:30

    注意:
      15:30以降は Yahoo補完や引け後再計算が入るため、
      当日DBが空なら fallback を許容する。
    """
    now = (now or _now_jst_naive()).replace(microsecond=0)

    if not _is_weekday(now.date()):
        return False

    t = now.time()

    morning_start = dt.time(9, 0, 0)
    morning_end = dt.time(11, 30, 0)
    afternoon_start = dt.time(12, 30, 0)
    afternoon_end = dt.time(15, 30, 0)

    return (
        morning_start <= t <= morning_end
        or afternoon_start <= t <= afternoon_end
    )


def _yyyymmdd(now: Optional[dt.datetime] = None) -> str:
    now = now or _now_jst_naive()
    return now.strftime("%Y%m%d")


def _today_ranking_db_path(now: Optional[dt.datetime] = None) -> Path:
    today = _yyyymmdd(now)
    return Path(RANKING_DB_ROOT) / f"ranking{today}.db"


# ============================================================
# DB inspection
# ============================================================

def _db_has_usable_ranking_tables(db_path: str) -> bool:
    """
    ranking DB が実データを持っているかを確認する。

    usable の定義:
      - PRIMARY_TABLES のいずれかに rows > 0
      - CATEGORY_TABLE_SPECS のいずれかに rows > 0

    注意:
      本関数は「fallback候補として使えるか」の判定用。
      当日DBが空の場合でも、場中は当日DBを返すため、
      _get_ranking_db_path 側でこの結果だけに依存しない。
    """
    if not db_path:
        return False

    p = Path(db_path)
    if not p.exists() or not p.is_file():
        return False

    try:
        with sqlite3.connect(str(p), timeout=3.0) as conn:
            tables = set(_list_tables(conn))

            for table_name in PRIMARY_TABLES:
                if table_name in tables:
                    cnt = _table_row_count(conn, table_name)
                    if cnt > 0:
                        logger.info(
                            "[ATS RANKING] usable db hit path=%s table=%s rows=%d",
                            db_path,
                            table_name,
                            cnt,
                        )
                        return True

            for table_name, _, _ in CATEGORY_TABLE_SPECS:
                if table_name in tables:
                    cnt = _table_row_count(conn, table_name)
                    if cnt > 0:
                        logger.info(
                            "[ATS RANKING] usable db hit path=%s table=%s rows=%d",
                            db_path,
                            table_name,
                            cnt,
                        )
                        return True

    except Exception:
        logger.exception("[ATS RANKING] usable db check failed path=%s", db_path)

    return False


def _safe_touch_parent(path: Path) -> None:
    """
    DBファイルをここで作成しない。
    ただし親ディレクトリがない場合は警告だけ出す。
    """
    try:
        parent = path.parent
        if not parent.exists():
            logger.warning("[ATS RANKING] ranking db root not found: %s", parent)
    except Exception:
        logger.exception("[ATS RANKING] parent check failed path=%s", path)


# ============================================================
# fallback
# ============================================================

def _find_latest_usable_ranking_db(
    *,
    exclude_path: Optional[Path] = None,
) -> Optional[str]:
    """
    過去DBを含めて usable な ranking DB を探す。

    注意:
      マーケット時間中の当日DB存在時には呼ばない。
    """
    root = Path(RANKING_DB_ROOT)
    if not root.exists():
        logger.warning("[ATS RANKING] ranking db root not found: %s", root)
        return None

    exclude_resolved: Optional[Path] = None
    try:
        if exclude_path is not None:
            exclude_resolved = exclude_path.resolve()
    except Exception:
        exclude_resolved = exclude_path

    try:
        cands = sorted(root.glob("ranking*.db"), reverse=True)
    except Exception:
        logger.exception("[ATS RANKING] ranking db glob failed root=%s", root)
        return None

    for p in cands:
        try:
            if not p.is_file():
                continue

            if exclude_resolved is not None:
                try:
                    if p.resolve() == exclude_resolved:
                        continue
                except Exception:
                    if str(p) == str(exclude_path):
                        continue

            path_str = str(p)
            if _db_has_usable_ranking_tables(path_str):
                logger.info("[ATS RANKING] fallback usable ranking db selected=%s", path_str)
                return path_str

        except Exception:
            logger.exception("[ATS RANKING] fallback candidate check failed path=%s", p)

    logger.warning("[ATS RANKING] no usable ranking db found under root=%s", root)
    return None


# ============================================================
# path resolver
# ============================================================

def _get_ranking_db_path(
    *,
    now: Optional[dt.datetime] = None,
    allow_fallback: Optional[bool] = None,
    prefer_today_even_if_empty: bool = True,
) -> Optional[str]:
    """
    ranking DB path を解決する。

    Parameters
    ----------
    now:
        判定基準時刻。未指定なら現在時刻。

    allow_fallback:
        None:
            自動判定。
            マーケット時間中は原則 fallback 禁止。
            時間外は fallback 許可。
        True:
            過去DB fallback を明示許可。
        False:
            過去DB fallback を禁止。

    prefer_today_even_if_empty:
        True:
            当日DBが存在する場合、空でも当日DBを返す。
            本番では True 推奨。
        False:
            当日DBが usable の場合のみ返す。
            ただし場中に False を使うと古いDB参照の危険がある。
    """
    try:
        now = (now or _now_jst_naive()).replace(microsecond=0)
        preferred = _today_ranking_db_path(now)
        preferred_str = str(preferred)

        in_session = _is_market_session_like(now)

        if allow_fallback is None:
            allow_fallback_effective = not in_session
        else:
            allow_fallback_effective = bool(allow_fallback)

        logger.info(
            "[ATS RANKING] resolve ranking db start today=%s preferred=%s exists=%s in_session=%s allow_fallback=%s prefer_today_even_if_empty=%s",
            _yyyymmdd(now),
            preferred_str,
            preferred.exists(),
            in_session,
            allow_fallback_effective,
            prefer_today_even_if_empty,
        )

        _safe_touch_parent(preferred)

        # ----------------------------------------------------
        # 1. 当日DBが存在する場合
        # ----------------------------------------------------
        if preferred.exists() and preferred.is_file():
            logger.info("[ATS RANKING] preferred ranking db=%s", preferred_str)

            if _db_has_usable_ranking_tables(preferred_str):
                logger.info("[ATS RANKING] use preferred usable today db=%s", preferred_str)
                return preferred_str

            # 重要:
            # 場中、または prefer_today_even_if_empty=True の場合は、
            # 空でも当日DBを返す。
            #
            # これにより、当日 09:01 の処理が 20260417.db を参照する事故を防ぐ。
            if prefer_today_even_if_empty or in_session:
                logger.warning(
                    "[ATS RANKING] preferred today db exists but empty/unusable -> use today db anyway path=%s in_session=%s allow_fallback=%s",
                    preferred_str,
                    in_session,
                    allow_fallback_effective,
                )
                return preferred_str

            # 時間外かつ明示的に fallback 許可されている場合のみ fallback
            if allow_fallback_effective:
                logger.warning(
                    "[ATS RANKING] preferred db exists but no usable tables/rows -> fallback search: %s",
                    preferred_str,
                )
                return _find_latest_usable_ranking_db(exclude_path=preferred)

            logger.warning(
                "[ATS RANKING] preferred db exists but unusable and fallback disabled -> returning today db path=%s",
                preferred_str,
            )
            return preferred_str

        # ----------------------------------------------------
        # 2. 当日DBが存在しない場合
        # ----------------------------------------------------
        logger.warning("[ATS RANKING] preferred today db not found path=%s", preferred_str)

        # 場中は、当日DBが未作成でも古いDBには逃げない。
        # 呼び出し側が DB 作成/ランキング保存の問題に気づけるよう None を返す。
        if in_session and not allow_fallback_effective:
            logger.warning(
                "[ATS RANKING] in market session and today db missing -> fallback blocked path=%s",
                preferred_str,
            )
            return None

        if allow_fallback_effective:
            logger.warning(
                "[ATS RANKING] today db missing -> fallback search allowed path=%s",
                preferred_str,
            )
            return _find_latest_usable_ranking_db(exclude_path=preferred)

        logger.warning(
            "[ATS RANKING] today db missing and fallback disabled -> None path=%s",
            preferred_str,
        )
        return None

    except Exception:
        logger.exception("ranking db path resolve failed")
        return None


# ============================================================
# cache api
# ============================================================

def _cache_valid(now_ts: float) -> bool:
    try:
        return (
            cache._ATS_RANKING_DB_PATH_CACHE is not None
            and (now_ts - cache._ATS_RANKING_DB_PATH_CACHE_TS) < cache._ATS_RANKING_DB_PATH_CACHE_SEC
        )
    except Exception:
        return False


def _clear_cache() -> None:
    try:
        cache._ATS_RANKING_DB_PATH_CACHE = None
        cache._ATS_RANKING_DB_PATH_CACHE_TS = 0.0
    except Exception:
        logger.exception("[ATS RANKING] clear cache failed")


def get_usable_ranking_db_path(
    force_refresh: bool = False,
    *,
    now: Optional[dt.datetime] = None,
    allow_fallback: Optional[bool] = None,
    prefer_today_even_if_empty: bool = True,
) -> Optional[str]:
    """
    ATSランキング用 ranking DB path を返す公開API。

    従来互換:
      get_usable_ranking_db_path(force_refresh=False)

    追加仕様:
      - 場中は当日DBを空でも優先
      - 古いDBへの fallback を抑止
      - cache が古いDBを返し続ける事故を抑止

    Parameters
    ----------
    force_refresh:
        True の場合、cache を無視して再解決する。

    now:
        判定基準時刻。

    allow_fallback:
        None の場合は自動判定。
        場中は fallback 禁止、時間外は fallback 許可。

    prefer_today_even_if_empty:
        当日DBが存在する場合、空でも当日DBを返す。
    """
    now_dt = (now or _now_jst_naive()).replace(microsecond=0)
    now_ts = time.time()

    today_path = str(_today_ranking_db_path(now_dt))
    in_session = _is_market_session_like(now_dt)

    # 場中は cache が過去DBを保持していると危険。
    # cache path が今日DBでない場合は強制 refresh。
    if not force_refresh and _cache_valid(now_ts):
        cached = cache._ATS_RANKING_DB_PATH_CACHE

        if in_session and cached and cached != today_path:
            logger.warning(
                "[ATS RANKING] cached path is not today during session -> invalidate cached=%s today=%s",
                cached,
                today_path,
            )
            _clear_cache()
        else:
            logger.info(
                "[ATS RANKING] return cached ranking db path=%s age=%.3fs in_session=%s",
                cached,
                now_ts - cache._ATS_RANKING_DB_PATH_CACHE_TS,
                in_session,
            )
            return cached

    path = _get_ranking_db_path(
        now=now_dt,
        allow_fallback=allow_fallback,
        prefer_today_even_if_empty=prefer_today_even_if_empty,
    )

    try:
        cache._ATS_RANKING_DB_PATH_CACHE = path
        cache._ATS_RANKING_DB_PATH_CACHE_TS = now_ts
    except Exception:
        logger.exception("[ATS RANKING] cache update failed path=%s", path)

    logger.info("[ATS RANKING] resolved ranking db path=%s", path)
    return path


def get_today_ranking_db_path(now: Optional[dt.datetime] = None) -> str:
    """
    当日 ranking DB path を無条件に返す。
    DBが存在するかどうかは問わない。
    """
    return str(_today_ranking_db_path(now))


def invalidate_ranking_db_path_cache() -> None:
    """
    外部から cache を明示破棄するための API。
    ランキング保存直後や日付切り替え時に使用可能。
    """
    _clear_cache()
    logger.info("[ATS RANKING] db path cache invalidated")


__all__ = [
    "get_usable_ranking_db_path",
    "get_today_ranking_db_path",
    "invalidate_ranking_db_path_cache",
]