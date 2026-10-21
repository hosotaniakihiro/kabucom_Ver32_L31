from __future__ import annotations
from nas_root import nas_path, nas_root
YAHOO_SUMMARY_INTERVALS = (1, 3, 5)
YAHOO_REFLECT_DELAY_MINUTES = 20
DEFAULT_YAHOO_1MIN_DB_TEMPLATE = nas_path(r"raw_data\yahoo\intraday\yahoo_1min_{yyyymmdd}.db")
__all__ = ["YAHOO_SUMMARY_INTERVALS", "YAHOO_REFLECT_DELAY_MINUTES", "DEFAULT_YAHOO_1MIN_DB_TEMPLATE"]
