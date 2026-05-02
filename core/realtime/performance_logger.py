    # ========================================================
    # 🔥 確定処理
    # ========================================================

    def _finalize_tf(self, tf, symbol, bar):

        if not bar:
            return

        dt_bar = _safe_dt(bar.get("minute"))
        if not dt_bar:
            return

        finalize_key = (tf, symbol, dt_bar)
        if finalize_key in self._finalized_keys:
            return

        if len(self._finalized_keys) > self._finalized_limit:
            self._finalized_keys.clear()

        # ======================================================
        # 履歴取得（DuckDB対応）
        # ======================================================

        try:
            df_hist = global_data.get_multi_summary(tf)
            if df_hist is not None and not df_hist.empty:
                df_hist = df_hist[df_hist["symbol"] == symbol].copy()
            else:
                df_hist = pd.DataFrame()
        except Exception:
            df_hist = pd.DataFrame()

        if df_hist.empty:
            try:
                query = f"""
                    SELECT *
                    FROM stock_summary_{tf}min
                    WHERE symbol = '{symbol}'
                    ORDER BY datetime ASC
                """
                with get_summary_engine().connect() as conn:
                    df_hist = pd.read_sql(query, conn)
            except Exception:
                df_hist = pd.DataFrame()

        # ======================================================
        # SYMBOLNAME継承（三段）
        # ======================================================

        symbolname = None

        try:
            df_meta = global_data.get_merged_summary(1)
            if df_meta is not None and not df_meta.empty:
                meta = df_meta[df_meta["symbol"] == symbol].tail(1)
                if not meta.empty:
                    symbolname = meta.iloc[0].get("symbolname")
        except Exception:
            pass

        if not symbolname and df_hist is not None and not df_hist.empty:
            if "symbolname" in df_hist.columns:
                last_row = df_hist.tail(1)
                symbolname = last_row.iloc[0].get("symbolname")

        if not symbolname:
            symbolname = symbol

        # ======================================================
        # 新バー生成
        # ======================================================

        df_new = pd.DataFrame([{
            "symbol": symbol,
            "symbolname": symbolname,
            "datetime": dt_bar,
            "open_price": _safe_float(bar.get("open_price")),
            "high_price": _safe_float(bar.get("high_price")),
            "low_price": _safe_float(bar.get("low_price")),
            "close_price": _safe_float(bar.get("close_price")),
            "volume": _safe_float(bar.get("volume")),
            "source": "push",
        }])

        df_new["datetime"] = pd.to_datetime(df_new["datetime"], errors="coerce")
        df_new = df_new.dropna(subset=["datetime"])
        if df_new.empty:
            return

        df_new["date"] = df_new["datetime"].dt.date
        df_new["time"] = df_new["datetime"].dt.time
        df_new["start_time"] = df_new["time"]
        df_new["end_time"] = (
            df_new["datetime"] + pd.to_timedelta(tf, unit="m")
        ).dt.time

        df_new["time_range"] = (
            df_new["start_time"].astype(str)
            + "-"
            + df_new["end_time"].astype(str)
        )

        # ======================================================
        # 履歴結合
        # ======================================================

        df_all = pd.concat([df_hist, df_new], ignore_index=True)
        df_all["datetime"] = pd.to_datetime(df_all["datetime"], errors="coerce")
        df_all = df_all.dropna(subset=["datetime"])
        df_all = df_all.sort_values("datetime")

        try:
            df_all = add_all_indicators(df_all)
        except Exception:
            logger.exception("[HTF] indicator failed")
            return

        if df_all.empty:
            return

        df_save = df_all.tail(1).copy()

        # ======================================================
        # NaN完全防御
        # ======================================================

        for col in df_save.columns:

            if df_save[col].dtype.kind in {"f", "i"}:
                df_save[col] = (
                    df_save[col]
                    .replace([np.inf, -np.inf], np.nan)
                    .fillna(0.0)
                )
            else:
                df_save[col] = df_save[col].where(pd.notna(df_save[col]), None)

        # ======================================================
        # 保存
        # ======================================================

        try:
            bulk_upsert_summary(df_save, interval=tf)
        except Exception:
            logger.exception("[HTF] upsert failed")

        # ======================================================
        # merged同期
        # ======================================================

        try:
            current = global_data.get_merged_summary(tf)

            if current is None or current.empty:
                df_updated = df_save.copy()
            else:
                current = current.copy()
                current["datetime"] = pd.to_datetime(current["datetime"], errors="coerce")
                df_updated = pd.concat([current, df_save], ignore_index=True)

            df_updated = (
                df_updated
                .sort_values(["symbol", "datetime"])
                .drop_duplicates(["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )

            global_data.set_merged_summary(tf, df_updated)

        except Exception:
            logger.exception("[HTF] merged sync failed")

        if tf == 3:
            last_state.update_3m(dt_bar)
        else:
            last_state.update_5m(dt_bar)

        self._finalized_keys.add(finalize_key)

        logger.info(f"[{tf}M CONFIRMED] {symbol} {dt_bar}")

    # ============================================================
    # Backward compatibility
    # ============================================================

    def force_resample(self):
        try:
            if hasattr(self, "force_finalize"):
                return self.force_finalize()

            if hasattr(self, "force_time_based_finalize"):
                return self.force_time_based_finalize()

            return

        except Exception:
            logger.exception("[HTF] force_resample compatibility failed")


# ============================================================
# Singleton
# ============================================================

incremental_higher_tf_engine = IncrementalHigherTFEngine()