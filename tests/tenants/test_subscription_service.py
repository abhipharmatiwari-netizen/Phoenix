from datetime import datetime, timedelta, timezone
import importlib

from app.tenants.models import BrokerAccountModel, SubscriptionModel
from app.tenants.subscription_service import compute_account_runtime_mode, is_subscription_active


def test_import_module_succeeds():
    mod = importlib.import_module("app.tenants.subscription_service")
    assert hasattr(mod, "is_subscription_active")


def _sub(mode: str, start_offset_hours: int = -1, end_offset_hours: int = 1) -> SubscriptionModel:
    now = datetime.now(timezone.utc)
    return SubscriptionModel(
        subscription_id="sub1",
        tenant_id="tenant-default",
        broker_account_id="broker-1",
        mode=mode,
        start_at=now + timedelta(hours=start_offset_hours),
        end_at=now + timedelta(hours=end_offset_hours),
    )


def _account(enabled: bool = True) -> BrokerAccountModel:
    return BrokerAccountModel(
        broker_account_id="broker-1",
        tenant_id="tenant-default",
        broker_type="angel",
        display_name="Test",
        client_code="abc",
        secret_ref="secret",
        enabled=enabled,
    )


def test_is_subscription_active_respects_window():
    active = _sub("PAPER", -1, 1)
    expired = _sub("PAPER", -3, -1)

    assert is_subscription_active(active) is True
    assert is_subscription_active(expired) is False


def test_compute_account_runtime_mode():
    account = _account(enabled=True)
    active_paper = _sub("PAPER")
    active_live = _sub("LIVE")
    active_shadow = _sub("SHADOW")
    expired = _sub("PAPER", -3, -1)

    assert compute_account_runtime_mode(account, active_paper) == "PAPER"
    assert compute_account_runtime_mode(account, active_live) == "LIVE"
    assert compute_account_runtime_mode(account, active_shadow) == "SHADOW"
    assert compute_account_runtime_mode(account, expired) == "DISABLED"
    assert compute_account_runtime_mode(_account(enabled=False), active_live) == "DISABLED"
