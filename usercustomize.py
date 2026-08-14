# -*- coding: utf-8 -*-
# ============================================================
# File   : usercustomize.py
# 機能   :
#   Python起動時に自動実行されるユーザー起動パッチ。
#
#   主な役割:
#   1. main.py 起動時の runtime defaults を復元する
#   2. Summary-AI / Ranking / Tonosama / PUSH rotation 関連の補助パッチを自動installする
#   3. 板なしエントリーを禁止する安全設定を固定する
#   4. Tonosama通知の直近出来高表示を補正する
#   5. PUSHローテーション時の監視銘柄除外を防ぐ
#   6. Summary-AI の AI_OK 後に、5分以内の利確到達確率・損切到達確率・期待値を確認する
#      実績確率ゲート summary_ai_emp_prob_gate_patch を自動installする
#
# 注意:
#   - 板なしエントリーは許可しない
#   - 実績確率ゲートは AI が出した confidence を確率として扱わず、
#     summary DB の1分足履歴から実測ベースで判定する
# ============================================================

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _env_default(name: str, value: str) -> None:
    try:
        if os.getenv(name) is None or str(os.getenv(name)).strip() == "":
            os.environ[name] = str(value)
    except Exception:
        pass


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _is_database_collector_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(
            x in argv
            for x in (
                "main_database.py",
                "db_prepare_runner.py",
                "ranking_collector_runner.py",
                "push_receiver_runner.py",
                "yahoo_complement_runner.py",
                "summary_database_runner.py",
                "data_collectors_runner.py",
            )
        ):
            return True
        return any(
            os.getenv(k) == "1"
            for k in (
                "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
                "AUTOSTOCK_MAIN_DATABASE_PROCESS",
                "AUTOSTOCK_SUMMARY_DB_WRITER",
                "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
            )
        )
    except Exception:
        return False


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and not _is_database_collector_context()
    except Exception:
        return False


def _patch_entry_order_builder_logger() -> bool:
    try:
        from trading.handlers import entry_order_builder as eob

        cur = getattr(eob, "logger", None)
        if cur is None or not hasattr(cur, "warning") or not hasattr(cur, "info"):
            eob.logger = logging.getLogger("trading.handlers.entry_order_builder")
            logger.warning("[USERCUSTOMIZE] entry_order_builder.logger patched version=V1-LOGGER-HOTFIX")
            return True

        logger.warning("[USERCUSTOMIZE] entry_order_builder.logger already ok version=V1-LOGGER-HOTFIX")
        return True
    except Exception:
        logger.exception("[USERCUSTOMIZE] entry_order_builder.logger patch failed version=V1-LOGGER-HOTFIX")
        return False


def _install_summary_ai_fast_order_builder() -> bool:
    try:
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_SEC", "0.8")
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.2")

        # summary_ai_fast_order_builder_patch は
        # trading/handlers/entry_order_builder.py へ本文化済み (_fob_*)。
        # env / 定数の適用は同ファイルの _fob_apply_env_and_constants() が
        # import 時に行うので、ここでは import して走らせるだけでよい。
        # 旧 install が連鎖していた 2 つはここから直接呼ぶ。
        import trading.handlers.entry_order_builder as _eob  # noqa: F401

        # summary_ai_strict_board_rest_patch は eob へ本文化済み
        # (_sbr_* / get_latest_bid_ask / _get_board_with_retry)。
        # 板 wrapper の設置は不要になったので、残る env・定数の強制
        # (と連鎖していた board missing defer の import) だけを呼ぶ。
        # breakout_support_resistance_guard_patch も本文化済み:
        #   trading/handlers/entry_order_builder.build_entry_order (_bsr_*)
        #   trading/entry/summary_ai/executor の
        #     _base_filter_blocked_ai_ok_items / _sort_key
        ok = True
        try:
            _r = bool(_eob.apply_strict_board_rest_policy())
            logger.warning("[USERCUSTOMIZE] strict board policy chain installed=%s", _r)
        except Exception:
            ok = False
            logger.exception("[USERCUSTOMIZE] strict board policy chain install failed")
        logger.warning(
            "[USERCUSTOMIZE] summary AI fast order builder ok=%s retry_sec=%s retry_interval=%s",
            ok,
            os.getenv("ENTRY_ORDER_BOARD_RETRY_SEC"),
            os.getenv("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"),
        )
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] summary AI fast order builder failed")
        return False


def _install_exit_no_progress_monitor() -> bool:
    """建玉が5分で利確/損切の70%水準に届かない場合の返済監視を起動する。

    旧 core/startup/summary_ai_ranking_prefilter_signal_patch.install() が
    連鎖起動していたもの。prefilter 本体は
    trading/entry/summary_ai/runner.py へ本文化済みのため、ここでは
    監視スレッドの起動だけを行う。
    (呼び出し元の _uc_delayed_watchlist_patch_loop が毎秒呼び直すため、
     旧パッチの watcher スレッドと同じリトライ性を保つ)
    """
    try:
        from trading.exit.no_progress_monitor import install as _install_exit_monitor

        ok = bool(_install_exit_monitor())
        logger.warning("[USERCUSTOMIZE] exit no-progress monitor ok=%s", ok)
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] exit no-progress monitor failed")
        return False


def _install_summary_ai_board_rest_cooldown() -> bool:
    """Summary-AI 経路の REST 板クールダウン回避と、連鎖する戦略オーバレイ。

    クールダウン回避本体は kabu_api/board_retry.py の
    _fetch_board_rest へ統合済み。ここでは旧パッチが併せて行っていた
    3つの戦略 install を、同じ位置・同じ順序で実行する。
    """
    results = {}
    # strategy_mode は trading/entry/summary_ai/executor.py へ本文化済み
    # (_sm_* / _sort_key / _passes_strict_candidate_quality)。
    # fibonacci_ev / nve_projection のオーバーレイもその _sm_classify /
    # _sm_strategy_enabled に取り込まれている (合成順も同じ)。
    # 有効/無効は SUMMARY_AI_FIB_EV_STRATEGY_ENABLED /
    # SUMMARY_AI_NVE_STRATEGY_ENABLED を各層が見る。
    # install は不要になったので、モジュールが読めることだけ確認する。
    try:
        import trading.entry.summary_ai.executor as _ex  # noqa: F401

        results["strategy_mode"] = True
    except Exception:
        results["strategy_mode"] = False
        logger.exception("[USERCUSTOMIZE] chained strategy_mode import failed")
    ok = any(results.values())
    logger.warning("[USERCUSTOMIZE] summary AI board REST cooldown bypass ok=%s chained=%s", ok, results)
    return ok


def _install_summary_ai_emp_prob_gate() -> bool:
    """
    Summary-AI 実績確率ゲートをinstallする。

    AIが allow=True を出した後に、
    summary DB の1分足履歴から以下を確認する。

    - 5分以内に利確方向へ先に到達した確率
    - 5分以内に損切方向へ先に到達した確率
    - 期待値

    これにより、AI confidence を「上がる確率」と誤解せず、
    実測ベースの確率で AI_OK を最終確認する。
    """
    try:
        # summary_ai_emp_prob_gate_patch は
        # trading/entry/summary_ai/emp_prob_gate.py へ移設し、
        # ai_gate_runner.py の内部呼び出し直後で apply_emp_prob_gate() を
        # 呼ぶ形へ本文化した。env 既定値は同モジュールが import 時に入れる。
        from trading.entry.summary_ai import emp_prob_gate as _emp_prob_gate

        _emp_prob_gate.apply_env_defaults()
        ok = True
        logger.warning(
            "[USERCUSTOMIZE] summary AI empirical probability gate ok=%s enabled=%s min_p=%s max_risk=%s samples=%s",
            ok,
            os.getenv("SUMMARY_AI_EMP_PROB_GATE_ENABLED"),
            os.getenv("SUMMARY_AI_EMP_PROB_MIN_TARGET_PROB"),
            os.getenv("SUMMARY_AI_EMP_PROB_MAX_RISK_PROB"),
            os.getenv("SUMMARY_AI_EMP_PROB_MIN_SAMPLES"),
        )
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] summary AI empirical probability gate failed")
        return False


def _install_runtime_defaults() -> bool:
    try:
        from core.startup.runtime_env.installer import install as _install_defaults

        ok = bool(_install_defaults())
        logger.warning("[USERCUSTOMIZE] centralized runtime defaults ok=%s", ok)
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] centralized runtime defaults failed")
        return False


def _pending_age_relax_safe_float(v, default: float) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _pending_age_relax_raise_min_env(name: str, minimum: float):
    old = os.environ.get(name)
    cur = _pending_age_relax_safe_float(old, minimum)
    if old is None or cur < minimum:
        os.environ[name] = str(int(minimum) if float(minimum).is_integer() else minimum)
    return old, os.environ.get(name, "")


def _install_summary_pending_age_relax() -> bool:
    # core/startup/summary_entry_pending_age_relax_patch.py (V1-SUMMARY-PENDING-AGE-900S) を native化。
    # SUMMARY_AI 承認行が pending 登録に届いても、既定の age limit (180s) が厳しすぎて
    # no_pending_registered として弾かれていた問題への対処。pending 登録の stale 判定 window
    # のみを緩和し (下限900s)、stale 保護自体は無効化しない。
    try:
        minimum = _pending_age_relax_safe_float(os.environ.get("SUMMARY_ENTRY_PENDING_RELAX_MIN_AGE_SEC"), 900.0)
        changed = {}
        for name in (
            "SUMMARY_ENTRY_PENDING_MAX_AGE_SEC",
            "SUMMARY_AI_PENDING_MAX_AGE_SEC",
            "SUMMARY_AI_MAX_CANDIDATE_AGE_SEC",
            "ENTRY_CANDIDATE_MAX_AGE_SEC",
        ):
            old, new = _pending_age_relax_raise_min_env(name, minimum)
            changed[name] = {"old": old, "new": new}

        # summary_entry_pending_existing_fix_patch は
        # trading/summary/summary_entry.register_pending_entries へ本文化済み。
        # env 値は都度動的に読むので、ここでの再install は不要になった。

        logger.warning(
            "[USERCUSTOMIZE] summary pending age relax ok=True max_age=%s (min_age=%s changed=%s)",
            os.getenv("SUMMARY_ENTRY_PENDING_MAX_AGE_SEC"), minimum, changed,
        )
        return True
    except Exception:
        logger.exception("[USERCUSTOMIZE] summary pending age relax failed")
        return False


def _summary_mtf_guard_is_db_collector_context() -> bool:
    """旧 summary_mtf_duplicate_datetime_guard_patch._is_database_collector_context。"""
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(
            x in argv
            for x in (
                "main_database.py",
                "db_prepare_runner.py",
                "ranking_collector_runner.py",
                "push_receiver_runner.py",
                "yahoo_complement_runner.py",
                "summary_database_runner.py",
                "data_collectors_runner.py",
            )
        ):
            return True
        return any(
            os.getenv(k) == "1"
            for k in (
                "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
                "AUTOSTOCK_MAIN_DATABASE_PROCESS",
                "AUTOSTOCK_SUMMARY_DB_WRITER",
                "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
            )
        )
    except Exception:
        return False


def _install_summary_mtf_duplicate_datetime_guard() -> bool:
    """旧 core/startup/summary_mtf_duplicate_datetime_guard_patch を削除して直呼びにした。

    旧 install() の 3 つのサブ処理のうち、生きていたのは 3 だけだった:

      1. summary_mtf_diff_from_1m_patch._repair_mtf_from_1m のラップ
         -> 同モジュールはこのヘルパを既に持っていない (571行のコメント参照)。
            getattr が None を返し「target repair missing」を出して False。
            付随する install フック (_install_target_install_hook) も、
            フック先で同じ _patch_target_repair を再試行するだけなので
            ログ以外の効果が無かった。
      2. summary_ai_candidate_refill_patch._push_writer_state のラップ
         -> 同モジュールにこの属性は存在しない。「target missing」で False。
      3. main_entry_operator (旧 main_entry_runtime_operator_fix_patch) の
         中継 install (main.py のみ)
         -> **これだけが実効**。しかもこのパッチを install しているのは
            リポジトリ全体でここ 1 箇所だけなので、そのまま残す。

    1 と 2 の対象属性はリポジトリ全体を検索しても定義箇所が無い
    (動的 setattr も含めて 0 件) ため、復活する余地も無い。
    """
    if not ("main.py" in " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
            and not _summary_mtf_guard_is_db_collector_context()):
        return False
    try:
        from core.startup import main_entry_operator as p

        fn = getattr(p, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[USERCUSTOMIZE] main entry runtime fix installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] main entry runtime fix failed")
        return False


_install_runtime_defaults()
_patch_entry_order_builder_logger()
_install_summary_ai_fast_order_builder()
_install_exit_no_progress_monitor()
_install_summary_ai_board_rest_cooldown()
_install_summary_ai_emp_prob_gate()
_install_summary_pending_age_relax()
_install_summary_mtf_duplicate_datetime_guard()


if _is_main_py():
    for k, v in {
        "AUTOSTOCK_MAIN_OPERATION_MODE": "full",
        "AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS": "0",
        "AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP": "0",
        "AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY": "0",
        "AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY": "0",
        "AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG": "0",
        "AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE": "0",
        "AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK": "0",
        "AUTOSTOCK_MAIN_SKIP_EXIT_LOOP_WHEN_BROKER_EMPTY": "0",
        "AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT": "1",
        "YAHOO_COMPLEMENT_RUN_IN_MAIN": "0",
        "AUTOSTOCK_ENABLE_YAHOO_COMPLEMENT_IN_MAIN": "0",
        "AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP": "1",
        "AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK": "1",
        "AUTOSTOCK_MAIN_ENABLE_RANKING_SUMMARY_SCHEDULE": "1",
        "FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK": "1",
    }.items():
        _env_default(k, v)

    logger.warning(
        "[USERCUSTOMIZE] main restore defaults mode=%s exit=%s ranking=%s tonosama=%s summary_ai=%s summary_parent=%s summary_db_save_skip=%s yahoo_skip=%s",
        os.getenv("AUTOSTOCK_MAIN_OPERATION_MODE"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK"),
        os.getenv("AUTOSTOCK_MAIN_SKIP_SUMMARY_DB_SAVE"),
        os.getenv("AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT"),
    )


# 板なしは流動性不足扱い。ここではエントリー許可へ緩めない。
os.environ.setdefault("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "0")
os.environ.setdefault("ENTRY_BOARD_MISSING_HARD_BLOCK", "1")


def _uc_float(v, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _uc_patch_tonosama_recent_volume_display() -> bool:
    if not _is_main_py():
        return False

    try:
        import re
        import pandas as pd
        import trading.entry.tonosama.volume_surge as vs
        import trading.entry.tonosama.pending_writer as pw

        def _norm_symbol_series(s):
            return s.astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

        orig_build = getattr(vs.build_scalping_feature_df, "_original", vs.build_scalping_feature_df)

        if not getattr(vs.build_scalping_feature_df, "_uc_recent_volume_display_v1", False):

            def _patched_build_scalping_feature_df():
                out = orig_build()
                try:
                    if out is None or out.empty or "symbol" not in out.columns:
                        return out

                    raw1 = vs.normalize_summary_base(vs.load_merged_summary(1), interval=1)
                    if raw1 is None or raw1.empty or "symbol" not in raw1.columns or "volume" not in raw1.columns:
                        return out

                    raw1 = raw1.copy()
                    raw1["symbol"] = _norm_symbol_series(raw1["symbol"])
                    raw1["volume"] = pd.to_numeric(raw1["volume"], errors="coerce").fillna(0.0)

                    if "datetime" in raw1.columns:
                        raw1["datetime"] = pd.to_datetime(raw1["datetime"], errors="coerce")
                        raw1 = raw1.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"])

                    rows = []
                    for sym, g in raw1.groupby("symbol", sort=False):
                        gv = g.tail(5)["volume"]
                        rows.append(
                            {
                                "symbol": str(sym),
                                "recent_volume_1m": float(gv.tail(1).sum()),
                                "recent_volume_3m": float(gv.tail(3).sum()),
                                "recent_volume_5m": float(gv.tail(5).sum()),
                            }
                        )

                    if not rows:
                        return out

                    recent = pd.DataFrame(rows)
                    x = out.copy()
                    x["symbol"] = _norm_symbol_series(x["symbol"])
                    x = x.merge(recent, on="symbol", how="left")

                    for c in ("recent_volume_1m", "recent_volume_3m", "recent_volume_5m"):
                        x[c] = pd.to_numeric(x.get(c, 0.0), errors="coerce").fillna(0.0)

                    logger.warning(
                        "[USERCUSTOMIZE][TONOSAMA RECENT VOLUME DISPLAY] attached rows=%s sample=%s",
                        len(x),
                        x[
                            [
                                c
                                for c in [
                                    "symbol",
                                    "recent_volume_1m",
                                    "recent_volume_3m",
                                    "recent_volume_5m",
                                ]
                                if c in x.columns
                            ]
                        ]
                        .head(10)
                        .to_dict("records"),
                    )
                    return x
                except Exception:
                    logger.exception("[USERCUSTOMIZE][TONOSAMA RECENT VOLUME DISPLAY] attach failed")
                    return out

            _patched_build_scalping_feature_df._uc_recent_volume_display_v1 = True  # type: ignore[attr-defined]
            _patched_build_scalping_feature_df._original = orig_build  # type: ignore[attr-defined]
            vs.build_scalping_feature_df = _patched_build_scalping_feature_df

        orig_conditions = getattr(pw._entry_conditions_from_row, "_original", pw._entry_conditions_from_row)

        if not getattr(pw._entry_conditions_from_row, "_uc_recent_volume_display_v1", False):

            def _row_vol(row, names, default=0.0):
                for name in names:
                    try:
                        val = _uc_float(row.get(name), -1.0)
                    except Exception:
                        val = -1.0
                    if val >= 0:
                        return val
                return float(default)

            def _replace_volume_text(reason: str, v1: float, v3: float, v5: float) -> str:
                new_text = f"出来高 1m={v1:.0f} / 3m={v3:.0f} / 5m={v5:.0f}"
                pat = r"出来高 1m=[0-9,\.]+\s*/\s*3m=[0-9,\.]+\s*/\s*5m=[0-9,\.]+"
                if re.search(pat, reason or ""):
                    return re.sub(pat, new_text, reason, count=1)
                return f"{reason} / {new_text}" if reason else new_text

            def _patched_entry_conditions_from_row(row, *, ai_reason: str, side: str, expire_at):
                cond = orig_conditions(row, ai_reason=ai_reason, side=side, expire_at=expire_at)
                try:
                    v1 = _row_vol(row, ["recent_volume_1m", "volume_1m", "latest_volume_1m", "volume", "_latest_volume"])
                    v3 = _row_vol(row, ["recent_volume_3m", "volume_last_3m", "rolling_volume_3m", "volume_3m_display", "volume_3m"], v1)
                    v5 = _row_vol(row, ["recent_volume_5m", "volume_last_5m", "rolling_volume_5m", "volume_5m_display", "volume_5m"], v3)

                    if isinstance(cond, dict):
                        cond["display_volume_1m"] = v1
                        cond["display_volume_3m"] = v3
                        cond["display_volume_5m"] = v5
                        cond["recent_volume_1m"] = v1
                        cond["recent_volume_3m"] = v3
                        cond["recent_volume_5m"] = v5
                        cond["reason"] = _replace_volume_text(str(cond.get("reason") or ""), v1, v3, v5)

                    logger.warning(
                        "[USERCUSTOMIZE][TONOSAMA RECENT VOLUME DISPLAY] reason fixed symbol=%s side=%s v1=%.0f v3=%.0f v5=%.0f",
                        row.get("symbol") if hasattr(row, "get") else None,
                        side,
                        v1,
                        v3,
                        v5,
                    )
                except Exception:
                    logger.exception("[USERCUSTOMIZE][TONOSAMA RECENT VOLUME DISPLAY] reason patch failed")
                return cond

            _patched_entry_conditions_from_row._uc_recent_volume_display_v1 = True  # type: ignore[attr-defined]
            _patched_entry_conditions_from_row._original = orig_conditions  # type: ignore[attr-defined]
            pw._entry_conditions_from_row = _patched_entry_conditions_from_row

        logger.warning("[USERCUSTOMIZE] TONOSAMA recent volume display patch installed")
        return True
    except Exception:
        logger.exception("[USERCUSTOMIZE] TONOSAMA recent volume display patch install failed")
        return False


def _uc_norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _uc_dedupe_symbols(values: Iterable[Any]) -> list[str]:
    out = []
    seen = set()

    for x in values or []:
        s = _uc_norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def _uc_is_push_rotation_context(context: str) -> bool:
    c = str(context or "").lower()
    return "push.rotation" in c or "apply_register_liquidity_guard" in c or "push_stream.rotation" in c


def _uc_patch_watchlist_recent_liq_rotation_failopen() -> bool:
    """watchlist_recent_liquidity の 3 層は本文化済み。

    guard の per-symbol 実装 / bulk の完全置換 / ここにあった min_keep
    fail-open ラッパーは、まとめて
    trading/ranking/active_symbols/recent_liquidity.py の
    filter_symbols() / apply_failopen_min_keep() に入れた。
    呼び出し側は
      trading/ranking/active_symbols/manager.py の 8 関数
      trading/push/push_stream/rotation_symbols.apply_register_liquidity_guard
    が本文で直接呼ぶ (互換ラッパーと rotation.py は from-import で追従)。
    """
    return True


def _uc_delayed_watchlist_patch_loop() -> None:
    for _ in range(10):
        try:
            _uc_patch_watchlist_recent_liq_rotation_failopen()
            _patch_entry_order_builder_logger()
            _install_summary_ai_fast_order_builder()
            _install_exit_no_progress_monitor()
            _install_summary_ai_board_rest_cooldown()
            _install_summary_ai_emp_prob_gate()
        except Exception:
            logger.exception("[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] delayed retry failed")

        time.sleep(1.0)


if _is_main_py():
    _patch_entry_order_builder_logger()
    _install_summary_ai_fast_order_builder()
    _install_exit_no_progress_monitor()
    _install_summary_ai_board_rest_cooldown()
    _install_summary_ai_emp_prob_gate()
    _uc_patch_tonosama_recent_volume_display()
    _uc_patch_watchlist_recent_liq_rotation_failopen()

    threading.Thread(
        target=_uc_delayed_watchlist_patch_loop,
        name="usercustomize-push-rotation-liq-failopen",
        daemon=True,
    ).start()