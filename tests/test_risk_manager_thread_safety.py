from __future__ import annotations

import threading
import importlib


class _DummyOrderClient:
    pass


def test_risk_manager_update_position_thread_safety(tmp_path):
    # Import strategy module first to avoid known package import order cycle in this repo.
    importlib.import_module("app.strategies.option_strategy")
    mod = importlib.import_module("app.core.risk_manager")
    RiskManager = mod.RiskManager

    state_path = tmp_path / "risk_state.json"
    risk_manager = RiskManager(
        instrument_meta={
            "NIFTY_ATM_CE_25750": {
                "underlying": "NIFTY",
                "exchange": "NFO",
                "token": "12345",
                "symbol": "NIFTY17FEB2625750CE",
                "lot_size": 1,
            }
        },
        order_client=_DummyOrderClient(),
        state_path=str(state_path),
    )

    failures: list[Exception] = []
    iterations = 250

    def _writer() -> None:
        for i in range(iterations):
            try:
                risk_manager.update_position_from_broker(
                    label="NIFTY_ATM_CE_25750",
                    side="BUY" if i % 2 == 0 else "SELL",
                    qty=(i % 5) + 1,
                    entry_price=100.0 + float(i),
                    exchange="NFO",
                    symboltoken="12345",
                    tradingsymbol="NIFTY17FEB2625750CE",
                )
            except Exception as exc:  # pragma: no cover - assertion target
                failures.append(exc)

    def _reader() -> None:
        for _ in range(iterations):
            try:
                _ = risk_manager._get_current_open_lots("NIFTY")
                with risk_manager._state_lock:
                    snapshot = dict(risk_manager.open_positions)
                for _label, entry in snapshot.items():
                    _ = entry.get("qty")
                    _ = entry.get("entry_price")
            except Exception as exc:  # pragma: no cover - assertion target
                failures.append(exc)

    writer_threads = [threading.Thread(target=_writer) for _ in range(3)]
    reader_threads = [threading.Thread(target=_reader) for _ in range(3)]
    for thread in writer_threads + reader_threads:
        thread.start()
    for thread in writer_threads + reader_threads:
        thread.join()

    assert failures == []
