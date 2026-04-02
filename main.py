import argparse
import asyncio
import logging
import sys

from alpha_agents.agents.strategist import run_analysis
from alpha_agents.monitor import NewsMonitor
from alpha_agents.data.index_builder import build_index
from alpha_agents.config import DB_PATH, MONITOR_INTERVAL_SECONDS


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_analyze(args: argparse.Namespace) -> None:
    if args.event:
        prompt = f"请分析以下事件对A股市场的影响，完成完整分析并输出推荐报告：\n\n{args.event}"
    else:
        prompt = "请获取最新财经新闻，分析当前市场局势，如有值得关注的事件，完成完整分析并输出推荐报告。"

    result = asyncio.run(run_analysis(prompt))
    print(result)


def cmd_monitor(args: argparse.Namespace) -> None:
    interval = args.interval or MONITOR_INTERVAL_SECONDS
    monitor = NewsMonitor(interval=interval)
    asyncio.run(monitor.run())


def cmd_build_index(args: argparse.Namespace) -> None:
    logging.info("Building stock concept index...")
    build_index(DB_PATH)
    logging.info("Index built successfully at %s", DB_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(description="AlphaAgents — 新闻驱动的自主选股系统")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # analyze
    p_analyze = subparsers.add_parser("analyze", help="手动触发分析")
    p_analyze.add_argument("--event", type=str, help="指定分析的事件")
    p_analyze.set_defaults(func=cmd_analyze)

    # monitor
    p_monitor = subparsers.add_parser("monitor", help="启动新闻监控（持续运行）")
    p_monitor.add_argument("--interval", type=int, help="监控间隔（秒）")
    p_monitor.set_defaults(func=cmd_monitor)

    # build-index
    p_index = subparsers.add_parser("build-index", help="构建股票概念索引")
    p_index.set_defaults(func=cmd_build_index)

    args = parser.parse_args()
    setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
