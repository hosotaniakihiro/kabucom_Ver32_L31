# ============================================================
# ops/themes/fetch_kabutan_themes.py
# 2026-10-06: 株探の個別銘柄ページから「テーマ」を集め、銘柄 -> テーマ の対応表を作る。
#   用途: テーマ連動 (同じテーマの銘柄がまとめて買われる日) の検知と検証。
#   10/6 は情報漏洩のニュースでセキュリティ関連 (4493 / 3042 / 3692 ...) がまとめて上げた。
# 使い方:
#   venv\Scripts\python.exe ops\themes\fetch_kabutan_themes.py              # backtest.db の銘柄すべて
#   venv\Scripts\python.exe ops\themes\fetch_kabutan_themes.py 3042 4493    # 指定銘柄だけ
# 出力: %LOCALAPPDATA%\autostock\themes\stock_themes.csv (symbol, theme)。途中で止めても続きから取る。
# 株探への負荷を抑えるため 1.2 秒に 1 回。Norton の TLS 傍受があるので certs の CA バンドルで検証する。
# ============================================================
import csv
import os
import re
import sqlite3
import sys
import time
import urllib.parse as up

import requests

os.environ.pop("SSLKEYLOGFILE", None)   # Norton の SSLKEYLOGFILE で HTTPS が固まる対策
LAD = os.path.expandvars(r"%LOCALAPPDATA%\autostock")
OUT = os.path.join(LAD, "themes", "stock_themes.csv")
CA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "certs", "ca_with_windows_store.pem")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
INTERVAL = 1.2


def universe():
    db = os.path.join(LAD, "backtest", "backtest.db")
    c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    try:
        return sorted(r[0] for r in c.execute("select distinct symbol from rank1m"))
    finally:
        c.close()


def themes_of(sess, code):
    r = sess.get(f"https://kabutan.jp/stock/?code={code}", headers=UA, timeout=15, verify=CA if os.path.exists(CA) else True)
    if r.status_code != 200:
        return None
    return [up.unquote(x) for x in dict.fromkeys(re.findall(r'/themes/\?theme=([^"&\']+)', r.text))]


def main(codes):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    done = set()
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8-sig") as f:
            done = {row["symbol"] for row in csv.DictReader(f)}
    todo = [c for c in codes if c not in done]
    print(f"todo {len(todo)} / done {len(done)}", flush=True)
    new = not os.path.exists(OUT)
    sess = requests.Session()
    with open(OUT, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["symbol", "theme"])
        for i, code in enumerate(todo):
            try:
                th = themes_of(sess, code)
            except Exception as e:
                print(code, "error", e, flush=True)
                time.sleep(5)
                continue
            if th is None:
                print(code, "http error", flush=True)
            else:
                # テーマが無い銘柄も「取得済み」と分かるよう空のテーマで 1 行書く
                for t in (th or [""]):
                    w.writerow([code, t])
                f.flush()
            if i % 50 == 0:
                print(i, len(todo), code, th[:3] if th else th, flush=True)
            time.sleep(INTERVAL)
    print("->", OUT)


if __name__ == "__main__":
    main(sys.argv[1:] or universe())
