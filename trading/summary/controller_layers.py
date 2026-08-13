# -*- coding: utf-8 -*-
# ============================================================
# File   : trading/summary/controller_layers.py
# ------------------------------------------------------------
# 旧 core/startup の 3 パッチを移設した共有ヘルパ。
#   _nsg_* : summary_controller_neutral_score_guard_patch
#   _pmm_* : summary_controller_publish_mtf_merged_patch
#   _rmt_* : summary_controller_ready_mtf_patch
#
# 3 つとも同じ 5 関数
#   controller_cache.attach_display_ready
#   controller_projection.rebuild_display_ready / rebuild_technical_ready /
#                         latest_row_per_symbol / latest_row_per_symbol_mature_first
# を包んでいた。各モジュールの _ORIGINALS を実測した結果、合成は
#
#   native <- neutral_score_guard <- publish_mtf_merged <- ready_mtf
#
# の 3 層で、捨てられている層は無い (5 関数すべて同じ)。
# ホスト側はこの順序どおりに neutralize / publish / repair を呼ぶ。
#
# 旧 neutral_score_guard の _watcher_loop() (2 秒ごとの再 install) は、
# 差し替えるモジュール属性が無くなったため不要になり撤去した。
#
# 適用スコープ: 3 つとも main.py プロセスでのみ install されていた
# (sitecustomize BACKGROUND_MAIN_PATCHES / startup.py / startup_runtime.py)。
# scope_enabled() がそれを再現し、各層の DISABLE env も個別に見る。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import time
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)

_SCOPE: bool | None = None
_LAST_PUBLISH_AT: dict[str, float] = {}


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def scope_enabled() -> bool:
    """旧 3 パッチの install スコープ (main.py プロセス) の再現。"""
    global _SCOPE
    if _SCOPE is not None:
        return _SCOPE
    enabled = False
    try:
        argv_parts = [str(x).replace("\\", "/").lower() for x in sys.argv]
        argv = " ".join(argv_parts)
        if not any(n in argv for n in _DB_PROCESS_NAMES):
            enabled = any(a.endswith("/main.py") or a == "main.py" for a in argv_parts)
    except Exception:
        enabled = False
    _SCOPE = enabled
    return enabled



_NSG__FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


_NSG__TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


_NSG__WATCHER_STARTED = False


_NSG__INSTALLED = False


def _nsg_env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _NSG__TRUE_SET:
            return True
        if s in _NSG__FALSE_SET:
            return False
    except Exception:
        pass
    return bool(default)


def _nsg_env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _nsg_num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").replace([float("inf"), float("-inf")], pd.NA).fillna(default)


def _nsg_safe_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    try:
        return pd.DataFrame(x).copy()
    except Exception:
        return pd.DataFrame()


def _nsg_neutralize_summary_scores(df: Any, *, context: str = "") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if not _nsg_env_bool("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_ENABLED", True):
        return df
    out = _nsg_safe_df(df)
    try:
        score = _nsg_num(out, "score", 0.0)
        score_sell = _nsg_num(out, "score_sell", _nsg_num(out, "sell_score", 0.0))
        score_buy = _nsg_num(out, "score_buy", _nsg_num(out, "buy_score", 0.0))
        slope = _nsg_num(out, "slope", 0.0).abs()
        slope_atr = _nsg_num(out, "slope_atr_scaled", 0.0).abs()
        score_slope = _nsg_num(out, "score_slope", 0.0).abs()
        rsi = _nsg_num(out, "rsi", 50.0)
        macd = _nsg_num(out, "macd", 0.0).abs()
        signal = _nsg_num(out, "signal", 0.0).abs()
        hist_len = _nsg_num(out, "symbol_hist_len", 1.0)
        mtf = _nsg_num(out, "score_mtf", _nsg_num(out, "mtf", 0.0)).abs()

        neutral = (
            (slope <= 1e-12)
            & (slope_atr <= 1e-12)
            & (score_slope <= 1e-12)
            & ((rsi - 50.0).abs() <= 1e-9)
            & (macd <= 1e-12)
            & (signal <= 1e-12)
        )
        default_sell = (score < 0) & (score_sell > 0) & (score_buy <= 0)
        # symbol_hist_len が過去patchで3以上に補正されていても、中立指標なら抑制する。
        # ただし強いMTFだけは残したい場合があるため、既定ではmtf<2を抑制対象にする。
        max_mtf = _nsg_env_float("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_MAX_MTF", 2.0)
        mask = neutral & default_sell & ((hist_len < 5) | (mtf < max_mtf))
        n = int(mask.sum())
        if n <= 0:
            return out
        for col in ("score", "score_total", "total_score", "final_score", "display_score", "score_sell", "sell_score"):
            if col in out.columns:
                out.loc[mask, col] = 0.0
        for col in ("score_buy", "buy_score"):
            if col in out.columns:
                out.loc[mask, col] = 0.0
        out.loc[mask, "neutral_default_score_suppressed"] = True
        logger.warning(
            "[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] suppressed context=%s rows=%s suppressed=%s score_nonzero_after=%s sell_nonzero_after=%s",
            context,
            len(out),
            n,
            int((_nsg_num(out, "score", 0.0) != 0).sum()),
            int((_nsg_num(out, "score_sell", 0.0) != 0).sum()),
        )
    except Exception:
        logger.exception("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] failed context=%s", context)
        return df
    return out


_PMM__PATCHED = False


def _pmm_env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _pmm_env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return max(0.0, float(raw))
    except Exception:
        return float(default)


def _pmm_safe_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    try:
        return pd.DataFrame(x).copy()
    except Exception:
        return pd.DataFrame()


def _pmm_nz(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
    except Exception:
        return -1


def _pmm_coerce_interval(v: Any) -> int | None:
    try:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip().lower().replace("min", "").replace("m", "")
            if not s:
                return None
            iv = int(float(s))
        else:
            iv = int(float(v))
        if iv in (1, 3, 5):
            return iv
    except Exception:
        pass
    return None


def _pmm_infer_interval_from_call(args: tuple[Any, ...] | None = None, kwargs: dict[str, Any] | None = None) -> int | None:
    """関数引数から interval を推定する。

    controller系関数は戻り値DFに interval/source が無いことがあるため、
    kwargs の interval/tf/timeframe、または小さな整数引数を優先して使う。
    """
    try:
        kw = kwargs or {}
        for name in ("interval", "tf", "timeframe", "minutes", "minute_interval"):
            iv = _pmm_coerce_interval(kw.get(name))
            if iv in (1, 3, 5):
                return iv
    except Exception:
        pass
    try:
        for a in args or ():
            iv = _pmm_coerce_interval(a)
            if iv in (1, 3, 5):
                return iv
    except Exception:
        pass
    return None


def _pmm_infer_interval(df: pd.DataFrame, *, args: tuple[Any, ...] | None = None, kwargs: dict[str, Any] | None = None) -> int | None:
    # まず呼び出し引数を優先する。DFに interval/source が無い1分足対策。
    iv_call = _pmm_infer_interval_from_call(args, kwargs)
    if iv_call in (1, 3, 5):
        return iv_call

    try:
        if "interval" in df.columns:
            vals = pd.to_numeric(df["interval"], errors="coerce").dropna().astype(int)
            vals = vals[vals.isin([1, 3, 5])]
            if not vals.empty:
                return int(vals.mode().iloc[0])
    except Exception:
        pass
    try:
        if "source" in df.columns:
            s = " ".join(df["source"].astype(str).head(20).tolist()).lower()
            if "5min" in s or "5m" in s:
                return 5
            if "3min" in s or "3m" in s:
                return 3
            if "1min" in s or "1m" in s:
                return 1
    except Exception:
        pass
    return None


def _pmm_is_push_like(df: pd.DataFrame) -> bool:
    try:
        if "source" not in df.columns:
            # controller戻り値DFには source が無いことがある。
            # 本patchはPUSH系summary controllerの戻り値publish用なので、未知は通す。
            return True
        s = df["source"].astype(str).str.lower()
        return bool(s.str.contains("push", na=False).any())
    except Exception:
        return True


def _pmm_publish_if_repaired(
    df: Any,
    *,
    context: str,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> Any:
    if not _pmm_env_bool("SUMMARY_CONTROLLER_PUBLISH_MTF_MERGED_ENABLED", True):
        return df
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = _pmm_safe_df(df)
    if out.empty or not _pmm_is_push_like(out):
        return df

    interval = _pmm_infer_interval(out, args=args, kwargs=kwargs)
    if interval not in {1, 3, 5}:
        logger.debug(
            "[SUMMARY PUBLISH MTF MERGED] skip interval unknown context=%s rows=%s cols=%s",
            context,
            len(out),
            len(out.columns),
        )
        return df

    score_nz = max(_pmm_nz(out, "score_mtf"), _pmm_nz(out, "mtf"), _pmm_nz(out, "mtf_score"))
    if score_nz <= 0:
        return df

    key = f"{interval}:{context}"
    now = time.time()
    min_gap = _pmm_env_float("SUMMARY_CONTROLLER_PUBLISH_MTF_MERGED_MIN_GAP_SEC", 1.0)
    last = _LAST_PUBLISH_AT.get(key, 0.0)
    if now - last < min_gap:
        return df
    _LAST_PUBLISH_AT[key] = now

    try:
        from global_state import global_data
        # 重複列を落としてから、PUSH merged latest に載せる。
        clean = out.loc[:, ~out.columns.duplicated()].copy()
        # interval列が無いDFは、後続の判定でも迷わないよう補完する。
        if "interval" not in clean.columns:
            clean["interval"] = int(interval)
        global_data.set_push_merged_summary(int(interval), clean)
        logger.warning(
            "[SUMMARY PUBLISH MTF MERGED] published context=%s interval=%s rows=%s cols=%s score_mtf=%s mtf=%s mtf_score=%s latest_dt=%s inferred_from_call=%s",
            context,
            interval,
            len(clean),
            len(clean.columns),
            _pmm_nz(clean, "score_mtf"),
            _pmm_nz(clean, "mtf"),
            _pmm_nz(clean, "mtf_score"),
            str(pd.to_datetime(clean["datetime"], errors="coerce").max()) if "datetime" in clean.columns else None,
            _pmm_infer_interval_from_call(args, kwargs),
        )
    except Exception:
        logger.exception("[SUMMARY PUBLISH MTF MERGED] publish failed context=%s interval=%s", context, interval)
    return df


_RMT__PATCHED = False


def _rmt_env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _rmt_safe_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    try:
        return pd.DataFrame(x).copy()
    except Exception:
        return pd.DataFrame()


def _rmt_nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
    except Exception:
        return -1


def _rmt_bool_count(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int(pd.Series(df[col]).fillna(False).astype(bool).sum())
    except Exception:
        return -1


def _rmt_normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = _rmt_safe_df(df)
    if out.empty:
        return out
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    return out


def _rmt_merge_daily_mtf_early(df: pd.DataFrame, *, context: str) -> pd.DataFrame:
    out = _rmt_normalize_symbol(df)
    if out.empty or "symbol" not in out.columns:
        return out
    if not _rmt_env_bool("SUMMARY_CONTROLLER_EARLY_DAILY_MTF", True):
        return out
    try:
        from trading.summary.mtf.daily_runtime_patch import merge_daily_mtf_for_ai

        before = _rmt_nonzero_count(out, "score_mtf")
        merged = merge_daily_mtf_for_ai(out, source=f"SUMMARY_CONTROLLER_EARLY-{context}")
        out2 = _rmt_normalize_symbol(merged)
        logger.warning(
            "[SUMMARY CONTROLLER READY MTF PATCH] daily merge context=%s rows=%s score_mtf %s->%s mtf=%s mtf_score=%s",
            context,
            len(out2),
            before,
            _rmt_nonzero_count(out2, "score_mtf"),
            _rmt_nonzero_count(out2, "mtf"),
            _rmt_nonzero_count(out2, "mtf_score"),
        )
        return out2
    except Exception:
        logger.exception("[SUMMARY CONTROLLER READY MTF PATCH] daily mtf merge failed context=%s", context)
        return out


def _rmt_align_mtf_aliases(df: pd.DataFrame, *, context: str) -> pd.DataFrame:
    out = _rmt_safe_df(df)
    if out.empty:
        return out
    try:
        for col in ("mtf", "score_mtf", "mtf_score"):
            if col not in out.columns:
                out[col] = 0.0

        s_mtf = pd.to_numeric(out["mtf"], errors="coerce")
        s_score = pd.to_numeric(out["score_mtf"], errors="coerce")
        s_mtf_score = pd.to_numeric(out["mtf_score"], errors="coerce")

        # score_mtf が入っていて mtf/mtf_score が 0 の場合は横展開
        score_valid = s_score.fillna(0) != 0
        mtf_zero = s_mtf.fillna(0) == 0
        mtf_score_zero = s_mtf_score.fillna(0) == 0
        out.loc[score_valid & mtf_zero, "mtf"] = s_score.loc[score_valid & mtf_zero]
        out.loc[score_valid & mtf_score_zero, "mtf_score"] = s_score.loc[score_valid & mtf_score_zero]

        # mtf 側だけ入っている場合も score_mtf/mtf_score へ横展開
        s_mtf = pd.to_numeric(out["mtf"], errors="coerce")
        mtf_valid = s_mtf.fillna(0) != 0
        s_score = pd.to_numeric(out["score_mtf"], errors="coerce")
        s_mtf_score = pd.to_numeric(out["mtf_score"], errors="coerce")
        out.loc[mtf_valid & (s_score.fillna(0) == 0), "score_mtf"] = s_mtf.loc[mtf_valid & (s_score.fillna(0) == 0)]
        out.loc[mtf_valid & (s_mtf_score.fillna(0) == 0), "mtf_score"] = s_mtf.loc[mtf_valid & (s_mtf_score.fillna(0) == 0)]

        logger.warning(
            "[SUMMARY CONTROLLER READY MTF PATCH] alias context=%s rows=%s mtf=%s score_mtf=%s mtf_score=%s",
            context,
            len(out),
            _rmt_nonzero_count(out, "mtf"),
            _rmt_nonzero_count(out, "score_mtf"),
            _rmt_nonzero_count(out, "mtf_score"),
        )
    except Exception:
        logger.exception("[SUMMARY CONTROLLER READY MTF PATCH] align mtf alias failed context=%s", context)
    return out


def _rmt_relax_technical_ready(df: pd.DataFrame, *, context: str) -> pd.DataFrame:
    out = _rmt_safe_df(df)
    if out.empty:
        return out
    try:
        idx = out.index
        if "technical_ready" not in out.columns:
            out["technical_ready"] = False

        symbol_ok = out["symbol"].fillna("").astype(str).str.strip().ne("") if "symbol" in out.columns else pd.Series(False, index=idx)
        close_ok = pd.Series(False, index=idx)
        for col in ("close", "close_price", "price", "current_price"):
            if col in out.columns:
                s = pd.to_numeric(out[col], errors="coerce")
                close_ok = close_ok | (s.notna() & s.fillna(0).ne(0))

        macd_ok = pd.Series(False, index=idx)
        if "macd" in out.columns:
            macd_ok = macd_ok | pd.to_numeric(out["macd"], errors="coerce").fillna(0).ne(0)
        if "signal" in out.columns:
            macd_ok = macd_ok | pd.to_numeric(out["signal"], errors="coerce").fillna(0).ne(0)

        slope_ok = pd.Series(False, index=idx)
        for col in ("slope", "slope_atr_scaled", "score_slope"):
            if col in out.columns:
                slope_ok = slope_ok | pd.to_numeric(out[col], errors="coerce").fillna(0).ne(0)

        mtf_ok = pd.Series(False, index=idx)
        for col in ("mtf", "score_mtf", "mtf_score"):
            if col in out.columns:
                mtf_ok = mtf_ok | pd.to_numeric(out[col], errors="coerce").fillna(0).ne(0)

        rsi_ok = pd.Series(False, index=idx)
        if "rsi" in out.columns:
            rsi = pd.to_numeric(out["rsi"], errors="coerce")
            # rsi=50 は初期値扱い。0/100 はシグナルとして扱う。
            rsi_ok = rsi.notna() & rsi.ne(50)

        ready = (symbol_ok & close_ok & (macd_ok | slope_ok | mtf_ok | rsi_ok)).fillna(False)
        before = _rmt_bool_count(out, "technical_ready")
        old = pd.Series(out["technical_ready"]).fillna(False).astype(bool)
        out["technical_ready"] = (old | ready).fillna(False).astype(bool)

        if "symbol_hist_len" not in out.columns:
            out["symbol_hist_len"] = pd.NA
        hist = pd.to_numeric(out["symbol_hist_len"], errors="coerce")
        fix_mask = out["technical_ready"].fillna(False).astype(bool) & (hist.fillna(0) < 3)
        out.loc[fix_mask, "symbol_hist_len"] = 3

        after = _rmt_bool_count(out, "technical_ready")
        logger.warning(
            "[SUMMARY CONTROLLER READY MTF PATCH] ready context=%s rows=%s ready %s->%s macd=%s signal=%s slope=%s mtf=%s score_mtf=%s rsi_non50=%s",
            context,
            len(out),
            before,
            after,
            _rmt_nonzero_count(out, "macd"),
            _rmt_nonzero_count(out, "signal"),
            _rmt_nonzero_count(out, "slope"),
            _rmt_nonzero_count(out, "mtf"),
            _rmt_nonzero_count(out, "score_mtf"),
            int(rsi_ok.sum()) if isinstance(rsi_ok, pd.Series) else -1,
        )
    except Exception:
        logger.exception("[SUMMARY CONTROLLER READY MTF PATCH] relax technical_ready failed context=%s", context)
    return out


def _rmt_repair(df: Any, *, context: str) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = _rmt_merge_daily_mtf_early(df, context=context)
    out = _rmt_align_mtf_aliases(out, context=context)
    out = _rmt_relax_technical_ready(out, context=context)
    return out


# ------------------------------------------------------------
# ホストから呼ぶ 3 つの層。呼び出し順は
#   ready(pre) -> neutral(pre) -> native -> neutral(post) -> publish -> ready(post)
# ------------------------------------------------------------


def neutral_enabled() -> bool:
    if str(os.environ.get("DISABLE_SUMMARY_MTF_EARLY_READY_PATCH", "")).strip() == "1":
        return False
    return scope_enabled() and _nsg_env_bool("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_ENABLED", True)


def publish_enabled() -> bool:
    return scope_enabled()


def ready_enabled() -> bool:
    return scope_enabled()


def neutralize(df: Any, *, context: str) -> Any:
    if not neutral_enabled():
        return df
    return _nsg_neutralize_summary_scores(df, context=context)


def publish_if_repaired(ret: Any, *, context: str, args, kwargs) -> Any:
    if not publish_enabled():
        return ret
    return _pmm_publish_if_repaired(ret, context=context, args=args, kwargs=kwargs)


def repair(df: Any, *, context: str) -> Any:
    if not ready_enabled():
        return df
    return _rmt_repair(df, context=context)


def apply_layers(fn_core, name: str, key: str, args, kwargs):
    """旧 3 ラッパーの合成をそのまま再現する。"""
    if name == "rebuild_technical_ready" and args and isinstance(args[0], pd.DataFrame):
        args = (repair(args[0], context=f"pre:{name}"),) + tuple(args[1:])
    new_args = args
    if args and isinstance(args[0], pd.DataFrame):
        new_args = (neutralize(args[0], context=f"pre:{key}"),) + tuple(args[1:])
    ret = fn_core(*new_args, **kwargs)
    if isinstance(ret, pd.DataFrame):
        ret = neutralize(ret, context=f"post:{key}")
    ret = publish_if_repaired(ret, context=name, args=args, kwargs=kwargs)
    if isinstance(ret, pd.DataFrame):
        ret = repair(ret, context=f"post:{name}")
    return ret
