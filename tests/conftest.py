from pathlib import Path
import shutil
import sys
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_TEST_TMP_ROOT = ROOT / ".test_tmp"


@pytest.fixture(autouse=True)
def _test_runtime_defaults(monkeypatch):
    # Keep suite defaults aligned with the non-LIVE architecture path.
    # LIVE-only durability requirements remain opt-in in tests that need them.
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_ENABLED", "false")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_REQUIRED", "false")
    # Redirect test log output to .test_tmp/logs so it never overlaps with
    # the production log directory (logs/) that is bind-mounted in compose.
    test_log_dir = str(ROOT / ".test_tmp" / "logs")
    monkeypatch.setenv("APP_LOG_DIR", test_log_dir)
    # §133 / Issue #12: Redirect production state files to .test_tmp/state so
    # pytest runs never overwrite ./state/risk_positions.json or
    # ./logs/executed_tokens_state.json used by the LIVE container between
    # trading sessions.  Issue #19 already redirected log output; this
    # extends that isolation to state files.
    test_state_dir = ROOT / ".test_tmp" / "state"
    test_state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(
        "RISK_STATE_PATH",
        str(test_state_dir / "risk_positions.json"),
    )
    monkeypatch.setenv(
        "EXECUTED_TOKENS_STATE_PATH",
        str(ROOT / ".test_tmp" / "logs" / "executed_tokens_state.json"),
    )


@pytest.fixture
def tmp_path():
    _TEST_TMP_ROOT.mkdir(exist_ok=True)
    path = _TEST_TMP_ROOT / f"pytest-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def dummy_instrument_meta():
    return {
        "NIFTY24MAYFUT": {
            "symbol": "NIFTY24MAYFUT",
            "underlying": "NIFTY",
            "lot_size": 65,
        },
        "BANKNIFTY24MAYFUT": {
            "symbol": "BANKNIFTY24MAYFUT",
            "underlying": "BANKNIFTY",
            "lot_size": 30,
        },
    }
