"""Compatibility shim for legacy imports.

Use trading.scoring.config.score_table as the source of truth.
"""

from trading.scoring.config.score_table import *  # noqa: F401,F403
