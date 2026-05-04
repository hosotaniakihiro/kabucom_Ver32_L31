# database/crud/crud_summary.py
import logging
import datetime as dt
import pandas as pd
from sqlalchemy.exc import SQLAlchemyError
from database.session import Session_summary

logger = logging.getLogger(__name__)


# =========================================================
# 🔹 ユーティリティ関数
# =========================================================
def _extract_end_time(time_range_str: str):
    """'HH:MM - HH:MM' または 'HH:MM-HH:MM' から終了時刻を抽出"""
    try:
        if not time_range_str or pd.isna(time_range_str):
            return None
        parts = str(time_range_str).replace(" ", "").split("-")
        end_str = parts[-1]
        return pd.to_datetime(end_str, format="%H:%M", errors="coerce").time()
    except Exception as e:
        logger.warning(f"end_time抽出失敗: {time_range_str} ({e})")
        return None


def _clean_value(v):
    """DBに保存可能なスカラ値へ強制変換"""
    import numpy as np
    if pd.isna(v):
        return None
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date, dt.time)):
        return v
    if isinstance(v, (np.generic,)):
        try:
            return v.item()
        except Exception:
            return None
    if isinstance(v, (list, dict, set, tuple)):
        return str(v)
    return v


# =========================================================
# 🔹 サマリーデータ保存関数（SQLite対応版）
# =========================================================
def store_summary_data_batch(df_summary: pd.DataFrame, model, session=None):
    """
    サマリー結果をDBに保存（SQLite対応版）
    - symbol + date + time_range で一意
    - start_time / end_time を time_range から補完し、Python time型に変換
    - SQLite TIME型エラー防止対応
    """
    if df_summary is None or df_summary.empty:
        logger.warning("⚠️ store_summary_data_batch: 空データのためスキップ")
        return

    df = df_summary.copy()

    # =====================================================
    # ✅ start_time / end_time 自動補完
    # =====================================================
    if "time_range" in df.columns and ("start_time" not in df.columns or "end_time" not in df.columns):
        logger.info("🕒 start_time / end_time 自動補完開始")

        def _split_time_range(tr):
            try:
                parts = str(tr).replace("−", "-").replace("〜", "-").replace("～", "-").split("-")
                if len(parts) == 2:
                    return parts[0].strip(), parts[1].strip()
            except Exception:
                pass
            return None, None

        start_end = df["time_range"].apply(_split_time_range)
        df["start_time"] = [s for s, _ in start_end]
        df["end_time"] = [e for _, e in start_end]
        logger.info(f"✅ start_time / end_time 補完完了 ({len(df)}件)")

    # =====================================================
    # ✅ 型正規化（SQLite TIME対応）
    # =====================================================
    try:
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date

        # Python time型への変換（Timestampエラー防止）
        def to_time_or_none(v):
            if pd.isna(v) or v in [None, "NaT", "nan", ""]:
                return None
            try:
                if isinstance(v, dt.time):
                    return v
                if isinstance(v, dt.datetime):
                    return v.time()
                # pandas Timestamp対応
                v2 = pd.to_datetime(str(v), errors="coerce")
                if pd.isna(v2):
                    return None
                return v2.time()
            except Exception:
                return None

        for col in ["start_time", "end_time"]:
            if col in df.columns:
                df[col] = df[col].apply(to_time_or_none)

        # datetime型に正規化（NaTはNone化）
        for col in ["time", "last_update"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce").apply(
                    lambda x: x.to_pydatetime() if pd.notna(x) else None
                )

        # NaN → None
        df = df.where(pd.notnull(df), None)

    except Exception as e:
        logger.error(f"⚠️ 型変換エラー: {e}", exc_info=True)

    # =====================================================
    # ✅ DB保存処理
    # =====================================================
    close_after = False
    if session is None:
        session = Session_summary()
        close_after = True

    try:
        # --- 重複削除（symbol + date + time_range）---
        delete_keys = df[["symbol", "date", "time_range"]].drop_duplicates()
        for _, row in delete_keys.iterrows():
            session.query(model).filter_by(
                symbol=row["symbol"],
                date=row["date"],
                time_range=row["time_range"]
            ).delete()

        # --- 一括INSERT ---
        session.bulk_insert_mappings(model, df.to_dict(orient="records"))
        session.commit()
        logger.info(f"💾 {model.__tablename__}: {len(df)} 件を保存しました")

    except SQLAlchemyError as e:
        session.rollback()
        logger.error(f"❌ store_summary_data_batch DBエラー: {e}", exc_info=True)

    except Exception as e:
        session.rollback()
        logger.error(f"❌ 予期しないDBエラー: {e}", exc_info=True)

    finally:
        if close_after:
            session.close()


# =========================================================
# 🔹 最新サマリー取得
# =========================================================
def get_latest_summary(symbol: str, model, session=None):
    """
    指定銘柄の最新サマリーデータを取得
    """
    close_session = False
    if session is None:
        session = Session_summary()
        close_session = True

    try:
        return (
            session.query(model)
            .filter(model.symbol == symbol)
            .order_by(model.date.desc(), model.time_range.desc())
            .first()
        )
    except SQLAlchemyError as e:
        logger.error(f"❌ get_latest_summary エラー: {e}", exc_info=True)
        return None
    finally:
        if close_session:
            session.close()
