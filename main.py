import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from alpha_agents.config import DB_PATH, CHROMA_PATH, MONITOR_INTERVAL_SECONDS


def load_env() -> None:
    """Load .env file from project root if it exists."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _ensure_index() -> None:
    """Build stock index + embeddings if not already done."""
    if DB_PATH.exists():
        from alpha_agents.data.db import get_connection
        conn = get_connection(DB_PATH)
        count = conn.execute("SELECT COUNT(*) as n FROM concepts").fetchone()["n"]
        conn.close()
        if count > 0:
            logging.info("Index exists (%d concepts), skipping build.", count)
            return

    logging.info("No index found, building stock concept index...")
    from alpha_agents.data.index_builder import build_index
    build_index(DB_PATH)
    logging.info("Index built successfully.")


def _ensure_embeddings() -> None:
    """Build concept embeddings if not already done."""
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        collection = client.get_or_create_collection("concepts")
        if collection.count() > 0:
            logging.info("Embeddings exist (%d vectors), skipping build.", collection.count())
            return
    except Exception:
        pass

    if not DB_PATH.exists():
        logging.warning("No index DB, skipping embeddings.")
        return

    logging.info("No embeddings found, building concept vectors...")
    from alpha_agents.data.embeddings import build_concept_embeddings
    from alpha_agents.data.db import get_connection
    conn = get_connection(DB_PATH)
    try:
        build_concept_embeddings(conn)
    except Exception as e:
        logging.warning("Embedding build failed (non-fatal): %s", e)
    finally:
        conn.close()


def cmd_web(args: argparse.Namespace) -> None:
    """Start web UI with real-time pipeline visualization."""
    _ensure_index()
    _ensure_embeddings()

    import uvicorn
    from alpha_agents.web.events import event_bus
    from alpha_agents.web.app import app, set_monitor
    from alpha_agents.monitor import NewsMonitor

    interval = args.interval or MONITOR_INTERVAL_SECONDS
    monitor = NewsMonitor(interval=interval, event_bus=event_bus)
    set_monitor(monitor)

    async def run_all():
        # Run monitor and uvicorn server concurrently
        config = uvicorn.Config(
            app, host=args.host, port=args.port,
            log_level="info", access_log=False,
        )
        server = uvicorn.Server(config)
        logging.info("Starting web UI at http://%s:%d", args.host, args.port)
        await asyncio.gather(
            server.serve(),
            monitor.run(),
        )

    asyncio.run(run_all())


def cmd_run(args: argparse.Namespace) -> None:
    """One command to rule them all: ensure index → ensure embeddings → start monitor."""
    _ensure_index()
    _ensure_embeddings()

    if args.event:
        # One-shot analysis
        from alpha_agents.agents.strategist import run_analysis
        prompt = f"请分析以下事件的多市场影响，完成完整分析并输出推荐报告：\n\n{args.event}"
        result = asyncio.run(run_analysis(prompt))
        print(result)
    else:
        # Continuous monitoring
        from alpha_agents.monitor import NewsMonitor
        interval = args.interval or MONITOR_INTERVAL_SECONDS
        monitor = NewsMonitor(interval=interval)
        logging.info("Starting continuous news monitoring...")
        asyncio.run(monitor.run())


def cmd_review(args: argparse.Namespace) -> None:
    """Run daily prediction review."""
    from alpha_agents.daily_review import run_daily_review
    import json as _json
    result = asyncio.run(run_daily_review(target_date=args.date))
    if result.get("status") == "no_predictions":
        print(f"没有找到 {result['date']} 的预测记录")
    elif result.get("status") == "no_market_data":
        print(f"无法获取行情数据")
    else:
        print(f"\n=== {result['date']} 预测回顾 ===")
        print(f"预测数: {result['predictions_count']}")
        print(f"匹配到行情: {result['matched']}")
        print(f"预测正确: {result['correct']}")
        print(f"准确率: {result['accuracy'] * 100:.0f}%")
        if result.get("review_text"):
            print(f"\n{result['review_text']}")


def cmd_build_index(args: argparse.Namespace) -> None:
    """Force rebuild stock index (even if it exists)."""
    logging.info("Building stock concept index...")
    from alpha_agents.data.index_builder import build_index
    build_index(DB_PATH)
    logging.info("Index built successfully at %s", DB_PATH)


def cmd_build_embeddings(args: argparse.Namespace) -> None:
    """Force rebuild concept embeddings."""
    from alpha_agents.data.embeddings import build_concept_embeddings
    from alpha_agents.data.db import get_connection
    logging.info("Building concept embeddings...")
    conn = get_connection(DB_PATH)
    try:
        n = build_concept_embeddings(conn)
        logging.info("Embedded %d concepts", n)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="AlphaAgents — 新闻驱动的自主选股系统")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出")
    subparsers = parser.add_subparsers(dest="command")

    # run (default) — auto-setup + start
    p_run = subparsers.add_parser("run", help="一键启动（自动构建索引 → 启动监控）")
    p_run.add_argument("--event", type=str, help="指定分析的事件（一次性分析，不启动监控）")
    p_run.add_argument("--interval", type=int, help="监控间隔（秒）")
    p_run.set_defaults(func=cmd_run)

    # web — web UI with pipeline visualization
    p_web = subparsers.add_parser("web", help="启动Web界面（实时Pipeline可视化）")
    p_web.add_argument("--host", default="0.0.0.0", help="监听地址")
    p_web.add_argument("--port", type=int, default=8000, help="监听端口")
    p_web.add_argument("--interval", type=int, help="监控间隔（秒）")
    p_web.set_defaults(func=cmd_web)

    # review — daily prediction review
    p_review = subparsers.add_parser("review", help="回顾验证昨日预测结果")
    p_review.add_argument("--date", type=str, help="指定日期（YYYY-MM-DD），默认昨天")
    p_review.set_defaults(func=cmd_review)

    # build-index — force rebuild
    p_index = subparsers.add_parser("build-index", help="强制重建股票概念索引")
    p_index.set_defaults(func=cmd_build_index)

    # build-embeddings — force rebuild
    p_embed = subparsers.add_parser("build-embeddings", help="强制重建概念语义搜索向量")
    p_embed.set_defaults(func=cmd_build_embeddings)

    args = parser.parse_args()
    load_env()
    setup_logging(getattr(args, "verbose", False))

    # Default to 'run' if no subcommand given
    if args.command is None:
        args.func = cmd_run
        args.event = None
        args.interval = None

    args.func(args)


if __name__ == "__main__":
    main()
