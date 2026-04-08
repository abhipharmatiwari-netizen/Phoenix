import importlib

from app.core.identifiers import BrokerAccountId, HubOrderKey, TenantId, make_hub_order_id


def test_import_module_succeeds():
    mod = importlib.import_module("app.core.identifiers")
    assert hasattr(mod, "make_hub_order_id")


def test_make_hub_order_id_builds_composite_id():
    key = HubOrderKey(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("ba-1"),
        broker_order_id="ord-99",
    )
    hub_id = make_hub_order_id(key)
    assert str(hub_id) == "tenant-1:ba-1:ord-99"
