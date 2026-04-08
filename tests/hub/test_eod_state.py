from __future__ import annotations

from dataclasses import dataclass

from app.hub import eod_state


@dataclass
class _MemoryStore:
    state: eod_state.EODState

    def get_state(self, tenant_id, broker_account_id):
        _ = (tenant_id, broker_account_id)
        return self.state

    def save_state(self, state):
        self.state = state


def test_eod_state_manager_records_and_reads_daily_exit():
    store = _MemoryStore(state=eod_state.EODState("t1", "a1"))
    manager = eod_state.EODStateManager(store, default_time_zone="UTC")

    assert manager.has_exited_today("t1", "a1") is False
    assert manager.record_eod_exit("t1", "a1") is True
    assert manager.has_exited_today("t1", "a1") is True


def test_postgres_eod_state_store_uses_sql_adapter_with_stubbed_connection(monkeypatch):
    db = {"rows": {}, "statements": []}

    class _Cursor:
        def __init__(self):
            self._fetchone = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            db["statements"].append(str(sql))
            sql_text = str(sql).strip().upper()
            if sql_text.startswith("SELECT TENANT_ID"):
                key = (str(params["tenant_id"]), str(params["broker_account_id"]))
                if key in db["rows"]:
                    self._fetchone = {
                        "tenant_id": key[0],
                        "broker_account_id": key[1],
                        "last_exit_date": db["rows"][key],
                    }
                else:
                    self._fetchone = None
            elif "INSERT INTO EOD_STATES" in sql_text:
                key = (str(params["tenant_id"]), str(params["broker_account_id"]))
                db["rows"][key] = params.get("last_exit_date")

        def fetchone(self):
            return self._fetchone

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def close(self):
            return None

        def cursor(self, *args, **kwargs):
            _ = (args, kwargs)
            return _Cursor()

    monkeypatch.setattr(
        eod_state,
        "connect_with_retry",
        lambda *_args, **_kwargs: _Conn(),
    )

    store = eod_state.PostgresEODStateStore("postgres://stub", default_time_zone="UTC")
    first = store.get_state("tenant-1", "acct-1")
    assert first.last_exit_date is None

    first.last_exit_date = "2026-03-03"
    store.save_state(first)
    loaded = store.get_state("tenant-1", "acct-1")
    assert loaded.last_exit_date == "2026-03-03"
    assert any("CREATE TABLE IF NOT EXISTS" in stmt for stmt in db["statements"])
