# ============================================================
# File   : trading/ranking/ranking_snapshot_manager.py
# Version: Ver2.1-PRODUCTION-FULL-COMPAT-SNAPSHOT-MANAGER
# ------------------------------------------------------------
# ✔ ranking 生データの in-memory 管理（短期）
# ✔ ranking_snapshot_1min 用 row を生成
# ✔ MA / 加速 / AI 用の共通土台
# ✔ 既存仕様100%保持（後方互換）
# ✔ 値上がり率 ENTRY 解禁用フィールド対応
# ✔ entry 判断は一切しない（材料提供のみ）
# ✔ snapshot_time を datetime 型で統一
# ✔ rank_type / market 正規化
# ✔ symbol 正規化強化（strip / .0除去）
# ✔ 数値変換安全化
# ✔ 表示側互換 alias 追加（price / vol / vspd / rank）
# ✔ None / NaN / 空文字に強い production hardened
# ============================================================

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional
import math


class RankingSnapshotManager:
    """
    symbol 単位で ranking スナップショットを保持するマネージャ

    - deque は短期履歴（MA / 加速 / AI 用）
    - DB 保存は ranking_snapshot_1min 用の row を返す
    - ENTRY 可否判断は行わない（判断は entry 側の責務）
    - 後段表示系との互換のため alias 列も同時に返す
    """

    # ========================================================
    # 初期化
    # ========================================================
    def __init__(self, maxlen: int = 5):
        """
        Parameters
        ----------
        maxlen : int
            symbol ごとに保持するスナップショット数
        """
        self.maxlen = int(maxlen) if maxlen is not None else 5
        if self.maxlen <= 0:
            self.maxlen = 5
        self.snapshots: Dict[str, Deque[Dict[str, Any]]] = {}

    # ========================================================
    # 内部ユーティリティ
    # ========================================================
    @staticmethod
    def _normalize_text(value: Optional[Any]) -> Optional[str]:
        """
        None / 空文字 / 'nan' 等を正規化
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None

        lowered = text.lower()
        if lowered in {"none", "nan", "null", "nat"}:
            return None

        return text

    @staticmethod
    def _normalize_symbol(value: Any) -> str:
        """
        symbol を join しやすい文字列へ正規化
        """
        if value is None:
            return ""
        text = str(value).strip()
        if text.endswith(".0"):
            text = text[:-2]
        return text

    @staticmethod
    def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        """
        安全な float 変換
        """
        if value is None:
            return default

        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return default
            if value.lower() in {"none", "nan", "null", "nat"}:
                return default
            value = value.replace(",", "")

        try:
            v = float(value)
            if math.isnan(v) or math.isinf(v):
                return default
            return v
        except Exception:
            return default

    @staticmethod
    def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
        """
        安全な int 変換
        """
        fv = RankingSnapshotManager._safe_float(value, None)
        if fv is None:
            return default
        try:
            return int(fv)
        except Exception:
            return default

    @staticmethod
    def _coalesce(*values: Any, default: Any = None) -> Any:
        """
        最初に使える値を返す
        """
        for v in values:
            if v is None:
                continue
            try:
                if isinstance(v, float) and math.isnan(v):
                    continue
            except Exception:
                pass
            if isinstance(v, str) and not v.strip():
                continue
            return v
        return default

    # ========================================================
    # ADD
    # ========================================================
    def add(
        self,
        *,
        symbol: str,
        symbolname: Optional[str],
        rank_type: Optional[str],
        market: Optional[str],
        price: Optional[float],
        volume: Optional[float],
        volume_speed: Optional[float],
        # ---- 値上がり率系（任意 / NEW）----
        change_rate: Optional[float] = None,   # 例: +5.2 (%)
        prev_price: Optional[float] = None,    # 前日終値など
        # ---- 互換用追加引数（任意）----
        rank: Optional[int] = None,
        source: str = "RANKING",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        ranking データを追加し、DB 保存用 row を返す

        Notes
        -----
        - ENTRY 可否は判断しない
        - あくまで「後段が判断できる材料」を生成する
        - 既存カラムは保持したまま、表示互換 alias も返す
        """

        # ----------------------------------------------------
        # 時刻
        # ----------------------------------------------------
        now = now or datetime.now()

        # ----------------------------------------------------
        # 正規化
        # ----------------------------------------------------
        symbol = self._normalize_symbol(symbol)
        symbolname = self._normalize_text(symbolname)
        rank_type = self._normalize_text(rank_type)
        market = self._normalize_text(market)
        source = self._normalize_text(source) or "RANKING"

        price_f = self._safe_float(price, None)
        volume_f = self._safe_float(volume, None)
        volume_speed_f = self._safe_float(volume_speed, 0.0)
        change_rate_f = self._safe_float(change_rate, None)
        prev_price_f = self._safe_float(prev_price, None)
        rank_i = self._safe_int(rank, None)

        # ----------------------------------------------------
        # deque 初期化
        # ----------------------------------------------------
        if symbol not in self.snapshots:
            self.snapshots[symbol] = deque(maxlen=self.maxlen)

        # ----------------------------------------------------
        # in-memory snapshot（短期履歴）
        # ----------------------------------------------------
        snap: Dict[str, Any] = {
            "timestamp": now,
            "price": price_f,
            "volume": volume_f,
            "volume_speed": volume_speed_f if volume_speed_f is not None else 0.0,
            # 値上がり率関連
            "change_rate": change_rate_f,
            "prev_price": prev_price_f,
            # 補助
            "rank_type": rank_type,
            "market": market,
            "symbolname": symbolname,
            "rank": rank_i,
            "source": source,
        }

        self.snapshots[symbol].append(snap)

        # ----------------------------------------------------
        # ENTRY 候補フラグ（※判断はしない）
        # ----------------------------------------------------
        entry_candidate = 0

        if rank_type == "値上がり率":
            # 値上がり率が正なら「候補フラグ」を立てるだけ
            if snap["change_rate"] is not None and snap["change_rate"] > 0:
                entry_candidate = 1
        else:
            # その他ランキングは従来どおり候補扱い
            if rank_type is not None:
                entry_candidate = 1

        # ----------------------------------------------------
        # DB 用 row（ranking_snapshot_1min）
        # ※ 既存カラムは一切削らない
        # ----------------------------------------------------
        row: Dict[str, Any] = {
            # ---- 基本情報 ----
            "symbol": symbol,
            "symbolname": symbolname,              # NULL 許容
            "rank_type": rank_type,                # None 正規化済
            "market": market,                      # None 正規化済
            "current_price": snap["price"],
            "trading_volume": snap["volume"],
            "volume_speed": snap["volume_speed"],
            "snapshot_time": now,                  # datetime 型
            "source": source,

            # ---- 拡張フィールド（後方互換）----
            "change_rate": snap["change_rate"],
            "prev_price": snap["prev_price"],
            "entry_candidate": int(entry_candidate),   # 0 / 1（SQL 安全）

            # ---- 互換拡張 ----
            "rank": rank_i,

            # ---- 表示側互換 alias ----
            # 後段が price / vol / vspd を期待しても落ちないようにする
            "price": snap["price"],
            "vol": snap["volume"],
            "vspd": snap["volume_speed"],
        }

        return row

    # ========================================================
    # GET
    # ========================================================
    def get(self, symbol: str) -> List[Dict[str, Any]]:
        """
        symbol の短期スナップショットを返す（MA / 加速判定用）
        """
        symbol = self._normalize_symbol(symbol)
        return list(self.snapshots.get(symbol, []))

    # ========================================================
    # CHECK
    # ========================================================
    def has_enough(self, symbol: str, n: int = 3) -> bool:
        """
        MA / 加速判定に十分な本数があるか
        """
        symbol = self._normalize_symbol(symbol)
        try:
            n = int(n)
        except Exception:
            n = 3
        return len(self.snapshots.get(symbol, [])) >= n

    # ========================================================
    # LATEST
    # ========================================================
    def get_latest(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        symbol の最新 snapshot を返す
        """
        symbol = self._normalize_symbol(symbol)
        dq = self.snapshots.get(symbol)
        if not dq:
            return None
        return dq[-1]

    # ========================================================
    # EXPORT
    # ========================================================
    def export_latest_row(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        最新 snapshot から表示互換 row を組み立てる
        DB が未更新でも後段が参照しやすいようにする
        """
        symbol = self._normalize_symbol(symbol)
        latest = self.get_latest(symbol)
        if not latest:
            return None

        return {
            "symbol": symbol,
            "symbolname": latest.get("symbolname"),
            "rank_type": latest.get("rank_type"),
            "market": latest.get("market"),
            "current_price": latest.get("price"),
            "trading_volume": latest.get("volume"),
            "volume_speed": latest.get("volume_speed"),
            "price": latest.get("price"),
            "vol": latest.get("volume"),
            "vspd": latest.get("volume_speed"),
            "rank": latest.get("rank"),
            "change_rate": latest.get("change_rate"),
            "prev_price": latest.get("prev_price"),
            "source": latest.get("source"),
            "snapshot_time": latest.get("timestamp"),
        }

    # ========================================================
    # CLEAR
    # ========================================================
    def clear_symbol(self, symbol: str):
        """
        symbol の履歴をクリア（ローテーション時など）
        """
        symbol = self._normalize_symbol(symbol)
        self.snapshots.pop(symbol, None)

    def clear_all(self):
        """
        全 symbol の履歴をクリア
        """
        self.snapshots.clear()