# ============================================================
# AI/mtf_shadow/build_dataset.py
# ------------------------------------------------------------
# TradingView CSV (Y:\tv_csv2) → 時間足ごとの学習データ。
#
#   python -m AI.mtf_shadow.build_dataset --src Y:\tv_csv2 --tf 1 3 5
#
# - ファイル名は "TSE_DLY_7203, 5.csv"。"TSE_DLY_7203, 5 (1).csv" のような
#   重複は期間が違うことがある (例: 2040 は重複の方が 1 か月長い) ので、
#   同じ銘柄・同じ足を結合し、同じ時刻は 1 本にする。
# - 指標は場中と同じ trading.summary.mtf_local_bars._add_indicators で計算する。
# - 先頭 75 本は MA75 が揃わないので捨てる。
# - 出力: <out>/dataset_{tf}m.pkl (特徴量 + y + fwd_ret + symbol + datetime)
#
# NAS を長く読むので場中は走らせない (SMB が劣化すると稼働中の main.py を巻き込む)。
# ============================================================
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from AI.mtf_shadow.features import build_features, build_fwd_return, build_label  # noqa: E402
from trading.summary.mtf_local_bars import _add_indicators  # noqa: E402

_NAME_RE = re.compile(r"^TSE_DLY_(.+?), (\d+[A-Z]?)(?: \(\d+\))?\.csv$")
WARMUP_BARS = 75


def default_out_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "autostock" / "ai_shadow"


def list_files(src: Path, tf: int) -> dict[str, list[Path]]:
    files: dict[str, list[Path]] = defaultdict(list)
    for name in os.listdir(src):
        m = _NAME_RE.match(name)
        if m and m.group(2) == str(tf):
            files[m.group(1)].append(src / name)
    return files


def read_tv_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=lambda c: c.strip().lower() in {"time", "open", "high", "low", "close"})
    df.columns = [c.strip().lower() for c in df.columns]
    t = pd.to_datetime(df["time"], errors="coerce", utc=True)
    df["datetime"] = t.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
    return df.drop(columns=["time"]).dropna()


def symbol_rows(args) -> pd.DataFrame | None:
    symbol, paths, tf = args
    try:
        parts = [read_tv_csv(p) for p in paths]
        df = pd.concat(parts, ignore_index=True)
        df = df.sort_values("datetime").drop_duplicates("datetime", keep="last")
        hm = df["datetime"].dt.hour * 100 + df["datetime"].dt.minute
        df = df[(hm >= 900) & (hm <= 1530)].reset_index(drop=True)
        if len(df) <= WARMUP_BARS + 20:
            return None
        df["volume"] = 0.0
        bars = _add_indicators(df)
        feats = build_features(bars)
        feats["y"] = build_label(bars, tf).astype("float32")
        feats["fwd_ret"] = build_fwd_return(bars, tf).astype("float32")
        feats["symbol"] = symbol
        feats["datetime"] = bars["datetime"].values
        feats = feats.iloc[WARMUP_BARS:]
        return feats[feats["y"].notna()]
    except Exception as exc:  # 壊れた CSV は飛ばす
        print(f"skip {symbol} tf={tf}: {type(exc).__name__}: {exc}", flush=True)
        return None


def build(src: Path, tf: int, out_dir: Path, workers: int) -> Path:
    files = list_files(src, tf)
    print(f"[tf={tf}] symbols={len(files)} files={sum(len(v) for v in files.values())}", flush=True)
    jobs = [(s, p, tf) for s, p in sorted(files.items())]
    t0 = time.time()
    parts = []
    with Pool(workers, maxtasksperchild=200) as pool:
        for i, r in enumerate(pool.imap_unordered(symbol_rows, jobs, chunksize=8), 1):
            if r is not None and not r.empty:
                parts.append(r)
            if i % 500 == 0:
                print(f"[tf={tf}] {i}/{len(jobs)} {time.time() - t0:.0f}s", flush=True)
    df = pd.concat(parts, ignore_index=True)
    df["symbol"] = df["symbol"].astype("category")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"dataset_{tf}m.pkl"
    df.to_pickle(out)
    print(f"[tf={tf}] rows={len(df):,} symbols={df['symbol'].nunique()} "
          f"{df['datetime'].min()} 〜 {df['datetime'].max()} -> {out} ({time.time() - t0:.0f}s)", flush=True)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"Y:\tv_csv2")
    ap.add_argument("--tf", type=int, nargs="+", default=[1, 3, 5])
    ap.add_argument("--out", default=str(default_out_dir()))
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    for tf in a.tf:
        build(Path(a.src), tf, Path(a.out), a.workers)


if __name__ == "__main__":
    main()
