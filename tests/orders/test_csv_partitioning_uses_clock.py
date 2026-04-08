from datetime import datetime, timedelta, timezone

from app.core.clock import SimulatedClock
from app.orders.router import _OrderTradeCsvWriter


def test_csv_writer_partition_uses_injected_clock(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDER_CSV_ENABLED", "true")
    clock = SimulatedClock(datetime(2026, 2, 10, 4, 0, tzinfo=timezone.utc))
    writer = _OrderTradeCsvWriter(base_dir=tmp_path, tz_name="UTC", clock=clock)

    writer.write_order({"created_at": clock.now_utc().isoformat()})
    first_path = tmp_path / "2026-02-10" / "orders.csv"
    assert first_path.exists()

    clock.advance(timedelta(hours=20))
    writer.write_order({"created_at": clock.now_utc().isoformat()})
    second_path = tmp_path / "2026-02-11" / "orders.csv"
    assert second_path.exists()
