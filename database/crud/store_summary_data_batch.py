#database/crud/store_summary_data_batch.py

import logging
from sqlalchemy.exc import SQLAlchemyError
from database import Session_summary

logger = logging.getLogger(__name__)


def store_summary_data_batch(df_summary, model, session=None, commit=True):
    """
    サマリーデータをDBへバルク保存（start_time / end_time対応）
    - model: StockSummary1Min / 3Min / 5Min
    - 重複キー(symbol, date, time_range)は上書き更新
    """
    if df_summary is None or df_summary.empty:
        logger.warning("⚠️ store_summary_data_batch: 保存対象が空です")
        return 0

    close_session = False
    if session is None:
        session = Session_summary()
        close_session = True

    try:
        inserted = 0

        # --- DataFrameを辞書に変換 ---
        records = df_summary.to_dict(orient="records")

        for rec in records:
            symbol = rec.get("symbol")
            date = rec.get("date")
            time_range = rec.get("time_range")

            if not symbol or not time_range:
                continue

            # --- 既存行チェック ---
            existing = (
                session.query(model)
                .filter_by(symbol=symbol, date=date, time_range=time_range)
                .first()
            )

            if existing:
                # --- 更新 ---
                for k, v in rec.items():
                    if hasattr(existing, k):
                        setattr(existing, k, v)
            else:
                # --- 新規作成 ---
                new_obj = model(**{k: v for k, v in rec.items() if hasattr(model, k)})
                session.add(new_obj)
                inserted += 1

        if commit:
            session.commit()

        logger.info(f"💾 {model.__tablename__}: {inserted}件 追加/更新完了")
        return inserted

    except SQLAlchemyError as e:
        session.rollback()
        logger.error(f"❌ store_summary_data_batch SQLAlchemyError: {e}", exc_info=True)
    except Exception as e:
        session.rollback()
        logger.error(f"❌ store_summary_data_batch 予期せぬエラー: {e}", exc_info=True)
    finally:
        if close_session:
            session.close()

    return 0
