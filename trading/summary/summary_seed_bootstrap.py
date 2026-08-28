# ============================================================
# summary_seed_bootstrap.py
# Ver1.1-PREV-DAY-SEED-COPY-MAIN-GUARD (R18)
# ------------------------------------------------------------
# ✔ 前日summaryDBから最終bars本コピー
# ✔ 1min / 3min / 5min対応
# ✔ 当日DB未存在でも自動作成
# ✔ 高速
#
# R18 での変更 (処理内容は変えていない):
#   - `if __name__ == "__main__"` ガードを付けた。
#     旧版は module-level スクリプトで、**import しただけで当日 DB へ
#     120 行 append していた**。テストの import smoke で実際に
#     summary20260828.db へ書き込む事故が起きた (削除済み)。
#     このファイルはコード中のどこからも import / 参照されていない
#     単体スクリプトなので、ガードを付けても script 実行の挙動は変わらない。
#   - engine を finally で dispose するようにした
#     (SQLAlchemy engine の閉じ忘れは NAS 上でハンドル枯渇の原因になる)。
#
# 注意: 旧コメントに「重複防止」とあるが実際には効いていない。
#   前日 DB の行を **id ごと** コピーして if_exists="append" するので、
#   同じ日に 2 回走らせると
#   `UNIQUE constraint failed: stock_summary_1min.id` で落ちる。
#   冪等ではないので、実行は 1 日 1 回に限ること。
# ============================================================

import datetime as dt
import logging
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from nas_root import nas_path, nas_root

logger = logging.getLogger("seed_bootstrap")

BASE_DIR = Path(
    nas_path(r"raw_data\kabu_station\summary")
)

BARS = 120

TABLES = [
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
]


def main() -> int:
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)

    today_str = today.strftime("%Y%m%d")
    yesterday_str = yesterday.strftime("%Y%m%d")

    today_db = BASE_DIR / f"summary{today_str}.db"
    yesterday_db = BASE_DIR / f"summary{yesterday_str}.db"

    if not yesterday_db.exists():
        logger.warning("No yesterday DB found. Skipping seed bootstrap.")
        return 0

    today_engine = create_engine(f"sqlite:///{today_db}")
    yesterday_engine = create_engine(f"sqlite:///{yesterday_db}")

    try:
        for table in TABLES:

            logger.info(f"Bootstrapping {table}")

            # 前日最終bars本取得
            df = pd.read_sql(
                text(f"""
                    SELECT *
                    FROM {table}
                    ORDER BY datetime DESC
                    LIMIT :bars
                """),
                yesterday_engine,
                params={"bars": BARS},
            )

            if df.empty:
                logger.warning(f"No rows found in {table}")
                continue

            # datetime順に戻す
            df = df.sort_values("datetime")

            # 当日DBへ挿入
            df.to_sql(
                table,
                today_engine,
                if_exists="append",
                index=False,
            )
    finally:
        for eng in (today_engine, yesterday_engine):
            try:
                eng.dispose()
            except Exception:
                logger.debug("engine dispose failed", exc_info=True)

    logger.info("=== SEED BOOTSTRAP DONE ===")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
