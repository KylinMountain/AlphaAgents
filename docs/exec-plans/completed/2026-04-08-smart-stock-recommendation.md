# Smart Stock Recommendation Enhancement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add quantitative data tools (stock quotes, financials, market breadth, earnings calendar) to enable data-driven stock/futures recommendations instead of pure narrative analysis.

**Architecture:** Four new tool modules in `alpha_agents/tools/`, each following the existing pattern (sync function returning JSON string, wrapped with `@function_tool` in registry.py, using `no_proxy()` for domestic APIs). Tools registered into STOCK_TOOLS/VERIFICATION_TOOLS. Strategist and reflection prompts updated to use new data.

**Tech Stack:** akshare (already installed), akshare uses `requests` library which requires `trust_env=False` monkey-patch on macOS with system proxy.

**Important:** akshare uses `requests` (not `httpx`), and macOS system proxy (Clash at 127.0.0.1:7890) causes `requests` to fail even with `no_proxy()`. All akshare calls must use the existing `no_proxy()` context manager PLUS a `requests.Session.trust_env = False` patch. The `no_proxy()` in `config.py` should be enhanced once to also patch `requests.Session`.

---

### Task 1: Fix `no_proxy()` to work with `requests` library

The existing `no_proxy()` context manager only patches `urllib.request.getproxies` and env vars. But akshare uses `requests` which reads macOS system proxy via `trust_env`. This causes all akshare API calls to fail when Clash proxy is configured.

**Files:**
- Modify: `alpha_agents/config.py:41-67`

- [ ] **Step 1: Update `no_proxy()` to also patch `requests.Session`**

```python
@contextmanager
def no_proxy():
    """Temporarily disable HTTP proxy for direct access to domestic APIs.

    Patches both urllib and requests to bypass macOS system proxy.
    """
    import urllib.request
    import requests

    # Patch urllib
    saved_getproxies = urllib.request.getproxies
    urllib.request.getproxies = lambda: {}

    # Patch requests — trust_env=False prevents reading macOS system proxy
    saved_trust_env_init = requests.Session.__init__
    _original_init = saved_trust_env_init

    def _patched_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        self.trust_env = False

    requests.Session.__init__ = _patched_init

    # Also clear env vars in case anything reads them directly
    saved_env = {}
    proxy_vars = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    for var in proxy_vars:
        if var in os.environ:
            saved_env[var] = os.environ.pop(var)
    old_no_proxy = os.environ.get("NO_PROXY", "")
    os.environ["NO_PROXY"] = "*"
    try:
        yield
    finally:
        urllib.request.getproxies = saved_getproxies
        requests.Session.__init__ = saved_trust_env_init
        os.environ["NO_PROXY"] = old_no_proxy
        for var, val in saved_env.items():
            os.environ[var] = val
```

- [ ] **Step 2: Test the fix**

Run: `uv run python -c "from alpha_agents.config import no_proxy; import akshare as ak; no_proxy().__enter__(); df = ak.stock_individual_info_em(symbol='000858'); print(df)"`
Expected: Shows stock info for 五粮液 without proxy errors.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/config.py
git commit -m "fix: no_proxy() now patches requests.Session.trust_env for macOS system proxy"
```

---

### Task 2: Add `get_stock_quotes` tool

Real-time stock quotes for recommended stocks. Uses `ak.stock_individual_info_em` for basic info and `ak.stock_zh_a_hist` for recent price history.

**Files:**
- Create: `alpha_agents/tools/stock_quotes.py`

- [ ] **Step 1: Create the tool implementation**

```python
"""Stock quote tool — fetch real-time prices and basic metrics for A-share stocks."""

import json
import logging
from datetime import datetime, timedelta

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_stock_quotes_fn(codes: str) -> str:
    """Fetch real-time quotes for a list of A-share stocks.

    Args:
        codes: Comma-separated stock codes, e.g. "000858,600519,002594"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        return json.dumps({"error": "no stock codes provided", "quotes": []}, ensure_ascii=False)

    results = []
    for code in code_list[:10]:  # limit to 10 stocks
        try:
            with no_proxy():
                info_df = ak.stock_individual_info_em(symbol=code)

            info = {}
            for _, row in info_df.iterrows():
                info[row["item"]] = row["value"]

            # Get recent 5-day history for trend
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
            try:
                with no_proxy():
                    hist_df = ak.stock_zh_a_hist(
                        symbol=code, period="daily",
                        start_date=start, end_date=end, adjust="qfq",
                    )
                if not hist_df.empty:
                    hist_df = hist_df.tail(5)
                    latest = hist_df.iloc[-1]
                    prev = hist_df.iloc[-2] if len(hist_df) > 1 else latest
                    change_pct = round(
                        (float(latest["收盘"]) - float(prev["收盘"])) / float(prev["收盘"]) * 100, 2
                    ) if float(prev["收盘"]) != 0 else 0
                    week_high = float(hist_df["最高"].max())
                    week_low = float(hist_df["最低"].min())
                else:
                    change_pct = 0
                    week_high = week_low = 0
            except Exception:
                change_pct = 0
                week_high = week_low = 0

            market_cap = info.get("总市值", 0)
            float_cap = info.get("流通市值", 0)

            results.append({
                "code": code,
                "name": info.get("股票简称", "").replace(" ", ""),
                "price": float(info.get("最新", 0)),
                "change_pct": change_pct,
                "market_cap_yi": round(float(market_cap) / 1e8, 1) if market_cap else None,
                "float_cap_yi": round(float(float_cap) / 1e8, 1) if float_cap else None,
                "industry": info.get("行业", ""),
                "pe_ttm": None,  # filled by get_financial_data
                "week_high": week_high,
                "week_low": week_low,
            })
        except Exception as e:
            logger.debug("Failed to fetch quote for %s: %s", code, e)
            results.append({"code": code, "error": str(e)})

    return json.dumps({"count": len(results), "quotes": results}, ensure_ascii=False)
```

- [ ] **Step 2: Test the tool**

Run: `uv run python -c "from alpha_agents.config import no_proxy; from alpha_agents.tools.stock_quotes import get_stock_quotes_fn; print(get_stock_quotes_fn('000858,600519'))"`
Expected: JSON with price, market cap, industry for 五粮液 and 贵州茅台.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/tools/stock_quotes.py
git commit -m "feat: add get_stock_quotes tool for real-time A-share prices"
```

---

### Task 3: Add `get_financial_data` tool

Key fundamental metrics: PE, PB, ROE, debt ratio, dividend yield, revenue growth.

**Files:**
- Create: `alpha_agents/tools/financial_data.py`

- [ ] **Step 1: Create the tool implementation**

```python
"""Financial data tool — fundamental metrics for A-share stocks."""

import json
import logging

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_financial_data_fn(code: str) -> str:
    """Fetch fundamental financial metrics for an A-share stock.

    Args:
        code: Stock code, e.g. "000858"
    """
    try:
        with no_proxy():
            df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(__import__('datetime').datetime.now().year - 1))

        if df.empty:
            return json.dumps({"code": code, "error": "no financial data"}, ensure_ascii=False)

        # Use most recent full-year or latest quarter
        latest = df.iloc[-1]

        def safe_float(val):
            try:
                v = float(val)
                return v if v == v else None  # NaN check
            except (ValueError, TypeError):
                return None

        result = {
            "code": code,
            "report_date": str(latest.get("日期", "")),
            "eps": safe_float(latest.get("摊薄每股收益(元)")),
            "bvps": safe_float(latest.get("每股净资产_调整后(元)")),
            "roe_pct": safe_float(latest.get("净资产收益率(%)")),
            "roa_pct": safe_float(latest.get("总资产利润率(%)")),
            "gross_margin_pct": safe_float(latest.get("销售毛利率(%)")),
            "net_margin_pct": safe_float(latest.get("销售净利率(%)")),
            "debt_to_equity_pct": safe_float(latest.get("负债与所有者权益比率(%)")),
            "current_ratio": safe_float(latest.get("流动比率")),
            "revenue_growth_pct": safe_float(latest.get("主营业务收入增长率(%)")),
            "net_profit_growth_pct": safe_float(latest.get("净利润增长率(%)")),
            "ocf_per_share": safe_float(latest.get("每股经营性现金流(元)")),
            "dividend_payout_pct": safe_float(latest.get("股息发放率(%)")),
            "error": None,
        }

        # Quick valuation flag
        roe = result["roe_pct"]
        debt = result["debt_to_equity_pct"]
        if roe is not None and roe > 15 and (debt is None or debt < 100):
            result["quality_flag"] = "优质"
        elif roe is not None and roe < 5:
            result["quality_flag"] = "盈利能力弱"
        else:
            result["quality_flag"] = "一般"

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error("get_financial_data failed for %s: %s", code, e)
        return json.dumps({"code": code, "error": str(e)}, ensure_ascii=False)
```

- [ ] **Step 2: Test the tool**

Run: `uv run python -c "from alpha_agents.tools.financial_data import get_financial_data_fn; print(get_financial_data_fn('000858'))"`
Expected: JSON with ROE, EPS, debt ratio etc. for 五粮液.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/tools/financial_data.py
git commit -m "feat: add get_financial_data tool for fundamental analysis"
```

---

### Task 4: Add `get_market_breadth` tool

Market-level sentiment: advance/decline, limit up/down counts, activity rate.

**Files:**
- Create: `alpha_agents/tools/market_breadth.py`

- [ ] **Step 1: Create the tool implementation**

```python
"""Market breadth tool — overall A-share market sentiment indicators."""

import json
import logging
from datetime import datetime

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_market_breadth_fn() -> str:
    """Fetch A-share market breadth indicators.

    Returns advance/decline ratio, limit up/down counts, and market activity.
    Use this to assess whether the market is risk-on or risk-off before making recommendations.
    """
    try:
        with no_proxy():
            df = ak.stock_market_activity_legu()

        data = {}
        for _, row in df.iterrows():
            data[row["item"]] = row["value"]

        advances = int(float(data.get("上涨", 0)))
        declines = int(float(data.get("下跌", 0)))
        limit_up = int(float(data.get("涨停", 0)))
        limit_down = int(float(data.get("跌停", 0)))
        real_limit_up = int(float(data.get("真实涨停", 0)))
        real_limit_down = int(float(data.get("真实跌停", 0)))
        flat = int(float(data.get("平盘", 0)))
        total = advances + declines + flat

        ad_ratio = round(advances / declines, 2) if declines > 0 else 999

        # Sentiment classification
        if ad_ratio > 3 and real_limit_up > 50:
            sentiment = "极度乐观"
        elif ad_ratio > 2:
            sentiment = "乐观"
        elif ad_ratio > 1:
            sentiment = "偏多"
        elif ad_ratio > 0.5:
            sentiment = "偏空"
        elif ad_ratio > 0.3:
            sentiment = "悲观"
        else:
            sentiment = "极度悲观"

        result = {
            "timestamp": data.get("统计日期", datetime.now().strftime("%Y-%m-%d %H:%M")),
            "advances": advances,
            "declines": declines,
            "flat": flat,
            "total": total,
            "advance_decline_ratio": ad_ratio,
            "limit_up": limit_up,
            "real_limit_up": real_limit_up,
            "limit_down": limit_down,
            "real_limit_down": real_limit_down,
            "activity_pct": data.get("活跃度", ""),
            "sentiment": sentiment,
            "error": None,
        }

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error("get_market_breadth failed: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)
```

- [ ] **Step 2: Test the tool**

Run: `uv run python -c "from alpha_agents.tools.market_breadth import get_market_breadth_fn; print(get_market_breadth_fn())"`
Expected: JSON with advances, declines, limit up/down, sentiment classification. May show "非交易时段" outside market hours.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/tools/market_breadth.py
git commit -m "feat: add get_market_breadth tool for market sentiment"
```

---

### Task 5: Add `get_earnings_calendar` tool

Upcoming earnings forecasts to flag "earnings landmine" risk.

**Files:**
- Create: `alpha_agents/tools/earnings_calendar.py`

- [ ] **Step 1: Create the tool implementation**

```python
"""Earnings calendar tool — upcoming earnings forecasts and disclosure dates."""

import json
import logging

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_earnings_calendar_fn(codes: str = "") -> str:
    """Check earnings forecast (业绩预告) for specific stocks.

    Args:
        codes: Comma-separated stock codes to check, e.g. "000858,600519".
               If empty, returns recent notable earnings forecasts (预增/预减/首亏).
    """
    try:
        # Get latest quarter's earnings forecasts
        from datetime import datetime
        now = datetime.now()
        # Determine latest reporting period
        if now.month <= 4:
            date = f"{now.year - 1}1231"
        elif now.month <= 8:
            date = f"{now.year}0630"
        elif now.month <= 10:
            date = f"{now.year}0930"
        else:
            date = f"{now.year}0930"

        with no_proxy():
            df = ak.stock_yjyg_em(date=date)

        if df.empty:
            return json.dumps({"error": "no earnings data available", "forecasts": []}, ensure_ascii=False)

        code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else []

        if code_list:
            # Filter for specific stocks
            # stock_yjyg_em returns 归属净利润 rows per stock
            mask = df["股票代码"].isin(code_list)
            filtered = df[mask]
            if filtered.empty:
                return json.dumps({
                    "codes": code_list,
                    "forecasts": [],
                    "note": "这些股票暂无业绩预告数据",
                }, ensure_ascii=False)
            df = filtered
        else:
            # Return notable ones: 首亏, 预减, 大幅预减
            notable = df[df["预告类型"].isin(["首亏", "预减", "大幅预减", "预增", "大幅预增"])]
            df = notable.head(20)

        results = []
        seen = set()
        for _, row in df.iterrows():
            code = str(row["股票代码"])
            if code in seen:
                continue
            seen.add(code)

            forecast_type = str(row.get("预告类型", ""))
            change_pct = row.get("业绩变动幅度")

            # Risk flag
            if forecast_type in ("首亏", "预减", "大幅预减"):
                risk = "high"
            elif forecast_type in ("预增", "大幅预增"):
                risk = "low"
            else:
                risk = "medium"

            results.append({
                "code": code,
                "name": str(row.get("股票简称", "")),
                "forecast_type": forecast_type,
                "change_pct": float(change_pct) if change_pct == change_pct and change_pct is not None else None,
                "disclosure_date": str(row.get("公告日期", "")),
                "earnings_risk": risk,
            })

        return json.dumps({
            "report_period": date,
            "count": len(results),
            "forecasts": results,
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_earnings_calendar failed: %s", e)
        return json.dumps({"error": str(e), "forecasts": []}, ensure_ascii=False)
```

- [ ] **Step 2: Test the tool**

Run: `uv run python -c "from alpha_agents.tools.earnings_calendar import get_earnings_calendar_fn; import json; r = json.loads(get_earnings_calendar_fn('000858')); print(json.dumps(r, indent=2, ensure_ascii=False))"`
Expected: JSON with earnings forecast for 五粮液 or note that no data available.

Run: `uv run python -c "from alpha_agents.tools.earnings_calendar import get_earnings_calendar_fn; import json; r = json.loads(get_earnings_calendar_fn()); print(f'Notable forecasts: {r[\"count\"]}'); print(json.dumps(r['forecasts'][:3], indent=2, ensure_ascii=False))"`
Expected: JSON with notable earnings forecasts (首亏/预减/预增).

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/tools/earnings_calendar.py
git commit -m "feat: add get_earnings_calendar tool for earnings risk detection"
```

---

### Task 6: Register all new tools

Wire up new tools in registry.py and add to STOCK_TOOLS + VERIFICATION_TOOLS.

**Files:**
- Modify: `alpha_agents/tools/registry.py`

- [ ] **Step 1: Add imports and function_tool wrappers**

Add after line 27 (after existing imports):

```python
from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
from alpha_agents.tools.financial_data import get_financial_data_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.tools.earnings_calendar import get_earnings_calendar_fn
```

Add before the `# --- Tool sets ---` section (before line 165):

```python
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
```

- [ ] **Step 2: Update STOCK_TOOLS to include new tools**

Replace the existing STOCK_TOOLS line:

```python
# Stock analysis tools — for the stock strategist agent
STOCK_TOOLS = [
    search_stocks, get_sector_data, filter_stocks, get_watchlist,
    get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
    web_search, web_fetch, get_pizzint,
]
```

- [ ] **Step 3: Update reflection agent tools**

In `alpha_agents/agents/reflection.py`, add new tools to the verification tool list:

Replace the existing `verify_tools` list:

```python
from alpha_agents.tools.registry import (
    get_sector_data, filter_stocks, get_watchlist,
    get_futures_quotes, get_futures_inventory, get_futures_basis,
    get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
)
```

And update the tool list:

```python
    # Only market data tools — no web_search, no news tools
    verify_tools = [
        get_sector_data, filter_stocks, get_watchlist,
        get_futures_quotes, get_futures_inventory, get_futures_basis,
        get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
    ]
```

- [ ] **Step 4: Test registration**

Run: `uv run python -c "from alpha_agents.tools.registry import STOCK_TOOLS; print([t.name for t in STOCK_TOOLS])"`
Expected: List includes `get_stock_quotes`, `get_financial_data`, `get_market_breadth`, `get_earnings_calendar`.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/tools/registry.py alpha_agents/agents/reflection.py
git commit -m "feat: register new data tools in STOCK_TOOLS and reflection agent"
```

---

### Task 7: Update strategist prompt to use new tools

Add new workflow steps and output sections for valuation checks, market sentiment, earnings risk, and short thesis.

**Files:**
- Modify: `alpha_agents/prompts/strategist.md`

- [ ] **Step 1: Add new workflow steps**

After line 38 (after step 8 "检查自选股"), add:

```markdown
9. **市场情绪检查** — 使用 get_market_breadth 获取涨跌比和涨停跌停数，判断当前是风险偏好还是避险环境
10. **估值验证** — 对推荐的个股使用 get_financial_data 检查ROE、负债率、利润增速，剔除基本面差的标的
11. **行情验证** — 对推荐的个股使用 get_stock_quotes 检查当前价格，避免推荐已经大幅上涨的标的
12. **业绩排雷** — 对推荐的个股使用 get_earnings_calendar 检查是否有首亏/预减风险
13. **反向思考** — 对每个看多板块，思考对应的看空机会（做空建议）
```

- [ ] **Step 2: Add valuation and risk sections to output template**

After the 【推荐关注】 table and before 【自选股影响】, add:

```markdown
【估值与风险检查】
• {代码} {名称} — ROE: {X}% | 负债率: {X}% | 净利润增速: {X}% | 业绩预告: {预增/预减/无} | 质量: {优质/一般/风险}

【反向机会（看空方向）】
• {承压板块} — {看空理由}
```

- [ ] **Step 3: Add market environment section at the top**

After 【核心事件】 and before 【全球市场影响速览】, add:

```markdown
【市场环境】
• 涨跌比: {X} ({sentiment}) | 涨停: {X}家 | 跌停: {X}家
• 市场情绪: {风险偏好/避险/中性}
```

- [ ] **Step 4: Commit**

```bash
git add alpha_agents/prompts/strategist.md
git commit -m "feat: update strategist prompt with valuation checks and market sentiment"
```

---

### Task 8: Update reflection prompt to use new tools

Reflection agent should now also verify valuation reasonableness and earnings risk.

**Files:**
- Modify: `alpha_agents/prompts/reflection.md`

- [ ] **Step 1: Add new verification dimensions**

Add to the "验证范围" section:

```markdown
- 推荐个股的估值合理性 → 调用 get_financial_data: ROE < 5% 或负债率 > 150% 则标记风险
- 推荐个股的价格位置 → 调用 get_stock_quotes: 已经涨超 10% 则标记"追高风险"
- 推荐个股的业绩风险 → 调用 get_earnings_calendar: 有"首亏"/"预减"预告则标记"业绩地雷"
- 当前市场情绪 → 调用 get_market_breadth: 如果涨跌比 < 0.5（悲观市场）但报告全是看多，标记矛盾
```

- [ ] **Step 2: Update output format**

Add a new section to the output:

```markdown
【估值验证】
✅ {code} {name} — ROE {X}%, 负债率 {X}%, 质量{优质}
❌ {code} {name} — ROE 仅{X}%, 业绩预告{首亏}, 建议剔除
```

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/prompts/reflection.md
git commit -m "feat: update reflection prompt to verify valuation and earnings risk"
```

---

### Task 9: Integration test — run full pipeline

End-to-end test with a single event to verify all new tools work together.

**Files:** (none created, test only)

- [ ] **Step 1: Run a single-event analysis**

```bash
source .env && uv run python main.py run --event "博通宣布与谷歌达成TPU芯片长期合作协议"
```

Expected: Report includes:
- 【市场环境】with breadth data
- 【推荐关注】with stock codes
- 【估值与风险检查】with ROE/debt/earnings data
- 【反向机会】with short thesis

Check that:
1. No proxy errors in logs
2. `get_stock_quotes`, `get_financial_data`, `get_market_breadth`, `get_earnings_calendar` appear in tool call logs
3. Report format matches the template
4. Total runtime < 5 minutes for single event

- [ ] **Step 2: Run continuous monitor for 1 cycle**

```bash
uv run python main.py run
```

Wait for one complete cycle. Check that:
1. Digest produces events with `industry` field
2. Both stock and futures agents complete without timeout
3. Reflection agent uses new tools for verification
4. No crash on tool errors (graceful degradation)

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: integration test fixes for new data tools"
```
