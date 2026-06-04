# ============================================================
# File: database/migrate/migrate_main.py
# Ver35-STARTUP-LIGHT-SKIP-RANKING-MIGRATION
# ------------------------------------------------------------
# ✔ SAFE MIGRATION MODE 対応
# ✔ Lazy session 完全互換
# ✔ multi-DB 完全対応
# ✔ DuckDB summary 自動判定（dialect安全判定）
# ✔ SQLite summary fallback対応
# ✔ engines未定義問題完全排除
# ✔ 既存機能削除ゼロ
# ✔ 責務分離オーケストレーター
# ✔ 将来拡張耐性
# ✔ summary migration 正本を本ファイルへ一本化
# ✔ session.py 側の副作用migration前提を排除しても完全動作
# ✔ 接続確認・詳細ログ・例外トレース強化
# ✔ daily SQLite補正保持
# ✔ production hardened
# ✔ 起動時軽量 migration 追加
# ✔ 手動完全 migration 追加
# ✔ 既存 run_migration() 互換維持
# ✔ Ver35: 通常起動では ranking migration を既定スキップして起動遅延を防止
# ============================================================

from __future__ import annotations

import logging
import os
import traceback

import database.session as session

from .migrate_push import migrate_push
from .migrate_summary_duck import migrate_summary_duck
from .migrate_summary_sqlite import migrate_summary_sqlite
from .migrate_position import migrate_position
try:
    from .migrate_ranking import migrate_ranking
except ImportError:
    from .migrate_ranking import run_migration as migrate_ranking
from .migrate_tosama import migrate_tosama
from .migrate_yahoo import migrate_yahoo
from .migrate_daily_sqlite import migrate_daily_sqlite


logger = logging.getLogger(__name__)


# ============================================================
# INTERNAL UTIL
# ============================================================

def _is_duck_engine(engine) -> bool:
    """
    DuckDBエンジン安全判定（将来耐性）
    """
    try:
        if engine is None:
            return False

        if hasattr(engine, "dialect") and getattr(engine, "dialect", None) is not None:
            return str(getattr(engine.dialect, "name", "")).lower() == "duckdb"

        if hasattr(engine, "_engine") and getattr(engine, "_engine", None) is not None:
            inner = engine._engine
            if hasattr(inner, "dialect") and getattr(inner.dialect, "name", None) is not None:
                return str(getattr(inner.dialect, "name", "")).lower() == "duckdb"

    except Exception:
        logger.exception("❌ DuckDB engine 判定失敗")

    return False


def _safe_connect_check(name: str, engine):
    """
    エンジン接続確認（SAFE MIGRATION対応）
    """
    if engine is None:
        raise RuntimeError(f"{name} が None です")

    try:
        with engine.connect():
            pass
        logger.info("✅ %s 接続確認OK", name)

    except Exception as e:
        logger.exception("❌ %s 接続失敗", name)
        raise RuntimeError(f"{name} 接続失敗: {e}") from e


def _log_engine_info(name: str, engine) -> None:
    """
    エンジン情報の安全ログ出力
    """
    try:
        if engine is None:
            logger.warning("⚠ %s = None", name)
            return

        url = getattr(engine, "url", None)
        if url is not None:
            logger.info("📂 %s = %s", name, url)
            return

        if hasattr(engine, "_engine") and getattr(engine, "_engine", None) is not None:
            inner_url = getattr(engine._engine, "url", None)
            if inner_url is not None:
                logger.info("📂 %s = %s", name, inner_url)
                return

        logger.info("📂 %s = <url unavailable>", name)

    except Exception:
        logger.exception("❌ %s engine info log failed", name)


def _run_step(label: str, fn, *args, **kwargs):
    """
    個別migrationステップ安全実行
    """
    logger.info("🚀 %s start", label)
    result = fn(*args, **kwargs)
    logger.info("✅ %s done", label)
    return result


def _safe_bool_from_env(name: str, default: bool = False) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")
    except Exception:
        return bool(default)


def _get_engines():
    """
    Lazy安全取得（直接engine変数参照禁止）
    """
    push_engine = session.get_push_engine()
    summary_engine = session.get_summary_engine()
    position_engine = session.get_position_engine()
    ranking_engine = session.get_ranking_engine()
    tosama_engine = session.get_tosama_engine()

    return {
        "push_engine": push_engine,
        "summary_engine": summary_engine,
        "position_engine": position_engine,
        "ranking_engine": ranking_engine,
        "tosama_engine": tosama_engine,
    }


def _log_all_engine_info(engines: dict) -> None:
    for name, engine in engines.items():
        _log_engine_info(name, engine)


def _safe_connect_check_all(engines: dict) -> None:
    for name, engine in engines.items():
        _safe_connect_check(name, engine)


def _run_summary_migration(summary_engine, include_heavy_sqlite_rebuild: bool = True):
    """
    summary migration 実行

    include_heavy_sqlite_rebuild:
        True  -> 従来どおり正本 migration を実行
        False -> 起動時軽量モードとして SQLite summary はスキップ可能
                 （DuckDB は比較的軽いので実行継続）
    """
    if _is_duck_engine(summary_engine):
        logger.info("🦆 SUMMARY = DuckDB")
        _run_step("migrate_summary_duck", migrate_summary_duck, summary_engine)
        return

    logger.info("📦 SUMMARY = SQLite")

    if include_heavy_sqlite_rebuild:
        _run_step("migrate_summary_sqlite", migrate_summary_sqlite, summary_engine)
    else:
        logger.info(
            "⏭ migrate_summary_sqlite skipped in startup-light mode "
            "(heavy rebuild / duplicate cleanup / legacy UNIQUE repair deferred)"
        )


# ============================================================
# STARTUP LIGHT MIGRATION
# ============================================================

def run_startup_migration(
    include_summary_sqlite: bool = False,
    include_daily_sqlite: bool = False,
    include_ranking_migration: bool | None = None,
):
    """
    通常起動用の軽量 migration

    方針:
      - 起動時に毎回必要な create_all / ADD ONLY 系を中心に実行
      - NAS + SQLite + 複数スレッド環境で重くなりやすい
        summary SQLite rebuild / daily SQLite補修は既定でスキップ
      - ranking migration は DBロック待ちで main.py 起動を大きく遅らせるため既定スキップ
      - 必要時のみフラグで含める

    既定:
      - include_summary_sqlite   = False
      - include_daily_sqlite     = False
      - include_ranking_migration = False

    ranking migration を起動時に含めたい場合:
      STARTUP_INCLUDE_RANKING_MIGRATION=1
    """

    if include_ranking_migration is None:
        include_ranking_migration = _safe_bool_from_env(
            "STARTUP_INCLUDE_RANKING_MIGRATION",
            default=False,
        )

    print("⏳ DB起動時軽量マイグレーション開始")
    logger.info(
        "📦 run_startup_migration start include_summary_sqlite=%s include_daily_sqlite=%s include_ranking_migration=%s",
        include_summary_sqlite,
        include_daily_sqlite,
        include_ranking_migration,
    )

    engines = _get_engines()

    _log_all_engine_info(engines)
    _safe_connect_check_all(engines)

    push_engine = engines["push_engine"]
    summary_engine = engines["summary_engine"]
    position_engine = engines["position_engine"]
    ranking_engine = engines["ranking_engine"]
    tosama_engine = engines["tosama_engine"]

    try:
        # ====================================================
        # PUSH
        # ====================================================
        _run_step("migrate_push", migrate_push, push_engine)

        # ====================================================
        # SUMMARY
        #   DuckDBならそのまま実行
        #   SQLiteは既定でスキップ
        # ====================================================
        _run_summary_migration(
            summary_engine,
            include_heavy_sqlite_rebuild=include_summary_sqlite,
        )

        # ====================================================
        # POSITION
        # ====================================================
        _run_step("migrate_position", migrate_position, position_engine)

        # ====================================================
        # RANKING
        #   main.py通常起動ではスキップ。
        #   ranking DB writer / ranking summary job とロック競合しやすく、
        #   12:57起動→13:02でも抜けない原因になっていた。
        # ====================================================
        if include_ranking_migration:
            _run_step("migrate_ranking", migrate_ranking, ranking_engine)
        else:
            logger.info(
                "⏭ migrate_ranking skipped in startup-light mode "
                "(set STARTUP_INCLUDE_RANKING_MIGRATION=1 to enable; main_database.py/full migration handles schema)"
            )

        # ====================================================
        # TOSAMA
        # ====================================================
        _run_step("migrate_tosama", migrate_tosama, tosama_engine)

        # ====================================================
        # Yahoo intraday
        # ====================================================
        _run_step("migrate_yahoo", migrate_yahoo)

        # ====================================================
        # daily summary SQLite補正（起動時既定ではスキップ）
        # ====================================================
        if include_daily_sqlite:
            _run_step("migrate_daily_sqlite", migrate_daily_sqlite)
        else:
            logger.info("⏭ migrate_daily_sqlite skipped in startup-light mode")

        logger.info("✅ run_startup_migration complete")
        print("🎉 DB起動時軽量マイグレーション完了")

    except Exception:
        logger.error("❌ STARTUP LIGHT MIGRATION FAILED")
        traceback.print_exc()
        raise


# ============================================================
# FULL MIGRATION
# ============================================================

def run_full_migration(
    include_daily_sqlite: bool = True,
):
    """
    手動保守用の完全 migration

    方針:
      - 従来 run_migration() 相当
      - summary SQLite 正本 migration を実行
      - ranking migration も実行
      - daily SQLite補修も必要に応じて実行
    """

    print("⏳ DB完全マイグレーション開始")
    logger.info("📦 run_full_migration start")

    engines = _get_engines()

    _log_all_engine_info(engines)
    _safe_connect_check_all(engines)

    push_engine = engines["push_engine"]
    summary_engine = engines["summary_engine"]
    position_engine = engines["position_engine"]
    ranking_engine = engines["ranking_engine"]
    tosama_engine = engines["tosama_engine"]

    try:
        # ====================================================
        # PUSH
        # ====================================================
        _run_step("migrate_push", migrate_push, push_engine)

        # ====================================================
        # SUMMARY（完全実行）
        # ====================================================
        _run_summary_migration(
            summary_engine,
            include_heavy_sqlite_rebuild=True,
        )

        # ====================================================
        # POSITION
        # ====================================================
        _run_step("migrate_position", migrate_position, position_engine)

        # ====================================================
        # RANKING
        # ====================================================
        _run_step("migrate_ranking", migrate_ranking, ranking_engine)

        # ====================================================
        # TOSAMA
        # ====================================================
        _run_step("migrate_tosama", migrate_tosama, tosama_engine)

        # ====================================================
        # Yahoo intraday
        # ====================================================
        _run_step("migrate_yahoo", migrate_yahoo)

        # ====================================================
        # daily summary SQLite補正（旧資産保護）
        # ====================================================
        if include_daily_sqlite:
            _run_step("migrate_daily_sqlite", migrate_daily_sqlite)
        else:
            logger.info("⏭ migrate_daily_sqlite skipped by option")

        logger.info("✅ run_full_migration complete")
        print("🎉 DB完全マイグレーション完了")

    except Exception:
        logger.error("❌ FULL MIGRATION FAILED")
        traceback.print_exc()
        raise


# ============================================================
# BACKWARD COMPATIBILITY
# ============================================================

def run_migration():
    """
    後方互換API

    従来コード互換のため残す。
    既存の run_migration() は「完全 migration」として扱う。
    """
    return run_full_migration(include_daily_sqlite=True)


# ============================================================
# CLI SUPPORT
# ============================================================

if __name__ == "__main__":
    """
    既定:
      - STARTUP_LIGHT_MIGRATION=1 -> 軽量 migration
      - それ以外                  -> 完全 migration

    追加フラグ:
      - STARTUP_INCLUDE_SUMMARY_SQLITE=1
          軽量 migrationでも SQLite summary migration を含める
      - STARTUP_INCLUDE_DAILY_SQLITE=1
          軽量 migrationでも daily SQLite補修を含める
      - STARTUP_INCLUDE_RANKING_MIGRATION=1
          軽量 migrationでも ranking migration を含める
      - FULL_INCLUDE_DAILY_SQLITE=0
          完全 migrationで daily SQLite補修をスキップする
    """
    startup_mode = _safe_bool_from_env("STARTUP_LIGHT_MIGRATION", default=False)

    if startup_mode:
        run_startup_migration(
            include_summary_sqlite=_safe_bool_from_env(
                "STARTUP_INCLUDE_SUMMARY_SQLITE",
                default=False,
            ),
            include_daily_sqlite=_safe_bool_from_env(
                "STARTUP_INCLUDE_DAILY_SQLITE",
                default=False,
            ),
            include_ranking_migration=_safe_bool_from_env(
                "STARTUP_INCLUDE_RANKING_MIGRATION",
                default=False,
            ),
        )
    else:
        run_full_migration(
            include_daily_sqlite=_safe_bool_from_env(
                "FULL_INCLUDE_DAILY_SQLITE",
                default=True,
            )
        )
