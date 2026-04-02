import asyncio
import json
import logging
from collections import deque

from alpha_agents.config import MONITOR_INTERVAL_SECONDS, NEWS_FETCH_LIMIT
from alpha_agents.tools.news import get_news_fn
from alpha_agents.tools.world_news import get_world_news_fn
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

    async def run(self) -> None:
        logger.info("News monitor started. Interval: %ds", self.interval)
        while True:
            try:
                # Fetch both domestic and international news
                domestic_raw = get_news_fn(limit=NEWS_FETCH_LIMIT)
                domestic_data = json.loads(domestic_raw)
                news_items = domestic_data.get("news", [])

                world_raw = get_world_news_fn(limit=NEWS_FETCH_LIMIT)
                world_data = json.loads(world_raw)
                news_items.extend(world_data.get("news", []))

                new_items = self.deduplicate(news_items)
                if new_items:
                    logger.info("Found %d new news items", len(new_items))
                    news_summary = "\n".join(
                        f"- [{item['time']}] {item['title']}: {item['summary']}"
                        for item in new_items
                    )
                    prompt = (
                        f"以下是最新获取的{len(new_items)}条财经新闻，请分析是否有值得关注的事件，"
                        f"如有，自主完成完整的分析流程并输出推荐报告：\n\n{news_summary}"
                    )
                    result = await run_analysis(prompt)
                    print(result)
                else:
                    logger.debug("No new news items")

            except Exception:
                logger.exception("Error in monitor loop")

            await asyncio.sleep(self.interval)
