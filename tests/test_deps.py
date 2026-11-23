from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import app.deps as deps


def test_store_initialization_is_serialized(monkeypatch):
    """A fresh SQLite database must not be initialized by two first requests."""

    created = []

    class FakeStore:
        def __init__(self, database_url):
            created.append(database_url)

    monkeypatch.setattr(deps, "Store", FakeStore)
    monkeypatch.setattr(deps, "settings", lambda: SimpleNamespace(database_url="sqlite:///test.db"))
    monkeypatch.setattr(deps, "_store_instance", None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        instances = list(pool.map(lambda _: deps.store(), range(8)))

    assert len(created) == 1
    assert all(instance is instances[0] for instance in instances)
