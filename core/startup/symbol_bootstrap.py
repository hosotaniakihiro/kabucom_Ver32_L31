# ============================================================
# File   : core/startup/symbol_bootstrap.py
# Ver    : PRODUCTION-STABLE-REV2-ACTIVE-GUARD
# ------------------------------------------------------------
# ✔ symbol_flags.db 読み込み
# ✔ symbol_name_map 構築
# ✔ symbol_flags 辞書化
# ✔ symbols_light 初期化
# ✔ symbols_active 初期化
# ✔ monitor_symbols 構築
# ✔ active_symbols 強制更新
# ✔ ★ active 空時 fallback 追加（NEW）
# ✔ ★ monitor 空時 fallback 追加（NEW）
# ✔ 例外耐性最大化
# ✔ 空データ時停止（安全設計）
# ✔ 将来ランキングベース preload 対応準備
# ✔ REV9 完全互換
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, List

from symbol_loader import load_symbol_flags_df
from global_state import global_data
from trading.monitor.monitor_symbol_builder import build_monitor_symbols
from trading.ranking.active_symbol_manager import update_active_symbols

logger = logging.getLogger(__name__)


# ============================================================
# 内部：flags DataFrame 検証
# ============================================================

def _validate_flags_df(df):
    required_cols = {"symbol", "symbolname"}

    if df is None or df.empty:
        raise RuntimeError("symbol_flags empty")

    missing = required_cols - set(df.columns)
    if missing:
        raise RuntimeError(f"symbol_flags missing columns: {missing}")


# ============================================================
# 外部公開：symbol bootstrap
# ============================================================

def bootstrap_symbols():
    """
    ✔ symbol_flags 読み込み
    ✔ global_data へ安全セット
    ✔ active_symbols 更新
    ✔ monitor_symbols 構築
    ✔ ★ 空状態ガード（push停止防止）
    """

    logger.info("🔖 symbol bootstrap start")

    # --------------------------------------------------------
    # symbol_flags 読み込み
    # --------------------------------------------------------
    try:
        flags = load_symbol_flags_df()
    except Exception:
        logger.exception("❌ symbol_flags load failed")
        raise

    _validate_flags_df(flags)

    # --------------------------------------------------------
    # 正規化
    # --------------------------------------------------------
    flags = flags.copy()
    flags["symbol"] = flags["symbol"].astype(str).str.strip()
    flags["symbolname"] = flags["symbolname"].astype(str).str.strip()

    # --------------------------------------------------------
    # 基本セット
    # --------------------------------------------------------
    symbols_all: List[str] = flags["symbol"].tolist()

    global_data.symbols = symbols_all
    global_data.symbols_light = set(symbols_all)

    # 名前マップ
    global_data.symbol_name_map: Dict[str, str] = dict(
        zip(flags["symbol"], flags["symbolname"])
    )

    # フラグ辞書
    global_data.symbol_flags = {
        str(row["symbol"]): row.to_dict()
        for _, row in flags.iterrows()
    }

    # --------------------------------------------------------
    # active 初期化（空）
    # --------------------------------------------------------
    global_data.symbols_active = set()

    # --------------------------------------------------------
    # monitor_symbols 構築
    # --------------------------------------------------------
    try:
        global_data.monitor_symbols = build_monitor_symbols()
    except Exception:
        logger.exception("⚠ monitor_symbols build failed")
        global_data.monitor_symbols = set()

    # --------------------------------------------------------
    # active_symbols 強制更新（rankingベース）
    # --------------------------------------------------------
    try:
        update_active_symbols(force=True)
    except Exception:
        logger.exception("⚠ update_active_symbols failed")

    # ========================================================
    # 🔥 NEW: active 空ガード（push停止防止）
    # ========================================================
    if not global_data.symbols_active:
        logger.warning(
            "⚠ active empty → fallback to symbols_light (%d symbols)",
            len(global_data.symbols_light),
        )
        global_data.symbols_active = set(global_data.symbols_light)

    # ========================================================
    # 🔥 NEW: monitor 空ガード（subscribe防止）
    # ========================================================
    if not global_data.monitor_symbols:
        logger.warning(
            "⚠ monitor empty → fallback to active (%d symbols)",
            len(global_data.symbols_active),
        )
        global_data.monitor_symbols = set(global_data.symbols_active)

    # ========================================================
    # 最終ログ
    # ========================================================
    logger.info(
        "🔖 symbol bootstrap complete total=%d active=%d monitor=%d",
        len(global_data.symbols),
        len(global_data.symbols_active),
        len(global_data.monitor_symbols),
    )