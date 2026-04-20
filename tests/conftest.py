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
