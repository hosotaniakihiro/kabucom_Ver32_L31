def store_pushdata(content: dict, retries: int = 5, delay: float = 0.5):

    now_dt = dt.datetime.now()

    payload = {
        # --------------------------------------------------
        # 既存保存（削除ゼロ）
        # --------------------------------------------------
        "time": now_dt.isoformat(timespec="seconds"),
        "content": json.dumps(content, ensure_ascii=False),
        "symbol": content.get("Symbol"),
        "symbolname": content.get("SymbolName") or content.get("symbolname"),
        "current_price": content.get("CurrentPrice"),
        "trading_volume": content.get("TradingVolume"),
        "trading_value": content.get("TradingValue"),
        "vwap": content.get("VWAP"),
        "previous_close": content.get("PreviousClose"),
        "high_price": content.get("HighPrice"),
        "high_price_time": content.get("HighPriceTime"),
        "low_price": content.get("LowPrice"),
        "low_price_time": content.get("LowPriceTime"),
        "bid_price": content.get("BidPrice"),
        "bid_qty": content.get("BidQty"),
        "ask_price": content.get("AskPrice"),
        "ask_qty": content.get("AskQty"),
        "current_price_time": content.get("CurrentPriceTime"),
        "previous_close_time": content.get("PreviousCloseTime"),

        # --------------------------------------------------
        # 🔥 incremental 用互換列（追加）
        # --------------------------------------------------
        "datetime": now_dt,
        "price": content.get("CurrentPrice"),
        "volume": content.get("TradingVolume"),
    }

    sql = text("""
        INSERT INTO stream_data (
            time,
            content,
            symbol,
            symbolname,
            current_price,
            trading_volume,
            trading_value,
            vwap,
            previous_close,
            high_price,
            high_price_time,
            low_price,
            low_price_time,
            bid_price,
            bid_qty,
            ask_price,
            ask_qty,
            current_price_time,
            previous_close_time,

            -- 🔥 incremental互換列
            datetime,
            price,
            volume
        ) VALUES (
            :time,
            :content,
            :symbol,
            :symbolname,
            :current_price,
            :trading_volume,
            :trading_value,
            :vwap,
            :previous_close,
            :high_price,
            :high_price_time,
            :low_price,
            :low_price_time,
            :bid_price,
            :bid_qty,
            :ask_price,
            :ask_qty,
            :current_price_time,
            :previous_close_time,

            :datetime,
            :price,
            :volume
        )
    """)

    for i in range(retries):

        session = Session_push()

        try:
            session.execute(sql, payload)
            session.commit()
            return True

        except OperationalError as e:
            session.rollback()

            if "database is locked" in str(e).lower():
                logger.warning(
                    "[PUSH][RETRY %d/%d] database is locked",
                    i + 1,
                    retries
                )
                time.sleep(delay)
            else:
                logger.exception("[PUSH] OperationalError")
                return False

        except Exception:
            session.rollback()
            logger.exception("[PUSH] 保存エラー")
            return False

        finally:
            session.close()

    logger.error("[PUSH] 保存失敗: retry 超過 symbol=%s", payload["symbol"])
    return False
