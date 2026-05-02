from trading.entry.entry_event_saver import save_entry_event

save_entry_event(
    symbol="TEST1",
    side="BUY",
    entry_price=1000.0,
    interval=1,
    score=25.0,
    features={
        "ret": 0.002,
        "vol_ratio": 1.5,
    },
    meta={
        "source": "TEST",
        "confidence": 0.82,
        "dominant_ratio": 0.67,
        "model_used": "debug_model",
        "ai_pred": 0.74,
        "ai_threshold": 0.7,
        "ai_pass": True,
        "pred_hold_seconds": 120,
        "index_shock": 0,
    },
)
