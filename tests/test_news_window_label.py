"""The news block must say which window it is, not which day it started.

The header read ``## 截至 {prev_day} 的新闻窗口`` — "news as of the previous
trading day". The window it described runs ``prev_day 15:00 → day 09:00``,
so the label named the *start* as if it were the end. Seen in a live replay
on the 2026-01-05 decision:

    ## 截至 2025-12-31 的新闻窗口
      • 2026-01-05 08:58:14 [财联社电报] 小米汽车公布疲劳驾驶干预专利

Flashes from 08:58 that morning, presented as five days stale. Nothing
here is a look-ahead — 08:58 is legitimately visible to a 09:00 decision —
but an agent told its news is old discounts it, which changes the decision
without changing the data. Same shape as every other defect this day
turned up: the label and the quantity disagree.

The label is now derived next to the bounds themselves and follows the
phase, because the buy path can also run at the close, where the window is
``day 09:00 → day 14:55`` and "overnight" would be the wrong word again.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402

from alpha_agents.agents import sector_stock_selector as S  # noqa: E402


class TestTheLabelMatchesTheBounds:
    def test_the_open_window_is_named_overnight(self):
        label = wf._news_window_label("2026-01-05", "2025-12-31", "open")
        assert "2025-12-31 15:00" in label and "2026-01-05 09:00" in label
        assert "隔夜" in label and "今晨" in label

    def test_the_close_window_is_named_intraday(self):
        label = wf._news_window_label("2026-01-05", "2025-12-31", "close")
        assert "2026-01-05 09:00" in label and "2026-01-05 14:55" in label
        assert "不含隔夜" in label

    def test_it_agrees_with_the_query_it_describes(self):
        """The bounds and their description live in the same file, next to
        each other, so they cannot drift the way they just did."""
        src = inspect.getsource(wf._news_window)
        assert 'f"{day} 09:00:00", f"{prev_day} 15:00:00"' in src
        assert 'f"{day} 14:55:00", f"{day} 09:00:00"' in src


class TestThePromptCarriesIt:
    def test_the_header_no_longer_says_as_of_the_previous_day(self):
        text = (Path(wf.__file__).resolve().parents[1]
                / "alpha_agents" / "prompts" / "sector_stock_select.md").read_text()
        assert "截至 {prev_day} 的新闻窗口" not in text
        assert "{news_window}" in text

    def test_the_rendered_block_shows_the_window(self):
        msg = S.build_message(
            day="2026-01-05", prev_day="2025-12-31", panel=[], news=[],
            market="", book="", knowledge="", trader_note="", picks=None,
            news_window=wf._news_window_label("2026-01-05", "2025-12-31", "open"))
        line = next(l for l in msg.splitlines() if "新闻窗口" in l)
        assert "2026-01-05 09:00" in line

    def test_the_caller_passes_the_computed_label(self):
        src = inspect.getsource(wf._sector_stock_choice)
        assert "news_window=_news_window_label(day, prev_day, phase)" in src

    def test_a_caller_that_says_nothing_still_gets_a_window_not_a_day(self):
        """The default is the shape, not the old "as of" wording."""
        msg = S.build_message(
            day="2026-01-05", prev_day="2025-12-31", panel=[], news=[],
            market="", book="", knowledge="", trader_note="", picks=None)
        line = next(l for l in msg.splitlines() if "新闻窗口" in l)
        assert "→" in line and "09:00" in line
