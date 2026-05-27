# ============================================================
# File   : core/startup/discord_summary_kwarg_safety_patch.py
# Version: V2.1-DISPLAY-KWARG-SAFETY-RICH-JA-STALE-GUARD
# ------------------------------------------------------------
# 目的:
#   1) display系関数へ interval=1 等の未知kwargsが渡っても壊れないようにする。
#   2) Discord SUMMARY TOP10 の理由を日本語で詳細化する。
#   3) PUSH由来で rank/chg/turn/tick が無い場合も、欠損理由を明示する。
#   4) 古い「結果時刻」のSUMMARYをDiscordへ送らない。
#
# V2.1:
#   - Discord送信直前に 結果時刻=YYYY-MM-DD HH:MM:SS を抽出。
#   - 市場時間中に古いSUMMARYを送信しない。
#   - 既定 stale 許容:
#       1分=4分 / 3分=8分 / 5分=12分
#     環境変数 SUMMARY_DISCORD_STALE_LIMIT_MIN で一括変更可。
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


# ============================================================
# kwargs safety
# ============================================================

def _is_df_like(v: Any) -> bool:
    try:
        import pandas as pd
        return isinstance(v, pd.DataFrame)
    except Exception:
        return False


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


def _wrap(fn: Callable) -> Callable:
    def _wrapped(summary_df=None, interval_label="1min", *, notify_discord=True, **kwargs):
        summary_df, interval_label, kwargs = _normalize(summary_df, interval_label, kwargs)
        return _safe_call(fn, summary_df, interval_label, notify_discord, kwargs)

    _wrapped._discord_kwarg_safety_patch = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


# ============================================================
# formatting helpers
# ============================================================

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


def _fmt_big(v: Any) -> str:
    if not _has_num(v):
        return "-"
    x = float(v)
    if abs(x) >= 100000000:
        return f"{x / 100000000:.2f}億"
    if abs(x) >= 10000:
        return f"{x / 10000:.1f}万"
    return f"{x:.0f}"


def _fmt_pct(v: Any, digits: int = 2) -> str:
    if not _has_num(v):
        return "-"
    return f"{float(v):.{digits}f}%"


# ============================================================
# stale guard
# ============================================================

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
        if str(os.getenv("SUMMARY_DISCORD_STALE_GUARD", "1")).strip().lower() in {"0", "false", "no", "off"}:
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
        if not callable(old) or getattr(old, "_summary_discord_stale_guard_v21", False):
            return 0

        def _send_guarded(lines: list[str], title: str | None = None) -> None:
            skip, reason = _should_skip_stale_discord(lines or [], title)
            if skip:
                logger.warning("[DISCORD SUMMARY STALE GUARD] skip old summary discord %s", reason)
                return None
            logger.info("[DISCORD SUMMARY STALE GUARD] allow summary discord %s", reason)
            return old(lines, title=title)

        _send_guarded._summary_discord_stale_guard_v21 = True  # type: ignore[attr-defined]
        _send_guarded._original = old  # type: ignore[attr-defined]
        disp._send_to_discord = _send_guarded
        return 1
    except Exception:
        logger.exception("[DISCORD SUMMARY STALE GUARD] install failed")
        return 0


# ============================================================
# Japanese rich reason / candidate line
# ============================================================

def _reason_ja(row: Any, side: str) -> str:
    side_u = str(side or "").upper()
    parts: list[str] = []

    buy = _f(_first(row, ("disp_buy_score", "score_buy", "buy_score"), 0.0))
    sell = _f(_first(row, ("disp_sell_score", "score_sell", "sell_score"), 0.0))
    slope = _f(_first(row, ("disp_slope", "slope", "score_slope", "slope_atr_scaled"), 0.0))
    mtf = _f(_first(row, ("disp_mtf", "mtf", "score_mtf", "mtf_score"), 0.0))
    base = _f(_first(row, ("disp_base", "score_base", "breakdown_base", "base_score", "base"), 0.0))
    trend = _f(_first(row, ("disp_trend", "score_trend", "breakdown_trend", "trend_score", "trend"), 0.0))
    mom = _f(_first(row, ("disp_mom", "score_momentum", "breakdown_mom", "score_mom", "momentum_score", "mom", "momentum"), 0.0))
    vel = _f(_first(row, ("disp_vel", "score_velocity", "breakdown_vel", "score_vel", "velocity_score", "vel", "velocity"), 0.0))
    pen = _f(_first(row, ("disp_pen", "score_penalty", "breakdown_pen", "score_pen", "penalty_score", "penalty", "pen"), 0.0))
    rsi = _f(_first(row, ("disp_rsi", "rsi"), 50.0), 50.0)
    macd = _f(_first(row, ("disp_macd", "macd"), 0.0))
    vwap_block = _f(_first(row, ("vwap_entry_block",), 0.0))

    if side_u == "BUY":
        if buy > 0:
            parts.append(f"買いスコア優勢 buy={buy:.2f}")
        parts.append(f"上向き傾き slope={slope:.4f}" if slope > 0 else f"傾きは弱い slope={slope:.4f}")
    else:
        if sell > 0:
            parts.append(f"売りスコア優勢 sell={sell:.2f}")
        parts.append(f"下向き傾き slope={slope:.4f}" if slope < 0 else f"下落傾きは弱い slope={slope:.4f}")

    if base:
        parts.append(f"基礎点={base:.2f}")
    if trend:
        parts.append(f"トレンド点={trend:.2f}")
    if mom:
        parts.append(f"勢い点={mom:.2f}")
    if vel:
        parts.append(f"速度点={vel:.2f}")
    if pen:
        parts.append(f"減点={pen:.2f}")
    if mtf:
        parts.append(f"複数時間足={mtf:.2f}")
    if rsi != 50.0:
        parts.append(f"RSI={rsi:.1f}")
    if macd:
        parts.append(f"MACD={macd:.3f}")
    if vwap_block > 0:
        parts.append("VWAP条件でブロック注意")

    code_reason = _clean(_first(row, ("reason", "entry_reason", "flag_reason", "signal_reason"), ""), max_len=40)
    if code_reason and code_reason not in {"-", "flag_score"}:
        parts.append(f"元理由={code_reason}")
    elif code_reason == "flag_score":
        parts.append("フラグスコア条件で抽出")

    return " / ".join(parts) if parts else "理由データ不足: スコア・傾き・補助指標から判定"


def _rich_candidate_line(i: int, row: Any, *, side: str) -> str:
    symbol = _clean(_first(row, ("symbol",), ""), max_len=8)
    name = _clean(_first(row, ("symbolname_view", "symbolname", "name"), ""), max_len=18)

    score = _first(row, ("disp_score", "score", "display_score", "final_score"), np.nan)
    buy = _first(row, ("disp_buy_score", "score_buy", "buy_score"), np.nan)
    sell = _first(row, ("disp_sell_score", "score_sell", "sell_score"), np.nan)
    total = _first(row, ("disp_total_score", "score_total", "total_score"), np.nan)
    final = _first(row, ("disp_final_score", "final_score", "display_score", "score"), np.nan)
    close = _first(row, ("disp_close", "close", "close_price", "current_price", "price"), np.nan)
    slope = _first(row, ("disp_slope", "slope", "score_slope", "slope_atr_scaled"), np.nan)
    mtf = _first(row, ("disp_mtf", "mtf", "score_mtf", "mtf_score"), np.nan)
    rsi = _first(row, ("disp_rsi", "rsi"), np.nan)
    macd = _first(row, ("disp_macd", "macd"), np.nan)
    signal = _first(row, ("disp_signal", "signal", "macd_signal"), np.nan)
    base = _first(row, ("disp_base", "score_base", "breakdown_base", "base_score", "base"), np.nan)
    trend = _first(row, ("disp_trend", "score_trend", "breakdown_trend", "trend_score", "trend"), np.nan)
    mom = _first(row, ("disp_mom", "score_momentum", "breakdown_mom", "score_mom", "momentum_score", "mom", "momentum"), np.nan)
    vel = _first(row, ("disp_vel", "score_velocity", "breakdown_vel", "score_vel", "velocity_score", "vel", "velocity"), np.nan)
    pen = _first(row, ("disp_pen", "score_penalty", "breakdown_pen", "score_pen", "penalty_score", "penalty", "pen"), np.nan)

    volume = _first(row, ("disp_volume", "volume", "Volume", "latest_volume", "_latest_volume"), np.nan)
    turnover = _first(row, ("disp_turnover", "turnover", "trading_value", "売買代金", "ranking_turnover"), np.nan)
    if not _has_num(turnover):
        c = _f(close, 0.0)
        v = _f(volume, 0.0)
        turnover = c * v if c > 0 and v > 0 else np.nan

    rank = _first(row, ("rank", "ranking_rank", "disp_rank", "Ranking", "順位"), "-")
    tick = _first(row, ("tick", "tick_count", "ticks", "disp_tick", "ranking_tick_count"), np.nan)
    chg = _first(row, ("change_rate", "chg", "ranking_change_rate", "disp_chg", "change_pct"), np.nan)
    pc1 = _first(row, ("price_change_pct_1m", "change_pct_1m", "change_rate_1m", "ret_1m"), np.nan)
    pc3 = _first(row, ("price_change_pct_3m", "change_pct_3m", "change_rate_3m", "ret_3m"), np.nan)
    pc5 = _first(row, ("price_change_pct_5m", "change_pct_5m", "change_rate_5m", "ret_5m"), np.nan)
    vs3 = _first(row, ("volume_surge_ratio_3m", "vol_surge_3m"), np.nan)
    vs5 = _first(row, ("volume_surge_ratio_5m", "vol_surge_5m"), np.nan)
    vsmax = _first(row, ("max_volume_surge_ratio", "_max_volume_surge_ratio"), np.nan)
    vwap = _first(row, ("vwap", "disp_vwap"), np.nan)
    above = _first(row, ("vwap_stable_above",), np.nan)
    below = _first(row, ("vwap_stable_below",), np.nan)
    block = _first(row, ("vwap_entry_block",), np.nan)

    missing = []
    if str(rank) == "-" and not _has_num(tick):
        missing.append("ランキング情報なし")
    if not _has_num(volume):
        missing.append("出来高なし")
    if not _has_num(pc1) and not _has_num(pc3) and not _has_num(pc5):
        missing.append("短期価格変化なし")
    if not _has_num(vs3) and not _has_num(vs5) and not _has_num(vsmax):
        missing.append("出来高急増率なし")

    miss_text = " / 欠損=" + ",".join(missing) if missing else ""
    reason = _reason_ja(row, side)
    mark = "🟦" if str(side).upper() == "BUY" else "🟥"

    return (
        f"{mark} {i}. {symbol} {name}\n"
        f"   株価={_fmt_price(close)} score={_fmt_metric(score)} buy={_fmt_metric(buy)} sell={_fmt_metric(sell)} total={_fmt_metric(total)} final={_fmt_metric(final)}\n"
        f"   slope={_fmt_metric(slope, 4)} mtf={_fmt_metric(mtf)} rsi={_fmt_metric(rsi)} macd={_fmt_metric(macd)} signal={_fmt_metric(signal)}\n"
        f"   内訳: base={_fmt_metric(base)} trend={_fmt_metric(trend)} mom={_fmt_metric(mom)} vel={_fmt_metric(vel)} pen={_fmt_metric(pen)}\n"
        f"   出来高={_fmt_big(volume)} 売買代金={_fmt_big(turnover)} rank={rank} tick={_fmt_big(tick)} chg={_fmt_pct(chg)} 1m={_fmt_pct(pc1)} 3m={_fmt_pct(pc3)} 5m={_fmt_pct(pc5)}\n"
        f"   出来高急増: 3m={_fmt_metric(vs3)}x 5m={_fmt_metric(vs5)}x max={_fmt_metric(vsmax)}x VWAP={_fmt_price(vwap)} above={_fmt_metric(above)} below={_fmt_metric(below)} block={_fmt_metric(block)}{miss_text}\n"
        f"   理由={reason}"
    )


def _install_rich_discord_builder(disp: Any) -> int:
    patched = 0
    try:
        old = getattr(disp, "_build_discord_candidate_2lines", None)
        if callable(old):
            def _candidate(i: int, row: Any, *, side: str) -> str:
                return _rich_candidate_line(i, row, side=side)
            _candidate._discord_rich_ja_reasons_v21 = True  # type: ignore[attr-defined]
            _candidate._original = old  # type: ignore[attr-defined]
            disp._build_discord_candidate_2lines = _candidate
            patched += 1

        old_reason = getattr(disp, "_reason_text_for_discord", None)
        def _reason(row: Any, side: str) -> str:
            return _reason_ja(row, side)
        _reason._discord_rich_ja_reasons_v21 = True  # type: ignore[attr-defined]
        _reason._original = old_reason  # type: ignore[attr-defined]
        disp._reason_text_for_discord = _reason
        patched += 1
    except Exception:
        logger.exception("[DISCORD KWARG SAFETY] rich discord builder install failed")
    return patched


# ============================================================
# install
# ============================================================

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

        rich_patched = _install_rich_discord_builder(disp)
        stale_patched = _install_stale_send_guard(disp)
        _PATCHED = True
        logger.warning(
            "[DISCORD KWARG SAFETY] installed V2.1 patched=%s rich_builder=%s stale_guard=%s",
            patched,
            rich_patched,
            stale_patched,
        )
        return True
    except Exception:
        logger.exception("[DISCORD KWARG SAFETY] install failed")
        return False


__all__ = ["install"]
