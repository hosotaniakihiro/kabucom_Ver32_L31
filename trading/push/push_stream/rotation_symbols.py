# ============================================================
# File   : trading/push/push_stream/rotation_symbols.py
# Version: PRODUCTION-STABLE-REV1-PUSH-ROTATION-SYMBOLS-FACADE
# ------------------------------------------------------------
# PUSH A/Bローテーション用の銘柄解決・正規化APIを分離するための窓口。
#
# Notes:
#   - 現段階では既存 rotation.py の実装へ委譲する互換facade。
#   - 次段階で中身をこのファイルへ移し、rotation.py を薄くする。
#   - 外部モジュールは今後こちらを import する。
# ============================================================

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple

VERSION = "PRODUCTION-STABLE-REV1-PUSH-ROTATION-SYMBOLS-FACADE"


def _rotation_module():
    from . import rotation
    return rotation


def is_filler_symbol(symbol: Any) -> bool:
    return bool(_rotation_module()._is_filler_symbol(symbol))


def is_real_symbol(symbol: Any) -> bool:
    return bool(_rotation_module()._is_real_symbol(symbol))


def normalize_real_symbol(symbol: Any) -> Optional[str]:
    return _rotation_module()._normalize_real_symbol(symbol)


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    return list(_rotation_module()._dedupe_keep_order(items))


def clean_symbol_list(src: Any) -> Tuple[List[str], int, int, int]:
    return _rotation_module()._clean_symbol_list(src)


def resolve_monitor_symbols() -> List[str]:
    return list(_rotation_module()._resolve_monitor_symbols())


def apply_register_liquidity_guard(targets: Sequence[str]) -> List[str]:
    return list(_rotation_module()._apply_register_liquidity_guard(targets))


def resolve_register_targets() -> List[str]:
    return list(_rotation_module()._resolve_register_targets())


__all__ = [
    "VERSION",
    "is_filler_symbol",
    "is_real_symbol",
    "normalize_real_symbol",
    "dedupe_keep_order",
    "clean_symbol_list",
    "resolve_monitor_symbols",
    "apply_register_liquidity_guard",
    "resolve_register_targets",
]
