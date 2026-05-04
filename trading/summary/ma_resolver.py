# trading/summary/ma_resolver.py
# ============================================================
# MA Resolver
# ------------------------------------------------------------
# ✔ PUSH / ranking / Yahoo MA の優先順位解決
# ✔ ENTRY / EXIT では使用禁止
# ✔ 表示・summary 補完専用
# ============================================================

from database.crud_ranking import get_latest_ranking_ma
from database.crud_yahoo import get_latest_yahoo_ma
from global_state import global_data


def get_effective_ma(symbol: str):
    """
    MA の最終解決
    優先順位:
      1. PUSH
      2. ranking MA
      3. Yahoo MA
    """

    # ① PUSH（実足）
    push_row = global_data.get_latest_summary("1M", symbol)
    if push_row is not None and not push_row.get("is_filled"):
        return {
            "ma5": push_row.get("ma5"),
            "ma25": push_row.get("ma25"),
            "ma75": push_row.get("ma75"),
            "source": "push",
        }

    # ② ranking MA
    r = get_latest_ranking_ma(symbol)
    if r and r.is_valid:
        return {
            "ma5": r.ma5,
            "ma25": r.ma25,
            "ma75": r.ma75,
            "source": "ranking",
        }

    # ③ Yahoo MA
    y = get_latest_yahoo_ma(symbol)
    if y and y.is_valid:
        return {
            "ma5": y.ma5,
            "ma25": y.ma25,
            "ma75": y.ma75,
            "source": "yahoo",
        }

    return None
# ============================================================
# trading/summary/ma_resolver.py
# ------------------------------------------------------------
# ✔ 表示・監視専用 MA 解決
# ✔ ENTRY / EXIT 使用禁止
# ============================================================

def get_effective_ma_row(
    df_ma,
    symbol: str,
):
    """
    最新行を返す
    """
    if df_ma is None or df_ma.empty:
        return None

    r = df_ma[df_ma["symbol"] == symbol].tail(1)
    if r.empty:
        return None

    return r.iloc[0].to_dict()
