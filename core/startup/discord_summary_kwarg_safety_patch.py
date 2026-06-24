# ============================================================
# File   : core/startup/discord_summary_kwarg_safety_patch.py
# Version: V2.7-DISPLAY-KWARG-SAFETY-FALLBACK
# ------------------------------------------------------------
# Purpose:
#   1) display系関数へ interval=1 等の未知kwargsが渡っても壊れないようにする。
#   2) Discord SUMMARY TOP10 を読みやすい3行表示に寄せる。
#   3) 古い「結果時刻」のSUMMARYをDiscordへ送らない。
#   4) 定時表示に空DF/Noneが渡った場合、直近の完成済みsummaryを再取得して表示する。
# ============================================================

from __future__ import annotations

import datetime as dt
import inspect
import logging
import os
import re
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINALS: dict[str, Callable] = {}


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enabled", "enable"}
    except Exception:
        return bool(default)


def _is_df_like(v: Any) -> bool:
    try:
        import pandas as pd
        return isinstance(v, pd.DataFrame)
    except Exception:
        return False


def _df_empty(v: Any) -> bool:
    try:
        return (v is None) or (_is_df_like(v) and bool(v.empty))
    except Exception:
        return True


def _label_from_value(v: Any) -> str | None:
    if v is None or _is_df_like(v):
        return None
    try:
        return f"{int(float(v))}min"
    except Exception:
        s = str(v).strip()
        if not s or len(s) > 40 or "\n" in s:
            return None
        return s


def _interval_min_from_label(label: Any) -> int:
    try:
        if isinstance(label, (int, float)):
            return max(1, int(label))
        s = str(label or "").strip().lower()
        m = re.search(r"(\d+)", s)
        if m:
            return max(1, int(m.group(1)))
    except Exception:
        pass
    return 1


def _normalize(summary_df: Any, interval_label: Any, kwargs: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    kw = dict(kwargs or {})
    if _is_df_like(interval_label) and not _is_df_like(summary_df):
        label = _label_from_value(summary_df) or "1min"
        summary_df = interval_label
    else:
        label = None
        for k in ("interval_label", "interval", "interval_min", "minutes", "tf"):
            if k in kw:
                label = _label_from_value(kw.get(k))
                if label:
                    break
        label = label or _label_from_value(interval_label) or "1min"
    for k in ("interval", "interval_min", "minutes", "tf", "interval_label"):
        kw.pop(k, None)
    return summary_df, label, kw


def _filter_kwargs(fn: Callable, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(kwargs or {})
        allowed = {
            name for name, p in params.items()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        return {k: v for k, v in (kwargs or {}).items() if k in allowed}
    except Exception:
        return {}


def _safe_call(fn: Callable, summary_df: Any, interval_label: str, notify_discord: bool, kwargs: dict[str, Any]):
    safe_kwargs = _filter_kwargs(fn, kwargs)
    try:
        return fn(summary_df, interval_label=interval_label, notify_discord=notify_discord, **safe_kwargs)
    except TypeError as e:
        if "unexpected keyword argument" in str(e):
            logger.warning(
                "[DISCORD KWARG SAFETY] retry without optional kwargs fn=%s err=%s keys=%s",
                getattr(fn, "__name__", str(fn)), e, list(safe_kwargs.keys()),
            )
            return fn(summary_df, interval_label=interval_label, notify_discord=notify_discord)
        raise


def _call_candidate(fn: Callable, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except TypeError:
        return None
    except Exception:
        logger.debug("[SUMMARY DISPLAY FALLBACK] candidate call failed fn=%s", getattr(fn, "__name__", fn), exc_info=True)
        return None


def _fallback_summary_df(summary_df: Any, interval_label: str, fn_name: str, kwargs: dict[str, Any]) -> Any:
    """定時表示でNone/空DFが来た時に、直近の完成済みsummaryを取り直す。"""
    if not _df_empty(summary_df):
        return summary_df
    if not _env_bool("SUMMARY_DISPLAY_FALLBACK_ENABLED", True):
        return summary_df

    interval_min = _interval_min_from_label(interval_label)
    source_hint = str(kwargs.get("source") or "").strip().lower()
    if not source_hint:
        source_hint = "ranking" if "ranking" in str(fn_name).lower() else "push"

    try:
        from core.global_context import context as gc
    except Exception:
        logger.debug("[SUMMARY DISPLAY FALLBACK] global_context import failed", exc_info=True)
        return summary_df

    candidates: list[tuple[str, Callable | None]] = []
    if source_hint == "ranking":
        candidates.extend([
            ("gc.get_ranking_summary", getattr(gc, "get_ranking_summary", None)),
            ("gc.get_merged_summary", getattr(gc, "get_merged_summary", None)),
        ])
    else:
        candidates.extend([
            ("gc.get_push_summary", getattr(gc, "get_push_summary", None)),
            ("gc.get_merged_summary", getattr(gc, "get_merged_summary", None)),
            ("gc.get_summary", getattr(gc, "get_summary", None)),
        ])

    for label, fn in candidates:
        if not callable(fn):
            continue
        call_patterns = [
            ((interval_min,), {"source": source_hint}),
            ((), {"tf": interval_min, "source": source_hint}),
            ((), {"interval": interval_min, "source": source_hint}),
            ((), {"interval_min": interval_min, "source": source_hint}),
            ((interval_min,), {}),
            ((), {"tf": interval_min}),
            ((), {"interval": interval_min}),
            ((), {"interval_min": interval_min}),
        ]
        for args, kw in call_patterns:
            df = _call_candidate(fn, *args, **kw)
            if not _df_empty(df):
                try:
                    rows = len(df) if hasattr(df, "__len__") else "?"
                    cols = len(getattr(df, "columns", []) or [])
                except Exception:
                    rows, cols = "?", "?"
                logger.warning(
                    "[SUMMARY DISPLAY FALLBACK] recovered summary fn=%s source=%s interval=%s rows=%s cols=%s original_empty=%s",
                    label, source_hint, interval_min, rows, cols, _df_empty(summary_df),
                )
                return df

    logger.warning(
        "[SUMMARY DISPLAY FALLBACK] no completed summary available fn=%s source=%s interval=%s original_empty=%s",
        fn_name, source_hint, interval_min, _df_empty(summary_df),
    )
    return summary_df


def _wrap(fn: Callable) -> Callable:
    fn_name = getattr(fn, "__name__", str(fn))

    def _wrapped(summary_df=None, interval_label="1min", *, notify_discord=True, **kwargs):
        summary_df, interval_label, kwargs = _normalize(summary_df, interval_label, kwargs)
        summary_df = _fallback_summary_df(summary_df, interval_label, fn_name, kwargs)
        return _safe_call(fn, summary_df, interval_label, notify_discord, kwargs)

    _wrapped._discord_kwarg_safety_patch = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def _clean(v: Any, *, max_len: int = 24) -> str:
    try:
        s = str(v if v is not None else "").replace("\r", " ").replace("\n", " ").strip()
        if len(s) > max_len:
            return s[: max_len - 1] + "…"
        return s
    except Exception:
        return ""


def _first(row: Any, keys: tuple[str, ...], default: Any = "-") -> Any:
    try:
        for k in keys:
            v = row.get(k) if hasattr(row, "get") else None
            if v is None:
                continue
            try:
                if np.isnan(v):
                    continue
            except Exception:
                pass
            if str(v).strip() != "":
                return v
    except Exception:
        pass
    return default


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "-" or str(v).strip() == "":
            return default
        x = float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default


def _has_num(v: Any) -> bool:
    try:
        x = float(v)
        return np.isfinite(x)
    except Exception:
        return False


def _fmt_metric(v: Any, digits: int = 2) -> str:
    if not _has_num(v):
        return "-"
    return f"{float(v):.{digits}f}"


def _fmt_price(v: Any) -> str:
    if not _has_num(v):
        return "-"
    x = float(v)
    if abs(x) >= 1000:
        return f"{x:.1f}"
    return f"{x:.2f}"


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _extract_interval_min(text: str) -> int:
    try:
        m = re.search(r"(?:TOP10|SUMMARY TOP10)\s*\(?\s*(\d+)\s*(?:min|m|分)?", text, flags=re.I)
        if m:
            return max(1, int(m.group(1)))
        m = re.search(r"\((\d+)\s*(?:min|m|分)?\)", text, flags=re.I)
        if m:
            return max(1, int(m.group(1)))
    except Exception:
        pass
    return 1


def _extract_result_dt(text: str) -> dt.datetime | None:
    try:
        m = re.search(r"結果時刻\s*=\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", text)
        if not m:
            return None
        return dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _stale_limit_min(interval_min: int) -> float:
    override = os.getenv("SUMMARY_DISCORD_STALE_LIMIT_MIN")
    if override is not None and str(override).strip() != "":
        return _env_float("SUMMARY_DISCORD_STALE_LIMIT_MIN", 8.0)
    if interval_min <= 1:
        return _env_float("SUMMARY_DISCORD_STALE_LIMIT_1MIN", 4.0)
    if interval_min <= 3:
        return _env_float("SUMMARY_DISCORD_STALE_LIMIT_3MIN", 8.0)
    if interval_min <= 5:
        return _env_float("SUMMARY_DISCORD_STALE_LIMIT_5MIN", 12.0)
    return _env_float("SUMMARY_DISCORD_STALE_LIMIT_OTHER", 15.0)


def _should_skip_stale_discord(lines: list[str], title: str | None) -> tuple[bool, str]:
    try:
        if not _env_bool("SUMMARY_DISCORD_STALE_GUARD", True):
            return False, "disabled"
        text = (str(title or "") + "\n" + "\n".join(str(x) for x in (lines or [])))[:6000]
        if "SUMMARY TOP10" not in text and "PUSH SUMMARY TOP10" not in text and "RANKING SUMMARY TOP10" not in text:
            return False, "not_summary"
        result_dt = _extract_result_dt(text)
        if result_dt is None:
            return False, "no_result_time"
        interval_min = _extract_interval_min(text)
        now = dt.datetime.now().replace(tzinfo=None, microsecond=0)
        age_min = (now - result_dt).total_seconds() / 60.0
        limit = _stale_limit_min(interval_min)
        if age_min > limit:
            return True, f"result_dt={result_dt} now={now} age_min={age_min:.1f} limit_min={limit:.1f} interval={interval_min}"
        return False, f"fresh age_min={age_min:.1f} limit_min={limit:.1f} interval={interval_min}"
    except Exception as e:
        return False, f"guard_error={e}"


def _install_stale_send_guard(disp: Any) -> int:
    try:
        old = getattr(disp, "_send_to_discord", None)
        if not callable(old) or getattr(old, "_summary_discord_stale_guard_v27", False):
            return 0

        def _send_guarded(lines: list[str], title: str | None = None) -> None:
            skip, reason = _should_skip_stale_discord(lines or [], title)
            if skip:
                logger.warning("[DISCORD SUMMARY STALE GUARD] skip old summary discord %s", reason)
                return None
            logger.info("[DISCORD SUMMARY STALE GUARD] allow summary discord %s", reason)
            return old(lines, title=title)

        _send_guarded._summary_discord_stale_guard_v27 = True  # type: ignore[attr-defined]
        _send_guarded._original = old  # type: ignore[attr-defined]
        disp._send_to_discord = _send_guarded
        return 1
    except Exception:
        logger.exception("[DISCORD SUMMARY STALE GUARD] install failed")
        return 0


def _reason_ja(row: Any, side: str) -> str:
    side_u = str(side or "").upper()
    parts: list[str] = []
    buy = _f(_first(row, ("disp_buy_score", "score_buy", "buy_score"), 0.0))
    sell = _f(_first(row, ("disp_sell_score", "score_sell", "sell_score"), 0.0))
    slope = _f(_first(row, ("disp_slope", "slope", "score_slope", "slope_atr_scaled"), 0.0))
    mtf = _f(_first(row, ("disp_mtf", "mtf", "score_mtf", "mtf_score"), 0.0))
    rsi = _f(_first(row, ("disp_rsi", "rsi"), 50.0), 50.0)
    macd = _f(_first(row, ("disp_macd", "macd"), 0.0))
    if side_u == "BUY":
        if buy > 0:
            parts.append(f"買いスコア優勢 buy={buy:.2f}")
        parts.append(f"上向き傾き slope={slope:.4f}" if slope > 0 else f"傾きは弱い slope={slope:.4f}")
    else:
        if sell > 0:
            parts.append(f"売りスコア優勢 sell={sell:.2f}")
        parts.append(f"下向き傾き slope={slope:.4f}" if slope < 0 else f"下落傾きは弱い slope={slope:.4f}")
    if mtf:
        parts.append(f"複数時間足={mtf:.2f}")
    if rsi != 50.0:
        parts.append(f"RSI={rsi:.1f}")
    if macd:
        parts.append(f"MACD={macd:.3f}")
    code_reason = _clean(_first(row, ("reason", "entry_reason", "flag_reason", "signal_reason"), ""), max_len=40)
    if code_reason and code_reason not in {"-", "flag_score"}:
        parts.append(f"元理由={code_reason}")
    elif code_reason == "flag_score":
        parts.append("スコア条件で抽出")
    return " / ".join(parts) if parts else "理由データ不足: スコア・傾き・補助指標から判定"


def _compact_candidate_line(i: int, row: Any, *, side: str) -> str:
    symbol = _clean(_first(row, ("symbol",), ""), max_len=8)
    name = _clean(_first(row, ("symbolname_view", "symbolname", "name"), ""), max_len=18)
    score = _first(row, ("disp_score", "score", "display_score", "final_score"), np.nan)
    buy = _first(row, ("disp_buy_score", "score_buy", "buy_score"), np.nan)
    sell = _first(row, ("disp_sell_score", "score_sell", "sell_score"), np.nan)
    close = _first(row, ("disp_close", "close", "close_price", "current_price", "price"), np.nan)
    slope = _first(row, ("disp_slope", "slope", "score_slope", "slope_atr_scaled"), np.nan)
    mtf = _first(row, ("disp_mtf", "mtf", "score_mtf", "mtf_score"), np.nan)
    rsi = _first(row, ("disp_rsi", "rsi"), np.nan)
    macd = _first(row, ("disp_macd", "macd"), np.nan)
    reason = _reason_ja(row, side)
    mark = "🟦" if str(side).upper() == "BUY" else "🟥"
    return (
        f"{mark} {i}. {symbol} {name} Price={_fmt_price(close)} Score={_fmt_metric(score)} Buy={_fmt_metric(buy)} Sell={_fmt_metric(sell)}\n"
        f"   Slope={_fmt_metric(slope, 4)} MTF={_fmt_metric(mtf)} RSI={_fmt_metric(rsi)} MACD={_fmt_metric(macd)}\n"
        f"   理由={reason}"
    )


def _install_compact_discord_builder(disp: Any) -> int:
    patched = 0
    try:
        old = getattr(disp, "_build_discord_candidate_2lines", None)
        if callable(old):
            def _candidate(i: int, row: Any, *, side: str) -> str:
                return _compact_candidate_line(i, row, side=side)
            _candidate._discord_3lines_readable_labels_ja_reason_v27 = True  # type: ignore[attr-defined]
            _candidate._original = old  # type: ignore[attr-defined]
            disp._build_discord_candidate_2lines = _candidate
            patched += 1
        old_reason = getattr(disp, "_reason_text_for_discord", None)
        def _reason(row: Any, side: str) -> str:
            return _reason_ja(row, side)
        _reason._discord_3lines_readable_labels_ja_reason_v27 = True  # type: ignore[attr-defined]
        _reason._original = old_reason  # type: ignore[attr-defined]
        disp._reason_text_for_discord = _reason
        patched += 1
    except Exception:
        logger.exception("[DISCORD KWARG SAFETY] compact discord builder install failed")
    return patched


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import scheduler_jobs.summary.display as disp
        patched = 0
        for name in (
            "print_summary_top10",
            "print_ranking_summary_top10",
            "display_summary",
            "display_push_summary",
            "print_push_summary",
            "display_ranking_summary",
            "print_ranking_summary",
        ):
            fn = getattr(disp, name, None)
            if callable(fn) and not getattr(fn, "_discord_kwarg_safety_patch", False):
                _ORIGINALS[name] = fn
                setattr(disp, name, _wrap(fn))
                patched += 1
        compact_patched = _install_compact_discord_builder(disp)
        stale_patched = _install_stale_send_guard(disp)
        _PATCHED = True
        logger.warning(
            "[DISCORD KWARG SAFETY] installed V2.7 patched=%s three_lines_readable_labels=%s stale_guard=%s display_fallback=%s",
            patched,
            compact_patched,
            stale_patched,
            _env_bool("SUMMARY_DISPLAY_FALLBACK_ENABLED", True),
        )
        return True
    except Exception:
        logger.exception("[DISCORD KWARG SAFETY] install failed")
        return False


__all__ = ["install"]
