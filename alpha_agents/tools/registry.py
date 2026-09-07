"""Register all data tools for OpenAI Agents SDK."""

from agents import function_tool

from alpha_agents.sources.eastmoney import get_news_fn
from alpha_agents.sources.world_news import get_world_news_fn
from alpha_agents.sources.cls_telegraph import get_cls_telegraph_fn
from alpha_agents.sources.wallstreetcn import get_wallstreetcn_fn
from alpha_agents.sources.whitehouse import get_whitehouse_fn
from alpha_agents.sources.pboc import get_pboc_news_fn
from alpha_agents.sources.jin10 import get_jin10_fn
from alpha_agents.sources.sina_7x24 import get_sina_7x24_fn
from alpha_agents.sources.xinhua import get_xinhua_fn
from alpha_agents.sources.fed import get_fed_news_fn
from alpha_agents.sources.sec import get_sec_news_fn
from alpha_agents.sources.truthsocial import get_social_media_fn
from alpha_agents.sources.eastmoney_live import get_eastmoney_live_fn
from alpha_agents.tools.stock_search import search_stocks_fn
from alpha_agents.tools.web_search import web_search_fn
from alpha_agents.tools.web_fetch import web_fetch_fn
from alpha_agents.sources.pizzint import get_pizzint_fn
from alpha_agents.tools.sector import get_sector_data_fn
from alpha_agents.tools.stock_filter import filter_stocks_fn
from alpha_agents.tools.watchlist import get_watchlist_fn
from alpha_agents.tools.futures_quotes import (
    get_futures_quotes_fn, get_futures_inventory_fn, get_futures_basis_fn,
    get_cftc_positions_fn,
)
from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
from alpha_agents.tools.financial_data import get_financial_data_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.tools.earnings_calendar import get_earnings_calendar_fn
from alpha_agents.tools.fund_flow import (
    get_lhb_detail_fn, get_block_trade_fn, get_north_flow_fn,
    get_margin_data_fn, get_stock_fund_flow_fn,
)
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn, get_concept_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.tools.market_snapshot import get_market_snapshot_fn
from alpha_agents.tools.institutional_position import get_institutional_position_fn
from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
from alpha_agents.tools.global_market import (
    get_us_market_fn, get_bond_yields_fn, get_global_overview_fn,
)
from alpha_agents.data.sentiment_cycle import get_sentiment_cycle as _get_sentiment_cycle, format_sentiment_cycle


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
def get_sina_7x24(limit: int = 30, keyword: str = "", stocks_only: bool = False) -> str:
    """获取新浪财经7x24快讯。

    与其他快讯源的区别：新浪已为每条快讯标注**关联标的**，公司新闻直接带
    6位股票代码，无需再猜。同时带分类标签（公司/市场/国际/其他）。

    参数:
        limit: 返回条数
        keyword: 可选关键词过滤
        stocks_only: 只返回已关联标的的快讯（想直接拿"新闻→个股"映射时用）

    返回: news 列表，每条含 stocks(关联标的) / tags(分类) / link。
    """
    return get_sina_7x24_fn(limit=limit, keyword=keyword or None,
                            stocks_only=stocks_only)


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
    """获取板块行情数据，包括涨跌幅和资金流向。

    注意：板块名称必须使用 get_sector_ranking 或 get_concept_ranking 返回的精确名称。
    例如用"共封装光学(CPO)"而不是"光通信"，用"5G"而不是"通信设备"。
    如果不确定名称，先调用 get_sector_ranking 查看实际的板块名。
    """
    return get_sector_data_fn(sector_name=sector_name)


@function_tool
def filter_stocks(stock_codes: list[str]) -> str:
    """过滤不适合的个股（剔除ST、停牌、市值过小）。"""
    return filter_stocks_fn(stock_codes=stock_codes)


@function_tool
def get_watchlist() -> str:
    """读取用户自选股列表。"""
    return get_watchlist_fn()


@function_tool
def get_stock_quotes(codes: str) -> str:
    """获取个股实时行情数据（价格、市值、涨跌幅）。

    输入股票代码，逗号分隔，例如："000858,600519,002594"。
    返回最新价格、涨跌幅、总市值、流通市值、所属行业。
    用于验证推荐股票的当前价格位置和市值规模。
    """
    return get_stock_quotes_fn(codes=codes)


@function_tool
def get_financial_data(code: str) -> str:
    """获取个股基本面财务数据（ROE、EPS、负债率、毛利率等）。

    输入单个股票代码，如"000858"。
    返回最近报告期的核心财务指标，用于评估个股质量：
    - ROE > 15% = 优质企业
    - 负债率 > 100% = 高杠杆风险
    - 净利润增速 < 0 = 业绩下滑
    """
    return get_financial_data_fn(code=code)


@function_tool
def get_market_breadth() -> str:
    """获取A股市场整体情绪指标（涨跌比、涨停跌停数、市场活跃度）。

    无需输入参数。返回当前市场情绪判断：
    - 涨跌比 > 2 = 乐观（适合看多）
    - 涨跌比 < 0.5 = 悲观（谨慎看多，关注超跌机会）
    在分析开始时调用此工具，了解当前市场环境再做推荐。
    """
    return get_market_breadth_fn()


@function_tool
def get_earnings_calendar(codes: str = "") -> str:
    """获取业绩预告数据 — 检查推荐股票是否有业绩地雷风险。

    输入股票代码（逗号分隔），返回该股票的业绩预告类型：
    - "首亏"/"预减" = 高风险（earnings_risk: high），建议回避
    - "预增"/"大幅预增" = 低风险（earnings_risk: low），业绩支撑
    留空则返回近期最值得关注的业绩预告（首亏/预减/大幅预增等）。
    """
    return get_earnings_calendar_fn(codes=codes)


@function_tool
def get_lhb_detail(date: str = "") -> str:
    """获取龙虎榜数据 — 机构/游资席位买卖明细。

    龙虎榜展示当日涨跌幅异常、成交量异常的个股的买卖席位。
    机构席位买入 = 持续性较好；游资席位买入 = 可能是一日游。
    输入日期(YYYYMMDD)，留空为最新交易日。
    """
    return get_lhb_detail_fn(date=date)


@function_tool
def get_block_trade(date: str = "") -> str:
    """获取大宗交易数据 — 折溢价率判断买卖意愿。

    折价成交 = 卖方急于出货；溢价成交 = 买方看好。
    输入日期(YYYYMMDD)，留空为最新交易日。
    """
    return get_block_trade_fn(date=date)


@function_tool
def get_north_flow(indicator: str = "today") -> str:
    """获取北向资金持股数据 — 外资方向是重要信号。

    外资通过陆股通买卖A股，其方向通常具有较强的参考价值。
    输入"today"查看今日持股排名；输入股票代码（如"000858"）查看该股是否被北向持有及增减仓情况。
    """
    return get_north_flow_fn(indicator=indicator)


@function_tool
def get_margin_data(code: str = "") -> str:
    """获取融资融券数据 — 杠杆资金方向。

    融资余额增加 = 杠杆资金看多；融资余额减少 = 去杠杆。
    输入股票代码查看该股融资融券情况；留空查看市场融资余额排名前20。
    """
    return get_margin_data_fn(code=code)


@function_tool
def get_stock_fund_flow(code: str, market: str = "") -> str:
    """获取个股资金流向 — 主力vs散户资金方向。

    主力净流入 = 大资金看好；主力净流出 = 大资金撤退。
    返回最近5个交易日的主力/散户资金流入流出数据及趋势判断。
    输入股票代码，如"000858"。market留空自动判断。
    """
    return get_stock_fund_flow_fn(code=code, market=market)


@function_tool
def get_sector_ranking(top_n: int = 20) -> str:
    """获取行业板块资金流排名 — 检测板块轮动方向。

    显示资金正在流入哪些行业、流出哪些行业。
    用于判断市场当前的轮动方向和主线热度。
    """
    return get_sector_ranking_fn(top_n=top_n)


@function_tool
def get_concept_ranking(top_n: int = 20) -> str:
    """获取概念板块资金流排名 — 发现热门投资主线。

    显示资金正在流入哪些概念板块（如华为昇腾、AI算力、低空经济）。
    概念名称与数据库中的概念板块一致，可直接用于主线发现和标的检索。
    比行业排名更细，更贴近市场热点。
    """
    return get_concept_ranking_fn(top_n=top_n)


@function_tool
def get_anomaly_stocks(date: str = "") -> str:
    """获取涨停/跌停/炸板数据 — 检测市场异动和主线方向。

    涨停板集中在某个行业 = 该行业可能是新主线。
    炸板多 = 市场分歧大，追高风险高。
    连板股 = 短线资金认可的最强方向。
    输入日期(YYYYMMDD)，留空为今日。
    """
    return get_anomaly_stocks_fn(date=date)


@function_tool
def get_market_snapshot(min_volume_ratio: float = 3.0, min_turnover: float = 15.0) -> str:
    """获取量比/换手率异常个股 — 发现盘中异动股。

    量比 > 3 说明成交量远超近期平均，可能有资金异动或消息刺激。
    换手率 > 15% 说明筹码交换活跃，可能有主力进出。
    返回 Top 20 异动股，按量比降序。
    """
    return get_market_snapshot_fn(min_volume_ratio=min_volume_ratio, min_turnover=min_turnover)


@function_tool
def get_institutional_position(code: str, market: str = "") -> str:
    """分析个股的机构持仓行为，输出量化买卖信号。

    综合4个维度给出操作建议：
    1. 资金流动量 — 主力资金连续流入/流出天数和强度
    2. 机构成本区间 — 龙虎榜机构买入价、大宗交易价、北向持仓、融资余额
    3. 相对强弱 — 近期涨幅、价格位置（高位/低位）、是否追高
    4. 换手率状态 — 量价配合度、缩量洗盘/放量突破判断

    输出包含: 综合评分(-10到+10)、多空信号列表、操作建议（介入区间+止损位）。
    基于机构行为数据，不使用MACD/KDJ等散户技术指标。
    """
    return get_institutional_position_fn(code=code, market=market)


@function_tool
def get_sector_best_stocks(concept_name: str, top_n: int = 10) -> str:
    """获取板块内综合评分最高的标的（基于历史beta跟涨弹性+实时多因子打分）。

    输入概念板块名，返回该板块内 Top N 标的。评分维度：
    1. 板块beta（40%）— 历史上板块涨时该股涨多少
    2. 当日涨幅位置（25%）— 还没涨的优先（补涨机会）
    3. 机构认可度（20%）— 北向增持+龙虎榜机构买入
    4. 流动性（15%）— 日均成交额

    用于：板块异动时选最佳标的，替代语义搜索+主观判断。
    """
    return get_sector_best_stocks_fn(concept_name=concept_name, top_n=top_n)


@function_tool
def get_sentiment_phase() -> str:
    """获取当前市场情绪周期阶段（冰点/修复/升温/狂热/分歧/退潮）。

    基于近3日涨停板数量趋势、炸板率、连板高度、涨跌比趋势综合判断。
    返回当前阶段、置信度、指标数据和对应的交易策略建议（仓位上限、买入风格、卖出风格）。
    """
    import json
    result = _get_sentiment_cycle()
    return json.dumps(result, ensure_ascii=False)


@function_tool
def get_us_market() -> str:
    """获取美股三大指数最新行情（道琼斯、标普500、纳斯达克）。

    返回最近2个交易日的收盘价和涨跌幅。用于晨扫判断隔夜外盘方向。
    """
    return get_us_market_fn()


@function_tool
def get_bond_yields() -> str:
    """获取中美国债收益率（2Y/5Y/10Y/30Y）及利差信号。

    关键信号：
    - 美债10Y上升 → A股成长股估值承压
    - 中美利差收窄 → 资本外流压力
    - 美债10Y-2Y倒挂 → 衰退信号
    """
    return get_bond_yields_fn()


@function_tool
def get_global_overview() -> str:
    """获取全球市场综合概览 — 美股指数 + 国债收益率 + 关键信号。

    一站式获取隔夜全球市场状态，用于晨扫和夜扫。
    """
    return get_global_overview_fn()


# --- Tool sets for different agents ---

# News source tools — used by monitor pipeline, NOT by agents during analysis.
# Agents receive pre-digested events; they don't need to re-fetch news.
NEWS_TOOLS = [
    get_news, get_eastmoney_live, get_world_news,
    get_cls_telegraph, get_wallstreetcn,
    get_whitehouse, get_pboc_news, get_jin10, get_xinhua,
    get_fed_news, get_sec_news, get_social_media,
]

# Stock analysis tools — for the stock strategist agent
STOCK_TOOLS = [
    search_stocks, get_sector_data, filter_stocks, get_watchlist,
    get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
    get_lhb_detail, get_block_trade, get_north_flow, get_margin_data,
    get_stock_fund_flow, get_sector_ranking, get_concept_ranking, get_anomaly_stocks,
    get_market_snapshot, get_institutional_position, get_sina_7x24,
    get_sector_best_stocks,
    get_us_market, get_bond_yields, get_global_overview,
    web_search, web_fetch, get_pizzint,
    get_sentiment_phase,
]

@function_tool
def get_futures_quotes(symbols: str = "", days: int = 5) -> str:
    """获取期货主力合约行情数据（OHLCV）。

    输入品种名称，逗号分隔，例如："沪铜,沪金,螺纹钢,原油"。
    留空则返回所有主力合约概览。返回最近N个交易日的开高低收、成交量、持仓量。
    支持品种：沪铜、沪铝、沪锌、沪镍、沪金、沪银、螺纹钢、热卷、铁矿石、
    焦煤、焦炭、原油、燃油、甲醇、PTA、乙二醇、聚丙烯、豆粕、豆油、棕榈油、
    玉米、棉花、白糖、生猪、橡胶等。
    """
    return get_futures_quotes_fn(symbols=symbols, days=days)


@function_tool
def get_futures_inventory(symbol: str) -> str:
    """获取期货品种交割仓库库存数据（仓单/库存）。

    用于判断品种供需格局：去库存=供不应求偏多，累库存=供过于求偏空。
    输入品种名称，如："沪铜"、"螺纹钢"、"铁矿石"。返回最近30天库存变化趋势。
    """
    return get_futures_inventory_fn(symbol=symbol)


@function_tool
def get_cftc_positions(commodity: str = "") -> str:
    """获取CFTC持仓报告（Commitment of Traders）— 国际期货大户持仓动向。

    追踪商业/非商业持仓变化，判断机构资金方向。
    输入品种名称（如"原油"、"黄金"、"大豆"），留空返回所有品种概览。
    返回最近10周的多单、空单、净多头寸及趋势。
    可选品种：原油、黄金、白银、铂金、天然气、铜、玉米、大豆、豆油、豆粕、棉花、白糖。
    """
    return get_cftc_positions_fn(commodity=commodity)


@function_tool
def get_futures_basis(date: str = "") -> str:
    """获取期现基差数据（现货价 vs 期货价）。

    基差 = 现货价 - 期货价。正基差(现货升水)通常看多，负基差(期货升水)可能看空。
    输入日期(YYYYMMDD格式)，留空为最新数据。返回所有品种的现货价、期货价、基差率。
    """
    return get_futures_basis_fn(date=date)


# Futures analysis tools — for the futures strategist agent
FUTURES_TOOLS = [
    get_futures_quotes, get_futures_inventory, get_futures_basis,
    get_cftc_positions, web_search, web_fetch, get_pizzint,
]

# ALL_TOOLS: full set including news (for backward compatibility / one-shot mode)
ALL_TOOLS = NEWS_TOOLS + STOCK_TOOLS
