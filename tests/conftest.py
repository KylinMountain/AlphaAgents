"""Shared fixtures and environment gating.

Some tests read data/market_history.db — the full-market K-line store,
about 1.1 GB, deliberately not in the repository. Without it they cannot
run, and a failure would be a lie: the code is fine, the data is absent.
They are skipped instead, and the skip says so.
"""

import pytest

from alpha_agents.config import DATA_DIR

MARKET_HISTORY_DB = DATA_DIR / "market_history.db"

_SKIP_REASON = (
    "需要 data/market_history.db (约1.1GB，不入库)。"
    "本地可从可信备份 symlink，见 deploy/README.md。"
)

# Test modules whose subjects read the K-line store directly.
_NEEDS_MARKET_HISTORY = (
    "test_vpa_regime",
    "test_vpa_v7_e2e_prior_state",
    "test_vpa_v7_scanner_recall",
)


def _has_market_history() -> bool:
    """True when the store exists and actually holds bars.

    Existence is not enough: starting the server creates an empty schema,
    and a test asserting on real prices would then fail rather than skip.
    """
    if not MARKET_HISTORY_DB.exists():
        return False
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{MARKET_HISTORY_DB}?mode=ro", uri=True)
        try:
            n = conn.execute(
                "SELECT 1 FROM daily_kline LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return n is not None
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    if _has_market_history():
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if any(name in item.nodeid for name in _NEEDS_MARKET_HISTORY):
            item.add_marker(skip)
