# ============================================================
# File   : core/startup/db_bootstrap.py
# Ver    : PRODUCTION-STABLE-REV5.6-MAIN-SKIP-PREV-SUMMARY-RESTORE
# ------------------------------------------------------------
# ✔ summary DB フォールバック完全対応
# ✔ ranking DB フォールバック完全対応
# ✔ 非営業日 anchor 対応
# ✔ 最大7日探索
# ✔ migration 自動実行保持
# ✔ summary_engine 再生成保持
# ✔ ranking_engine 再生成保持
# ✔ session内部実体 _summary_engine / _ranking_engine も再bind
# ✔ 公開変数 summary_engine / ranking_engine も再bind
# ✔ 例外耐性最大化
# ✔ ログ詳細化
# ✔ production hardened
# ✔ 市場時間中は当日 summary/ranking DB を強制採用
# ✔ 当日DBが空でも保存先として採用
# ✔ DB未作成/必須テーブル未作成時は full migration 自動切替
# ✔ 既存DBあり通常起動時は startup-light migration
# ✔ summary snapshot preload 高速化用 index ensure
# ✔ 土日祝日・営業日9時前は直近営業日DBを優先
# ✔ 起動時の effective_trade_date を summary/ranking 解決に使用
# ✔ 日曜/祝日に today_db を参照先として採用しない
# ✔ os 未import問題を解消
# ✔ split mode: main.pyでは migration と ranking rebind をskip可能
# ✔ split mode: main.pyでは前営業日 summary DB の強制検証/直読みをskip可能
#    - NAS上の旧summary DB読み込みで 0xC0000006 が出る事故を回避
#    - main_database.pyが当日DBを作成するまで main.py は当日DBパスで空起動可能
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine

from utils.business_day_utils import (
    is_business_day,
    get_last_market_close_datetime,
    get_previous_business_day,
)

from database.migrate.migrate_main import (
    run_startup_migration,
    run_full_migration,
)
from database.migrate.ensure_summary_indexes import (
    ensure_summary_snapshot_indexes,
)
from database import session as db_session_module

logger = logging.getLogger(__name__)

MARKET_OPEN = dt.time(9, 0)
MARKET_CLOSE = dt.time(15, 30)


def _env_bool(name: str, default: bool = False) -> bool:
    """環境変数のON/OFF判定。"""
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


def _is_market_hours_now(now: Optional[dt.datetime] = None) -> bool:
    try:
        now = now or dt.datetime.now()
        if not is_business_day(now.date()):
            return False
        t = now.time()
        return MARKET_OPEN <= t <= MARKET_CLOSE
    except Exception:
        logger.exception("❌ market hours check failed")
        return False


def _is_after_market_close_today(now: Optional[dt.datetime] = None) -> bool:
    try:
        now = now or dt.datetime.now()
        if not is_business_day(now.date()):
            return False
        return now.time() >= MARKET_CLOSE
    except Exception:
        logger.exception("❌ after market close check failed")
        return False


def _get_startup_trade_date(now: Optional[dt.datetime] = None) -> dt.date:
    now = now or dt.datetime.now()
    try:
        try:
            from utils.business_day_utils import get_effective_trade_date_for_startup
            d = get_effective_trade_date_for_startup(now)
            logger.info("[DBBOOT][trade_date] effective startup trade_date=%s source=business_day_utils", d)
            return d
        except Exception:
            pass

        today = now.date()
        if not is_business_day(today):
            d = get_previous_business_day(today)
            logger.info("[DBBOOT][trade_date] non-business day today=%s -> effective=%s", today, d)
            return d
        if now.time() < MARKET_OPEN:
            d = get_previous_business_day(today)
            logger.info("[DBBOOT][trade_date] before market open today=%s -> effective=%s", today, d)
            return d
        logger.info("[DBBOOT][trade_date] business day after open -> effective=%s", today)
        return today
    except Exception:
        logger.warning("[DBBOOT][trade_date] effective trade date failed; fallback today", exc_info=True)
        return dt.date.today()


def _db_date_str(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _summary_db_path_for_date(summary_dir: Path, d: dt.date) -> Path:
    return summary_dir / f"summary{_db_date_str(d)}.db"


def _ranking_db_path_for_date(ranking_dir: Path, d: dt.date) -> Path:
    return ranking_dir / f"ranking{_db_date_str(d)}.db"


def _sqlite_table_exists(db_path: Path, table_name: str) -> bool:
    if not db_path.exists():
        logger.info("[DBBOOT][sqlite_table_exists] db missing path=%s table=%s", db_path, table_name)
        return False
    try:
        logger.info("[DBBOOT][sqlite_table_exists] open start path=%s table=%s", db_path, table_name)
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table'
                  AND name=?
                LIMIT 1
                """,
                (table_name,),
            )
            ok = cur.fetchone() is not None
            logger.info("[DBBOOT][sqlite_table_exists] open done path=%s table=%s exists=%s", db_path, table_name, ok)
            return ok
    except Exception as e:
        logger.warning("⚠ table existence check failed db=%s table=%s err=%s", db_path, table_name, e)
        return False


def _db_has_summary_data(db_path: Path) -> bool:
    if not db_path.exists():
        logger.info("[DBBOOT][summary_data] db missing path=%s", db_path)
        return False
    try:
        logger.info("[DBBOOT][summary_data] connect start path=%s", db_path)
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_summary_1min'")
            if cur.fetchone() is None:
                logger.info("[DBBOOT][summary_data] stock_summary_1min missing path=%s", db_path)
                return False
            cur.execute("SELECT COUNT(*) FROM stock_summary_1min")
            row = cur.fetchone()
            count = int(row[0]) if row and row[0] is not None else 0
            logger.info("[DBBOOT][summary_data] count=%s path=%s", count, db_path)
            return count > 0
    except Exception as e:
        logger.warning("⚠ summary DB validation failed: %s", e)
        return False


def _db_has_summary_table(db_path: Path) -> bool:
    return _sqlite_table_exists(db_path, "stock_summary_1min")


def _db_has_ranking_data(db_path: Path) -> bool:
    if not db_path.exists():
        logger.info("[DBBOOT][ranking_data] db missing path=%s", db_path)
        return False
    try:
        logger.info("[DBBOOT][ranking_data] connect start path=%s", db_path)
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ranking_snapshot_1min'")
            if cur.fetchone() is None:
                logger.info("[DBBOOT][ranking_data] ranking_snapshot_1min missing path=%s", db_path)
                return False
            cur.execute("SELECT COUNT(*) FROM ranking_snapshot_1min")
            row = cur.fetchone()
            count = int(row[0]) if row and row[0] is not None else 0
            logger.info("[DBBOOT][ranking_data] count=%s path=%s", count, db_path)
            return count > 0
    except Exception as e:
        logger.warning("⚠ ranking DB validation failed: %s", e)
        return False


def _db_has_ranking_table(db_path: Path) -> bool:
    return _sqlite_table_exists(db_path, "ranking_snapshot_1min")


def _should_run_full_migration(summary_dir: Path, ranking_dir: Optional[Path] = None) -> bool:
    try:
        trade_date = _get_startup_trade_date()
        summary_db = _summary_db_path_for_date(summary_dir, trade_date)
        logger.info("[DBBOOT][route] check effective summary_db=%s trade_date=%s", summary_db, trade_date)
        if not summary_db.exists():
            logger.info("🆕 full migration reason: effective summary DB missing -> %s", summary_db)
            return True
        if not _db_has_summary_table(summary_db):
            logger.info("🆕 full migration reason: summary table missing -> %s", summary_db)
            return True

        if ranking_dir is not None:
            ranking_db = _ranking_db_path_for_date(ranking_dir, trade_date)
            logger.info("[DBBOOT][route] check effective ranking_db=%s trade_date=%s", ranking_db, trade_date)
            if not ranking_db.exists():
                logger.info("🆕 full migration reason: effective ranking DB missing -> %s", ranking_db)
                return True
            if not _db_has_ranking_table(ranking_db):
                logger.info("🆕 full migration reason: ranking table missing -> %s", ranking_db)
                return True

        logger.info("⚡ existing DB/tables detected -> startup-light migration route")
        return False
    except Exception:
        logger.exception("❌ migration route decision failed -> fallback to full migration")
        return True


def _run_auto_migration(summary_dir: Path, ranking_dir: Optional[Path] = None) -> None:
    logger.info("[DBBOOT][auto_migration] start summary_dir=%s ranking_dir=%s", summary_dir, ranking_dir)
    use_full = _should_run_full_migration(summary_dir=summary_dir, ranking_dir=ranking_dir)
    logger.info("[DBBOOT][auto_migration] route use_full=%s", use_full)
    if use_full:
        logger.info("🚀 AUTO MIGRATION ROUTE = FULL")
        run_full_migration(include_daily_sqlite=False)
        logger.info("✅ Full migration completed")
    else:
        logger.info("⚡ AUTO MIGRATION ROUTE = STARTUP-LIGHT")
        run_startup_migration(include_summary_sqlite=False, include_daily_sqlite=False)
        logger.info("✅ Startup-light migration completed")
    logger.info("[DBBOOT][auto_migration] done")


def resolve_latest_valid_summary_db(
    summary_dir: Path,
    *,
    skip_prev_restore: bool = False,
) -> Path:
    """
    summary DB 解決。

    skip_prev_restore=True:
      main.py split mode 用。
      起動時に前営業日/anchor/fallback の summary DB を検証しない。
      NAS上の旧DBを sqlite3.connect した瞬間に Windows 0xC0000006 で落ちる事故を避ける。
      当日DBが未作成でも today_db を返し、main_database.py 側のDB作成を待てる状態にする。
    """
    now = dt.datetime.now()
    today = now.date()
    effective_trade_date = _get_startup_trade_date(now)
    today_db = _summary_db_path_for_date(summary_dir, today)
    effective_db = _summary_db_path_for_date(summary_dir, effective_trade_date)
    logger.info("🔍 Resolving latest valid summary DB")
    logger.info("[DBBOOT][resolve_summary] today_db=%s", today_db)
    logger.info("[DBBOOT][resolve_summary] effective_trade_date=%s effective_db=%s", effective_trade_date, effective_db)
    logger.info("[DBBOOT][resolve_summary] skip_prev_restore=%s", skip_prev_restore)

    if _is_market_hours_now(now):
        logger.info("📈 Market hours detected -> force today summary DB: %s", today_db)
        return today_db

    if skip_prev_restore:
        logger.warning(
            "[DBBOOT][resolve_summary] skip previous summary restore enabled -> use today summary DB without old DB validation: %s",
            today_db,
        )
        return today_db

    if _is_after_market_close_today(now):
        if today_db.exists() and _db_has_summary_data(today_db):
            logger.info("📂 Using today summary DB after close: %s", today_db)
            return today_db
    if effective_db.exists() and _db_has_summary_data(effective_db):
        logger.info("📂 Using effective summary DB: %s", effective_db)
        return effective_db
    try:
        anchor_dt = get_last_market_close_datetime()
        anchor_date = anchor_dt.date()
        anchor_db = _summary_db_path_for_date(summary_dir, anchor_date)
        logger.info("[DBBOOT][resolve_summary] anchor candidate=%s", anchor_db)
        if anchor_db.exists() and _db_has_summary_data(anchor_db):
            logger.info("📂 Using anchor summary DB: %s", anchor_db)
            return anchor_db
    except Exception as e:
        logger.warning("⚠ anchor summary DB resolution failed: %s", e)
    for i in range(0, 7):
        target_date = effective_trade_date - dt.timedelta(days=i)
        db_path = _summary_db_path_for_date(summary_dir, target_date)
        logger.info("[DBBOOT][resolve_summary] fallback candidate i=%s path=%s", i, db_path)
        if _db_has_summary_data(db_path):
            logger.info("📂 Using fallback summary DB: %s", db_path)
            return db_path
    if effective_trade_date != today:
        logger.warning("⚠ No populated summary DB found -> fallback to effective DB for read target: %s", effective_db)
        return effective_db
    logger.warning("⚠ No populated summary DB found -> fallback to today DB for write target: %s", today_db)
    return today_db


def resolve_latest_valid_ranking_db(ranking_dir: Path) -> Path:
    now = dt.datetime.now()
    today = now.date()
    effective_trade_date = _get_startup_trade_date(now)
    today_db = _ranking_db_path_for_date(ranking_dir, today)
    effective_db = _ranking_db_path_for_date(ranking_dir, effective_trade_date)
    logger.info("🔍 Resolving latest valid ranking DB")
    logger.info("[DBBOOT][resolve_ranking] ranking_dir=%s today=%s", ranking_dir, today)
    logger.info("[DBBOOT][resolve_ranking] today_db=%s", today_db)
    logger.info("[DBBOOT][resolve_ranking] effective_trade_date=%s effective_db=%s", effective_trade_date, effective_db)
    if _is_market_hours_now(now):
        logger.info("📈 Market hours detected -> force today ranking DB: %s", today_db)
        return today_db
    if _is_after_market_close_today(now):
        if today_db.exists() and _db_has_ranking_data(today_db):
            logger.info("📂 Using today ranking DB after close: %s", today_db)
            return today_db
    if effective_db.exists() and _db_has_ranking_data(effective_db):
        logger.info("📂 Using effective ranking DB: %s", effective_db)
        return effective_db
    try:
        anchor_dt = get_last_market_close_datetime()
        anchor_date = anchor_dt.date()
        anchor_db = _ranking_db_path_for_date(ranking_dir, anchor_date)
        logger.info("[DBBOOT][resolve_ranking] anchor candidate=%s", anchor_db)
        if anchor_db.exists() and _db_has_ranking_data(anchor_db):
            logger.info("📂 Using anchor ranking DB: %s", anchor_db)
            return anchor_db
    except Exception as e:
        logger.warning("⚠ anchor ranking DB resolution failed: %s", e)
    for i in range(0, 7):
        target_date = effective_trade_date - dt.timedelta(days=i)
        db_path = _ranking_db_path_for_date(ranking_dir, target_date)
        logger.info("[DBBOOT][resolve_ranking] fallback candidate i=%s path=%s", i, db_path)
        if _db_has_ranking_data(db_path):
            logger.info("📂 Using fallback ranking DB: %s", db_path)
            return db_path
    if effective_trade_date != today:
        logger.warning("⚠ No populated ranking DB found -> fallback to effective DB for read target: %s", effective_db)
        return effective_db
    logger.warning("⚠ No populated ranking DB found -> fallback to today DB for write/read target: %s", today_db)
    return today_db


def _build_sqlite_engine(db_path: Path):
    logger.info("[DBBOOT][build_engine] db_path=%s", db_path)
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, future=True)


def _rebind_summary_engine(db_path: Path):
    logger.info("🔄 Rebinding summary_engine → %s", db_path)
    engine = _build_sqlite_engine(db_path)
    db_session_module.summary_engine = engine
    if hasattr(db_session_module, "_summary_engine"):
        db_session_module._summary_engine = engine
    if hasattr(db_session_module, "_Session_summary"):
        try:
            from sqlalchemy.orm import sessionmaker
            db_session_module._Session_summary = sessionmaker(bind=engine)
        except Exception:
            logger.exception("❌ _Session_summary rebind failed")
            raise
    return engine


def _rebind_ranking_engine(db_path: Path):
    logger.info("🔄 Rebinding ranking_engine → %s", db_path)
    engine = _build_sqlite_engine(db_path)
    db_session_module.ranking_engine = engine
    if hasattr(db_session_module, "_ranking_engine"):
        db_session_module._ranking_engine = engine
    if hasattr(db_session_module, "_Session_ranking"):
        try:
            from sqlalchemy.orm import sessionmaker
            db_session_module._Session_ranking = sessionmaker(bind=engine)
        except Exception:
            logger.exception("❌ _Session_ranking rebind failed")
            raise
    return engine


def bootstrap_database(summary_dir: Path, ranking_dir: Optional[Path] = None, *, skip_migration: bool = False) -> Path:
    """
    ✔ migration 自動選択実行
    ✔ summary DB 解決
    ✔ ranking DB 解決（指定時）
    ✔ summary_engine 再バインド
    ✔ ranking_engine 再バインド
    ✔ summary snapshot 用 index ensure

    split mode:
      - skip_migration=True の場合、main.py側では migration を実行しない
      - ranking_dir=None の場合、ranking DB resolve / rebind を実行しない
      - MAIN_SKIP_PREV_SUMMARY_RESTORE=1 または既定ONにより、main.py側では前営業日summary DBを直読みしない
    """
    logger.info("🗄️ Database bootstrap start")
    logger.info("📁 summary_dir = %s", summary_dir)
    logger.info("📁 ranking_dir = %s", ranking_dir)
    logger.info("[DBBOOT] skip_migration=%s", skip_migration)

    ranking_db_path = None

    if skip_migration:
        logger.warning(
            "[DBBOOT] migration skipped by split mode. main_database.py handles DB creation/migration for PUSH/RANKING."
        )
    else:
        try:
            logger.info("[DBBOOT] step=1 before _run_auto_migration")
            _run_auto_migration(summary_dir=summary_dir, ranking_dir=ranking_dir)
            logger.info("[DBBOOT] step=2 after _run_auto_migration")
        except Exception:
            logger.exception("❌ AUTO MIGRATION FAILED")
            raise

    try:
        # main.py split mode では、前営業日DBをNASから直読みして存在/件数検証しない。
        # 2026-05-18ログで summary20260515.db connect start 直後に 0xC0000006 でプロセス終了したため。
        skip_prev_restore = bool(skip_migration) and _env_bool("MAIN_SKIP_PREV_SUMMARY_RESTORE", True)
        logger.info(
            "[DBBOOT] step=3 before resolve summary skip_prev_restore=%s env_MAIN_SKIP_PREV_SUMMARY_RESTORE=%s",
            skip_prev_restore,
            os.getenv("MAIN_SKIP_PREV_SUMMARY_RESTORE"),
        )
        summary_db_path = resolve_latest_valid_summary_db(
            summary_dir,
            skip_prev_restore=skip_prev_restore,
        )
        logger.info("[DBBOOT] step=4 after resolve summary path=%s", summary_db_path)
        logger.info("✅ summary DB resolved: %s", summary_db_path)
    except Exception:
        logger.exception("❌ summary DB resolution failed")
        raise

    if ranking_dir is not None:
        try:
            logger.info("[DBBOOT] step=5 before resolve ranking")
            ranking_db_path = resolve_latest_valid_ranking_db(ranking_dir)
            logger.info("[DBBOOT] step=6 after resolve ranking path=%s", ranking_db_path)
            logger.info("✅ ranking DB resolved: %s", ranking_db_path)
        except Exception:
            logger.exception("❌ ranking DB resolution failed")
            raise
    else:
        logger.warning("[DBBOOT] step=5 skipped resolve ranking because ranking_dir is None")

    try:
        logger.info("[DBBOOT] step=7 before rebind summary")
        summary_engine_obj = _rebind_summary_engine(summary_db_path)
        logger.info("[DBBOOT] step=8 after rebind summary")
        logger.info("✅ summary_engine rebound: %s", summary_db_path)
    except Exception:
        logger.exception("❌ summary_engine rebind failed")
        raise

    try:
        logger.info("[DBBOOT] step=8.1 before ensure summary snapshot indexes")
        ensure_summary_snapshot_indexes(summary_engine_obj)
        logger.info("[DBBOOT] step=8.2 after ensure summary snapshot indexes")
    except Exception:
        logger.exception("❌ summary snapshot index ensure failed")
        logger.warning("⚠ continue without summary snapshot index ensure")

    if ranking_db_path is not None:
        try:
            logger.info("[DBBOOT] step=9 before rebind ranking")
            _rebind_ranking_engine(ranking_db_path)
            logger.info("[DBBOOT] step=10 after rebind ranking")
            logger.info("✅ ranking_engine rebound: %s", ranking_db_path)
        except Exception:
            logger.exception("❌ ranking_engine rebind failed")
            raise
    else:
        logger.warning("[DBBOOT] step=9 skipped rebind ranking because ranking_db_path is None")

    logger.info("🗄️ Database bootstrap complete")
    return summary_db_path


__all__ = [
    "bootstrap_database",
    "resolve_latest_valid_summary_db",
    "resolve_latest_valid_ranking_db",
]