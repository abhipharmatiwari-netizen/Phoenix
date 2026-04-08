import logging

from app.core.feature_flags import load_stability_feature_flags


def test_feature_flags_invalid_values_fall_back_to_defaults_with_warning(caplog):
    env = {
        "STRATEGY_BRIDGE_MODE": "invalid_mode",
        "STRATEGY_BRIDGE_TIMEOUT_S": "-5",
        "STRATEGY_BRIDGE_MAX_INFLIGHT": "0",
        "BROKER_SCHEMA_CHECK_MODE": "broken",
        "EXECUTED_TOKENS_STATE_FLUSH_INTERVAL_S": "bad",
    }
    test_logger = logging.getLogger("tests.feature_flags")

    with caplog.at_level(logging.WARNING, logger="tests.feature_flags"):
        flags = load_stability_feature_flags(env, log=test_logger)

    assert flags.strategy_bridge_mode == "per_call_loop"
    assert flags.strategy_bridge_timeout_s == 2.5
    assert flags.strategy_bridge_max_inflight == 50
    assert flags.broker_schema_check_mode == "off"
    assert flags.executed_tokens_state_flush_interval_s == 10.0
    assert any("falling back" in rec.message.lower() for rec in caplog.records)
