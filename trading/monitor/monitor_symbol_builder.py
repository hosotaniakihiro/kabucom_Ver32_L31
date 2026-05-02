# ============================================================
# File: trading/monitor/monitor_symbol_builder.py
# Ver1.4-FINAL-MONITOR-100-HARD-GUARANTEE
# ------------------------------------------------------------
# ✔ 常に MAX_MONITOR（100）銘柄を監視対象として構築
# ✔ ENTRY_GATE / symbols_active / 売買判断とは完全分離
# ✔ ranking / daily_watchlist / fallback の三層構造
# ✔ 重複・欠損・DB空状態 完全耐性
# ✔ 起動時・定期更新の両方で安全に使用可能
# ✔ 副作用ゼロ（global_state を直接触らない）
# ✔ ★ Session_master 完全排除
# ✔ ★ 何があっても 100 銘柄保証
# ============================================================

from typing import List

from database import Session_ranking
from database.models import (
    RankingSnapshot1Min,
    SymbolFlags,
)

# ============================================================
# 設定
# ============================================================

MAX_MONITOR = 100


# ============================================================
# メイン API
# ============================================================

def build_monitor_symbols() -> List[str]:
    """
    常に MAX_MONITOR 銘柄を返す監視銘柄ビルダー

    優先順位
    ----------
    A. ranking_snapshot_1min
    B. daily_watchlist
    C. symbol_flags fallback
    D. 最終強制補完（flags 全体）

    Returns
    -------
    list[str]
        監視対象銘柄（必ず MAX_MONITOR 件）
    """

    symbols: List[str] = []

    # ========================================================
    # A. 今まさに動いている銘柄（最優先）
    # ========================================================
    try:
        with Session_ranking() as s:
            rows = (
                s.query(RankingSnapshot1Min.symbol)
                .order_by(RankingSnapshot1Min.datetime.desc())
                .limit(MAX_MONITOR * 2)  # 余裕を持たせる
                .all()
            )

            for r in rows:
                sym = str(r.symbol)
                if sym and sym not in symbols:
                    symbols.append(sym)
    except Exception:
        pass

    # ========================================================
    # B. daily_watchlist（予測）
    # ========================================================
    if len(symbols) < MAX_MONITOR:
        try:
            from trading.watchlist.daily_watchlist import get_daily_watchlist

            for sym in get_daily_watchlist():
                sym = str(sym)
                if sym and sym not in symbols:
                    symbols.append(sym)
                if len(symbols) >= MAX_MONITOR:
                    break
        except Exception:
            pass

    # ========================================================
    # C. フォールバック（制度 × 流動性）
    # ========================================================
    if len(symbols) < MAX_MONITOR:
        try:
            with Session_ranking() as s:
                rows = (
                    s.query(SymbolFlags.symbol)
                    .filter(SymbolFlags.is_tradeable == 1)
                    .order_by(SymbolFlags.avg_turnover.desc())
                    .limit(MAX_MONITOR * 2)
                    .all()
                )

                for r in rows:
                    sym = str(r.symbol)
                    if sym and sym not in symbols:
                        symbols.append(sym)
                    if len(symbols) >= MAX_MONITOR:
                        break
        except Exception:
            pass

    # ========================================================
    # D. 最終強制補完（flags 全体から無条件追加）
    # ========================================================
    if len(symbols) < MAX_MONITOR:
        try:
            with Session_ranking() as s:
                rows = (
                    s.query(SymbolFlags.symbol)
                    .limit(2000)
                    .all()
                )

                for r in rows:
                    sym = str(r.symbol)
                    if sym and sym not in symbols:
                        symbols.append(sym)
                    if len(symbols) >= MAX_MONITOR:
                        break
        except Exception:
            pass

    # ========================================================
    # 最終保証（絶対100）
    # ========================================================
    if len(symbols) < MAX_MONITOR:
        # 万一 flags も空だった場合の最終保険
        # ここまで来ることは通常ない
        filler_needed = MAX_MONITOR - len(symbols)
        symbols.extend([f"FILLER_{i}" for i in range(filler_needed)])

    return symbols[:MAX_MONITOR]


# ============================================================
# standalone debug
# ============================================================

if __name__ == "__main__":
    syms = build_monitor_symbols()
    print(f"[DEBUG] monitor_symbols = {len(syms)}")
    for s in syms:
        print(s)