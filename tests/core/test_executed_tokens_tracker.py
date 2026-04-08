import importlib

from app.core.executed_tokens_tracker import ExecutedTokensTracker


def test_import_module_succeeds():
    mod = importlib.import_module("app.core.executed_tokens_tracker")
    assert hasattr(mod, "ExecutedTokensTracker")


def test_executed_tokens_tracker_tracks_and_extends():
    tracker = ExecutedTokensTracker()
    positions = {
        "OPT1": {"symboltoken": "111"},
        "OPT2": {"symboltoken": "222"},
    }
    meta = {
        "OPT1": {"token": "111", "exchangeType": 1},
        "OPT2": {"token": "222", "exchangeType": 2},
    }

    tracker.bootstrap_from_positions(positions, meta)
    tracker.register_position("OPT3", {"token": "333", "exchangeType": 1})
    tracker.on_tick("OPT1", 123.45)

    extended = tracker.extend_token_list([{"exchangeType": 1, "tokens": ["999"]}])
    assert {"exchangeType": 1, "tokens": ["111", "333", "999"]} in extended
    assert {"exchangeType": 2, "tokens": ["222"]} in extended
    assert tracker.get_last_ltp("OPT1") == 123.45

    tracker.unregister_position("OPT1")
    extended_after = tracker.extend_token_list([])
    assert all("111" not in entry.get("tokens", []) for entry in extended_after)
