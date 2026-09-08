"""The UI process does not run the pipeline unless asked.

``cmd_web`` used to always start ``monitor.run()`` beside uvicorn. Deployed
alongside the scheduler container that owns the pipeline, that meant two
processes digesting the same news with the same LLM into one shared DB —
a round-the-clock token bill for duplicate work.
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest

import main


def _args(**kw):
    ns = argparse.Namespace(host="127.0.0.1", port=8000, interval=None,
                            no_monitor=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture()
def stub_web(monkeypatch):
    """Stub everything cmd_web reaches for, capturing what it scheduled."""
    started = {"monitor": False, "server": False}

    monkeypatch.setattr(main, "_ensure_index", lambda: None)
    monkeypatch.setattr(main, "_ensure_embeddings", lambda: None)

    async def fake_serve():
        started["server"] = True

    async def fake_monitor_run():
        started["monitor"] = True

    server = MagicMock()
    server.serve = fake_serve

    monitor = MagicMock()
    monitor.run = fake_monitor_run

    with patch("uvicorn.Config"), \
         patch("uvicorn.Server", return_value=server), \
         patch("alpha_agents.pipeline.monitor.NewsMonitor", return_value=monitor), \
         patch("alpha_agents.server.app.set_monitor") as set_monitor:
        yield started, set_monitor


class TestWebMonitorToggle:
    def test_no_monitor_skips_the_pipeline_loop(self, stub_web):
        started, _ = stub_web

        main.cmd_web(_args(no_monitor=True))

        assert started["server"], "UI must still serve"
        assert not started["monitor"], "monitor loop must not run"

    def test_default_still_runs_it(self, stub_web):
        started, _ = stub_web

        main.cmd_web(_args(no_monitor=False))

        assert started["server"]
        assert started["monitor"]

    def test_monitor_stays_registered_for_manual_trigger(self, stub_web):
        """/api/trigger is the deliberate, user-initiated single cycle."""
        _, set_monitor = stub_web

        main.cmd_web(_args(no_monitor=True))

        assert set_monitor.called


class TestComposeWiring:
    def test_web_service_passes_no_monitor(self):
        """The flag only helps if the deployed command actually uses it."""
        from pathlib import Path
        compose = Path(main.__file__).parent / "docker-compose.yml"
        text = compose.read_text(encoding="utf-8")
        web_block = text.split("  web:", 1)[1]
        assert "--no-monitor" in web_block.split("volumes:", 1)[0]
