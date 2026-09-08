"""What the UI process runs beside the web server.

Two independent switches, because the two loops cost different things:

- the monitor loop calls digest_news — an LLM call per batch. Deployed
  next to the scheduler container it billed a full pipeline around the
  clock for output that landed in one shared DB.
- news ingestion only fetches and stores. Switching the monitor off used
  to stop that too, which froze the flash feed: a live-looking page
  serving four-hour-old news with nothing on screen to say why.
"""

import argparse
import asyncio
from unittest.mock import MagicMock, patch

import pytest

import main


def _args(**kw):
    ns = argparse.Namespace(host="127.0.0.1", port=8000, interval=None,
                            no_monitor=False, no_ingest=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture()
def stub_web(monkeypatch):
    """Stub everything cmd_web reaches for, capturing what it scheduled."""
    started = {"monitor": False, "server": False, "ingest": False}

    monkeypatch.setattr(main, "_ensure_index", lambda: None)
    monkeypatch.setattr(main, "_ensure_embeddings", lambda: None)

    async def fake_serve():
        started["server"] = True

    async def fake_monitor_run():
        started["monitor"] = True

    async def fake_ingest():
        started["ingest"] = True
        # CancelledError, not a plain Exception: ingest_loop deliberately
        # swallows exceptions and sleeps, so anything else would leave the
        # test sitting in the retry sleep for the full interval.
        raise asyncio.CancelledError

    server = MagicMock()
    server.serve = fake_serve

    monitor = MagicMock()
    monitor.run = fake_monitor_run

    with patch("uvicorn.Config"), \
         patch("uvicorn.Server", return_value=server), \
         patch("alpha_agents.pipeline.monitor.NewsMonitor", return_value=monitor), \
         patch("alpha_agents.pipeline.tasks.news_ingest.run_news_ingest",
               side_effect=fake_ingest), \
         patch("alpha_agents.server.app.set_monitor") as set_monitor:
        yield started, set_monitor


class TestPipelineToggle:
    def test_no_monitor_skips_the_llm_loop(self, stub_web):
        started, _ = stub_web

        main.cmd_web(_args(no_monitor=True, no_ingest=True))

        assert started["server"], "UI must still serve"
        assert not started["monitor"], "monitor loop must not run"

    def test_default_still_runs_it(self, stub_web):
        started, _ = stub_web

        main.cmd_web(_args())

        assert started["server"]
        assert started["monitor"]

    def test_monitor_stays_registered_for_manual_trigger(self, stub_web):
        """/api/trigger is the deliberate, user-initiated single cycle."""
        _, set_monitor = stub_web

        main.cmd_web(_args(no_monitor=True, no_ingest=True))

        assert set_monitor.called


class TestIngestToggle:
    def test_feed_keeps_moving_without_the_llm_pipeline(self, stub_web):
        """The whole point: --no-monitor must not freeze the news feed."""
        started, _ = stub_web

        with pytest.raises(asyncio.CancelledError):
            main.cmd_web(_args(no_monitor=True))

        assert started["ingest"], "ingestion is free and must keep running"
        assert not started["monitor"]

    def test_no_ingest_gives_a_read_only_ui(self, stub_web):
        started, _ = stub_web

        main.cmd_web(_args(no_monitor=True, no_ingest=True))

        assert not started["ingest"]
        assert not started["monitor"]
        assert started["server"]

    def test_no_ingest_alone_does_not_disable_the_monitor(self, stub_web):
        """The monitor does its own fetching, so this combination is a no-op."""
        started, _ = stub_web

        main.cmd_web(_args(no_ingest=True))

        assert started["monitor"]
        assert not started["ingest"], "monitor fetches; no separate loop"


class TestComposeWiring:
    def test_web_service_skips_the_llm_pipeline(self):
        """The flag only helps if the deployed command actually uses it.

        --no-ingest is deliberately NOT set there: the deployment is
        full-function, and a second sweep costs nothing but duplicate
        fetches, which the news hash already de-duplicates on write.
        """
        from pathlib import Path
        compose = Path(main.__file__).parent / "docker-compose.yml"
        web_block = compose.read_text(encoding="utf-8").split("  web:", 1)[1]
        command = web_block.split("volumes:", 1)[0]
        assert "--no-monitor" in command
