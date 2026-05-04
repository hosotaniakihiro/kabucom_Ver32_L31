import pandas as pd
from sqlalchemy import create_engine

# DBファイルのパス（必要に応じて変更）
engine = create_engine("sqlite:///y:/trades/trade_log.db")

# 取引履歴読み込み
df = pd.read_sql("SELECT * FROM positions", engine)

# CSV出力先
output_path = "y:/trades/trade_history_export.csv"

# CSVに保存
df.to_csv(output_path, index=False, encoding="utf-8-sig")
print(f"✅ 取引履歴をCSVに出力しました → {output_path}")
