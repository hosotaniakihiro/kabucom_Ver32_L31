# ============================================================
# backtest16/config.py
# ------------------------------------------------------------
# 買い6戦略＋空売り10戦略 比較バックテストの設定（単一の真実源）。
#
# 絶対条件（本ファイルは定数のみ。実発注・本番変更は一切しない）:
#   - 既存SQLite DBは読み取り専用。immutable=1 は「ライブ書込みの無い過去日」専用。
#   - 当日(基準日)ファイルはライブ書込み中のため既定で対象外。
#   - 未来情報を使わない / 推測値で埋めない。
# ============================================================

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

# ------------------------------------------------------------
# 基準日・検証期間（§2）
# ------------------------------------------------------------
ASOF_DATE = dt.date(2026, 10, 20)             # 基準日
PERIOD_START = dt.date(2026, 4, 20)           # 検証期間 開始
PERIOD_END = dt.date(2026, 10, 20)            # 検証期間 終了
# 当日(基準日)および未来日はライブ書込み中・未確定のため既定で除外する。
EXCLUDE_DATES_FROM = ASOF_DATE                # この日以降(当日含む)は既定で読まない

# 時系列3分割（§11）。最終評価期間を見た後にパラメータを変えないための固定分割。
# 前半3か月=開発/学習, 中間1.5か月=検証, 後半1.5か月=最終評価。
SPLIT_TRAIN_END = dt.date(2026, 7, 20)        # 前半3か月(学習) の終わり
SPLIT_VALID_END = dt.date(2026, 9, 5)         # 中間1.5か月(検証) の終わり
# それ以降 PERIOD_END までが 最終評価(最後まで触らない)

# ------------------------------------------------------------
# データ所在（§3）。読み取り専用。
# ------------------------------------------------------------
DATA_ROOT = os.getenv("BT16_DATA_ROOT", r"X:/raw_data/kabu_station")
PUSH_DIR = os.path.join(DATA_ROOT, "push")        # pushYYYYMMDD.db (stream_data)
SUMMARY_DIR = os.path.join(DATA_ROOT, "summary")  # summaryYYYYMMDD.db (stock_summary_{1,3,5}min)
RANKING_DIR = os.path.join(DATA_ROOT, "ranking")  # rankingYYYYMMDD.db

# 監視ユニバースの履歴（当時の購読銘柄）。現在のリストを過去へ無条件適用しない(§3)。
SUBSCRIPTION_HISTORY_DB = os.path.join(RANKING_DIR, "push_subscription_symbols_history.db")

# SQLite 読み取り方針:
#   過去日(ライブ書込み無し)は immutable=1 で高速・安全に開ける(実測0.0-0.2s)。
#   当日/書込み中ファイルには immutable を使わず mode=ro のみ、既定では触らない。
SQLITE_RO_IMMUTABLE = "file:{path}?mode=ro&immutable=1"
SQLITE_RO = "file:{path}?mode=ro"

# ------------------------------------------------------------
# 取引基本設定（§2）
# ------------------------------------------------------------
TAKE_PROFIT_PCT = 0.30      # 利確: 建値から有利方向 0.30%
STOP_LOSS_PCT = 0.30        # 損切り: 建値から不利方向 0.30%
MAX_HOLD_SECONDS = 300      # 最大保有 300秒
PER_SYMBOL_NOTIONAL_YEN = 700_000   # 1銘柄建玉上限 70万円
MAX_CONCURRENT_POSITIONS = 3        # 最大同時建玉(買い+売り 合計)
ALLOW_OVERNIGHT = False     # 持ち越し禁止
# 基本対象時間 9:05〜11:25（前場内。寄り直後5分とザラ場終盤を避ける）
SESSION_ENTRY_START = dt.time(9, 5, 0)
SESSION_ENTRY_END = dt.time(11, 25, 0)
FORCE_EXIT_BY = dt.time(11, 29, 0)   # 前場引け手前で強制手仕舞い（持ち越し禁止）

# 同一銘柄で買いと空売りの同時建玉は標準で禁止（§9D）
ALLOW_SIMULTANEOUS_LONG_SHORT_SAME_SYMBOL = False

# 同時シグナルの優先順位（§9D: 事前固定）。小さいほど優先。
# データ品質の高い/実行可能性の確実な戦略を優先する。
SIGNAL_PRIORITY = [
    "SHORT_01", "LONG_01",            # GU起点(最優先検証)
    "SHORT_05", "SHORT_02", "SHORT_04",
    "LONG_03", "LONG_04", "LONG_05", "LONG_02", "LONG_06",
    "SHORT_06", "SHORT_07", "SHORT_03", "SHORT_09", "SHORT_08", "SHORT_10",
]

# ------------------------------------------------------------
# 戦略共通パラメータ（§4/§5）
# ------------------------------------------------------------
GAP_UP_PCT = 3.0            # GU基準 +3%
GAP_DOWN_PCT = -3.0         # GD基準 -3%
VWAP_NEAR_PCT = 0.15        # VWAP接近 ±0.15%
VWAP_FAIL_PCT = 0.10        # VWAP押し目の失敗閾(0.10%超の下抜け 等)
RETEST_NEAR_PCT = 0.15      # リテスト ±0.15%
RETEST_FAIL_PCT = 0.20      # リテスト失敗 0.20%超
VOLUME_SPIKE_MULT = 3.0     # 出来高倍率(ベースライン3倍)
VOLUME_LOOKBACK_BARS = 5    # 直前確定足5本(評価足は含めない)
MA5_NEAR_PCT = 0.15         # MA5接近 ±0.15%
RANGE15_END = dt.time(9, 15, 0)   # 9:00-9:15 レンジ
FIRST_BAR_START = dt.time(9, 0, 0)
FIRST_BAR_LOCK = dt.time(9, 5, 0) # 最初の5分足の確定時刻

# ------------------------------------------------------------
# 売買コスト（§2/§6.3）。実口座条件が不明なものは「仮定」と明示。
#   kabu.com 信用取引(デイトレ)を想定した保守的な仮定値。実測が得られれば置換する。
# ------------------------------------------------------------
COST_ASSUMPTIONS = {
    "source": "ASSUMED",   # ASSUMED=仮定 / ACTUAL=実口座条件
    # 売買委託手数料(信用デイトレは無料プランが多いが保守的に片道bpsを置く)
    "commission_bps_per_side": 0.0,      # 片道 手数料率(bps)。0=無料プラン想定
    "commission_min_yen": 0.0,
    # スプレッド(往復で負担。板から実測できる場合はモデルで上書き)
    "assumed_spread_bps": 5.0,           # 片道 2.5bps 相当の仮定(=往復5bps)
    # スリッページ(成行/ブレイクの滑り。片道bps)
    "slippage_bps_per_side": 3.0,
    # 貸株料(空売り。年率%)。5分保有でも0と仮定しない(§6.3)→1日分を日割り計上。
    "stock_lending_fee_annual_pct": 1.15,   # 制度信用 貸株料(仮定, 年率%)
    # 逆日歩(品貸料)。銘柄・日次で変動し事前確定不可 → 既定0だが「不明」フラグ。
    "negative_rate_yen_per_share_per_day": 0.0,
    "negative_rate_known": False,
    # 一般信用の売建プレミアム料(年率%)。一般信用を使う場合のみ。
    "general_short_premium_annual_pct": 3.9,  # 仮定
    # 信用金利(買い建ての買方金利。年率%)。日計りは1日分。
    "margin_buy_interest_annual_pct": 2.8,    # 仮定
    # 1年の課金日数
    "annual_days": 365,
}

# ------------------------------------------------------------
# パラメータ感度分析の候補（§11）。標準条件と分離して扱う。
# ------------------------------------------------------------
PARAM_GRID = {
    "take_profit_pct": [0.2, 0.3, 0.5],
    "stop_loss_pct": [0.2, 0.3, 0.5],
    "max_hold_seconds": [180, 300, 600],
    "gap_up_pct": [2.0, 3.0, 4.0, 5.0],
    "gap_down_pct": [-2.0, -3.0, -4.0, -5.0],
    "vwap_near_pct": [0.10, 0.15, 0.20],
    "volume_spike_mult": [2.0, 3.0, 5.0],
}

# 約定モード（§8）。A/B/C を混ぜて単一勝率にしない。
EXECUTION_MODES = ("A_BOARD", "B_PUSH", "C_OHLC")

# 信用売建可否の区分（§6.1）
BORROW_CLASS = {
    "A": "当時の売建可能性と数量を確認済み(実行可能性確認済みに算入)",
    "B": "銘柄/信用区分として適格だが当時の在庫・発注可否は不明(仮想)",
    "C": "売建不可が確認済み(売買不可として記録)",
    "D": "適格性自体が不明(仮想)",
}

# 出力先（すべてローカル。外部アップロード不要 §12）
REPORTS_DIR = str(Path(__file__).resolve().parent / "reports")
CHECKPOINT_DIR = str(Path(__file__).resolve().parent / "data" / "checkpoints")


def trading_cost_note() -> str:
    src = COST_ASSUMPTIONS["source"]
    return f"売買コストは {src}（{'実口座条件' if src=='ACTUAL' else '明示的な仮定値'}）。逆日歩は銘柄・日次変動のため既定で不明扱い。"
