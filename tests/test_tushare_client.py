"""The Tushare client: which token, which endpoint.

Both defects here were invisible in the worst way — the archive reported
"failed" for four endpoints while every table stayed empty, and the obvious
reading ("bad credentials") was wrong. Measured 2026-09-17:

| combination | result |
|---|---|
| operator token + official endpoint | 11 rows / 0.3s |
| operator token + shared endpoint   | 0 rows / 5.0s |
| shared token   + official endpoint | `您的token不对` |
| shared token   + shared endpoint   | 0 rows / 5.0s |

So the operator's token was always fine; the module pinned a dead community
proxy, and read a variable name `.env` does not use.
"""

from __future__ import annotations

import pytest

from alpha_agents.data import tushare_client as TC


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    """`get_pro` caches in a module global; a leak would make the second test
    assert against the first test's client."""
    monkeypatch.setattr(TC, "_pro", None, raising=False)
    for name in ("TUSHARE_TOKEN", "TUSHARE_API_KEY", "TUSHARE_HTTP_URL"):
        monkeypatch.delenv(name, raising=False)
    yield
    TC._pro = None


class TestWhichTokenIsUsed:
    def test_it_reads_the_name_the_env_file_actually_uses(self, monkeypatch):
        """The regression. `.env` writes TUSHARE_API_KEY; the module read
        TUSHARE_TOKEN, so the operator's token was ignored entirely."""
        monkeypatch.setenv("TUSHARE_API_KEY", "own-token")
        token, source = TC.configured_token()
        assert token == "own-token"
        assert source == "TUSHARE_API_KEY"

    def test_the_older_name_still_wins_when_both_are_set(self, monkeypatch):
        """Both names are in use across deployments; neither may break."""
        monkeypatch.setenv("TUSHARE_TOKEN", "primary")
        monkeypatch.setenv("TUSHARE_API_KEY", "secondary")
        token, source = TC.configured_token()
        assert token == "primary" and source == "TUSHARE_TOKEN"

    def test_it_falls_back_and_names_the_fallback(self, monkeypatch):
        """The fallback is a last resort, so the caller can log it rather
        than discover it from an empty table."""
        token, source = TC.configured_token()
        assert token == TC._DEFAULT_TOKEN
        assert "shared default" in source

    def test_whitespace_only_is_not_a_token(self, monkeypatch):
        """A `TUSHARE_API_KEY=` line left in `.env` must not shadow a real
        TUSHARE_TOKEN behind it."""
        monkeypatch.setenv("TUSHARE_API_KEY", "   ")
        monkeypatch.setenv("TUSHARE_TOKEN", "real")
        token, source = TC.configured_token()
        assert token == "real"


class TestWhichEndpointIsUsed:
    def test_the_default_is_the_official_endpoint(self):
        """The old default was a community proxy that answers every call with
        an empty frame after five seconds."""
        assert "121.40.135.59" not in TC._DEFAULT_URL
        assert "tushare" in TC._DEFAULT_URL

    def test_the_env_override_is_still_honoured(self, monkeypatch):
        """Only the *default* changed; an operator pointing at a proxy must
        still reach it, or the dead host would have become the only option."""
        monkeypatch.setenv("TUSHARE_TOKEN", "t")
        monkeypatch.setenv("TUSHARE_HTTP_URL", "http://proxy.example/")
        pro = TC.get_pro()
        assert getattr(pro, "_DataApi__http_url", None) == "http://proxy.example/"

    def test_it_does_not_pin_the_url_when_unset(self, monkeypatch):
        """Pinning the official host explicitly is redundant, and skipping it
        keeps the SDK's own default in play if Tushare ever moves."""
        monkeypatch.setenv("TUSHARE_TOKEN", "t")
        pro = TC.get_pro()
        assert "121.40.135.59" not in str(
            getattr(pro, "_DataApi__http_url", ""))

    def test_it_warns_when_running_on_the_shared_default(
            self, monkeypatch, caplog):
        """Silent degradation is how seven tables stayed empty unnoticed."""
        import logging
        monkeypatch.setenv("TUSHARE_TOKEN", "")
        monkeypatch.setenv("TUSHARE_API_KEY", "")
        with caplog.at_level(logging.WARNING):
            TC.get_pro()
        assert any("shared token" in r.message for r in caplog.records)


class TestTheArchiveIsScheduled:
    def test_daily_archive_is_registered(self):
        """It had a function and no registration: seven tables held zero rows
        with complete schemas and complete writers, because nothing ran it."""
        import re
        src = (TC.__file__.rsplit("alpha_agents", 1)[0]
               + "main.py")
        text = open(src, encoding="utf-8").read()
        assert 'Task(\n        "daily_archive"' in text or \
               '"daily_archive"' in text
        assert "run_daily_archive" in text
