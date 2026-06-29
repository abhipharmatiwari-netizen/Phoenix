import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ops" / "export_live_deploy_values_from_postgres.py"


spec = importlib.util.spec_from_file_location("export_live_deploy_values", SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def test_capital_limits_meta_exports_account_specific_json():
    out = module._capital_limits_from_meta(
        {
            "capital_limits": {
                "max_notional_per_order": 500000,
                "max_gross_exposure": 1000000,
            }
        },
        tenant_id="tenant-1",
        broker_account_id="A1",
    )

    assert out == (
        '{"tenant-1:A1":{"max_notional_per_order":500000,'
        '"max_gross_exposure":1000000}}'
    )


def test_capital_limits_json_meta_passes_through_mapping():
    out = module._capital_limits_from_meta(
        {
            "capital_limits_json": {
                "tenant-1:A1": {"max_notional_per_order": 250000},
                "default": {"max_gross_exposure": 750000},
            }
        },
        tenant_id="tenant-1",
        broker_account_id="A1",
    )

    assert out == (
        '{"tenant-1:A1":{"max_notional_per_order":250000},'
        '"default":{"max_gross_exposure":750000}}'
    )


def test_capital_limits_json_must_have_account_key():
    assert module._capital_limits_json_has_account_key(
        '{"tenant-1:A1":{"max_notional_per_order":250000}}',
        tenant_id="tenant-1",
        broker_account_id="A1",
    )
    assert module._capital_limits_json_has_account_key(
        '{"A1":{"max_notional_per_order":250000}}',
        tenant_id="tenant-1",
        broker_account_id="A1",
    )
    assert not module._capital_limits_json_has_account_key(
        '{"default":{"max_notional_per_order":250000}}',
        tenant_id="tenant-1",
        broker_account_id="A1",
    )


def test_risk_max_daily_loss_reads_nested_or_flat_meta():
    assert (
        module._risk_max_daily_loss_from_meta({"risk": {"max_daily_loss": 10000}})
        == "10000"
    )
    assert (
        module._risk_max_daily_loss_from_meta({"risk_max_daily_loss": "12000"})
        == "12000"
    )


def test_host_docker_internal_maps_to_localhost_for_host_side_query(monkeypatch):
    captured = {}

    def fake_make_conninfo(**kwargs):
        captured.update(kwargs)
        return "dsn"

    monkeypatch.setattr(module, "make_conninfo", fake_make_conninfo)
    monkeypatch.setenv("CONTROL_PLANE_PG_HOST", "host.docker.internal")
    monkeypatch.setenv("CONTROL_PLANE_PG_DB", "phoenix")
    monkeypatch.setenv("CONTROL_PLANE_PG_USER", "phoenix_app")
    monkeypatch.setenv("CONTROL_PLANE_PG_PASSWORD_HOST", "secret")
    monkeypatch.setenv("CONTROL_PLANE_PG_SSLMODE", "prefer")

    assert module._conninfo_from_env() == "dsn"
    assert captured["host"] == "127.0.0.1"


def test_missing_required_fields_lists_blank_postgres_secret_fields():
    missing = module._missing_required_fields(
        {
            "api_key": "",
            "client_code": "CLIENT",
            "pin": None,
            "totp_secret": "TOTP",
        },
        module._REQUIRED_BROKER_CREDENTIAL_FIELDS,
    )

    assert missing == ["api_key", "pin"]


def test_fetch_deploy_values_rejects_missing_account_specific_limits(monkeypatch):
    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return None

        def fetchone(self):
            return {
                "tenant_id": "tenant-1",
                "meta": {"capital_limits_json": {"default": {"max_gross_exposure": 1}}},
                "api_key": "api",
                "client_code": "client",
                "pin": "pin",
                "totp_secret": "totp",
                "client_local_ip": "10.0.0.1",
                "client_public_ip": "203.0.113.1",
                "mac_address": "02:00:00:00:00:01",
            }

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    class FakePsycopg:
        pass

    monkeypatch.setattr(module, "psycopg", FakePsycopg())
    monkeypatch.setattr(module, "dict_row", object())
    monkeypatch.setattr(module, "_conninfo_from_env", lambda: "dsn")
    monkeypatch.setattr(
        module.psycopg,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="must include an account key"):
        module.fetch_deploy_values(tenant_id="tenant-1", broker_account_id="A1")
