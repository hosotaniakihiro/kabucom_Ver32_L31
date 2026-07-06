# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-RANKING-LOGGER-CSV-GUARD"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _candidate_log_dirs() -> list[Path]:
    out: list[Path] = []
    for raw in (
        os.getenv("RANKING_LOG_DIR"),
        os.getenv("AUTOSTOCK_RANKING_LOG_DIR"),
        r"\\192.168.0.22\AutoStockBuyAndSell\Logs\ranking",
        str(Path.cwd() / "logs" / "ranking"),
    ):
        try:
            if raw and str(raw).strip():
                p = Path(str(raw).strip())
                if p not in out:
                    out.append(p)
        except Exception:
            pass
    return out


def _prepare_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path.is_dir()
    except OSError as exc:
        logger.warning("[RANKING LOGGER CSV GUARD] log dir unavailable path=%s err=%s", path, exc)
        return False
    except Exception:
        logger.debug("[RANKING LOGGER CSV GUARD] log dir check failed path=%s", path, exc_info=True)
        return False


def _resolve_log_dir() -> Path | None:
    for p in _candidate_log_dirs():
        if _prepare_dir(p):
            return p
    return None


def _now_str() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("RANKING_LOGGER_CSV_GUARD", True):
        logger.warning("[RANKING LOGGER CSV GUARD] disabled by env")
        return False
    try:
        import pandas as pd
        import trading.ranking.logging.ranking_logger as rl

        original = getattr(rl, "_save_csv", None)
        if not callable(original):
            logger.warning("[RANKING LOGGER CSV GUARD] target missing version=%s", VERSION)
            return False
        if getattr(original, "_ranking_logger_csv_guard_v1", False):
            _INSTALLED = True
            return True

        def _patched_save_csv(df: Any, interval: int):
            if not bool(getattr(rl, "SAVE_CSV", True)):
                return
            try:
                out_dir = _resolve_log_dir()
                if out_dir is None:
                    logger.warning("[RANKING LOGGER CSV GUARD] csv skipped: no usable log dir version=%s", VERSION)
                    return
                if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                    return
                cols = [
                    "symbol",
                    "score",
                    "_score_base",
                    "_score_trend",
                    "_score_momentum",
                    "_score_velocity",
                ]
                export_cols = [c for c in cols if c in df.columns]
                if not export_cols:
                    logger.warning("[RANKING LOGGER CSV GUARD] csv skipped: no export columns interval=%s cols=%s version=%s", interval, list(getattr(df, "columns", [])), VERSION)
                    return
                filename = out_dir / f"ranking_{interval}m_{_now_str()}.csv"
                df[export_cols].to_csv(str(filename), index=False)
                logger.info("[ranking_logger] saved csv: %s", filename)
            except OSError as exc:
                logger.warning("[RANKING LOGGER CSV GUARD] csv skipped due to OS error interval=%s err=%s version=%s", interval, exc, VERSION)
            except Exception:
                logger.exception("[RANKING LOGGER CSV GUARD] csv save failed safely interval=%s version=%s", interval, VERSION)

        _patched_save_csv._ranking_logger_csv_guard_v1 = True  # type: ignore[attr-defined]
        _patched_save_csv._original = original  # type: ignore[attr-defined]
        rl._save_csv = _patched_save_csv
        try:
            out_dir = _resolve_log_dir()
        except Exception:
            out_dir = None
        _INSTALLED = True
        logger.warning("[RANKING LOGGER CSV GUARD] installed log_dir=%s version=%s", out_dir, VERSION)
        return True
    except Exception:
        logger.exception("[RANKING LOGGER CSV GUARD] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING LOGGER CSV GUARD] auto install failed")


__all__ = ["install", "VERSION"]
