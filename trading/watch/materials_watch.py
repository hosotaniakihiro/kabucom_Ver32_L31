# ============================================================
# File   : trading/watch/materials_watch.py
# 2026-10-03: 前日の材料株と夜間 PTS で動いた銘柄を、朝の監視 (PUSH 登録) に優先して入れる
# ------------------------------------------------------------
# ユーザー要望: 株探の【明日の好悪材料】を開示情報でチェック！ に載った銘柄と、
#   夜間 PTS で反応している銘柄をチェックしたい。
#
# 取得 (1 日 1 回・本番が動き出してから裏のスレッドで):
#   1) 株探 ニュース [注目] (category=9) の一覧から最新の「明日の好悪材料」記事 → 【好材料】【悪材料】の銘柄と一言。
#      平日分は当日 20 時ごろ、金曜分は日曜に出る。直近 4 日以内の最新の記事を使う。
#   2) 株探 PTS ナイトタイムセッション 上昇率 / 下落率ランキング (各 2 ページ = 30 銘柄)。朝 6 時ごろまでの値。
#   株探の robots.txt (Crawl-delay: 3) に合わせ、リクエストの間は 3 秒以上あける (1 日 5〜7 回)。
#   結果は %LOCALAPPDATA%\autostock\runtime\materials_watch_YYYYMMDD.json と Discord に出す。
#
# 使い方 (rotation_stability.resolve_monitor_symbols から):
#   watch_symbols() が MATERIALS_WATCH_START〜MATERIALS_WATCH_END (既定 08:00〜10:00) の間だけ
#   優先銘柄 (最大 MATERIALS_WATCH_MAX、既定 15) を返す。並びは
#   PTS で |変化率| が大きい順 (|変化率| >= 5%・PTS 売買代金 >= 1,000 万円・株価 >= 200 円)
#   → 好材料 → 悪材料。10 時以降は通常の候補と同じ扱い (当日の変化率で並ぶ) になる。
#   売買の判断は変えない (監視に入れるだけ)。
# 止め方: MATERIALS_WATCH_ENABLED=0
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BASE = "https://kabutan.jp"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
_lock = threading.Lock()
_state: Dict[str, Any] = {"day": "", "data": None, "last_try": 0.0, "running": False}


def _env(name: str, default: str) -> str:
    return str(os.getenv(name, default) or default).strip()


def enabled() -> bool:
    return _env("MATERIALS_WATCH_ENABLED", "1").lower() not in ("0", "false", "no", "off")


def _runtime_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "autostock" / "runtime"


def _path(day: dt.date) -> Path:
    return _runtime_dir() / f"materials_watch_{day.strftime('%Y%m%d')}.json"


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _num(s: str) -> float:
    try:
        return float(re.sub(r"[^0-9.\-]", "", s.replace("+", "")) or "nan")
    except Exception:
        return float("nan")


class _Client:
    def __init__(self) -> None:
        os.environ.pop("SSLKEYLOGFILE", None)   # Norton: これがあると Python の HTTPS が永久に止まる
        import requests
        try:
            from scripts.yahoo_ca_bundle import install
            install()
        except Exception:
            logger.debug("[MATERIALS WATCH] ca bundle install failed", exc_info=True)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ja"})
        self.last = 0.0

    def get(self, path: str) -> str:
        wait = 3.2 - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        r = self.s.get(BASE + path, timeout=20)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return r.text


_LIST_RE = re.compile(r'<td class="news_time"><time datetime="([^"]+)">.*?</td>\s*<td><div class="newslist_ctg [^"]+">.*?</div></td>\s*'
                      r'<td><a href="([^"]+)">(.*?)</a>', re.S)
_ITEM_RE = re.compile(r'■\s*(.*?)\s*&lt;<a href="/stock/\?code=(\w+)">\w+</a>&gt;\s*\[([^\]]*)\]\s*<br\s*/?>\s*(.*?)<br\s*/?>', re.S)


def _parse_kouaku(html: str) -> Dict[str, List[Dict[str, str]]]:
    k = html.find('<div class="body">')
    body = html[k:] if k >= 0 else html
    end = body.find("</div>\n</div>")
    good_i, bad_i = body.find("【好材料】"), body.find("【悪材料】")
    out: Dict[str, List[Dict[str, str]]] = {"good": [], "bad": []}
    for key, a, b in (("good", good_i, bad_i if bad_i > good_i else len(body)), ("bad", bad_i, len(body))):
        if a < 0:
            continue
        seg = body[a:b]
        for name, code, market, note in _ITEM_RE.findall(seg):
            out[key].append({"symbol": code, "name": _text(name), "market": _text(market), "note": _text(note)[:160]})
    return out


def _fetch_kouaku(c: _Client, now: dt.datetime) -> Dict[str, Any]:
    for page in (1, 2, 3):
        html = c.get(f"/news/marketnews/?category=9&page={page}")
        for ts, href, title in _LIST_RE.findall(html):
            t = _text(title)
            if "好悪材料" not in t:
                continue
            try:
                pub = dt.datetime.fromisoformat(ts).replace(tzinfo=None)
            except Exception:
                continue
            if now - pub > dt.timedelta(days=4):
                return {"title": None, "reason": f"latest article too old ({pub})"}
            art = c.get(href.replace("&amp;", "&"))
            parsed = _parse_kouaku(art)
            return {"title": t, "published": pub.isoformat(), "url": BASE + href, **parsed}
    return {"title": None, "reason": "not found in 3 pages"}


def _fetch_pts(c: _Client, kind: str) -> List[Dict[str, Any]]:
    rows_out: List[Dict[str, Any]] = []
    for page in (1, 2):
        html = c.get(f"/warning/pts_night_price_{kind}" + (f"?page={page}" if page > 1 else ""))
        k = html.find('class="stock_table st_market"')
        if k < 0:
            break
        body = html[k:html.find("</table>", k)]
        for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
            cells = [_text(x) for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            if len(cells) < 10 or not re.fullmatch(r"\w{4}", cells[0] or ""):
                continue
            close, pts, chg_pct, vol = _num(cells[5]), _num(cells[6]), _num(cells[8]), _num(cells[9])
            rows_out.append({"symbol": cells[0], "name": cells[1], "market": cells[2], "close": close, "pts": pts,
                             "chg_pct": chg_pct, "volume": vol, "value_yen": (pts * vol) if pts == pts and vol == vol else 0.0})
        if page == 1 and len(rows_out) < 15:
            break
    return rows_out


def fetch(now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    now = now or dt.datetime.now()
    c = _Client()
    data: Dict[str, Any] = {"fetched_at": now.isoformat(timespec="seconds"), "errors": []}
    try:
        data["kouaku"] = _fetch_kouaku(c, now)
    except Exception as e:
        data["kouaku"] = {"title": None, "reason": repr(e)[:200]}
        data["errors"].append("kouaku")
        logger.warning("[MATERIALS WATCH] kouaku fetch failed", exc_info=True)
    for kind in ("increase", "decrease"):
        try:
            data[f"pts_{kind}"] = _fetch_pts(c, kind)
        except Exception:
            data[f"pts_{kind}"] = []
            data["errors"].append(f"pts_{kind}")
            logger.warning("[MATERIALS WATCH] pts %s fetch failed", kind, exc_info=True)
    data["watch"] = _build_watch(data)
    return data


def _non_stock_symbols() -> set:
    """ETF/ETN/REIT/インフラファンド/TOKYO PRO Market (今の売買戦略では使わない) の銘柄コード。symbol_flags.db から。"""
    try:
        import sqlite3
        from nas_root import nas_path
        p = nas_path("Basic", "symbol_flags.db")
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT symbol, market_type, is_etf FROM symbol_flags").fetchall()
        finally:
            con.close()
        bad = ("ETF", "ETN", "REIT", "インフラ", "PRO")
        return {str(s) for s, mt, etf in rows if str(etf or "0") in ("1", "True") or any(b in str(mt or "") for b in bad)}
    except Exception:
        logger.debug("[MATERIALS WATCH] symbol_flags read failed", exc_info=True)
        return set()


def _build_watch(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    # 2026-10-03 (ユーザー判断): PTS は「変動率が大きく商いも多い」銘柄だけ。
    #   10/2 夜の実測で |変化率| >= 5%・株価 >= 200 円 のうち PTS 売買代金 >= 3,000 万円は 4 銘柄、>= 1,000 万円は 8 銘柄。
    #   売買代金はそこまで大きくなくてよい (ユーザー判断) → 1,000 万円。
    min_value = float(_env("MATERIALS_WATCH_PTS_MIN_VALUE_YEN", "10000000"))
    min_chg = float(_env("MATERIALS_WATCH_PTS_MIN_CHG_PCT", "5"))
    min_price = float(_env("MATERIALS_WATCH_PTS_MIN_PRICE", "200"))
    kou = data.get("kouaku") or {}
    tag: Dict[str, str] = {}
    for x in kou.get("good") or []:
        tag[x["symbol"]] = "好材料"
    for x in kou.get("bad") or []:
        tag.setdefault(x["symbol"], "悪材料")
    pts = [x for x in (data.get("pts_increase") or []) + (data.get("pts_decrease") or [])
           if abs(x.get("chg_pct") or 0) >= min_chg and (x.get("value_yen") or 0) >= min_value
           and (x.get("pts") or 0) >= min_price]
    pts.sort(key=lambda x: -abs(x["chg_pct"]))
    out: List[Dict[str, Any]] = []
    seen = set(_non_stock_symbols())
    for x in pts:
        if x["symbol"] in seen:
            continue
        seen.add(x["symbol"])
        out.append({"symbol": x["symbol"], "name": x["name"], "why": f"PTS {x['chg_pct']:+.1f}%" + (f"・{tag[x['symbol']]}" if x["symbol"] in tag else "")})
    for key, label in (("good", "好材料"), ("bad", "悪材料")):
        for x in kou.get(key) or []:
            if x["symbol"] not in seen:
                seen.add(x["symbol"])
                out.append({"symbol": x["symbol"], "name": x["name"], "why": label})
    return out


def _notify(data: Dict[str, Any]) -> None:
    try:
        from utils.alerts_util import send_discord_text
    except Exception:
        return
    kou = data.get("kouaku") or {}
    lines = ["📰 **今朝の材料株・夜間PTS** (9:00〜10:00 は優先して監視)"]
    if kou.get("title"):
        lines.append(f"{kou['title']}  好材料 {len(kou.get('good') or [])} / 悪材料 {len(kou.get('bad') or [])}")
    else:
        lines.append(f"好悪材料: 取得できず ({kou.get('reason')})")
    mx = int(_env("MATERIALS_WATCH_MAX", "15"))
    for i, w in enumerate(data.get("watch") or []):
        if i >= 25:
            lines.append(f"…ほか {len(data['watch']) - 25} 銘柄")
            break
        mark = "★" if i < mx else "・"
        lines.append(f"{mark}`{w['symbol']}` {w['name']}  {w['why']}")
    try:
        send_discord_text("\n".join(lines))
    except Exception:
        logger.debug("[MATERIALS WATCH] discord failed", exc_info=True)


def _run(day: dt.date) -> None:
    try:
        data = fetch()
        p = _path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        kou = data.get("kouaku") or {}
        logger.warning("[MATERIALS WATCH] saved %s kouaku=%s good=%s bad=%s pts_up=%s pts_down=%s watch=%s head=%s errors=%s",
                       p, kou.get("title") or kou.get("reason"), len(kou.get("good") or []), len(kou.get("bad") or []),
                       len(data.get("pts_increase") or []), len(data.get("pts_decrease") or []), len(data["watch"]),
                       [w["symbol"] for w in data["watch"][:15]], data["errors"])
        with _lock:
            _state["data"] = data
        _notify(data)
    except Exception:
        logger.warning("[MATERIALS WATCH] run failed", exc_info=True)
    finally:
        with _lock:
            _state["running"] = False


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def load_today(now: Optional[dt.datetime] = None) -> Optional[Dict[str, Any]]:
    """今日の結果 (無ければ裏で取得を始めて None)。待たない。"""
    if not enabled():
        return None
    now = now or dt.datetime.now()
    day = now.date()
    if now.weekday() >= 5 or now.time() < dt.time(7, 0):
        return None
    with _lock:
        if _state["day"] != day.isoformat():
            _state.update(day=day.isoformat(), data=None, last_try=0.0)
        if _state["data"] is not None:
            return _state["data"]
    p = _path(day)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            with _lock:
                _state["data"] = data
            return data
        except Exception:
            pass
    with _lock:
        if _state["running"] or time.time() - _state["last_try"] < 600:
            return None
        _state["running"] = True
        _state["last_try"] = time.time()
    threading.Thread(target=_run, args=(day,), name="materials-watch", daemon=True).start()
    return None


def watch_symbols(now: Optional[dt.datetime] = None) -> List[str]:
    """朝の優先監視銘柄 (MATERIALS_WATCH_START〜END の間だけ)。"""
    try:
        now = now or dt.datetime.now()
        data = load_today(now)
        if not data:
            return []
        if not (_hm(_env("MATERIALS_WATCH_START", "08:00")) <= now.time() < _hm(_env("MATERIALS_WATCH_END", "10:00"))):
            return []
        mx = int(_env("MATERIALS_WATCH_MAX", "15"))
        return [w["symbol"] for w in (data.get("watch") or [])][:mx]
    except Exception:
        logger.debug("[MATERIALS WATCH] watch_symbols failed", exc_info=True)
        return []


__all__ = ["fetch", "load_today", "watch_symbols", "enabled"]
