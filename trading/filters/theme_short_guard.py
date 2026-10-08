# ============================================================
# File   : trading/filters/theme_short_guard.py
# 2026-10-08: テーマ全体が崩れるまで、そのテーマの銘柄は空売りしない (ユーザー判断)
# ------------------------------------------------------------
# 情報漏洩が相次ぎ、セキュリティ関連に一斉に買いが入っている。3927 (フーバーブレイン) は
# +13% で空売りした 8 分後に +5% の損切り (-1.2 万円)。テーマで買われている間は 1 銘柄が崩れても
# また買われるので、テーマの銘柄が揃って崩れてから売る。
#
# 判定 (ranking の当日騰落率で見る。再起動しても当日の最大値は JSON に残す):
#   - 当日 +RISE_MIN% 以上まで上がった テーマ銘柄 = 上昇銘柄
#   - 上昇銘柄が 1 つでも「当日の最大上昇幅の GIVEBACK 割以上を失った」状態でなければ、空売りを止める
#     (全部崩れたら解除。また買われて戻せば自動で止める側に戻る)
#   - 上昇銘柄が無ければ止めない (テーマに買いが入っていない)
# 対象: 株探テーマ (サイバーセキュリティ / 情報セキュリティ / セキュリティソフト) + 下の手入れリスト。
#   株探の対応表には 3927・2326・3042・3968・4288・5133 などが無かったので足した。
# 止め方: THEME_SHORT_GUARD_ENABLED=0。銘柄の追加は THEME_SHORT_GUARD_EXTRA="1234,5678"。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
THEME = "security"
KABUTAN_THEMES = ("サイバーセキュリティ", "情報セキュリティ", "セキュリティソフト")
EXTRA = ("2326", "3040", "3042", "3692", "3762", "3857", "3858", "3927", "3967", "3968", "4288", "4382",
         "4398", "4417", "4475", "4493", "4498", "5133")
_CACHE: Dict[str, Any] = {"at": 0.0, "members": None, "status": None}


def _env_f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _enabled() -> bool:
    # 2026-10-08 09:50: 既定 OFF (ユーザー判断「空売り停止しなくていい」)。使う時は THEME_SHORT_GUARD_ENABLED=1
    return str(os.getenv("THEME_SHORT_GUARD_ENABLED", "0")).strip().lower() not in ("0", "false", "no", "off")


def _base() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "autostock"


def members() -> Set[str]:
    if _CACHE["members"] is not None:
        return _CACHE["members"]
    s: Set[str] = set(EXTRA)
    try:
        import csv
        with open(_base() / "themes" / "stock_themes.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r.get("theme") or "") in KABUTAN_THEMES:
                    s.add(str(r.get("symbol") or "").strip())
    except Exception:
        logger.warning("[THEME SHORT GUARD] stock_themes.csv unavailable (手入れリストだけで判定)", exc_info=True)
    s |= {x.strip() for x in str(os.getenv("THEME_SHORT_GUARD_EXTRA", "")).split(",") if x.strip()}
    s.discard("")
    _CACHE["members"] = s
    return s


def _state_path() -> Path:
    p = _base() / "runtime" / f"theme_short_guard_{dt.date.today():%Y%m%d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def status(force: bool = False) -> Dict[str, Any]:
    """{"block": bool, "risers": n, "broken": n, "holding": [...上がったまま...]}。20 秒キャッシュ。"""
    with _LOCK:
        now = time.time()
        if not force and _CACHE["status"] is not None and now - _CACHE["at"] < _env_f("THEME_SHORT_GUARD_TTL_SEC", 20):
            return _CACHE["status"]
        rise_min = _env_f("THEME_SHORT_GUARD_RISE_MIN_PCT", 5.0)
        giveback = _env_f("THEME_SHORT_GUARD_GIVEBACK", 0.4)
        p = _state_path()
        try:
            st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            st = {}
        mx: Dict[str, float] = st.get("max", {})
        cur: Dict[str, float] = st.get("cur", {})
        try:
            from trading.ranking.active_symbols.ranking_source import build_liquidity_map
            lm = build_liquidity_map() or {}
        except Exception:
            lm = {}
        mem = members()
        for sym, v in lm.items():
            if sym not in mem:
                continue
            try:
                chg = float(v.get("change_percentage") or 0)
            except Exception:
                continue
            cur[sym] = chg
            if chg > mx.get(sym, -999.0):
                mx[sym] = chg
        risers = {s: m for s, m in mx.items() if m >= rise_min}
        holding = sorted((s for s, m in risers.items() if cur.get(s, m) > m * (1 - giveback)),
                         key=lambda s: -cur.get(s, 0))
        res = {"block": bool(holding), "risers": len(risers), "broken": len(risers) - len(holding),
               "holding": [(s, round(cur.get(s, 0), 1), round(risers[s], 1)) for s in holding[:8]]}
        prev = st.get("block")
        st.update(max=mx, cur=cur, block=res["block"], updated=dt.datetime.now().strftime("%H:%M:%S"))
        try:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            logger.debug("[THEME SHORT GUARD] state write failed", exc_info=True)
        if prev is not None and prev != res["block"]:
            logger.warning("[THEME SHORT GUARD] %s theme=%s risers=%d broken=%d holding=%s",
                           "BLOCK ON (テーマにまた買い)" if res["block"] else "BLOCK OFF (上昇銘柄が全部崩れた)",
                           THEME, res["risers"], res["broken"], res["holding"])
        _CACHE.update(at=now, status=res)
        return res


def block_reason(symbol: Any, side: Any) -> Optional[str]:
    """空売りを止めるなら理由の文字列、通すなら None。失敗したら通す。"""
    try:
        if not _enabled() or str(side or "").upper() != "SELL":
            return None
        sym = str(symbol or "").strip()
        if sym not in members():
            return None
        s = status()
        if not s["block"]:
            return None
        return (f"THEME_SHORT_GUARD:{THEME} risers={s['risers']} broken={s['broken']} "
                f"holding={s['holding'][:4]}")
    except Exception:
        logger.debug("[THEME SHORT GUARD] check failed symbol=%s", symbol, exc_info=True)
        return None


__all__ = ["block_reason", "status", "members"]
