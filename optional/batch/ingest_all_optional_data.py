# ============================================================
# File   : optional/batch/ingest_all_optional_data.py
# Version: Ver2.0-PRODUCTION-OPTIONAL-INGEST-ALL-SYMBOL-FLAGS-PATH-FIX-FINAL
# ------------------------------------------------------------
# ✔ 既存 ingest 機能を一切削除しない
# ✔ config.paths の optional_db / symbol_flags_db 分離に正式対応
# ✔ update_symbol_flags の参照先DB誤りを修正
# ✔ raw_kabutan / raw_kabu の既存運用を完全維持
# ✔ news_events upsert / margin master / watchlist の既存挙動維持
# ✔ path / logging / validation を本番向けに強化
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime

import pandas as pd

from config.paths import get_path

# ------------------------------------------------------------
# scrapers
# ------------------------------------------------------------
from optional.scrapers.ingest_kabutan_marketnews import ingest_kabutan_marketnews
from optional.scrapers.kabutan_kessan import fetch_kabutan_kessan
from optional.scrapers.fetch_kabutan_rise_rate import fetch_kabutan_rise_rate
from optional.scrapers.fetch_kabutan_stop_high import fetch_kabutan_stop_high

# ★ NEW
from optional.scrapers.tomorrow_auto_pipeline import (
    build_tomorrow_dataset_auto,
)

# ------------------------------------------------------------
# batch系
# ------------------------------------------------------------
from optional.batch.ingest_margin_master_from_excel import ingest_margin_master_from_excel
from optional.batch.update_symbol_flags import update_symbol_flags
from optional.batch.build_daily_watchlist import (
    build_daily_watchlist,
    save_watchlist,
)

# ------------------------------------------------------------
# DB
# ------------------------------------------------------------
from optional.db.news_events_upserter import upsert_news_events

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# paths
# ------------------------------------------------------------
DB_OPTIONAL: Path = get_path("optional_db")
DB_SYMBOL_FLAGS: Path = get_path("symbol_flags_db")
RAW_KABUTAN: Path = get_path("raw_kabutan")
RAW_KABU: Path = get_path("raw_kabu")


# ============================================================
# path check
# ============================================================
def _check_paths() -> None:
    if not DB_OPTIONAL.exists():
        raise FileNotFoundError(f"optional DB not found: {DB_OPTIONAL}")

    if not DB_SYMBOL_FLAGS.exists():
        raise FileNotFoundError(f"symbol_flags DB not found: {DB_SYMBOL_FLAGS}")

    for p in (RAW_KABUTAN, RAW_KABU):
        if not p.exists():
            logger.warning("⚠ raw directory not found: %s", p)


# ============================================================
# news_events統合ヘルパー
# ============================================================
def _upsert_if_not_empty(df: pd.DataFrame, label: str) -> None:
    if df is None or df.empty:
        logger.info("ℹ %s empty -> skip", label)
        return

    try:
        result = upsert_news_events(df, db_path=DB_OPTIONAL)
        logger.info(
            "💾 %s saved inserted=%d skipped=%d",
            label,
            int(result.get("inserted", 0)),
            int(result.get("skipped", 0)),
        )
    except Exception:
        logger.exception("❌ %s upsert failed", label)


# ============================================================
# main
# ============================================================
def ingest_all(trade_date: str) -> None:
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("trade_date must be YYYY-MM-DD")

    _check_paths()

    logger.info("=" * 60)
    logger.info("🚀 OPTIONAL ingest START")
    logger.info(" trade_date       = %s", trade_date)
    logger.info(" DB_OPTIONAL      = %s", DB_OPTIONAL)
    logger.info(" DB_SYMBOL_FLAGS  = %s", DB_SYMBOL_FLAGS)
    logger.info(" RAW_KABUTAN      = %s", RAW_KABUTAN)
    logger.info(" RAW_KABU         = %s", RAW_KABU)
    logger.info("=" * 60)

    # ========================================================
    # ① kabutan marketnews（明日の材料 / 注目ニュース）
    # ========================================================
    try:
        df_marketnews = ingest_kabutan_marketnews(
            trade_date=trade_date,
            db_path=DB_OPTIONAL,
            base_dir=RAW_KABUTAN,
        )
        _upsert_if_not_empty(df_marketnews, "marketnews")
    except Exception:
        logger.exception("❌ kabutan marketnews ingest failed")

    # ========================================================
    # ★ NEW ② 明日の好悪材料（自動検出）
    # ========================================================
    try:
        df_tomorrow = build_tomorrow_dataset_auto(trade_date=trade_date)
        _upsert_if_not_empty(df_tomorrow, "tomorrow_material")
    except Exception:
        logger.exception("❌ tomorrow_material ingest failed")

    # ========================================================
    # ③ kabutan warning（上昇率 / 下落率）
    # ========================================================
    try:
        rise_data = fetch_kabutan_rise_rate(trade_date)
        if rise_data:
            df_rise = pd.DataFrame(rise_data)
            _upsert_if_not_empty(df_rise, "rise/fall_rate")
        else:
            logger.info("ℹ rise/fall_rate empty")
    except Exception:
        logger.exception("❌ rise/fall_rate ingest failed")

    # ========================================================
    # ④ kabutan stop（高 / 安）
    # ========================================================
    try:
        stop_data = fetch_kabutan_stop_high(trade_date)
        if stop_data:
            df_stop = pd.DataFrame(stop_data)
            _upsert_if_not_empty(df_stop, "stop_high/low")
        else:
            logger.info("ℹ stop_high/low empty")
    except Exception:
        logger.exception("❌ stop_high ingest failed")

    # ========================================================
    # ⑤ kabutan kessan（決算速報）
    # ========================================================
    try:
        df_kessan = fetch_kabutan_kessan(trade_date)
        _upsert_if_not_empty(df_kessan, "kessan")
    except Exception:
        logger.exception("❌ kessan ingest failed")

    # ========================================================
    # ⑥ 信用取引マスタ
    # ========================================================
    try:
        ingest_margin_master_from_excel(
            db_path=DB_OPTIONAL,
            base_dir=RAW_KABU,
        )
        logger.info("✅ margin master ingested")
    except Exception:
        logger.exception("❌ margin master ingest failed")

    # ========================================================
    # ⑦ symbol_flags 更新
    #   ★ optional_db ではなく symbol_flags_db を更新する
    # ========================================================
    try:
        logger.info("🔄 update_symbol_flags start db=%s", DB_SYMBOL_FLAGS)
        update_symbol_flags(db_path=DB_SYMBOL_FLAGS)
        logger.info("✅ symbol_flags updated")
    except Exception:
        logger.exception("❌ update_symbol_flags failed")

    # ========================================================
    # ⑧ daily_watchlist 作成
    # ========================================================
    try:
        rows = build_daily_watchlist(trade_date)
        save_watchlist(rows)
        logger.info("✅ daily_watchlist built rows=%d", len(rows))
    except Exception:
        logger.exception("❌ daily_watchlist build failed")

    logger.info("=" * 60)
    logger.info("🎉 OPTIONAL ingest ALL DONE")
    logger.info("=" * 60)


# ============================================================
# entry
# ============================================================
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python ingest_all_optional_data.py YYYY-MM-DD")
        sys.exit(1)

    ingest_all(sys.argv[1])