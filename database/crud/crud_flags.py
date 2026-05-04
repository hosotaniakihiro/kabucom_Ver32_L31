# ============================================================
# database/crud/crud_flags.py
# Ver1.5-FINAL-SYMBOL-FLAGS-CRUD-LOADABLE
# ------------------------------------------------------------
# ✔ SymbolFlags 専用 CRUD
# ✔ DB 欠損・カラム欠損に完全耐性
# ✔ ats_ok / push_ok / short_sellable 等を安全に扱う
# ✔ セッションリーク防止
# ✔ 例外は握りつぶさずログ出力
# ✔ load_symbol_flags 提供（runtime 初期化用）
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Any, Iterable, Optional

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from database import Session_position
from database.models import SymbolFlags

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _has_column(col_name: str) -> bool:
    """
    SymbolFlags に指定カラムが存在するか
    """
    try:
        mapper = inspect(SymbolFlags)
        return col_name in mapper.columns
    except Exception:
        return False


def _safe_set(obj, key: str, value: Any):
    """
    カラムが存在する場合のみ setattr
    """
    if _has_column(key):
        setattr(obj, key, value)


# ============================================================
# READ
# ============================================================

def get_symbol_flags(symbol: str) -> Dict[str, Any]:
    """
    単一銘柄のフラグを dict で取得
    - 存在しない場合は空 dict
    """
    session = Session_position()
    try:
        row = (
            session.query(SymbolFlags)
            .filter(SymbolFlags.symbol == str(symbol))
            .one_or_none()
        )
        if not row:
            return {}

        mapper = inspect(SymbolFlags)
        return {
            col.key: getattr(row, col.key)
            for col in mapper.columns
        }

    except Exception:
        logger.exception("get_symbol_flags failed symbol=%s", symbol)
        return {}

    finally:
        session.close()


def get_all_symbol_flags() -> Dict[str, Dict[str, Any]]:
    """
    全銘柄分の flags を
    {symbol: {flag: value}} 形式で取得
    """
    session = Session_position()
    result: Dict[str, Dict[str, Any]] = {}

    try:
        rows = session.query(SymbolFlags).all()
        mapper = inspect(SymbolFlags)

        for row in rows:
            sym = str(row.symbol)
            result[sym] = {
                col.key: getattr(row, col.key)
                for col in mapper.columns
            }

        return result

    except Exception:
        logger.exception("get_all_symbol_flags failed")
        return {}

    finally:
        session.close()


# ============================================================
# ★ LOAD（runtime / push / ATS 初期化用）
# ============================================================

def load_symbol_flags() -> Dict[str, Dict[str, Any]]:
    """
    全銘柄の SymbolFlags を一括ロードする
    - global_data.symbol_flags にそのまま渡せる
    """
    flags = get_all_symbol_flags()
    logger.info("🚩 SymbolFlags loaded count=%d", len(flags))
    return flags


# ============================================================
# CREATE / UPDATE
# ============================================================

def upsert_symbol_flags(
    symbol: str,
    *,
    flags: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> bool:
    """
    SymbolFlags を upsert
    - flags dict または kwargs で指定
    - 存在しなければ CREATE
    """
    session = Session_position()
    try:
        row = (
            session.query(SymbolFlags)
            .filter(SymbolFlags.symbol == str(symbol))
            .one_or_none()
        )

        if row is None:
            row = SymbolFlags(symbol=str(symbol))
            session.add(row)

        data = {}
        if flags:
            data.update(flags)
        data.update(kwargs)

        for k, v in data.items():
            _safe_set(row, k, v)

        session.commit()
        return True

    except SQLAlchemyError:
        session.rollback()
        logger.exception("upsert_symbol_flags failed symbol=%s", symbol)
        return False

    finally:
        session.close()


def bulk_upsert_symbol_flags(
    items: Iterable[Dict[str, Any]],
    *,
    symbol_key: str = "symbol",
) -> int:
    """
    複数銘柄をまとめて upsert
    items: [{symbol: "7203", ats_ok: 1, push_ok: 1}, ...]
    戻り値: 成功件数
    """
    session = Session_position()
    success = 0

    try:
        for item in items:
            symbol = item.get(symbol_key)
            if not symbol:
                continue

            row = (
                session.query(SymbolFlags)
                .filter(SymbolFlags.symbol == str(symbol))
                .one_or_none()
            )

            if row is None:
                row = SymbolFlags(symbol=str(symbol))
                session.add(row)

            for k, v in item.items():
                if k == symbol_key:
                    continue
                _safe_set(row, k, v)

            success += 1

        session.commit()
        return success

    except SQLAlchemyError:
        session.rollback()
        logger.exception("bulk_upsert_symbol_flags failed")
        return success

    finally:
        session.close()


# ============================================================
# DELETE
# ============================================================

def delete_symbol_flags(symbol: str) -> bool:
    """
    単一銘柄の flags を削除
    """
    session = Session_position()
    try:
        cnt = (
            session.query(SymbolFlags)
            .filter(SymbolFlags.symbol == str(symbol))
            .delete()
        )
        session.commit()
        return cnt > 0

    except SQLAlchemyError:
        session.rollback()
        logger.exception("delete_symbol_flags failed symbol=%s", symbol)
        return False

    finally:
        session.close()


def delete_all_symbol_flags() -> int:
    """
    全削除（危険・テスト用）
    戻り値: 削除件数
    """
    session = Session_position()
    try:
        cnt = session.query(SymbolFlags).delete()
        session.commit()
        logger.warning("ALL SymbolFlags deleted count=%d", cnt)
        return cnt

    except SQLAlchemyError:
        session.rollback()
        logger.exception("delete_all_symbol_flags failed")
        return 0

    finally:
        session.close()


# ============================================================
# 高頻度ユースケース用ショートカット
# ============================================================

def is_ats_ok(symbol: str) -> bool:
    """
    ATS 監視可否
    - カラムが無い場合は True（安全側）
    """
    if not _has_column("ats_ok"):
        return True

    flags = get_symbol_flags(symbol)
    return bool(flags.get("ats_ok", 1))


def is_push_ok(symbol: str) -> bool:
    """
    PUSH 監視可否
    - カラムが無い場合は True（安全側）
    """
    if not _has_column("push_ok"):
        return True

    flags = get_symbol_flags(symbol)
    return bool(flags.get("push_ok", 1))


def is_short_sellable(symbol: str) -> bool:
    """
    信用新規売り可否
    - カラムが無い場合は False（安全側）
    """
    if not _has_column("short_sellable"):
        return False

    flags = get_symbol_flags(symbol)
    return bool(flags.get("short_sellable", 0))
