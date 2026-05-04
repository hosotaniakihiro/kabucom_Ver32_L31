# ============================================================
# AI/train/ai_threshold_trainer.py
# ------------------------------------------------------------
# ✔ 夜間自動 AI 閾値更新
# ✔ 時間帯別 / 銘柄×時間帯 最適化
# ✔ BLOCK多発銘柄 自動冷却
# ✔ CSV → JSON 出力
# ✔ entry_controller.py と完全連動
# ============================================================

import csv
import json
import os
from collections import defaultdict
from statistics import mean

# ============================================================
# PATH
# ============================================================
LOG_PATH = "AI/logs/ai_pass_log.csv"

OUT_TIMEBAND_TH = "AI/config/timeband_ai_threshold.json"
OUT_SYMBOL_TIME_TH = "AI/config/symbol_timeband_th.json"
OUT_BLOCK_COOLING = "AI/config/block_cooldown_symbols.json"

os.makedirs("AI/config", exist_ok=True)

# ============================================================
# PARAMS（調整可）
# ============================================================
MIN_SAMPLES_TIMEBAND = 30
MIN_SAMPLES_SYMBOL   = 20

BASE_THRESHOLD = 0.70
TH_STEP_UP     = 0.03
TH_STEP_DOWN   = 0.03

BLOCK_RATE_LIMIT = 0.85
BLOCK_MIN_COUNT  = 15

MAX_TH = 0.90
MIN_TH = 0.55

# ============================================================
# LOAD CSV
# ============================================================
rows = []
with open(LOG_PATH, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for r in reader:
        if r["stage"] != "final_ai":
            continue
        rows.append(r)

if not rows:
    print("❌ ai_pass_log.csv が空です")
    exit(0)

# ============================================================
# AGGREGATION
# ============================================================
timeband_stats = defaultdict(list)
symbol_timeband_stats = defaultdict(list)
symbol_block_counter = defaultdict(lambda: {"block": 0, "total": 0})

for r in rows:
    hour = r["datetime"][11:13]   # HH
    symbol = r["symbol"]
    passed = r["result"] == "PASS"
    confidence = float(r.get("confidence", 0.0))

    timeband_stats[hour].append((passed, confidence))
    symbol_timeband_stats[(symbol, hour)].append((passed, confidence))

    symbol_block_counter[symbol]["total"] += 1
    if not passed:
        symbol_block_counter[symbol]["block"] += 1

# ============================================================
# ① TIMEBAND THRESHOLD
# ============================================================
timeband_th = {}

for hour, items in timeband_stats.items():
    if len(items) < MIN_SAMPLES_TIMEBAND:
        continue

    pass_rate = sum(p for p, _ in items) / len(items)
    avg_conf  = mean(c for _, c in items)

    th = BASE_THRESHOLD

    if pass_rate > 0.65:
        th += TH_STEP_UP
    elif pass_rate < 0.45:
        th -= TH_STEP_DOWN

    th = min(MAX_TH, max(MIN_TH, th))
    timeband_th[hour] = round(th, 3)

# ============================================================
# ② SYMBOL × TIMEBAND THRESHOLD
# ============================================================
symbol_time_th = defaultdict(dict)

for (symbol, hour), items in symbol_timeband_stats.items():
    if len(items) < MIN_SAMPLES_SYMBOL:
        continue

    pass_rate = sum(p for p, _ in items) / len(items)
    avg_conf  = mean(c for _, c in items)

    th = BASE_THRESHOLD

    if pass_rate > 0.70:
        th += TH_STEP_UP
    elif pass_rate < 0.40:
        th -= TH_STEP_DOWN

    th = min(MAX_TH, max(MIN_TH, th))
    symbol_time_th[symbol][hour] = round(th, 3)

# ============================================================
# ③ BLOCK COOLING SYMBOLS
# ============================================================
block_symbols = []

for symbol, d in symbol_block_counter.items():
    if d["total"] < BLOCK_MIN_COUNT:
        continue

    block_rate = d["block"] / d["total"]
    if block_rate >= BLOCK_RATE_LIMIT:
        block_symbols.append(symbol)

# ============================================================
# SAVE JSON
# ============================================================
with open(OUT_TIMEBAND_TH, "w", encoding="utf-8") as f:
    json.dump(timeband_th, f, indent=2, ensure_ascii=False)

with open(OUT_SYMBOL_TIME_TH, "w", encoding="utf-8") as f:
    json.dump(symbol_time_th, f, indent=2, ensure_ascii=False)

with open(OUT_BLOCK_COOLING, "w", encoding="utf-8") as f:
    json.dump(block_symbols, f, indent=2, ensure_ascii=False)

# ============================================================
# REPORT
# ============================================================
print("✅ AI Threshold Training COMPLETE")
print(f"  ・Timeband thresholds : {len(timeband_th)}")
print(f"  ・Symbol×Timeband     : {len(symbol_time_th)}")
print(f"  ・Cooling symbols     : {len(block_symbols)}")
