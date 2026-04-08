from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.clock import SimulatedClock


def test_simulated_clock_fixed_and_advance_deterministic():
    base = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)
    clock = SimulatedClock(base)

    assert clock.now_utc() == base
    assert clock.now_local(ZoneInfo("Asia/Kolkata")).hour == 15

    clock.advance(timedelta(minutes=45))
    assert clock.now_utc() == base + timedelta(minutes=45)
