import asyncio
import json
import logging
import time
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
from alpha_agents.tools.eastmoney_live import get_eastmoney_live_fn
from alpha_agents.news_digest import digest_news
from alpha_agents.agents.strategist import run_analysis

logger = logging.getLogger(__name__)

MAX_SEEN = 1000

# Source registry: (id, display_name, fetch_fn_factory)
NEWS_SOURCES = [
    ("eastmoney", "东方财富", lambda: get_news_fn(limit=NEWS_FETCH_LIMIT)),
    ("eastmoney_live", "东方财富7x24", lambda: get_eastmoney_live_fn(limit=NEWS_FETCH_LIMIT)),
    ("world_rss", "国际RSS", lambda: get_world_news_fn(limit=NEWS_FETCH_LIMIT)),
    ("cls", "财联社电报", lambda: get_cls_telegraph_fn(limit=NEWS_FETCH_LIMIT)),
    ("wallstreetcn", "华尔街见闻", lambda: get_wallstreetcn_fn(limit=NEWS_FETCH_LIMIT)),
    ("whitehouse", "白宫", lambda: get_whitehouse_fn(limit=10)),
    ("pboc", "人民银行", lambda: get_pboc_news_fn(limit=10)),
    ("jin10", "金十数据", lambda: get_jin10_fn(limit=NEWS_FETCH_LIMIT)),
    ("xinhua", "新华社", lambda: get_xinhua_fn(limit=20)),
    ("fed", "美联储", lambda: get_fed_news_fn(limit=10)),
    ("sec", "SEC", lambda: get_sec_news_fn(limit=10)),
    ("social", "社交媒体", lambda: get_social_media_fn(limit=20)),
]


class NewsMonitor:
    def __init__(self, interval: int = MONITOR_INTERVAL_SECONDS, event_bus=None):
        self.interval = interval
        self._seen_titles: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._bus = event_bus  # optional web event bus

    async def _emit(self, stage: str, status: str, message: str = "", data: dict | None = None):
        """Emit a pipeline event if web event bus is attached."""
        if self._bus is None:
            return
        from alpha_agents.web.events import StageEvent, StageStatus
        status_map = {
            "running": StageStatus.RUNNING,
            "success": StageStatus.SUCCESS,
            "error": StageStatus.ERROR,
            "idle": StageStatus.IDLE,
        }
        await self._bus.emit(StageEvent(
            stage=stage,
            status=status_map.get(status, StageStatus.IDLE),
            message=message,
            data=data or {},
        ))

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

    async def _fetch_one(self, source_id: str, name: str, fetch_fn) -> tuple[str, list[dict]]:
        """Fetch a single source in a thread (non-blocking)."""
        await self._emit(f"source_{source_id}", "running", f"抓取 {name}...")
        try:
            raw = await asyncio.to_thread(fetch_fn)
            data = json.loads(raw)
            items = data.get("news", [])
            if items:
                logger.debug("Fetched %d items from %s", len(items), name)
            await self._emit(f"source_{source_id}", "success",
                             f"{name}: {len(items)}条", {"count": len(items)})
            return source_id, items
        except Exception:
            logger.warning("Failed to fetch from %s", name, exc_info=True)
            await self._emit(f"source_{source_id}", "error", f"{name}: 抓取失败")
            return source_id, []

    async def _fetch_all_sources(self) -> list[dict]:
        """Fetch from all news sources concurrently, return combined list."""
        await self._emit("fetch", "running", f"正在从{len(NEWS_SOURCES)}个源并发抓取...")

        tasks = [
            self._fetch_one(sid, name, fn)
            for sid, name, fn in NEWS_SOURCES
        ]
        results = await asyncio.gather(*tasks)

        news_items: list[dict] = []
        source_results = {}
        for source_id, items in results:
            news_items.extend(items)
            source_results[source_id] = len(items)

        await self._emit("fetch", "success",
                         f"共抓取{len(news_items)}条新闻",
                         {"total": len(news_items), "sources": source_results})
        return news_items

    async def run(self) -> None:
        logger.info("News monitor started. Interval: %ds", self.interval)
        cycle = 0
        while True:
            cycle += 1
            try:
                await self._emit("pipeline", "running", f"第{cycle}轮监控开始")

                # 1. Fetch from all sources
                raw_items = await self._fetch_all_sources()
                new_items = self.deduplicate(raw_items)

                await self._emit("dedup", "success",
                                 f"去重后{len(new_items)}条新消息（原始{len(raw_items)}条）",
                                 {"raw": len(raw_items), "new": len(new_items)})

                if not new_items:
                    logger.debug("No new news items")
                    await self._emit("pipeline", "idle",
                                     f"无新消息，{self.interval}秒后重试")
                    await asyncio.sleep(self.interval)
                    continue

                logger.info("Found %d new news items, running digest...", len(new_items))

                # 2. Cheap model pre-filters and aggregates into events
                await self._emit("digest", "running",
                                 f"正在用便宜模型筛选{len(new_items)}条新闻...")
                events = await digest_news(new_items)

                if not events:
                    logger.info("No significant events after digest filtering")
                    await self._emit("digest", "success", "无重要事件",
                                     {"event_count": 0})
                    await self._emit("pipeline", "idle",
                                     f"本轮无重要事件，{self.interval}秒后重试")
                    await asyncio.sleep(self.interval)
                    continue

                # Emit digest results with category breakdown
                categories = {}
                for e in events:
                    cat = e.get("category", "未知")
                    categories[cat] = categories.get(cat, 0) + 1

                await self._emit("digest", "success",
                                 f"筛选出{len(events)}条重要事件",
                                 {"event_count": len(events),
                                  "categories": categories,
                                  "events": events})

                logger.info(
                    "Digest produced %d events (top: [%s] %s, importance=%d)",
                    len(events),
                    events[0].get("category", "?"),
                    events[0].get("event", "?"),
                    events[0].get("importance", 0),
                )

                # 3. Feed digested events to the expensive strategist Agent
                await self._emit("agent", "running",
                                 "Agent正在深度分析...")
                events_json = json.dumps(events, ensure_ascii=False, indent=2)
                prompt = (
                    f"以下是经过预处理的{len(events)}条重要事件摘要，"
                    f"请对高重要性事件进行深度多市场影响分析，输出完整分析报告：\n\n"
                    f"{events_json}"
                )
                result = await run_analysis(prompt)

                # Store report
                report = {
                    "cycle": cycle,
                    "timestamp": time.time(),
                    "event_count": len(events),
                    "categories": categories,
                    "events_summary": [
                        {"event": e.get("event"), "category": e.get("category"),
                         "importance": e.get("importance")}
                        for e in events
                    ],
                    "report": result,
                }
                if self._bus:
                    self._bus.add_report(report)

                await self._emit("agent", "success", "分析完成",
                                 {"report_preview": result[:500]})
                await self._emit("pipeline", "success",
                                 f"第{cycle}轮分析完成",
                                 {"cycle": cycle})

                print(result)

            except Exception:
                logger.exception("Error in monitor loop")
                await self._emit("pipeline", "error", "监控循环出错")

            await asyncio.sleep(self.interval)
