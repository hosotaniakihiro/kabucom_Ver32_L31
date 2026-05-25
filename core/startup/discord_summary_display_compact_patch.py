# ============================================================
# File   : core/startup/discord_summary_display_compact_patch.py
# Version: V1.4-COMPACT-DISCORD-SUMMARY-KWARGS-GUARD
# ------------------------------------------------------------
# 目的:
#   Discordへ送信されるサマリー表示が横長・桁揃え・日本語銘柄名で崩れる問題を補正する。
#
# 方針:
#   - Discord専用表示は「縦リスト・短い2行」に統一する。
#   - 日本語銘柄名の桁揃えをやめる。
#   - PUSH由来サマリーは 1分 / 3分 / 5分 を必ずDiscord送信する。
#   - ランキング由来サマリーの1分抑止は従来通り環境変数で制御する。
#   - ENTRY / EXIT 通知や重要アラートは対象外。
#   - コンソール表示は既存のまま変更しない。
#
# V1.2:
#   - PUSH由来 SUMMARY 1min は環境変数に関係なく送信する
#   - 旧wrapperが残っていても _original をたどって必ず差し替える
#   - 起動ログに force_summary_1min=True を出す
#
# V1.3:
#   - compact patch の wrapper 自体で interval_label を安全化する。
#   - 呼び出し元が display_xxx(df, df) / print_xxx(df, df) をしても、
#     DataFrame本体をタイトルへ表示しない。
#   - 呼び出し元が display_xxx(label, df) の順で渡しても、DataFrameを捨てずに入れ替える。
#
# V1.4:
#   - display_runner 側から渡る interval / source / slot 等の内部 kwargs を、
#     元の print_summary_top10 / print_ranking_summary_top10 へ渡さない。
#   - 「unexpected keyword argument 'interval'」でPUSH表示が落ちる問題を修正。
# ============================================================

from __future__ import annotations

import logging
import os
import re
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)
_PATCHED = False

_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}

# display_runner / safe_io / scheduler 側の内部制御用引数。
# 元の表示関数が受け取れない可能性が高いため、base関数へ渡す直前で必ず除去する。
_BASE_UNSUPPORTED_KWARGS = {
    "interval",
    "interval_min",
    "minutes",
    "source",
    "source_label",
    "market",
    "now",
    "slot",
    "save_reason",
    "display_reason",
    "reason",
    "origin",
    "runner",
    "job_name",
}


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _send_summary_1min_enabled() -> bool:
    # PUSH由来は 1分/3分/5分を必ず見たいので、1分も常に送る。
    return True


def _send_ranking_summary_1min_enabled() -> bool:
    # ランキング由来は従来互換として既定では1分抑止。
    return _env_bool("RANKING_SUMMARY_DISCORD_SEND_1MIN", False)


def _is_1min_label(label: Any) -> bool:
    s = str(label or "").strip().lower().replace(" ", "")
    return s in {"1min", "1m", "1分", "1分足", "1"}


def _is_df_like(v: Any) -> bool:
    try:
        import pandas as pd
        return isinstance(v, pd.DataFrame)
    except Exception:
        return False


def _is_series_like(v: Any) -> bool:
    try:
        import pandas as pd
        return isinstance(v, pd.Series)
    except Exception:
        return False


def _looks_bad_label(v: Any) -> bool:
    """タイトルに出してはいけない値を検出する。"""
    try:
        if v is None:
            return False
        if _is_df_like(v) or _is_series_like(v):
            return True
        if isinstance(v, (dict, list, tuple, set)):
            return True
        s = str(v)
        if " rows x " in s and "columns" in s:
            return True
        if "[" in s and " rows x " in s:
            return True
        if "symbol" in s and "columns" in s:
            return True
        if "vwap_entry_block" in s and "symbol" in s:
            return True
        if "\n" in s and len(s) > 40:
            return True
        if len(s) > 80:
            return True
        return False
    except Exception:
        return True


def _safe_interval_label(label: Any, *, kwargs: dict[str, Any] | None = None, default: str = "1min") -> str:
    try:
        if label is not None and not _looks_bad_label(label):
            s = str(label).strip()
            if s:
                return s
        kwargs = kwargs or {}
        interval = kwargs.get("interval") or kwargs.get("interval_min") or kwargs.get("minutes")
        if interval is not None and not _looks_bad_label(interval):
            try:
                return f"{int(float(interval))}min"
            except Exception:
                s = str(interval).strip()
                if s:
                    return s
    except Exception:
        pass
    return default


def _base_safe_kwargs(kwargs: dict[str, Any] | None) -> dict[str, Any]:
    """
    元の print_summary_top10 系へ渡してよい kwargs だけに近づける。

    display_runner 側は interval=1 などの内部情報を渡すことがあるが、
    元関数はそれを受け取れないため TypeError になる。
    interval_label の復元には _normalize_summary_call 側で kwargs を参照済みなので、
    base関数へ渡す直前では削除してよい。
    """
    safe = dict(kwargs or {})
    removed: dict[str, Any] = {}
    for k in list(safe.keys()):
        if k in _BASE_UNSUPPORTED_KWARGS:
            removed[k] = safe.pop(k, None)
    if removed:
        try:
            logger.debug("[DISCORD SUMMARY COMPACT] stripped unsupported kwargs for base: %s", sorted(removed.keys()))
        except Exception:
            pass
    return safe


def _normalize_summary_call(summary_df: Any, interval_label: Any, kwargs: dict[str, Any]) -> tuple[Any, str]:
    """
    compact patch wrapper入口の最終防衛。

    想定事故:
      1) print_summary_top10(df, df)
         -> interval_label を 1min に戻す
      2) print_summary_top10("1min", df)
         -> (df, "1min") に入れ替える
      3) print_summary_top10(summary_df=df, interval_label=df)
         -> interval_label を 1min に戻す
    """
    try:
        # display_xxx(label, df) 型の順序ミスは、DataFrameを本体として救済する。
        if _is_df_like(interval_label) and not _is_df_like(summary_df):
            fixed_label = _safe_interval_label(summary_df, kwargs=kwargs, default="1min")
            logger.warning(
                "[DISCORD SUMMARY COMPACT] swapped bad positional call summary_df_type=%s interval_label_type=%s fixed_label=%s rows=%s",
                type(summary_df).__name__,
                type(interval_label).__name__,
                fixed_label,
                len(interval_label) if hasattr(interval_label, "__len__") else "-",
            )
            return interval_label, fixed_label

        fixed_label = _safe_interval_label(interval_label, kwargs=kwargs, default="1min")
        if fixed_label != interval_label:
            logger.warning(
                "[DISCORD SUMMARY COMPACT] fixed bad interval_label old_type=%s new=%s",
                type(interval_label).__name__,
                fixed_label,
            )
        return summary_df, fixed_label
    except Exception:
        logger.exception("[DISCORD SUMMARY COMPACT] normalize summary call failed")
        return summary_df, "1min"


def _clean_text(v: Any, *, max_len: int = 28) -> str:
    try:
        s = str(v if v is not None else "").replace("\r", " ").replace("\n", " ").strip()
        s = re.sub(r"\s+", " ", s)
        if len(s) > max_len:
            return s[:max_len - 1] + "…"
        return s
    except Exception:
        return ""


def _first(disp: Any, row: Any, names: list[str], default: Any = "-") -> Any:
    try:
        return disp.first_existing(row, names, default)
    except Exception:
        return default


def _fmt_num(disp: Any, v: Any) -> str:
    try:
        if v == "-" or v is None:
            return "-"
        return disp.fmt_metric(v)
    except Exception:
        try:
            x = float(v)
            if not np.isfinite(x):
                return "-"
            return f"{x:.2f}"
        except Exception:
            return _clean_text(v, max_len=12) or "-"


def _fmt_price(disp: Any, v: Any) -> str:
    try:
        if v == "-" or v is None:
            return "-"
        return disp.fmt_price(v)
    except Exception:
        return _fmt_num(disp, v)


def _reason_for_discord(disp: Any, row: Any, side: str) -> str:
    try:
        raw = disp._reason_text_for_discord(row, side)
        return _clean_text(raw or "-", max_len=70) or "-"
    except Exception:
        return "-"


def _build_candidate_compact(disp: Any, i: int, row: Any, *, side: str) -> str:
    symbol = _clean_text(_first(disp, row, ["symbol"], ""), max_len=8)
    name = _clean_text(_first(disp, row, ["symbolname_view", "symbolname", "name"], ""), max_len=18)

    score = _fmt_num(disp, _first(disp, row, ["disp_score", "score", "display_score", "final_score"], np.nan))
    buy = _fmt_num(disp, _first(disp, row, ["disp_buy_score", "score_buy", "buy_score"], np.nan))
    sell = _fmt_num(disp, _first(disp, row, ["disp_sell_score", "score_sell", "sell_score"], np.nan))
    close = _fmt_price(disp, _first(disp, row, ["disp_close", "close", "close_price", "current_price", "price"], np.nan))
    slope = _fmt_num(disp, _first(disp, row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], np.nan))
    mtf = _fmt_num(disp, _first(disp, row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], np.nan))
    rsi = _fmt_num(disp, _first(disp, row, ["disp_rsi", "rsi"], np.nan))
    macd = _fmt_num(disp, _first(disp, row, ["disp_macd", "macd"], np.nan))
    reason = _reason_for_discord(disp, row, side)

    mark = "🟦" if str(side).upper() == "BUY" else "🟥"
    return (
        f"{mark} {i}. {symbol} {name}\n"
        f"   株価={close} score={score} buy={buy} sell={sell} slope={slope} mtf={mtf} rsi={rsi} macd={macd}\n"
        f"   理由={reason}"
    )


def _build_ai_candidate_compact(disp: Any, i: int, row: Any, *, side: str) -> str:
    symbol = _clean_text(_first(disp, row, ["symbol"], ""), max_len=8)
    name = _clean_text(_first(disp, row, ["symbolname_view", "symbolname", "name"], ""), max_len=18)
    conf = _first(disp, row, ["confidence", "conf", "ai_confidence"], np.nan)
    try:
        conf_text = disp.fmt_confidence(conf)
    except Exception:
        conf_text = _fmt_num(disp, conf)
    lot = _fmt_num(disp, _first(disp, row, ["lot", "order_lot", "qty", "lot_multiplier"], np.nan))
    close = _fmt_price(disp, _first(disp, row, ["disp_close", "close", "close_price", "current_price", "price"], np.nan))
    score = _fmt_num(disp, _first(disp, row, ["disp_score", "score", "display_score", "final_score"], np.nan))
    buy = _fmt_num(disp, _first(disp, row, ["disp_buy_score", "score_buy", "buy_score"], np.nan))
    sell = _fmt_num(disp, _first(disp, row, ["disp_sell_score", "score_sell", "sell_score"], np.nan))
    reason = _clean_text(_first(disp, row, ["ai_reason", "reason", "gate_reason"], "") or _reason_for_discord(disp, row, side), max_len=80)
    mark = "🤖🟦" if str(side).upper() == "BUY" else "🤖🟥"
    return (
        f"{mark} {i}. {symbol} {name}\n"
        f"   conf={conf_text} lot={lot} 株価={close} score={score} buy={buy} sell={sell}\n"
        f"   理由={reason or '-'}"
    )


def _send_to_discord_compact(disp: Any, lines: list[str], title: str | None = None) -> None:
    try:
        if not callable(getattr(disp, "send_discord_message", None)):
            logger.info("[DISCORD SUMMARY COMPACT] sender not available")
            return
        cleaned = [_clean_text(x, max_len=900) for x in lines if x is not None and str(x).strip() != ""]
        if not cleaned:
            return
        header = _clean_text(title or "", max_len=120)
        text = (header + "\n" if header else "") + "\n".join(cleaned)
        text = text.strip()
        if not text:
            return

        limit = 1800
        chunks: list[str] = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            cut = text.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()

        for idx, chunk in enumerate(chunks, start=1):
            ok = disp.send_discord_message(content=chunk)
            logger.info("[DISCORD SUMMARY COMPACT] sent chunk=%s/%s ok=%s chars=%s", idx, len(chunks), ok, len(chunk))
    except Exception:
        logger.exception("[DISCORD SUMMARY COMPACT] send failed")


def _unwrap(fn: Any) -> Any:
    """旧patch wrapperが残っている場合、元関数まで戻してから新wrapperをかける。"""
    try:
        seen = set()
        cur = fn
        while callable(getattr(cur, "_original", None)) and id(cur) not in seen:
            seen.add(id(cur))
            cur = getattr(cur, "_original")
        return cur
    except Exception:
        return fn


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import scheduler_jobs.summary.display as disp
    except Exception:
        logger.exception("[DISCORD SUMMARY COMPACT] display import failed")
        return False

    try:
        # 念のため環境変数にも明示して、他patchが参照しても1分PUSHを落とさない。
        os.environ["SUMMARY_DISCORD_SEND_1MIN"] = "1"

        old_send = getattr(disp, "_send_to_discord", None)
        base_send = _unwrap(old_send)
        if callable(base_send):
            def _send_to_discord_patched(lines: list[str], title: str | None = None) -> None:
                return _send_to_discord_compact(disp, lines, title)

            _send_to_discord_patched._discord_compact_patch = True  # type: ignore[attr-defined]
            _send_to_discord_patched._original = base_send  # type: ignore[attr-defined]
            disp._send_to_discord = _send_to_discord_patched

        def _candidate(i: int, row: Any, *, side: str) -> str:
            return _build_candidate_compact(disp, i, row, side=side)

        def _ai_candidate(i: int, row: Any, *, side: str) -> str:
            return _build_ai_candidate_compact(disp, i, row, side=side)

        disp._build_discord_candidate_2lines = _candidate
        disp._build_discord_ai_candidate_2lines = _ai_candidate

        old_print_summary = getattr(disp, "print_summary_top10", None)
        base_print_summary = _unwrap(old_print_summary)
        if callable(base_print_summary):
            def _print_summary_top10_patched(summary_df, interval_label="1min", *, notify_discord=True, **kwargs):
                summary_df, interval_label = _normalize_summary_call(summary_df, interval_label, kwargs)
                safe_kwargs = _base_safe_kwargs(kwargs)
                # PUSH由来SUMMARYは1分も必ず送る。ここでは抑止しない。
                if notify_discord and _is_1min_label(interval_label):
                    logger.info("[DISCORD SUMMARY COMPACT] allow 1min SUMMARY discord interval=%s", interval_label)
                return base_print_summary(
                    summary_df,
                    interval_label=interval_label,
                    notify_discord=notify_discord,
                    **safe_kwargs,
                )

            _print_summary_top10_patched._discord_1min_force_patch = True  # type: ignore[attr-defined]
            _print_summary_top10_patched._summary_display_label_guard_v14 = True  # type: ignore[attr-defined]
            _print_summary_top10_patched._original = base_print_summary  # type: ignore[attr-defined]
            disp.print_summary_top10 = _print_summary_top10_patched

        old_print_ranking = getattr(disp, "print_ranking_summary_top10", None)
        base_print_ranking = _unwrap(old_print_ranking)
        if callable(base_print_ranking):
            def _print_ranking_summary_top10_patched(summary_df, interval_label="1min", *, notify_discord=True, **kwargs):
                summary_df, interval_label = _normalize_summary_call(summary_df, interval_label, kwargs)
                safe_kwargs = _base_safe_kwargs(kwargs)
                if notify_discord and _is_1min_label(interval_label) and not _send_ranking_summary_1min_enabled():
                    logger.info("[DISCORD SUMMARY COMPACT] suppress 1min RANKING SUMMARY discord interval=%s", interval_label)
                    notify_discord = False
                return base_print_ranking(
                    summary_df,
                    interval_label=interval_label,
                    notify_discord=notify_discord,
                    **safe_kwargs,
                )

            _print_ranking_summary_top10_patched._discord_1min_suppress_patch = True  # type: ignore[attr-defined]
            _print_ranking_summary_top10_patched._summary_display_label_guard_v14 = True  # type: ignore[attr-defined]
            _print_ranking_summary_top10_patched._original = base_print_ranking  # type: ignore[attr-defined]
            disp.print_ranking_summary_top10 = _print_ranking_summary_top10_patched

        # display_summary / display_push_summary / display_ranking_summary が
        # 旧関数参照を保持していても、入口で同じ正規化をかける。
        for attr, target_name in (
            ("display_summary", "print_summary_top10"),
            ("display_push_summary", "print_summary_top10"),
            ("print_push_summary", "print_summary_top10"),
            ("display_ranking_summary", "print_ranking_summary_top10"),
            ("print_ranking_summary", "print_ranking_summary_top10"),
            ("display_ai_passed_summary", None),
        ):
            old = getattr(disp, attr, None)
            if not callable(old):
                continue
            base_old = _unwrap(old)

            if target_name is None:
                def _make_ai_wrapper(fn):
                    def _wrapped(summary_df=None, interval_label="1min", *, notify_discord=True, **kwargs):
                        summary_df, interval_label = _normalize_summary_call(summary_df, interval_label, kwargs)
                        safe_kwargs = _base_safe_kwargs(kwargs)
                        return fn(
                            summary_df=summary_df,
                            interval_label=interval_label,
                            notify_discord=notify_discord,
                            **safe_kwargs,
                        )
                    _wrapped._summary_display_label_guard_v14 = True  # type: ignore[attr-defined]
                    _wrapped._original = fn  # type: ignore[attr-defined]
                    return _wrapped
                setattr(disp, attr, _make_ai_wrapper(base_old))
            else:
                def _make_display_wrapper(target_attr: str):
                    def _wrapped(summary_df=None, interval_label="1min", *, notify_discord=True, **kwargs):
                        summary_df, interval_label = _normalize_summary_call(summary_df, interval_label, kwargs)
                        safe_kwargs = _base_safe_kwargs(kwargs)
                        fn = getattr(disp, target_attr)
                        return fn(
                            summary_df,
                            interval_label=interval_label,
                            notify_discord=notify_discord,
                            **safe_kwargs,
                        )
                    _wrapped._summary_display_label_guard_v14 = True  # type: ignore[attr-defined]
                    _wrapped._original = base_old  # type: ignore[attr-defined]
                    return _wrapped
                setattr(disp, attr, _make_display_wrapper(target_name))

        _PATCHED = True
        logger.warning(
            "[DISCORD SUMMARY COMPACT] installed compact_display=True label_guard=V1.4 kwargs_guard=True force_summary_1min=True suppress_summary_1min=False suppress_ranking_1min=%s send_summary_1min=True send_ranking_1min=%s",
            not _send_ranking_summary_1min_enabled(),
            _send_ranking_summary_1min_enabled(),
        )
        return True
    except Exception:
        logger.exception("[DISCORD SUMMARY COMPACT] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[DISCORD SUMMARY COMPACT] auto install failed")


__all__ = ["install"]
