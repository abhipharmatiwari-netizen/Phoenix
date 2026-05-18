from __future__ import annotations

from datetime import date, datetime, timezone
import json

from app.data.option_chain_provider import OptionQuote
from scripts.data import validate_nse_vs_angel_option_chain as cli


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _nse_payload():
    return {
        "records": {
            "timestamp": "18-May-2026 15:30:00",
            "underlyingValue": 23649.95,
            "data": [
                {
                    "strikePrice": 23600,
                    "expiryDate": "19-May-2026",
                    "CE": {
                        "identifier": "OPTIDXNIFTY19-05-2026CE23600.00",
                        "openInterest": 1250,
                        "totalTradedVolume": 3210,
                        "impliedVolatility": 11.75,
                        "lastPrice": 82.4,
                        "bidprice": 81.8,
                        "askPrice": 82.7,
                    },
                }
            ],
        }
    }


def _angel_quote():
    return OptionQuote(
        snapshot_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        source_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        strike=23600,
        option_type="CE",
        trading_symbol="NIFTY19MAY2623600CE",
        exchange="NFO",
        symbol_token="23600CE",
        provider="angel",
        oi=1250,
        volume=3210,
        iv="11.75",
        bid="81.8",
        ask="82.7",
        ltp="82.4",
    )


def test_parse_args_defaults_to_validation_without_store():
    args = cli.parse_args(["--expiry", "2026-05-19"])

    assert args.underlying == "NIFTY"
    assert args.store_nse is False
    assert args.fail_on_mismatch is False


def test_main_loads_saved_nse_payload_and_compares_latest_angel_snapshot(
    monkeypatch,
    tmp_path,
    capsys,
):
    payload_path = tmp_path / "nse.json"
    payload_path.write_text(json.dumps(_nse_payload()), encoding="utf-8")

    class FakeRepository:
        def __init__(self, conn):
            self.conn = conn

        def fetch_latest_snapshot(self, **kwargs):
            assert kwargs["provider"] == "angel"
            assert kwargs["underlying"] == "NIFTY"
            return [_angel_quote()]

    monkeypatch.setattr(cli, "connect_with_retry", lambda *_, **__: FakeConn())
    monkeypatch.setattr(cli, "OptionChainRepository", FakeRepository)

    rc = cli.main(
        [
            "--expiry",
            "2026-05-19",
            "--decision-ts",
            "2026-05-18T10:00:00+00:00",
            "--dsn",
            "postgresql://test",
            "--nse-json-input",
            str(payload_path),
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert rc == 0
    assert report["ok"] is True
    assert report["metadata"]["angel_quotes_count"] == 1
    assert report["metadata"]["nse_quotes_count"] == 1
    assert report["metadata"]["validation_only"] is True


def test_main_returns_operational_error_when_angel_snapshot_missing(
    monkeypatch,
    tmp_path,
    capsys,
):
    payload_path = tmp_path / "nse.json"
    payload_path.write_text(json.dumps(_nse_payload()), encoding="utf-8")

    class EmptyRepository:
        def __init__(self, conn):
            self.conn = conn

        def fetch_latest_snapshot(self, **kwargs):
            return []

    monkeypatch.setattr(cli, "connect_with_retry", lambda *_, **__: FakeConn())
    monkeypatch.setattr(cli, "OptionChainRepository", EmptyRepository)

    rc = cli.main(
        [
            "--expiry",
            "2026-05-19",
            "--decision-ts",
            "2026-05-18T10:00:00+00:00",
            "--dsn",
            "postgresql://test",
            "--nse-json-input",
            str(payload_path),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert report["metadata"]["error"] == "no_angel_snapshot"
