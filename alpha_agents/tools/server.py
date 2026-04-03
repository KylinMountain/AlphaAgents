"""Register all data tools for OpenAI Agents SDK."""

from agents import function_tool

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
from alpha_agents.tools.web_search import web_search_fn
from alpha_agents.tools.web_fetch import web_fetch_fn
from alpha_agents.tools.pizzint import get_pizzint_fn
from alpha_agents.tools.sector import get_sector_data_fn
from alpha_agents.tools.stock_filter import filter_stocks_fn
from alpha_agents.tools.watchlist import get_watchlist_fn


@function_tool
def get_news(limit: int = 50, keyword: str = "") -> str:
    """获取最新财经新闻。可按关键词过滤。"""
    return get_news_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_world_news(limit: int = 30, keyword: str = "") -> str:
    """获取国际新闻（路透社、AP、BBC、CNBC等）。用于获取地缘政治、国际时事等信息。"""
    return get_world_news_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_cls_telegraph(limit: int = 30, keyword: str = "") -> str:
    """获取财联社电报快讯。A股最快的实时新闻源。"""
    return get_cls_telegraph_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_wallstreetcn(limit: int = 30, keyword: str = "") -> str:
    """获取华尔街见闻快讯。国际财经新闻中文解读。"""
    return get_wallstreetcn_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_whitehouse(limit: int = 20, keyword: str = "") -> str:
    """获取白宫官方声明和行政令。追踪美国政策动态。"""
    return get_whitehouse_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_pboc_news(limit: int = 20, keyword: str = "") -> str:
    """获取中国人民银行公告。追踪货币政策动态。"""
    return get_pboc_news_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_jin10(limit: int = 30, keyword: str = "") -> str:
    """获取金十数据实时快讯。覆盖全球宏观、外汇、商品。"""
    return get_jin10_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_xinhua(limit: int = 20, keyword: str = "") -> str:
    """获取新华社财经新闻。国内官方政策信号。"""
    return get_xinhua_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_fed_news(limit: int = 20, keyword: str = "") -> str:
    """获取美联储新闻发布。追踪美国货币政策。"""
    return get_fed_news_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_sec_news(limit: int = 20, keyword: str = "") -> str:
    """获取SEC新闻发布。追踪美国证券监管动态。"""
    return get_sec_news_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_social_media(limit: int = 20, keyword: str = "") -> str:
    """获取特朗普(Truth Social)和马斯克(X)的最新动态。"""
    return get_social_media_fn(limit=limit, keyword=keyword or None)


@function_tool
def get_eastmoney_live(limit: int = 30, keyword: str = "") -> str:
    """获取东方财富7x24小时实时快讯。全天候财经快讯流。"""
    return get_eastmoney_live_fn(limit=limit, keyword=keyword or None)


@function_tool
def search_stocks(keyword: str) -> str:
    """根据概念/板块描述检索相关A股个股。支持语义搜索。

    输入可以是完整句子，例如"特朗普关税利好的国产替代和半导体板块"。
    不需要拆成多个关键词分别搜索，一次调用即可覆盖多个相关概念。
    系统会同时做语义匹配和关键词匹配，返回所有相关概念板块及其成分股。
    """
    return search_stocks_fn(keyword=keyword)


@function_tool
def web_search(query: str, max_results: int = 10) -> str:
    """通用网页搜索（DuckDuckGo）。用于搜索最新新闻、验证信息、获取其他工具未覆盖的数据。

    支持中英文搜索。英文搜索效果更好，建议对国际事件使用英文查询。
    例如："Trump tariff China 2026" 或 "特朗普关税最新消息"
    """
    return web_search_fn(query=query, max_results=max_results)


@function_tool
def web_fetch(url: str) -> str:
    """获取网页内容。输入URL，返回页面的文本内容（自动去除HTML标签）。

    用于深入阅读 web_search 返回的链接、查看新闻全文、读取报告原文等。
    """
    return web_fetch_fn(url=url)


@function_tool
def get_pizzint() -> str:
    """获取五角大楼披萨指数（Pentagon Pizza Index）— 地缘政治紧张度的OSINT早期预警。

    返回数据包括：
    - 五角大楼附近披萨店的异常活动（订单暴涨=可能有大事）
    - "末日指数"（基于Polymarket预测市场的地缘风险综合评分）
    - 突发预测市场（最大波动的地缘政治赌盘）
    - 双边威胁等级（美俄、美中、美伊等）

    在分析地缘政治事件时建议调用此工具获取实时紧张度评估。
    """
    return get_pizzint_fn()


@function_tool
def get_sector_data(sector_name: str) -> str:
    """获取板块行情数据，包括涨跌幅和资金流向。"""
    return get_sector_data_fn(sector_name=sector_name)


@function_tool
def filter_stocks(stock_codes: list[str]) -> str:
    """过滤不适合的个股（剔除ST、停牌、市值过小）。"""
    return filter_stocks_fn(stock_codes=stock_codes)


@function_tool
def get_watchlist() -> str:
    """读取用户自选股列表。"""
    return get_watchlist_fn()


# All tools as a list for agent construction
ALL_TOOLS = [
    get_news, get_eastmoney_live, get_world_news,
    get_cls_telegraph, get_wallstreetcn,
    get_whitehouse, get_pboc_news, get_jin10, get_xinhua,
    get_fed_news, get_sec_news, get_social_media,
    search_stocks, web_search, web_fetch, get_pizzint,
    get_sector_data, filter_stocks, get_watchlist,
]
