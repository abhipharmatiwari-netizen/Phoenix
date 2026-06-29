import importlib.util
from pathlib import Path


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
