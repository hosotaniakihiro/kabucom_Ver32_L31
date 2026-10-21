# 戦略を import して REGISTRY に登録する。
from backtest16.strategies import base  # noqa: F401
from backtest16.strategies import longs  # noqa: F401
from backtest16.strategies import shorts  # noqa: F401
from backtest16.strategies.base import REGISTRY  # noqa: F401


def all_strategies():
    return dict(REGISTRY)
