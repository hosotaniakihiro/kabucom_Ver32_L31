# ============================================================
# File   : config/paths.py
# ------------------------------------------------------------
# Centralized path configuration (X drive / NAS compatible)
# ------------------------------------------------------------
# ✔ ENV 切替対応（local / nas）
# ✔ 論理パスは PATHS に一元管理
# ✔ raw_data/AI を AIイベントDBの唯一の正とする
# ✔ 既存キー・意味・互換性は一切破壊しない
# ✔ AI専用DB（regime / bandit）を ai/ 以下に正式統合
# ============================================================

from pathlib import Path
import os

# ------------------------------------------------------------
# Environment
# ------------------------------------------------------------
ENV = os.getenv("KABU_ENV", "local")

BASE_PATHS = {
    # "local": Path("X:/"),
    "local": Path("//192.168.0.22/AutoStockBuyAndSell"),
    "nas":   Path("//192.168.0.22/AutoStockBuyAndSell"),
}

BASE_DIR = BASE_PATHS.get(ENV)
if BASE_DIR is None:
    raise ValueError(f"Invalid KABU_ENV: {ENV}")

# ------------------------------------------------------------
# ★ AI ROOT（NEW：公式AI保存領域）
# ------------------------------------------------------------
AI_BASE_DIR = BASE_DIR / "ai"

AI_REGIME_DIR = AI_BASE_DIR / "regime"
AI_BANDIT_DIR = AI_BASE_DIR / "bandit"

REGIME_DB_PATH = AI_REGIME_DIR / "market_regime_log.db"
BANDIT_DB_PATH = AI_BANDIT_DIR / "bandit_exit_state.db"

# ------------------------------------------------------------
# Logical paths（★全体仕様の中核）
# ------------------------------------------------------------
PATHS = {

    # ========================================================
    # ROOT / BASE
    # ========================================================
    "base":                 BASE_DIR,

    # ========================================================
    # Basic / Master
    # ========================================================
    "basic":                BASE_DIR / "Basic",
    "symbol_flags_db":      BASE_DIR / "Basic/symbol_flags.db",
    "symbol_master_db":     BASE_DIR / "Basic/symbol_master.db",
    "all_symbols":          BASE_DIR / "Basic/AllSymbols",
    "taisyaku":             BASE_DIR / "Basic/taisyaku",
    # ============================================================
    # ORDER BOOK RAW DB PATH（NEW）
    # ============================================================

    "raw_order_book": BASE_DIR / "raw_data" / "order_book",
    # ========================================================
    # Logs
    # ========================================================
    "log_root":             BASE_DIR / "logs",
    "log_entry_exit":       BASE_DIR / "logs/entry_exit",
    "log_errors":           BASE_DIR / "logs/errors",
    "log_runtime":          BASE_DIR / "logs/runtime",

    # ========================================================
    # Raw data（再生成可能）
    # ========================================================
    "raw_root":             BASE_DIR / "raw_data",

    # --------------------------------------------------------
    # ★ raw_data/AI（AIイベント・学習の唯一の正）
    # --------------------------------------------------------
    "raw_ai":               BASE_DIR / "raw_data/AI",
    "ai_entry_events_db":   BASE_DIR / "raw_data/AI/ai_entry_events.db",

    # --- Kabutan ---
    "raw_kabutan":          BASE_DIR / "raw_data/kabutan",
    "raw_kabutan_db":       BASE_DIR / "raw_data/kabutan/optional_data.db",
    "raw_stock_data":       BASE_DIR / "raw_data/stock",

    # ========================================================
    # ★ kabuステーション 生データ
    # ========================================================
    "raw_kabu":             BASE_DIR / "raw_data/kabu_station",
    "raw_kabu_excel":       BASE_DIR / "raw_data/kabu_station",
    "raw_kabu_station":     BASE_DIR / "raw_data/kabu_station",

    # --------------------------------------------------------
    # ★ PUSH データ（唯一の正解）
    # --------------------------------------------------------
    "raw_push":             BASE_DIR / "raw_data/kabu_station/push",
    "raw_push_logs":        BASE_DIR / "raw_data/kabu_station/push_logs",

    # --------------------------------------------------------
    # ranking / summary
    # --------------------------------------------------------
    "raw_ranking":          BASE_DIR / "raw_data/kabu_station/ranking",
    "raw_summary":          BASE_DIR / "raw_data/kabu_station/summary",

    # --------------------------------------------------------
    # alias（旧コード互換・意味は同じ）
    # --------------------------------------------------------
    "push":                 BASE_DIR / "raw_data/kabu_station/push",
    "summary":              BASE_DIR / "raw_data/kabu_station/summary",
    "ranking":              BASE_DIR / "raw_data/kabu_station/ranking",
    "summary_db_dir":       BASE_DIR / "raw_data/kabu_station/summary",

    # --------------------------------------------------------
    # ⚠️ legacy / 注意
    # --------------------------------------------------------
    "raw_data":             BASE_DIR / "raw_data",

    # ========================================================
    # ★ SBI 寄り前ランキング（raw）
    # ========================================================
    "raw_yorimae_ranking":  BASE_DIR / "raw_data/SBI/SBI_yorimae_ranking",

    # ========================================================
    # TradingView / Yahoo
    # ========================================================
    "raw_tradingview":      BASE_DIR / "raw_data/tradingview",
    "raw_tv_csv":           BASE_DIR / "raw_data/tradingview/csv",
    "raw_yahoo":            BASE_DIR / "raw_data/yahoo",
    "raw_yahoo_intraday":   BASE_DIR / "raw_data/yahoo/intraday",

    # ========================================================
    # optional 系（DB はここに統一）
    # ========================================================
    "optional_db":          BASE_DIR / "raw_data/kabutan/optional_data.db",

    # ========================================================
    # Temporary
    # ========================================================
    "tmp":                  BASE_DIR / "tmp",
    "tmp_pc_realtime":      BASE_DIR / "tmp/pc_realtime",

    # ========================================================
    # Trading runtime / AI（実行系）
    # ========================================================
    "trading":              BASE_DIR / "trading",

    # --- AI 共通 ---
    "ai_root":              BASE_DIR / "trading/ai",
    "ai_data":              BASE_DIR / "trading/ai/data",
    "ai_models":            BASE_DIR / "trading/ai/models",
    "ai_training_logs":     BASE_DIR / "trading/ai/training_logs",

    # --- AI ENTRY ---
    "ai_models_immediate":  BASE_DIR / "trading/ai/models/immediate",
    "ai_models_mtf":        BASE_DIR / "trading/ai/models/mtf",

    # --- AI EXIT ---
    "ai_model_exit":        BASE_DIR / "trading/ai/models/exit",
    "ai_train_exit":        BASE_DIR / "trading/ai/train_data/exit",
    "ai_logs_exit":         BASE_DIR / "trading/ai/logs/exit",

    # ========================================================
    # ★ AI DB（NEW：正式保存領域）
    # ========================================================
    "ai_base_dir":          AI_BASE_DIR,
    "ai_regime_dir":        AI_REGIME_DIR,
    "ai_bandit_dir":        AI_BANDIT_DIR,
    "regime_db":            REGIME_DB_PATH,
    "bandit_db":            BANDIT_DB_PATH,

    # ========================================================
    # Analysis
    # ========================================================
    "analysis_output":      BASE_DIR / "analysis/output",

    # ========================================================
    # batch
    # ========================================================
    "batch":                BASE_DIR / "trading/batch",
    "batch_logs":           BASE_DIR / "trading/batch/logs",
    "batch_ranking":        BASE_DIR / "trading/batch/ranking",
    "batch_rebuild":        BASE_DIR / "trading/batch/rebuild",

    # ========================================================
    # common
    # ========================================================
    "common":               BASE_DIR / "trading/common",
    "config_snapshot":      BASE_DIR / "trading/common/config_snapshot",

    # ========================================================
    # optional（論理フォルダ）
    # ========================================================
    "optional_data":        BASE_DIR / "trading/optional_data",

    # ========================================================
    # runtime
    # ========================================================
    "runtime":              BASE_DIR / "trading/runtime",
    "runtime_locks":        BASE_DIR / "trading/runtime/locks",
    "runtime_merged":       BASE_DIR / "trading/runtime/merged",
    "runtime_positions":    BASE_DIR / "trading/runtime/positions",
    "runtime_summary":      BASE_DIR / "trading/runtime/summary",
}

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def get_path(role: str) -> Path:

    try:
        p = PATHS[role]

        # ★ ディレクトリなら自動作成
        if isinstance(p, Path) and p.suffix == "":
            p.mkdir(parents=True, exist_ok=True)

        return p

    except KeyError:
        raise KeyError(f"Undefined path role: {role}")

def ensure_dirs():
    """
    PATHS 内のディレクトリのみを自動生成する
    ※ .db などのファイルパスは mkdir しない
    """

    for p in PATHS.values():
        try:
            if isinstance(p, Path) and p.suffix == "":
                p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass