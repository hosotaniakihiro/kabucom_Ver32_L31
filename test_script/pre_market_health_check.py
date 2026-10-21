# -*- coding: utf-8 -*-
"""寄り付き前 / 本番再起動後の read-only ヘルスチェック。

使い方:
    .venv\\Scripts\\python.exe test_script\\pre_market_health_check.py
    .venv\\Scripts\\python.exe test_script\\pre_market_health_check.py --json

このスクリプトは **一切の副作用を持たない**:
  * 発注・取消をしない
  * PUSH register / unregister をしない
  * DB へ書き込まない (SELECT のみ)
  * Discord へ送信しない (TLS ハンドシェイクの確認だけ)
  * /token を発行しない (settings.ini の token を読むだけ)

FAIL が 1 つでもあれば終了コード 1 を返し、
「AUTO TRADE ENABLE 禁止」と表示する。
"""
from __future__ import annotations

import argparse
import configparser
import datetime as dt
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

_results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))


def _safe(fn, name, *, warn_on_error: bool = False):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        record(name, WARN if warn_on_error else FAIL, "例外: %s: %s" % (type(e).__name__, e))


# ------------------------------------------------------------
# 共通
# ------------------------------------------------------------

def _settings():
    conf = configparser.ConfigParser(interpolation=None)
    conf.read(str(ROOT / "settings.ini"), encoding="utf-8")
    return conf


def _token() -> str:
    conf = _settings()
    for sec in ("aukabu", "kabusapi"):
        if conf.has_section(sec) and conf.has_option(sec, "token"):
            return str(conf.get(sec, "token") or "").strip()
    return ""


def _nas_root() -> Path:
    try:
        from nas_root import nas_root

        return Path(str(nas_root()))
    except Exception:
        return Path(r"\\192.168.0.22\AutoStockBuyAndSell")


def _today() -> str:
    return dt.date.today().strftime("%Y%m%d")


def _kabu_dir(*parts) -> Path:
    return _nas_root().joinpath("raw_data", "kabu_station", *parts)


def _ro_conn(path: Path):
    """読み取り専用で開く。UNC パスは file: URI の書式が特殊なので分岐する。"""
    p = str(path)
    if p.startswith("\\\\") or p.startswith("//"):
        # \server\share.db -> file:////server/share/a.db
        uri = "file:" + "//" + p.replace("\\", "/")
    else:
        uri = "file:/" + p.replace("\\", "/").lstrip("/")
    return sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5.0)


def _age_sec(ts) -> float | None:
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "").split("+")[0])
        return max(0.0, (dt.datetime.now() - t).total_seconds())
    except Exception:
        return None


def _log_dirs() -> list:
    """ログを探すディレクトリ。ローカル (現行の書き先) を先に見る。

    2026-09-05: 書き先を LOG_DIR (既定でローカル) へ移した。NAS 側には
    ミラーが数分遅れで届くので、鮮度を見る判定 (14 FAST PATH / 15 Entry
    pipeline) はローカルを見ないと古い内容で判定してしまう。
    """
    dirs = []
    try:
        from data_collectors.config import LOG_DIR

        dirs.append(Path(LOG_DIR))
    except Exception:
        pass
    dirs.append(_nas_root() / "Logs" / "console")
    return dirs


def _newest_log(pattern: str) -> Path | None:
    hits: list = []
    for d in _log_dirs():
        try:
            hits.extend(glob.glob(str(d / pattern)))
        except Exception:
            continue
    logs = sorted(hits, key=lambda p: os.path.getmtime(p), reverse=True)
    return Path(logs[0]) if logs else None


def _tail_text(path: Path, max_bytes: int = 3_000_000) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", "replace")


def _market_hours() -> bool:
    now = dt.datetime.now()
    if now.weekday() >= 5:
        return False
    hhmm = now.hour * 100 + now.minute
    return (900 <= hhmm <= 1130) or (1230 <= hhmm <= 1530)


# ------------------------------------------------------------
# 1. kabu Station API 疎通 (read-only)
# ------------------------------------------------------------

def check_api_reachable() -> None:
    import requests

    tok = _token()
    if not tok:
        record("01 kabu API 疎通", FAIL, "settings.ini に token が無い")
        return
    hdr = {"Content-Type": "application/json", "X-API-KEY": tok}
    detail = []
    ok = True
    for ep in ("/positions", "/wallet/cash"):
        t0 = time.monotonic()
        try:
            r = requests.get("http://localhost:18080/kabusapi" + ep, headers=hdr, timeout=8)
            el = time.monotonic() - t0
            detail.append("%s=%s(%.2fs)" % (ep, r.status_code, el))
            if r.status_code != 200:
                ok = False
        except Exception as e:  # noqa: BLE001
            detail.append("%s=%s" % (ep, type(e).__name__))
            ok = False
    record("01 kabu API 疎通", PASS if ok else FAIL, " ".join(detail))


# ------------------------------------------------------------
# 2. ranking raw_rows > 0 (DB 書き込みなし)
# ------------------------------------------------------------

def check_ranking_collect() -> None:
    from trading.ranking import api_circuit, api_client

    api_circuit.reset_for_test()

    # 28 回のフルサイクルは重く API も叩きすぎるので、
    # ここでは client 経路が生きているかを 1 回で確かめる。
    # 実際の収集結果は 03 (ranking_snapshot_1min) で確認する。
    t0 = time.monotonic()
    data = api_client.get_data_from_api({"type": 1, "ExchangeDivision": "ALL"})
    el = time.monotonic() - t0
    outcome, _d, status = api_client.last_outcome()
    n = len((data or {}).get("Ranking") or []) if isinstance(data, dict) else 0
    detail = "raw_rows=%d elapsed=%.1fs outcome=%s status=%s" % (n, el, outcome, status)
    if n > 0:
        record("02 ranking API rows>0", PASS, detail)
    elif outcome == api_client.OUTCOME_RATE_LIMITED:
        record("02 ranking API rows>0", WARN, detail + " (429=流量制御。少し待って再実行)")
    else:
        record("02 ranking API rows>0", FAIL, detail)


# ------------------------------------------------------------
# 3-4. ranking_snapshot_1min
# ------------------------------------------------------------

def _ranking_db_path() -> Path:
    """ranking DB の場所。ローカル (writer の書き先) を優先する。

    2026-09-10: ここは NAS 固定だった。ranking DB は 2026-09-07 に
    ローカル書き + NAS 複製へ移っており、NAS への複製は既定で 30 分に
    1 回しか走らない。そのため寄り後すぐに実行すると NAS 側が 0 行で、
    「03 ranking_snapshot_1min rows>0 が FAIL」という誤判定になる。
    実測 (2026-09-10 09:07):
        ローカル raw=9,350 / snapshot=8,949   (正常に溜まっている)
        NAS      raw=0     / snapshot=0       (まだ複製されていない)
    このチェックは「自動売買を有効化してよいか」の判断材料なので、
    誤 FAIL は害がある。
    """
    try:
        from trading.ranking import ranking_db_location

        p = Path(ranking_db_location.ranking_db_path())
        if p.exists():
            return p
    except Exception:
        pass
    return _kabu_dir("ranking") / ("ranking%s.db" % _today())


def check_ranking_db() -> None:
    path = _ranking_db_path()
    if not path.exists():
        record("03 ranking_snapshot_1min rows>0", FAIL, "DB が無い: %s" % path)
        record("04 ranking 最新時刻 fresh", FAIL, "DB が無い")
        return
    con = _ro_conn(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ranking_snapshot_1min" not in tables:
            record("03 ranking_snapshot_1min rows>0", FAIL, "テーブルが無い tables=%s" % sorted(tables))
            record("04 ranking 最新時刻 fresh", FAIL, "テーブルが無い")
            return
        rows = con.execute("SELECT COUNT(*) FROM ranking_snapshot_1min").fetchone()[0]
        record("03 ranking_snapshot_1min rows>0", PASS if rows > 0 else FAIL, "rows=%d" % rows)

        latest = None
        for col in ("datetime", "created_at", "inserted_at"):
            try:
                latest = con.execute(
                    "SELECT MAX(%s) FROM ranking_snapshot_1min" % col
                ).fetchone()[0]
                if latest:
                    break
            except Exception:
                continue
        age = _age_sec(latest) if latest else None
        if age is None:
            record("04 ranking 最新時刻 fresh", WARN, "最新時刻を判定できない latest=%r" % latest)
        elif age <= 300:
            record("04 ranking 最新時刻 fresh", PASS, "latest=%s age=%.0fs" % (latest, age))
        else:
            st = FAIL if _market_hours() else WARN
            record("04 ranking 最新時刻 fresh", st, "latest=%s age=%.0fs" % (latest, age))
    finally:
        con.close()


# ------------------------------------------------------------
# 5-7. PUSH heartbeat
# ------------------------------------------------------------

def _heartbeat_path(name: str):
    """heartbeat の置き場所。

    2026-09-05: 書き先を LOG_DIR (既定でローカル) へ移した。ここを NAS 固定に
    しておくと、月曜に 05-08 が「heartbeat が無い」で全部 FAIL する。
    NAS 側に古いファイルが残っている場合もあるので、ローカルを先に見る。
    """
    candidates = []
    try:
        from data_collectors.config import LOG_DIR

        candidates.append(Path(LOG_DIR) / name)
    except Exception:
        pass
    candidates.append(_nas_root() / "Logs" / "console" / name)
    for c in candidates:
        try:
            if c.exists():
                return c
        except Exception:
            continue
    return candidates[0]


def check_push_health() -> None:
    hb = _heartbeat_path("push_owner_health.heartbeat.json")
    if not hb.exists():
        record("05 PUSH websocket connected", FAIL, "heartbeat が無い")
        record("06 reconnect storm でない", FAIL, "heartbeat が無い")
        record("07 confirmed_registered>0", FAIL, "heartbeat が無い")
        record("08 PUSH DB 最新が fresh", FAIL, "heartbeat が無い")
        return

    data = json.loads(hb.read_text(encoding="utf-8"))
    hb_age = _age_sec(data.get("time"))
    if hb_age is not None and hb_age > 120:
        record("05 PUSH websocket connected", FAIL,
               "heartbeat が古い age=%.0fs (push_receiver が停止している可能性)" % hb_age)
    else:
        connected = bool(data.get("ws_connected"))
        record("05 PUSH websocket connected", PASS if connected else FAIL,
               "ws_connected=%s uptime=%.0fs last_recv_age=%s"
               % (connected, data.get("uptime_sec") or 0, data.get("last_recv_age")))

    status = str(data.get("health_status") or "")
    errs = int(data.get("error_total") or 0)
    if status == "RECONNECT_STORM":
        record("06 reconnect storm でない", FAIL, "health_status=RECONNECT_STORM error_total=%d" % errs)
    elif status in ("HEALTHY", ""):
        record("06 reconnect storm でない", PASS, "health_status=%s error_total=%d" % (status or "-", errs))
    else:
        record("06 reconnect storm でない", WARN, "health_status=%s error_total=%d" % (status, errs))

    # confirmed_registered: registration_state の永続ファイル -> heartbeat の順で見る
    reg = None
    try:
        from trading.push import registration_state as rs

        sp = Path(rs.state_path())
        if sp.exists():
            reg = len(json.loads(sp.read_text(encoding="utf-8")).get("last_good_registered_symbols") or [])
    except Exception:
        reg = None
    if reg is None:
        reg = data.get("registered_count")
    if isinstance(reg, int) and reg > 0:
        record("07 confirmed_registered>0", PASS, "registered=%d" % reg)
    else:
        record("07 confirmed_registered>0", WARN,
               "registered=%r (未起動直後は空。数分後に再確認)" % (reg,))

    latest = data.get("db_max_datetime")
    age = _age_sec(latest)
    if age is None:
        record("08 PUSH DB 最新が fresh", WARN, "db_max_datetime=%r" % latest)
    elif age <= 180:
        record("08 PUSH DB 最新が fresh", PASS, "latest=%s age=%.0fs" % (latest, age))
    else:
        record("08 PUSH DB 最新が fresh", FAIL if _market_hours() else WARN,
               "latest=%s age=%.0fs" % (latest, age))


# ------------------------------------------------------------
# 9-13. summary DB
# ------------------------------------------------------------

def check_summary_db() -> None:
    path = _kabu_dir("summary") / ("summary%s.db" % _today())
    labels = {
        1: ("09 summary 1min rows>0", "10 summary 1min latest fresh"),
        3: ("11 summary 3min rows>0", None),
        5: ("12 summary 5min rows>0", None),
    }
    if not path.exists():
        for iv, (rows_label, fresh_label) in labels.items():
            record(rows_label, FAIL, "DB が無い: %s" % path)
            if fresh_label:
                record(fresh_label, FAIL, "DB が無い")
        record("13 technical_ready>0", FAIL, "DB が無い")
        return

    con = _ro_conn(path)
    try:
        ready_total = 0
        for iv, (rows_label, fresh_label) in labels.items():
            table = "stock_summary_%dmin" % iv
            try:
                rows = con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            except Exception as e:  # noqa: BLE001
                record(rows_label, FAIL, "%s 読み取り失敗: %s" % (table, e))
                if fresh_label:
                    record(fresh_label, FAIL, "読み取り失敗")
                continue
            record(rows_label, PASS if rows > 0 else FAIL, "%s rows=%d" % (table, rows))

            if fresh_label:
                latest = con.execute("SELECT MAX(datetime) FROM %s" % table).fetchone()[0]
                age = _age_sec(latest)
                if age is None:
                    record(fresh_label, WARN, "latest=%r" % latest)
                elif age <= 300:
                    record(fresh_label, PASS, "latest=%s age=%.0fs" % (latest, age))
                else:
                    record(fresh_label, FAIL if _market_hours() else WARN,
                           "latest=%s age=%.0fs" % (latest, age))
            try:
                cols = {r[1] for r in con.execute('PRAGMA table_info("%s")' % table)}
                if "technical_ready" in cols:
                    ready_total += con.execute(
                        "SELECT COUNT(*) FROM %s WHERE technical_ready IN (1,'1','True','true')" % table
                    ).fetchone()[0]
            except Exception:
                pass
        record("13 technical_ready>0", PASS if ready_total > 0 else FAIL,
               "technical_ready 行数=%d (1m+3m+5m)" % ready_total)
    finally:
        con.close()


# ------------------------------------------------------------
# 14-15, 17. ログから判定
# ------------------------------------------------------------

def check_logs() -> None:
    main_log = _newest_log("console_*.log")
    if main_log is None:
        record("14 FAST PATH 利用可能", WARN, "main.py のログが見つからない")
        record("15 Entry pipeline 稼働", WARN, "main.py のログが見つからない")
        record("17 REST coordinator 正常", WARN, "ログが見つからない")
        return

    text = _tail_text(main_log)

    hits = text.count("[FAST_PATH_DECISION] tick=") and text.count("result=HIT")
    misses = text.count("result=MISS")
    if hits:
        record("14 FAST PATH 利用可能", PASS, "HIT=%d MISS=%d (%s)" % (hits, misses, main_log.name))
    elif misses:
        record("14 FAST PATH 利用可能", FAIL,
               "HIT=0 MISS=%d -> 1分足 fresh projection が作れていない" % misses)
    else:
        record("14 FAST PATH 利用可能", WARN, "FAST_PATH_DECISION のログがまだ無い")

    audits = text.count("[ENTRY READY AUDIT]")
    gates = text.count("[SUMMARY AI GATE] SEND_TO_AI start")
    if gates or audits:
        record("15 Entry pipeline 稼働", PASS, "SEND_TO_AI=%d ENTRY_READY_AUDIT=%d" % (gates, audits))
    else:
        record("15 Entry pipeline 稼働", WARN,
               "AI へ渡した形跡が無い (候補が無いだけの可能性もある)")

    if "[REST COORD] degraded" in text:
        tail = text[text.rfind("[REST COORD"):][:200].replace("\n", " ")
        recovered = text.rfind("[REST COORD] recovered") > text.rfind("[REST COORD] degraded")
        record("17 REST coordinator 正常", PASS if recovered else WARN,
               ("degraded から復帰済み" if recovered else "degraded のまま: %s" % tail))
    else:
        record("17 REST coordinator 正常", PASS, "degraded の記録なし")


# ------------------------------------------------------------
# 16. Discord TLS (送信しない)
# ------------------------------------------------------------

def check_discord_tls() -> None:
    import requests

    from utils import alerts_util

    verify = alerts_util._resolve_ca_bundle()
    if verify is False:
        record("16 Discord TLS 疎通", FAIL, "verify=False になっている")
        return
    try:
        r = requests.get("https://discord.com/api/v10/gateway", timeout=8, verify=verify)
        record("16 Discord TLS 疎通", PASS if r.status_code == 200 else WARN,
               "status=%s verify=%s" % (r.status_code, verify))
    except Exception as e:  # noqa: BLE001
        record("16 Discord TLS 疎通", WARN,
               "%s: %s (アラート通知のみ影響。売買には影響しない)" % (type(e).__name__, str(e)[:120]))


# ------------------------------------------------------------
# main
# ------------------------------------------------------------

CHECKS = [
    (check_api_reachable, "01 kabu API 疎通"),
    (check_ranking_collect, "02 ranking API rows>0"),
    (check_ranking_db, "03/04 ranking DB"),
    (check_push_health, "05-08 PUSH"),
    (check_summary_db, "09-13 summary"),
    (check_logs, "14/15/17 ログ"),
    (check_discord_tls, "16 Discord TLS"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="JSON で出力する")
    args = ap.parse_args()

    started = dt.datetime.now()
    for fn, label in CHECKS:
        _safe(fn, label)

    fails = [r for r in _results if r[1] == FAIL]
    warns = [r for r in _results if r[1] == WARN]

    if args.json:
        print(json.dumps({
            "checked_at": started.isoformat(timespec="seconds"),
            "results": [{"name": n, "status": s, "detail": d} for n, s, d in _results],
            "fail": len(fails), "warn": len(warns),
            "auto_trade_enable_allowed": not fails,
        }, ensure_ascii=False, indent=2))
        return 1 if fails else 0

    print("=" * 78)
    print("寄り付き前ヘルスチェック  %s  (read-only)" % started.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)
    for name, status, detail in sorted(_results):
        print("  [%-4s] %-34s %s" % (status, name, detail))
    print("-" * 78)
    print("  PASS=%d  WARN=%d  FAIL=%d"
          % (len(_results) - len(fails) - len(warns), len(warns), len(fails)))
    print("=" * 78)
    if fails:
        print("*** AUTO TRADE ENABLE 禁止 ***")
        print("FAIL 項目:")
        for name, _s, detail in fails:
            print("  - %s : %s" % (name, detail))
        return 1
    if warns:
        print("AUTO TRADE ENABLE 可 (ただし WARN を確認すること)")
    else:
        print("AUTO TRADE ENABLE 可")
    return 0


if __name__ == "__main__":
    sys.exit(main())
