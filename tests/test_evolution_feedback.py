"""Tests for alpha_agents.evolution.feedback."""
from unittest.mock import patch


def test_inject_sentiment_returns_formatted_block():
    fake_cycle = {"phase": "升温", "confidence": 0.8,
                  "strategy": "可追强势，高beta优先"}
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=fake_cycle):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert "【情绪周期】" in result
    assert "升温" in result
    assert "可追强势" in result


def test_inject_sentiment_returns_empty_when_db_empty():
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=None):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert result == ""


def test_inject_sentiment_fits_budget():
    """Sentiment section budget is 50 chars per spec."""
    fake_cycle = {"phase": "升温", "confidence": 0.8,
                  "strategy": "可追强势，高beta优先，" + "额外说明" * 30}
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=fake_cycle):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert len(result) <= 80  # 50 char budget + small header overhead
