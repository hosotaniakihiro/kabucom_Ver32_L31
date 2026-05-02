# ============================================================
# File   : scheduler_jobs/push_summary/runners.py
# Version: Ver32_L01-PUSH-SUMMARY-RUNNER-JA-ANNOUNCE-BRIDGE
# ------------------------------------------------------------
# 機能:
#   - PUSH由来サマリーの実行線を一本化
#   - PUSH pipeline 実行
#   - PUSH専用cacheへ保存
#   - PUSH専用displayを呼び出す
#   - Discord通知を実行
#   - TOP候補抽出
#   - entry bridge 実行（候補銘柄のみ）
#   - SUMMARY AI entry 実行（AI_OK -> entry_pipeline）
#   - AI Gate runner の import 解決を堅牢化
#   - 日本語 announce bridge を追加
#   - 例外時のログ切り分け
# ============================================================

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pandas as pd

from trading.push_summary.cache import (
    set_push_summary,
    set_push_summary_latest_dt,
    set_push_summary_meta,
)
from scheduler_jobs.push_summary.display import display_push_summary

logger = logging.getLogger(__name__)

_SUMMARY_AI_RUNNER_CACHE: Optional[Callable[..., Any]] = None


# ============================================================
# env helpers
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in {"1", "true", "t", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "f", "no", "n", "off", "ng", ""}:
            return False
        return default
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


# ============================================================
# optional resolver
# ============================================================

def _resolve_push_pipeline():
    try:
        from trading.push_summary.pipeline import run_push_summary_pipeline
        return run_push_summary_pipeline
    except Exception:
        logger.exception("[push_summary.runners] resolve push pipeline failed")
        return None


def _resolve_announce_dispatcher():
    try:
        from scheduler_jobs.summary.announce_dispatcher import dispatch_summary_announce
        return dispatch_summary_announce
    except Exception:
        logger.exception("[push_summary.runners] resolve announce dispatcher failed")
        return None


def _resolve_top_candidates():
    # 旧実装互換
    try:
        from trading.summary.top_candidates import get_top_buy_candidates
        return get_top_buy_candidates
    except Exception:
        logger.debug("[push_summary.runners] resolve get_top_buy_candidates failed", exc_info=True)

    # 新実装候補
    try:
        from trading.summary.top_candidates import prepare_buy_top_df
        return prepare_buy_top_df
    except Exception:
        logger.debug("[push_summary.runners] resolve prepare_buy_top_df failed", exc_info=True)

    logger.exception("[push_summary.runners] resolve top candidates failed")
    return None


def _resolve_entry_bridge():
    try:
        from trading.signals.entry.runner_bridge import run_entry_bridge_for_symbol
        return run_entry_bridge_for_symbol
    except Exception:
        logger.exception("[push_summary.runners] resolve run_entry_bridge_for_symbol failed")
        return None


def _resolve_announce_bridge_push():
    try:
        from scheduler_jobs.summary.announce_bridge import (
            announce_push_top_candidates,
            build_push_top_candidates_message,
        )
        return announce_push_top_candidates, build_push_top_candidates_message
    except Exception:
        logger.debug("[push_summary.runners] resolve announce bridge push failed", exc_info=True)
        return None, None


def _callable_attrs_sample(mod: Any) -> list[str]:
    try:
        names: list[str] = []
        for name in dir(mod):
            low = str(name).lower()
            if "run" in low or "entry" in low or "gate" in low or "summary" in low or "start" in low:
                names.append(str(name))
        return sorted(names)[:60]
    except Exception:
        return []


def _try_resolve_callable(module_name: str, func_name: str):
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, func_name, None)

        if callable(fn):
            logger.info(
                "[push_summary.runners] summary_ai runner resolved %s.%s module_file=%s",
                module_name,
                func_name,
                getattr(mod, "__file__", None),
            )
            return fn

        logger.warning(
            "[push_summary.runners] summary_ai callable not found module=%s func=%s module_file=%s attrs_sample=%s",
            module_name,
            func_name,
            getattr(mod, "__file__", None),
            _callable_attrs_sample(mod),
        )
    except Exception as e:
        logger.warning(
            "[push_summary.runners] summary_ai resolve failed module=%s func=%s error=%s: %s",
            module_name,
            func_name,
            type(e).__name__,
            e,
        )
        logger.debug(
            "[push_summary.runners] summary_ai resolve traceback module=%s func=%s\n%s",
            module_name,
            func_name,
            traceback.format_exc(),
        )

    return None


def _project_root_candidates() -> list[Path]:
    roots: list[Path] = []

    try:
        roots.append(Path.cwd())
    except Exception:
        pass

    try:
        p = Path(__file__).resolve()
        for parent in p.parents:
            roots.append(parent)
    except Exception:
        pass

    try:
        env_root = os.environ.get("PROJECT_ROOT") or os.environ.get("KABU_PROJECT_ROOT")
        if env_root:
            roots.append(Path(env_root))
    except Exception:
        pass

    uniq: list[Path] = []
    seen: set[str] = set()

    for r in roots:
        try:
            rr = r.resolve()
            key = str(rr).lower()
            if key not in seen:
                uniq.append(rr)
                seen.add(key)
        except Exception:
            continue

    return uniq


def _find_summary_ai_file(filename: str) -> Optional[Path]:
    rel = Path("trading") / "entry" / "summary_ai" / filename

    for root in _project_root_candidates():
        try:
            path = root / rel
            if path.exists() and path.is_file():
                return path
        except Exception:
            continue

    return None


def _try_load_summary_ai_from_file(filename: str, func_name: str):
    path = _find_summary_ai_file(filename)
    if path is None:
        logger.warning(
            "[push_summary.runners] summary_ai direct file not found filename=%s roots=%s",
            filename,
            [str(x) for x in _project_root_candidates()[:8]],
        )
        return None

    try:
        module_key = f"_direct_push_summary_{filename.replace('.py', '').replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_key, str(path))
        if spec is None or spec.loader is None:
            logger.warning(
                "[push_summary.runners] summary_ai direct spec failed path=%s func=%s",
                path,
                func_name,
            )
            return None

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = mod
        spec.loader.exec_module(mod)

        fn = getattr(mod, func_name, None)
        if callable(fn):
            logger.info(
                "[push_summary.runners] summary_ai runner resolved direct-file path=%s func=%s",
                path,
                func_name,
            )
            return fn

        logger.warning(
            "[push_summary.runners] summary_ai direct callable not found path=%s func=%s attrs_sample=%s",
            path,
            func_name,
            _callable_attrs_sample(mod),
        )
    except Exception as e:
        logger.warning(
            "[push_summary.runners] summary_ai direct load failed path=%s func=%s error=%s: %s",
            path,
            func_name,
            type(e).__name__,
            e,
        )
        logger.debug(
            "[push_summary.runners] summary_ai direct traceback path=%s func=%s\n%s",
            path,
            func_name,
            traceback.format_exc(),
        )

    return None


def _resolve_summary_ai_runner(force_refresh: bool = False):
    global _SUMMARY_AI_RUNNER_CACHE

    if force_refresh:
        _SUMMARY_AI_RUNNER_CACHE = None

    if callable(_SUMMARY_AI_RUNNER_CACHE):
        return _SUMMARY_AI_RUNNER_CACHE

    candidates = [
        ("trading.entry.summary_ai.ai_gate_runner", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai.ai_gate_runner", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai.ai_gate_runner", "run_summary_ai_gate"),
        ("trading.entry.summary_ai.ai_gate_runner", "run_ai_gate_once"),
        ("trading.entry.summary_ai.ai_gate_runner", "start"),
        ("trading.entry.summary_ai.ai_gate_runner", "run"),
        ("trading.entry.summary_ai.runner", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai.runner", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai.runner", "run_summary_ai_gate"),
        ("trading.entry.summary_ai.runner", "run_ai_gate_once"),
        ("trading.entry.summary_ai.runner", "_run_summary_ai_entry"),
        ("trading.entry.summary_ai", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai", "run_summary_ai_gate"),
        ("trading.entry.summary_ai", "run_ai_gate_once"),
        ("trading.entry.summary_ai_entry_runner", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai_entry_runner", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai_entry_runner", "run_summary_ai_gate"),
        ("trading.entry.summary_ai_entry_runner", "run_ai_gate_once"),
    ]

    for module_name, func_name in candidates:
        fn = _try_resolve_callable(module_name, func_name)
        if callable(fn):
            _SUMMARY_AI_RUNNER_CACHE = fn
            return _SUMMARY_AI_RUNNER_CACHE

    direct_targets = [
        ("ai_gate_runner.py", "run_push_summary_ai_entry"),
        ("ai_gate_runner.py", "run_summary_ai_entry_from_df"),
        ("ai_gate_runner.py", "run_summary_ai_gate"),
        ("ai_gate_runner.py", "run_ai_gate_once"),
        ("ai_gate_runner.py", "start"),
        ("ai_gate_runner.py", "run"),
        ("runner.py", "run_push_summary_ai_entry"),
        ("runner.py", "run_summary_ai_entry_from_df"),
        ("runner.py", "run_summary_ai_gate"),
        ("runner.py", "run_ai_gate_once"),
        ("runner.py", "_run_summary_ai_entry"),
    ]

    for filename, func_name in direct_targets:
        fn = _try_load_summary_ai_from_file(filename, func_name)
        if callable(fn):
            _SUMMARY_AI_RUNNER_CACHE = fn
            return _SUMMARY_AI_RUNNER_CACHE

    logger.warning(
        "[push_summary.runners] summary_ai runner unavailable all candidates=%s direct_targets=%s",
        len(candidates),
        len(direct_targets),
    )
    return None


# ============================================================
# helpers
# ============================================================

def _extract_latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        for col in ("datetime", "dt", "timestamp"):
            if col in df.columns:
                s = df[col].dropna()
                if not s.empty:
                    return s.max()
        return None
    except Exception:
        logger.exception("[push_summary.runners] extract latest_dt failed")
        return None


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _safe_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "ok")
    try:
        return bool(v)
    except Exception:
        return default


def _normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip()
    except Exception:
        return ""


def _get_in_session(kwargs: Dict[str, Any]) -> bool:
    if "in_session" in kwargs:
        return _safe_bool(kwargs.get("in_session"), True)
    return True


def _get_df_from_kwargs(kwargs: Dict[str, Any], *keys: str) -> pd.DataFrame:
    for k in keys:
        v = kwargs.get(k)
        if isinstance(v, pd.DataFrame):
            return v
    return pd.DataFrame()


def _empty_result_dict(interval, df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    return {
        "df": df if isinstance(df, pd.DataFrame) else pd.DataFrame(),
        "interval": interval,
        "announce_results": {
            "buy": False,
            "sell": False,
            "grouped": False,
            "setup_summary": False,
            "bridge_push": False,
        },
        "announce_messages": {
            "push": "",
        },
        "top_buy": pd.DataFrame(),
        "entry_results": [],
        "summary_ai_result": {
            "candidates": [],
            "ai_results": [],
            "ai_ok": [],
            "approved_rows": [],
            "execution": None,
            "dry_run": False,
            "skip_reason": "not_run",
        },
    }


def _summary_ai_default_enabled(kwargs: Dict[str, Any]) -> bool:
    if "ai_entry_enabled" in kwargs:
        return _safe_bool(kwargs.get("ai_entry_enabled"), False)
    return _env_bool("SUMMARY_AI_ENTRY_ENABLED", True)


def _summary_ai_default_dry_run(kwargs: Dict[str, Any]) -> bool:
    if "ai_entry_dry_run" in kwargs:
        return _safe_bool(kwargs.get("ai_entry_dry_run"), False)
    return _env_bool("SUMMARY_AI_ENTRY_DRY_RUN", False)


def _summary_ai_default_require_market_open(kwargs: Dict[str, Any]) -> bool:
    if "ai_require_market_open" in kwargs:
        return _safe_bool(kwargs.get("ai_require_market_open"), True)
    return _env_bool("SUMMARY_AI_ENTRY_REQUIRE_MARKET_OPEN", True)


# ============================================================
# announce
# ============================================================

def _run_announce(
    *,
    df: pd.DataFrame,
    interval: int | str,
    in_session: bool,
    discord_sender=None,
    discord_webhook_url: Optional[str] = None,
    announce_enabled: bool = True,
) -> Dict[str, bool]:
    results = {
        "buy": False,
        "sell": False,
        "grouped": False,
        "setup_summary": False,
    }

    if not announce_enabled:
        logger.info("[push_summary.runners] announce skipped disabled interval=%r", interval)
        return results

    fn = _resolve_announce_dispatcher()
    if not callable(fn):
        logger.warning("[push_summary.runners] announce dispatcher unavailable interval=%r", interval)
        return results

    try:
        resolved = fn(
            df,
            interval=_safe_int(interval, 1),
            source="push",
            in_session=in_session,
            sender=discord_sender,
            webhook_url=discord_webhook_url,
        )

        if isinstance(resolved, dict):
            results.update({k: bool(v) for k, v in resolved.items() if k in results})
    except Exception:
        logger.exception("[push_summary.runners] announce failed interval=%r", interval)

    return results


def _run_announce_bridge_push(
    *,
    interval: int | str,
    discord_sender=None,
    announce_bridge_enabled: bool = False,
    top_n: int = 10,
    sides=("BUY", "SELL"),
) -> Dict[str, Any]:
    out = {
        "bridge_push": False,
        "push_message": "",
    }

    if not announce_bridge_enabled:
        return out

    announce_fn, build_msg_fn = _resolve_announce_bridge_push()
    if not callable(announce_fn) and not callable(build_msg_fn):
        logger.warning("[push_summary.runners] announce bridge push unavailable interval=%r", interval)
        return out

    try:
        if callable(build_msg_fn):
            out["push_message"] = build_msg_fn(
                intervals=(int(interval),),
                top_n=top_n,
                sides=sides,
                title=f"PUSH候補 ({interval}分)",
                max_rows=top_n,
            )

        if callable(announce_fn) and callable(discord_sender):
            out["bridge_push"] = bool(
                announce_fn(
                    discord_sender=discord_sender,
                    intervals=(int(interval),),
                    top_n=top_n,
                    sides=sides,
                    title=f"PUSH候補 ({interval}分)",
                    max_rows=top_n,
                )
            )
    except Exception:
        logger.exception("[push_summary.runners] announce bridge push failed interval=%r", interval)

    return out


# ============================================================
# top candidates
# ============================================================

def _run_top_candidates(
    *,
    df: pd.DataFrame,
    interval: int | str,
    top_n: int = 10,
    min_entry_score: Optional[float] = None,
    entry_candidates_enabled: bool = True,
) -> pd.DataFrame:
    if not entry_candidates_enabled:
        logger.info("[push_summary.runners] top candidates skipped disabled interval=%r", interval)
        return pd.DataFrame()

    fn = _resolve_top_candidates()
    if not callable(fn):
        logger.warning("[push_summary.runners] top candidates resolver unavailable interval=%r", interval)
        return pd.DataFrame()

    try:
        if min_entry_score is None:
            min_entry_score = 57.0 if _safe_int(interval, 1) == 5 else 55.0

        # get_top_buy_candidates 互換
        try:
            top_buy = fn(
                df,
                top_n=top_n,
                latest_per_symbol=True,
                only_setup_entry=True,
                min_entry_score=float(min_entry_score),
            )
        except TypeError:
            # prepare_buy_top_df 互換
            top_buy = fn(df, top_n=top_n)

        if not isinstance(top_buy, pd.DataFrame):
            return pd.DataFrame()

        return top_buy.reset_index(drop=True)
    except Exception:
        logger.exception("[push_summary.runners] get top candidates failed interval=%r", interval)
        return pd.DataFrame()


# ============================================================
# entry bridge
# ============================================================

def _run_entry_candidates(
    *,
    top_buy: pd.DataFrame,
    interval: int | str,
    df_1m_summary: pd.DataFrame,
    df_3m_summary: pd.DataFrame,
    df_5m_summary: pd.DataFrame,
    signal_state_map: Optional[Dict[str, Any]],
    prev_state_map: Optional[Dict[str, Any]],
    position_state_map: Optional[Dict[str, Any]],
    recent_realized_pnl_map: Optional[Dict[str, float]],
    entry_enabled: bool = False,
    commit: bool = True,
    now=None,
    min_setup_score_buy: Optional[float] = None,
    min_setup_score_sell: Optional[float] = None,
    use_setup_gate: bool = True,
    use_retest_gate: bool = True,
) -> list[Dict[str, Any]]:
    entry_results: list[Dict[str, Any]] = []

    if not entry_enabled:
        logger.info("[push_summary.runners] entry bridge skipped disabled interval=%r", interval)
        return entry_results

    if top_buy is None or top_buy.empty:
        logger.info("[push_summary.runners] entry bridge skipped no candidates interval=%r", interval)
        return entry_results

    if (
        not isinstance(signal_state_map, dict)
        or not isinstance(prev_state_map, dict)
        or not isinstance(position_state_map, dict)
    ):
        logger.warning("[push_summary.runners] entry bridge skipped missing state maps interval=%r", interval)
        return entry_results

    fn = _resolve_entry_bridge()
    if not callable(fn):
        logger.warning("[push_summary.runners] entry bridge unavailable interval=%r", interval)
        return entry_results

    if min_setup_score_buy is None:
        min_setup_score_buy = 20.0 if _safe_int(interval, 1) != 5 else 22.0
    if min_setup_score_sell is None:
        min_setup_score_sell = 20.0 if _safe_int(interval, 1) != 5 else 22.0

    for _, row in top_buy.iterrows():
        symbol = _normalize_symbol(row.get("symbol"))
        if not symbol:
            continue

        signal_state = signal_state_map.get(symbol)
        prev_state = prev_state_map.get(symbol)
        position_state = position_state_map.get(symbol)

        if signal_state is None or prev_state is None or position_state is None:
            logger.warning(
                "[push_summary.runners] entry skipped state missing interval=%r symbol=%s",
                interval,
                symbol,
            )
            continue

        try:
            result = fn(
                symbol=symbol,
                df_1m=df_1m_summary,
                df_3m=df_3m_summary,
                df_5m=df_5m_summary,
                signal_state=signal_state,
                prev_state=prev_state,
                position_state=position_state,
                recent_realized_pnl=(recent_realized_pnl_map or {}).get(symbol),
                now=now,
                commit=commit,
                min_setup_score_buy=float(min_setup_score_buy),
                min_setup_score_sell=float(min_setup_score_sell),
                use_setup_gate=bool(use_setup_gate),
                use_retest_gate=bool(use_retest_gate),
            )

            if isinstance(result, dict):
                entry_results.append(result)
            else:
                entry_results.append({"symbol": symbol, "result": result})

            if isinstance(result, dict) and result.get("signal"):
                logger.info(
                    "[push_summary.runners] entry signal interval=%r symbol=%s signal=%s reasons=%s",
                    interval,
                    symbol,
                    result.get("signal"),
                    result.get("reasons"),
                )
        except Exception:
            logger.exception(
                "[push_summary.runners] run_entry_bridge_for_symbol failed interval=%r symbol=%s",
                interval,
                symbol,
            )

    return entry_results


# ============================================================
# summary AI entry
# ============================================================

def _normalize_ai_result(
    result: Any,
    *,
    ai_entry_dry_run: bool,
    skip_reason: str = "invalid_result",
) -> Dict[str, Any]:
    if isinstance(result, dict):
        result.setdefault("candidates", [])
        result.setdefault("ai_results", [])
        result.setdefault("ai_ok", [])
        result.setdefault("approved_rows", [])
        result.setdefault("execution", None)
        result.setdefault("dry_run", ai_entry_dry_run)
        return result

    if isinstance(result, pd.DataFrame):
        rows = result.to_dict("records")
        return {
            "candidates": rows,
            "ai_results": rows,
            "ai_ok": rows,
            "approved_rows": rows,
            "execution": {
                "executed": False,
                "skip_reason": "runner_returned_dataframe",
            },
            "dry_run": ai_entry_dry_run,
        }

    if isinstance(result, list):
        return {
            "candidates": result,
            "ai_results": result,
            "ai_ok": result,
            "approved_rows": result,
            "execution": {
                "executed": False,
                "skip_reason": "runner_returned_list",
            },
            "dry_run": ai_entry_dry_run,
        }

    return {
        "candidates": [],
        "ai_results": [],
        "ai_ok": [],
        "approved_rows": [],
        "execution": None,
        "dry_run": ai_entry_dry_run,
        "skip_reason": skip_reason,
    }


def _run_summary_ai_entry(
    *,
    df: pd.DataFrame,
    interval: int | str,
    ai_entry_enabled: bool = False,
    ai_entry_dry_run: bool = False,
    ai_require_market_open: bool = True,
    ai_top_n: int = 10,
    ai_max_entries: int = 1,
    ai_min_confidence: float = 0.65,
    ai_min_buy_score: float = 5.0,
    ai_max_sell_score: float = 2.0,
    ai_min_volume: float = 1.0,
    ai_min_price: float = 1.0,
    source: str = "SUMMARY",
    now=None,
) -> Dict[str, Any]:
    if not ai_entry_enabled:
        logger.info("[push_summary.runners] summary_ai skipped disabled interval=%r", interval)
        return {
            "candidates": [],
            "ai_results": [],
            "ai_ok": [],
            "approved_rows": [],
            "execution": None,
            "dry_run": ai_entry_dry_run,
            "skip_reason": "disabled",
        }

    if not isinstance(df, pd.DataFrame) or df.empty:
        logger.info("[push_summary.runners] summary_ai skipped empty df interval=%r", interval)
        return {
            "candidates": [],
            "ai_results": [],
            "ai_ok": [],
            "approved_rows": [],
            "execution": None,
            "dry_run": ai_entry_dry_run,
            "skip_reason": "empty_df",
        }

    fn = _resolve_summary_ai_runner()
    if not callable(fn):
        fn = _resolve_summary_ai_runner(force_refresh=True)

    if not callable(fn):
        logger.warning("[push_summary.runners] summary_ai runner unavailable interval=%r", interval)
        return {
            "candidates": [],
            "ai_results": [],
            "ai_ok": [],
            "approved_rows": [],
            "execution": None,
            "dry_run": ai_entry_dry_run,
            "skip_reason": "runner_unavailable",
        }

    try:
        logger.info(
            "[push_summary.runners] summary_ai start interval=%r rows=%d runner=%s dry_run=%s require_market_open=%s top_n=%s max_entries=%s min_conf=%.3f min_buy=%.2f max_sell=%.2f",
            interval,
            len(df),
            getattr(fn, "__name__", repr(fn)),
            ai_entry_dry_run,
            ai_require_market_open,
            ai_top_n,
            ai_max_entries,
            ai_min_confidence,
            ai_min_buy_score,
            ai_max_sell_score,
        )

        result = fn(
            summary_df=df,
            df=df,
            interval=interval,
            source=str(source or "SUMMARY").upper(),
            now=now,
            dry_run=ai_entry_dry_run,
            require_market_open=ai_require_market_open,
            top_n=ai_top_n,
            max_entries=ai_max_entries,
            min_ai_confidence=ai_min_confidence,
            min_confidence=ai_min_confidence,
            min_conf=ai_min_confidence,
            min_buy_score=ai_min_buy_score,
            max_sell_score=ai_max_sell_score,
            min_volume=ai_min_volume,
            min_price=ai_min_price,
            require_buy_target=False,
            exclude_etf_fund=True,
            default_dominant_ratio=1.0,
        )

        normalized = _normalize_ai_result(
            result,
            ai_entry_dry_run=ai_entry_dry_run,
            skip_reason="invalid_result",
        )

        execution = normalized.get("execution") if isinstance(normalized.get("execution"), dict) else {}

        logger.info(
            "[push_summary.runners] summary_ai done interval=%r candidates=%s ai_results=%s ai_ok=%s approved=%s executed=%s dry_run=%s skip=%s",
            interval,
            len(normalized.get("candidates", []) or []),
            len(normalized.get("ai_results", []) or []),
            len(normalized.get("ai_ok", []) or []),
            len(normalized.get("approved_rows", []) or []),
            bool(execution.get("executed")) if isinstance(execution, dict) else False,
            ai_entry_dry_run,
            execution.get("skip_reason") if isinstance(execution, dict) else normalized.get("skip_reason"),
        )

        return normalized
    except TypeError:
        logger.warning(
            "[push_summary.runners] summary_ai kwargs call failed interval=%r -> retry compact",
            interval,
            exc_info=True,
        )

        try:
            result = fn(
                summary_df=df,
                interval=interval,
                source=str(source or "SUMMARY").upper(),
                dry_run=ai_entry_dry_run,
                require_market_open=ai_require_market_open,
                top_n=ai_top_n,
                max_entries=ai_max_entries,
                min_ai_confidence=ai_min_confidence,
            )

            return _normalize_ai_result(
                result,
                ai_entry_dry_run=ai_entry_dry_run,
                skip_reason="invalid_result_compact",
            )
        except Exception:
            logger.exception("[push_summary.runners] summary_ai compact retry failed interval=%r", interval)
            return {
                "candidates": [],
                "ai_results": [],
                "ai_ok": [],
                "approved_rows": [],
                "execution": None,
                "dry_run": ai_entry_dry_run,
                "skip_reason": "exception_compact_retry",
            }
    except Exception:
        logger.exception("[push_summary.runners] summary_ai failed interval=%r", interval)
        return {
            "candidates": [],
            "ai_results": [],
            "ai_ok": [],
            "approved_rows": [],
            "execution": None,
            "dry_run": ai_entry_dry_run,
            "skip_reason": "exception",
        }


# ============================================================
# public
# ============================================================

def run_push_summary_job(
    interval: int | str = 1,
    display: bool = True,
    **kwargs,
) -> pd.DataFrame | Dict[str, Any]:
    details = _empty_result_dict(interval)

    try:
        logger.info("[push_summary.runners] job start interval=%r kwargs_keys=%s", interval, sorted(kwargs.keys()))

        fn = _resolve_push_pipeline()
        if not callable(fn):
            logger.error("[push_summary.runners] push pipeline is not callable interval=%r", interval)
            return details if _safe_bool(kwargs.get("return_details"), False) else pd.DataFrame()

        df = fn(interval=interval, **kwargs)
        if not isinstance(df, pd.DataFrame):
            logger.warning(
                "[push_summary.runners] pipeline returned non-DataFrame interval=%r type=%s",
                interval,
                type(df).__name__,
            )
            df = pd.DataFrame()

        details["df"] = df

        set_push_summary(interval, df)
        set_push_summary_latest_dt(interval, _extract_latest_dt(df))
        set_push_summary_meta(
            interval,
            {
                "rows": len(df),
                "columns": list(df.columns),
                "source": "push",
                "interval": interval,
            },
        )

        logger.info(
            "[push_summary.runners] job pipeline/cache finished interval=%r rows=%s cols=%s",
            interval,
            len(df),
            len(df.columns) if isinstance(df, pd.DataFrame) else 0,
        )

        if display:
            try:
                display_push_summary(interval=interval)
            except Exception:
                logger.exception("[push_summary.runners] display failed interval=%r", interval)
        else:
            logger.info("[push_summary.runners] display skipped interval=%r reason=display_false", interval)

        in_session = _get_in_session(kwargs)
        announce_results = _run_announce(
            df=df,
            interval=interval,
            in_session=in_session,
            discord_sender=kwargs.get("discord_sender"),
            discord_webhook_url=kwargs.get("discord_webhook_url"),
            announce_enabled=_safe_bool(kwargs.get("announce_enabled"), True),
        )
        details["announce_results"] = announce_results

        bridge_res = _run_announce_bridge_push(
            interval=interval,
            discord_sender=kwargs.get("discord_sender"),
            announce_bridge_enabled=_safe_bool(kwargs.get("announce_bridge"), False),
            top_n=_safe_int(kwargs.get("top_n_buy"), 10),
            sides=kwargs.get("sides", ("BUY", "SELL")),
        )
        details["announce_results"]["bridge_push"] = bool(bridge_res.get("bridge_push"))
        details["announce_messages"]["push"] = bridge_res.get("push_message", "")

        top_buy = _run_top_candidates(
            df=df,
            interval=interval,
            top_n=_safe_int(kwargs.get("top_n_buy"), 10),
            min_entry_score=kwargs.get("min_entry_score"),
            entry_candidates_enabled=_safe_bool(kwargs.get("entry_candidates_enabled"), True),
        )
        details["top_buy"] = top_buy

        df_1m_summary = _get_df_from_kwargs(kwargs, "df_1m_summary", "summary_1m_df")
        df_3m_summary = _get_df_from_kwargs(kwargs, "df_3m_summary", "summary_3m_df")
        df_5m_summary = _get_df_from_kwargs(kwargs, "df_5m_summary", "summary_5m_df")

        iv = _safe_int(interval, 1)
        if df_1m_summary.empty and iv == 1:
            df_1m_summary = df
        if df_3m_summary.empty and iv == 3:
            df_3m_summary = df
        if df_5m_summary.empty and iv == 5:
            df_5m_summary = df

        entry_results = _run_entry_candidates(
            top_buy=top_buy,
            interval=interval,
            df_1m_summary=df_1m_summary,
            df_3m_summary=df_3m_summary,
            df_5m_summary=df_5m_summary,
            signal_state_map=kwargs.get("signal_state_map"),
            prev_state_map=kwargs.get("prev_state_map"),
            position_state_map=kwargs.get("position_state_map"),
            recent_realized_pnl_map=kwargs.get("recent_realized_pnl_map"),
            entry_enabled=_safe_bool(kwargs.get("entry_enabled"), False),
            commit=_safe_bool(kwargs.get("commit_entry"), True),
            now=kwargs.get("now"),
            min_setup_score_buy=kwargs.get("min_setup_score_buy"),
            min_setup_score_sell=kwargs.get("min_setup_score_sell"),
            use_setup_gate=_safe_bool(kwargs.get("use_setup_gate"), True),
            use_retest_gate=_safe_bool(kwargs.get("use_retest_gate"), True),
        )
        details["entry_results"] = entry_results

        ai_enabled = _summary_ai_default_enabled(kwargs)
        ai_dry_run = _summary_ai_default_dry_run(kwargs)
        ai_require_market_open = _summary_ai_default_require_market_open(kwargs)

        summary_ai_result = _run_summary_ai_entry(
            df=df,
            interval=interval,
            ai_entry_enabled=ai_enabled,
            ai_entry_dry_run=ai_dry_run,
            ai_require_market_open=ai_require_market_open,
            ai_top_n=_safe_int(kwargs.get("ai_top_n"), _env_int("SUMMARY_AI_ENTRY_TOP_N", 10)),
            ai_max_entries=_safe_int(kwargs.get("ai_max_entries"), _env_int("SUMMARY_AI_ENTRY_MAX_ENTRIES", 1)),
            ai_min_confidence=_safe_float(kwargs.get("ai_min_confidence"), _env_float("SUMMARY_AI_ENTRY_MIN_CONFIDENCE", 0.65)),
            ai_min_buy_score=_safe_float(kwargs.get("ai_min_buy_score"), _env_float("SUMMARY_AI_ENTRY_MIN_BUY_SCORE", 5.0)),
            ai_max_sell_score=_safe_float(kwargs.get("ai_max_sell_score"), _env_float("SUMMARY_AI_ENTRY_MAX_SELL_SCORE", 2.0)),
            ai_min_volume=_safe_float(kwargs.get("ai_min_volume"), _env_float("SUMMARY_AI_ENTRY_MIN_VOLUME", 1.0)),
            ai_min_price=_safe_float(kwargs.get("ai_min_price"), _env_float("SUMMARY_AI_ENTRY_MIN_PRICE", 1.0)),
            source="SUMMARY",
            now=kwargs.get("now"),
        )
        details["summary_ai_result"] = summary_ai_result

        if _safe_bool(kwargs.get("return_details"), False):
            return details
        return df
    except Exception:
        logger.exception("[push_summary.runners] run_push_summary_job failed interval=%r", interval)
        if _safe_bool(kwargs.get("return_details"), False):
            return details
        return pd.DataFrame()


def job_push_summary(
    interval: int | str = 1,
    display: bool = True,
    **kwargs,
) -> pd.DataFrame | Dict[str, Any]:
    return run_push_summary_job(interval=interval, display=display, **kwargs)


def run_summary_job(
    interval: int | str = 1,
    display: bool = True,
    **kwargs,
) -> pd.DataFrame | Dict[str, Any]:
    return run_push_summary_job(interval=interval, display=display, **kwargs)