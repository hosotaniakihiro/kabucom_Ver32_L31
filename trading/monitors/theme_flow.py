# ============================================================
# File   : trading/monitors/theme_flow.py
# 2026-10-08: ランキング銘柄を株探テーマで束ね、「今どのテーマに資金が入っているか」を毎日記録する (売買はしない)
# ------------------------------------------------------------
# 5 分ごと (場中) に build_liquidity_map() の当日騰落率・売買代金を stock_themes.csv のテーマへ集計し、
#   ログ [THEME FLOW] と %LOCALAPPDATA%\autostock\themes\flow\theme_flow_YYYYMMDD.csv に残す。
# 1 銘柄の急騰で関係の薄いテーマまで上位に出るので、+RISE% 以上の銘柄が MIN_RISERS 以上あるテーマだけ出す。
# 10/6 の検証で「出遅れ銘柄の後追い買い」は効かなかった (theme_laggard_no_edge)。判断材料として使う。
# 単体でも動く: venv\Scripts\python.exe -m trading.monitors.theme_flow [--loop]
# 止め方: THEME_FLOW_ENABLED=0
# ============================================================
from __future__ import annotations

import collections
import csv
import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_THEMES: Dict[str, Set[str]] = {}
_THEMES_MTIME = 0.0


def _env_f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _base() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "autostock" / "themes"


def themes() -> Dict[str, Set[str]]:
    """theme -> {symbol}。csv が夜間に更新されたら読み直す。"""
    global _THEMES, _THEMES_MTIME
    p = _base() / "stock_themes.csv"
    try:
        m = p.stat().st_mtime
    except Exception:
        return _THEMES
    if m != _THEMES_MTIME:
        th: Dict[str, Set[str]] = collections.defaultdict(set)
        with open(p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                s = str(r.get("symbol") or "").strip()
                if s and r.get("theme"):
                    th[r["theme"]].add(s)
        _THEMES, _THEMES_MTIME = dict(th), m
    return _THEMES


def _f(v: Dict[str, Any], k: str) -> float:
    try:
        return float(v.get(k) or 0)
    except Exception:
        return 0.0


def snapshot(lm: Dict[str, Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    """資金が入っているテーマを強い順に返す。"""
    if lm is None:
        from trading.ranking.active_symbols.ranking_source import build_liquidity_map
        lm = build_liquidity_map() or {}
    rise = _env_f("THEME_FLOW_RISE_PCT", 5.0)
    min_risers = int(_env_f("THEME_FLOW_MIN_RISERS", 2))
    min_members = int(_env_f("THEME_FLOW_MIN_MEMBERS", 3))
    out = []
    for t, mem in themes().items():
        hit = [s for s in mem if s in lm]
        if len(hit) < min_members:
            continue
        chg = sorted(((_f(lm[s], "change_percentage"), s) for s in hit), reverse=True)
        risers = [(c, s) for c, s in chg if c >= rise]
        if len(risers) < min_risers:
            continue
        up = sum(1 for c, _ in chg if c > 0)
        out.append({
            "theme": t, "members": len(hit), "risers": len(risers), "up": up,
            "up_ratio": round(up / len(hit), 2),
            "avg_chg": round(sum(c for c, _ in chg) / len(chg), 2),
            "tv_up_oku": round(sum(_f(lm[s], "trading_value") for c, s in chg if c > 0) / 1e8, 1),
            "top": " ".join(f"{s}({c:+.1f}%)" for c, s in chg[:5]),
        })
    # 急騰銘柄の数 × 上昇割合 (IT関連のような大きいテーマが 1〜2 銘柄で上位に来ないように) → 平均騰落
    for r in out:
        r["score"] = round(r["risers"] * r["up_ratio"], 2)
    out.sort(key=lambda r: (-r["score"], -r["avg_chg"]))
    return out


def _hot_path() -> Path:
    return _base() / "flow" / "hot_symbols.json"


def _write_hot(rows: List[Dict[str, Any]], now: dt.datetime, lm: Dict[str, Dict[str, Any]]) -> None:
    """2026-10-08 (ユーザー要望): PUSH 50 枠で優先する銘柄を書き出す。読むのは push_receiver 側の rotation。
    上位 THEME_FLOW_HOT_THEMES テーマ (スコア 1.0 以上) の銘柄のうち、当日 +HOT_MIN_CHG% 以上で動いているもの。
    株価 200 円未満・売買代金 HOT_MIN_TV 未満は入れない (低位株・薄い銘柄はスプレッドで負ける)。"""
    min_chg = _env_f("THEME_FLOW_HOT_MIN_CHG", 2.0)
    min_tv = _env_f("THEME_FLOW_HOT_MIN_TV", 5e8)
    k = int(_env_f("THEME_FLOW_HOT_THEMES", 3))
    hot: Dict[str, Dict[str, Any]] = {}
    for r in [r for r in rows if r["score"] >= 1.0][:k]:
        for s in themes().get(r["theme"], ()):
            v = lm.get(s)
            if not v or s in hot:
                continue
            chg, px, tv = _f(v, "change_percentage"), _f(v, "current_price"), _f(v, "trading_value")
            if chg >= min_chg and px >= 200 and tv >= min_tv:
                hot[s] = {"theme": r["theme"], "chg": round(chg, 2)}
    syms = sorted(hot, key=lambda s: -hot[s]["chg"])[: int(_env_f("THEME_FLOW_HOT_MAX", 10))]
    p = _hot_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": now.strftime("%Y-%m-%d %H:%M:%S"), "symbols": syms,
                               "detail": {s: hot[s] for s in syms}}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def hot_symbols(max_age_min: float | None = None) -> List[str]:
    """PUSH 枠で優先する銘柄。古い (既定 15 分超) か無効なら空。"""
    try:
        if str(os.getenv("THEME_FLOW_PUSH_PRIORITY", "1")).strip().lower() in ("0", "false", "no", "off"):
            return []
        d = json.loads(_hot_path().read_text(encoding="utf-8"))
        at = dt.datetime.strptime(d["at"], "%Y-%m-%d %H:%M:%S")
        age = (dt.datetime.now() - at).total_seconds() / 60
        if age > (max_age_min if max_age_min is not None else _env_f("THEME_FLOW_HOT_MAX_AGE_MIN", 15)):
            return []
        return [str(s) for s in d.get("symbols") or []]
    except Exception:
        return []


def run_cycle() -> List[Dict[str, Any]]:
    with _LOCK:
        t0 = time.time()
        from trading.ranking.active_symbols.ranking_source import build_liquidity_map
        lm = build_liquidity_map() or {}
        rows = snapshot(lm)
        now = dt.datetime.now()
        d = _base() / "flow"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"theme_flow_{now:%Y%m%d}.csv"
        new = not p.exists()
        cols = ["time", "rank", "theme", "score", "members", "risers", "up", "up_ratio", "avg_chg", "tv_up_oku", "top"]
        with open(p, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            if new:
                w.writeheader()
            for i, r in enumerate(rows[: int(_env_f("THEME_FLOW_TOP_N", 15))], 1):
                w.writerow({"time": f"{now:%H:%M}", "rank": i, **r})
        try:
            _write_hot(rows, now, lm)
        except Exception:
            logger.exception("[THEME FLOW] hot symbols write failed")
        head = "; ".join(f"{r['theme']} {r['risers']}/{r['members']} 上昇{int(r['up_ratio']*100)}% 平均{r['avg_chg']:+.1f}%"
                         for r in rows[:6])
        logger.warning("[THEME FLOW] %s themes=%d elapsed=%.1fs top: %s", f"{now:%H:%M}", len(rows), time.time() - t0, head or "-")
        return rows


def _run_guarded() -> None:
    def _job():
        try:
            from core.startup.scheduler.market_guard import is_market_time_for_entry
            if not is_market_time_for_entry():
                return
            run_cycle()
        except Exception:
            logger.exception("[THEME FLOW] cycle failed")
    if _LOCK.locked():
        return
    threading.Thread(target=_job, name="theme-flow", daemon=True).start()


def register_scheduler_safe() -> bool:
    try:
        if str(os.getenv("THEME_FLOW_ENABLED", "1")).strip().lower() in ("0", "false", "no", "off"):
            return False
        import schedule
        from core.startup.scheduler.helpers import has_schedule_tag
        if has_schedule_tag("theme_flow"):
            return True
        every = int(_env_f("THEME_FLOW_INTERVAL_SEC", 300))
        schedule.every(every).seconds.do(_run_guarded).tag("theme_flow", "monitor")
        logger.warning("[THEME FLOW] scheduler registered (record only) every=%ss", every)
        return True
    except Exception:
        logger.exception("[THEME FLOW] scheduler register failed")
        return False


def _in_session(now: dt.datetime) -> bool:
    hm = now.hour * 100 + now.minute
    return (900 <= hm < 1130) or (1230 <= hm < 1530)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--report" in sys.argv:   # 1 日のまとめ: --report [YYYYMMDD]
        args = [a for a in sys.argv[1:] if a.isdigit()]
        day = args[0] if args else f"{dt.date.today():%Y%m%d}"
        p = _base() / "flow" / f"theme_flow_{day}.csv"
        agg: Dict[str, Dict[str, Any]] = {}
        with open(p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                a = agg.setdefault(r["theme"], {"first": r["time"], "last": r["time"], "n": 0, "top5": 0, "max": 0.0, "top": ""})
                a["n"] += 1
                a["last"] = r["time"]
                a["top5"] += int(r["rank"]) <= 5
                if float(r["score"]) > a["max"]:
                    a["max"], a["top"] = float(r["score"]), r["top"]
        print(f"## {day} 資金が入ったテーマ (5 分ごとの記録 {p.name})")
        print("| テーマ | 最初 | 最後 | 記録回数 | 上位5回数 | 最大スコア | その時の銘柄 |")
        for t, a in sorted(agg.items(), key=lambda kv: (-kv[1]["top5"], -kv[1]["max"]))[:20]:
            print(f"| {t} | {a['first']} | {a['last']} | {a['n']} | {a['top5']} | {a['max']:.2f} | {a['top']} |")
    elif "--loop" in sys.argv:   # システムを再起動せずに今日の分を取る用
        while dt.datetime.now().hour * 100 + dt.datetime.now().minute < 1531:
            if _in_session(dt.datetime.now()):
                try:
                    run_cycle()
                except Exception:
                    logger.exception("[THEME FLOW] cycle failed")
            time.sleep(300)
    else:
        for r in run_cycle()[:15]:
            print(r)

__all__ = ["snapshot", "run_cycle", "register_scheduler_safe", "themes"]
