import pytest

from kit.clock import EPOCH, SimClock
from kit.service import SIM_TIME_HEADER, AsgiService, JsonStore, SqliteStore, sim_time_from_headers

fastapi = pytest.importorskip("fastapi")

SCHEMA = """
CREATE TABLE account (id TEXT PRIMARY KEY, balance_paise INTEGER NOT NULL);
INSERT INTO account VALUES ('alice', 100000);
"""


def test_json_store_snapshots_are_canonical() -> None:
    first = JsonStore({"b": 1, "a": 2})
    second = JsonStore({"a": 2, "b": 1})
    assert first.snapshot() == second.snapshot()


def test_json_store_restores_exactly() -> None:
    store = JsonStore({"count": 0})
    blob = store.snapshot()
    store.data["count"] = 99
    store.restore(blob)
    assert store.data == {"count": 0}


def test_sqlite_store_snapshot_and_restore_are_exact() -> None:
    store = SqliteStore(SCHEMA)
    blob = store.snapshot()
    store.connection.execute("UPDATE account SET balance_paise = 1 WHERE id = 'alice'")
    assert store.query_all("SELECT balance_paise FROM account")[0]["balance_paise"] == 1
    store.restore(blob)
    assert store.query_all("SELECT balance_paise FROM account")[0]["balance_paise"] == 100000
    assert store.snapshot() == blob
    store.close()


def build_app() -> tuple[object, JsonStore]:
    store = JsonStore({"calls": []})
    app = fastapi.FastAPI()

    @app.post("/tick")
    def tick(request: fastapi.Request) -> dict[str, object]:
        now = sim_time_from_headers(request.headers)
        store.data["calls"].append(now)
        return {"now": now, "calls": len(store.data["calls"])}

    @app.get("/admin/state")
    def state() -> dict[str, object]:
        return dict(store.data)

    return app, store


def test_services_are_told_the_simulated_time_on_every_request() -> None:
    clock = SimClock(EPOCH)
    app, _ = build_app()
    service = AsgiService("ticker", app, JsonStore(), clock)
    first = service.post("/tick").json()
    clock.advance(3600)
    second = service.post("/tick").json()
    assert first["now"] == EPOCH.isoformat()
    assert second["now"] == (EPOCH.replace(hour=EPOCH.hour + 1)).isoformat()
    service.close()


def test_a_service_called_without_sim_time_refuses_to_guess() -> None:
    clock = SimClock(EPOCH)
    app, _ = build_app()
    service = AsgiService("ticker", app, JsonStore(), clock)
    with pytest.raises(Exception, match=SIM_TIME_HEADER):
        # Deliberately bypassing the header the episode always sets.
        service.request("POST", "/tick", json_body={}, headers={})
    service.close()


def test_query_reads_the_admin_surface() -> None:
    clock = SimClock(EPOCH)
    app, store = build_app()
    service = AsgiService("ticker", app, store, clock)
    service.post("/tick")
    assert len(service.query("state")["calls"]) == 1
    service.close()
