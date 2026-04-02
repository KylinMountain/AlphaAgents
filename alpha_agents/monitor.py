import asyncio
import json
import logging
from collections import deque

from alpha_agents.config import MONITOR_INTERVAL_SECONDS, NEWS_FETCH_LIMIT
from alpha_agents.tools.news import get_news_fn
from alpha_agents.tools.world_news import get_world_news_fn
from alpha_agents.tools.cls_telegraph import get_cls_telegraph_fn
from alpha_agents.tools.wallstreetcn import get_wallstreetcn_fn
from alpha_agents.tools.whitehouse import get_whitehouse_fn
from alpha_agents.tools.pboc import get_pboc_news_fn
from alpha_agents.tools.jin10 import get_jin10_fn
from alpha_agents.tools.xinhua import get_xinhua_fn
from alpha_agents.tools.fed import get_fed_news_fn
from alpha_agents.tools.sec import get_sec_news_fn
from alpha_agents.tools.truthsocial import get_social_media_fn
from alpha_agents.news_digest import digest_news
from alpha_agents.agents.strategist import run_analysis

logger = logging.getLogger(__name__)

MAX_SEEN = 1000


class NewsMonitor:
    def __init__(self, interval: int = MONITOR_INTERVAL_SECONDS):
        self.interval = interval
        self._seen_titles: set[str] = set()
        self._seen_order: deque[str] = deque()

    def deduplicate(self, news_items: list[dict]) -> list[dict]:
        new_items = []
        for item in news_items:
            title = item["title"]
            if title not in self._seen_titles:
                self._seen_titles.add(title)
                self._seen_order.append(title)
                new_items.append(item)

        # Evict oldest to keep bounded
        while len(self._seen_titles) > MAX_SEEN:
            old = self._seen_order.popleft()
            self._seen_titles.discard(old)

        return new_items

    def _fetch_all_sources(self) -> list[dict]:
        """Fetch from all news sources, return combined list."""
        news_items: list[dict] = []
        sources = [
            ("东方财富", lambda: get_news_fn(limit=NEWS_FETCH_LIMIT)),
            ("国际RSS", lambda: get_world_news_fn(limit=NEWS_FETCH_LIMIT)),
            ("财联社电报", lambda: get_cls_telegraph_fn(limit=NEWS_FETCH_LIMIT)),
            ("华尔街见闻", lambda: get_wallstreetcn_fn(limit=NEWS_FETCH_LIMIT)),
            ("白宫", lambda: get_whitehouse_fn(limit=10)),
            ("人民银行", lambda: get_pboc_news_fn(limit=10)),
            ("金十数据", lambda: get_jin10_fn(limit=NEWS_FETCH_LIMIT)),
            ("新华社", lambda: get_xinhua_fn(limit=20)),
            ("美联储", lambda: get_fed_news_fn(limit=10)),
            ("SEC", lambda: get_sec_news_fn(limit=10)),
            ("社交媒体", lambda: get_social_media_fn(limit=20)),
        ]
        for name, fetch in sources:
            try:
                data = json.loads(fetch())
                items = data.get("news", [])
                news_items.extend(items)
                if items:
                    logger.debug("Fetched %d items from %s", len(items), name)
            except Exception:
                logger.warning("Failed to fetch from %s", name, exc_info=True)
        return news_items

    async def run(self) -> None:
        logger.info("News monitor started. Interval: %ds", self.interval)
        while True:
            try:
                # 1. Fetch from all sources
                raw_items = self._fetch_all_sources()
                new_items = self.deduplicate(raw_items)

                if not new_items:
                    logger.debug("No new news items")
                    await asyncio.sleep(self.interval)
                    continue

                logger.info("Found %d new news items, running digest...", len(new_items))

                # 2. Cheap model pre-filters and aggregates into events
                events = await digest_news(new_items)

                if not events:
                    logger.info("No significant events after digest filtering")
                    await asyncio.sleep(self.interval)
                    continue

                logger.info(
                    "Digest produced %d events (top: %s, importance=%d)",
                    len(events),
                    events[0].get("event", "?"),
                    events[0].get("importance", 0),
                )

                # 3. Feed digested events to the expensive strategist Agent
                events_json = json.dumps(events, ensure_ascii=False, indent=2)
                prompt = (
                    f"以下是经过预处理的{len(events)}条重要事件摘要，"
                    f"请对高重要性事件进行深度多市场影响分析，输出完整分析报告：\n\n"
                    f"{events_json}"
                )
                result = await run_analysis(prompt)
                print(result)

            except Exception:
                logger.exception("Error in monitor loop")

            await asyncio.sleep(self.interval)
