"""Offline tests with isolated default storage, including during collection.

Keep one stable path for import-by-value constants and function defaults. Clear
only this run's private stores between tests; never inspect the business DBs.
These are in-process guards, not a sandbox for subprocesses or native libraries.
"""

import _sqlite3
import atexit
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
from urllib.parse import unquote_to_bytes, urlsplit

import pytest


_PROJECT_ROOT = Path(__file__).absolute().parents[1]
_PRODUCTION_ROOTS = (
    _PROJECT_ROOT / "data",
    (_PROJECT_ROOT / "data").resolve(),
)
_ORIGINAL_CONNECT = sqlite3.connect
_BASE_CONNECTION = sqlite3.Connection
_SANDBOX_CONNECTIONS = set()


class StorageIsolationError(RuntimeError):
    """An operation would escape the test storage boundary."""


def _within(path, root):
    path, root = os.fspath(path), os.fspath(root)
    # macOS commonly uses case-insensitive volumes; reject case aliases too.
    if sys.platform in ("darwin", "win32"):
        path, root = path.casefold(), root.casefold()
    return os.path.commonpath((path, root)) == root


def _database_paths(database):
    """Check both literal filenames and SQLite's decoded file: URI filename."""
    name = os.fsdecode(database)
    if name in ("", ":memory:"):
        return ()
    names = [name]
    if name.startswith("file:"):
        uri = urlsplit(name)
        if uri.netloc not in ("", "localhost"):
            raise StorageIsolationError("SQLite URI authorities are not allowed in tests")
        decoded = os.fsdecode(unquote_to_bytes(uri.path))
        if decoded not in ("", ":memory:"):
            names.append(decoded)
    return tuple(Path(name) for name in names)


def _require_safe_database(database):
    for path in _database_paths(database):
        # Check the lexical path as well: data/ itself may be a symlink out of
        # the checkout. resolve() also catches aliases pointing into data/.
        for candidate in (Path(os.path.abspath(path)), path.resolve()):
            if any(_within(candidate, root) for root in _PRODUCTION_ROOTS):
                raise StorageIsolationError(
                    "Tests may not open SQLite databases in project data/: "
                    f"{database!r}; use tmp_path or :memory:"
                )


# Validate the temp parent before creating anything (TMPDIR is user-controlled).
_require_safe_database(tempfile.gettempdir())
_RUN_ROOT = Path(tempfile.mkdtemp(prefix="alphaagents-pytest-")).resolve()
_SANDBOX_DATA_DIR = _RUN_ROOT / "data"
_SANDBOX_DATA_DIR.mkdir()

# tiktoken downloads its vocabulary on first use, and TMPDIR is redirected above,
# so without this the very first token count inside a test would try to reach the
# network — which the audit hook below treats as a failure. Point it at the copy
# kept in the repository instead. Rebuild the fixture with:
#   TIKTOKEN_CACHE_DIR=tests/fixtures/tiktoken python -c \
#     "import tiktoken; tiktoken.get_encoding('cl100k_base')"
_TOKENIZER_CACHE = _PROJECT_ROOT / "tests" / "fixtures" / "tiktoken"
if not _TOKENIZER_CACHE.is_dir():
    raise RuntimeError(
        "tests/fixtures/tiktoken is missing. The tokenizer would download its "
        "vocabulary, and tests are not allowed to use the network. Rebuild it "
        "with TIKTOKEN_CACHE_DIR=tests/fixtures/tiktoken and re-run "
        "tiktoken.get_encoding('cl100k_base')."
    )
os.environ["TIKTOKEN_CACHE_DIR"] = str(_TOKENIZER_CACHE)


def _sqlite_authorizer(action, arg1, arg2, database, source):
    # Deny every ATTACH, including bound parameters, expressions and VACUUM
    # INTO. An authorizer cannot reliably recover computed target filenames.
    if action == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class _IsolatedConnection(_BASE_CONNECTION):
    def __init__(self, database, *args, **kwargs):
        super().__init__(database, *args, **kwargs)
        self.set_authorizer(None)
        if any(_within(path.resolve(), _SANDBOX_DATA_DIR)
               for path in _database_paths(database)):
            _SANDBOX_CONNECTIONS.add(self)

    def set_authorizer(self, callback):
        if callback is None:
            return super().set_authorizer(_sqlite_authorizer)

        def authorize(*args):
            result = _sqlite_authorizer(*args)
            return result if result != sqlite3.SQLITE_OK else callback(*args)

        return super().set_authorizer(authorize)

    def close(self):
        super().close()
        _SANDBOX_CONNECTIONS.discard(self)


def _guarded_connect(database, *args, **kwargs):
    database = os.fspath(database)
    _require_safe_database(database)  # Must run before the real connect callable.
    args = list(args)
    factory = args[4] if len(args) > 4 else kwargs.get("factory", _IsolatedConnection)
    if factory is _BASE_CONNECTION:
        factory = _IsolatedConnection
    if not isinstance(factory, type) or not issubclass(factory, _IsolatedConnection):
        raise StorageIsolationError("SQLite factories must preserve the test authorizer")
    if len(args) > 4:
        args[4] = factory
    else:
        kwargs["factory"] = factory
    return _ORIGINAL_CONNECT(database, *args, **kwargs)


def _isolation_audit(event, args):
    if event == "sqlite3.connect":
        # Covers pre-imported connect aliases and direct Connection(...), too.
        _require_safe_database(args[0])
    elif event == "sqlite3.connect/handle":
        # This event fires before Connection initialization finishes, so it
        # cannot install an authorizer. Fail closed on unguarded factories.
        if not isinstance(args[0], _IsolatedConnection):
            raise StorageIsolationError("Use sqlite3.connect with the test connection factory")
    elif event in ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"):
        if args[0].family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("Network access and IP service binding are disabled in tests")
    elif event in ("socket.getaddrinfo", "socket.gethostbyname",
                   "socket.gethostbyaddr", "socket.getnameinfo"):
        raise RuntimeError("Network name resolution is disabled in tests")


# Install before any application storage module can be imported by a test.
# Audit hooks intentionally stay active through fixture/session teardown.
sys.addaudithook(_isolation_audit)
sqlite3.connect = _guarded_connect
sqlite3.dbapi2.connect = _guarded_connect
_sqlite3.connect = _guarded_connect

from alpha_agents import config as app_config  # noqa: E402


def _redirect_config():
    app_config.DATA_DIR = _SANDBOX_DATA_DIR
    app_config.DB_PATH = _SANDBOX_DATA_DIR / "stocks.db"
    app_config.CHROMA_PATH = _SANDBOX_DATA_DIR / "chroma"
    app_config.MEMORY_DB_PATH = _SANDBOX_DATA_DIR / "memory.db"


_redirect_config()

_STORE_PATHS = {
    "memory_store": {"MEMORY_DB_PATH": "memory.db"},
    "activity_log": {"DB_PATH": "activity.db"},
    "report_store": {"REPORTS_DB_PATH": "reports.db"},
    "snapshot_store": {"SNAPSHOTS_DB_PATH": "market_snapshots.db"},
    "market_history": {"DB_PATH": "market_history.db", "PROGRESS_PATH": "init_progress.json"},
    "token_usage": {"_DB": "usage.db"},
    "embeddings": {"CHROMA_PATH": "chroma"},
    "news_index": {"CHROMA_PATH": "chroma"},
}


def _reset_default_storage():
    # Do not silently unlink a live DB owned by another thread. Such tests must
    # close their worker connections before teardown rather than leak workers.
    for conn in tuple(_SANDBOX_CONNECTIONS):
        try:
            conn.close()
        except sqlite3.ProgrammingError as exc:
            raise StorageIsolationError(
                "Close default SQLite connections in their owning threads before teardown"
            ) from exc

    _redirect_config()
    for name, paths in _STORE_PATHS.items():
        module = sys.modules.get(f"alpha_agents.data.{name}")
        if module is None:
            continue
        for attr, filename in paths.items():
            setattr(module, attr, _SANDBOX_DATA_DIR / filename)
        if "DATA_DIR" in vars(module):
            module.DATA_DIR = _SANDBOX_DATA_DIR
        if "_local" in vars(module):
            # Replacing, rather than clearing, invalidates other threads' slots.
            module._local = threading.local()
        if name == "embeddings":
            clear = getattr(module._get_store, "cache_clear", None)
            if clear is not None:
                clear()
        elif name == "news_index":
            module._store = None
    main = sys.modules.get("main")
    if main is not None and "DATA_DIR" in vars(main):
        main.DATA_DIR = _SANDBOX_DATA_DIR

    # Never use mutable config/fixture paths as deletion targets, and never
    # follow symlinks planted inside the sandbox to their outside targets.
    if (_RUN_ROOT.is_symlink() or _RUN_ROOT.resolve() != _RUN_ROOT
            or _SANDBOX_DATA_DIR.is_symlink()
            or _SANDBOX_DATA_DIR.resolve() != _RUN_ROOT / "data"):
        raise StorageIsolationError("Refusing cleanup of a redirected test sandbox")
    for child in _SANDBOX_DATA_DIR.iterdir():
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    # Hooks bracket all fixtures, including unittest setUp/tearDown and module
    # autouse fixtures that would otherwise run ahead of a function fixture.
    _reset_default_storage()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    try:
        yield
    finally:
        _reset_default_storage()


def _cleanup_run():
    _reset_default_storage()
    shutil.rmtree(_RUN_ROOT)


atexit.register(_cleanup_run)

_SKIP_REASON = (
    "Requires real market-history data, which is not supplied to the isolated "
    "test sandbox. Reading project data/market_history.db is forbidden."
)

# No database probe: these legacy tests explicitly read the business corpus.
# Keep their in-memory/schema-only cases runnable.
_NEEDS_MARKET_HISTORY = {
    "test_vpa_regime": {
        "test_301379_2025_11_12_must_be_downtrend",
        "test_300429_2026_01_07_must_emit_supply_warning",
        "test_300443_2025_12_15_must_not_be_downtrend",
        "test_300429_2026_01_06_must_be_long_actionable",
        "test_300429_2026_01_07_must_be_short_watch_or_none",
        "test_301379_2025_11_12_must_not_be_long",
        "test_300443_2025_12_05_must_be_long",
        "test_002066_2025_09_23_must_be_long",
    },
    "test_vpa_v7_e2e_prior_state": {
        "test_prev_trading_day_skips_weekends",
        "test_prev_trading_day_skips_holidays",
        "test_prev_trading_day_returns_none_for_first_known_date",
        "test_e2e_prior_state_propagates_into_user_message",
    },
    "test_vpa_v7_scanner_recall": None,
    "test_vpa_v7_e2e_integration": None,
}


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        module = item.path.stem
        if module in _NEEDS_MARKET_HISTORY:
            names = _NEEDS_MARKET_HISTORY[module]
            name = item.name.split("[", 1)[0]
            if names is None or name in names:
                item.add_marker(skip)
