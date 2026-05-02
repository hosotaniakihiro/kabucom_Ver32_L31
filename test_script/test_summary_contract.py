import pandas as pd

from trading.summary.initial_summary_rebuild import (
    run_initial_summary_rebuild,
)
from trading.summary.summary_incremental_rebuild import (
    run_incremental_summary_rebuild,
)


def _assert_summary_dict(summary_dict: dict):
    assert isinstance(summary_dict, dict)

    for k, v in summary_dict.items():
        assert isinstance(k, str)
        assert k in ("1min", "3min", "5min")
        assert isinstance(
            v, pd.DataFrame
        ), f"{k} is not DataFrame: {type(v)}"


def test_initial_summary_contract():
    summary = run_initial_summary_rebuild()
    _assert_summary_dict(summary)


def test_incremental_summary_contract():
    summary = run_incremental_summary_rebuild()
    if summary is not None:
        _assert_summary_dict(summary)
