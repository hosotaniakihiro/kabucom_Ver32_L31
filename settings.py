import configparser
import logging
import os

# === ログ設定 ===
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

# === 設定ファイル読み込み ===
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")

# === グローバル変数の定義 ===
Token = conf.get("aukabu", "token", fallback=None)
APIPassword = conf.get("aukabu", "apipassword", fallback=None)
WEBHOOK_URL = conf.get("Discord", "webhook_url", fallback=None)

# --- スコア判定のしきい値 ---
THRESHOLD = int(conf.get("trade", "threshold", fallback="5"))
SELL_THRESHOLD = int(conf.get("trade", "sell_threshold", fallback="-5"))
# --- スコア判定のしきい値 ---
BUY_THRESHOLD = float(conf.get("trade", "threshold", fallback="5"))  # ← THRESHOLD と同じ値を流用
SELL_THRESHOLD = float(conf.get("trade", "sell_threshold", fallback="-5"))

# --- 売買設定 ---
ENTRY_BUDGET = float(conf.get("strategy", "entry_budget", fallback="500000"))
UNIT_SIZE = float(conf.get("strategy", "unit_size", fallback="100"))

# --- ファイルパス ---
PKL_SYMBOLS_FILE = "y:/kabu/symbols.pkl"  # 実際のpickleファイルのパス
BASE_PATH = os.path.dirname(os.path.abspath(__file__))

# --- ランキング由来エントリー設定 ---
RANKING_COOL_TIME_MINUTES = int(conf.get("ranking", "cool_time_minutes", fallback="30"))
