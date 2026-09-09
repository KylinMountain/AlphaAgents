"""一个死掉的数据源不该饿死整轮扫描。

晨扫跑在一个总预算里，每次工具调用都从里面花钱，而卡住的源花得远超它
应得的份额：一次 300s 超时的运行里，最慢的五次调用花掉 65s、49s、39s、
38s、30s —— 220 秒，大部分耗在对一个返回连接超时和 500 的主机反复重试。
那轮完成了 25 次调用，比一次从容跑完的 33 次还少。**它不是做得太多而
失败，是有几次调用什么都没做，却做得很慢。**
"""

import time

import pytest

from alpha_agents.tools.budget import with_timeout


def test_a_fast_tool_is_untouched():
    @with_timeout
    def quick(a, b=2):
        return f"{a}-{b}"
    assert quick(1, b=3) == "1-3"


def test_a_hung_tool_returns_instead_of_blocking():
    """整轮扫描继续，只是少了一个输入。"""
    @with_timeout
    def hangs():
        time.sleep(10)
        return "never"
    started = time.monotonic()
    out = with_timeout(hangs.__wrapped__, timeout=1)()
    assert time.monotonic() - started < 3, "没有在预算内返回"
    assert "超过 1秒未响应" in out


def test_the_timeout_message_tells_the_model_not_to_retry():
    """裸的报错串会招来又一次重试，而重试正是刚才烧掉预算的东西。"""
    def hangs():
        time.sleep(10)
    out = with_timeout(hangs, timeout=1)()
    assert "不要重试" in out
    assert "继续" in out


def test_an_exception_becomes_a_readable_result():
    """抛异常会中断工具循环；一段说明不会。"""
    def broken():
        raise ConnectionError("host unreachable")
    out = with_timeout(broken, timeout=5)()
    assert "调用失败" in out
    assert "host unreachable" in out
    assert "不要重试" in out


def test_the_wrapper_keeps_the_name_and_docstring():
    """SDK 用函数名和 docstring 生成工具描述——丢了 agent 就不知道这是什么。"""
    def get_market_breadth(limit: int = 10) -> str:
        """获取市场涨跌比。"""
        return "{}"
    wrapped = with_timeout(get_market_breadth)
    assert wrapped.__name__ == "get_market_breadth"
    assert "涨跌比" in wrapped.__doc__


def test_every_registered_tool_is_wrapped():
    """一个漏网的工具就足以重现那次超时。"""
    from alpha_agents.tools.registry import STOCK_TOOLS, FUTURES_TOOLS
    for tool in list(STOCK_TOOLS) + list(FUTURES_TOOLS):
        assert tool.name, "工具名丢失说明包装破坏了注册"
