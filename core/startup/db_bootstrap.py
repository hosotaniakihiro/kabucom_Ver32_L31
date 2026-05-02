# ============================================================
# File   : core/startup/db_bootstrap.py
# Ver    : PRODUCTION-STABLE-REV5.3-STARTUP-LIGHT-WITH-RANKING-TODAY-FORCE
# ------------------------------------------------------------
# ✔ REV5.2 完全保持ベース
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
# ✔ 市場時間中は当日 summary DB を強制採用
# ✔ 当日DBが空でも保存先として採用
# ✔ fallback は閉場後/非営業日のみ強く使用
# ✔ DB未作成/必須テーブル未作成時は full migration 自動切替
# ✔ 既存DBあり通常起動時は startup-light migration
# ✔ NEW: bootstrap_database に step ログ追加
# ✔ NEW: どの段で落ちたかを切り分け可能
# ✔ NEW: summary snapshot preload 高速化用 index ensure
# ✔ NEW: 市場時間中は当日 ranking DB を強制採用
# ✔ NEW: 当日 ranking DB が空でも参照先を当日に固定
# ✔ NEW: 起動直後に古い ranking DB へ誤fallbackしない
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine

from utils.business_day_utils import (
    is_today_business_day,
    get_last_market_close_datetime,
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


# ============================================================
# 内部：市場時間判定
# ============================================================

def _is_market_hours_now(now: Optional[dt.datetime] = None) -> bool:
    try:
        now = now or dt.datetime.now()
        if not is_today_business_day():
            return False
        t = now.time()
        return MARKET_OPEN <= t <= MARKET_CLOSE
    except Exception:
        logger.exception("❌ market hours check failed")
        return False


# ============================================================
# 内部：SQLite table existence
# ============================================================

def _sqlite_table_exists(db_path: Path, table_name: str) -> bool:
    if not db_path.exists():
        logger.info("[DBBOOT][sqlite_table_exists] db missing path=%s table=%s", db_path, table_name)
        return False

    try:
        logger.info("[DBBOOT][sqlite_table_exists] open start path=%s table=%s", db_path, table_name)
        with sqlite3.connect(str(db_path)) as conn:
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
            logger.info(
                "[DBBOOT][sqlite_table_exists] open done path=%s table=%s exists=%s",
                db_path, table_name, ok
            )
            return ok
    except Exception as e:
        logger.warning("⚠ table existence check failed db=%s table=%s err=%s", db_path, table_name, e)
        return False


# ============================================================
# 内部：summaryテーブル存在＆データ確認
# ============================================================

def _db_has_summary_data(db_path: Path) -> bool:
    if not db_path.exists():
        logger.info("[DBBOOT][summary_data] db missing path=%s", db_path)
        return False

    try:
        logger.info("[DBBOOT][summary_data] connect start path=%s", db_path)
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()

            cur.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                  AND name='stock_summary_1min'
                """
            )
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


# ============================================================
# 内部：rankingテーブル存在確認
# ============================================================

def _db_has_ranking_data(db_path: Path) -> bool:
    if not db_path.exists():
        logger.info("[DBBOOT][ranking_data] db missing path=%s", db_path)
        return False

    try:
        logger.info("[DBBOOT][ranking_data] connect start path=%s", db_path)
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()

            cur.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                  AND name='ranking_snapshot_1min'
                """
            )
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


# ============================================================
# migration route decision
# ============================================================

def _should_run_full_migration(
    summary_dir: Path,
    ranking_dir: Optional[Path] = None,
) -> bool:
    """
    初回起動やDB欠損時は full migration へ自動切替。
    """
    try:
        today = dt.date.today()

        today_summary_db = summary_dir / f"summary{today:%Y%m%d}.db"
        logger.info("[DBBOOT][route] check today_summary_db=%s", today_summary_db)

        if not today_summary_db.exists():
            logger.info("🆕 full migration reason: today summary DB missing -> %s", today_summary_db)
            return True

        if not _db_has_summary_table(today_summary_db):
            logger.info("🆕 full migration reason: summary table missing -> %s", today_summary_db)
            return True

        if ranking_dir is not None:
            today_ranking_db = ranking_dir / f"ranking{today:%Y%m%d}.db"
            logger.info("[DBBOOT][route] check today_ranking_db=%s", today_ranking_db)

            if not today_ranking_db.exists():
                logger.info("🆕 full migration reason: today ranking DB missing -> %s", today_ranking_db)
                return True

            if not _db_has_ranking_table(today_ranking_db):
                logger.info("🆕 full migration reason: ranking table missing -> %s", today_ranking_db)
                return True

        logger.info("⚡ existing DB/tables detected -> startup-light migration route")
        return False

    except Exception:
        logger.exception("❌ migration route decision failed -> fallback to full migration")
        return True


def _run_auto_migration(
    summary_dir: Path,
    ranking_dir: Optional[Path] = None,
) -> None:
    """
    DB状態に応じて migration 経路を自動選択する。
    """
    logger.info("[DBBOOT][auto_migration] start summary_dir=%s ranking_dir=%s", summary_dir, ranking_dir)

    use_full = _should_run_full_migration(
        summary_dir=summary_dir,
        ranking_dir=ranking_dir,
    )

    logger.info("[DBBOOT][auto_migration] route use_full=%s", use_full)

    if use_full:
        logger.info("🚀 AUTO MIGRATION ROUTE = FULL")
        run_full_migration(include_daily_sqlite=False)
        logger.info("✅ Full migration completed")
    else:
        logger.info("⚡ AUTO MIGRATION ROUTE = STARTUP-LIGHT")
        run_startup_migration(
            include_summary_sqlite=False,
            include_daily_sqlite=False,
        )
        logger.info("✅ Startup-light migration completed")

    logger.info("[DBBOOT][auto_migration] done")


# ============================================================
# summary DB 解決ロジック
# ============================================================

def resolve_latest_valid_summary_db(summary_dir: Path) -> Path:
    """
    市場時間中:
        当日 summary DB を必ず採用（空でも可）
    非営業日 / 閉場後:
        anchor / 直近データあり DB を採用
    """
    today = dt.date.today()
    today_db = summary_dir / f"summary{today:%Y%m%d}.db"

    logger.info("🔍 Resolving latest valid summary DB")
    logger.info("[DBBOOT][resolve_summary] today_db=%s", today_db)

    if _is_market_hours_now():
        logger.info("📈 Market hours detected -> force today summary DB: %s", today_db)
        return today_db

    if not is_today_business_day():
        try:
            anchor_dt = get_last_market_close_datetime()
            anchor_date = anchor_dt.date()
            db_path = summary_dir / f"summary{anchor_date:%Y%m%d}.db"

            logger.info("[DBBOOT][resolve_summary] anchor candidate=%s", db_path)

            if _db_has_summary_data(db_path):
                logger.info("📂 Using anchor summary DB: %s", db_path)
                return db_path
        except Exception as e:
            logger.warning("⚠ anchor summary DB resolution failed: %s", e)

    if today_db.exists() and _db_has_summary_data(today_db):
        logger.info("📂 Using today summary DB with data: %s", today_db)
        return today_db

    for i in range(0, 7):
        target_date = today - dt.timedelta(days=i)
        db_path = summary_dir / f"summary{target_date:%Y%m%d}.db"
        logger.info("[DBBOOT][resolve_summary] fallback candidate i=%s path=%s", i, db_path)

        if _db_has_summary_data(db_path):
            logger.info("📂 Using fallback summary DB: %s", db_path)
            return db_path

    logger.warning("⚠ No populated summary DB found -> fallback to today DB for write target: %s", today_db)
    return today_db


# ============================================================
# ranking DB 解決ロジック
# ============================================================

def resolve_latest_valid_ranking_db(ranking_dir: Path) -> Path:
    """
    市場時間中:
        当日 ranking DB を必ず採用（空でも可）
    非営業日 / 閉場後:
        anchor / 直近データあり DB を採用
    """
    today = dt.date.today()
    today_db = ranking_dir / f"ranking{today:%Y%m%d}.db"

    logger.info("🔍 Resolving latest valid ranking DB")
    logger.info("[DBBOOT][resolve_ranking] ranking_dir=%s today=%s", ranking_dir, today)
    logger.info("[DBBOOT][resolve_ranking] today_db=%s", today_db)

    # --------------------------------------------------------
    # 市場時間中は当日DBを強制採用
    # 起動直後に当日DBが空でも、保存先/参照先を当日に固定する
    # --------------------------------------------------------
    if _is_market_hours_now():
        logger.info("📈 Market hours detected -> force today ranking DB: %s", today_db)
        return today_db

    # --------------------------------------------------------
    # 非営業日は anchor を優先
    # --------------------------------------------------------
    if not is_today_business_day():
        try:
            anchor_dt = get_last_market_close_datetime()
            anchor_date = anchor_dt.date()
            db_path = ranking_dir / f"ranking{anchor_date:%Y%m%d}.db"

            logger.info("[DBBOOT][resolve_ranking] anchor candidate=%s", db_path)

            if _db_has_ranking_data(db_path):
                logger.info("📂 Using anchor ranking DB: %s", db_path)
                return db_path
        except Exception as e:
            logger.warning("⚠ anchor ranking DB resolution failed: %s", e)

    # --------------------------------------------------------
    # 閉場後は当日DBにデータがあれば優先
    # --------------------------------------------------------
    if today_db.exists() and _db_has_ranking_data(today_db):
        logger.info("📂 Using today ranking DB with data: %s", today_db)
        return today_db

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------
    for i in range(0, 7):
        target_date = today - dt.timedelta(days=i)
        db_path = ranking_dir / f"ranking{target_date:%Y%m%d}.db"
        logger.info("[DBBOOT][resolve_ranking] fallback candidate i=%s path=%s", i, db_path)

        if _db_has_ranking_data(db_path):
            logger.info("📂 Using ranking DB: %s", db_path)
            return db_path

    logger.warning("⚠ No populated ranking DB found -> fallback to today DB for write/read target: %s", today_db)
    return today_db


# ============================================================
# 共通：SQLite engine生成
# ============================================================

def _build_sqlite_engine(db_path: Path):
    logger.info("[DBBOOT][build_engine] db_path=%s", db_path)
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )


# ============================================================
# summary_engine 再生成
# ============================================================

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


# ============================================================
# ranking_engine 再生成
# ============================================================

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


# ============================================================
# 外部公開：DB bootstrap
# ============================================================

def bootstrap_database(
    summary_dir: Path,
    ranking_dir: Optional[Path] = None,
) -> Path:
    """
    ✔ migration 自動選択実行
    ✔ summary DB 解決
    ✔ ranking DB 解決（指定時）
    ✔ summary_engine 再バインド
    ✔ ranking_engine 再バインド
    ✔ summary snapshot 用 index ensure
    ✔ 安全ログ出力

    戻り値:
        実際に採用した summary DB の Path
    """
    logger.info("🗄️ Database bootstrap start")
    logger.info("📁 summary_dir = %s", summary_dir)
    logger.info("📁 ranking_dir = %s", ranking_dir)

    ranking_db_path = None

    # --------------------------------------------------------
    # Migration
    # --------------------------------------------------------
    try:
        logger.info("[DBBOOT] step=1 before _run_auto_migration")
        _run_auto_migration(
            summary_dir=summary_dir,
            ranking_dir=ranking_dir,
        )
        logger.info("[DBBOOT] step=2 after _run_auto_migration")
    except Exception:
        logger.exception("❌ AUTO MIGRATION FAILED")
        raise

    # --------------------------------------------------------
    # summary DB 解決
    # --------------------------------------------------------
    try:
        logger.info("[DBBOOT] step=3 before resolve summary")
        summary_db_path = resolve_latest_valid_summary_db(summary_dir)
        logger.info("[DBBOOT] step=4 after resolve summary path=%s", summary_db_path)
        logger.info("✅ summary DB resolved: %s", summary_db_path)
    except Exception:
        logger.exception("❌ summary DB resolution failed")
        raise

    # --------------------------------------------------------
    # ranking DB 解決（存在する場合のみ）
    # --------------------------------------------------------
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
        logger.info("[DBBOOT] step=5 skipped resolve ranking because ranking_dir is None")

    # --------------------------------------------------------
    # summary_engine 再生成
    # --------------------------------------------------------
    try:
        logger.info("[DBBOOT] step=7 before rebind summary")
        summary_engine_obj = _rebind_summary_engine(summary_db_path)
        logger.info("[DBBOOT] step=8 after rebind summary")
        logger.info("✅ summary_engine rebound: %s", summary_db_path)
    except Exception:
        logger.exception("❌ summary_engine rebind failed")
        raise

    # --------------------------------------------------------
    # summary snapshot 用 index ensure
    # --------------------------------------------------------
    try:
        logger.info("[DBBOOT] step=8.1 before ensure summary snapshot indexes")
        ensure_summary_snapshot_indexes(summary_engine_obj)
        logger.info("[DBBOOT] step=8.2 after ensure summary snapshot indexes")
    except Exception:
        logger.exception("❌ summary snapshot index ensure failed")
        # index失敗では全体を止めない
        logger.warning("⚠ continue without summary snapshot index ensure")

    # --------------------------------------------------------
    # ranking_engine 再生成
    # --------------------------------------------------------
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
        logger.info("[DBBOOT] step=9 skipped rebind ranking because ranking_db_path is None")

    logger.info("🗄️ Database bootstrap complete")
    return summary_db_path


__all__ = [
    "bootstrap_database",
    "resolve_latest_valid_summary_db",
    "resolve_latest_valid_ranking_db",
]