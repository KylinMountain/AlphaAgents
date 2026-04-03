import asyncio
import json
import logging
import time
from collections import deque

from alpha_agents.config import MONITOR_INTERVAL_SECONDS, NEWS_FETCH_LIMIT
from alpha_agents.sources.eastmoney import get_news_fn
from alpha_agents.sources.world_news import get_world_news_fn
from alpha_agents.sources.cls_telegraph import get_cls_telegraph_fn
from alpha_agents.sources.wallstreetcn import get_wallstreetcn_fn
from alpha_agents.sources.whitehouse import get_whitehouse_fn
from alpha_agents.sources.pboc import get_pboc_news_fn
from alpha_agents.sources.jin10 import get_jin10_fn
from alpha_agents.sources.xinhua import get_xinhua_fn
from alpha_agents.sources.fed import get_fed_news_fn
from alpha_agents.sources.sec import get_sec_news_fn
from alpha_agents.sources.truthsocial import get_social_media_fn
from alpha_agents.sources.eastmoney_live import get_eastmoney_live_fn
from alpha_agents.pipeline.digest import digest_news
from alpha_agents.agents.strategist import run_analysis
from alpha_agents.agents.futures import run_futures_analysis
from alpha_agents.data.report_store import save_report, save_predictions, save_event, link_events
from alpha_agents.pipeline.event_linker import analyze_event_links
from alpha_agents.notify import notify_all, format_report_notification
from alpha_agents.pipeline.source_health import health_tracker

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


async def route_and_analyze(events: list[dict], event_bus=None) -> dict[str, str]:
    """Route events by target_market and run stock/futures agents in parallel.

    Returns dict with "stock" and "futures" report strings (may be empty).
    If event_bus is provided, tool call events are broadcast in real-time.
    """
    from alpha_agents.agents.hooks import ToolEventHooks

    stock_events = [e for e in events if e.get("target_market", "both") in ("stock", "both")]
    futures_events = [e for e in events if e.get("target_market", "both") in ("futures", "both")]

    logger.info("Routing: %d stock events, %d futures events",
                len(stock_events), len(futures_events))

    # Build hooks that emit tool call and reasoning events to the event bus
    def _make_emitter(agent_label):
        if event_bus is None:
            return None
        def emit(hook_event):
            import asyncio
            from alpha_agents.web.events import StageEvent, StageStatus
            evt_type = hook_event.get("type", "")
            stage = f"tool_{agent_label}"
            data = {"agent": hook_event.get("agent", ""), "event_type": evt_type}

            if evt_type == "tool_start":
                status = StageStatus.RUNNING
                message = hook_event["tool"]
                data["tool"] = hook_event["tool"]
                data["tool_status"] = "start"
            elif evt_type == "tool_end":
                status = StageStatus.SUCCESS
                message = f"{hook_event['tool']} ✓"
                data["tool"] = hook_event["tool"]
                data["tool_status"] = "end"
                data["result_preview"] = hook_event.get("result_preview", "")
            elif evt_type == "reasoning":
                status = StageStatus.RUNNING
                message = hook_event.get("text", "")
                data["text"] = hook_event.get("text", "")
            elif evt_type == "handoff":
                status = StageStatus.RUNNING
                message = hook_event.get("text", "handoff")
                data["tool"] = hook_event.get("tool", "")
                data["text"] = hook_event.get("text", "")
            elif evt_type == "agent_start":
                status = StageStatus.RUNNING
                message = hook_event.get("text", "")
                data["text"] = hook_event.get("text", "")
            else:
                return

            event = StageEvent(stage=stage, status=status, message=message, data=data)
            # Fire-and-forget emit (we're in a sync callback)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(event_bus.emit(event))
            except RuntimeError:
                pass
        return emit

    stock_hooks = ToolEventHooks(_make_emitter("stock"), agent_label="股票策略师") if event_bus else None
    futures_hooks = ToolEventHooks(_make_emitter("futures"), agent_label="期货策略师") if event_bus else None

    analysis_tasks = []
    if stock_events:
        stock_json = json.dumps(stock_events, ensure_ascii=False, indent=2)
        stock_prompt = (
            f"以下是经过预处理的{len(stock_events)}条重要事件摘要，"
            f"请对高重要性事件进行深度多市场影响分析，输出完整分析报告：\n\n"
            f"{stock_json}"
        )
        analysis_tasks.append(("stock", run_analysis(stock_prompt, hooks=stock_hooks)))

    if futures_events:
        futures_json = json.dumps(futures_events, ensure_ascii=False, indent=2)
        futures_prompt = (
            f"以下是经过预处理的{len(futures_events)}条重要事件摘要，"
            f"请分析这些事件对期货市场各品种的影响，输出完整期货分析报告：\n\n"
            f"{futures_json}"
        )
        analysis_tasks.append(("futures", run_futures_analysis(futures_prompt, hooks=futures_hooks)))

    results_map: dict[str, str] = {}
    if analysis_tasks:
        labels = [t[0] for t in analysis_tasks]
        coros = [t[1] for t in analysis_tasks]
        outputs = await asyncio.gather(*coros, return_exceptions=True)
        for label, output in zip(labels, outputs):
            if isinstance(output, Exception):
                logger.error("%s agent failed: %s", label, output)
                results_map[label] = f"[{label} agent error: {output}]"
            else:
                results_map[label] = output

    return {
        "stock": results_map.get("stock", ""),
        "futures": results_map.get("futures", ""),
        "stock_events": stock_events,
        "futures_events": futures_events,
    }


class NewsMonitor:
    def __init__(self, interval: int = MONITOR_INTERVAL_SECONDS, event_bus=None):
        self.interval = interval
        self._seen_titles: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._bus = event_bus  # optional web event bus
        # Register all sources for health tracking
        for sid, name, _ in NEWS_SOURCES:
            health_tracker.register(sid, name)

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
        if health_tracker.should_skip(source_id):
            logger.debug("Skipping unhealthy source %s", name)
            await self._emit(f"source_{source_id}", "error",
                             f"{name}: 暂时跳过(不健康)", {"skipped": True})
            return source_id, []

        await self._emit(f"source_{source_id}", "running", f"抓取 {name}...")
        try:
            raw = await asyncio.to_thread(fetch_fn)
            data = json.loads(raw)
            items = data.get("news", [])
            if items:
                logger.debug("Fetched %d items from %s", len(items), name)
            health_tracker.record_success(source_id, len(items))
            await self._emit(f"source_{source_id}", "success",
                             f"{name}: {len(items)}条", {"count": len(items)})
            return source_id, items
        except Exception as exc:
            logger.warning("Failed to fetch from %s", name, exc_info=True)
            health_tracker.record_failure(source_id, str(exc))
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
        consecutive_errors = 0
        max_consecutive_errors = 10
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

                # 3. Route events and run agents in parallel
                await self._emit("agent", "running", "Agent正在深度分析...")
                results = await route_and_analyze(events, event_bus=self._bus)
                stock_events = results["stock_events"]
                futures_events = results["futures_events"]
                stock_result = results["stock"]
                futures_result = results["futures"]

                # Combine results for display
                combined_result = ""
                if stock_result:
                    combined_result += stock_result
                if futures_result:
                    if combined_result:
                        combined_result += "\n\n" + "=" * 50 + "\n\n"
                    combined_result += futures_result

                if not combined_result:
                    combined_result = "[No analysis produced]"

                # 5. Persist reports to SQLite
                ts = time.time()
                today = time.strftime("%Y-%m-%d")
                report_id = await asyncio.to_thread(
                    save_report, cycle, ts, events, categories, combined_result)
                logger.info("Saved report #%d", report_id)

                # 6. Extract predictions from events and save
                predictions = []
                event_ids = []
                for e in events:
                    mi = e.get("market_impact", {})
                    a_share = mi.get("a_share", {})
                    cat = e.get("category", "")
                    evt_summary = e.get("event", "")

                    # Save event node for graph
                    eid = await asyncio.to_thread(
                        save_event, evt_summary, cat,
                        e.get("importance", 0), ts, e.get("summary", ""), report_id)
                    event_ids.append(eid)

                    for sector in a_share.get("sectors_bullish", []):
                        if isinstance(sector, str):
                            predictions.append({"direction": "bullish", "sector": sector,
                                                "category": cat, "event_summary": evt_summary})
                        elif isinstance(sector, dict):
                            predictions.append({"direction": "bullish",
                                                "sector": sector.get("name", sector.get("sector", "")),
                                                "strength": sector.get("strength", 0),
                                                "reason": sector.get("reason", ""),
                                                "category": cat, "event_summary": evt_summary})
                    for sector in a_share.get("sectors_bearish", []):
                        if isinstance(sector, str):
                            predictions.append({"direction": "bearish", "sector": sector,
                                                "category": cat, "event_summary": evt_summary})
                        elif isinstance(sector, dict):
                            predictions.append({"direction": "bearish",
                                                "sector": sector.get("name", sector.get("sector", "")),
                                                "strength": sector.get("strength", 0),
                                                "reason": sector.get("reason", ""),
                                                "category": cat, "event_summary": evt_summary})

                if predictions:
                    await asyncio.to_thread(save_predictions, report_id, today, predictions)
                    logger.info("Saved %d predictions for %s", len(predictions), today)

                # 7. LLM analyzes causal relationships between events
                if len(events) >= 2:
                    llm_links = await analyze_event_links(events)
                    n_events = len(event_ids)
                    for lnk in llm_links:
                        si, ti = lnk["source"], lnk["target"]
                        if si >= n_events or ti >= n_events:
                            logger.warning("Event link index out of range: %d->%d (max %d)", si, ti, n_events - 1)
                            continue
                        await asyncio.to_thread(
                            link_events, event_ids[si], event_ids[ti],
                            lnk["relation"], lnk["confidence"],
                            lnk.get("reason", ""))
                    if llm_links:
                        logger.info("Linked %d event relationships", len(llm_links))

                # 8. Push to web event bus
                report = {
                    "cycle": cycle,
                    "timestamp": ts,
                    "event_count": len(events),
                    "categories": categories,
                    "routes": {
                        "stock": len(stock_events),
                        "futures": len(futures_events),
                    },
                    "events_summary": [
                        {"event": e.get("event"), "category": e.get("category"),
                         "importance": e.get("importance"),
                         "target_market": e.get("target_market")}
                        for e in events
                    ],
                    "report": stock_result,
                    "futures_report": futures_result,
                }
                if self._bus:
                    self._bus.add_report(report)

                # 9. Push notifications (separate for stock/futures)
                if stock_result:
                    title, body = format_report_notification(stock_events, stock_result[:500])
                    await asyncio.to_thread(notify_all, title, body)
                if futures_result:
                    title = f"AlphaAgents 期货: {futures_events[0].get('event', '?')[:30]}"
                    body = f"共{len(futures_events)}条期货相关事件\n\n{futures_result[:500]}"
                    await asyncio.to_thread(notify_all, title, body)

                await self._emit("agent", "success", "分析完成",
                                 {"stock_preview": stock_result[:300],
                                  "futures_preview": futures_result[:300]})
                await self._emit("pipeline", "success",
                                 f"第{cycle}轮分析完成 (股票:{len(stock_events)} 期货:{len(futures_events)})",
                                 {"cycle": cycle})

                # Print both reports
                if stock_result:
                    print(stock_result)
                if futures_result:
                    print("\n" + "=" * 50)
                    print(futures_result)
                consecutive_errors = 0  # reset on success

            except Exception:
                consecutive_errors += 1
                logger.exception("Error in monitor loop (consecutive: %d/%d)",
                                 consecutive_errors, max_consecutive_errors)
                await self._emit("pipeline", "error",
                                 f"监控循环出错 ({consecutive_errors}/{max_consecutive_errors})")
                if consecutive_errors >= max_consecutive_errors:
                    logger.critical("Too many consecutive errors (%d), stopping monitor", consecutive_errors)
                    await self._emit("pipeline", "error", "连续错误过多，监控已停止")
                    break

            await asyncio.sleep(self.interval)
