"""An order needs a theme the system actually tracks.

check_pending_orders already refuses to hold a position whose theme is not
in theme_lines — "no theme, no logical basis". That rule only ran on the
next cycle, so an order was created from whatever concept string the agent
wrote and then cancelled with "关联主线'磷化工'不存在". Observed live: 雅克
科技 was ordered under 磷化工, a theme that never existed, and killed one
cycle later — a capital slot spent on an idea the system had already
decided it would not hold.
"""

from unittest.mock import patch

import pytest

from alpha_agents.data import portfolio


def with_themes(names):
    return patch("alpha_agents.data.memory_store.get_active_themes",
                 return_value=[{"name": n} for n in names])


class TestResolveTheme:
    def test_exact_match(self):
        with with_themes(["小金属概念", "金属铜"]):
            assert portfolio.resolve_theme("小金属概念") == "小金属概念"

    def test_compound_label_resolves_to_the_tracked_part(self):
        """Agents write "化肥/磷化工" for a theme tracked as one of its parts."""
        with with_themes(["磷化工", "国企改革"]):
            assert portfolio.resolve_theme("化肥/磷化工") == "磷化工"

    @pytest.mark.parametrize("sep", ["/", "、", ",", "，", "|"])
    def test_every_separator_the_agents_use(self, sep):
        with with_themes(["磷化工"]):
            assert portfolio.resolve_theme(f"化肥{sep}磷化工") == "磷化工"

    def test_containment_either_direction(self):
        with with_themes(["国企改革"]):
            assert portfolio.resolve_theme("国企改革 ") == "国企改革"
            assert portfolio.resolve_theme("国企改革主线") == "国企改革"

    def test_unknown_theme_is_rejected(self):
        with with_themes(["小金属概念"]):
            assert portfolio.resolve_theme("磷化工") is None

    @pytest.mark.parametrize("value", ["", None])
    def test_missing_theme_is_rejected(self, value):
        with with_themes(["小金属概念"]):
            assert portfolio.resolve_theme(value) is None

    def test_lookup_failure_does_not_drop_the_idea(self):
        """An unrelated DB failure must not silently discard orders."""
        with patch("alpha_agents.data.memory_store.get_active_themes",
                   side_effect=RuntimeError("db down")):
            assert portfolio.resolve_theme("磷化工") == "磷化工"


class TestCreateOrderRejects:
    @pytest.fixture()
    def store(self, tmp_path, monkeypatch):
        """portfolio shares memory_store's connection, so patch it there."""
        from alpha_agents.data import memory_store
        monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                            tmp_path / "memory.db", raising=False)
        monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
        yield
        conn = getattr(memory_store._local, "conn", None)
        if conn is not None:
            conn.close()
        memory_store._local.conn = None

    def test_untracked_theme_creates_no_order(self, store, caplog):
        with with_themes(["小金属概念"]), caplog.at_level("WARNING"):
            got = portfolio.create_pending_order(
                code="002409", name="雅克科技", theme="磷化工",
                order_date="2026-09-08", entry_low=1.0, entry_high=2.0,
            )

        assert got is None
        assert any("磷化工" in r.getMessage() for r in caplog.records), caplog.text

    def test_tracked_theme_is_stored_canonically(self, store):
        with with_themes(["磷化工"]):
            order_id = portfolio.create_pending_order(
                code="002409", name="雅克科技", theme="化肥/磷化工",
                order_date="2026-09-08", entry_low=1.0, entry_high=2.0,
            )

        assert order_id is not None
        pending = portfolio.get_pending_orders()
        assert [o["theme"] for o in pending] == ["磷化工"], (
            "the compound label must not reach check_pending_orders, which "
            "looks the theme up by exact name"
        )
