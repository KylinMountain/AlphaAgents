import json
from unittest.mock import patch, AsyncMock

import pytest

from alpha_agents.monitor import NewsMonitor


@pytest.fixture
def monitor():
    return NewsMonitor(interval=10)


def test_monitor_dedup(monitor):
    """Same news title should not trigger twice."""
    news_batch_1 = [
        {"title": "特朗普加关税", "summary": "...", "time": "2026-04-02 08:00", "source": "新浪"},
        {"title": "央行降准", "summary": "...", "time": "2026-04-02 07:00", "source": "东方财富"},
    ]
    new_items = monitor.deduplicate(news_batch_1)
    assert len(new_items) == 2

    # Second call with same news + one new
    news_batch_2 = [
        {"title": "特朗普加关税", "summary": "...", "time": "2026-04-02 08:00", "source": "新浪"},
        {"title": "新的重大消息", "summary": "...", "time": "2026-04-02 09:00", "source": "财联社"},
    ]
    new_items = monitor.deduplicate(news_batch_2)
    assert len(new_items) == 1
    assert new_items[0]["title"] == "新的重大消息"


def test_monitor_seen_limit(monitor):
    """Seen set should not grow unbounded."""
    for i in range(2000):
        monitor.deduplicate([{"title": f"news_{i}", "summary": "", "time": "", "source": ""}])
    assert len(monitor._seen_titles) <= 1500
