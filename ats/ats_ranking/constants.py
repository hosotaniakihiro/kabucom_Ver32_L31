# ============================================================
# File   : ats/ats_ranking/constants.py
# Version: Ver1.0-ATS-RANKING-CONSTANTS
# ============================================================

RANKING_DB_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking"

PRIMARY_TABLES = [
    "ranking_snapshot_1min",
    "ranking_raw_1min",
    "ranking",
]

CATEGORY_TABLE_SPECS = [
    ("値上がり率_ALL", "", "値上がり率"),
    ("値上がり率_TP", "プライム", "値上がり率"),
    ("値上がり率_TS", "スタンダード", "値上がり率"),
    ("値上がり率_TG", "グロース", "値上がり率"),
    ("値下がり率_ALL", "", "値下がり率"),
    ("値下がり率_TP", "プライム", "値下がり率"),
    ("値下がり率_TS", "スタンダード", "値下がり率"),
    ("値下がり率_TG", "グロース", "値下がり率"),
    ("売買代金_ALL", "", "売買代金"),
    ("売買代金_TP", "プライム", "売買代金"),
    ("売買代金_TS", "スタンダード", "売買代金"),
    ("売買代金_TG", "グロース", "売買代金"),
    ("売買代金急増_ALL", "", "売買代金急増"),
    ("売買代金急増_TP", "プライム", "売買代金急増"),
    ("売買代金急増_TS", "スタンダード", "売買代金急増"),
    ("売買代金急増_TG", "グロース", "売買代金急増"),
    ("売買高上位_ALL", "", "売買高上位"),
    ("売買高上位_TP", "プライム", "売買高上位"),
    ("売買高上位_TS", "スタンダード", "売買高上位"),
    ("売買高上位_TG", "グロース", "売買高上位"),
    ("売買高急増_ALL", "", "売買高急増"),
    ("売買高急増_TP", "プライム", "売買高急増"),
    ("売買高急増_TS", "スタンダード", "売買高急増"),
    ("売買高急増_TG", "グロース", "売買高急増"),
    ("TICK回数_ALL", "", "TICK回数"),
    ("TICK回数_TP", "プライム", "TICK回数"),
    ("TICK回数_TS", "スタンダード", "TICK回数"),
    ("TICK回数_TG", "グロース", "TICK回数"),
]

MARKET_CODE_TO_LABEL = {
    "TP": "プライム",
    "TS": "スタンダード",
    "TG": "グロース",
    "ALL": "",
    "P": "プライム",
    "S": "スタンダード",
    "G": "グロース",
    "PRIME": "プライム",
    "STANDARD": "スタンダード",
    "GROWTH": "グロース",
}

TOP_N_INFLOW = 60
TOP_N_GAINERS = 40
TOP_N_LOSERS = 40
TOP_N_TURNOVER = 40
TOP_N_VOLUME_SPIKE = 40

TOP_N_MARKET_GAINERS = 25
TOP_N_MARKET_LOSERS = 25
TOP_N_MARKET_VOLUME = 25

CROSS_BASE_N = 50
CROSS_OUT_N = 20

MIN_ABS_TRADING_VOLUME = 100.0
MIN_VOLUME_SPEED = 0.5
MIN_PRICE = 100
MAX_PRICE = 10000