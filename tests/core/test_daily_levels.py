import importlib
import json
import types
from datetime import date, datetime, timezone

import pytest

MODULE_PATH = "app.core.daily_levels"
CALLABLE_NAMES = ['DailyLevels', 'DailyLevelsCache']


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_callable_behaviour_placeholder(name):
    mod = importlib.import_module(MODULE_PATH)
    obj = getattr(mod, name)
    assert obj is not None


def test_daily_levels_fetch_uses_proxy_aware_angel_connection(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    requests = []
    acquire_calls = []

    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps(
                {
                    "status": True,
                    "data": [
                        [
                            "2026-06-03T00:00:00+05:30",
                            100.0,
                            110.0,
                            90.0,
                            105.0,
                            12345,
                        ]
                    ],
                }
            ).encode("utf-8")

    class FakeConnection:
        def request(self, method, path, *, body=None, headers=None):
            requests.append(
                {
                    "method": method,
                    "path": path,
                    "body": body,
                    "headers": headers,
                }
            )

        def getresponse(self):
            return FakeResponse()

    class FakeClient:
        def _headers(self):
            return {"Authorization": "Bearer token", "X-PrivateKey": "api-key"}

    class FixedClock:
        def now_utc(self):
            return datetime(2026, 6, 4, tzinfo=timezone.utc)

    def fake_make_connection():
        requests.append({"connection": "proxy-aware"})
        return FakeConnection()

    monkeypatch.setattr(mod, "_make_angel_connection", fake_make_connection)
    monkeypatch.setattr(
        mod.rate_limiter,
        "acquire",
        lambda key: acquire_calls.append(key),
    )

    cache = mod.DailyLevelsCache(FakeClient(), clock=FixedClock())
    levels = cache._fetch_last_daily_candle(
        exch_seg="NSE",
        token="26000",
        from_date=date(2026, 5, 28),
        to_date=date(2026, 6, 4),
    )

    assert requests[0] == {"connection": "proxy-aware"}
    assert requests[1]["method"] == "POST"
    assert requests[1]["path"] == "/rest/secure/angelbroking/historical/v1/getCandleData"
    assert json.loads(requests[1]["body"])["symboltoken"] == "26000"
    assert requests[1]["headers"]["Authorization"] == "Bearer token"
    assert acquire_calls == ["historical"]
    assert levels.trading_date == date(2026, 6, 3)
    assert levels.pdh == 110.0
    assert levels.pdl == 90.0
    assert levels.pclose == 105.0
