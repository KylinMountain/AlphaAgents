"""The settings page's API: .env round-trips, write-only secrets, live tests."""

from __future__ import annotations

import asyncio
import os
import stat

import pytest
from fastapi.testclient import TestClient

from alpha_agents import env_file
from alpha_agents.server import settings_api as S

ENV = """# ── LLM ──
# 决策模型
AGENT_API_KEY=sk-real-secret-0123456789
AGENT_MODEL=old-model
# DIGEST_MODEL=commented-out
AGENT_MODEL=dead-duplicate

HARD_STOP_PCT=8.0
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(ENV, encoding="utf-8")
    os.chmod(p, 0o600)
    monkeypatch.setenv("ALPHAAGENTS_ENV_FILE", str(p))
    for k in ("AGENT_API_KEY", "AGENT_MODEL", "AGENT_BASE_URL", "SILICONFLOW_API_KEY",
              "HARD_STOP_PCT", "NOTIFY_FEISHU_WEBHOOK"):
        monkeypatch.delenv(k, raising=False)
    return p


@pytest.fixture()
def client():
    from alpha_agents.server.app import app
    return TestClient(app)


class TestTheFileIsRewrittenInPlace:
    def test_set_remove_append_keep_comments_and_mode(self, env):
        env_file.update_env({"AGENT_MODEL": "new-model", "HARD_STOP_PCT": None,
                             "TUSHARE_TOKEN": "t0k"}, env)
        text = env.read_text(encoding="utf-8")
        assert "AGENT_MODEL=new-model" in text
        assert "dead-duplicate" not in text, "a later duplicate would show a value not in effect"
        assert "HARD_STOP_PCT" not in text
        assert text.rstrip().endswith(f"{env_file.APPENDED_HEADER}\nTUSHARE_TOKEN=t0k")
        for comment in ("# ── LLM ──", "# 决策模型", "# DIGEST_MODEL=commented-out"):
            assert comment in text
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert (env.parent / ".env.bak-settings").read_text(encoding="utf-8") == ENV

    def test_a_value_cannot_inject_a_line(self, env):
        with pytest.raises(ValueError):
            env_file.update_env({"AGENT_MODEL": "x\nEVIL=1"}, env)
        assert env.read_text(encoding="utf-8") == ENV


class TestSecretsAreWriteOnly:
    def test_the_value_never_leaves(self, env, client):
        body = client.get("/api/settings").text
        assert "sk-real-secret-0123456789" not in body
        row = next(f for g in client.get("/api/settings").json()["groups"]
                   for f in g["fields"] if f["key"] == "AGENT_API_KEY")
        assert row["set"] and row["masked"] == "sk-…6789" and row["value"] == ""

    def test_a_blank_secret_is_left_alone(self, env, client):
        r = client.put("/api/settings", json={"values": {"AGENT_API_KEY": "",
                                                         "AGENT_MODEL": "m2"}})
        assert r.status_code == 200
        assert env_file.read_env(env)["AGENT_API_KEY"] == "sk-real-secret-0123456789"
        assert env_file.read_env(env)["AGENT_MODEL"] == "m2"

    def test_an_unmanaged_or_bad_value_is_refused(self, env, client):
        assert client.put("/api/settings", json={"values": {"PATH": "/x"}}).status_code == 400
        assert client.put("/api/settings",
                          json={"values": {"HARD_STOP_PCT": "eight"}}).status_code == 400
        assert env.read_text(encoding="utf-8") == ENV


def test_a_copied_placeholder_is_not_configured(env):
    env.write_text("AGENT_API_KEY=sk-xxx\n", encoding="utf-8")
    snap = S.snapshot()
    assert snap["onboarding"]["agent_configured"] is False
    assert S.is_placeholder("sk-ant-xxx") and not S.is_placeholder("sk-live-1234")


class TestTheModelTestShowsWhatCameBack:
    def test_the_reply_text(self, env, monkeypatch):
        seen = {}

        async def chat(key, url, model):
            seen.update(key=key, model=model)
            return "连接正常"
        monkeypatch.setattr(S, "_chat", chat)
        got = asyncio.run(S.test_llm("AGENT", {"AGENT_MODEL": "unsaved"}))
        assert got["ok"] and got["reply"] == "连接正常"
        assert seen == {"key": "sk-real-secret-0123456789", "model": "unsaved"}

    def test_the_provider_error(self, env, monkeypatch):
        async def chat(key, url, model):
            raise RuntimeError("Unsupported model (model=LongCat)")
        monkeypatch.setattr(S, "_chat", chat)
        got = asyncio.run(S.test_llm("AGENT"))
        assert not got["ok"] and "Unsupported model" in got["error"]

    def test_summary_inherits_digest(self, env):
        e = S.effective_llm("SUMMARY", {"DIGEST_API_KEY": "k", "DIGEST_MODEL": "cheap",
                                        "DIGEST_BASE_URL": "https://d"}, {})
        assert e["model"] == "cheap" and e["inherited"] == "DIGEST"


@pytest.fixture()
def traders(tmp_path, monkeypatch):
    from alpha_agents import config
    from alpha_agents.data import trader
    d = tmp_path / "traders"
    d.mkdir()
    monkeypatch.setattr(trader, "TRADERS_DIR", d)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return d


class TestTraders:
    def test_a_trader_round_trips_and_keeps_its_header(self, traders):
        (traders / "fast.yaml").write_text("# 我的说明\nid: fast\ncapital: 100\n",
                                           encoding="utf-8")
        S.save_trader("fast", {"name": "快手", "capital": "300000", "enabled": False,
                               "extra_prompt": "追强势", "tags": "a, b"})
        text = (traders / "fast.yaml").read_text(encoding="utf-8")
        assert text.startswith("# 我的说明\n")
        import yaml
        raw = yaml.safe_load(text)
        assert raw["capital"] == 300000 and raw["enabled"] is False
        assert raw["tags"] == ["a", "b"] and raw["extra_prompt"] == "追强势"

    def test_a_malformed_trader_is_refused_and_the_file_unchanged(self, traders):
        (traders / "fast.yaml").write_text("id: fast\n", encoding="utf-8")
        with pytest.raises(ValueError):
            S.save_trader("fast", {"capital": "lots"})
        with pytest.raises(ValueError):
            S.save_trader("default", {})
        with pytest.raises(ValueError):
            S.save_trader("fast", {"prompt_file": "../../etc/passwd"})
        assert (traders / "fast.yaml").read_text(encoding="utf-8") == "id: fast\n"

    def test_the_handbook_and_its_versions(self, traders):
        from alpha_agents.evolution import handbook
        hist = handbook._history("default")
        hist.mkdir(parents=True)
        (hist / "2026-01-05.md").write_text("R1 旧", encoding="utf-8")
        S.save_handbook("default", "# 我的交易守则\nR1 人改过")
        view = S.handbook_view("default")
        assert "人改过" in view["text"] and view["versions"] == ["2026-01-05"]
        assert S.handbook_version("default", "2026-01-05") == "R1 旧"
        with pytest.raises(ValueError):
            S.handbook_version("default", "../../x")
