# 2026-06-16 Full pipeline stability fixes

## Goal

Apply the remaining fixes in this order:

1. Yahoo complement differential summary reflection
2. kabu Station token/API-key unification
3. Strict PUSH A/B rotation defaults
4. 3m/5m summary recovery defaults
5. Entry/exit scheduler congestion reduction

## Added runtime patch

`core/startup/full_pipeline_stability_runtime_patch.py`

This patch is loaded by `core/startup/runtime_env_defaults_patch.py` and logs:

```text
[FULL PIPELINE STABILITY] installed version=V1-FULL-PIPELINE-STABILITY
```

## Runtime defaults set

### Yahoo

- `YAHOO_LIVE_SKIP_PRE_DOWNLOAD_REFLECT=1`
- `YAHOO_LIVE_SKIP_EMPTY_DOWNLOAD_REFLECT=1`

This keeps post-download reflection, but skips expensive all-symbol reflection when no new data was downloaded.

### Token/API key

Wraps token-manager refresh/get functions and publishes refreshed token to:

- `KABU_API_KEY`
- `KABUSAPI_TOKEN`
- `X_API_KEY`
- `KABU_STATION_TOKEN`
- `global_data.headers["X-API-KEY"]` where available

### PUSH rotation

- `PUSH_ROTATION_STRICT_SEQUENCE=1`
- `PUSH_ROTATION_USE_UNREGISTER_ALL=1`
- `PUSH_ROTATION_REGISTER_SECONDS=4.8`
- `PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2`
- `PUSH_ROTATION_WAIT_AFTER_CLEAR_SEC=0.2`
- `PUSH_REGISTER_ABORT_IF_CLEAR_FAILED=1`
- `PUSH_REGISTER_RETRY_ON_REGIST_COUNT_ERROR=1`

### Summary recovery

- `SUMMARY_RECOVER_TF3_TF5_FROM_DB=1`
- `SUMMARY_RECOVER_EMPTY_TF_FROM_LAST_GOOD=1`
- `SUMMARY_KEEP_LAST_GOOD_CONTEXT=1`
- `SUMMARY_DB_BACKFILL_FOR_MTF=1`
- `ENTRY_ORDER_REQUIRE_MTF_DATA=0`
- `ENTRY_SHORT_MTF_MIN_AVAILABLE=1`
- `ENTRY_SHORT_MTF_MIN_ALIGNED=1`

### Entry/exit congestion

- `ENTRY_BOARD_WAIT_SEC=0.2`
- `ENTRY_ORDER_BUILD_TIMEOUT_SEC=4.0`
- `ENTRY_PASS_TIMEOUT_SEC=12.0`
- `RANKING_ENTRY_BUILD_TIMEOUT_SEC=12.0`
- `TONOSAMA_ENTRY_TIMEOUT_SEC=12.0`
- `ENTRY_CONTINUE_NEXT_ON_TIMEOUT=1`
- `EXIT_LOOP_SKIP_HEAVY_WHEN_NO_POSITION=1`

## Confirmation logs

`main.py` / `main_database.py` startup should include:

```text
[RUNTIME ENV DEFAULTS PATCH] installed ... full_pipeline=True
[FULL PIPELINE STABILITY] installed version=V1-FULL-PIPELINE-STABILITY
```

`main.py` should still show DB-save skip from the previous database-owner patch:

```text
[PushStorage] start skipped in main process
[summary.runners] DB save skipped ... reason=summary_save_owner_gate
```
