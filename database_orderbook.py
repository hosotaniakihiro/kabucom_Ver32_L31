# database_orderbook.py

from sqlalchemy import Column, Float, Integer, String, DateTime, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime
import os

Base = declarative_base()

# ✅ 日付付きファイル名
today_str = datetime.datetime.now().strftime("%Y%m%d")
db_filename = f"orderbook_{today_str}.db"
db_dir = "y:/trades"
db_path = f"sqlite:///{os.path.join(db_dir, db_filename)}"

# ✅ DB接続
engine = create_engine(db_path, connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)



# --- テーブル定義 ---
class OrderBook(Base):
    __tablename__ = 'orderbook'
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.now)
    symbol = Column(String)
    symbol_name = Column(String)
    current_price = Column(Float)
    vwap = Column(Float)
    bid_price = Column(Float)
    ask_price = Column(Float)

    # 板情報（Buy1〜Buy10、Sell1〜Sell10）
    buy1_price = Column(Float); buy1_qty = Column(Float)
    buy2_price = Column(Float); buy2_qty = Column(Float)
    buy3_price = Column(Float); buy3_qty = Column(Float)
    buy4_price = Column(Float); buy4_qty = Column(Float)
    buy5_price = Column(Float); buy5_qty = Column(Float)
    buy6_price = Column(Float); buy6_qty = Column(Float)
    buy7_price = Column(Float); buy7_qty = Column(Float)
    buy8_price = Column(Float); buy8_qty = Column(Float)
    buy9_price = Column(Float); buy9_qty = Column(Float)
    buy10_price = Column(Float); buy10_qty = Column(Float)

    sell1_price = Column(Float); sell1_qty = Column(Float)
    sell2_price = Column(Float); sell2_qty = Column(Float)
    sell3_price = Column(Float); sell3_qty = Column(Float)
    sell4_price = Column(Float); sell4_qty = Column(Float)
    sell5_price = Column(Float); sell5_qty = Column(Float)
    sell6_price = Column(Float); sell6_qty = Column(Float)
    sell7_price = Column(Float); sell7_qty = Column(Float)
    sell8_price = Column(Float); sell8_qty = Column(Float)
    sell9_price = Column(Float); sell9_qty = Column(Float)
    sell10_price = Column(Float); sell10_qty = Column(Float)

# --- 初期化 ---
Base.metadata.create_all(engine)


# --- 保存処理（表示なし） ---
def save_orderbook_to_db(data):
    session = Session()
    try:
        ob = OrderBook(
            symbol=data.get("Symbol"),
            symbol_name=data.get("SymbolName"),
            current_price=data.get("CurrentPrice"),
            vwap=data.get("VWAP"),
            bid_price=data.get("BidPrice"),
            ask_price=data.get("AskPrice"),
        )

        for i in range(1, 11):
            buy = data.get(f"Buy{i}", {})
            sell = data.get(f"Sell{i}", {})
            setattr(ob, f"buy{i}_price", buy.get("Price"))
            setattr(ob, f"buy{i}_qty", buy.get("Qty"))
            setattr(ob, f"sell{i}_price", sell.get("Price"))
            setattr(ob, f"sell{i}_qty", sell.get("Qty"))

        session.add(ob)
        session.commit()
        # 表示しない（ログも出さない）
    except Exception:
        session.rollback()
        # 完全非表示（loggingしたい場合はここに追加）
    finally:
        session.close()
