import logging

from app.core.executed_tokens_tracker import ExecutedTokensTracker


def test_executed_tokens_persistence_corrupt_file_warns_and_continues(tmp_path, caplog):
    state_path = tmp_path / "executed_tokens_state.json"
    state_path.write_text("{bad json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        tracker = ExecutedTokensTracker(
            persist_enabled=True,
            state_path=str(state_path),
            flush_interval_s=0.1,
        )

    tracker.register_position("NIFTY_CE_1", {"token": "111", "exchangeType": 1})
    tracker.on_tick("NIFTY_CE_1", 222.0)
    tracker.flush()

    assert tracker.get_last_ltp("NIFTY_CE_1") == 222.0
    assert any("state load skipped" in rec.message.lower() for rec in caplog.records)
