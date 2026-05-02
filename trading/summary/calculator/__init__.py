from .summary_pipeline import calculate_summary


# ------------------------------------------------------------
# backward compatibility（超重要）
# ------------------------------------------------------------

def iter_trade_dates(start_date=None, end_date=None):
    """
    旧API互換（簡易版）

    NOTE:
    ranking側の依存を壊さないための暫定実装
    """

    import pandas as pd

    if start_date is None or end_date is None:
        return []

    try:
        return pd.date_range(start_date, end_date, freq="B").tolist()
    except Exception:
        return []