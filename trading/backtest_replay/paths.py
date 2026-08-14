# ============================================================
# File   : trading/backtest_replay/paths.py
# Version: Ver02-REPLAY-PATHS
# ------------------------------------------------------------
# リプレイ対象日の各 DB パスを組み立てる。
#
# Ver02:
#   - raw 文字列がバックスラッシュで終わっていて SyntaxError になっていたのを修正。
#     (r'...\summary\' は生文字列でも閉じクォートがエスケープされる)
#     基底ディレクトリを定数化し、rf 文字列で 1 本にまとめた。パスは従来と同一。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass

BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station"


@dataclass
class ReplayPaths:
    trade_date: str

    @property
    def summary_db(self) -> str:
        return rf"{BASE_DIR}\summary\summary{self.trade_date}.db"

    @property
    def ranking_db(self) -> str:
        return rf"{BASE_DIR}\ranking\ranking{self.trade_date}.db"

    @property
    def push_db(self) -> str:
        return rf"{BASE_DIR}\push\push{self.trade_date}.db"

    @property
    def audit_db(self) -> str:
        return rf"{BASE_DIR}\audit\audit_{self.trade_date}.db"

    @property
    def market_audit_db(self) -> str:
        return rf"{BASE_DIR}\audit\market_audit_{self.trade_date}.db"
