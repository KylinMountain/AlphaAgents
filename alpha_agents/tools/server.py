from claude_agent_sdk import tool, create_sdk_mcp_server

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
from alpha_agents.tools.stock_search import search_stocks_fn
from alpha_agents.tools.sector import get_sector_data_fn
from alpha_agents.tools.stock_filter import filter_stocks_fn
from alpha_agents.tools.watchlist import get_watchlist_fn


@tool("get_news", "获取最新财经新闻。可按关键词过滤。", {"limit": int, "keyword": str})
async def get_news(args):
    result = get_news_fn(limit=args.get("limit", 50), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_world_news", "获取国际新闻（路透社、AP、BBC、CNBC等）。用于获取地缘政治、国际时事等信息。", {"limit": int, "keyword": str})
async def get_world_news(args):
    result = get_world_news_fn(limit=args.get("limit", 30), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_cls_telegraph", "获取财联社电报快讯。A股最快的实时新闻源。", {"limit": int, "keyword": str})
async def get_cls_telegraph(args):
    result = get_cls_telegraph_fn(limit=args.get("limit", 30), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_wallstreetcn", "获取华尔街见闻快讯。国际财经新闻中文解读。", {"limit": int, "keyword": str})
async def get_wallstreetcn(args):
    result = get_wallstreetcn_fn(limit=args.get("limit", 30), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_whitehouse", "获取白宫官方声明和行政令。追踪美国政策动态。", {"limit": int, "keyword": str})
async def get_whitehouse(args):
    result = get_whitehouse_fn(limit=args.get("limit", 20), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_pboc_news", "获取中国人民银行公告。追踪货币政策动态。", {"limit": int, "keyword": str})
async def get_pboc_news(args):
    result = get_pboc_news_fn(limit=args.get("limit", 20), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_jin10", "获取金十数据实时快讯。覆盖全球宏观、外汇、商品。", {"limit": int, "keyword": str})
async def get_jin10(args):
    result = get_jin10_fn(limit=args.get("limit", 30), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_xinhua", "获取新华社财经新闻。国内官方政策信号。", {"limit": int, "keyword": str})
async def get_xinhua(args):
    result = get_xinhua_fn(limit=args.get("limit", 20), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_fed_news", "获取美联储新闻发布。追踪美国货币政策。", {"limit": int, "keyword": str})
async def get_fed_news(args):
    result = get_fed_news_fn(limit=args.get("limit", 20), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_sec_news", "获取SEC新闻发布。追踪美国证券监管动态。", {"limit": int, "keyword": str})
async def get_sec_news(args):
    result = get_sec_news_fn(limit=args.get("limit", 20), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_social_media", "获取特朗普(Truth Social)和马斯克(X)的最新动态。", {"limit": int, "keyword": str})
async def get_social_media(args):
    result = get_social_media_fn(limit=args.get("limit", 20), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("get_eastmoney_live", "获取东方财富7x24小时实时快讯。全天候财经快讯流。", {"limit": int, "keyword": str})
async def get_eastmoney_live(args):
    result = get_eastmoney_live_fn(limit=args.get("limit", 30), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("search_stocks", "根据概念/板块关键词检索相关个股。从本地同花顺概念板块索引中模糊匹配。", {"keyword": str})
async def search_stocks(args):
    result = search_stocks_fn(keyword=args["keyword"])
    return {"content": [{"type": "text", "text": result}]}


@tool("get_sector_data", "获取板块行情数据，包括涨跌幅和资金流向。", {"sector_name": str})
async def get_sector_data(args):
    result = get_sector_data_fn(sector_name=args["sector_name"])
    return {"content": [{"type": "text", "text": result}]}


@tool("filter_stocks", "过滤不适合的个股（剔除ST、停牌、市值过小）。", {"stock_codes": list})
async def filter_stocks(args):
    result = filter_stocks_fn(stock_codes=args["stock_codes"])
    return {"content": [{"type": "text", "text": result}]}


@tool("get_watchlist", "读取用户自选股列表。", {})
async def get_watchlist(args):
    result = get_watchlist_fn()
    return {"content": [{"type": "text", "text": result}]}


def create_tools_server():
    return create_sdk_mcp_server(
        "alpha-agents-data",
        tools=[
            get_news, get_eastmoney_live, get_world_news,
            get_cls_telegraph, get_wallstreetcn,
            get_whitehouse, get_pboc_news, get_jin10, get_xinhua,
            get_fed_news, get_sec_news, get_social_media,
            search_stocks, get_sector_data, filter_stocks, get_watchlist,
        ],
    )
