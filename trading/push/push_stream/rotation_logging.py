# ============================================================
# File   : trading/push/push_stream/rotation_logging.py
# Version: PRODUCTION-STABLE-REV1-PUSH-ROTATION-LOGGING-FACADE
# ------------------------------------------------------------
# PUSH A/Bローテーション用ログAPIを分離するための窓口。
#
# Notes:
#   - 現段階では既存 rotation.py の実装へ委譲する互換facade。
#   - 次段階で候補ログ・登録対象ログをこのファイルへ移し、
#     rotation.py を薄くする。
# ============================================================

from __future__ import annotations

from typing import Sequence

VERSION = "PRODUCTION-STABLE-REV1-PUSH-ROTATION-LOGGING-FACADE"


def _rotation_module():
    from . import rotation
    return rotation


def log_register_targets_with_names(
    symbols: Sequence[str],
    *,
    label: str,
    reason: str,
) -> None:
    _rotation_module()._log_register_targets_with_names(
        symbols,
        label=label,
        reason=reason,
    )


def log_candidate_result(
    *,
    source: str,
    raw_count: int,
    real: Sequence[str],
    filler_count: int,
    invalid_count: int,
) -> None:
    _rotation_module()._log_candidate_result(
        source=source,
        raw_count=raw_count,
        real=real,
        filler_count=filler_count,
        invalid_count=invalid_count,
    )


__all__ = [
    "VERSION",
    "log_register_targets_with_names",
    "log_candidate_result",
]
