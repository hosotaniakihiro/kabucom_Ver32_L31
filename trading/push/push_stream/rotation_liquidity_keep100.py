# ============================================================
# File   : trading/push/push_stream/rotation_liquidity_keep100.py
# Version: V1.0-ROTATION-LIQUIDITY-KEEP100
# ------------------------------------------------------------
# PUSH rotation 用の流動性ガード補正。
# 100銘柄候補を作れているのに liquidity guard が50銘柄へ削る場合、
# ローテーション用途では100銘柄を維持して A=50 / B=50 を保つ。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)
VERSION = "V1.0-ROTATION-LIQUIDITY-KEEP100"
_INSTALLED = False


def _i(name: str, default: int) -> int:
    try:
        return int(float(str(os.environ.get(name, str(default))).replace(",", "")))
    except Exception:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _d(xs: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in xs or []:
        s = str(x).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def apply_keep100_guard(targets: Sequence[str], orig) -> list[str]:
    from . import rotation_symbols

    if not _b("PUSH_ROTATION_LIQ_KEEP100_PATCH", True):
        return orig(targets)

    try:
        cleaned, _, _, _ = rotation_symbols.clean_symbol_list(targets)
    except Exception:
        cleaned = _d(targets)

    filtered = orig(targets)
    try:
        fc, _, _, _ = rotation_symbols.clean_symbol_list(filtered)
    except Exception:
        fc = _d(filtered)

    # 既定は100維持。必要なら環境変数で50などへ戻せる。
    target_keep = max(
        1,
        _i(
            "PUSH_ROTATION_LIQ_GUARD_ROTATION_MIN_KEEP",
            _i("PUSH_ROTATION_TARGET_MIN_KEEP", 100),
        ),
    )
    collapse_ratio = max(0.0, min(1.0, float(os.environ.get("PUSH_ROTATION_LIQ_COLLAPSE_RATIO", "0.80"))))
    collapse_threshold = max(1, int(len(cleaned) * collapse_ratio))

    # 例: before=100 after=50 はローテーション崩壊としてfail-open。
    if len(cleaned) >= target_keep and len(fc) < target_keep:
        logger.warning(
            "[PUSH ROTATION LIQ KEEP100] fail-open keep original before=%d after=%d target_keep=%d version=%s head=%s",
            len(cleaned),
            len(fc),
            target_keep,
            VERSION,
            cleaned[:20],
        )
        return cleaned[:max(len(cleaned), target_keep)]

    # 100未満でも大きく削れた場合は保険で戻す。
    if cleaned and len(cleaned) >= 50 and len(fc) < collapse_threshold:
        logger.warning(
            "[PUSH ROTATION LIQ KEEP100] collapse fail-open before=%d after=%d threshold=%d version=%s head=%s",
            len(cleaned),
            len(fc),
            collapse_threshold,
            VERSION,
            cleaned[:20],
        )
        return cleaned

    return fc


__all__ = ["VERSION", "apply_keep100_guard"]
