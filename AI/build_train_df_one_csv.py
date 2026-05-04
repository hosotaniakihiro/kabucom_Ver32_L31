# ============================================================
# AI/build_train_df_one_csv.py
# ------------------------------------------------------------
# ✔ 1 CSV 専用処理
# ✔ 絶対に親を止めない
# ✔ 不正CSVは静かにスキップ
# ============================================================

import sys
import pandas as pd
from pathlib import Path
import gc
import traceback

from build_train_df_from_tv_csv import (
    parse_filename,
    normalize_volume,
    add_features,
    add_label,
    OUT_DIR,
)

path = Path(sys.argv[1])

try:
    parsed = parse_filename(path)
    if not parsed:
        sys.exit(0)

    symbol, timeframe, unit = parsed
    print(f"📄 [child] load {path.name}")

    out_path = OUT_DIR / f"train_{timeframe}.csv"
    write_header = not out_path.exists()

    with open(path, "rb") as rf, open(out_path, "ab") as wf:
        for chunk in pd.read_csv(rf, chunksize=200_000):

            chunk.columns = [c.lower() for c in chunk.columns]

            # --------------------------------------------
            # 必須カラムチェック（★超重要）
            # --------------------------------------------
            required = {"time", "open", "high", "low", "close"}
            if not required.issubset(chunk.columns):
                print(f"⚠ skip invalid columns: {path.name}")
                sys.exit(0)

            chunk = normalize_volume(chunk)

            if len(chunk) < 10:
                print(f"⚠ skip too small: {path.name}")
                sys.exit(0)

            chunk["datetime"] = pd.to_datetime(chunk["time"], errors="coerce")
            chunk = chunk.dropna(subset=["datetime"])

            chunk["symbol"] = symbol
            chunk["timeframe"] = timeframe

            chunk = add_features(chunk)
            chunk = add_label(chunk, unit)
            chunk = chunk.dropna()

            if chunk.empty:
                sys.exit(0)

            chunk["symbol_id"] = (
                chunk["symbol"].astype("category").cat.codes
            )

            chunk.to_csv(
                wf,
                header=write_header,
                index=False,
            )
            write_header = False

            del chunk
            gc.collect()

except Exception:
    # ★ どんな例外でも親は止めない
    print(f"❌ [child] error skip: {path.name}")
    traceback.print_exc(limit=1)
    sys.exit(0)

sys.exit(0)
