import json

from app.core.executed_tokens_tracker import ExecutedTokensTracker


def test_executed_tokens_persistence_roundtrip(tmp_path):
    state_path = tmp_path / "executed_tokens_state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "labels": {
                    "NIFTY_CE_1": {
                        "last_ltp": 123.45,
                        "last_ltp_ts": "2026-03-05T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    tracker = ExecutedTokensTracker(
        persist_enabled=True,
        state_path=str(state_path),
        flush_interval_s=10.0,
    )
    tracker.bootstrap_from_positions(
        {"NIFTY_CE_1": {"symboltoken": "111"}},
        {"NIFTY_CE_1": {"token": "111", "exchangeType": 1}},
    )

    assert tracker.get_last_ltp("NIFTY_CE_1") == 123.45
