"""pytest 公共夹具：隔离 SQLite + 预载样例 + TestClient。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import archive, settings  # noqa: E402


@pytest.fixture(scope="session")
def sample_events() -> dict[str, list[archive.Event]]:
    return archive.load_sample_events(settings.EVENTS_DIR, settings.TABLES)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例一个全新 SQLite 文件；lifespan 会建表并幂等 seed 样例。"""
    monkeypatch.setenv("CDCWATER_DB", str(tmp_path / "cdcwater-test.sqlite3"))
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setenv("CDCWATER_DB", str(tmp_path / "store-test.sqlite3"))
    connection = store.connect()
    store.init_db(connection)
    store.seed_sample(connection)
    try:
        yield connection
    finally:
        connection.close()
