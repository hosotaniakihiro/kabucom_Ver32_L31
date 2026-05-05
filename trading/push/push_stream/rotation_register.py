# ============================================================
# File   : trading/push/push_stream/rotation_register.py
# Version: PRODUCTION-STABLE-REV1-PUSH-ROTATION-REGISTER-FACADE
# ------------------------------------------------------------
# PUSH A/Bローテーション用の登録委譲APIを分離するための窓口。
#
# Notes:
#   - 現段階では既存 rotation.py の実装へ委譲する互換facade。
#   - 次段階で register_symbols / timeout登録処理をこのファイルへ移し、
#     rotation.py を薄くする。
#   - 旧 import path は rotation.py 側に残す。
# ============================================================

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .rotation_settings import REGISTER_TIMEOUT_SEC

VERSION = "PRODUCTION-STABLE-REV1-PUSH-ROTATION-REGISTER-FACADE"


def _rotation_module():
    from . import rotation
    return rotation


def register_symbols(symbols: Iterable[str], force: bool = False, **kwargs: Any) -> bool:
    return bool(_rotation_module().register_symbols(symbols, force=force, **kwargs))


def run_one_batch(*, label: str, symbols: Sequence[str]) -> bool:
    return bool(_rotation_module()._run_one_batch(label=label, symbols=symbols))


def run_one_batch_with_timeout(
    *,
    label: str,
    symbols: Sequence[str],
    timeout_sec: float = REGISTER_TIMEOUT_SEC,
) -> bool:
    return bool(
        _rotation_module()._run_one_batch_with_timeout(
            label=label,
            symbols=symbols,
            timeout_sec=timeout_sec,
        )
    )


__all__ = [
    "VERSION",
    "REGISTER_TIMEOUT_SEC",
    "register_symbols",
    "run_one_batch",
    "run_one_batch_with_timeout",
]
