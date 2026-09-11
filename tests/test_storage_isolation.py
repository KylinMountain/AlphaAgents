"""Regression tests for collection-time storage and offline boundaries."""

import _sqlite3
from contextlib import closing
import importlib
import os
from pathlib import Path
import socket
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from tests import conftest as isolation
from alpha_agents import config
from alpha_agents.config import CHROMA_PATH, DATA_DIR, DB_PATH, MEMORY_DB_PATH
from alpha_agents.data import activity_log, memory_store, report_store


# These assertions execute while importing the test module, before any fixtures
# or pytest_collection_modifyitems can repair unsafe import-by-value constants.
assert DATA_DIR == isolation._SANDBOX_DATA_DIR
assert DB_PATH == DATA_DIR / "stocks.db"
assert CHROMA_PATH == DATA_DIR / "chroma"
assert MEMORY_DB_PATH == DATA_DIR / "memory.db"
assert memory_store.MEMORY_DB_PATH == MEMORY_DB_PATH
assert activity_log.DB_PATH == DATA_DIR / "activity.db"
assert report_store.REPORTS_DB_PATH == DATA_DIR / "reports.db"
assert not DATA_DIR.is_relative_to(isolation._PROJECT_ROOT / "data")

# A deliberate collection-time write must also be gone before the first test.
with closing(sqlite3.connect(DATA_DIR / "collection-probe.db")) as _collection_conn:
    _collection_conn.execute("CREATE TABLE collection_probe (value INTEGER)")
    _collection_conn.commit()


def test_collection_paths_are_safe_and_collection_store_is_reset():
    assert config.DATA_DIR == DATA_DIR
    assert not (DATA_DIR / "collection-probe.db").exists()


@pytest.mark.parametrize("variant", [
    "path", "string", "bytes", "relative", "dotdot", "file_uri", "relative_uri",
    "localhost_uri", "encoded_uri", "uri_without_flag", "memory_mode_uri",
    "directory_symlink", "file_symlink", "symlink_uri",
])
def test_production_paths_rejected_before_real_connect(tmp_path, monkeypatch, variant):
    production = isolation._PROJECT_ROOT / "data" / "storage-isolation-never-open.db"
    relative = os.path.relpath(production, isolation._PROJECT_ROOT)
    monkeypatch.chdir(isolation._PROJECT_ROOT)
    uri = False
    targets = {
        "path": production,
        "string": str(production),
        "bytes": os.fsencode(production),
        "relative": relative,
        "dotdot": production.parent / "unused" / ".." / production.name,
        "file_uri": production.as_uri() + "?mode=ro",
        "relative_uri": "file:" + relative + "?mode=ro",
        "localhost_uri": "file://localhost" + str(production) + "?mode=ro",
        "encoded_uri": "file:" + quote(str(production), safe="") + "?mode=ro",
        "uri_without_flag": production.as_uri() + "?mode=ro",
        "memory_mode_uri": production.as_uri() + "?mode=memory&cache=shared",
    }
    if variant in ("directory_symlink", "symlink_uri"):
        alias = tmp_path / "business-alias"
        alias.symlink_to(production.parent, target_is_directory=True)
        target = alias / production.name
        if variant == "symlink_uri":
            target = target.as_uri() + "?mode=ro"
            uri = True
    elif variant == "file_symlink":
        target = tmp_path / "business-alias.db"
        target.symlink_to(production)
    else:
        target = targets[variant]
        uri = "uri" in variant and variant != "uri_without_flag"
    real_connect = Mock(side_effect=AssertionError("Real connect must not be called"))
    monkeypatch.setattr(isolation, "_ORIGINAL_CONNECT", real_connect)
    with pytest.raises(isolation.StorageIsolationError, match="project data/"):
        sqlite3.connect(target, uri=uri)
    real_connect.assert_not_called()


def test_case_alias_is_blocked_before_connect_on_case_insensitive_platforms(monkeypatch):
    if isolation.sys.platform not in ("darwin", "win32"):
        pytest.skip("Case-alias policy is specific to macOS and Windows")
    path = str(isolation._PROJECT_ROOT / "DATA" / "never-open.db")
    real_connect = Mock(side_effect=AssertionError("Real connect must not be called"))
    monkeypatch.setattr(isolation, "_ORIGINAL_CONNECT", real_connect)
    with pytest.raises(isolation.StorageIsolationError):
        sqlite3.connect(path)
    real_connect.assert_not_called()


@pytest.mark.parametrize("connector", [isolation._ORIGINAL_CONNECT, isolation._BASE_CONNECTION])
@pytest.mark.parametrize("uri", [False, True])
def test_audit_blocks_native_aliases_before_sqlite_opens(connector, uri):
    path = isolation._PROJECT_ROOT / "data" / "storage-isolation-never-open.db"
    target = path.as_uri() + "?mode=ro" if uri else path
    with pytest.raises(isolation.StorageIsolationError, match="project data/"):
        connector(target, uri=uri)


@pytest.mark.parametrize("connector", [sqlite3.connect, sqlite3.dbapi2.connect, _sqlite3.connect])
@pytest.mark.parametrize("variant", ["path", "uri", "memory", "named_memory", "temporary"])
def test_temporary_and_memory_databases_remain_usable(tmp_path, connector, variant):
    db = tmp_path / "isolated.db"
    target = {
        "path": db,
        "uri": db.as_uri() + "?mode=rwc",
        "memory": ":memory:",
        "named_memory": "file:isolated-memory?mode=memory&cache=shared",
        "temporary": "",
    }[variant]
    with closing(connector(target, uri=variant in ("uri", "named_memory"))) as conn:
        conn.execute("CREATE TABLE probe (value INTEGER)")
        conn.execute("INSERT INTO probe VALUES (7)")
        assert conn.execute("SELECT value FROM probe").fetchone() == (7,)


@pytest.mark.parametrize("variant", ["literal", "parameter", "expression", "script", "uri"])
def test_attach_cannot_open_business_database(variant):
    target = str(isolation._PROJECT_ROOT / "data" / "storage-isolation-never-open.db")
    if variant == "uri":
        target = Path(target).as_uri() + "?mode=ro"
    quoted = "'" + target.replace("'", "''") + "'"
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            if variant == "parameter":
                conn.execute("ATTACH DATABASE ? AS business", (target,))
            elif variant == "expression":
                conn.execute("ATTACH DATABASE (? || ?) AS business", (target[:-3], ".db"))
            elif variant == "script":
                conn.executescript(f"SELECT 1; ATTACH DATABASE {quoted} AS business;")
            else:
                conn.execute(f"ATTACH DATABASE {quoted} AS business")
        assert [row[1] for row in conn.execute("PRAGMA database_list")] == ["main"]


@pytest.mark.parametrize("callback", [None, lambda *args: sqlite3.SQLITE_OK])
def test_authorizer_cannot_be_disabled(callback):
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.set_authorizer(callback)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute("ATTACH DATABASE ':memory:' AS other")
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_vacuum_into_is_denied_without_creating_a_file(tmp_path):
    target = tmp_path / "vacuum-copy.db"
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("VACUUM INTO ?", (str(target),))
    assert not target.exists()


def test_default_factory_cannot_bypass_attach_guard():
    with closing(sqlite3.connect(":memory:", factory=sqlite3.Connection)) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("ATTACH DATABASE ':memory:' AS other")
    with closing(sqlite3.connect(":memory:", 5, 0, None, True, sqlite3.Connection)) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_unguarded_custom_factory_is_rejected_before_connect(monkeypatch):
    class CustomConnection(sqlite3.Connection):
        pass

    real_connect = Mock(side_effect=AssertionError("Real connect must not be called"))
    monkeypatch.setattr(isolation, "_ORIGINAL_CONNECT", real_connect)
    with pytest.raises(isolation.StorageIsolationError, match="factories"):
        sqlite3.connect(":memory:", factory=CustomConnection)
    real_connect.assert_not_called()


@pytest.mark.parametrize("store_name, getter", [
    ("memory_store", "_get_conn"), ("activity_log", "_get_conn"),
    ("report_store", "_get_conn"), ("snapshot_store", "_get_conn"),
    ("market_history", "_get_conn"), ("token_usage", "_conn"),
])
def test_default_store_reset_closes_connection_and_clears_thread_local(store_name, getter):
    module = importlib.import_module(f"alpha_agents.data.{store_name}")
    conn = getattr(module, getter)()
    old_local = module._local
    assert Path(conn.execute("PRAGMA database_list").fetchone()[2]).is_relative_to(DATA_DIR)
    conn.execute("CREATE TABLE isolation_marker (value INTEGER)")
    conn.commit()
    isolation._reset_default_storage()
    assert module._local is not old_local
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    fresh = getattr(module, getter)()
    assert fresh is not conn
    assert fresh.execute(
        "SELECT name FROM sqlite_master WHERE name='isolation_marker'"
    ).fetchone() is None


@pytest.mark.parametrize("iteration", range(2))
def test_each_test_starts_with_empty_default_store(iteration):
    # Each case writes the same table and leaves its default connection open.
    # Both must pass independently and in either order.
    conn = memory_store._get_conn()
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='per_test_marker'"
    ).fetchone() is None
    conn.execute("CREATE TABLE per_test_marker (value INTEGER)")
    conn.execute("INSERT INTO per_test_marker VALUES (?)", (iteration,))
    conn.commit()


def test_worker_thread_default_slot_is_reset():
    errors = []
    old_local = memory_store._local

    def write_in_thread():
        try:
            conn = memory_store._get_conn()
            conn.execute("CREATE TABLE worker_marker (value INTEGER)")
            conn.commit()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=write_in_thread)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert not errors
    isolation._reset_default_storage()
    assert memory_store._local is not old_local
    assert memory_store._get_conn().execute(
        "SELECT name FROM sqlite_master WHERE name='worker_marker'"
    ).fetchone() is None


@pytest.mark.parametrize("store_name, getter", [("embeddings", "_get_store"), ("news_index", "get_store")])
def test_vector_store_caches_are_reset(store_name, getter):
    module = importlib.import_module(f"alpha_agents.data.{store_name}")
    store = getattr(module, getter)()
    conn = store._connection()
    conn.execute("CREATE TABLE vector_marker (value INTEGER)")
    conn.commit()
    isolation._reset_default_storage()
    fresh = getattr(module, getter)()
    assert fresh is not store
    assert fresh._connection().execute(
        "SELECT name FROM sqlite_master WHERE name='vector_marker'"
    ).fetchone() is None


def test_cleanup_never_follows_symlinks_or_mutable_config(tmp_path, monkeypatch):
    outside = tmp_path / "not-owned"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (DATA_DIR / "directory-alias").symlink_to(outside, target_is_directory=True)
    (DATA_DIR / "file-alias").symlink_to(sentinel)
    monkeypatch.setattr(config, "DATA_DIR", outside)
    isolation._reset_default_storage()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not list(DATA_DIR.iterdir())
    assert config.DATA_DIR == DATA_DIR


def test_market_data_skip_does_not_probe_any_database(monkeypatch):
    real_connect = Mock(side_effect=AssertionError("Collection must not probe SQLite"))
    monkeypatch.setattr(isolation, "_ORIGINAL_CONNECT", real_connect)
    items = []
    expected = []
    for module, names in isolation._NEEDS_MARKET_HISTORY.items():
        for name in names or {"test_real_corpus"}:
            item = SimpleNamespace(path=Path(module + ".py"), name=name, add_marker=Mock())
            items.append(item)
            expected.append(item)
    pure = SimpleNamespace(path=Path("test_vpa_regime.py"),
                           name="test_classify_regime_handles_short_history", add_marker=Mock())
    items.append(pure)
    isolation.pytest_collection_modifyitems(None, items)
    for item in expected:
        item.add_marker.assert_called_once()
        assert "not supplied" in item.add_marker.call_args.args[0].kwargs["reason"]
    pure.add_marker.assert_not_called()
    real_connect.assert_not_called()


@pytest.mark.parametrize("operation", ["connect", "connect_ex", "bind", "sendto", "dns"])
def test_real_network_operations_are_blocked_before_io(operation):
    if operation == "dns":
        with pytest.raises(RuntimeError, match="resolution is disabled"):
            socket.getaddrinfo("example.invalid", 443)
        return
    kind = socket.SOCK_DGRAM if operation == "sendto" else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, kind) as sock:
        with pytest.raises(RuntimeError, match="disabled in tests"):
            if operation == "sendto":
                sock.sendto(b"probe", ("127.0.0.1", 9))
            else:
                getattr(sock, operation)(("127.0.0.1", 9))


def test_socketpair_for_asyncio_wakeup_still_works():
    left, right = socket.socketpair()
    with closing(left), closing(right):
        assert left.family == socket.AF_UNIX
        left.sendall(b"probe")
        assert right.recv(5) == b"probe"


def test_fake_http_transport_does_not_require_network():
    httpx = pytest.importorskip("httpx")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.invalid/probe").json() == {"ok": True}
