from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_PATCHED = False


def _install_order_5s_optional() -> bool:
    try:
        import core.startup.entry_order_5s_breakout_optional_patch as p
        fn = getattr(p, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[TONOSAMA 5SEC ADVISORY PATCH] order 5s optional installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] order 5s optional install failed")
        return False


def install() -> bool:
    global _PATCHED
    ok_order_5s = _install_order_5s_optional()
    if _PATCHED:
        return True and ok_order_5s
    try:
        import pandas as pd
        import trading.entry.tonosama.runner as r

        old_apply = getattr(r, "_apply_climax_guards", None)
        if callable(old_apply) and getattr(old_apply, "_tonosama_climax_safe_v2", False):
            _PATCHED = True
            logger.warning(
                "[TONOSAMA 5SEC ADVISORY PATCH] installed v2.1 existing_climax_safe=True order_5s_optional=%s",
                ok_order_5s,
            )
            return True and ok_order_5s

        def _patched_apply_climax_guards(x: pd.DataFrame, *, stage: str, sample_cols: list[str]) -> pd.DataFrame:
            # Do not override candidate generation. Keep existing guard behavior if present.
            if callable(old_apply):
                try:
                    return old_apply(x, stage=stage, sample_cols=sample_cols)
                except Exception:
                    logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] original climax guard failed")
            return x if x is not None else pd.DataFrame()

        _patched_apply_climax_guards._tonosama_climax_safe_v2 = True  # type: ignore[attr-defined]
        r._apply_climax_guards = _patched_apply_climax_guards
        _PATCHED = True
        logger.warning(
            "[TONOSAMA 5SEC ADVISORY PATCH] installed v2.1 no_override=True climax_safe=True order_5s_optional=%s",
            ok_order_5s,
        )
        return True and ok_order_5s
    except Exception:
        logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] install failed")
        return bool(ok_order_5s)


__all__ = ["install"]
